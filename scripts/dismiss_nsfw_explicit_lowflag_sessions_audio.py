"""
dismiss_nsfw_explicit_lowflag_sessions_audio.py

Cleanup for the audio review database (store/audio_review.db): on any lightly
flagged session that carries an NSFW_EXPLICIT flag, dismiss ONLY the
NSFW_EXPLICIT flags. Other intents on the session (ABUSIVE_LANGUAGE,
FEAR_MANIPULATION, …) are left ACTIVE and untouched.

A session is IN SCOPE when BOTH hold:

  1. "Has NSFW_EXPLICIT" — the session has at least one LIVE flag whose intent is
     NSFW_EXPLICIT (case/format-insensitive: 'NSFW-EXPLICIT', 'nsfw explicit' …
     all match). Live = active (amendment row, or an original with no amendment)
     and not already DISMISSED.

  2. "flag count <= 5" — the session's live flag count (ALL intents, not just
     explicit) is between 1 and 5. This matches the flag_count column in the
     queue exactly. Sessions with more than 5 live flags are left alone.

What gets dismissed: only the LIVE NSFW_EXPLICIT flag rows on in-scope sessions.
A session becomes CLEAN only when NSFW_EXPLICIT was its ONLY live intent; a
session that also has other live flags stays FLAGGED (those flags remain active).

Dismissal follows the audio script convention (soft dismiss): each targeted flag
row is set to status = 'DISMISSED' (restorable via undismiss), a 'DISMISSED' row
is written to audio_review_log, and the session verdict is recomputed. Flags are
NEVER hard-deleted.

SCOPE: by default only PENDING and LOCKED sessions are cleaned. Sessions that are
SUBMITTED_FOR_REVIEW or REVIEWED are left alone. Override with --status.

DRY-RUN BY DEFAULT — running with no flags only previews what would change and
prints how many sessions would become CLEAN (split by PENDING / LOCKED). Pass
--commit to apply.

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
NOTE = "Auto-dismiss: NSFW_EXPLICIT flag on a session with <=5 live flags"


def _norm_intent(intent) -> str:
    """Canonical intent form, matching the DB/API/frontend normalisation
    ('nsfw-explicit', 'NSFW Explicit' -> 'NSFW_EXPLICIT')."""
    return (intent or "").strip().upper().replace("-", "_").replace(" ", "_")


def find_qualifying_sessions(conn, statuses, max_flags):
    """Return (eligible, skipped_no_explicit, skipped_too_many).

    eligible: list of dicts per in-scope session:
        {s_id, review_status, live_count, explicit_flag_ids, becomes_clean}
      - explicit_flag_ids: the LIVE NSFW_EXPLICIT flags to dismiss.
      - becomes_clean: True when explicit was the session's only live intent, so
        dismissing it leaves no active flag (session -> CLEAN). False when other
        live flags remain (session stays FLAGGED).
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

        explicit = [r for r in live if _norm_intent(r["intent"]) == TARGET_INTENT]

        # Rule 1: at least one LIVE NSFW_EXPLICIT flag on the session.
        if not explicit:
            skipped_no_explicit += 1
            continue

        # Rule 2: 1..max_flags live flags total (a session with a live explicit
        # flag already has >=1, so the lower bound is implicit).
        if len(live) > max_flags:
            skipped_too_many += 1
            continue

        eligible.append({
            "s_id": s_id,
            "review_status": review_status or "—",
            "live_count": len(live),
            "explicit_flag_ids": [r["flag_id"] for r in explicit],
            # CLEAN only if explicit was the ONLY live intent — no other flag
            # remains active after we dismiss the explicit ones.
            "becomes_clean": len(explicit) == len(live),
        })

    eligible.sort(key=lambda e: e["s_id"])
    return eligible, skipped_no_explicit, skipped_too_many


def _print_clean_bifurcation(eligible, commit: bool) -> None:
    """Print how many sessions become CLEAN, split PENDING / LOCKED / other.
    A session becomes CLEAN only when NSFW_EXPLICIT was its only live intent."""
    clean = [e for e in eligible if e["becomes_clean"]]
    clean_counts = Counter(e["review_status"] for e in clean)
    pending_clean = clean_counts.get("PENDING", 0)
    locked_clean = clean_counts.get("LOCKED", 0)
    other_clean = len(clean) - pending_clean - locked_clean
    verb = "became" if commit else "will become"
    print(f"  Sessions that {verb} CLEAN (explicit was their only flag):")
    print(f"    PENDING                  {pending_clean:>6,}")
    print(f"    LOCKED                   {locked_clean:>6,}")
    if other_clean:
        print(f"    OTHER STATUSES           {other_clean:>6,}")
    print(f"    {'TOTAL':<24} {len(clean):>6,}")


def run(commit: bool, statuses, max_flags) -> int:
    conn = get_audio_connection()
    try:
        eligible, skipped_no_explicit, skipped_too_many = find_qualifying_sessions(
            conn, statuses, max_flags
        )

        if not eligible:
            print("No sessions in scope. Nothing to dismiss.")
            _print_clean_bifurcation([], commit)
            print(f"  (skipped: {skipped_no_explicit} without a live NSFW_EXPLICIT flag, "
                  f"{skipped_too_many} with more than {max_flags} live flags)")
            return 0

        total_explicit = sum(len(e["explicit_flag_ids"]) for e in eligible)
        stay_flagged = sum(1 for e in eligible if not e["becomes_clean"])
        print(f"Found {len(eligible):,} in-scope session(s) "
              f"({total_explicit:,} NSFW_EXPLICIT flag(s) to dismiss):\n")
        print(f"  {'SESSION':<10} {'STATUS':<20} {'LIVE':>5} {'EXPLICIT':>9} {'RESULT':>9}")
        print(f"  {'-'*10} {'-'*20} {'-'*5} {'-'*9} {'-'*9}")
        for e in eligible:
            result = "CLEAN" if e["becomes_clean"] else "FLAGGED"
            print(f"  {e['s_id']:<10} {e['review_status']:<20} {e['live_count']:>5} "
                  f"{len(e['explicit_flag_ids']):>9} {result:>9}")
        print()

        status_counts = Counter(e["review_status"] for e in eligible)
        print("  In-scope sessions by review_status:")
        for status, count in sorted(status_counts.items()):
            print(f"    {status:<24} {count:>6,}")
        print()

        _print_clean_bifurcation(eligible, commit)
        print(f"  Sessions still FLAGGED (kept other flags): {stay_flagged:,}")
        print()

        print(f"  Skipped (no live NSFW_EXPLICIT flag) : {skipped_no_explicit:,}")
        print(f"  Skipped (> {max_flags} live flags)             : {skipped_too_many:,}")
        print()

        if not commit:
            print(f"DRY RUN — would dismiss {total_explicit:,} NSFW_EXPLICIT flag(s) across "
                  f"{len(eligible):,} session(s). No changes written. Re-run with "
                  f"--commit to apply.")
            return len(eligible)

        for e in eligible:
            for fid in e["explicit_flag_ids"]:
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
        print(f"Done. Dismissed {total_explicit:,} NSFW_EXPLICIT flag(s) across "
              f"{len(eligible):,} session(s); verdicts recomputed.")
        return len(eligible)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dismiss NSFW_EXPLICIT flags on sessions with <=5 live flags "
                    "(audio DB); other intents are left active."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually dismiss (default is dry-run preview only).")
    parser.add_argument("--status", default=",".join(DEFAULT_STATUSES),
                        help="Comma-separated review_status values to clean. "
                             "Default: PENDING,LOCKED. Pass 'ALL' to consider every status.")
    parser.add_argument("--max-flags", type=int, default=MAX_LIVE_FLAGS,
                        help=f"Only touch sessions with at most this many live flags "
                             f"(default: {MAX_LIVE_FLAGS}).")
    args = parser.parse_args()

    # 'ALL' disables the status filter; otherwise a comma-separated set.
    if str(args.status).strip().upper() == "ALL":
        statuses = None
    else:
        statuses = tuple(s.strip().upper() for s in args.status.split(",") if s.strip())

    print("=" * 72)
    print("  Dismiss NSFW_EXPLICIT flags on sessions with <=%d live flags (audio)"
          % args.max_flags)
    print("=" * 72)
    print(f"  Database  : {AUDIO_DB_PATH}")
    print(f"  Intent    : {TARGET_INTENT} (only these flags are dismissed)")
    print(f"  Max flags : <= {args.max_flags} live flags (all intents)")
    print(f"  Scope     : {', '.join(statuses) if statuses else 'ALL statuses'}")
    print(f"  Mode      : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    run(commit=args.commit, statuses=statuses, max_flags=args.max_flags)


if __name__ == "__main__":
    main()
