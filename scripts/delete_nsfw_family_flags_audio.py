"""
delete_nsfw_family_flags_audio.py

Cleanup for the audio review database (store/audio_review.db): delete the flags
of any session whose flags are ENTIRELY within the NSFW family and few in number.

A session qualifies when BOTH hold:

  1. "Only these flags" — every flag row on the session (any status, any source,
     including amendment/dismissed rows) has an intent in:
         NSFW, NSFW_GROOMING, NSFW_APPEARANCE
     If the session carries even one flag of any other intent, it is left alone.

  2. "1 to 5, not exceeding 5" — the session has between 1 and 5 LIVE flags
     (active and not dismissed — the ones a reviewer actually sees). Sessions
     with 0 live flags, or more than 5, are skipped.

For qualifying sessions every NSFW-family flag row is hard-deleted (since by
rule 1 those are all the flags the session has), and overall_verdict is
recomputed — a session left with no active flags becomes CLEAN.

Active-flag semantics reuse store/audio_db.py (_active_audio_flag_rows /
recompute_audio_session_verdict) so counts match the queue, the analytics and
the export scripts.

SCOPE: only PENDING sessions are cleaned by default. Sessions that are
SUBMITTED_FOR_REVIEW, LOCKED or REVIEWED are never touched (their flags are
part of in-flight or finalised review work). Pass --status ALL to override.

DRY-RUN BY DEFAULT — running with no flags only previews what would change.
Pass --commit to actually delete.

Usage:
  python scripts/delete_nsfw_family_flags_audio.py            # preview PENDING (dry-run)
  python scripts/delete_nsfw_family_flags_audio.py --commit   # apply to PENDING
  python scripts/delete_nsfw_family_flags_audio.py --status ALL --commit   # every status
"""

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import (  # noqa: E402
    get_audio_connection,
    recompute_audio_session_verdict,
    _active_audio_flag_rows,
    AUDIO_DB_PATH,
)

# Intents that a session may contain and still qualify for cleanup.
ALLOWED_INTENTS = {"NSFW", "NSFW_GROOMING", "NSFW_APPEARANCE"}
MAX_LIVE_FLAGS = 5


def _norm(intent) -> str:
    return (intent or "").strip().upper()


def find_qualifying_sessions(conn, status_filter: str | None):
    """Return (eligible, skipped_other_intent, skipped_count) session lists.

    eligible: list of dicts {s_id, review_status, n_live, n_rows} ready to clean.
    The two skipped lists are only used for the summary breakdown.
    """
    status_by_sid = {
        r["s_id"]: r["review_status"]
        for r in conn.execute("SELECT s_id, review_status FROM audio_sessions").fetchall()
    }

    rows_by_sid: dict[int, list] = defaultdict(list)
    for r in conn.execute(
        "SELECT flag_id, s_id, parent_flag_id, intent, status, source FROM audio_flags"
    ).fetchall():
        rows_by_sid[r["s_id"]].append(r)

    eligible = []
    skipped_other_intent = 0
    skipped_bad_count = 0

    for s_id, rows in rows_by_sid.items():
        review_status = status_by_sid.get(s_id)
        if status_filter and review_status != status_filter:
            continue

        # Rule 1: every row must be NSFW-family (strict — includes amendment
        # parents and dismissed rows, so a session that ever held another
        # intent is never touched).
        if not all(_norm(r["intent"]) in ALLOWED_INTENTS for r in rows):
            skipped_other_intent += 1
            continue

        # Rule 2: 1..5 LIVE flags (active and not dismissed).
        live = [r for r in _active_audio_flag_rows(rows) if (r["status"] or "") != "DISMISSED"]
        n_live = len(live)
        if not (1 <= n_live <= MAX_LIVE_FLAGS):
            skipped_bad_count += 1
            continue

        eligible.append({
            "s_id": s_id,
            "review_status": review_status or "—",
            "n_live": n_live,
            "n_rows": len(rows),
        })

    eligible.sort(key=lambda e: e["s_id"])
    return eligible, skipped_other_intent, skipped_bad_count


def run(commit: bool, status_filter: str | None) -> int:
    conn = get_audio_connection()
    try:
        eligible, skipped_other, skipped_count = find_qualifying_sessions(conn, status_filter)

        if not eligible:
            print("No qualifying sessions found. Nothing to delete.")
            print(f"  (skipped: {skipped_other} with other intents, "
                  f"{skipped_count} outside the 1-{MAX_LIVE_FLAGS} live-flag range)")
            return 0

        total_rows = sum(e["n_rows"] for e in eligible)
        print(f"Found {len(eligible):,} qualifying session(s) "
              f"({total_rows:,} flag row(s) to delete):\n")
        print(f"  {'SESSION':<10} {'STATUS':<24} {'LIVE FLAGS':>10} {'ROWS DEL':>9}")
        print(f"  {'-'*10} {'-'*24} {'-'*10} {'-'*9}")
        for e in eligible:
            print(f"  {e['s_id']:<10} {e['review_status']:<24} {e['n_live']:>10} {e['n_rows']:>9}")
        print()

        status_counts = Counter(e["review_status"] for e in eligible)
        print("  Breakdown by review_status:")
        for status, count in sorted(status_counts.items()):
            print(f"    {status:<24} {count:>6,}")
        print()
        print(f"  Skipped (other intents present)      : {skipped_other:,}")
        print(f"  Skipped (0 or >{MAX_LIVE_FLAGS} live flags)          : {skipped_count:,}")
        print()

        if not commit:
            print(f"DRY RUN — would delete {total_rows:,} flag row(s) across "
                  f"{len(eligible):,} session(s) and recompute their verdicts. "
                  f"No changes written. Re-run with --commit to apply.")
            return len(eligible)

        for e in eligible:
            conn.execute("DELETE FROM audio_flags WHERE s_id = ?", (e["s_id"],))
            recompute_audio_session_verdict(e["s_id"], conn)
        conn.commit()
        print(f"Done. Deleted {total_rows:,} flag row(s) from {len(eligible):,} "
              f"session(s); verdicts recomputed (now CLEAN).")
        return len(eligible)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete NSFW-family-only flags (1-5 live) from audio sessions."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually delete (default is dry-run preview only).")
    parser.add_argument("--status", default="PENDING",
                        help="Limit to sessions with this review_status. "
                             "Default: PENDING (only PENDING sessions are cleaned; "
                             "SUBMITTED_FOR_REVIEW / LOCKED / REVIEWED are never touched). "
                             "Pass 'ALL' to consider every status.")
    args = parser.parse_args()

    # 'ALL' disables the status filter; any other value restricts to that status.
    status_filter = None if str(args.status).strip().upper() == "ALL" else args.status

    print("=" * 64)
    print("  Delete NSFW-family-only flags (1-5 live) from audio sessions")
    print("=" * 64)
    print(f"  Database : {AUDIO_DB_PATH}")
    print(f"  Intents  : {', '.join(sorted(ALLOWED_INTENTS))}")
    print(f"  Scope    : {status_filter or 'ALL statuses'}")
    print(f"  Mode     : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    run(commit=args.commit, status_filter=status_filter)


if __name__ == "__main__":
    main()
