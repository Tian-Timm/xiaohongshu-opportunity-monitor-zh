from __future__ import annotations

import contextlib
import importlib.util
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "xiaohongshu-opportunity-monitor-zh" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ledger = load_module("xhs_ledger_for_review_tests", SCRIPTS / "ledger.py")
review_server = load_module("xhs_review_server", SCRIPTS / "review_server.py")


class ReviewServerTests(unittest.TestCase):
    def test_review_runtime_starts_once_and_reuses_the_working_local_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor_root = Path(temp_dir)
            state = ledger.new_state("AI 实习", 14, "Asia/Shanghai")
            state["last_run"] = {"completed_at": "2026-07-18T21:00:00+08:00"}
            state_path = monitor_root / "monitor-a" / "state.json"
            ledger.save_json(state_path, state)
            try:
                first_url = review_server.ensure_running(monitor_root, "monitor-a")
                second_url = review_server.ensure_running(monitor_root, "monitor-a")
                with urlopen(first_url) as response:
                    page = response.read().decode("utf-8")
            finally:
                review_server.stop_running(monitor_root)

            self.assertEqual(first_url, second_url)
            self.assertIn("今日审核", page)
            self.assertTrue(first_url.startswith("http://127.0.0.1:"))

    def test_default_session_orders_today_items_then_deferred_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor_root = Path(temp_dir)
            completed_at = "2026-07-18T21:00:00+08:00"
            state = ledger.new_state("AI 实习", 14, "Asia/Shanghai")
            state["last_run"] = {"completed_at": completed_at}
            specs = (
                ("later-new", "稍后看较新", None, "2026-07-10T10:00:00+08:00", "later", "2026-07-17T10:00:00+08:00"),
                ("unknown-old", "日期不明较早", None, "2026-07-18T08:00:00+08:00", None, None),
                ("known-old", "有日期较早", "2026-07-17T12:00:00+08:00", "2026-07-18T12:00:00+08:00", None, None),
                ("later-old", "稍后看最早", None, "2026-07-09T10:00:00+08:00", "later", "2026-07-16T10:00:00+08:00"),
                ("known-new", "有日期最新", "2026-07-18T20:00:00+08:00", "2026-07-18T20:30:00+08:00", None, None),
                ("unknown-new", "日期不明较新", None, "2026-07-18T18:00:00+08:00", None, None),
            )
            for index, (key, title, published_at, first_seen_at, review, reviewed_at) in enumerate(specs):
                stable_id = f"XHS-ORDER-{index}"
                state["seen_posts"][key] = {
                    "stable_id": stable_id,
                    "title": title,
                    "published_at": published_at,
                    "first_seen_at": first_seen_at,
                    "latest_url": f"https://www.xiaohongshu.com/explore/{index:024x}",
                    "canonical_url": f"https://www.xiaohongshu.com/explore/{index:024x}",
                    "status": "verified_active",
                    "notified_at": completed_at if review is None else "2026-07-10T21:00:00+08:00",
                }
                if review:
                    state["user_reviews"][stable_id] = {"value": review, "updated_at": reviewed_at}
            state_path = monitor_root / "monitor-a" / "state.json"
            ledger.save_json(state_path, state)

            expected = ["有日期最新", "有日期较早", "日期不明较新", "日期不明较早", "稍后看最早", "稍后看较新"]
            seen = []
            with running_server(monitor_root) as base_url:
                for _ in expected:
                    with urlopen(f"{base_url}/monitors/monitor-a/review") as response:
                        page = response.read().decode("utf-8")
                    title = next(candidate for candidate in expected if candidate in page)
                    seen.append(title)
                    saved = ledger.load_json(state_path)
                    stable_id = saved["review_session"]["current_id"]
                    request = Request(
                        f"{base_url}/monitors/monitor-a/review",
                        data=urlencode({"stable_id": stable_id, "review": "valuable"}).encode(),
                        method="POST",
                    )
                    with urlopen(request) as response:
                        response.read()

            self.assertEqual(seen, expected)

    def test_default_session_resumes_current_item_and_explains_queue_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor_root = Path(temp_dir)
            state = ledger.new_state("AI 实习", 14, "Asia/Shanghai")
            state["last_run"] = {"completed_at": "2026-07-18T21:00:00+08:00"}
            for index, title in enumerate(("恢复时仍是这一条", "队列变化后的下一条")):
                state["seen_posts"][f"post-{index}"] = {
                    "stable_id": f"XHS-RESUME-{index}",
                    "title": title,
                    "published_at": f"2026-07-18T{20 - index:02d}:00:00+08:00",
                    "first_seen_at": f"2026-07-18T{20 - index:02d}:30:00+08:00",
                    "latest_url": f"https://www.xiaohongshu.com/explore/{index:024x}",
                    "canonical_url": f"https://www.xiaohongshu.com/explore/{index:024x}",
                    "status": "verified_active",
                    "notified_at": "2026-07-18T21:00:00+08:00",
                }
            state_path = monitor_root / "monitor-a" / "state.json"
            ledger.save_json(state_path, state)

            with running_server(monitor_root) as base_url:
                with urlopen(f"{base_url}/monitors/monitor-a/review") as response:
                    first_visit = response.read().decode("utf-8")
            with running_server(monitor_root) as base_url:
                with urlopen(f"{base_url}/monitors/monitor-a/review") as response:
                    resumed_visit = response.read().decode("utf-8")

            self.assertIn("恢复时仍是这一条", first_visit)
            self.assertIn("恢复时仍是这一条", resumed_visit)

            changed = ledger.load_json(state_path)
            changed["seen_posts"]["post-0"]["status"] = "excluded_irrelevant"
            ledger.save_json(state_path, changed)
            with running_server(monitor_root) as base_url:
                with urlopen(f"{base_url}/monitors/monitor-a/review") as response:
                    changed_visit = response.read().decode("utf-8")

            self.assertNotIn("恢复时仍是这一条", changed_visit)
            self.assertIn("队列变化后的下一条", changed_visit)
            self.assertIn("审核队列已变化", changed_visit)
            self.assertEqual(ledger.load_json(state_path)["review_session"]["current_id"], "XHS-RESUME-1")

    def test_deferred_item_waits_until_the_next_default_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor_root = Path(temp_dir)
            state = ledger.new_state("AI 实习", 14, "Asia/Shanghai")
            state["last_run"] = {"completed_at": "2026-07-18T21:00:00+08:00"}
            for index, title in enumerate(("本轮稍后看", "本轮继续审核")):
                state["seen_posts"][f"post-{index}"] = {
                    "stable_id": f"XHS-DEFER-{index}",
                    "title": title,
                    "published_at": f"2026-07-18T{20 - index:02d}:00:00+08:00",
                    "first_seen_at": f"2026-07-18T{20 - index:02d}:30:00+08:00",
                    "latest_url": f"https://www.xiaohongshu.com/explore/{index:024x}",
                    "canonical_url": f"https://www.xiaohongshu.com/explore/{index:024x}",
                    "status": "verified_active",
                    "notified_at": "2026-07-18T21:00:00+08:00",
                }
            state_path = monitor_root / "monitor-a" / "state.json"
            ledger.save_json(state_path, state)

            with running_server(monitor_root) as base_url:
                defer_request = Request(
                    f"{base_url}/monitors/monitor-a/review",
                    data=urlencode({"stable_id": "XHS-DEFER-0", "review": "later"}).encode(),
                    method="POST",
                )
                with urlopen(defer_request) as response:
                    after_defer = response.read().decode("utf-8")
                self.assertNotIn("本轮稍后看", after_defer)
                self.assertIn("本轮继续审核", after_defer)

                finish_request = Request(
                    f"{base_url}/monitors/monitor-a/review",
                    data=urlencode({"stable_id": "XHS-DEFER-1", "review": "valuable"}).encode(),
                    method="POST",
                )
                with urlopen(finish_request) as response:
                    response.read()

                next_session_request = Request(
                    f"{base_url}/monitors/monitor-a/review",
                    data=urlencode({"action": "start_session"}).encode(),
                    method="POST",
                )
                with urlopen(next_session_request) as response:
                    next_session = response.read().decode("utf-8")

            self.assertIn("本轮稍后看", next_session)
            self.assertIn("稍后看 · 已确认", next_session)
            saved = ledger.load_json(state_path)
            self.assertEqual(saved["user_reviews"]["XHS-DEFER-0"]["value"], "later")
            self.assertEqual(saved["seen_posts"]["post-0"]["notified_at"], "2026-07-18T21:00:00+08:00")

    def test_history_entries_are_separate_from_the_default_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor_root = Path(temp_dir)
            state = ledger.new_state("AI 实习", 14, "Asia/Shanghai")
            state["last_run"] = {"completed_at": "2026-07-18T21:00:00+08:00"}
            records = (
                ("today", "今日默认队列机会", "XHS-TODAY", "2026-07-18T21:00:00+08:00", "2026-07-18T20:00:00+08:00"),
                ("old", "历史未标记机会", "XHS-OLD", "2026-07-10T21:00:00+08:00", "2026-07-10T20:00:00+08:00"),
            )
            for index, (key, title, stable_id, notified_at, first_seen_at) in enumerate(records):
                state["seen_posts"][key] = {
                    "stable_id": stable_id,
                    "title": title,
                    "first_seen_at": first_seen_at,
                    "latest_url": f"https://www.xiaohongshu.com/explore/{index:024x}",
                    "canonical_url": f"https://www.xiaohongshu.com/explore/{index:024x}",
                    "status": "verified_active",
                    "notified_at": notified_at,
                }
            state_path = monitor_root / "monitor-a" / "state.json"
            ledger.save_json(state_path, state)

            with running_server(monitor_root) as base_url:
                with urlopen(f"{base_url}/monitors/monitor-a/review") as response:
                    default_page = response.read().decode("utf-8")
                self.assertIn("今日默认队列机会", default_page)
                self.assertNotIn("历史未标记机会", default_page)

                mark_request = Request(
                    f"{base_url}/monitors/monitor-a/review",
                    data=urlencode({"stable_id": "XHS-TODAY", "review": "valuable"}).encode(),
                    method="POST",
                )
                with urlopen(mark_request) as response:
                    completed_default = response.read().decode("utf-8")
                self.assertNotIn("历史未标记机会", completed_default)

                with urlopen(f"{base_url}/monitors/monitor-a/review?view=unreviewed") as response:
                    unreviewed_page = response.read().decode("utf-8")
                with urlopen(f"{base_url}/monitors/monitor-a/review?view=valuable") as response:
                    valuable_page = response.read().decode("utf-8")

            self.assertIn("历史未标记机会", unreviewed_page)
            self.assertIn("未标记记录", unreviewed_page)
            self.assertIn("今日默认队列机会", valuable_page)
            self.assertIn("值得看记录", valuable_page)
            self.assertIn("?view=unreviewed", valuable_page)
            self.assertIn("?view=valuable", unreviewed_page)

    def test_history_record_can_be_reclassified_without_becoming_today_new(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor_root = Path(temp_dir)
            state = ledger.new_state("AI 实习", 14, "Asia/Shanghai")
            state["last_run"] = {"completed_at": "2026-07-18T21:00:00+08:00"}
            state["seen_posts"] = {
                "history": {
                    "stable_id": "XHS-HISTORY",
                    "title": "可改判的历史机会",
                    "first_seen_at": "2026-07-10T20:00:00+08:00",
                    "latest_url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
                    "canonical_url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
                    "status": "verified_active",
                    "notified_at": "2026-07-10T21:00:00+08:00",
                }
            }
            state["user_reviews"]["XHS-HISTORY"] = {
                "value": "valuable",
                "updated_at": "2026-07-11T09:00:00+08:00",
            }
            state_path = monitor_root / "monitor-a" / "state.json"
            ledger.save_json(state_path, state)

            with running_server(monitor_root) as base_url:
                change_request = Request(
                    f"{base_url}/monitors/monitor-a/review",
                    data=urlencode({"view": "valuable", "stable_id": "XHS-HISTORY", "review": "later"}).encode(),
                    method="POST",
                )
                with urlopen(change_request) as response:
                    valuable_after_change = response.read().decode("utf-8")
                self.assertNotIn("可改判的历史机会", valuable_after_change)

                next_session_request = Request(
                    f"{base_url}/monitors/monitor-a/review",
                    data=urlencode({"action": "start_session"}).encode(),
                    method="POST",
                )
                with urlopen(next_session_request) as response:
                    default_page = response.read().decode("utf-8")

            self.assertIn("可改判的历史机会", default_page)
            self.assertIn("稍后看 · 已确认", default_page)
            self.assertNotIn("今日新增 · 已确认", default_page)
            saved = ledger.load_json(state_path)
            self.assertIn("history", saved["seen_posts"])
            self.assertEqual(saved["user_reviews"]["XHS-HISTORY"]["value"], "later")
            self.assertEqual(saved["seen_posts"]["history"]["notified_at"], "2026-07-10T21:00:00+08:00")

    def test_completed_default_session_shows_round_summary_and_history_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor_root = Path(temp_dir)
            state = ledger.new_state("AI 实习", 14, "Asia/Shanghai")
            state["last_run"] = {"completed_at": "2026-07-18T21:00:00+08:00"}
            for index in range(3):
                state["seen_posts"][f"post-{index}"] = {
                    "stable_id": f"XHS-SUMMARY-{index}",
                    "title": f"汇总机会 {index}",
                    "published_at": f"2026-07-18T{20 - index:02d}:00:00+08:00",
                    "latest_url": f"https://www.xiaohongshu.com/explore/{index:024x}",
                    "canonical_url": f"https://www.xiaohongshu.com/explore/{index:024x}",
                    "status": "verified_active",
                    "notified_at": "2026-07-18T21:00:00+08:00",
                }
            state_path = monitor_root / "monitor-a" / "state.json"
            ledger.save_json(state_path, state)

            completion_page = ""
            with running_server(monitor_root) as base_url:
                for index, review in enumerate(("valuable", "not_valuable", "later")):
                    request = Request(
                        f"{base_url}/monitors/monitor-a/review",
                        data=urlencode({"stable_id": f"XHS-SUMMARY-{index}", "review": review}).encode(),
                        method="POST",
                    )
                    with urlopen(request) as response:
                        completion_page = response.read().decode("utf-8")

            self.assertIn("本轮完成汇总", completion_page)
            self.assertIn("值得看：1", completion_page)
            self.assertIn("不感兴趣：1", completion_page)
            self.assertIn("稍后看：1", completion_page)
            self.assertIn('href="?view=unreviewed"', completion_page)
            self.assertIn('href="?view=valuable"', completion_page)

    def test_review_page_shows_one_today_item_with_agent_details_and_original_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor_root = Path(temp_dir)
            state = ledger.new_state("AI 实习", 14, "Asia/Shanghai")
            signed_url = (
                "https://www.xiaohongshu.com/search_result/0123456789abcdef01234567"
                "?xsec_token=signed-token&xsec_source=pc_search"
            )
            state["last_run"] = {"completed_at": "2026-07-18T21:00:00+08:00"}
            state["seen_posts"] = {
                "0123456789abcdef01234567": {
                    "stable_id": "XHS-234567",
                    "note_id": "0123456789abcdef01234567",
                    "title": "AI 产品实习",
                    "author": "招聘账号",
                    "company": "示例科技",
                    "role": "产品实习生",
                    "location": "上海",
                    "published_text": "2 小时前",
                    "published_at": "2026-07-18T19:00:00+08:00",
                    "canonical_url": "https://www.xiaohongshu.com/search_result/0123456789abcdef01234567",
                    "latest_url": signed_url,
                    "reason": "岗位性质需查看图片确认",
                    "status": "manual_review",
                    "notified_at": "2026-07-18T21:00:00+08:00",
                },
                "fedcba9876543210fedcba98": {
                    "stable_id": "XHS-DCBA98",
                    "title": "昨天的机会",
                    "canonical_url": "https://www.xiaohongshu.com/explore/fedcba9876543210fedcba98",
                    "latest_url": "https://www.xiaohongshu.com/explore/fedcba9876543210fedcba98",
                    "status": "verified_active",
                    "notified_at": "2026-07-17T21:00:00+08:00",
                },
            }
            state_path = monitor_root / "monitor-a" / "state.json"
            ledger.save_json(state_path, state)

            with running_server(monitor_root) as base_url:
                with urlopen(f"{base_url}/monitors/monitor-a/review") as response:
                    page = response.read().decode("utf-8")

            self.assertIn("AI 产品实习", page)
            self.assertNotIn("昨天的机会", page)
            self.assertIn("请你核实", page)
            self.assertIn("示例科技", page)
            self.assertIn("产品实习生", page)
            self.assertIn("上海", page)
            self.assertIn("岗位性质需查看图片确认", page)
            self.assertIn(f'href="{signed_url.replace("&", "&amp;")}"', page)
            self.assertIn('target="_blank"', page)
            self.assertNotIn('href="https://www.xiaohongshu.com/search_result/0123456789abcdef01234567"', page)

    def test_successful_review_is_saved_before_the_page_advances(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor_root = Path(temp_dir)
            state = ledger.new_state("AI 实习", 14, "Asia/Shanghai")
            state["last_run"] = {"completed_at": "2026-07-18T21:00:00+08:00"}
            state["seen_posts"] = {
                "first": {
                    "stable_id": "XHS-FIRST",
                    "title": "第一条机会",
                    "latest_url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
                    "canonical_url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
                    "status": "verified_active",
                    "notified_at": "2026-07-18T21:00:00+08:00",
                },
                "second": {
                    "stable_id": "XHS-SECOND",
                    "title": "第二条机会",
                    "latest_url": "https://www.xiaohongshu.com/explore/fedcba9876543210fedcba98",
                    "canonical_url": "https://www.xiaohongshu.com/explore/fedcba9876543210fedcba98",
                    "status": "manual_review",
                    "notified_at": "2026-07-18T21:00:00+08:00",
                },
            }
            state_path = monitor_root / "monitor-a" / "state.json"
            ledger.save_json(state_path, state)

            with running_server(monitor_root) as base_url:
                request = Request(
                    f"{base_url}/monitors/monitor-a/review",
                    data=urlencode({"stable_id": "XHS-FIRST", "review": "valuable"}).encode(),
                    method="POST",
                )
                with urlopen(request) as response:
                    page = response.read().decode("utf-8")

            self.assertNotIn("第一条机会", page)
            self.assertIn("第二条机会", page)
            saved_review = ledger.load_json(state_path)["user_reviews"]["XHS-FIRST"]
            self.assertEqual(saved_review["value"], "valuable")
            self.assertTrue(saved_review["updated_at"])

    def test_failed_review_keeps_the_current_item_and_shows_a_retry_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor_root = Path(temp_dir)
            state = ledger.new_state("AI 实习", 14, "Asia/Shanghai")
            state["last_run"] = {"completed_at": "2026-07-18T21:00:00+08:00"}
            state["seen_posts"] = {
                "post": {
                    "stable_id": "XHS-CURRENT",
                    "title": "不能丢失的当前机会",
                    "latest_url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
                    "canonical_url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
                    "status": "verified_active",
                    "notified_at": "2026-07-18T21:00:00+08:00",
                }
            }
            state_path = monitor_root / "monitor-a" / "state.json"
            ledger.save_json(state_path, state)

            def reject_write(path: Path, value: dict) -> None:
                raise OSError("disk unavailable")

            with running_server(monitor_root, save_state=reject_write) as base_url:
                request = Request(
                    f"{base_url}/monitors/monitor-a/review",
                    data=urlencode({"stable_id": "XHS-CURRENT", "review": "not_valuable"}).encode(),
                    method="POST",
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request)
                self.assertEqual(raised.exception.code, 500)
                failure_page = raised.exception.read().decode("utf-8")

                with urlopen(f"{base_url}/monitors/monitor-a/review") as response:
                    retry_page = response.read().decode("utf-8")

            self.assertIn("不能丢失的当前机会", failure_page)
            self.assertIn("保存失败，请重试", failure_page)
            self.assertIn("不能丢失的当前机会", retry_page)
            self.assertEqual(ledger.load_json(state_path)["user_reviews"], {})

    def test_each_review_choice_is_persisted_through_the_review_page(self) -> None:
        choices = {
            "valuable": "值得看",
            "not_valuable": "不感兴趣",
            "later": "稍后看",
        }
        for value, label in choices.items():
            with self.subTest(review=value), tempfile.TemporaryDirectory() as temp_dir:
                monitor_root = Path(temp_dir)
                state = ledger.new_state("AI 实习", 14, "Asia/Shanghai")
                state["last_run"] = {"completed_at": "2026-07-18T21:00:00+08:00"}
                state["seen_posts"] = {
                    "post": {
                        "stable_id": "XHS-CHOICE",
                        "title": "待选择状态的机会",
                        "latest_url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
                        "canonical_url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
                        "status": "verified_active",
                        "notified_at": "2026-07-18T21:00:00+08:00",
                    }
                }
                state_path = monitor_root / "monitor-a" / "state.json"
                ledger.save_json(state_path, state)

                with running_server(monitor_root) as base_url:
                    with urlopen(f"{base_url}/monitors/monitor-a/review") as response:
                        page = response.read().decode("utf-8")
                    self.assertIn(f'value="{value}">{label}</button>', page)

                    request = Request(
                        f"{base_url}/monitors/monitor-a/review",
                        data=urlencode({"stable_id": "XHS-CHOICE", "review": value}).encode(),
                        method="POST",
                    )
                    with urlopen(request) as response:
                        response.read()

                self.assertEqual(
                    ledger.load_json(state_path)["user_reviews"]["XHS-CHOICE"]["value"],
                    value,
                )

    def test_monitor_pages_and_review_writes_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor_root = Path(temp_dir)
            state_paths = {}
            for monitor_id, title in (("monitor-a", "甲任务机会"), ("monitor-b", "乙任务机会")):
                state = ledger.new_state(monitor_id, 14, "Asia/Shanghai")
                state["last_run"] = {"completed_at": "2026-07-18T21:00:00+08:00"}
                state["seen_posts"] = {
                    "post": {
                        "stable_id": "XHS-SAME-ID",
                        "title": title,
                        "latest_url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
                        "canonical_url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
                        "status": "verified_active",
                        "notified_at": "2026-07-18T21:00:00+08:00",
                    }
                }
                state_path = monitor_root / monitor_id / "state.json"
                ledger.save_json(state_path, state)
                state_paths[monitor_id] = state_path

            with running_server(monitor_root) as base_url:
                with urlopen(f"{base_url}/monitors/monitor-a/review") as response:
                    page_a = response.read().decode("utf-8")
                self.assertIn("甲任务机会", page_a)
                self.assertNotIn("乙任务机会", page_a)

                request = Request(
                    f"{base_url}/monitors/monitor-a/review",
                    data=urlencode({"stable_id": "XHS-SAME-ID", "review": "valuable"}).encode(),
                    method="POST",
                )
                with urlopen(request) as response:
                    response.read()

                with urlopen(f"{base_url}/monitors/monitor-b/review") as response:
                    page_b = response.read().decode("utf-8")

            self.assertIn("乙任务机会", page_b)
            self.assertEqual(
                ledger.load_json(state_paths["monitor-a"])["user_reviews"]["XHS-SAME-ID"]["value"],
                "valuable",
            )
            self.assertEqual(ledger.load_json(state_paths["monitor-b"])["user_reviews"], {})


@contextlib.contextmanager
def running_server(monitor_root: Path, **options):
    server = review_server.create_server(monitor_root, port=0, **options)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


if __name__ == "__main__":
    unittest.main()
