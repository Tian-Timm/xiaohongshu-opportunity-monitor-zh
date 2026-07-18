#!/usr/bin/env python3
"""Local-only review page for a monitor's newly discovered posts."""

from __future__ import annotations

import argparse
import copy
import html
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import urlopen

import ledger


REVIEW_PATH_RE = re.compile(r"^/monitors/([^/]+)/review$")
STATUS_LABELS = {
    "verified_active": "已确认",
    "manual_review": "请你核实",
}
RUNTIME_FILE = ".review-server.json"


def _monitor_state_path(monitor_root: Path, monitor_id: str) -> Path:
    monitor_id = unquote(monitor_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", monitor_id):
        raise ValueError("监测任务不存在")
    return monitor_root / monitor_id / "state.json"


def _timestamp(value: object, fallback: float) -> float:
    if not value:
        return fallback
    try:
        return ledger.parse_iso(str(value)).timestamp()
    except ValueError:
        return fallback


def _records_by_stable_id(state: dict) -> dict[str, dict]:
    return {
        record.get("stable_id", ""): record
        for record in state.get("seen_posts", {}).values()
        if record.get("stable_id") and record.get("status") in ledger.REPORTABLE
    }


def _default_queue(state: dict) -> list[str]:
    completed_at = state.get("last_run", {}).get("completed_at")
    reviews = state.get("user_reviews", {})
    known_today: list[dict] = []
    unknown_today: list[dict] = []
    deferred: list[dict] = []
    for record in _records_by_stable_id(state).values():
        stable_id = record["stable_id"]
        review = reviews.get(stable_id, {}).get("value")
        if record.get("notified_at") == completed_at and review is None:
            (known_today if record.get("published_at") else unknown_today).append(record)
        elif review == "later":
            deferred.append(record)
    known_today.sort(key=lambda record: _timestamp(record.get("published_at"), float("-inf")), reverse=True)
    unknown_today.sort(key=lambda record: _timestamp(record.get("first_seen_at"), float("-inf")), reverse=True)
    deferred.sort(
        key=lambda record: _timestamp(
            reviews.get(record["stable_id"], {}).get("updated_at"),
            float("inf"),
        )
    )
    return [record["stable_id"] for record in (*known_today, *unknown_today, *deferred)]


def _new_default_session(state: dict) -> dict:
    queue = _default_queue(state)
    reviews = state.get("user_reviews", {})
    session = {
        "view": "default",
        "scan_completed_at": state.get("last_run", {}).get("completed_at"),
        "queue": queue,
        "queue_kinds": {
            stable_id: "later" if reviews.get(stable_id, {}).get("value") == "later" else "today"
            for stable_id in queue
        },
        "position": 0,
        "current_id": queue[0] if queue else None,
        "deferred_ids": [],
        "counts": {"valuable": 0, "not_valuable": 0, "later": 0},
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    state["review_session"] = session
    return session


def _ensure_default_session(state: dict) -> tuple[dict, bool]:
    session = state.get("review_session")
    if not isinstance(session, dict) or session.get("view") != "default":
        return _new_default_session(state), True
    latest_scan = state.get("last_run", {}).get("completed_at")
    session_scan = session.get("scan_completed_at")
    if session_scan and session_scan != latest_scan:
        return _new_default_session(state), True
    if "scan_completed_at" not in session:
        session["scan_completed_at"] = latest_scan
        return session, True
    return session, False


def _session_item_is_eligible(state: dict, stable_id: str) -> bool:
    session = state.get("review_session", {})
    record = _records_by_stable_id(state).get(stable_id)
    if record is None:
        return False
    review = state.get("user_reviews", {}).get(stable_id, {}).get("value")
    kind = session.get("queue_kinds", {}).get(stable_id)
    if kind is None:
        kind = "later" if review == "later" else "today"
    return review == "later" if kind == "later" else review is None


def _reconcile_default_session(state: dict) -> bool:
    session = state.get("review_session", {})
    current_id = session.get("current_id")
    if not current_id or _session_item_is_eligible(state, current_id):
        return False
    queue = session.get("queue", [])
    position = int(session.get("position", 0)) + 1
    while position < len(queue) and not _session_item_is_eligible(state, queue[position]):
        position += 1
    session["position"] = position
    session["current_id"] = queue[position] if position < len(queue) else None
    return True


def _current_item(state: dict) -> dict | None:
    session = state.get("review_session", {})
    current_id = session.get("current_id")
    record = _records_by_stable_id(state).get(current_id)
    return ledger.report_view(record) if record else None


def _history_items(state: dict, view: str) -> list[dict]:
    reviews = state.get("user_reviews", {})
    records = []
    for record in _records_by_stable_id(state).values():
        value = reviews.get(record["stable_id"], {}).get("value")
        if (view == "unreviewed" and value is None) or value == view:
            records.append(record)
    records.sort(key=lambda record: _timestamp(record.get("first_seen_at"), float("-inf")), reverse=True)
    return [ledger.report_view(record) for record in records]


def _visible_item(state: dict, view: str) -> dict | None:
    if view == "default":
        return _current_item(state)
    items = _history_items(state, view)
    return items[0] if items else None


def _detail(label: str, value: object) -> str:
    if value in (None, "", []):
        return ""
    return (
        '<div class="detail"><dt>'
        + html.escape(label)
        + "</dt><dd>"
        + html.escape(str(value))
        + "</dd></div>"
    )


def _render_page(state: dict, error_message: str = "", view: str = "default") -> str:
    item = _visible_item(state, view)
    monitor_name = html.escape(str(state.get("monitor", {}).get("name") or "机会监测"))
    if item is None:
        if view == "default":
            counts = state.get("review_session", {}).get("counts", {})
            body = (
                '<section class="card empty"><h2>本轮完成汇总</h2>'
                '<div class="summary">'
                f'<span>值得看：{int(counts.get("valuable", 0))}</span>'
                f'<span>不感兴趣：{int(counts.get("not_valuable", 0))}</span>'
                f'<span>稍后看：{int(counts.get("later", 0))}</span>'
                '</div>'
                '<p><a href="?view=unreviewed">查看未标记记录</a> · '
                '<a href="?view=valuable">查看值得看记录</a></p>'
                '<form method="post"><button name="action" value="start_session">开始下一轮审核</button></form>'
                '</section>'
            )
        else:
            label = "未标记" if view == "unreviewed" else "值得看"
            body = f'<section class="card empty"><h2>没有{label}记录</h2></section>'
    else:
        status = STATUS_LABELS.get(item.get("status", ""), "请你核实")
        review_value = state.get("user_reviews", {}).get(item["stable_id"], {}).get("value")
        if view == "unreviewed":
            queue_label = "未标记记录"
        elif view == "valuable":
            queue_label = "值得看记录"
        else:
            queue_label = "稍后看" if review_value == "later" else "今日新增"
        published = item.get("published_text") or item.get("published_at")
        details = "".join(
            (
                _detail("作者", item.get("author")),
                _detail("公司", item.get("company")),
                _detail("岗位", item.get("role")),
                _detail("地点", item.get("location")),
                _detail("发布时间", published),
                _detail("需人工审核原因", item.get("reason")),
            )
        )
        stable_id = html.escape(str(item["stable_id"]), quote=True)
        body = f"""
        <section class="card">
          <div class="eyebrow">{queue_label} · {html.escape(status)}</div>
          <h2>{html.escape(str(item.get("title") or "未命名机会"))}</h2>
          <dl>{details}</dl>
          <a class="original" href="{html.escape(str(item['url']), quote=True)}" target="_blank" rel="noopener noreferrer">打开小红书原帖 ↗</a>
          <form method="post">
            <input type="hidden" name="view" value="{html.escape(view, quote=True)}">
            <input type="hidden" name="stable_id" value="{stable_id}">
            <button name="review" value="valuable">值得看</button>
            <button name="review" value="not_valuable">不感兴趣</button>
            <button name="review" value="later">稍后看</button>
          </form>
        </section>
        """
    alert = f'<div class="alert" role="alert">{html.escape(error_message)}</div>' if error_message else ""
    page_title = {"default": "今日审核", "unreviewed": "未标记记录", "valuable": "值得看记录"}[view]
    navigation = (
        '<nav><a href="?view=default">默认审核队列</a>'
        '<a href="?view=unreviewed">未标记</a>'
        '<a href="?view=valuable">值得看</a></nav>'
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{monitor_name} · 今日审核</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, "PingFang SC", "Microsoft YaHei", sans-serif; color: #241f20; background: #f7f3ed; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; background: radial-gradient(circle at top left, #fff 0, #f7f3ed 48%, #efe7dc 100%); }}
    main {{ width: min(760px, calc(100% - 32px)); margin: 0 auto; padding: 56px 0; }}
    header {{ margin-bottom: 28px; }}
    h1 {{ margin: 0 0 8px; font-size: clamp(28px, 6vw, 44px); letter-spacing: -.04em; }}
    header p {{ margin: 0; color: #73696b; }}
    nav {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 20px 0 28px; }} nav a {{ color: #6e4b50; font-weight: 700; }}
    .card {{ padding: clamp(24px, 5vw, 40px); border: 1px solid rgba(79, 55, 50, .12); border-radius: 24px; background: rgba(255,255,255,.9); box-shadow: 0 24px 70px rgba(70, 45, 39, .10); }}
    .eyebrow {{ color: #b24650; font-weight: 700; font-size: 14px; }}
    h2 {{ margin: 12px 0 28px; font-size: clamp(24px, 5vw, 34px); }}
    dl {{ display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 18px 28px; margin: 0 0 32px; }}
    .detail {{ min-width: 0; }} dt {{ margin-bottom: 5px; color: #897d7f; font-size: 13px; }} dd {{ margin: 0; overflow-wrap: anywhere; }}
    .original {{ display: inline-flex; padding: 13px 18px; border-radius: 12px; color: white; background: #241f20; text-decoration: none; font-weight: 700; }}
    form {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 28px; padding-top: 24px; border-top: 1px solid #eee6df; }}
    button {{ padding: 12px 16px; border: 1px solid #d9cdca; border-radius: 12px; background: #fff; color: #352d2f; font: inherit; font-weight: 700; cursor: pointer; }}
    button:first-of-type {{ border-color: #b24650; color: #a03943; }}
    .empty {{ text-align: center; }}
    .summary {{ display: flex; justify-content: center; flex-wrap: wrap; gap: 12px; margin: 20px 0; }} .summary span {{ padding: 10px 14px; border-radius: 999px; background: #f5efea; font-weight: 700; }}
    .alert {{ margin-bottom: 18px; padding: 14px 16px; border: 1px solid #e2a3a8; border-radius: 12px; color: #8f2731; background: #fff1f2; font-weight: 700; }}
    @media (max-width: 560px) {{ main {{ padding-top: 32px; }} dl {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body><main><header><h1>{page_title}</h1><p>{monitor_name}</p></header>{navigation}{alert}{body}</main></body>
</html>"""


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        monitor_root: Path,
        save_state: Callable[[Path, dict], None],
    ):
        self.monitor_root = monitor_root
        self.save_state = save_state
        super().__init__(address, ReviewRequestHandler)


class ReviewRequestHandler(BaseHTTPRequestHandler):
    server: ReviewServer

    def do_GET(self) -> None:
        request_url = urlsplit(self.path)
        if request_url.path == "/health":
            self._send_page("ok")
            return
        match = REVIEW_PATH_RE.fullmatch(request_url.path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            state_path = _monitor_state_path(self.server.monitor_root, match.group(1))
            state = ledger.load_json(state_path)
        except (OSError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        view = parse_qs(request_url.query).get("view", ["default"])[0]
        if view not in {"default", "unreviewed", "valuable"}:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        created = False
        reconciled = False
        if view == "default":
            _, created = _ensure_default_session(state)
            reconciled = _reconcile_default_session(state)
        error_message = "审核队列已变化，已为你显示下一条符合条件的帖子" if reconciled else ""
        if created or reconciled:
            try:
                self.server.save_state(state_path, state)
            except OSError:
                error_message = "会话进度暂时无法保存，请重试"
        page = _render_page(state, error_message, view)
        self._send_page(page)

    def do_POST(self) -> None:
        match = REVIEW_PATH_RE.fullmatch(urlsplit(self.path).path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            action = form.get("action", [""])[0]
            view = form.get("view", ["default"])[0]
            if view not in {"default", "unreviewed", "valuable"}:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            stable_id = form.get("stable_id", [""])[0]
            review = form.get("review", [""])[0]
            state_path = _monitor_state_path(self.server.monitor_root, match.group(1))
            state = ledger.load_json(state_path)
            if action == "start_session":
                updated = copy.deepcopy(state)
                _new_default_session(updated)
                try:
                    self.server.save_state(state_path, updated)
                except OSError:
                    self._send_page(_render_page(state, "保存失败，请重试"), HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self._send_page(_render_page(updated))
                return
            if view == "default":
                _ensure_default_session(state)
            item = _visible_item(state, view)
            if not item or stable_id != item.get("stable_id"):
                self._send_page(_render_page(state, "当前审核内容已变化，请刷新后重试", view), HTTPStatus.CONFLICT)
                return
            if review not in ledger.REVIEW_VALUES:
                self._send_page(_render_page(state, "请选择有效的审核状态", view), HTTPStatus.BAD_REQUEST)
                return
            updated = copy.deepcopy(state)
            updated.setdefault("user_reviews", {})[stable_id] = {
                "value": review,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if view == "default":
                updated_session = updated["review_session"]
                updated_session["counts"][review] = int(updated_session["counts"].get(review, 0)) + 1
                if review == "later" and stable_id not in updated_session["deferred_ids"]:
                    updated_session["deferred_ids"].append(stable_id)
                updated_session["position"] = int(updated_session.get("position", 0)) + 1
                position = updated_session["position"]
                queue = updated_session.get("queue", [])
                updated_session["current_id"] = queue[position] if position < len(queue) else None
            try:
                self.server.save_state(state_path, updated)
            except OSError:
                self._send_page(_render_page(state, "保存失败，请重试", view), HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            page = _render_page(updated, view=view)
        except (OSError, UnicodeError, ValueError):
            self._send_page("<!doctype html><meta charset=\"utf-8\"><p>保存失败，请重试</p>", HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_page(page)

    def _send_page(self, page: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(
    monitor_root: Path | str,
    host: str = "127.0.0.1",
    port: int = 0,
    save_state: Callable[[Path, dict], None] | None = None,
) -> ReviewServer:
    """Create a local review server; callers control its lifecycle."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("review server must bind to the local machine")
    return ReviewServer((host, port), Path(monitor_root), save_state or ledger.save_json)


def _runtime_path(monitor_root: Path) -> Path:
    return monitor_root / RUNTIME_FILE


def _read_runtime(monitor_root: Path) -> dict:
    try:
        return ledger.load_json(_runtime_path(monitor_root))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _healthy(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.4) as response:
            return response.status == HTTPStatus.OK and response.read() == b"ok"
    except OSError:
        return False


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _review_url(port: int, monitor_id: str) -> str:
    return f"http://127.0.0.1:{port}/monitors/{quote(monitor_id, safe='')}/review"


def ensure_running(monitor_root: Path | str, monitor_id: str) -> str:
    """Start or reuse the local runtime and return this monitor's review URL."""
    monitor_root = Path(monitor_root).resolve()
    state_path = _monitor_state_path(monitor_root, monitor_id)
    if not state_path.is_file():
        raise OSError(f"monitor state does not exist: {state_path}")
    runtime = _read_runtime(monitor_root)
    port = int(runtime.get("port", 0) or 0)
    if runtime.get("monitor_root") == str(monitor_root) and port and _healthy(port):
        return _review_url(port, monitor_id)

    port = _available_port()
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--monitor-root",
        str(monitor_root),
        "--port",
        str(port),
        "--metadata",
        str(_runtime_path(monitor_root)),
    ]
    options: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(command, **options)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _healthy(port):
            # The detached child intentionally outlives this launcher. Mark the
            # launcher handle complete so Python does not report it as leaked.
            process.returncode = 0
            return _review_url(port, monitor_id)
        if process.poll() is not None:
            break
        time.sleep(0.05)
    raise OSError("local review runtime did not start")


def stop_running(monitor_root: Path | str) -> None:
    """Stop a runtime started by ensure_running; primarily useful for lifecycle management and tests."""
    monitor_root = Path(monitor_root).resolve()
    runtime = _read_runtime(monitor_root)
    pid = int(runtime.get("pid", 0) or 0)
    port = int(runtime.get("port", 0) or 0)
    owns_runtime = runtime.get("monitor_root") == str(monitor_root)
    if owns_runtime and pid and port and _healthy(port):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if port:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _healthy(port):
            time.sleep(0.05)
    try:
        _runtime_path(monitor_root).unlink()
    except FileNotFoundError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--monitor-root", required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--metadata")
    args = parser.parse_args()
    server = create_server(args.monitor_root, port=args.port)
    if args.metadata:
        ledger.save_json(
            Path(args.metadata),
            {"pid": os.getpid(), "port": server.server_port, "monitor_root": str(Path(args.monitor_root).resolve())},
        )
    print(f"审核页已启动：http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
