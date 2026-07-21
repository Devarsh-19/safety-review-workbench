"""
export_session_turns_with_flags_json.py

JSON counterpart of export_session_turns_with_flags.py. Chat review database
(store/astrotalk.db). Exports EVERY session with ALL its turns, each turn marked
with its ACTIVE flags. Read-only (mode=ro — cannot modify the database).

It reuses the exact same read-only connection and export query as the CSV script,
so the two exports always agree on rows and on the "active flag" rule (status not
DISMISSED; amendment row supersedes an amended original).

Shape — one object per session, turns nested inside (rows come out grouped and
ordered by (session_id, turn_id), so this is built streaming, one session at a
time; memory stays flat regardless of DB size):

  {
    "session_id": "SESS_1002",
    "overall_verdict": "FLAGGED",
    "review_status": "PENDING",
    "astrotalk_flagged": 1,
    "turns": [
      {
        "turn_id": 3,
        "speaker": "USER",
        "is_automated": 0,
        "timestamp": "...",
        "message_text": "...",
        "has_active_flag": true,
        "active_flag_count": 1,
        "active_flag_categories": ["FEAR_MANIPULATION"]
      },
      ...
    ]
  }

Output (--out):
  * default: a single pretty JSON array of session objects (valid .json).
  * --ndjson: newline-delimited JSON, one session object per line — the robust
    choice for very large exports and stream consumers.
Both are written streaming; nothing but one session is held in memory at a time.

Sessions with no turns do not appear (the export is driven by the turns table),
matching the CSV script.

Usage:
  python scripts/export_session_turns_with_flags_json.py
  python scripts/export_session_turns_with_flags_json.py --ndjson --out exports/session_turns.ndjson
  python scripts/export_session_turns_with_flags_json.py --report-unlinked
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Reuse the CSV script's read-only connection + query so both stay in lockstep.
from scripts.export_session_turns_with_flags import (  # noqa: E402
    get_readonly_connection,
    EXPORT_SQL,
    COUNT_UNLINKED_SQL,
    DB_PATH,
)

# Column order returned by EXPORT_SQL (== HEADER in the CSV script).
(C_SESSION_ID, C_OVERALL, C_STATUS, C_REVIEWED_AT, C_LOCKED_BY, C_ASTRO, C_LANGUAGE,
 C_TURN_ID, C_SPEAKER, C_AUTOMATED, C_TS, C_MSG,
 C_HAS_FLAG, C_FLAG_COUNT, C_FLAG_CATS) = range(15)


def _turn_obj(r):
    cats = r[C_FLAG_CATS]
    return {
        "turn_id": r[C_TURN_ID],
        "speaker": r[C_SPEAKER],
        "is_automated": r[C_AUTOMATED],
        "timestamp": r[C_TS],
        "message_text": r[C_MSG],
        "has_active_flag": bool(r[C_HAS_FLAG]),
        "active_flag_count": r[C_FLAG_COUNT],
        # group_concat -> list; category codes contain no commas, so ", " is safe.
        "active_flag_categories": cats.split(", ") if cats else [],
    }


def iter_sessions(cur):
    """Group the ordered cursor into one session object at a time. Relies on
    EXPORT_SQL ordering rows by (session_id, turn_id), so all of a session's
    turns arrive consecutively."""
    current_sid = None
    session = None
    for r in cur:
        sid = r[C_SESSION_ID]
        if sid != current_sid:
            if session is not None:
                yield session
            current_sid = sid
            session = {
                "session_id": sid,
                "overall_verdict": r[C_OVERALL],
                "review_status": r[C_STATUS],
                "reviewed_at": r[C_REVIEWED_AT],
                "locked_by": r[C_LOCKED_BY],
                "astrotalk_flagged": r[C_ASTRO],
                "language": r[C_LANGUAGE],
                "turns": [],
            }
        session["turns"].append(_turn_obj(r))
    if session is not None:
        yield session


def write_json_array(sessions, out_path):
    """Stream a single valid JSON array — one session object per element, written
    incrementally so the whole document is never assembled in memory."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_sessions = n_turns = 0
    with open(out_path, "w", encoding="utf-8", buffering=1 << 20) as fh:
        fh.write("[\n")
        first = True
        for s in sessions:
            fh.write(("" if first else ",\n") + json.dumps(s, ensure_ascii=False))
            first = False
            n_sessions += 1
            n_turns += len(s["turns"])
        fh.write("\n]\n" if not first else "]\n")
    return n_sessions, n_turns


def write_ndjson(sessions, out_path):
    """One compact JSON object per line — ideal for large / streamed exports."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_sessions = n_turns = 0
    with open(out_path, "w", encoding="utf-8", buffering=1 << 20) as fh:
        for s in sessions:
            fh.write(json.dumps(s, ensure_ascii=False) + "\n")
            n_sessions += 1
            n_turns += len(s["turns"])
    return n_sessions, n_turns


def main():
    parser = argparse.ArgumentParser(
        description="Chat DB: export every session's turns (with active-flag markers) as JSON."
    )
    parser.add_argument("--out", help="Output path (default: exports/session_turns_with_flags"
                                       "[.ndjson|.json]).")
    parser.add_argument("--ndjson", action="store_true",
                        help="Newline-delimited JSON (one session per line) instead of a JSON array.")
    parser.add_argument("--report-unlinked", action="store_true",
                        help="Also print how many active flags have no resolvable turn.")
    args = parser.parse_args()

    out_path = Path(args.out) if args.out else Path(
        "exports/session_turns_with_flags." + ("ndjson" if args.ndjson else "json")
    )

    conn = get_readonly_connection()
    try:
        unlinked = conn.execute(COUNT_UNLINKED_SQL).fetchone()[0] if args.report_unlinked else None
        cur = conn.execute(EXPORT_SQL)
        writer = write_ndjson if args.ndjson else write_json_array
        n_sessions, n_turns = writer(iter_sessions(cur), out_path)
    finally:
        conn.close()

    print("=" * 64)
    print("  Session turns + active-flag markers (JSON)")
    print(f"  DB     : {DB_PATH}")
    print(f"  Format : {'NDJSON (one session/line)' if args.ndjson else 'JSON array'}")
    print(f"  Out    : {out_path}")
    print(f"  Wrote  : {n_sessions:,} sessions / {n_turns:,} turns")
    if unlinked is not None:
        print(f"  Active flags with no resolvable turn (not in export): {unlinked:,}")
    print("=" * 64)


if __name__ == "__main__":
    main()
