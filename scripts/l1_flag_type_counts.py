"""
l1_flag_type_counts.py

Read-only report: per L1 reviewer, how many flags of each flag type (NSFW,
CSAM_RISK, ABUSIVE_LANGUAGE, …) live on the sessions they submitted. Prints a
nested dictionary { l1_user: { FLAG_TYPE: count } } plus a { l1_user: number of
distinct flag types } summary.

Attribution: a flag is credited to the L1 who submitted its session
(sessions.submitted_by / audio_sessions.submitted_by). The flags tables don't
store a per-flag author, so "an L1's flags" means the flags on that L1's
submitted sessions. The non-human submitters 'LLM' / 'AUTO_LOCK' are excluded.

Counting mirrors the app's violation stats:
  * flag type must be non-empty (chat: category_code, audio: intent),
  * DISMISSED flags are excluded (audio soft-dismiss),
  * amendment-superseded flags are excluded (a flag_id that appears as another
    flag's parent_flag_id has been replaced by its amendment).
Types are normalised to UPPER_SNAKE ('off-platform solicitation' ->
'OFF_PLATFORM_SOLICITATION') so chat/audio spellings line up.

Works on both stores:
  * chat review DB  — flags / sessions              (store/astrotalk.db)     [default]
  * audio review DB — audio_flags / audio_sessions  (store/audio_review.db)  [--audio]

Usage:
  python scripts/l1_flag_type_counts.py                    # chat DB (default)
  python scripts/l1_flag_type_counts.py --audio            # audio DB
  python scripts/l1_flag_type_counts.py --db path/to.db    # explicit DB
  python scripts/l1_flag_type_counts.py --since 2026-07-01 # only sessions submitted on/after
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

# Non-human submitters excluded from the L1 tally.
NON_L1_SUBMITTERS = ("LLM", "AUTO_LOCK")

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


def fetch_counts(conn: sqlite3.Connection, store: str,
                 since: str | None) -> dict[str, dict[str, int]]:
    """Nested { l1_user: { FLAG_TYPE: count } }."""
    s = SCHEMA[store]
    ph = ", ".join("?" for _ in NON_L1_SUBMITTERS)
    params: list = list(NON_L1_SUBMITTERS)
    since_clause = ""
    if since:
        since_clause = "AND date(se.submitted_at) >= ?"
        params.append(since)
    norm_type = f"UPPER(REPLACE(REPLACE(TRIM(f.{s['type']}), '-', '_'), ' ', '_'))"
    sql = f"""
        SELECT se.submitted_by            AS l1,
               {norm_type}                AS ftype,
               COUNT(*)                   AS n
        FROM {s['flags']} f
        JOIN {s['sessions']} se ON se.{s['sid']} = f.{s['sid']}
        WHERE se.submitted_by IS NOT NULL
          AND se.submitted_by NOT IN ({ph})
          AND f.{s['type']} IS NOT NULL
          AND TRIM(f.{s['type']}) != ''
          AND (f.status IS NULL OR f.status != 'DISMISSED')
          AND f.flag_id NOT IN (
              SELECT parent_flag_id FROM {s['flags']} WHERE parent_flag_id IS NOT NULL
          )
          {since_clause}
        GROUP BY l1, ftype
        ORDER BY l1, ftype
    """
    out: dict[str, dict[str, int]] = {}
    for l1, ftype, n in conn.execute(sql, params).fetchall():
        out.setdefault(l1, {})[ftype] = n
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Flag-type counts per L1 reviewer (read-only).")
    ap.add_argument("--audio", action="store_true",
                    help="Use the audio review DB (store/audio_review.db or "
                         "$AUDIO_DB_PATH) instead of the chat DB.")
    ap.add_argument("--db",
                    help="Explicit SQLite DB path (overrides --audio and the default chat DB).")
    ap.add_argument("--since", metavar="YYYY-MM-DD",
                    help="Only sessions submitted on/after this date.")
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
        counts = fetch_counts(conn, store, args.since)
    finally:
        conn.close()

    distinct_types = {user: len(types) for user, types in counts.items()}

    if args.json:
        print(json.dumps(counts, indent=2, sort_keys=True))
        return

    print("# Flags per flag type, per L1 user")
    print(f"#   DB    : {db_path}")
    print(f"#   Store : {store}"
          + (f"   since {args.since}" if args.since else ""))
    print(f"#   Excludes submitted_by in {NON_L1_SUBMITTERS}; "
          "DISMISSED + amendment-superseded flags")
    print()
    print(pformat(counts, sort_dicts=True, width=100))
    print()
    print("# Distinct flag types per L1 user")
    print(pformat(distinct_types, sort_dicts=True, width=100))


if __name__ == "__main__":
    main()
