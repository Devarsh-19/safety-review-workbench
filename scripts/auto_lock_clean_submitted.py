"""
auto_lock_clean_submitted.py

Auto-locks sessions that were submitted for L2 review but whose verdict is CLEAN,
so they bypass manual L2 review. A session is locked when:

    review_status  = 'SUBMITTED_FOR_REVIEW'   (submitted for L2 review)
    overall_verdict = 'CLEAN'                  (nothing flagged)

Locking sets (same effect as the UI Lock button):
    review_status = 'LOCKED'
    locked_by     = 'AUTO_LOCK'   (marks it as a script lock, not a manual L2 lock)
    locked_at     = datetime('now')

DRY-RUN BY DEFAULT — running with no flags only previews what would change.
Pass --commit to actually write the locks.

Usage:
  python scripts/auto_lock_clean_submitted.py            # preview (dry-run)
  python scripts/auto_lock_clean_submitted.py --commit   # apply the locks
"""

import argparse
import os
import sqlite3

from dotenv import load_dotenv
load_dotenv()

DB_PATH = os.getenv("DB_PATH", "store/astrotalk.db")

LOCKED_BY = "AUTO_LOCK"

# Sessions submitted for L2 review whose verdict is CLEAN.
SELECT_TARGETS = """
    SELECT session_id, assigned_to, submitted_by
    FROM sessions
    WHERE review_status = 'SUBMITTED_FOR_REVIEW'
      AND overall_verdict = 'CLEAN'
    ORDER BY session_id ASC
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def auto_lock(commit: bool = False) -> int:
    """
    Locks every SUBMITTED_FOR_REVIEW + CLEAN session. Returns the number of
    matching sessions. When commit is False, nothing is written.
    """
    conn = get_connection()
    try:
        rows = conn.execute(SELECT_TARGETS).fetchall()
        total = len(rows)

        if total == 0:
            print("No SUBMITTED_FOR_REVIEW + CLEAN sessions found. Nothing to lock.")
            return 0

        print(f"Found {total:,} CLEAN session(s) submitted for L2 review:\n")
        print(f"  {'SESSION ID':<28} {'ASSIGNED TO':<14} {'SUBMITTED BY':<14}")
        print(f"  {'-'*28} {'-'*14} {'-'*14}")
        for r in rows:
            print(
                f"  {str(r['session_id']):<28} "
                f"{str(r['assigned_to'] or '—'):<14} "
                f"{str(r['submitted_by'] or '—'):<14}"
            )
        print()

        if not commit:
            print("DRY RUN — no changes written. Re-run with --commit to lock these.")
            return total

        conn.executemany(
            """UPDATE sessions
                  SET review_status = 'LOCKED',
                      locked_by     = ?,
                      locked_at     = datetime('now')
                WHERE session_id = ?
                  AND review_status = 'SUBMITTED_FOR_REVIEW'
                  AND overall_verdict = 'CLEAN'""",
            [(LOCKED_BY, r["session_id"]) for r in rows],
        )
        conn.commit()
        print(f"Done. Locked {total:,} session(s) as '{LOCKED_BY}'.")
        return total
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-lock CLEAN sessions submitted for L2 review (no L2 review needed)."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write the locks (default is dry-run preview only).",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  Auto-lock CLEAN sessions submitted for L2 review")
    print("=" * 60)
    print(f"  Database : {DB_PATH}")
    print(f"  Mode     : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    auto_lock(commit=args.commit)


if __name__ == "__main__":
    main()
