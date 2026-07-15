import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "report_to_feishu.py"
SPEC = importlib.util.spec_from_file_location("report_to_feishu", str(MODULE_PATH))
REPORTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORTER)


def summary():
    return {
        "model_name": "Qwen3-8B",
        "script_path": "/tmp/serve_vllm_qwen3-8b_bw1000_1x.sh",
        "framework": "vllm",
        "card": "BW1000",
        "timestamp_iso": "2026-07-14T16:00:00+08:00",
        "timestamp_ms": 1784016000000,
        "uses_kvcache_fp8": True,
    }


class LocalConfigTests(unittest.TestCase):
    def test_reads_config_from_bundled_assets_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "feishu.json"
            config_path.write_text(
                json.dumps(
                    {
                        "app_id": "cli_test",
                        "app_secret": "test-secret",
                        "recipient_id": "oc_test",
                        "recipient_id_type": "chat_id",
                        "table_type": "sheets",
                        "table_url": "https://example.test/sheets/test",
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(REPORTER, "CONFIG_PATH", config_path):
                config = REPORTER.load_local_config()

            self.assertEqual(config["app_id"], "cli_test")
            self.assertEqual(config["recipient_id_type"], "chat_id")

    def test_placeholder_is_treated_as_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(REPORTER.FeishuError):
                REPORTER.env_or_config(
                    "FEISHU_APP_ID",
                    {"app_id": "<contact-skill-maintainer>"},
                    "app_id",
                )

    def test_missing_bundled_config_fails_clearly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "missing.json"
            with patch.object(REPORTER, "CONFIG_PATH", config_path):
                with self.assertRaises(REPORTER.FeishuError) as context:
                    REPORTER.load_local_config()

            self.assertIn("bundled Feishu config not found", str(context.exception))


class SheetsUpsertTests(unittest.TestCase):
    def setUp(self):
        self.config = {"spreadsheet_token": "sheet-token", "sheet_id": "sheet-id"}

    @patch.object(REPORTER, "request_json")
    def test_creates_record_when_model_and_card_are_new(self, request_json):
        request_json.side_effect = [
            {
                "data": {
                    "valueRange": {
                        "values": [
                            REPORTER.HEADERS,
                            ["Other", "/tmp/other.sh", "BW1000", "old", "否"],
                        ]
                    }
                }
            },
            {"data": {"updates": {"updatedRange": "sheet-id!A3:E3"}}},
        ]

        result = REPORTER.write_table("sheets", self.config, summary(), "token")

        self.assertEqual(result["action"], "created")
        self.assertEqual(result["previous_records"], 0)
        self.assertIn("values_append", request_json.call_args_list[1][0][1])

    @patch.object(REPORTER, "request_json")
    def test_updates_current_fields_and_clears_legacy_duplicates(self, request_json):
        request_json.side_effect = [
            {
                "data": {
                    "valueRange": {
                        "values": [
                            REPORTER.HEADERS,
                            ["Qwen3-8B", "/home/a/old-name.sh", "BW1000", "old-1", "否"],
                            ["Other", "/tmp/other.sh", "BW1000", "old", "否"],
                            ["qwen3-8b", "/home/b/other-name.sh", "bw-1000", "old-2", "是"],
                        ]
                    }
                }
            },
            {"data": {"updatedRange": "sheet-id!A2:E2"}},
            {"data": {"updatedRange": "sheet-id!A4:E4"}},
        ]

        result = REPORTER.write_table("sheets", self.config, summary(), "token")

        self.assertEqual(result["action"], "updated")
        self.assertEqual(result["previous_records"], 2)
        self.assertEqual(result["removed_duplicates"], 1)
        updated_payload = request_json.call_args_list[1][0][2]
        cleared_payload = request_json.call_args_list[2][0][2]
        self.assertEqual(updated_payload["valueRange"]["range"], "sheet-id!A2:E2")
        self.assertEqual(
            updated_payload["valueRange"]["values"],
            [[
                "Qwen3-8B",
                summary()["script_path"],
                "BW1000",
                summary()["timestamp_iso"],
                "是",
            ]],
        )
        self.assertEqual(cleared_payload["valueRange"]["values"], [["", "", "", "", ""]])


class BitableUpsertTests(unittest.TestCase):
    @patch.object(REPORTER, "request_json")
    def test_updates_record_and_deletes_legacy_duplicates(self, request_json):
        request_json.side_effect = [
            {
                "data": {
                    "items": [
                        {
                            "record_id": "rec-1",
                            "fields": {},
                        },
                        {
                            "record_id": "rec-2",
                            "fields": {},
                        },
                    ],
                    "has_more": False,
                }
            },
            {"data": {"record": {"record_id": "rec-1"}}},
            {"data": {}},
        ]
        config = {"app_token": "app-token", "table_id": "table-id"}

        result = REPORTER.write_table("bitable", config, summary(), "token")

        self.assertEqual(result["action"], "updated")
        self.assertEqual(result["removed_duplicates"], 1)
        search_payload = request_json.call_args_list[0][0][2]
        self.assertEqual(
            search_payload["filter"]["conditions"],
            [
                {
                    "field_name": REPORTER.FIELD_MODEL,
                    "operator": "is",
                    "value": [summary()["model_name"]],
                },
                {
                    "field_name": REPORTER.FIELD_CARD,
                    "operator": "is",
                    "value": [summary()["card"]],
                },
            ],
        )
        self.assertEqual(
            request_json.call_args_list[1][0][2],
            {
                "fields": {
                    REPORTER.FIELD_SCRIPT: summary()["script_path"],
                    REPORTER.FIELD_TIMESTAMP: summary()["timestamp_ms"],
                    REPORTER.FIELD_KVCACHE_FP8: summary()["uses_kvcache_fp8"],
                }
            },
        )
        self.assertIn("batch_delete", request_json.call_args_list[2][0][1])
        self.assertEqual(request_json.call_args_list[2][0][2], {"records": ["rec-2"]})


class ReportingRetryTests(unittest.TestCase):
    def test_retries_three_times_then_succeeds(self):
        calls = []
        sleeps = []

        def operation():
            calls.append(True)
            if len(calls) <= 3:
                raise REPORTER.FeishuError("temporary failure")
            return {"status": "reported"}

        result, attempts = REPORTER.report_with_retries(
            operation, sleep_fn=sleeps.append
        )

        self.assertEqual(result, {"status": "reported"})
        self.assertEqual(attempts, 4)
        self.assertEqual(sleeps, [3, 3, 3])

    def test_raises_after_initial_attempt_and_three_retries(self):
        calls = []
        sleeps = []

        def operation():
            calls.append(True)
            raise REPORTER.FeishuError("still failing")

        with self.assertRaises(REPORTER.FeishuError) as context:
            REPORTER.report_with_retries(operation, sleep_fn=sleeps.append)

        self.assertEqual(len(calls), 4)
        self.assertEqual(sleeps, [3, 3, 3])
        self.assertIn("did not close after 4 attempts", str(context.exception))


if __name__ == "__main__":
    unittest.main()
