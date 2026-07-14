"""
auto_lock_clean_submitted_audio.py

Audio counterpart of scripts/auto_lock_clean_submitted.py — operates on the
separate audio review database (store/audio_review.db), not the chat DB.

Auto-locks audio sessions that are clean by BOTH automated signals AND have been
submitted for L2 review, so they bypass manual L2 review. A session is locked
when ALL of these hold:

    review_status     = 'SUBMITTED_FOR_REVIEW'   (an L1 submitted it for L2)
    overall_verdict   = 'CLEAN'                  (LLM/reviewer verdict is clean)
    astrotalk_verdict = 'CLEAN'                  (AstroTalk also did not flag it)

Note: the audio DB records the AstroTalk signal as a verdict string
(astrotalk_verdict = 'CLEAN' | 'FLAGGED') rather than the chat DB's
astrotalk_flagged integer — legacy 'SEVERE' counts as FLAGGED, so only an
explicit 'CLEAN' qualifies here.

The script still SCANS the whole clean + AstroTalk-clean population and prints a
breakdown by review_status (PENDING, SUBMITTED_FOR_REVIEW, etc.), but it only
LOCKS the SUBMITTED_FOR_REVIEW ones. PENDING and other not-yet-submitted clean
sessions are shown for visibility but are NOT locked. Sessions AstroTalk flagged
(astrotalk_verdict != 'CLEAN') are never auto-locked even if the verdict is CLEAN.

Locking sets (same effect as the UI Lock button):
    review_status = 'LOCKED'
    locked_by     = 'AUTO_LOCK'   (marks it as a script lock, not a manual L2 lock)
    locked_at     = datetime('now')

DRY-RUN BY DEFAULT — running with no flags only previews what would change.
Pass --commit to actually write the locks.

Usage:
  python scripts/auto_lock_clean_submitted_audio.py            # preview (dry-run)
  python scripts/auto_lock_clean_submitted_audio.py --commit   # apply the locks
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import get_audio_connection, AUDIO_DB_PATH  # noqa: E402

LOCKED_BY = "AUTO_LOCK"

# Only sessions in this status are actually locked.
LOCK_STATUS = "SUBMITTED_FOR_REVIEW"

# All sessions CLEAN by BOTH automated signals: verdict CLEAN AND AstroTalk
# clean (astrotalk_verdict = 'CLEAN'), excluding ones already LOCKED. This is
# the full scanned population shown in the breakdown; only the
# SUBMITTED_FOR_REVIEW subset is locked. UPPER() guards against any legacy
# lower-case verdict values.
SELECT_TARGETS = """
    SELECT s_id, review_status, assigned_to, submitted_by
    FROM audio_sessions
    WHERE UPPER(overall_verdict) = 'CLEAN'
      AND UPPER(astrotalk_verdict) = 'CLEAN'
      AND review_status != 'LOCKED'
    ORDER BY s_id ASC
"""


def auto_lock(commit: bool = False) -> int:
    """
    Scans all CLEAN + AstroTalk-clean audio sessions (not already LOCKED) and
    prints a breakdown by review_status, but only LOCKS the SUBMITTED_FOR_REVIEW
    subset. Returns the number of sessions actually eligible to be locked (i.e.
    the SUBMITTED_FOR_REVIEW count). When commit is False, nothing is written.
    """
    conn = get_audio_connection()
    try:
        rows = conn.execute(SELECT_TARGETS).fetchall()
        total = len(rows)

        if total == 0:
            print("No CLEAN + AstroTalk-clean audio sessions found. Nothing to lock.")
            return 0

        lock_rows = [r for r in rows if r["review_status"] == LOCK_STATUS]
        lock_total = len(lock_rows)

        print(f"Found {total:,} CLEAN + AstroTalk-clean audio session(s):\n")
        print(f"  {'SESSION ID':<28} {'STATUS':<22} {'ASSIGNED TO':<14} {'SUBMITTED BY':<14}")
        print(f"  {'-'*28} {'-'*22} {'-'*14} {'-'*14}")
        for r in rows:
            print(
                f"  {str(r['s_id']):<28} "
                f"{str(r['review_status'] or '—'):<22} "
                f"{str(r['assigned_to'] or '—'):<14} "
                f"{str(r['submitted_by'] or '—'):<14}"
            )
        print()

        # Breakdown of the scanned population by review_status.
        status_counts = Counter(str(r["review_status"] or "—") for r in rows)
        print("  Breakdown by current review_status:")
        for status, count in sorted(status_counts.items()):
            tag = "  -> will be locked" if status == LOCK_STATUS else ""
            print(f"    {status:<22} {count:>6,}{tag}")
        print()
        print(f"  Only '{LOCK_STATUS}' sessions are locked; "
              f"{total - lock_total:,} other clean session(s) are left untouched.")
        print()

        if lock_total == 0:
            print(f"No '{LOCK_STATUS}' sessions to lock. Nothing to do.")
            return 0

        if not commit:
            print(f"DRY RUN — {lock_total:,} '{LOCK_STATUS}' session(s) would be "
                  f"locked. No changes written. Re-run with --commit to apply.")
            return lock_total

        conn.executemany(
            """UPDATE audio_sessions
                  SET review_status = 'LOCKED',
                      locked_by     = ?,
                      locked_at     = datetime('now')
                WHERE s_id = ?
                  AND UPPER(overall_verdict) = 'CLEAN'
                  AND UPPER(astrotalk_verdict) = 'CLEAN'
                  AND review_status = ?""",
            [(LOCKED_BY, r["s_id"], LOCK_STATUS) for r in lock_rows],
        )
        conn.commit()
        print(f"Done. Locked {lock_total:,} '{LOCK_STATUS}' audio session(s) as '{LOCKED_BY}'.")
        return lock_total
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-lock audio sessions clean by both signals (LLM verdict CLEAN + AstroTalk clean)."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write the locks (default is dry-run preview only).",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  Auto-lock CLEAN + AstroTalk-clean audio sessions")
    print("=" * 60)
    print(f"  Database : {AUDIO_DB_PATH}")
    print(f"  Mode     : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    auto_lock(commit=args.commit)


if __name__ == "__main__":
    main()
