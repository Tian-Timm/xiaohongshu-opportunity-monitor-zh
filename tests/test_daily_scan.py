from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

from tests.test_review_server import running_server


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "xiaohongshu-opportunity-monitor-zh" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ledger = load_module("xhs_ledger_for_daily_scan_tests", SCRIPTS / "ledger.py")
daily_scan = load_module("xhs_daily_scan", SCRIPTS / "daily_scan.py")


class DailyScanTests(unittest.TestCase):
    def test_successful_new_scan_replaces_a_completed_previous_round(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor_root = Path(temp_dir) / "work" / "xiaohongshu-monitor"
            state_path = monitor_root / "monitor-a" / "state.json"
            input_path = Path(temp_dir) / "scan.json"
            state = ledger.new_state("AI 实习", 14, "Asia/Shanghai")
            state["last_run"] = {"completed_at": "2026-07-17T21:00:00+08:00"}
            state["review_session"] = {
                "view": "default",
                "scan_completed_at": "2026-07-17T21:00:00+08:00",
                "queue": [],
                "queue_kinds": {},
                "position": 0,
                "current_id": None,
                "deferred_ids": [],
                "counts": {"valuable": 1, "not_valuable": 0, "later": 0},
            }
            ledger.save_json(state_path, state)
            ledger.save_json(
                input_path,
                {
                    "run_at": "2026-07-18T21:00:00+08:00",
                    "queries": ["AI 产品实习"],
                    "candidates": [
                        {
                            "note_id": "0123456789abcdef01234567",
                            "title": "新一天的审核机会",
                            "url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
                            "published_at": "2026-07-18T19:00:00+08:00",
                            "classification": "verified_active",
                            "query": "AI 产品实习",
                        }
                    ],
                },
            )

            with running_server(monitor_root) as base_url:
                result = daily_scan.run_daily_scan(
                    state_path,
                    input_path,
                    ensure_review=lambda root, monitor_id: f"{base_url}/monitors/{monitor_id}/review",
                )
                with urlopen(result["review_url"]) as response:
                    review_page = response.read().decode("utf-8")

            self.assertIn("新一天的审核机会", review_page)
            self.assertEqual(
                ledger.load_json(state_path)["review_session"]["scan_completed_at"],
                "2026-07-18T21:00:00+08:00",
            )

    def test_successful_scan_returns_a_working_monitor_review_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor_root = Path(temp_dir) / "work" / "xiaohongshu-monitor"
            state_path = monitor_root / "monitor-a" / "state.json"
            input_path = Path(temp_dir) / "scan.json"
            ledger.save_json(state_path, ledger.new_state("AI 实习", 14, "Asia/Shanghai"))
            ledger.save_json(
                input_path,
                {
                    "run_at": "2026-07-18T21:00:00+08:00",
                    "queries": ["AI 产品实习"],
                    "candidates": [
                        {
                            "note_id": "0123456789abcdef01234567",
                            "title": "扫描后可审核的机会",
                            "url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
                            "published_at": "2026-07-18T19:00:00+08:00",
                            "classification": "verified_active",
                            "query": "AI 产品实习",
                        }
                    ],
                },
            )

            with running_server(monitor_root) as base_url:
                result = daily_scan.run_daily_scan(
                    state_path,
                    input_path,
                    ensure_review=lambda root, monitor_id: f"{base_url}/monitors/{monitor_id}/review",
                )
                with urlopen(result["review_url"]) as response:
                    review_page = response.read().decode("utf-8")

            self.assertTrue(result["ok"])
            self.assertEqual(result["action_label"], "打开今日审核页")
            self.assertEqual(result["counts"]["new"], 1)
            self.assertIn("/monitors/monitor-a/review", result["review_url"])
            self.assertIn("扫描后可审核的机会", review_page)

    def test_failed_scan_reports_failure_without_a_review_link_or_empty_queue_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor_root = Path(temp_dir) / "work" / "xiaohongshu-monitor"
            state_path = monitor_root / "monitor-a" / "state.json"
            input_path = Path(temp_dir) / "scan.json"
            ledger.save_json(state_path, ledger.new_state("AI 实习", 14, "Asia/Shanghai"))
            ledger.save_json(
                input_path,
                {
                    "run_at": "2026-07-18T21:00:00+08:00",
                    "queries": ["AI 产品实习"],
                    "candidates": [
                        {
                            "note_id": "0123456789abcdef01234567",
                            "title": "缺少签名的结果",
                            "url": "https://www.xiaohongshu.com/search_result/0123456789abcdef01234567",
                            "published_at": "2026-07-18T19:00:00+08:00",
                            "classification": "verified_active",
                            "query": "AI 产品实习",
                        }
                    ],
                },
            )

            result = daily_scan.run_daily_scan(
                state_path,
                input_path,
                ensure_review=lambda root, monitor_id: "http://127.0.0.1:1/should-not-be-reported",
            )

            self.assertFalse(result["ok"])
            self.assertIn("扫描失败", result["message"])
            self.assertNotIn("review_url", result)
            self.assertNotIn("没有发现", result["message"])
            self.assertEqual(ledger.load_json(state_path)["seen_posts"], {})

    def test_review_runtime_failure_is_reported_after_a_successful_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor_root = Path(temp_dir) / "work" / "xiaohongshu-monitor"
            state_path = monitor_root / "monitor-a" / "state.json"
            input_path = Path(temp_dir) / "scan.json"
            ledger.save_json(state_path, ledger.new_state("AI 实习", 14, "Asia/Shanghai"))
            ledger.save_json(
                input_path,
                {
                    "run_at": "2026-07-18T21:00:00+08:00",
                    "queries": ["AI 产品实习"],
                    "candidates": [
                        {
                            "note_id": "0123456789abcdef01234567",
                            "title": "已成功写入的机会",
                            "url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
                            "published_at": "2026-07-18T19:00:00+08:00",
                            "classification": "verified_active",
                            "query": "AI 产品实习",
                        }
                    ],
                },
            )

            def unavailable_runtime(root: Path, monitor_id: str) -> str:
                raise OSError("port unavailable")

            result = daily_scan.run_daily_scan(state_path, input_path, ensure_review=unavailable_runtime)

            self.assertFalse(result["ok"])
            self.assertTrue(result["scan_completed"])
            self.assertIn("审核页启动失败", result["message"])
            self.assertNotIn("review_url", result)
            self.assertEqual(len(ledger.load_json(state_path)["seen_posts"]), 1)


if __name__ == "__main__":
    unittest.main()
