"""
pending_flag_count_distribution.py

Count how many sessions have exactly N active flags, for a given review_status
(default PENDING), in either the audio or the chat review database.

"Active flags" uses the same definition as the review queue: DISMISSED rows are
excluded, and an original that has an amendment is not double-counted (the
amendment row counts, the original does not).

By default it prints the 1 / 2 / 3-flag buckets in the requested format, e.g.:

    Total PENDING audio sessions: 37
    1: 10 sessions, 2: 13 sessions, 3: 14 sessions

Use --counts to change which buckets are shown, or --all for the full
distribution (every flag count that occurs, including 0).

Read-only — never modifies the database.

Usage:
  python scripts/pending_flag_count_distribution.py                    # audio, PENDING, 1/2/3
  python scripts/pending_flag_count_distribution.py --db chat
  python scripts/pending_flag_count_distribution.py --counts 1,2,3,4,5
  python scripts/pending_flag_count_distribution.py --status SUBMITTED_FOR_REVIEW
  python scripts/pending_flag_count_distribution.py --all
"""

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import AUDIO_DB_PATH  # noqa: E402
from store.db import DB_PATH              # noqa: E402

# Per-database table/column names (both share the same flag model: a `flags`
# table keyed to the session, with status + parent_flag_id for amendments).
DB_CONFIG = {
    "audio": {"path": AUDIO_DB_PATH, "sessions": "audio_sessions", "flags": "audio_flags", "id": "s_id"},
    "chat":  {"path": DB_PATH,       "sessions": "sessions",       "flags": "flags",       "id": "session_id"},
}


def active_flag_distribution(cfg: dict, status: str) -> tuple[int, Counter]:
    """Return (total sessions in `status`, Counter of active-flag-count -> n sessions)."""
    conn = sqlite3.connect(cfg["path"])
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""
            SELECT s.{cfg['id']} AS sid,
                   (SELECT COUNT(*) FROM {cfg['flags']} f
                     WHERE f.{cfg['id']} = s.{cfg['id']}
                       AND (f.status IS NULL OR f.status != 'DISMISSED')
                       AND f.flag_id NOT IN (
                           SELECT parent_flag_id FROM {cfg['flags']}
                           WHERE parent_flag_id IS NOT NULL)
                   ) AS active_flags
            FROM {cfg['sessions']} s
            WHERE s.review_status = ?
            """,
            (status,),
        ).fetchall()
    finally:
        conn.close()

    dist = Counter(r["active_flags"] for r in rows)
    return len(rows), dist


def main() -> None:
    p = argparse.ArgumentParser(
        description="Count sessions by their number of active flags for a given review_status."
    )
    p.add_argument("--db", choices=["audio", "chat"], default="audio",
                   help="Which review database to query (default: audio).")
    p.add_argument("--status", default="PENDING",
                   help="review_status to filter on (default: PENDING).")
    p.add_argument("--counts", default="1,2,3",
                   help="Comma-separated flag counts to report (default: 1,2,3). Ignored with --all.")
    p.add_argument("--all", action="store_true",
                   help="Show the full distribution (every flag count that occurs, including 0).")
    args = p.parse_args()

    cfg = DB_CONFIG[args.db]
    status = args.status.strip().upper()
    total, dist = active_flag_distribution(cfg, status)

    print(f"Total {status} {args.db} sessions: {total}")

    if args.all:
        buckets = sorted(dist)
    else:
        try:
            buckets = [int(x) for x in args.counts.split(",") if x.strip() != ""]
        except ValueError:
            print("--counts must be a comma-separated list of integers, e.g. 1,2,3")
            sys.exit(1)

    if not buckets:
        print("(no flag counts to report)")
        return

    print(", ".join(f"{n}: {dist.get(n, 0)} sessions" for n in buckets))


if __name__ == "__main__":
    main()
