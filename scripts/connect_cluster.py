#!/usr/bin/env python3
"""Open an explicitly requested SSH session to the Zhengzhou cluster."""

import argparse
import ast
import base64
import binascii
import gzip
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse


CLUSTER_NAME = "zhengzhou"
ZHENGZHOU_2FA_HOST = "42.228.13.241"
ZHENGZHOU_2FA_PORT = 65024
DEFAULT_DIRECT_PORT = 22
DEFAULT_CONNECT_TIMEOUT = 15
LOGIN_CHECK_COMMAND = "printf 'DCU_CLUSTER_LOGIN_OK\\n'"
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_SCRIPT_BYTES = 256 * 1024
MAX_REMOTE_COMMAND_CHARS = 24000
RESULT_WRITE_FAILURE = 74
LOCAL_FEISHU_REPORT_FAILURE = 76
REMOTE_FEISHU_SUMMARY_PREFIX = b"DCU_FEISHU_SUMMARY="
REMOTE_FEISHU_SUMMARY_MAX_BYTES = 16 * 1024
REMOTE_FEISHU_SUMMARY_MAX_COUNT = 64
LOCAL_FEISHU_RESULT_MAX_BYTES = 128 * 1024
REMOTE_FEISHU_EVIDENCE_KEYS = (
    "schema",
    "status",
    "model_name",
    "script_path",
    "framework",
    "card",
    "uses_kvcache_fp8",
    "script_sha256",
    "script_mode",
)
SUBPROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output"}
SCRIPT_INTERPRETERS = {
    "python3": "python3",
    "bash": "bash",
}


@dataclass(frozen=True)
class ConnectionSpec:
    method: str
    host: str
    port: int
    user: str
    connect_timeout: int
    remote_command: str = ""
    action: str = "interactive"


def parse_ip(value):
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("host must be a valid IPv4 or IPv6 address") from exc


def parse_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def parse_timeout(value):
    try:
        timeout = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("connect timeout must be an integer") from exc
    if not 1 <= timeout <= 300:
        raise argparse.ArgumentTypeError("connect timeout must be between 1 and 300 seconds")
    return timeout


def parse_username(value):
    value = value.strip()
    if not USERNAME_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "username may contain only letters, digits, dot, underscore, and hyphen"
        )
    return value


def _python36_tree(source, filename):
    try:
        return ast.parse(source, filename=filename, feature_version=(3, 6))
    except TypeError:
        return ast.parse(source, filename=filename, feature_version=6)


def validate_remote_python36(source, filename="<remote-script>"):
    try:
        tree = _python36_tree(source, filename)
    except SyntaxError as exc:
        raise ValueError(
            "remote Python script must use Python 3.6-compatible syntax: {}".format(exc)
        ) from exc

    errors = []
    subprocess_modules = set()
    subprocess_functions = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    subprocess_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess":
                for alias in node.names:
                    if alias.name == "*":
                        errors.append("subprocess star imports cannot be compatibility-checked")
                    elif alias.name in SUBPROCESS_CALLS:
                        subprocess_functions.add(alias.asname or alias.name)
            if node.module == "__future__" and any(
                alias.name == "annotations" for alias in node.names
            ):
                errors.append("from __future__ import annotations requires Python 3.7+")

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module_names = []
            if isinstance(node, ast.Import):
                module_names = [alias.name for alias in node.names]
            elif node.module:
                module_names = [node.module]
            if any(name == "dataclasses" or name.startswith("dataclasses.") for name in module_names):
                errors.append("dataclasses is not part of Python 3.6")

        if not isinstance(node, ast.Call):
            continue
        keyword_names = {keyword.arg for keyword in node.keywords if keyword.arg}
        subprocess_call = False
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in subprocess_modules
            and node.func.attr in SUBPROCESS_CALLS
        ):
            subprocess_call = True
        elif isinstance(node.func, ast.Name) and node.func.id in subprocess_functions:
            subprocess_call = True
        if subprocess_call:
            if any(keyword.arg is None for keyword in node.keywords):
                errors.append("subprocess **kwargs cannot be compatibility-checked")
            if "text" in keyword_names:
                errors.append("subprocess text= is unavailable; use universal_newlines=True")
            if "capture_output" in keyword_names:
                errors.append(
                    "subprocess capture_output= is unavailable; use stdout/stderr=subprocess.PIPE"
                )
        call_name = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if call_name == "unlink" and "missing_ok" in keyword_names:
            errors.append("Path.unlink(missing_ok=...) is unavailable")
        if call_name == "is_relative_to":
            errors.append("Path.is_relative_to() is unavailable")

    if errors:
        raise ValueError(
            "remote Python 3.6 compatibility check failed: {}".format(
                "; ".join(sorted(set(errors)))
            )
        )


def build_script_command(script_path, interpreter):
    path = Path(script_path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError("script file must be a regular file, not a symlink")
    content = path.read_bytes()
    if not content:
        raise ValueError("script file must not be empty")
    if len(content) > MAX_SCRIPT_BYTES:
        raise ValueError(
            "script file is too large: {} bytes (maximum {})".format(
                len(content), MAX_SCRIPT_BYTES
            )
        )
    if b"\x00" in content:
        raise ValueError("script file must not contain NUL bytes")
    try:
        source = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("script file must be valid UTF-8") from exc
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    content = source.encode("utf-8")
    if interpreter == "python3":
        validate_remote_python36(source, filename=str(path))

    payload = base64.b64encode(gzip.compress(content)).decode("ascii")
    remote_base = "/tmp/dcu_workflow_{}".format(uuid.uuid4().hex)
    command = (
        "umask 077; p={base}; "
        "printf %s {payload} | base64 -d > ${{p}}.gz && "
        "gzip -dc ${{p}}.gz > ${{p}}.src && "
        "{interpreter} ${{p}}.src; "
        "rc=$?; rm -f ${{p}}.gz ${{p}}.src; exit $rc"
    ).format(
        base=remote_base,
        payload=payload,
        interpreter=SCRIPT_INTERPRETERS[interpreter],
    )
    if len(command) > MAX_REMOTE_COMMAND_CHARS:
        raise ValueError(
            "compressed remote script command is too large: {} characters (maximum {})".format(
                len(command), MAX_REMOTE_COMMAND_CHARS
            )
        )
    return command


def action_from_args(args):
    if args.check:
        return "check"
    if args.command is not None:
        return "command"
    if args.script_file is not None:
        return "script_file"
    return "interactive"


def validate_local_feishu_args(args):
    if not args.local_feishu_report:
        return
    if args.script_file is None:
        raise ValueError("--local-feishu-report requires --script-file")
    if not args.output_file or not args.result_file:
        raise ValueError(
            "--local-feishu-report requires unique --output-file and --result-file paths"
        )


def local_feishu_paths():
    skill_root = Path(__file__).resolve().parents[1]
    return skill_root / "scripts" / "report_to_feishu.py", skill_root / "assets" / "feishu.json"


def validate_local_table_url(table_url, table_type):
    parsed = urlparse(table_url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError("local Feishu table_url must be an absolute HTTPS URL")
    parts = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)
    detected_type = ""
    if "base" in parts:
        index = parts.index("base")
        if len(parts) <= index + 1 or not query.get("table", [""])[0]:
            raise ValueError("local Feishu bitable URL must include app and table identifiers")
        detected_type = "bitable"
    elif "sheets" in parts:
        index = parts.index("sheets")
        if len(parts) <= index + 1:
            raise ValueError("local Feishu sheets URL must include a spreadsheet token")
        detected_type = "sheets"
    elif "wiki" in parts:
        index = parts.index("wiki")
        if len(parts) <= index + 1:
            raise ValueError("local Feishu wiki URL must include a node token")
    else:
        raise ValueError("local Feishu table_url must contain /base/, /sheets/, or /wiki/")
    if detected_type and detected_type != table_type:
        raise ValueError("local Feishu table_type conflicts with table_url")


def validate_local_feishu_runtime():
    reporter_path, config_path = local_feishu_paths()
    if reporter_path.is_symlink() or not reporter_path.is_file():
        raise ValueError("local Feishu reporter must be a regular file")
    if config_path.is_symlink() or not config_path.is_file():
        raise ValueError("local Feishu config must be a regular file")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("local Feishu config must be valid UTF-8 JSON")
    if not isinstance(config, dict):
        raise ValueError("local Feishu config must be a JSON object")
    required = ("app_id", "app_secret", "recipient_id", "table_url")
    missing = []
    for key in required:
        value = str(config.get(key, "")).strip()
        if not value or (value.startswith("<") and value.endswith(">")):
            missing.append(key)
    if missing:
        raise ValueError("local Feishu config is incomplete: " + ", ".join(missing))
    table_type = str(config.get("table_type", "")).strip().lower()
    if table_type not in {"sheets", "bitable"}:
        raise ValueError("local Feishu config table_type is unsupported")
    if str(config.get("recipient_id_type", "open_id")).strip().lower() not in {
        "open_id",
        "user_id",
        "union_id",
        "email",
        "chat_id",
    }:
        raise ValueError("local Feishu config recipient_id_type is unsupported")
    validate_local_table_url(str(config.get("table_url", "")).strip(), table_type)


def extract_remote_feishu_summaries(output_path):
    if not output_path or output_path.is_symlink() or not output_path.is_file():
        raise ValueError("remote stdout file is missing or unsafe")
    markers = []
    with output_path.open("rb") as handle:
        for line in handle:
            stripped = line.rstrip(b"\r\n")
            if stripped.startswith(REMOTE_FEISHU_SUMMARY_PREFIX):
                encoded = stripped[len(REMOTE_FEISHU_SUMMARY_PREFIX) :]
                if len(encoded) > ((REMOTE_FEISHU_SUMMARY_MAX_BYTES + 2) // 3) * 4:
                    raise ValueError("remote Feishu summary marker is oversized")
                markers.append(encoded)
                if len(markers) > REMOTE_FEISHU_SUMMARY_MAX_COUNT:
                    raise ValueError("remote workflow emitted too many Feishu summaries")
    if not markers:
        raise ValueError(
            "remote workflow must emit at least one DCU_FEISHU_SUMMARY marker"
        )

    summaries = []
    seen = set()
    for marker in markers:
        try:
            raw = base64.b64decode(marker, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("remote Feishu summary marker is not valid Base64")
        if base64.b64encode(raw) != marker:
            raise ValueError("remote Feishu summary marker is not canonical Base64")
        if not raw or len(raw) > REMOTE_FEISHU_SUMMARY_MAX_BYTES or b"\x00" in raw:
            raise ValueError("decoded remote Feishu summary is empty, oversized, or contains NUL")
        if raw in seen:
            raise ValueError("remote workflow emitted a duplicate Feishu summary")
        seen.add(raw)
        summaries.append(raw)
    return summaries


def extract_remote_feishu_summary(output_path):
    summaries = extract_remote_feishu_summaries(output_path)
    if len(summaries) != 1:
        raise ValueError(
            "remote workflow must emit exactly one DCU_FEISHU_SUMMARY marker; found {}".format(
                len(summaries)
            )
        )
    return summaries[0]


def validate_local_feishu_dry_run(data):
    if not isinstance(data, dict) or data.get("dry_run") is not True:
        raise ValueError("local Feishu reporter did not validate the remote summary")
    summary = data.get("summary") or {}
    if summary.get("schema") != "dcu.feishu.summary/v1" or summary.get("status") != "validated":
        raise ValueError("local Feishu validation lost remote evidence")
    return summary


def validate_remote_evidence(summary, expected_summary):
    if any(
        summary.get(key) != expected_summary.get(key)
        for key in REMOTE_FEISHU_EVIDENCE_KEYS
    ):
        raise ValueError("local Feishu result does not match the validated remote summary")


def environment_without_feishu():
    environment = os.environ.copy()
    for key in list(environment):
        if key.upper().startswith("FEISHU_"):
            del environment[key]
    return environment


def validate_local_feishu_result(data, expected_summary=None):
    if not isinstance(data, dict) or data.get("status") != "reported":
        raise ValueError("local Feishu reporter did not return status=reported")
    summary = data.get("summary") or {}
    if summary.get("schema") != "dcu.feishu.summary/v1" or summary.get("status") != "validated":
        raise ValueError("local Feishu result lost remote validation evidence")
    table = data.get("table") or {}
    message = data.get("message") or {}
    if table.get("type") not in {"sheets", "bitable"}:
        raise ValueError("local Feishu result table type is unsupported")
    if table.get("action") not in {"created", "updated"}:
        raise ValueError("local Feishu result has no upsert action")
    if table.get("type") == "sheets" and not table.get("updated_range"):
        raise ValueError("local Feishu sheets result has no updated_range")
    if table.get("type") == "bitable" and not table.get("record_id"):
        raise ValueError("local Feishu bitable result has no record_id")
    if not message.get("message_id"):
        raise ValueError("local Feishu robot result has no message_id")
    attempts = data.get("attempts")
    retries = data.get("retries")
    if (
        type(attempts) is not int
        or type(retries) is not int
        or not 1 <= attempts <= 4
        or retries != attempts - 1
    ):
        raise ValueError("local Feishu retry evidence is invalid")
    if expected_summary is not None:
        validate_remote_evidence(summary, expected_summary)


def run_local_feishu_report(summary_raw, dry_run=False):
    reporter_path, _config_path = local_feishu_paths()
    command = [sys.executable, str(reporter_path), "--summary-stdin"]
    if dry_run:
        command.append("--dry-run")
    reporter_environment = environment_without_feishu()
    reporter_environment["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        command,
        input=summary_raw,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=reporter_environment,
    )
    if completed.returncode != 0:
        error = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise ValueError("local Feishu reporter failed: " + (error[-1500:] or "unknown error"))
    if len(completed.stdout or b"") > LOCAL_FEISHU_RESULT_MAX_BYTES:
        raise ValueError("local Feishu reporter result is oversized")
    try:
        data = json.loads((completed.stdout or b"").decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("local Feishu reporter returned invalid JSON")
    try:
        expected_summary = json.loads(summary_raw.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, ValueError):
        raise ValueError("validated remote summary could not be compared locally")
    if dry_run:
        validate_remote_evidence(validate_local_feishu_dry_run(data), expected_summary)
    else:
        validate_local_feishu_result(data, expected_summary)
    return data


def local_path(value):
    return Path(value).expanduser().resolve() if value else None


def prepare_local_files(args):
    script_path = local_path(args.script_file)
    output_path = local_path(args.output_file)
    result_path = local_path(args.result_file)
    named_paths = [path for path in (script_path, output_path, result_path) if path]
    if len({str(path).casefold() for path in named_paths}) != len(named_paths):
        raise ValueError("script, output, and result files must use different paths")
    for path in (output_path, result_path):
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_dir():
                raise ValueError("output and result paths must be files")
    for path in (output_path, result_path):
        if path and path.exists():
            path.unlink()
    return output_path, result_path


def base_result_from_args(args, output_path):
    if args.method == "2fa":
        host = ZHENGZHOU_2FA_HOST
        port = ZHENGZHOU_2FA_PORT
    else:
        host = args.host
        port = args.port
    return {
        "action": action_from_args(args),
        "cluster": CLUSTER_NAME,
        "host": host,
        "method": args.method,
        "local_feishu_report": bool(args.local_feishu_report),
        "output_file": str(output_path) if output_path else "",
        "port": port,
        "user": args.user or "ssh_config_or_local_user",
    }


def write_result_file(path, result):
    if not path:
        return
    temporary = path.with_name("{}.tmp.{}".format(path.name, os.getpid()))
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def publish_result_file(path, result):
    try:
        write_result_file(path, result)
        return True
    except OSError as exc:
        print("Failed to publish local result file: {}".format(exc), file=sys.stderr)
        return False


def find_ssh_executable():
    if os.name == "nt":
        windows_root = os.environ.get("WINDIR", r"C:\Windows")
        system_ssh = Path(windows_root) / "System32" / "OpenSSH" / "ssh.exe"
        if system_ssh.is_file():
            return str(system_ssh)
    return shutil.which("ssh")


def add_execution_options(parser):
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--check",
        action="store_true",
        help="Authenticate, print a fixed success marker, and disconnect.",
    )
    action.add_argument(
        "--command",
        help="Run one non-secret remote shell command after authentication.",
    )
    action.add_argument(
        "--script-file",
        help=(
            "Compress and execute one local UTF-8 workflow file after authentication. "
            "Use this for multi-step work instead of nested shell quoting."
        ),
    )
    parser.add_argument(
        "--script-interpreter",
        choices=sorted(SCRIPT_INTERPRETERS),
        default="python3",
        help="Remote interpreter for --script-file (default: python3).",
    )
    parser.add_argument(
        "--output-file",
        help="Capture remote stdout in this local file; authentication prompts stay visible.",
    )
    parser.add_argument(
        "--result-file",
        help="Atomically write the final non-secret connection result as JSON.",
    )
    parser.add_argument(
        "--connect-timeout",
        type=parse_timeout,
        default=DEFAULT_CONNECT_TIMEOUT,
    )
    parser.add_argument(
        "--local-feishu-report",
        action="store_true",
        help=(
            "After a successful --script-file workflow, consume 1-64 validated summaries "
            "and report each with this local skill's fixed Feishu config."
        ),
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Connect to the Zhengzhou cluster only after the user explicitly requests it. "
            "Passwords and verification codes are entered at OpenSSH prompts."
        )
    )
    subparsers = parser.add_subparsers(dest="method", required=True)

    direct = subparsers.add_parser("direct", help="Use a user-provided direct IP.")
    direct.add_argument("--host", required=True, type=parse_ip)
    direct.add_argument("--port", type=parse_port, default=DEFAULT_DIRECT_PORT)
    direct.add_argument("--user", type=parse_username)
    add_execution_options(direct)

    two_factor = subparsers.add_parser(
        "2fa", help="Use the fixed Zhengzhou two-factor gateway."
    )
    two_factor.add_argument("--user", required=True, type=parse_username)
    add_execution_options(two_factor)
    return parser


def spec_from_args(args):
    validate_local_feishu_args(args)
    action = action_from_args(args)
    if args.check:
        command = LOGIN_CHECK_COMMAND
    elif args.command is not None:
        if not args.command.strip():
            raise ValueError("--command must not be empty")
        command = args.command
    elif args.script_file is not None:
        command = build_script_command(args.script_file, args.script_interpreter)
    else:
        command = ""
    if args.method == "2fa":
        return ConnectionSpec(
            method="2fa",
            host=ZHENGZHOU_2FA_HOST,
            port=ZHENGZHOU_2FA_PORT,
            user=args.user,
            connect_timeout=args.connect_timeout,
            remote_command=command,
            action=action,
        )
    return ConnectionSpec(
        method="direct",
        host=args.host,
        port=args.port,
        user=args.user or "",
        connect_timeout=args.connect_timeout,
        remote_command=command,
        action=action,
    )


def build_ssh_command(ssh_executable, spec):
    command = [
        ssh_executable,
        "-o",
        "ForwardAgent=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "StrictHostKeyChecking=ask",
        "-o",
        "ConnectTimeout={}".format(spec.connect_timeout),
        "-p",
        str(spec.port),
    ]
    if spec.method == "2fa":
        command.extend(
            [
                "-tt",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "KbdInteractiveAuthentication=yes",
                "-o",
                "PasswordAuthentication=yes",
                "-o",
                "PreferredAuthentications=keyboard-interactive,password",
            ]
        )
    target = "{}@{}".format(spec.user, spec.host) if spec.user else spec.host
    command.append(target)
    if spec.remote_command:
        command.append(spec.remote_command)
    return command


def main(argv=None):
    args = build_parser().parse_args(argv)
    output_path = local_path(args.output_file)
    result_path = local_path(args.result_file)
    base_result = base_result_from_args(args, output_path)
    try:
        validate_local_feishu_args(args)
        output_path, result_path = prepare_local_files(args)
        spec = spec_from_args(args)
        if args.local_feishu_report:
            validate_local_feishu_runtime()
    except (OSError, ValueError) as exc:
        print("Cluster workflow preflight failed: {}".format(exc), file=sys.stderr)
        if result_path and result_path != output_path:
            failed_result = dict(base_result)
            failed_result.update(
                {"status": "preflight_failed", "exit_code": 2, "error": str(exc)}
            )
            if not publish_result_file(result_path, failed_result):
                return RESULT_WRITE_FAILURE
        return 2

    base_result["action"] = spec.action

    if spec.method == "2fa" and not sys.stdin.isatty():
        print(
            "Two-factor login requires a visible interactive terminal. "
            "Enter the password and current verification code only at OpenSSH prompts.",
            file=sys.stderr,
        )
        failed_result = dict(base_result, status="failed", exit_code=2)
        if not publish_result_file(result_path, failed_result):
            return RESULT_WRITE_FAILURE
        return 2

    ssh_executable = find_ssh_executable()
    if not ssh_executable:
        print("OpenSSH client 'ssh' was not found on PATH.", file=sys.stderr)
        failed_result = dict(base_result, status="failed", exit_code=127)
        if not publish_result_file(result_path, failed_result):
            return RESULT_WRITE_FAILURE
        return 127

    print(
        "Connecting to {cluster} via {method}: {host}:{port}".format(
            cluster=CLUSTER_NAME,
            method=spec.method,
            host=spec.host,
            port=spec.port,
        ),
        file=sys.stderr,
    )
    output_handle = None
    try:
        if output_path:
            output_handle = output_path.open("wb")
        completed = subprocess.run(
            build_ssh_command(ssh_executable, spec),
            check=False,
            stdout=output_handle,
            env=environment_without_feishu(),
        )
        exit_code = completed.returncode
        error = ""
    except OSError as exc:
        exit_code = 126
        error = "failed to start or monitor OpenSSH: {}".format(exc)
    except KeyboardInterrupt:
        exit_code = 130
        error = "connection interrupted locally"
    finally:
        if output_handle:
            output_handle.close()

    remote_exit_code = exit_code
    validated_summaries = []
    feishu_results = []
    feishu_failures = []
    stage = ""
    if exit_code == 0 and args.local_feishu_report:
        try:
            summary_payloads = extract_remote_feishu_summaries(output_path)
            summary_identities = set()
            summary_report_keys = set()
            for summary_raw in summary_payloads:
                dry_run_result = run_local_feishu_report(summary_raw, dry_run=True)
                validated_summary = dry_run_result["summary"]
                identity = validated_summary.get("script_path")
                if identity in summary_identities:
                    raise ValueError("remote workflow emitted the same script summary more than once")
                summary_identities.add(identity)
                report_key = (
                    str(validated_summary.get("framework", "")).strip().casefold(),
                    str(validated_summary.get("model_name", "")).strip().casefold(),
                    re.sub(
                        r"[^a-z0-9]",
                        "",
                        str(validated_summary.get("card", "")).strip().casefold(),
                    ),
                )
                if report_key in summary_report_keys:
                    raise ValueError(
                        "remote workflow emitted duplicate model-and-card report keys"
                    )
                summary_report_keys.add(report_key)
                validated_summaries.append(validated_summary)
        except (OSError, ValueError) as exc:
            exit_code = LOCAL_FEISHU_REPORT_FAILURE
            error = str(exc)
            stage = "remote_summary_validation"

        if exit_code == 0:
            for index, summary_raw in enumerate(summary_payloads):
                try:
                    feishu_results.append(run_local_feishu_report(summary_raw))
                except (OSError, ValueError) as exc:
                    summary = validated_summaries[index]
                    feishu_failures.append(
                        {
                            "index": index,
                            "model_name": summary.get("model_name"),
                            "script_path": summary.get("script_path"),
                            "framework": summary.get("framework"),
                            "card": summary.get("card"),
                            "error": str(exc),
                        }
                    )
            if feishu_failures:
                exit_code = LOCAL_FEISHU_REPORT_FAILURE
                error = "{} of {} local Feishu reports failed".format(
                    len(feishu_failures), len(summary_payloads)
                )
                stage = "local_feishu_report"

    result = dict(
        base_result,
        status="completed" if exit_code == 0 else "failed",
        exit_code=exit_code,
    )
    if args.local_feishu_report:
        result["remote_exit_code"] = remote_exit_code
    if validated_summaries:
        result["remote_summaries"] = validated_summaries
    if feishu_results:
        result["feishu_reports"] = feishu_results
        if len(validated_summaries) == 1 and len(feishu_results) == 1:
            result["feishu"] = feishu_results[0]
    if feishu_failures:
        result["feishu_failures"] = feishu_failures
    if stage:
        result["stage"] = stage
    if error:
        result["error"] = error
    if not publish_result_file(result_path, result):
        return RESULT_WRITE_FAILURE
    if exit_code != 0:
        if stage in {"remote_summary_validation", "local_feishu_report"}:
            print(
                "Remote workflow succeeded, but local Feishu closure failed with exit code {}. {}".format(
                    exit_code, error
                ),
                file=sys.stderr,
            )
        elif spec.remote_command and not error and remote_exit_code != 255:
            remote_action = "workflow" if spec.action == "script_file" else "command"
            print(
                "SSH login succeeded, but the remote {} failed with exit code {}.".format(
                    remote_action, remote_exit_code
                ),
                file=sys.stderr,
            )
        else:
            print(
                "SSH login or connection failed with exit code {}.{}".format(
                    exit_code, " " + error if error else ""
                ),
                file=sys.stderr,
            )
        return exit_code

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
