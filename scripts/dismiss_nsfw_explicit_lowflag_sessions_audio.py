"""
dismiss_nsfw_explicit_lowflag_sessions_audio.py

Session-level cleanup for the audio review database (store/audio_review.db):
dismiss every live flag of any session that carries an NSFW_EXPLICIT flag and is
lightly flagged overall. A session with an explicit hit but only a handful of
flags is treated as low-signal and cleared in one go.

SESSION-LEVEL FILTER — a session qualifies when BOTH hold:

  1. "Has NSFW_EXPLICIT" — the session has at least one LIVE flag whose intent is
     NSFW_EXPLICIT (case/format-insensitive: 'NSFW-EXPLICIT', 'nsfw explicit' …
     all match). Live = active (amendment row, or an original with no amendment)
     and not already DISMISSED.

  2. "flag count <= 5" — the session's live flag count is between 1 and 5. This
     matches the flag_count column in the queue exactly (active, non-dismissed,
     amended originals excluded). Sessions with more than 5 live flags are left
     alone.

Dismissal follows the audio script convention (see remove_audio_output.py): a
SOFT dismiss of EVERY live flag on the qualifying session — each live flag row is
set to status = 'DISMISSED' (restorable via undismiss), a 'DISMISSED' row is
written to audio_review_log, and the session verdict is recomputed (a session
left with no active flag becomes CLEAN). Flags are NEVER hard-deleted.

SCOPE: by default only PENDING and LOCKED sessions are cleaned. Sessions that are
SUBMITTED_FOR_REVIEW or REVIEWED are left alone. Override with --status.

DRY-RUN BY DEFAULT — running with no flags only previews what would change and
prints the number of sessions that would be affected. Pass --commit to apply.

Usage:
  python scripts/dismiss_nsfw_explicit_lowflag_sessions_audio.py            # preview PENDING+LOCKED (dry-run)
  python scripts/dismiss_nsfw_explicit_lowflag_sessions_audio.py --commit   # apply to PENDING+LOCKED
  python scripts/dismiss_nsfw_explicit_lowflag_sessions_audio.py --status PENDING          # single status
  python scripts/dismiss_nsfw_explicit_lowflag_sessions_audio.py --status ALL --commit     # every status
  python scripts/dismiss_nsfw_explicit_lowflag_sessions_audio.py --max-flags 3             # tighten the cap
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

TARGET_INTENT = "NSFW_EXPLICIT"
MAX_LIVE_FLAGS = 5
DEFAULT_STATUSES = ("PENDING", "LOCKED")

REVIEWER_ID = "AUTO_DISMISS"
NOTE = "Auto-dismiss: session had an NSFW_EXPLICIT flag and <=5 live flags"


def _norm_intent(intent) -> str:
    """Canonical intent form, matching the DB/API/frontend normalisation
    ('nsfw-explicit', 'NSFW Explicit' -> 'NSFW_EXPLICIT')."""
    return (intent or "").strip().upper().replace("-", "_").replace(" ", "_")


def find_qualifying_sessions(conn, statuses, max_flags):
    """Return (eligible, skipped_no_explicit, skipped_too_many).

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
        "SELECT flag_id, s_id, parent_flag_id, intent, status FROM audio_flags"
    ).fetchall():
        rows_by_sid[r["s_id"]].append(r)

    eligible = []
    skipped_no_explicit = 0
    skipped_too_many = 0

    for s_id, rows in rows_by_sid.items():
        review_status = status_by_sid.get(s_id)
        if status_set is not None and review_status not in status_set:
            continue

        # Live flags = active (amendment rows + un-amended originals) and not
        # already dismissed — exactly what the queue's flag_count column shows.
        live = [r for r in _active_audio_flag_rows(rows) if (r["status"] or "") != "DISMISSED"]

        # Rule 1: at least one LIVE NSFW_EXPLICIT flag on the session.
        if not any(_norm_intent(r["intent"]) == TARGET_INTENT for r in live):
            skipped_no_explicit += 1
            continue

        # Rule 2: 1..max_flags live flags (a session with a live explicit flag
        # already has >=1, so the lower bound is implicit).
        if len(live) > max_flags:
            skipped_too_many += 1
            continue

        eligible.append({
            "s_id": s_id,
            "review_status": review_status or "—",
            "live_flag_ids": [r["flag_id"] for r in live],
        })

    eligible.sort(key=lambda e: e["s_id"])
    return eligible, skipped_no_explicit, skipped_too_many


def run(commit: bool, statuses, max_flags) -> int:
    conn = get_audio_connection()
    try:
        eligible, skipped_no_explicit, skipped_too_many = find_qualifying_sessions(
            conn, statuses, max_flags
        )

        if not eligible:
            print("No qualifying sessions found. Nothing to dismiss.")
            print(f"  (skipped: {skipped_no_explicit} without a live NSFW_EXPLICIT flag, "
                  f"{skipped_too_many} with more than {max_flags} live flags)")
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
        print(f"  Skipped (no live NSFW_EXPLICIT flag) : {skipped_no_explicit:,}")
        print(f"  Skipped (> {max_flags} live flags)             : {skipped_too_many:,}")
        print()

        if not commit:
            print(f"DRY RUN — {len(eligible):,} session(s) would be affected "
                  f"({total_flags:,} live flag(s) dismissed, verdicts recomputed to "
                  f"CLEAN). No changes written. Re-run with --commit to apply.")
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
        description="Session-level dismiss: clear sessions that have an "
                    "NSFW_EXPLICIT flag and <=5 live flags (audio DB)."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually dismiss (default is dry-run preview only).")
    parser.add_argument("--status", default=",".join(DEFAULT_STATUSES),
                        help="Comma-separated review_status values to clean. "
                             "Default: PENDING,LOCKED. Pass 'ALL' to consider every status.")
    parser.add_argument("--max-flags", type=int, default=MAX_LIVE_FLAGS,
                        help=f"Only clear sessions with at most this many live flags "
                             f"(default: {MAX_LIVE_FLAGS}).")
    args = parser.parse_args()

    # 'ALL' disables the status filter; otherwise a comma-separated set.
    if str(args.status).strip().upper() == "ALL":
        statuses = None
    else:
        statuses = tuple(s.strip().upper() for s in args.status.split(",") if s.strip())

    print("=" * 68)
    print("  Dismiss sessions with an NSFW_EXPLICIT flag and <=%d live flags (audio)"
          % args.max_flags)
    print("=" * 68)
    print(f"  Database  : {AUDIO_DB_PATH}")
    print(f"  Intent    : {TARGET_INTENT}")
    print(f"  Max flags : <= {args.max_flags} live flags")
    print(f"  Scope     : {', '.join(statuses) if statuses else 'ALL statuses'}")
    print(f"  Mode      : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    run(commit=args.commit, statuses=statuses, max_flags=args.max_flags)


if __name__ == "__main__":
    main()
