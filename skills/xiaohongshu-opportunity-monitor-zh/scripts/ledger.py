#!/usr/bin/env python3
"""Deterministic state, age filtering, deduplication, and review labels."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo


VERSION = 1
DEFAULT_TZ = "Asia/Shanghai"
NOTE_ID_RE = re.compile(r"/(?:search_result|explore)/([0-9a-f]{24})(?:[/?]|$)", re.I)
# These values are removed only from the internal deduplication URL. Some of
# them, especially xsec_token, are required when a person opens the post.
DEDUP_QUERY_KEYS = {
    "xsec_token",
    "xsec_source",
    "source",
    "channel_type",
    "parent_page_channel_type",
}
REVIEW_VALUES = {"valuable", "not_valuable", "later"}
REPORTABLE = {"verified_active", "manual_review"}


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
    temp.replace(path)


def parse_iso(value: str) -> datetime:
    value = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp must include a timezone: {value}")
    return parsed


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    kept = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in DEDUP_QUERY_KEYS or lowered.startswith("utm_"):
            continue
        kept.append((key, value))
    path = re.sub(r"/+$", "", parts.path) or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(kept), ""))


def report_url_error(url: str) -> str:
    if not url:
        return "reportable candidate is missing its original URL"
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in {"xiaohongshu.com", "www.xiaohongshu.com"} and re.fullmatch(
        r"/search_result/[0-9a-f]{24}/?", parts.path, re.I
    ):
        query = {key.lower(): value for key, value in parse_qsl(parts.query, keep_blank_values=True)}
        if not query.get("xsec_token"):
            return (
                "Xiaohongshu search_result URL is missing xsec_token; "
                "capture the complete href from the page instead of rebuilding it from note_id"
            )
    return ""


def report_view(record: dict) -> dict:
    """Expose a single unambiguous URL while keeping dedup fields internal."""
    url = record.get("latest_url") or record.get("canonical_url", "")
    url_error = report_url_error(url)
    if url_error:
        label = record.get("note_id") or record.get("stable_id") or record.get("title") or "unknown record"
        raise ValueError(f"{url_error}: {label}")
    return {
        "stable_id": record.get("stable_id", ""),
        "note_id": record.get("note_id", ""),
        "title": record.get("title", ""),
        "author": record.get("author", ""),
        "company": record.get("company", ""),
        "role": record.get("role", ""),
        "location": record.get("location", ""),
        "published_text": record.get("published_text", ""),
        "published_at": record.get("published_at"),
        "queries": record.get("queries", []),
        "reason": record.get("reason", ""),
        "status": record.get("status", ""),
        "url": url,
    }


def extract_note_id(candidate: dict) -> str:
    direct = str(candidate.get("note_id") or "").lower()
    if re.fullmatch(r"[0-9a-f]{24}", direct):
        return direct
    match = NOTE_ID_RE.search(str(candidate.get("url") or ""))
    return match.group(1).lower() if match else ""


def normalize_text(value: object) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\s+", "", text)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def fingerprint(candidate: dict) -> str:
    title = normalize_text(candidate.get("title"))
    if not title:
        return ""
    parts = [
        normalize_text(candidate.get("author")),
        title,
        normalize_text(candidate.get("company")),
        normalize_text(candidate.get("role")),
    ]
    material = "|".join(parts)
    if not material.strip("|"):
        return ""
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def infer_published_at(candidate: dict, run_at: datetime, tz: ZoneInfo) -> datetime | None:
    explicit = candidate.get("published_at")
    if explicit:
        return parse_iso(str(explicit)).astimezone(tz)

    raw = str(candidate.get("published_text") or "").strip()
    if not raw:
        return None
    now = run_at.astimezone(tz)

    match = re.search(r"(\d+)\s*分钟前", raw)
    if match:
        return now - timedelta(minutes=int(match.group(1)))
    match = re.search(r"(\d+)\s*小时前", raw)
    if match:
        return now - timedelta(hours=int(match.group(1)))
    match = re.search(r"(\d+)\s*天前", raw)
    if match:
        return now - timedelta(days=int(match.group(1)))
    if "刚刚" in raw:
        return now

    time_match = re.search(r"(\d{1,2}):(\d{2})", raw)
    hour = int(time_match.group(1)) if time_match else 23
    minute = int(time_match.group(2)) if time_match else 59
    if "前天" in raw:
        day = (now - timedelta(days=2)).date()
        return datetime(day.year, day.month, day.day, hour, minute, 59, tzinfo=tz)
    if "昨天" in raw:
        day = (now - timedelta(days=1)).date()
        return datetime(day.year, day.month, day.day, hour, minute, 59, tzinfo=tz)

    match = re.search(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)", raw)
    if match:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), 23, 59, 59, tzinfo=tz)
    match = re.search(r"(?<!\d)(\d{1,2})-(\d{1,2})(?!\d)", raw)
    if match:
        candidate_date = datetime(now.year, int(match.group(1)), int(match.group(2)), 23, 59, 59, tzinfo=tz)
        if candidate_date > now + timedelta(days=1):
            candidate_date = candidate_date.replace(year=now.year - 1)
        return candidate_date
    return None


def stable_id(note_id: str, identity: str, state: dict) -> str:
    seed = (note_id or hashlib.sha256(identity.encode("utf-8")).hexdigest()).upper()
    used = {
        record.get("stable_id"): key
        for key, record in state.get("seen_posts", {}).items()
        if record.get("stable_id")
    }
    for width in (6, 8, 10, 12):
        proposed = f"XHS-{seed[-width:]}"
        if proposed not in used or used[proposed] == identity:
            return proposed
    return f"XHS-{seed}"


def new_state(name: str, max_age_days: int, tz_name: str) -> dict:
    return {
        "version": VERSION,
        "monitor": {
            "name": name,
            "max_age_days": max_age_days,
            "timezone": tz_name,
        },
        "profile": {},
        "query_pool": {"core": [], "rotating_groups": []},
        "query_state": {},
        "seen_posts": {},
        "user_reviews": {},
        "last_run": {},
    }


def command_init(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    if state_path.exists() and not args.force:
        raise SystemExit(f"State already exists: {state_path}. Use --force to replace it.")
    save_json(state_path, new_state(args.name, args.max_age_days, args.timezone))
    print(json.dumps({"state": str(state_path), "created": True}, ensure_ascii=False))
    return 0


def command_configure(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_json(state_path)
    config = load_json(Path(args.config))
    profile = config.get("profile")
    query_pool = config.get("query_pool")
    if not isinstance(profile, dict):
        raise ValueError("config.profile must be an object")
    if not isinstance(query_pool, dict):
        raise ValueError("config.query_pool must be an object")
    core = query_pool.get("core")
    groups = query_pool.get("rotating_groups")
    if not isinstance(core, list) or len(core) != 3 or not all(isinstance(q, str) and q.strip() for q in core):
        raise ValueError("query_pool.core must contain exactly three non-empty queries")
    if not isinstance(groups, list) or not all(
        isinstance(group, list) and len(group) == 2 and all(isinstance(q, str) and q.strip() for q in group)
        for group in groups
    ):
        raise ValueError("query_pool.rotating_groups must contain two queries per group")
    state["profile"] = profile
    state["query_pool"] = {"core": core, "rotating_groups": groups}
    save_json(state_path, state)
    print(json.dumps({"configured": True, "core_queries": len(core), "rotating_groups": len(groups)}, ensure_ascii=False))
    return 0


def command_ingest(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_json(state_path)
    payload = load_json(Path(args.input))
    tz_name = state.get("monitor", {}).get("timezone", DEFAULT_TZ)
    tz = ZoneInfo(tz_name)
    run_at = parse_iso(str(payload.get("run_at"))).astimezone(tz)
    max_age = int(state.get("monitor", {}).get("max_age_days", 14))
    cutoff = run_at - timedelta(days=max_age)

    seen = state.setdefault("seen_posts", {})
    url_index = {record.get("canonical_url"): key for key, record in seen.items() if record.get("canonical_url")}
    fp_index = {record.get("fingerprint"): key for key, record in seen.items() if record.get("fingerprint")}
    counts = {
        "input": 0,
        "new": 0,
        "duplicates": 0,
        "expired_by_age": 0,
        "verified_active": 0,
        "manual_review": 0,
        "excluded_irrelevant": 0,
    }
    reportable = []
    query_metrics = {
        str(query): {"result_rows": 0, "new_reportable": 0}
        for query in payload.get("queries", [])
    }

    for candidate in payload.get("candidates", []):
        counts["input"] += 1
        query_values = candidate.get("queries")
        if not query_values and candidate.get("query"):
            query_values = [candidate.get("query")]
        query_values = sorted(set(str(query) for query in (query_values or []) if query))
        for query in query_values:
            query_metrics.setdefault(query, {"result_rows": 0, "new_reportable": 0})["result_rows"] += 1
        note_id = extract_note_id(candidate)
        original_url = str(candidate.get("url") or "")
        canonical_url = canonicalize_url(original_url)
        fp = fingerprint(candidate)
        identity = note_id or canonical_url or fp
        if not identity:
            identity = "anonymous:" + hashlib.sha256(
                json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:24]

        existing_key = None
        if note_id and note_id in seen:
            existing_key = note_id
        elif canonical_url and canonical_url in url_index:
            existing_key = url_index[canonical_url]
        elif fp and fp in fp_index:
            existing_key = fp_index[fp]

        if existing_key:
            counts["duplicates"] += 1
            if original_url and not report_url_error(original_url):
                seen[existing_key]["latest_url"] = original_url
            if identity not in seen:
                seen[identity] = {
                    "stable_id": stable_id(note_id, identity, state),
                    "note_id": note_id,
                    "title": candidate.get("title") or "",
                    "author": candidate.get("author") or "",
                    "canonical_url": canonical_url,
                    "latest_url": original_url,
                    "first_seen_at": run_at.isoformat(),
                    "status": "duplicate",
                    "duplicate_of": existing_key,
                    "fingerprint": fp,
                }
            continue

        published_at = infer_published_at(candidate, run_at, tz)
        requested = str(candidate.get("classification") or "manual_review")
        if requested not in {"verified_active", "manual_review", "excluded_irrelevant"}:
            requested = "manual_review"
        if published_at is not None and published_at < cutoff:
            status = "expired_by_age"
        elif published_at is None and requested != "excluded_irrelevant":
            status = "manual_review"
        else:
            status = requested

        if status in REPORTABLE:
            url_error = report_url_error(original_url)
            if url_error:
                label = note_id or str(candidate.get("title") or "unknown candidate")
                raise ValueError(f"{url_error}: {label}")

        record = {
            "stable_id": stable_id(note_id, identity, state),
            "note_id": note_id,
            "title": candidate.get("title") or "",
            "author": candidate.get("author") or "",
            "company": candidate.get("company") or "",
            "role": candidate.get("role") or "",
            "location": candidate.get("location") or "",
            "published_text": candidate.get("published_text") or "",
            "published_at": published_at.isoformat() if published_at else None,
            "canonical_url": canonical_url,
            "latest_url": original_url,
            "queries": sorted(set(query_values or [])),
            "reason": candidate.get("reason") or "",
            "first_seen_at": run_at.isoformat(),
            "status": status,
            "fingerprint": fp,
        }
        if status in REPORTABLE:
            record["notified_at"] = run_at.isoformat()
            reportable.append(report_view(record))
            for query in query_values:
                query_metrics[query]["new_reportable"] += 1
        seen[identity] = record
        if canonical_url:
            url_index[canonical_url] = identity
        if fp:
            fp_index[fp] = identity
        counts["new"] += 1
        counts[status] = counts.get(status, 0) + 1

    query_state = state.setdefault("query_state", {})
    for query, metrics in query_metrics.items():
        previous = query_state.get(query, {})
        query_state[query] = {
            "last_success_at": run_at.isoformat(),
            "successful_runs": int(previous.get("successful_runs", 0)) + 1,
            "last_result_rows": metrics["result_rows"],
            "last_new_reportable": metrics["new_reportable"],
            "total_result_rows": int(previous.get("total_result_rows", 0)) + metrics["result_rows"],
            "total_new_reportable": int(previous.get("total_new_reportable", 0)) + metrics["new_reportable"],
        }
    state["last_run"] = {"completed_at": run_at.isoformat(), **counts}
    save_json(state_path, state)
    print(json.dumps({"counts": counts, "new_reportable": reportable}, ensure_ascii=False, indent=2))
    return 0


def resolve_stable_ids(state: dict) -> dict[str, str]:
    return {
        record.get("stable_id", "").upper(): key
        for key, record in state.get("seen_posts", {}).items()
        if record.get("stable_id")
    }


def command_mark(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_json(state_path)
    index = resolve_stable_ids(state)
    now = datetime.now(timezone.utc).isoformat()
    updates: dict[str, str] = {}
    for value, ids in (
        ("valuable", args.valuable),
        ("not_valuable", args.not_valuable),
        ("later", args.later),
    ):
        for stable in ids or []:
            updates[stable.upper()] = value

    missing = [stable for stable in updates if stable not in index]
    if missing:
        raise SystemExit("Unknown stable IDs: " + ", ".join(missing))
    reviews = state.setdefault("user_reviews", {})
    for stable, value in updates.items():
        reviews[stable] = {"value": value, "updated_at": now}
    save_json(state_path, state)
    print(json.dumps({"updated": updates}, ensure_ascii=False, indent=2))
    return 0


def command_list(args: argparse.Namespace) -> int:
    state = load_json(Path(args.state))
    reviews = state.get("user_reviews", {})
    rows = []
    for record in state.get("seen_posts", {}).values():
        if record.get("status") not in REPORTABLE:
            continue
        stable = record.get("stable_id", "")
        review = reviews.get(stable)
        value = review.get("value") if review else "unreviewed"
        if args.review != "all" and value != args.review:
            continue
        rows.append({
            "stable_id": stable,
            "review": value,
            "title": record.get("title", ""),
            "author": record.get("author", ""),
            "status": record.get("status", ""),
            "url": report_view(record)["url"],
        })
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def command_stats(args: argparse.Namespace) -> int:
    state = load_json(Path(args.state))
    statuses: dict[str, int] = {}
    for record in state.get("seen_posts", {}).values():
        status = record.get("status", "unknown")
        statuses[status] = statuses.get(status, 0) + 1
    review_counts = {value: 0 for value in sorted(REVIEW_VALUES | {"unreviewed"})}
    reviews = state.get("user_reviews", {})
    for record in state.get("seen_posts", {}).values():
        if record.get("status") not in REPORTABLE:
            continue
        stable = record.get("stable_id", "")
        value = reviews.get(stable, {}).get("value", "unreviewed")
        review_counts[value] = review_counts.get(value, 0) + 1
    print(json.dumps({"statuses": statuses, "reviews": review_counts, "last_run": state.get("last_run", {})}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Create a new state file")
    init.add_argument("--state", required=True)
    init.add_argument("--name", required=True)
    init.add_argument("--max-age-days", type=int, default=14)
    init.add_argument("--timezone", default=DEFAULT_TZ)
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=command_init)

    configure = commands.add_parser("configure", help="Store the user profile and generated query pool")
    configure.add_argument("--state", required=True)
    configure.add_argument("--config", required=True)
    configure.set_defaults(func=command_configure)

    ingest = commands.add_parser("ingest", help="Deduplicate and ingest one scan batch")
    ingest.add_argument("--state", required=True)
    ingest.add_argument("--input", required=True)
    ingest.set_defaults(func=command_ingest)

    mark = commands.add_parser("mark", help="Record personal value labels")
    mark.add_argument("--state", required=True)
    mark.add_argument("--valuable", nargs="*", default=[])
    mark.add_argument("--not-valuable", nargs="*", default=[])
    mark.add_argument("--later", nargs="*", default=[])
    mark.set_defaults(func=command_mark)

    listing = commands.add_parser("list", help="List records by personal label")
    listing.add_argument("--state", required=True)
    listing.add_argument("--review", choices=sorted(REVIEW_VALUES | {"unreviewed", "all"}), default="all")
    listing.set_defaults(func=command_list)

    stats = commands.add_parser("stats", help="Summarize ledger status")
    stats.add_argument("--state", required=True)
    stats.set_defaults(func=command_stats)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
