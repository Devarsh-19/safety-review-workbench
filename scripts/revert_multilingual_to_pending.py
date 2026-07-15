"""
revert_multilingual_to_pending.py

Revert audio sessions assigned to the "Multilingual" reviewer back to PENDING.

Clean regional-language sessions can be auto-submitted / auto-locked by the LLM
ingest (scripts/ingest_audio_results.py) or the auto_process / auto_lock scripts,
which pushes them past the Multilingual reviewer. This script pulls every
Multilingual session that is NOT already PENDING back to a clean PENDING state so
the reviewer actually sees them.

For each affected session it resets:
    review_status = 'PENDING'
    submitted_by / submitted_at / reviewed_at = NULL
    locked_by / locked_at                     = NULL
    reviewer_id / reviewer_note               = NULL

`assigned_to` is left as 'Multilingual' so the sessions stay in that reviewer's
queue (an L1 login sees only PENDING sessions assigned to them). Sessions already
PENDING are left untouched.

DRY RUN BY DEFAULT — prints what would change and writes nothing. Pass --apply to
commit.

Usage:
  python scripts/revert_multilingual_to_pending.py                 # dry run
  python scripts/revert_multilingual_to_pending.py --apply         # perform the revert
  python scripts/revert_multilingual_to_pending.py --reviewer Multilingual --apply
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import get_audio_connection, AUDIO_DB_PATH  # noqa: E402

DEFAULT_REVIEWER = "Multilingual"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--reviewer", default=DEFAULT_REVIEWER,
                    help=f"Reviewer bucket to revert (default: {DEFAULT_REVIEWER}).")
    ap.add_argument("--apply", action="store_true",
                    help="Actually revert the sessions and commit. Without it, dry run only.")
    args = ap.parse_args()

    conn = get_audio_connection()
    try:
        rows = conn.execute(
            """SELECT s_id, lang, review_status, submitted_by, locked_by, reviewer_id
               FROM audio_sessions
               WHERE assigned_to = ? AND review_status != 'PENDING'
               ORDER BY s_id ASC""",
            (args.reviewer,),
        ).fetchall()

        print("=" * 72)
        print("  Revert Multilingual audio sessions -> PENDING")
        print("=" * 72)
        print(f"  Database        : {AUDIO_DB_PATH}")
        print(f"  Reviewer bucket : {args.reviewer}")
        print(f"  To revert (not already PENDING): {len(rows)}")
        print()

        if not rows:
            print("  Nothing to revert - no non-PENDING sessions assigned to "
                  f"'{args.reviewer}'.")
            return

        print("  WOULD REVERT:")
        print(f"    {'S_ID':<12} {'LANG':<16} {'STATUS':<22} {'SUBMITTED_BY':<14} "
              f"{'LOCKED_BY':<12} {'REVIEWER':<10}")
        print(f"    {'-'*12} {'-'*16} {'-'*22} {'-'*14} {'-'*12} {'-'*10}")
        for r in rows:
            print(f"    {str(r['s_id']):<12} {str(r['lang'] or '—'):<16} "
                  f"{str(r['review_status'] or '—'):<22} {str(r['submitted_by'] or '—'):<14} "
                  f"{str(r['locked_by'] or '—'):<12} {str(r['reviewer_id'] or '—'):<10}")
        print()

        status_counts = Counter(str(r["review_status"] or "—") for r in rows)
        print("  Breakdown by current review_status:")
        for status, count in sorted(status_counts.items()):
            print(f"    {status:<22} {count:>6}")
        print()

        if not args.apply:
            print("  DRY RUN - nothing changed. Re-run with --apply to revert the above.")
            return

        conn.executemany(
            """UPDATE audio_sessions
               SET review_status = 'PENDING',
                   submitted_by  = NULL,
                   submitted_at  = NULL,
                   reviewed_at   = NULL,
                   locked_by     = NULL,
                   locked_at     = NULL,
                   reviewer_id   = NULL,
                   reviewer_note = NULL
               WHERE s_id = ? AND assigned_to = ?""",
            [(r["s_id"], args.reviewer) for r in rows],
        )
        conn.commit()
        print(f"  REVERTED {len(rows)} '{args.reviewer}' session(s) to PENDING.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
