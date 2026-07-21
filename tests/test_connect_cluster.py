import base64
import gzip
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "connect_cluster.py"
SPEC = importlib.util.spec_from_file_location("connect_cluster", MODULE_PATH)
CONNECTOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CONNECTOR
SPEC.loader.exec_module(CONNECTOR)


class ParserTests(unittest.TestCase):
    def test_two_factor_endpoint_is_fixed(self):
        args = CONNECTOR.build_parser().parse_args(
            ["2fa", "--user", "wanghy18", "--check"]
        )
        spec = CONNECTOR.spec_from_args(args)
        self.assertEqual(spec.host, "42.228.13.241")
        self.assertEqual(spec.port, 65024)
        self.assertEqual(spec.remote_command, CONNECTOR.LOGIN_CHECK_COMMAND)

    def test_direct_mode_uses_user_ip_and_default_port(self):
        args = CONNECTOR.build_parser().parse_args(
            ["direct", "--host", "10.20.30.40", "--command", "hostname"]
        )
        spec = CONNECTOR.spec_from_args(args)
        self.assertEqual(spec.host, "10.20.30.40")
        self.assertEqual(spec.port, 22)
        self.assertEqual(spec.remote_command, "hostname")

    def test_direct_host_must_be_an_ip_address(self):
        with self.assertRaises(SystemExit):
            CONNECTOR.build_parser().parse_args(
                ["direct", "--host", "example.com", "--check"]
            )

    def test_secret_options_are_not_supported(self):
        with self.assertRaises(SystemExit):
            CONNECTOR.build_parser().parse_args(
                ["2fa", "--user", "wanghy18", "--password", "secret"]
            )

    def test_script_file_is_a_mutually_exclusive_action(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "workflow.py"
            script.write_text("print('ok')\n", encoding="utf-8")
            args = CONNECTOR.build_parser().parse_args(
                ["2fa", "--user", "wanghy18", "--script-file", str(script)]
            )
            spec = CONNECTOR.spec_from_args(args)
        self.assertEqual(spec.action, "script_file")
        self.assertIn("| base64 -d > ${p}.gz", spec.remote_command)
        self.assertIn("gzip -dc ${p}.gz > ${p}.src", spec.remote_command)
        self.assertIn("python3 ${p}.src", spec.remote_command)
        self.assertNotIn("'", spec.remote_command)
        self.assertNotIn('"', spec.remote_command)

    def test_local_feishu_report_requires_script_output_and_result(self):
        args = CONNECTOR.build_parser().parse_args(
            ["2fa", "--user", "wanghy18", "--check", "--local-feishu-report"]
        )
        with self.assertRaisesRegex(ValueError, "requires --script-file"):
            CONNECTOR.spec_from_args(args)

    def test_command_and_script_file_cannot_be_combined(self):
        with self.assertRaises(SystemExit):
            CONNECTOR.build_parser().parse_args(
                [
                    "2fa",
                    "--user",
                    "wanghy18",
                    "--command",
                    "hostname",
                    "--script-file",
                    "workflow.py",
                ]
            )

    def test_empty_command_is_rejected(self):
        args = CONNECTOR.build_parser().parse_args(
            ["direct", "--host", "10.20.30.40", "--command", ""]
        )
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            CONNECTOR.spec_from_args(args)


class ScriptPayloadTests(unittest.TestCase):
    def test_remote_summary_reporter_remains_python36_compatible(self):
        reporter_path = MODULE_PATH.parent / "report_to_feishu.py"
        CONNECTOR.validate_remote_python36(
            reporter_path.read_text(encoding="utf-8"), filename=str(reporter_path)
        )

    def test_script_payload_round_trips_without_shell_quotes(self):
        source = b"print('hello from remote')\n"
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "workflow.py"
            script.write_bytes(source)
            command = CONNECTOR.build_script_command(script, "python3")
        payload = re.search(r"printf %s ([A-Za-z0-9+/=]+) \|", command).group(1)
        self.assertEqual(gzip.decompress(base64.b64decode(payload)), source)
        self.assertIn("&& gzip -dc", command)
        self.assertIn("&& python3", command)
        self.assertNotIn("'", command)
        self.assertNotIn('"', command)

    def test_python36_preflight_rejects_subprocess_text_keyword(self):
        source = "import subprocess\nsubprocess.run(['true'], text=True)\n"
        with self.assertRaisesRegex(ValueError, "universal_newlines"):
            CONNECTOR.validate_remote_python36(source)

    def test_python36_preflight_rejects_capture_output_keyword(self):
        source = "import subprocess\nsubprocess.run(['true'], capture_output=True)\n"
        with self.assertRaisesRegex(ValueError, "stdout/stderr"):
            CONNECTOR.validate_remote_python36(source)

    def test_python36_preflight_accepts_compatible_subprocess_usage(self):
        source = (
            "import subprocess\n"
            "subprocess.run(['true'], stdout=subprocess.PIPE, "
            "stderr=subprocess.PIPE, universal_newlines=True)\n"
        )
        CONNECTOR.validate_remote_python36(source)

    def test_python36_preflight_rejects_future_annotations(self):
        with self.assertRaisesRegex(ValueError, "Python 3.7"):
            CONNECTOR.validate_remote_python36(
                "from __future__ import annotations\nprint('ok')\n"
            )

    def test_python36_preflight_tracks_subprocess_aliases(self):
        source = "from subprocess import run as invoke\ninvoke(['true'], text=True)\n"
        with self.assertRaisesRegex(ValueError, "universal_newlines"):
            CONNECTOR.validate_remote_python36(source)

    def test_python36_preflight_rejects_subprocess_dynamic_kwargs(self):
        source = "import subprocess\noptions = {}\nsubprocess.run(['true'], **options)\n"
        with self.assertRaisesRegex(ValueError, "cannot be compatibility-checked"):
            CONNECTOR.validate_remote_python36(source)

    def test_python36_preflight_does_not_reject_unrelated_run_method(self):
        source = "class Runner:\n    def run(self, text=None):\n        return text\nRunner().run(text='ok')\n"
        CONNECTOR.validate_remote_python36(source)

    def test_script_payload_normalizes_utf8_bom_and_crlf(self):
        source = b"\xef\xbb\xbfprint('ok')\r\n"
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "workflow.py"
            script.write_bytes(source)
            command = CONNECTOR.build_script_command(script, "python3")
        payload = re.search(r"printf %s ([A-Za-z0-9+/=]+) \|", command).group(1)
        self.assertEqual(gzip.decompress(base64.b64decode(payload)), b"print('ok')\n")

    def test_script_file_rejects_nul_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "workflow.py"
            script.write_bytes(b"print('ok')\x00\n")
            with self.assertRaisesRegex(ValueError, "NUL"):
                CONNECTOR.build_script_command(script, "python3")

    def test_high_entropy_script_respects_windows_command_limit(self):
        random_comment = base64.b64encode(os.urandom(90000)).decode("ascii")
        source = ("# " + random_comment + "\nprint('ok')\n").encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "workflow.py"
            script.write_bytes(source)
            with self.assertRaisesRegex(ValueError, "remote script command is too large"):
                CONNECTOR.build_script_command(script, "python3")

    def test_full_windows_command_line_stays_below_createprocess_limit(self):
        source = ("# repeated workflow body\n" * 1000 + "print('ok')\n").encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "workflow.py"
            script.write_bytes(source)
            remote_command = CONNECTOR.build_script_command(script, "python3")
        spec = CONNECTOR.ConnectionSpec(
            method="2fa",
            host="42.228.13.241",
            port=65024,
            user="wanghy18",
            connect_timeout=15,
            remote_command=remote_command,
            action="script_file",
        )
        command_line = subprocess.list2cmdline(CONNECTOR.build_ssh_command("ssh.exe", spec))
        self.assertLess(len(command_line), 32767)


class CommandTests(unittest.TestCase):
    def test_two_factor_ssh_options_and_command(self):
        spec = CONNECTOR.ConnectionSpec(
            method="2fa",
            host="42.228.13.241",
            port=65024,
            user="wanghy18",
            connect_timeout=15,
            remote_command="hostname",
        )
        command = CONNECTOR.build_ssh_command("ssh", spec)
        self.assertIn("StrictHostKeyChecking=ask", command)
        self.assertIn("PubkeyAuthentication=no", command)
        self.assertIn("PreferredAuthentications=keyboard-interactive,password", command)
        self.assertIn("wanghy18@42.228.13.241", command)
        self.assertEqual(command[-1], "hostname")

    def test_direct_interactive_shell_has_no_remote_command(self):
        spec = CONNECTOR.ConnectionSpec(
            method="direct",
            host="10.20.30.40",
            port=22,
            user="",
            connect_timeout=15,
        )
        command = CONNECTOR.build_ssh_command("ssh", spec)
        self.assertEqual(command[-1], "10.20.30.40")


class LocalFeishuBranchTests(unittest.TestCase):
    @staticmethod
    def remote_summary_bytes():
        return json.dumps(
            {
                "schema": "dcu.feishu.summary/v1",
                "status": "validated",
                "model_name": "Qwen3-32B",
                "script_path": "/public/home/test/cookbook/serve-scripts/sglang-0.5.10-single-node/serve_sglang_qwen3-32b_bw1100_2x.sh",
                "framework": "sglang",
                "card": "BW1100",
                "uses_kvcache_fp8": False,
                "script_sha256": "a" * 64,
                "script_mode": "0766",
            }
        ).encode("utf-8")

    @staticmethod
    def reported_result():
        remote = json.loads(LocalFeishuBranchTests.remote_summary_bytes().decode("utf-8"))
        remote.update(
            {
                "timestamp_iso": "2026-07-20T12:00:00+08:00",
                "timestamp_ms": 1784510400000,
            }
        )
        return {
            "status": "reported",
            "summary": remote,
            "table": {
                "type": "sheets",
                "action": "updated",
                "updated_range": "sglang!A2:E2",
            },
            "message": {"message_id": "om_test"},
            "attempts": 1,
            "retries": 0,
        }

    @staticmethod
    def dry_run_result():
        return {"dry_run": True, "summary": LocalFeishuBranchTests.reported_result()["summary"]}

    def test_local_table_url_is_checked_before_authentication(self):
        CONNECTOR.validate_local_table_url(
            "https://example.feishu.cn/sheets/sht_test", "sheets"
        )
        with self.assertRaisesRegex(ValueError, "conflicts"):
            CONNECTOR.validate_local_table_url(
                "https://example.feishu.cn/base/bascn_test?table=tbl_test", "sheets"
            )

    def test_extracts_exactly_one_base64_summary(self):
        marker = CONNECTOR.REMOTE_FEISHU_SUMMARY_PREFIX + base64.b64encode(
            self.remote_summary_bytes()
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "remote.stdout"
            output.write_bytes(b"step\r\n" + marker + b"\r\n")
            extracted = CONNECTOR.extract_remote_feishu_summary(output)
        self.assertEqual(extracted, self.remote_summary_bytes())

    def test_duplicate_summary_markers_are_rejected(self):
        marker = CONNECTOR.REMOTE_FEISHU_SUMMARY_PREFIX + base64.b64encode(
            self.remote_summary_bytes()
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "remote.stdout"
            output.write_bytes(marker + b"\n" + marker + b"\n")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                CONNECTOR.extract_remote_feishu_summary(output)

    def test_multiple_unique_summaries_are_supported_for_batch_generation(self):
        first = self.remote_summary_bytes()
        second_data = json.loads(first.decode("utf-8"))
        second_data["card"] = "BW1000"
        second_data["script_path"] = second_data["script_path"].replace("bw1100", "bw1000")
        second = json.dumps(second_data).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "remote.stdout"
            output.write_bytes(
                CONNECTOR.REMOTE_FEISHU_SUMMARY_PREFIX
                + base64.b64encode(first)
                + b"\n"
                + CONNECTOR.REMOTE_FEISHU_SUMMARY_PREFIX
                + base64.b64encode(second)
                + b"\n"
            )
            extracted = CONNECTOR.extract_remote_feishu_summaries(output)
        self.assertEqual(extracted, [first, second])

    def test_local_reporter_receives_stdin_and_clean_environment(self):
        completed = subprocess.CompletedProcess(
            ["reporter"], 0, stdout=json.dumps(self.reported_result()).encode("utf-8"), stderr=b""
        )
        with mock.patch.dict(os.environ, {"FEISHU_APP_ID": "wrong", "KEEP_ME": "yes"}), mock.patch.object(
            CONNECTOR.subprocess, "run", return_value=completed
        ) as invoke:
            result = CONNECTOR.run_local_feishu_report(self.remote_summary_bytes())
        self.assertEqual(result["status"], "reported")
        kwargs = invoke.call_args.kwargs
        self.assertEqual(kwargs["input"], self.remote_summary_bytes())
        self.assertNotIn("FEISHU_APP_ID", kwargs["env"])
        self.assertEqual(kwargs["env"]["KEEP_ME"], "yes")
        self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertIn("--summary-stdin", invoke.call_args.args[0])

    def test_main_reports_locally_after_one_successful_ssh(self):
        marker = CONNECTOR.REMOTE_FEISHU_SUMMARY_PREFIX + base64.b64encode(
            self.remote_summary_bytes()
        )
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "workflow.py"
            output = Path(directory) / "remote.stdout"
            result_file = Path(directory) / "result.json"
            script.write_text("print('remote')\n", encoding="utf-8")

            def run_ssh(*_args, **kwargs):
                kwargs["stdout"].write(marker + b"\r\n")
                kwargs["stdout"].flush()
                return subprocess.CompletedProcess(["ssh"], 0)

            def report_locally(summary_raw, dry_run=False):
                self.assertEqual(summary_raw, self.remote_summary_bytes())
                return self.dry_run_result() if dry_run else self.reported_result()

            with mock.patch.object(CONNECTOR, "find_ssh_executable", return_value="ssh"), mock.patch.object(
                CONNECTOR, "validate_local_feishu_runtime"
            ), mock.patch.object(
                CONNECTOR.subprocess, "run", side_effect=run_ssh
            ) as ssh_call, mock.patch.object(
                CONNECTOR, "run_local_feishu_report", side_effect=report_locally
            ) as report:
                result = CONNECTOR.main(
                    [
                        "direct",
                        "--host",
                        "10.20.30.40",
                        "--script-file",
                        str(script),
                        "--output-file",
                        str(output),
                        "--result-file",
                        str(result_file),
                        "--local-feishu-report",
                    ]
                )
            stored = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(result, 0)
        self.assertEqual(ssh_call.call_count, 1)
        self.assertEqual(report.call_count, 2)
        self.assertEqual(report.call_args_list[0].kwargs, {"dry_run": True})
        self.assertEqual(report.call_args_list[1].kwargs, {})
        self.assertEqual(stored["remote_exit_code"], 0)
        self.assertEqual(stored["feishu"]["status"], "reported")
        self.assertEqual(len(stored["feishu_reports"]), 1)

    def test_missing_remote_summary_fails_locally_without_second_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "workflow.py"
            output = Path(directory) / "remote.stdout"
            result_file = Path(directory) / "result.json"
            script.write_text("print('remote without summary')\n", encoding="utf-8")

            def run_ssh(*_args, **kwargs):
                kwargs["stdout"].write(b"remote completed without marker\n")
                return subprocess.CompletedProcess(["ssh"], 0)

            with mock.patch.object(CONNECTOR, "find_ssh_executable", return_value="ssh"), mock.patch.object(
                CONNECTOR, "validate_local_feishu_runtime"
            ), mock.patch.object(
                CONNECTOR.subprocess, "run", side_effect=run_ssh
            ) as ssh_call, mock.patch.object(
                CONNECTOR, "run_local_feishu_report"
            ) as report:
                result = CONNECTOR.main(
                    [
                        "direct",
                        "--host",
                        "10.20.30.40",
                        "--script-file",
                        str(script),
                        "--output-file",
                        str(output),
                        "--result-file",
                        str(result_file),
                        "--local-feishu-report",
                    ]
                )
            stored = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(result, CONNECTOR.LOCAL_FEISHU_REPORT_FAILURE)
        self.assertEqual(ssh_call.call_count, 1)
        report.assert_not_called()
        self.assertEqual(stored["remote_exit_code"], 0)
        self.assertEqual(stored["stage"], "remote_summary_validation")

    def test_batch_attempts_every_local_report_and_aggregates_failures(self):
        first = self.remote_summary_bytes()
        second_data = json.loads(first.decode("utf-8"))
        second_data["card"] = "BW1000"
        second_data["script_path"] = second_data["script_path"].replace("bw1100", "bw1000")
        second = json.dumps(second_data).encode("utf-8")
        marker_output = b"\n".join(
            [
                CONNECTOR.REMOTE_FEISHU_SUMMARY_PREFIX + base64.b64encode(first),
                CONNECTOR.REMOTE_FEISHU_SUMMARY_PREFIX + base64.b64encode(second),
            ]
        ) + b"\n"

        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "workflow.py"
            output = Path(directory) / "remote.stdout"
            result_file = Path(directory) / "result.json"
            script.write_text("print('batch')\n", encoding="utf-8")

            def run_ssh(*_args, **kwargs):
                kwargs["stdout"].write(marker_output)
                return subprocess.CompletedProcess(["ssh"], 0)

            def report_locally(summary_raw, dry_run=False):
                remote = json.loads(summary_raw.decode("utf-8"))
                remote.update(
                    {
                        "timestamp_iso": "2026-07-20T12:00:00+08:00",
                        "timestamp_ms": 1784510400000,
                    }
                )
                if dry_run:
                    return {"dry_run": True, "summary": remote}
                if remote["card"] == "BW1100":
                    raise ValueError("synthetic first report failure")
                reported = self.reported_result()
                reported["summary"] = remote
                return reported

            with mock.patch.object(CONNECTOR, "find_ssh_executable", return_value="ssh"), mock.patch.object(
                CONNECTOR, "validate_local_feishu_runtime"
            ), mock.patch.object(
                CONNECTOR.subprocess, "run", side_effect=run_ssh
            ) as ssh_call, mock.patch.object(
                CONNECTOR, "run_local_feishu_report", side_effect=report_locally
            ) as report:
                result = CONNECTOR.main(
                    [
                        "direct",
                        "--host",
                        "10.20.30.40",
                        "--script-file",
                        str(script),
                        "--output-file",
                        str(output),
                        "--result-file",
                        str(result_file),
                        "--local-feishu-report",
                    ]
                )
            stored = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(result, CONNECTOR.LOCAL_FEISHU_REPORT_FAILURE)
        self.assertEqual(ssh_call.call_count, 1)
        self.assertEqual(report.call_count, 4)
        self.assertEqual(len(stored["feishu_failures"]), 1)
        self.assertEqual(stored["feishu_failures"][0]["index"], 0)
        self.assertEqual(len(stored["feishu_reports"]), 1)
        self.assertNotIn("feishu", stored)


class MainTests(unittest.TestCase):
    def test_two_factor_requires_real_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            result_file = Path(directory) / "result.json"
            with mock.patch.object(
                CONNECTOR.sys.stdin, "isatty", return_value=False
            ), mock.patch.object(CONNECTOR, "find_ssh_executable") as find_ssh:
                result = CONNECTOR.main(
                    [
                        "2fa",
                        "--user",
                        "wanghy18",
                        "--check",
                        "--result-file",
                        str(result_file),
                    ]
                )
            stored = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(result, 2)
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["exit_code"], 2)
        find_ssh.assert_not_called()

    def test_ssh_exit_code_is_returned(self):
        completed = subprocess.CompletedProcess(["ssh"], 255)
        with mock.patch.dict(
            os.environ, {"FEISHU_APP_SECRET": "must-not-cross-ssh", "KEEP_ME": "yes"}
        ), mock.patch.object(CONNECTOR.shutil, "which", return_value="ssh"), mock.patch.object(
            CONNECTOR.subprocess, "run", return_value=completed
        ) as invoke:
            result = CONNECTOR.main(
                ["direct", "--host", "10.20.30.40", "--command", "hostname"]
            )
        self.assertEqual(result, 255)
        self.assertNotIn("FEISHU_APP_SECRET", invoke.call_args.kwargs["env"])
        self.assertEqual(invoke.call_args.kwargs["env"]["KEEP_ME"], "yes")

    def test_output_and_atomic_result_files_are_written(self):
        completed = subprocess.CompletedProcess(["ssh"], 0)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "remote.stdout"
            result_file = Path(directory) / "result.json"
            result_file.write_text('{"status":"stale"}\n', encoding="utf-8")

            def run_ssh(*args, **kwargs):
                self.assertFalse(result_file.exists())
                self.assertIsNotNone(kwargs.get("stdout"))
                self.assertNotIn("stderr", kwargs)
                return completed

            with mock.patch.object(CONNECTOR.shutil, "which", return_value="ssh"), mock.patch.object(
                CONNECTOR.subprocess, "run", side_effect=run_ssh
            ):
                result = CONNECTOR.main(
                    [
                        "direct",
                        "--host",
                        "10.20.30.40",
                        "--command",
                        "hostname",
                        "--output-file",
                        str(output),
                        "--result-file",
                        str(result_file),
                    ]
                )

            stored = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(result, 0)
            self.assertEqual(stored["status"], "completed")
            self.assertEqual(stored["exit_code"], 0)
            self.assertEqual(stored["action"], "command")
            self.assertEqual(stored["output_file"], str(output.resolve()))
            self.assertNotIn("remote_command", stored)
            self.assertNotIn("payload", stored)
            self.assertTrue(output.exists())

    def test_failed_ssh_is_published_to_result_file(self):
        completed = subprocess.CompletedProcess(["ssh"], 255)
        with tempfile.TemporaryDirectory() as directory:
            result_file = Path(directory) / "result.json"
            with mock.patch.object(CONNECTOR.shutil, "which", return_value="ssh"), mock.patch.object(
                CONNECTOR.subprocess, "run", return_value=completed
            ):
                result = CONNECTOR.main(
                    [
                        "direct",
                        "--host",
                        "10.20.30.40",
                        "--check",
                        "--result-file",
                        str(result_file),
                    ]
                )
            stored = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(result, 255)
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["exit_code"], 255)

    def test_remote_workflow_failure_does_not_report_login_failure(self):
        completed = subprocess.CompletedProcess(["ssh"], 15)
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "workflow.py"
            output = Path(directory) / "remote.stdout"
            result_file = Path(directory) / "result.json"
            script.write_text("raise SystemExit(15)\n", encoding="utf-8")
            with mock.patch.object(CONNECTOR.shutil, "which", return_value="ssh"), mock.patch.object(
                CONNECTOR.subprocess, "run", return_value=completed
            ), mock.patch.object(CONNECTOR.sys, "stderr", new_callable=io.StringIO) as stderr:
                result = CONNECTOR.main(
                    [
                        "direct",
                        "--host",
                        "10.20.30.40",
                        "--script-file",
                        str(script),
                        "--output-file",
                        str(output),
                        "--result-file",
                        str(result_file),
                    ]
                )
                message = stderr.getvalue()
        self.assertEqual(result, 15)
        self.assertIn(
            "SSH login succeeded, but the remote workflow failed with exit code 15.",
            message,
        )
        self.assertNotIn("SSH login or connection failed", message)

    def test_local_file_paths_must_be_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory) / "shared.json"
            result = CONNECTOR.main(
                [
                    "direct",
                    "--host",
                    "10.20.30.40",
                    "--check",
                    "--output-file",
                    str(shared),
                    "--result-file",
                    str(shared),
                ]
            )
        self.assertEqual(result, 2)

    def test_python_preflight_replaces_stale_result_without_opening_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "workflow.py"
            result_file = Path(directory) / "result.json"
            output = Path(directory) / "remote.stdout"
            script.write_text(
                "import subprocess\nsubprocess.run(['true'], text=True)\n",
                encoding="utf-8",
            )
            result_file.write_text('{"status":"completed"}\n', encoding="utf-8")
            output.write_text("stale output\n", encoding="utf-8")
            with mock.patch.object(CONNECTOR, "find_ssh_executable") as find_ssh:
                result = CONNECTOR.main(
                    [
                        "direct",
                        "--host",
                        "10.20.30.40",
                        "--script-file",
                        str(script),
                        "--output-file",
                        str(output),
                        "--result-file",
                        str(result_file),
                    ]
                )
            stored = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(result, 2)
        self.assertEqual(stored["status"], "preflight_failed")
        self.assertIn("universal_newlines", stored["error"])
        self.assertFalse(output.exists())
        find_ssh.assert_not_called()

    def test_openssh_start_failure_is_published(self):
        with tempfile.TemporaryDirectory() as directory:
            result_file = Path(directory) / "result.json"
            with mock.patch.object(
                CONNECTOR, "find_ssh_executable", return_value="ssh"
            ), mock.patch.object(
                CONNECTOR.subprocess, "run", side_effect=OSError("start failed")
            ):
                result = CONNECTOR.main(
                    [
                        "direct",
                        "--host",
                        "10.20.30.40",
                        "--check",
                        "--result-file",
                        str(result_file),
                    ]
                )
            stored = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(result, 126)
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["exit_code"], 126)

    def test_result_publish_failure_returns_dedicated_exit_code(self):
        completed = subprocess.CompletedProcess(["ssh"], 0)
        with tempfile.TemporaryDirectory() as directory:
            result_file = Path(directory) / "result.json"
            with mock.patch.object(
                CONNECTOR, "find_ssh_executable", return_value="ssh"
            ), mock.patch.object(
                CONNECTOR.subprocess, "run", return_value=completed
            ), mock.patch.object(
                CONNECTOR, "write_result_file", side_effect=OSError("disk full")
            ):
                result = CONNECTOR.main(
                    [
                        "direct",
                        "--host",
                        "10.20.30.40",
                        "--check",
                        "--result-file",
                        str(result_file),
                    ]
                )
        self.assertEqual(result, CONNECTOR.RESULT_WRITE_FAILURE)

    @unittest.skipUnless(os.name == "nt", "Windows OpenSSH path test")
    def test_windows_system_openssh_is_preferred(self):
        path = CONNECTOR.find_ssh_executable()
        self.assertEqual(
            Path(path),
            Path(os.environ.get("WINDIR", r"C:\Windows"))
            / "System32"
            / "OpenSSH"
            / "ssh.exe",
        )


if __name__ == "__main__":
    unittest.main()
