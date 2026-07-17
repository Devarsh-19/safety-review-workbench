"""
dismiss_nsfw_family_flags_audio.py

Cleanup for the audio review database (store/audio_review.db): dismiss the flags
of any session whose flags are ENTIRELY within the NSFW family and few in number.

A session qualifies when BOTH hold:

  1. "Only these flags" — every flag row on the session (any status, any source,
     including amendment/dismissed rows) has an intent in:
         NSFW, NSFW_GROOMING, NSFW_APPEARANCE
     If the session carries even one flag of any other intent, it is left alone.

  2. "1 to 5, not exceeding 5" — the session has between 1 and 5 LIVE flags
     (active and not dismissed — the ones a reviewer actually sees). Sessions
     with 0 live flags, or more than 5, are skipped.

Dismissal follows the audio script convention (see remove_audio_output.py): a
SOFT dismiss — each live flag row is set to status = 'DISMISSED' (restorable via
undismiss), a 'DISMISSED' row is written to audio_review_log, and the session
verdict is recomputed (a session left with no active flag becomes CLEAN). Flags
are NEVER hard-deleted.

SCOPE: by default only PENDING and LOCKED sessions are cleaned. Sessions that
are SUBMITTED_FOR_REVIEW or REVIEWED are left alone. Override with --status.

DRY-RUN BY DEFAULT — running with no flags only previews what would change.
Pass --commit to actually dismiss.

Usage:
  python scripts/dismiss_nsfw_family_flags_audio.py            # preview PENDING+LOCKED (dry-run)
  python scripts/dismiss_nsfw_family_flags_audio.py --commit   # apply to PENDING+LOCKED
  python scripts/dismiss_nsfw_family_flags_audio.py --status PENDING          # single status
  python scripts/dismiss_nsfw_family_flags_audio.py --status ALL --commit     # every status
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
DEFAULT_STATUSES = ("PENDING", "LOCKED")

REVIEWER_ID = "AUTO_DISMISS"
NOTE = "Auto-dismiss: session's only flags were NSFW-family (1-5 live)"


def _norm(intent) -> str:
    return (intent or "").strip().upper()


def find_qualifying_sessions(conn, statuses):
    """Return (eligible, skipped_other_intent, skipped_bad_count).

    eligible: list of dicts {s_id, review_status, live_flag_ids} ready to clean.
    statuses: iterable of review_status values to consider (None -> all statuses).
    """
    status_set = set(statuses) if statuses else None

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
        if status_set is not None and review_status not in status_set:
            continue

        # Rule 1: every row must be NSFW-family (strict — includes amendment
        # parents and dismissed rows, so a session that ever held another
        # intent is never touched).
        if not all(_norm(r["intent"]) in ALLOWED_INTENTS for r in rows):
            skipped_other_intent += 1
            continue

        # Rule 2: 1..5 LIVE flags (active and not dismissed).
        live = [r for r in _active_audio_flag_rows(rows) if (r["status"] or "") != "DISMISSED"]
        if not (1 <= len(live) <= MAX_LIVE_FLAGS):
            skipped_bad_count += 1
            continue

        eligible.append({
            "s_id": s_id,
            "review_status": review_status or "—",
            "live_flag_ids": [r["flag_id"] for r in live],
        })

    eligible.sort(key=lambda e: e["s_id"])
    return eligible, skipped_other_intent, skipped_bad_count


def run(commit: bool, statuses) -> int:
    conn = get_audio_connection()
    try:
        eligible, skipped_other, skipped_count = find_qualifying_sessions(conn, statuses)

        if not eligible:
            print("No qualifying sessions found. Nothing to dismiss.")
            print(f"  (skipped: {skipped_other} with other intents, "
                  f"{skipped_count} outside the 1-{MAX_LIVE_FLAGS} live-flag range)")
            return 0

        total_flags = sum(len(e["live_flag_ids"]) for e in eligible)
        print(f"Found {len(eligible):,} qualifying session(s) "
              f"({total_flags:,} live flag(s) to dismiss):\n")
        print(f"  {'SESSION':<10} {'STATUS':<24} {'LIVE FLAGS':>10}")
        print(f"  {'-'*10} {'-'*24} {'-'*10}")
        for e in eligible:
            print(f"  {e['s_id']:<10} {e['review_status']:<24} {len(e['live_flag_ids']):>10}")
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
            print(f"DRY RUN — would dismiss {total_flags:,} live flag(s) across "
                  f"{len(eligible):,} session(s) and recompute their verdicts (now CLEAN). "
                  f"No changes written. Re-run with --commit to apply.")
            return len(eligible)

        for e in eligible:
            for fid in e["live_flag_ids"]:
                conn.execute(
                    "UPDATE audio_flags SET status = 'DISMISSED' WHERE flag_id = ?",
                    (fid,),
                )
                conn.execute(
                    """INSERT INTO audio_review_log (s_id, flag_id, action, reviewer_id, note)
                       VALUES (?, ?, 'DISMISSED', ?, ?)""",
                    (e["s_id"], fid, REVIEWER_ID, NOTE),
                )
            recompute_audio_session_verdict(e["s_id"], conn)
        conn.commit()
        print(f"Done. Dismissed {total_flags:,} live flag(s) across {len(eligible):,} "
              f"session(s); verdicts recomputed (now CLEAN).")
        return len(eligible)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dismiss NSFW-family-only flags (1-5 live) from audio sessions."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually dismiss (default is dry-run preview only).")
    parser.add_argument("--status", default=",".join(DEFAULT_STATUSES),
                        help="Comma-separated review_status values to clean. "
                             "Default: PENDING,LOCKED. Pass 'ALL' to consider every status.")
    args = parser.parse_args()

    # 'ALL' disables the status filter; otherwise a comma-separated set.
    if str(args.status).strip().upper() == "ALL":
        statuses = None
    else:
        statuses = tuple(s.strip().upper() for s in args.status.split(",") if s.strip())

    print("=" * 64)
    print("  Dismiss NSFW-family-only flags (1-5 live) from audio sessions")
    print("=" * 64)
    print(f"  Database : {AUDIO_DB_PATH}")
    print(f"  Intents  : {', '.join(sorted(ALLOWED_INTENTS))}")
    print(f"  Scope    : {', '.join(statuses) if statuses else 'ALL statuses'}")
    print(f"  Mode     : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    run(commit=args.commit, statuses=statuses)


if __name__ == "__main__":
    main()
