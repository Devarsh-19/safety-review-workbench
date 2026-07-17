"""
l1_flag_type_counts.py

Read-only report: per L1 reviewer (by assignment), how big their PENDING queue is
and how many of those pending sessions carry each flag type. Prints a nested
dictionary keyed by reviewer, e.g.

  {'Divyansh': {'pending': 1300, 'CSAM_RISK': 500, 'NSFW_EXPLICIT': 200, ...},
   'Nikhil':   {'pending':  900, 'ABUSIVE_LANGUAGE': 120, ...},
   ...}

Attribution is by sessions.assigned_to / audio_sessions.assigned_to — the
reviewer the session is queued to. Only review_status = 'PENDING' sessions are
counted (a reviewer's outstanding queue). 'pending' is the total pending session
count; each FLAG_TYPE is the number of DISTINCT pending sessions carrying at
least one such flag, so two NSFW flags on one session count once and a session
with both CSAM + NSFW is counted under each. The per-flag numbers therefore do
not sum to 'pending'.

Flag counting mirrors the app's violation stats:
  * flag type must be non-empty (chat: category_code, audio: intent),
  * DISMISSED flags are excluded (audio soft-dismiss),
  * amendment-superseded flags are excluded (a flag_id that appears as another
    flag's parent_flag_id has been replaced by its amendment).
Types are normalised to UPPER_SNAKE ('off-platform solicitation' ->
'OFF_PLATFORM_SOLICITATION') so chat/audio spellings line up.

Reviewers are ordered by pending queue size (largest first); within each
reviewer the flag types are ordered by count (largest first).

Works on both stores:
  * chat review DB  — flags / sessions              (store/astrotalk.db)     [default]
  * audio review DB — audio_flags / audio_sessions  (store/audio_review.db)  [--audio]

Usage:
  python scripts/l1_flag_type_counts.py                    # chat DB (default)
  python scripts/l1_flag_type_counts.py --audio            # audio DB
  python scripts/l1_flag_type_counts.py --db path/to.db    # explicit DB
  python scripts/l1_flag_type_counts.py --json             # emit JSON instead of a pprint dict

Read-only: never writes to the database.
"""

import argparse
import json
import os
import sqlite3
import sys
from pprint import pformat
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "store" / "astrotalk.db"
# --audio target: the audio review DB, honouring AUDIO_DB_PATH (see store/audio_db.py).
AUDIO_DB = PROJECT_ROOT / os.getenv("AUDIO_DB_PATH", "store/audio_review.db")

# Per-store table / column layout. Both flag tables share flag_id, status and
# parent_flag_id; they differ in the session id column and the flag-type column.
SCHEMA = {
    "chat":  {"flags": "flags",       "sessions": "sessions",
              "sid": "session_id", "type": "category_code"},
    "audio": {"flags": "audio_flags", "sessions": "audio_sessions",
              "sid": "s_id",       "type": "intent"},
}


def detect_store(conn: sqlite3.Connection) -> str:
    """Return 'chat' or 'audio' for whichever session table this DB has."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "audio_sessions" in tables:
        return "audio"
    if "sessions" in tables:
        return "chat"
    raise SystemExit("No 'sessions' or 'audio_sessions' table in this database.")


def fetch_counts(conn: sqlite3.Connection, store: str) -> dict[str, dict[str, int]]:
    """{ reviewer: {'pending': N, FLAG_TYPE: distinct_pending_sessions, ...} }."""
    s = SCHEMA[store]

    # 1) Pending queue size per reviewer.
    pending: dict[str, int] = {}
    for user, n in conn.execute(f"""
        SELECT assigned_to AS user, COUNT(*) AS n
        FROM {s['sessions']}
        WHERE review_status = 'PENDING'
          AND assigned_to IS NOT NULL AND TRIM(assigned_to) != ''
        GROUP BY assigned_to
    """).fetchall():
        pending[user] = n

    # 2) Distinct pending sessions per (reviewer, flag type).
    norm_type = f"UPPER(REPLACE(REPLACE(TRIM(f.{s['type']}), '-', '_'), ' ', '_'))"
    flags: dict[str, dict[str, int]] = {}
    for user, ftype, n in conn.execute(f"""
        SELECT se.assigned_to              AS user,
               {norm_type}                 AS ftype,
               COUNT(DISTINCT f.{s['sid']}) AS n
        FROM {s['flags']} f
        JOIN {s['sessions']} se ON se.{s['sid']} = f.{s['sid']}
        WHERE se.review_status = 'PENDING'
          AND se.assigned_to IS NOT NULL AND TRIM(se.assigned_to) != ''
          AND f.{s['type']} IS NOT NULL AND TRIM(f.{s['type']}) != ''
          AND (f.status IS NULL OR f.status != 'DISMISSED')
          AND f.flag_id NOT IN (
              SELECT parent_flag_id FROM {s['flags']} WHERE parent_flag_id IS NOT NULL
          )
        GROUP BY user, ftype
    """).fetchall():
        flags.setdefault(user, {})[ftype] = n

    # 3) Assemble: reviewers by pending desc; flag types within by count desc.
    out: dict[str, dict[str, int]] = {}
    for user in sorted(pending, key=lambda u: (-pending[u], u)):
        ftypes = flags.get(user, {})
        ordered = dict(sorted(ftypes.items(), key=lambda kv: (-kv[1], kv[0])))
        out[user] = {"pending": pending[user], **ordered}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Per-reviewer pending queue size + flag-type breakdown (read-only).")
    ap.add_argument("--audio", action="store_true",
                    help="Use the audio review DB (store/audio_review.db or "
                         "$AUDIO_DB_PATH) instead of the chat DB.")
    ap.add_argument("--db",
                    help="Explicit SQLite DB path (overrides --audio and the default chat DB).")
    ap.add_argument("--json", action="store_true",
                    help="Emit the nested dict as JSON instead of a pprint dict literal.")
    args = ap.parse_args()

    if args.db:
        db_path = Path(args.db)
    elif args.audio:
        db_path = Path(AUDIO_DB)
    else:
        db_path = Path(DEFAULT_DB)
    if not db_path.exists():
        sys.exit(f"Database not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        store = detect_store(conn)
        counts = fetch_counts(conn, store)
    finally:
        conn.close()

    if args.json:
        # sort_keys=False preserves the reviewer/flag ordering built above.
        print(json.dumps(counts, indent=2, sort_keys=False))
        return

    print("# Pending sessions + flag-type breakdown, per reviewer (assigned_to)")
    print(f"#   DB    : {db_path}")
    print(f"#   Store : {store}")
    print("#   PENDING sessions only; FLAG_TYPE = distinct pending sessions with "
          "that flag")
    print("#   Excludes DISMISSED + amendment-superseded flags")
    print()
    # sort_dicts=False keeps the pending-desc / count-desc ordering.
    print(pformat(counts, sort_dicts=False, width=90))


if __name__ == "__main__":
    main()
