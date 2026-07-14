import importlib.util
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


class SheetsUpsertTests(unittest.TestCase):
    def setUp(self):
        self.config = {"spreadsheet_token": "sheet-token", "sheet_id": "sheet-id"}

    @patch.object(REPORTER, "request_json")
    def test_creates_record_when_script_path_is_new(self, request_json):
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
    def test_updates_one_record_and_clears_legacy_duplicates(self, request_json):
        target = "/public/home/user/serve_vllm_qwen3-8b_bw1000_1x.sh"
        request_json.side_effect = [
            {
                "data": {
                    "valueRange": {
                        "values": [
                            REPORTER.HEADERS,
                            ["Qwen3-8B", target, "BW1000", "old-1", "否"],
                            ["Other", "/tmp/other.sh", "BW1000", "old", "否"],
                            ["Qwen3-8B", target, "BW1000", "old-2", "否"],
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
                            "fields": {
                                REPORTER.FIELD_SCRIPT: "/public/home/user/serve_vllm_qwen3-8b_bw1000_1x.sh"
                            },
                        },
                        {
                            "record_id": "rec-2",
                            "fields": {
                                REPORTER.FIELD_SCRIPT: "/public2/home/user/serve_vllm_qwen3-8b_bw1000_1x.sh"
                            },
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
        self.assertIn("batch_delete", request_json.call_args_list[2][0][1])
        self.assertEqual(request_json.call_args_list[2][0][2], {"records": ["rec-2"]})


if __name__ == "__main__":
    unittest.main()
