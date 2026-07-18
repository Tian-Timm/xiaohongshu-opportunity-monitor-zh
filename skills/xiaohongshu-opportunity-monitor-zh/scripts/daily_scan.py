#!/usr/bin/env python3
"""Complete one scan and make its monitor-scoped local review page available."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
from typing import Callable

import ledger
import review_server


ReviewRuntime = Callable[[Path, str], str]


def run_daily_scan(
    state_path: Path | str,
    input_path: Path | str,
    ensure_review: ReviewRuntime | None = None,
) -> dict:
    """Persist a successful scan, then return its working local review link."""
    state_path = Path(state_path)
    input_path = Path(input_path)
    ensure_review = ensure_review or review_server.ensure_running
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            ledger.command_ingest(argparse.Namespace(state=str(state_path), input=str(input_path)))
        scan_result = json.loads(output.getvalue())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "scan_completed": False, "message": f"扫描失败：{exc}"}
    monitor_id = state_path.parent.name
    monitor_root = state_path.parent.parent
    try:
        review_url = ensure_review(monitor_root, monitor_id)
    except OSError as exc:
        return {
            "ok": False,
            "scan_completed": True,
            "counts": scan_result["counts"],
            "message": f"扫描已完成，但审核页启动失败：{exc}",
        }
    return {
        "ok": True,
        "scan_completed": True,
        "message": "扫描成功",
        "counts": scan_result["counts"],
        "new_reportable": scan_result["new_reportable"],
        "action_label": "打开今日审核页",
        "review_url": review_url,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    result = run_daily_scan(args.state, args.input)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
