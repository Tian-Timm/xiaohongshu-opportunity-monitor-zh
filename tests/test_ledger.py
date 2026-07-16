from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = ROOT / "skills" / "xiaohongshu-opportunity-monitor-zh" / "scripts" / "ledger.py"
SPEC = importlib.util.spec_from_file_location("xhs_ledger", LEDGER_PATH)
ledger = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ledger)


class LedgerTests(unittest.TestCase):
    def test_canonicalize_url_removes_tracking_parameters(self) -> None:
        url = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567/?xsec_token=secret&utm_source=test&keep=1#top"
        self.assertEqual(
            ledger.canonicalize_url(url),
            "https://www.xiaohongshu.com/explore/0123456789abcdef01234567?keep=1",
        )

    def test_ingest_filters_age_deduplicates_and_preserves_unknown_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            payload_path = Path(temp_dir) / "scan.json"
            ledger.save_json(state_path, ledger.new_state("test", 14, "Asia/Shanghai"))
            ledger.save_json(
                payload_path,
                {
                    "run_at": "2026-07-17T21:00:00+08:00",
                    "queries": ["AI 产品实习"],
                    "candidates": [
                        {
                            "note_id": "aaaaaaaaaaaaaaaaaaaaaaaa",
                            "title": "AI 产品实习",
                            "author": "招聘账号",
                            "url": "https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa?xsec_token=one",
                            "published_at": "2026-07-16T12:00:00+08:00",
                            "classification": "verified_active",
                            "query": "AI 产品实习",
                        },
                        {
                            "note_id": "bbbbbbbbbbbbbbbbbbbbbbbb",
                            "title": "过期实习",
                            "url": "https://www.xiaohongshu.com/explore/bbbbbbbbbbbbbbbbbbbbbbbb",
                            "published_at": "2026-06-30T12:00:00+08:00",
                            "classification": "verified_active",
                            "query": "AI 产品实习",
                        },
                        {
                            "note_id": "cccccccccccccccccccccccc",
                            "title": "图片里的招聘信息",
                            "url": "https://www.xiaohongshu.com/explore/cccccccccccccccccccccccc",
                            "classification": "verified_active",
                            "query": "AI 产品实习",
                        },
                        {
                            "note_id": "aaaaaaaaaaaaaaaaaaaaaaaa",
                            "title": "AI 产品实习",
                            "author": "招聘账号",
                            "url": "https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa?xsec_token=two",
                            "published_at": "2026-07-16T12:00:00+08:00",
                            "classification": "verified_active",
                            "query": "AI 产品实习",
                        },
                    ],
                },
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = ledger.command_ingest(argparse.Namespace(state=str(state_path), input=str(payload_path)))
            self.assertEqual(result, 0)
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["counts"]["duplicates"], 1)
            self.assertEqual(summary["counts"]["expired_by_age"], 1)
            self.assertEqual(summary["counts"]["verified_active"], 1)
            self.assertEqual(summary["counts"]["manual_review"], 1)

            state = ledger.load_json(state_path)
            statuses = {record["status"] for record in state["seen_posts"].values()}
            self.assertEqual(statuses, {"verified_active", "manual_review", "expired_by_age"})

            verified = next(record for record in state["seen_posts"].values() if record["status"] == "verified_active")
            with contextlib.redirect_stdout(io.StringIO()):
                ledger.command_mark(
                    argparse.Namespace(
                        state=str(state_path),
                        valuable=[verified["stable_id"]],
                        not_valuable=[],
                        later=[],
                    )
                )

            listed = io.StringIO()
            with contextlib.redirect_stdout(listed):
                ledger.command_list(argparse.Namespace(state=str(state_path), review="all"))
            rows = json.loads(listed.getvalue())
            self.assertEqual(len(rows), 2)
            self.assertNotIn("expired_by_age", {row["status"] for row in rows})

            stats_output = io.StringIO()
            with contextlib.redirect_stdout(stats_output):
                ledger.command_stats(argparse.Namespace(state=str(state_path)))
            stats = json.loads(stats_output.getvalue())
            self.assertEqual(stats["reviews"]["valuable"], 1)
            self.assertEqual(stats["reviews"]["unreviewed"], 1)


if __name__ == "__main__":
    unittest.main()
