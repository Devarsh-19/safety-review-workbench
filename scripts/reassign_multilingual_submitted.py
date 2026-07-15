"""
reassign_multilingual_submitted.py

Fix SUBMITTED_FOR_REVIEW audio sessions that were wrongly parked on the
"Multilingual" bucket but were actually reviewed and submitted by a real L1
reviewer. These show up in the L2 (Amogh) dashboard with a real reviewer name
yet assigned_to = 'Multilingual'.

For a submitted session the submitter is preserved in reviewer_id / submitted_by
(see store/audio_db.submit_audio_session, which sets both to the L1's name), so
the true owner can be recovered. This script sets:

    assigned_to = <reviewer_id or submitted_by>

for every session where:

    review_status = 'SUBMITTED_FOR_REVIEW'   (configurable via --status)
    assigned_to   = 'Multilingual'           (configurable via --reviewer)
    reviewer_id / submitted_by is a real name (NOT 'LLM' / 'AUTO_LOCK' / the
    bucket itself / empty)

Sessions whose only submitter is 'LLM' / 'AUTO_LOCK' (auto-submitted, no human
reviewer) are reported and left untouched — use
scripts/revert_multilingual_to_pending.py for those.

DRY RUN BY DEFAULT — prints what would change and writes nothing. Pass --apply to
commit.

Usage:
  python scripts/reassign_multilingual_submitted.py                 # dry run
  python scripts/reassign_multilingual_submitted.py --apply         # perform the reassign
  python scripts/reassign_multilingual_submitted.py --reviewer Multilingual --apply
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
DEFAULT_STATUS = "SUBMITTED_FOR_REVIEW"

# Sentinels that are NOT real human reviewers — cannot be an assignment target.
NON_REVIEWERS = {"LLM", "AUTO_LOCK"}


def real_reviewer(row, bucket: str) -> str | None:
    """The L1 name that owns a submitted session, or None if there is no human
    reviewer to fall back on. Prefers reviewer_id, then submitted_by."""
    for candidate in (row["reviewer_id"], row["submitted_by"]):
        name = (candidate or "").strip()
        if name and name not in NON_REVIEWERS and name != bucket:
            return name
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--reviewer", default=DEFAULT_REVIEWER,
                    help=f"Wrongly-parked bucket to fix (default: {DEFAULT_REVIEWER}).")
    ap.add_argument("--status", default=DEFAULT_STATUS,
                    help=f"Only touch sessions in this review_status (default: {DEFAULT_STATUS}).")
    ap.add_argument("--apply", action="store_true",
                    help="Actually reassign the sessions and commit. Without it, dry run only.")
    args = ap.parse_args()

    conn = get_audio_connection()
    try:
        rows = conn.execute(
            """SELECT s_id, lang, review_status, assigned_to, submitted_by, reviewer_id
               FROM audio_sessions
               WHERE assigned_to = ? AND review_status = ?
               ORDER BY s_id ASC""",
            (args.reviewer, args.status),
        ).fetchall()

        to_fix, no_human = [], []
        for r in rows:
            target = real_reviewer(r, args.reviewer)
            (to_fix if target else no_human).append((r, target))

        print("=" * 74)
        print("  Reassign wrongly-parked SUBMITTED sessions back to their L1 reviewer")
        print("=" * 74)
        print(f"  Database        : {AUDIO_DB_PATH}")
        print(f"  Bucket          : {args.reviewer}")
        print(f"  Status          : {args.status}")
        print(f"  Candidates      : {len(rows)}")
        print(f"  Reassignable    : {len(to_fix)}")
        print(f"  No human reviewer (skip): {len(no_human)}")
        print()

        if to_fix:
            print("  WOULD REASSIGN:")
            print(f"    {'S_ID':<12} {'LANG':<16} {'FROM':<14} -> {'TO (reviewer)':<14}")
            print(f"    {'-'*12} {'-'*16} {'-'*14}    {'-'*14}")
            for r, target in to_fix:
                print(f"    {str(r['s_id']):<12} {str(r['lang'] or '-'):<16} "
                      f"{str(r['assigned_to'] or '-'):<14} -> {target:<14}")
            print()
            tally = Counter(target for _, target in to_fix)
            print("  New assignee tally:")
            for name, count in sorted(tally.items()):
                print(f"    {name:<16} {count:>6}")
            print()

        if no_human:
            print(f"  SKIPPED (no human reviewer - only {'/'.join(sorted(NON_REVIEWERS))}; "
                  f"use revert_multilingual_to_pending.py):")
            for r, _ in no_human:
                print(f"    {str(r['s_id']):<12} lang={str(r['lang'] or '-'):<14} "
                      f"reviewer_id={str(r['reviewer_id'] or '-')}  submitted_by={str(r['submitted_by'] or '-')}")
            print()

        if not to_fix:
            print("  Nothing to reassign.")
            return

        if not args.apply:
            print("  DRY RUN - nothing changed. Re-run with --apply to reassign the above.")
            return

        conn.executemany(
            """UPDATE audio_sessions
               SET assigned_to = ?
               WHERE s_id = ? AND assigned_to = ? AND review_status = ?""",
            [(target, r["s_id"], args.reviewer, args.status) for r, target in to_fix],
        )
        conn.commit()
        print(f"  REASSIGNED {len(to_fix)} session(s) from '{args.reviewer}' back to their reviewer.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
