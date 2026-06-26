"""
auto_lock_reengagement_submitted.py

Auto-locks sessions submitted for L2 review whose ONLY flag is
RE_ENGAGEMENT_SOLICITATION, marking them CLEAN so they bypass manual L2 review.
A lone post-session re-engagement flag is treated as non-actionable.

A session qualifies when:
    review_status = 'SUBMITTED_FOR_REVIEW'
    its only active flag category is RE_ENGAGEMENT_SOLICITATION
        (at least one such flag, and NO other category; matched case-insensitively
         so both 'RE_ENGAGEMENT_SOLICITATION' and 're_engagement_solicitation' count)

Locking sets (verdict overridden to CLEAN, flag row left untouched):
    overall_verdict = 'CLEAN'
    review_status   = 'LOCKED'
    locked_by       = 'AUTO_LOCK_REENGAGEMENT'
    locked_at       = datetime('now')

DRY-RUN BY DEFAULT — running with no flags only previews what would change.
Pass --commit to actually write the locks.

Usage:
  python scripts/auto_lock_reengagement_submitted.py            # preview (dry-run)
  python scripts/auto_lock_reengagement_submitted.py --commit   # apply the locks
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import get_connection, DB_PATH  # noqa: E402

LOCKED_BY = "AUTO_LOCK_REENGAGEMENT"
TARGET_FLAG = "RE_ENGAGEMENT_SOLICITATION"   # matched case-insensitively


def _active_categories_by_session(conn) -> dict[str, set[str]]:
    """Map each SUBMITTED_FOR_REVIEW session to its set of active flag
    categories (upper-cased). Active = amendment row if present, else original.
    """
    rows = conn.execute(
        """SELECT flag_id, session_id, parent_flag_id, category_code
           FROM flags
           WHERE session_id IN (
               SELECT session_id FROM sessions
               WHERE review_status = 'SUBMITTED_FOR_REVIEW'
           )"""
    ).fetchall()

    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}
    cats: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        is_active = (r["parent_flag_id"] is not None) or (r["flag_id"] not in amended_parents)
        if is_active and r["category_code"]:
            cats[r["session_id"]].add(r["category_code"].strip().upper())
    return cats


def _qualifying_sessions(conn) -> list:
    """SUBMITTED_FOR_REVIEW sessions whose only active flag is the target."""
    cats = _active_categories_by_session(conn)
    sessions = conn.execute(
        """SELECT session_id, overall_verdict, assigned_to, submitted_by
           FROM sessions
           WHERE review_status = 'SUBMITTED_FOR_REVIEW'
           ORDER BY session_id ASC"""
    ).fetchall()
    return [s for s in sessions if cats.get(s["session_id"]) == {TARGET_FLAG}]


def auto_lock(commit: bool = False) -> int:
    """Locks every SUBMITTED_FOR_REVIEW session whose only flag is
    RE_ENGAGEMENT_SOLICITATION, marking it CLEAN. Returns the match count.
    When commit is False, nothing is written.
    """
    conn = get_connection()
    try:
        rows = _qualifying_sessions(conn)
        total = len(rows)

        if total == 0:
            print("No SUBMITTED_FOR_REVIEW sessions with only "
                  f"{TARGET_FLAG} found. Nothing to lock.")
            return 0

        print(f"Found {total:,} session(s) flagged ONLY for {TARGET_FLAG}, "
              "submitted for L2 review:\n")
        print(f"  {'SESSION ID':<28} {'VERDICT':<10} {'ASSIGNED TO':<14} {'SUBMITTED BY':<14}")
        print(f"  {'-'*28} {'-'*10} {'-'*14} {'-'*14}")
        for r in rows:
            print(
                f"  {str(r['session_id']):<28} "
                f"{str(r['overall_verdict'] or '—'):<10} "
                f"{str(r['assigned_to'] or '—'):<14} "
                f"{str(r['submitted_by'] or '—'):<14}"
            )
        print()

        if not commit:
            print("DRY RUN — no changes written. Re-run with --commit to lock these.")
            return total

        conn.executemany(
            """UPDATE sessions
                  SET overall_verdict = 'CLEAN',
                      review_status   = 'LOCKED',
                      locked_by       = ?,
                      locked_at       = datetime('now')
                WHERE session_id = ?
                  AND review_status = 'SUBMITTED_FOR_REVIEW'""",
            [(LOCKED_BY, r["session_id"]) for r in rows],
        )
        conn.commit()
        print(f"Done. Marked CLEAN and locked {total:,} session(s) as '{LOCKED_BY}'.")
        return total
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-lock re-engagement-only sessions submitted for L2 "
                    "review (mark CLEAN, no L2 review needed)."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write the locks (default is dry-run preview only).",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  Auto-lock re-engagement-only sessions submitted for L2 review")
    print("=" * 60)
    print(f"  Database : {DB_PATH}")
    print(f"  Only flag: {TARGET_FLAG}")
    print(f"  Mode     : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    auto_lock(commit=args.commit)


if __name__ == "__main__":
    main()
