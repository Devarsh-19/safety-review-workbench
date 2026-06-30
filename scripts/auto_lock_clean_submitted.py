"""
auto_lock_clean_submitted.py

Auto-locks sessions that are clean by BOTH automated signals, so they bypass
manual L2 review. A session is locked when:

    overall_verdict = 'CLEAN'                  (LLM/reviewer verdict is clean)
    astrotalk_flagged = 0                      (AstroTalk also did not flag it)
    review_status != 'LOCKED'                  (not already locked)

This no longer requires a human to have submitted the session for review — any
session clean by both signals is locked, whether it is still PENDING, was
SUBMITTED_FOR_REVIEW, or reviewed. Sessions AstroTalk flagged
(astrotalk_flagged = 1) are NOT auto-locked even if the verdict is CLEAN.
Already-LOCKED sessions are skipped so an existing manual lock's locked_by /
locked_at is never overwritten.

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
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import get_connection, DB_PATH  # noqa: E402

LOCKED_BY = "AUTO_LOCK"

# Sessions CLEAN by BOTH automated signals: verdict CLEAN AND AstroTalk not
# flagged (astrotalk_flagged = 0). No submission gate — any status except
# already-LOCKED is eligible.
SELECT_TARGETS = """
    SELECT session_id, review_status, assigned_to, submitted_by
    FROM sessions
    WHERE overall_verdict = 'CLEAN'
      AND astrotalk_flagged = 0
      AND review_status != 'LOCKED'
    ORDER BY session_id ASC
"""


def auto_lock(commit: bool = False) -> int:
    """
    Locks every session that is CLEAN by both signals (overall_verdict = CLEAN
    AND astrotalk_flagged = 0) and not already LOCKED. Returns the number of
    matching sessions. When commit is False, nothing is written.
    """
    conn = get_connection()
    try:
        rows = conn.execute(SELECT_TARGETS).fetchall()
        total = len(rows)

        if total == 0:
            print("No CLEAN + AstroTalk-clean sessions found. Nothing to lock.")
            return 0

        print(f"Found {total:,} CLEAN session(s) eligible for auto-lock:\n")
        print(f"  {'SESSION ID':<28} {'STATUS':<22} {'ASSIGNED TO':<14} {'SUBMITTED BY':<14}")
        print(f"  {'-'*28} {'-'*22} {'-'*14} {'-'*14}")
        for r in rows:
            print(
                f"  {str(r['session_id']):<28} "
                f"{str(r['review_status'] or '—'):<22} "
                f"{str(r['assigned_to'] or '—'):<14} "
                f"{str(r['submitted_by'] or '—'):<14}"
            )
        print()

        if not commit:
            print(f"DRY RUN — {total:,} session(s) would be locked. "
                  f"No changes written. Re-run with --commit to apply.")
            return total

        conn.executemany(
            """UPDATE sessions
                  SET review_status = 'LOCKED',
                      locked_by     = ?,
                      locked_at     = datetime('now')
                WHERE session_id = ?
                  AND overall_verdict = 'CLEAN'
                  AND astrotalk_flagged = 0
                  AND review_status != 'LOCKED'""",
            [(LOCKED_BY, r["session_id"]) for r in rows],
        )
        conn.commit()
        print(f"Done. Locked {total:,} session(s) as '{LOCKED_BY}'.")
        return total
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-lock sessions clean by both signals (LLM verdict CLEAN + AstroTalk clean)."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write the locks (default is dry-run preview only).",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  Auto-lock CLEAN + AstroTalk-clean sessions")
    print("=" * 60)
    print(f"  Database : {DB_PATH}")
    print(f"  Mode     : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    auto_lock(commit=args.commit)


if __name__ == "__main__":
    main()
