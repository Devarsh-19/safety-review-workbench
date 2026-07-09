"""Flag/session statistics for the audio moderation output JSON.

Reads an *_audio_moderation.json file produced by gemini_audio_batch.py
(a JSON array; one record per session with segments[*].flags nested inside)
and prints:
  - total distinct sessions, and how many have at least one flag
  - distinct session count per intent (a session counts once per intent)
  - flag-count distribution (how many sessions have exactly N flags)

Usage:
    python flag_session_stats.py                      # newest *_audio_moderation.json in data/
    python flag_session_stats.py path\\to\\file.json  # explicit file
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_JSON_DIR = BASE_DIR / "data"


def find_latest_moderation_json(json_dir: Path) -> Path | None:
    candidates = sorted(
        json_dir.glob("*_audio_moderation.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def session_flags(record: dict) -> list[dict]:
    """All flag dicts of one session record (flags are nested in segments)."""
    flags = []
    for segment in record.get("segments") or []:
        flags.extend(segment.get("flags") or [])
    # Older/error records may carry a flat "flags" list instead.
    flags.extend(record.get("flags") or [])
    return flags


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "json_file",
        nargs="?",
        default=None,
        help="Moderation JSON file; defaults to the newest *_audio_moderation.json in data/.",
    )
    args = parser.parse_args()

    json_path = Path(args.json_file) if args.json_file else find_latest_moderation_json(DEFAULT_JSON_DIR)
    if not json_path or not json_path.exists():
        print(f"[X] No moderation JSON found ({args.json_file or DEFAULT_JSON_DIR}/*_audio_moderation.json)")
        return 1

    records = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        print(f"[X] Expected a JSON array in {json_path.name}")
        return 1

    # Same session appended twice (rerun) -> the later record wins entirely.
    latest_record_by_session: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        session_id = str(record.get("s_id") or record.get("session_id") or "")
        if session_id:
            latest_record_by_session[session_id] = record

    sessions_per_intent: dict[str, set[str]] = {}
    flag_count_by_session: dict[str, int] = {}
    for session_id, record in latest_record_by_session.items():
        flags = session_flags(record)
        flag_count_by_session[session_id] = len(flags)
        for flag in flags:
            intent = str(flag.get("intent") or "UNKNOWN")
            sessions_per_intent.setdefault(intent, set()).add(session_id)

    total_sessions = len(flag_count_by_session)
    flagged_sessions = sum(1 for count in flag_count_by_session.values() if count)

    print(f"File: {json_path}")
    print(f"Distinct sessions: {total_sessions}")
    print(f"Sessions with >=1 flag: {flagged_sessions}")
    print(f"Sessions with no flags: {total_sessions - flagged_sessions}")

    print("\nDistinct sessions per intent:")
    if sessions_per_intent:
        width = max(len(intent) for intent in sessions_per_intent)
        for intent, sessions in sorted(sessions_per_intent.items(), key=lambda kv: -len(kv[1])):
            print(f"  {intent:<{width}}  {len(sessions)}")
    else:
        print("  (no flags in file)")

    print("\nFlag-count distribution (flags per session -> sessions):")
    distribution = Counter(flag_count_by_session.values())
    for flag_count in sorted(distribution):
        print(f"  {flag_count:>3} flag(s): {distribution[flag_count]} session(s)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
