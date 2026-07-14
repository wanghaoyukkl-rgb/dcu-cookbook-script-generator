#!/usr/bin/env python3
"""Write generated serve-script metadata to Feishu and notify a recipient."""

import argparse
import json
import os
import re
import shlex
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen


API_BASE = "https://open.feishu.cn/open-apis"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "dcu-cookbook-script-generator" / "feishu.json"
FIELD_MODEL = "模型名"
FIELD_SCRIPT = "脚本绝对路径"
FIELD_CARD = "加速卡"
FIELD_TIMESTAMP = "时间戳"
FIELD_KVCACHE_FP8 = "KVCache-FP8"
HEADERS = [FIELD_MODEL, FIELD_SCRIPT, FIELD_CARD, FIELD_TIMESTAMP, FIELD_KVCACHE_FP8]
RECIPIENT_TYPES = {"open_id", "user_id", "union_id", "email", "chat_id"}


class FeishuError(RuntimeError):
    pass


def require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise FeishuError("missing required environment variable: {}".format(name))
    return value


def env_or_config(env_name, config, config_name):
    value = os.environ.get(env_name, "").strip() or str(config.get(config_name, "")).strip()
    if not value:
        raise FeishuError(
            "missing required environment variable or local config value: {} / {}".format(
                env_name, config_name
            )
        )
    return value


def load_local_config():
    config_path = Path(
        os.environ.get("FEISHU_CONFIG_FILE", str(DEFAULT_CONFIG_PATH))
    ).expanduser()
    if not config_path.is_file():
        return {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FeishuError("invalid local Feishu config: {}".format(exc))
    if not isinstance(config, dict):
        raise FeishuError("local Feishu config must be a JSON object")
    if str(config.get("app_secret", "")).strip():
        mode = stat.S_IMODE(config_path.stat().st_mode)
        if mode & 0o077:
            raise FeishuError(
                "local Feishu config contains app_secret and must not be accessible by group/others"
            )
    return config


def command_option(content, option):
    normalized = re.sub(r"\\\s*\n", " ", content)
    try:
        tokens = shlex.split(normalized, comments=True)
    except ValueError as exc:
        raise FeishuError("failed to parse serve script command: {}".format(exc))
    for index, token in enumerate(tokens):
        if token == option and index + 1 < len(tokens):
            return tokens[index + 1]
        if token.startswith(option + "="):
            return token.split("=", 1)[1]
    return ""


def parse_metadata(script_path):
    content = script_path.read_text(encoding="utf-8")
    metadata = {}
    for line in content.splitlines():
        match = re.match(r"^#\s*([a-z][a-z0-9_]*):\s*(.*?)\s*$", line)
        if match:
            metadata[match.group(1)] = match.group(2)

    filename_match = re.match(
        r"^serve_(vllm|sglang)_(.+)_([a-z0-9]+)_([0-9]+x)\.sh$",
        script_path.name,
        re.IGNORECASE,
    )
    model_path = (
        metadata.get("model_path", "").strip() or command_option(content, "--model-path")
    ).rstrip("/")
    if not model_path or not os.path.isabs(model_path):
        raise FeishuError("script metadata requires an absolute model_path")
    card = metadata.get("card", "").strip() or (
        filename_match.group(3).upper() if filename_match else ""
    )
    if not card:
        raise FeishuError("script metadata is missing card")
    framework = metadata.get("framework", "").strip().lower()
    if framework not in {"vllm", "sglang"}:
        framework = filename_match.group(1).lower() if filename_match else ""
    if framework not in {"vllm", "sglang"}:
        raise FeishuError("script metadata is missing a supported framework")

    kvcache_value = metadata.get("kvcache", "").strip().lower()
    uses_kvcache_fp8 = kvcache_value == "kvcache_fp8" or bool(
        re.search(r"--kv-cache-dtype\s+fp8(?:_|\b)", content, re.IGNORECASE)
    )
    now = datetime.now(timezone.utc).astimezone()
    return {
        "model_name": os.path.basename(model_path),
        "script_path": os.path.abspath(str(script_path)),
        "framework": framework,
        "card": card,
        "timestamp_iso": now.isoformat(timespec="seconds"),
        "timestamp_ms": int(time.time() * 1000),
        "uses_kvcache_fp8": uses_kvcache_fp8,
    }


def request_json(method, url, payload=None, access_token=None, timeout=30):
    data = None
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if access_token:
        headers["Authorization"] = "Bearer {}".format(access_token)
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise FeishuError("Feishu HTTP {}: {}".format(exc.code, raw[:1000]))
    except URLError as exc:
        raise FeishuError("Feishu request failed: {}".format(exc.reason))

    try:
        result = json.loads(raw) if raw else {}
    except ValueError:
        raise FeishuError("Feishu returned a non-JSON response")
    if result.get("code", 0) != 0:
        raise FeishuError(
            "Feishu API error {}: {}".format(result.get("code"), result.get("msg", "unknown error"))
        )
    return result


def get_tenant_access_token(app_id, app_secret):
    result = request_json(
        "POST",
        API_BASE + "/auth/v3/tenant_access_token/internal/",
        {"app_id": app_id, "app_secret": app_secret},
    )
    token = result.get("tenant_access_token", "")
    if not token:
        raise FeishuError("Feishu token response did not include tenant_access_token")
    return token


def parse_table_url(table_url):
    parsed = urlparse(table_url)
    parts = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)
    if "base" in parts:
        index = parts.index("base")
        app_token = parts[index + 1] if len(parts) > index + 1 else ""
        table_id = query.get("table", [""])[0]
        return "bitable", {"app_token": app_token, "table_id": table_id}
    if "sheets" in parts:
        index = parts.index("sheets")
        spreadsheet_token = parts[index + 1] if len(parts) > index + 1 else ""
        sheet_id = query.get("sheet", [""])[0]
        return "sheets", {"spreadsheet_token": spreadsheet_token, "sheet_id": sheet_id}
    if "wiki" in parts:
        index = parts.index("wiki")
        node_token = parts[index + 1] if len(parts) > index + 1 else ""
        return "wiki", {
            "node_token": node_token,
            "sheet_id": query.get("sheet", [""])[0],
            "table_id": query.get("table", [""])[0],
        }
    raise FeishuError("FEISHU_TABLE_URL must contain /base/, /sheets/, or /wiki/")


def resolve_wiki_node(node_token, access_token):
    if not node_token:
        raise FeishuError("wiki table URL is missing its node token")
    url = API_BASE + "/wiki/v2/spaces/get_node?" + urlencode({"token": node_token})
    result = request_json("GET", url, access_token=access_token)
    node = result.get("data", {}).get("node", {})
    obj_type = node.get("obj_type", "")
    obj_token = node.get("obj_token", "")
    if not obj_token:
        raise FeishuError("Feishu wiki node response did not include obj_token")
    if obj_type == "sheet":
        return "sheets", {"spreadsheet_token": obj_token}
    if obj_type == "bitable":
        return "bitable", {"app_token": obj_token}
    raise FeishuError("wiki node is not an electronic spreadsheet or bitable: {}".format(obj_type))


def framework_env(name, framework):
    if framework:
        value = os.environ.get("{}_{}".format(name, framework.upper()), "").strip()
        if value:
            return value
    return os.environ.get(name, "").strip()


def find_sheet_id_by_title(spreadsheet_token, title, access_token):
    url = "{}/sheets/v3/spreadsheets/{}/sheets/query".format(
        API_BASE, quote(spreadsheet_token, safe="")
    )
    result = request_json("GET", url, access_token=access_token)
    sheets = result.get("data", {}).get("sheets", [])
    matches = [
        sheet
        for sheet in sheets
        if sheet.get("title", "").strip().lower() == title.strip().lower()
    ]
    if len(matches) > 1:
        raise FeishuError("multiple worksheets are named {}".format(title))
    if matches:
        return matches[0].get("sheet_id", "")
    return ""


def resolve_table_config(table_type, framework, access_token):
    local_config = load_local_config()
    table_url = framework_env("FEISHU_TABLE_URL", framework) or str(
        local_config.get("table_url", "")
    ).strip()
    parsed_type = ""
    parsed_values = {}
    if table_url:
        parsed_type, parsed_values = parse_table_url(table_url)
        if parsed_type == "wiki":
            wiki_type, wiki_values = resolve_wiki_node(parsed_values.get("node_token", ""), access_token)
            parsed_type = wiki_type
            parsed_values.update(wiki_values)
    resolved_type = (
        table_type
        or os.environ.get("FEISHU_TABLE_TYPE", "").strip().lower()
        or str(local_config.get("table_type", "")).strip().lower()
        or parsed_type
    )
    if resolved_type not in {"bitable", "sheets"}:
        raise FeishuError("FEISHU_TABLE_TYPE must be bitable or sheets")
    if parsed_type and parsed_type != resolved_type:
        raise FeishuError("FEISHU_TABLE_TYPE conflicts with FEISHU_TABLE_URL")

    if resolved_type == "bitable":
        app_token = os.environ.get("FEISHU_BITABLE_APP_TOKEN", "").strip() or parsed_values.get(
            "app_token", ""
        )
        table_id = os.environ.get("FEISHU_BITABLE_TABLE_ID", "").strip() or parsed_values.get(
            "table_id", ""
        )
        if not app_token or not table_id:
            raise FeishuError("bitable reporting requires app_token and table_id")
        return resolved_type, {"app_token": app_token, "table_id": table_id}

    spreadsheet_token = os.environ.get("FEISHU_SPREADSHEET_TOKEN", "").strip() or parsed_values.get(
        "spreadsheet_token", ""
    )
    framework_sheet_id = os.environ.get(
        "FEISHU_SHEET_ID_{}".format(framework.upper()), ""
    ).strip()
    sheet_id = framework_sheet_id or os.environ.get("FEISHU_SHEET_ID", "").strip()
    if not sheet_id and spreadsheet_token and framework:
        sheet_id = find_sheet_id_by_title(spreadsheet_token, framework, access_token)
    sheet_id = sheet_id or parsed_values.get("sheet_id", "")
    if not spreadsheet_token or not sheet_id:
        raise FeishuError("sheets reporting requires spreadsheet_token and sheet_id")
    return resolved_type, {"spreadsheet_token": spreadsheet_token, "sheet_id": sheet_id}


def build_bitable_payload(summary):
    return {
        "fields": {
            FIELD_MODEL: summary["model_name"],
            FIELD_SCRIPT: summary["script_path"],
            FIELD_CARD: summary["card"],
            FIELD_TIMESTAMP: summary["timestamp_ms"],
            FIELD_KVCACHE_FP8: summary["uses_kvcache_fp8"],
        }
    }


def build_sheets_values(summary):
    return [
        summary["model_name"],
        summary["script_path"],
        summary["card"],
        summary["timestamp_iso"],
        "是" if summary["uses_kvcache_fp8"] else "否",
    ]


def build_sheets_payload(summary, sheet_id):
    return {
        "valueRange": {
            "range": "{}!A1:E1".format(sheet_id),
            "values": [build_sheets_values(summary)],
        }
    }


def build_bitable_update_payload(summary):
    return {
        "fields": {
            FIELD_SCRIPT: summary["script_path"],
            FIELD_TIMESTAMP: summary["timestamp_ms"],
            FIELD_KVCACHE_FP8: summary["uses_kvcache_fp8"],
        }
    }


def search_bitable_records(table_config, summary, access_token):
    base_url = "{}/bitable/v1/apps/{}/tables/{}/records/search".format(
        API_BASE,
        quote(table_config["app_token"], safe=""),
        quote(table_config["table_id"], safe=""),
    )
    payload = {
        "field_names": [FIELD_MODEL, FIELD_CARD],
        "filter": {
            "conjunction": "and",
            "conditions": [
                {
                    "field_name": FIELD_MODEL,
                    "operator": "is",
                    "value": [summary["model_name"]],
                },
                {
                    "field_name": FIELD_CARD,
                    "operator": "is",
                    "value": [summary["card"]],
                },
            ],
        },
    }
    records = []
    page_token = ""
    while True:
        query = {"page_size": 500}
        if page_token:
            query["page_token"] = page_token
        result = request_json(
            "POST", base_url + "?" + urlencode(query), payload, access_token
        )
        data = result.get("data", {})
        records.extend(data.get("items", []))
        if not data.get("has_more"):
            return records
        page_token = data.get("page_token", "")
        if not page_token:
            raise FeishuError("bitable search response is missing page_token")


def write_bitable(table_config, summary, access_token):
    app_token = quote(table_config["app_token"], safe="")
    table_id = quote(table_config["table_id"], safe="")
    records_url = "{}/bitable/v1/apps/{}/tables/{}/records".format(
        API_BASE, app_token, table_id
    )
    existing = search_bitable_records(table_config, summary, access_token)
    if not existing:
        result = request_json(
            "POST", records_url, build_bitable_payload(summary), access_token
        )
        record = result.get("data", {}).get("record", {})
        return {
            "type": "bitable",
            "action": "created",
            "record_id": record.get("record_id", ""),
            "previous_records": 0,
            "removed_duplicates": 0,
        }

    record_id = existing[0].get("record_id", "")
    if not record_id:
        raise FeishuError("bitable search result is missing record_id")
    result = request_json(
        "PUT",
        records_url + "/" + quote(record_id, safe=""),
        build_bitable_update_payload(summary),
        access_token,
    )
    updated_record = result.get("data", {}).get("record", {})
    duplicate_ids = [
        record.get("record_id", "") for record in existing[1:] if record.get("record_id", "")
    ]
    if duplicate_ids:
        request_json(
            "POST",
            records_url + "/batch_delete",
            {"records": duplicate_ids},
            access_token,
        )
    return {
        "type": "bitable",
        "action": "updated",
        "record_id": updated_record.get("record_id", "") or record_id,
        "previous_records": len(existing),
        "removed_duplicates": len(duplicate_ids),
    }


def read_sheet_rows(table_config, access_token):
    target_range = "{}!A:E".format(table_config["sheet_id"])
    url = "{}/sheets/v2/spreadsheets/{}/values/{}".format(
        API_BASE,
        quote(table_config["spreadsheet_token"], safe=""),
        quote(target_range, safe=""),
    )
    result = request_json("GET", url, access_token=access_token)
    return result.get("data", {}).get("valueRange", {}).get("values", []) or []


def normalize_model_name(value):
    return str(value or "").strip().casefold()


def normalize_card(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").strip().casefold())


def matching_sheet_rows(rows, model_name, card):
    matches = []
    target_model = normalize_model_name(model_name)
    target_card = normalize_card(card)
    for row_number, row in enumerate(rows, start=1):
        if not isinstance(row, list) or len(row) <= 2:
            continue
        if (
            normalize_model_name(row[0]) == target_model
            and normalize_card(row[2]) == target_card
        ):
            matches.append(row_number)
    return matches


def build_sheets_update_payload(summary, sheet_id, row_number, existing_row):
    values = list(existing_row[: len(HEADERS)])
    values.extend([""] * (len(HEADERS) - len(values)))
    values[1] = summary["script_path"]
    values[3] = summary["timestamp_iso"]
    values[4] = "是" if summary["uses_kvcache_fp8"] else "否"
    return {
        "valueRange": {
            "range": "{}!A{}:E{}".format(sheet_id, row_number, row_number),
            "values": [values],
        }
    }


def write_sheet_range(table_config, payload, access_token):
    url = "{}/sheets/v2/spreadsheets/{}/values".format(
        API_BASE, quote(table_config["spreadsheet_token"], safe="")
    )
    return request_json("PUT", url, payload, access_token)


def write_sheets(table_config, summary, access_token):
    rows = read_sheet_rows(table_config, access_token)
    matching_rows = matching_sheet_rows(
        rows, summary["model_name"], summary["card"]
    )
    if matching_rows:
        target_row = matching_rows[0]
        result = write_sheet_range(
            table_config,
            build_sheets_update_payload(
                summary,
                table_config["sheet_id"],
                target_row,
                rows[target_row - 1],
            ),
            access_token,
        )
        for duplicate_row in matching_rows[1:]:
            write_sheet_range(
                table_config,
                {
                    "valueRange": {
                        "range": "{}!A{}:E{}".format(
                            table_config["sheet_id"], duplicate_row, duplicate_row
                        ),
                        "values": [["", "", "", "", ""]],
                    }
                },
                access_token,
            )
        data = result.get("data", {})
        return {
            "type": "sheets",
            "action": "updated",
            "sheet_id": table_config["sheet_id"],
            "updated_range": data.get("updatedRange", ""),
            "previous_records": len(matching_rows),
            "removed_duplicates": max(0, len(matching_rows) - 1),
        }

    query = urlencode({"insertDataOption": "INSERT_ROWS"})
    url = "{}/sheets/v2/spreadsheets/{}/values_append?{}".format(
        API_BASE, quote(table_config["spreadsheet_token"], safe=""), query
    )
    result = request_json(
        "POST", url, build_sheets_payload(summary, table_config["sheet_id"]), access_token
    )
    updates = result.get("data", {}).get("updates", {})
    return {
        "type": "sheets",
        "action": "created",
        "sheet_id": table_config["sheet_id"],
        "updated_range": updates.get("updatedRange", ""),
        "previous_records": 0,
        "removed_duplicates": 0,
    }


def write_table(table_type, table_config, summary, access_token):
    if table_type == "bitable":
        return write_bitable(table_config, summary, access_token)
    return write_sheets(table_config, summary, access_token)


def build_message(summary):
    return "\n".join(
        [
            "DCU serve 脚本已生成并同步飞书表格",
            "模型名：{}".format(summary["model_name"]),
            "推理框架：{}".format(summary["framework"]),
            "脚本绝对路径：{}".format(summary["script_path"]),
            "加速卡：{}".format(summary["card"]),
            "生成时间戳：{}".format(summary["timestamp_iso"]),
            "KVCache FP8：{}".format("是" if summary["uses_kvcache_fp8"] else "否"),
        ]
    )


def send_message(recipient_id, recipient_type, summary, access_token):
    if recipient_type not in RECIPIENT_TYPES:
        raise FeishuError("unsupported FEISHU_RECIPIENT_ID_TYPE: {}".format(recipient_type))
    url = API_BASE + "/im/v1/messages?" + urlencode({"receive_id_type": recipient_type})
    payload = {
        "receive_id": recipient_id,
        "msg_type": "text",
        "content": json.dumps({"text": build_message(summary)}, ensure_ascii=False),
    }
    result = request_json("POST", url, payload, access_token)
    message_id = result.get("data", {}).get("message_id", "")
    return {"recipient_type": recipient_type, "message_id": message_id}


def dry_run_output(summary, table_type):
    output = {
        "dry_run": True,
        "headers": HEADERS,
        "summary": summary,
        "table_type": table_type,
        "table_payload": (
            build_bitable_payload(summary)
            if table_type == "bitable"
            else build_sheets_payload(summary, "<sheet_id>")
        ),
        "message": build_message(summary),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(
        description="Write generated DCU serve-script metadata to Feishu and notify a recipient."
    )
    parser.add_argument("--script-path", required=True, help="Generated serve script to report.")
    parser.add_argument("--table-type", choices=["bitable", "sheets"], help="Override table backend.")
    parser.add_argument("--dry-run", action="store_true", help="Print derived payload without network calls.")
    args = parser.parse_args()

    script_path = Path(args.script_path).expanduser()
    if not script_path.is_file():
        print("serve script not found: {}".format(script_path), file=sys.stderr)
        return 2

    try:
        summary = parse_metadata(script_path)
        local_config = load_local_config()
        table_type = (
            args.table_type
            or os.environ.get("FEISHU_TABLE_TYPE", "").strip().lower()
            or str(local_config.get("table_type", "")).strip().lower()
            or "bitable"
        )
        if args.dry_run:
            dry_run_output(summary, table_type)
            return 0

        app_id = env_or_config("FEISHU_APP_ID", local_config, "app_id")
        app_secret = env_or_config("FEISHU_APP_SECRET", local_config, "app_secret")
        recipient_id = env_or_config("FEISHU_RECIPIENT_ID", local_config, "recipient_id")
        recipient_type = (
            os.environ.get("FEISHU_RECIPIENT_ID_TYPE", "").strip().lower()
            or str(local_config.get("recipient_id_type", "open_id")).strip().lower()
        )
        access_token = get_tenant_access_token(app_id, app_secret)
        table_type, table_config = resolve_table_config(
            args.table_type, summary["framework"], access_token
        )
        table_result = write_table(table_type, table_config, summary, access_token)
        message_result = send_message(recipient_id, recipient_type, summary, access_token)
        print(
            json.dumps(
                {
                    "status": "reported",
                    "summary": summary,
                    "table": table_result,
                    "message": message_result,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (FeishuError, OSError, ValueError) as exc:
        print("Feishu reporting failed: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
