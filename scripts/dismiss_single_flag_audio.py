"""
dismiss_single_flag_audio.py

For the audio review database (store/audio_review.db): dismiss the flag of every
session that has exactly ONE active flag.

"Active flag" uses the review-queue definition — DISMISSED rows are excluded and
an original that has an amendment is not counted (the amendment row is). A
session qualifies only when exactly one such flag remains.

Dismissal follows the audio script convention (see scripts/remove_audio_output.py):
a SOFT dismiss — the flag row is set to status = 'DISMISSED' (restorable via
undismiss), a 'DISMISSED' row is written to audio_review_log, and the session
verdict is recomputed (a session left with no active flag becomes CLEAN). The
flag is never hard-deleted.

SCOPE: by default only PENDING and LOCKED sessions are cleaned. Sessions that
are SUBMITTED_FOR_REVIEW or REVIEWED are left alone. Override with --status.

DRY-RUN BY DEFAULT — running with no flags only previews what would change.
Pass --commit to actually dismiss.

Usage:
  python scripts/dismiss_single_flag_audio.py            # preview PENDING+LOCKED (dry-run)
  python scripts/dismiss_single_flag_audio.py --commit   # apply to PENDING+LOCKED
  python scripts/dismiss_single_flag_audio.py --status PENDING          # single status
  python scripts/dismiss_single_flag_audio.py --status ALL --commit     # every status
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import (  # noqa: E402
    get_audio_connection,
    recompute_audio_session_verdict,
    AUDIO_DB_PATH,
)

DEFAULT_STATUSES = ("PENDING", "LOCKED")
REVIEWER_ID = "AUTO_DISMISS"
NOTE = "Auto-dismiss: session had exactly one active flag"


def find_targets(conn, statuses):
    """Sessions with exactly one ACTIVE flag; returns rows of (s_id, flag_id, review_status).

    statuses: iterable of review_status values to consider (None -> all statuses).
    """
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        status_clause = f"s.review_status IN ({placeholders})"
        params = list(statuses)
    else:
        status_clause = "1=1"
        params = []

    return conn.execute(
        f"""
        SELECT s.s_id AS s_id, MIN(f.flag_id) AS flag_id, s.review_status AS review_status
        FROM audio_sessions s
        JOIN audio_flags f ON f.s_id = s.s_id
        WHERE {status_clause}
          AND (f.status IS NULL OR f.status != 'DISMISSED')
          AND f.flag_id NOT IN (
              SELECT parent_flag_id FROM audio_flags WHERE parent_flag_id IS NOT NULL)
        GROUP BY s.s_id
        HAVING COUNT(*) = 1
        ORDER BY s.s_id
        """,
        params,
    ).fetchall()


def run(commit: bool, statuses) -> int:
    conn = get_audio_connection()
    try:
        targets = find_targets(conn, statuses)
        if not targets:
            print("No sessions with exactly one active flag in scope. Nothing to dismiss.")
            return 0

        print(f"Found {len(targets):,} session(s) with exactly one active flag:\n")
        print(f"  {'SESSION':<10} {'STATUS':<24} {'FLAG ID':<10}")
        print(f"  {'-'*10} {'-'*24} {'-'*10}")
        for t in targets:
            print(f"  {t['s_id']:<10} {t['review_status'] or '—':<24} {t['flag_id']:<10}")
        print()

        status_counts = Counter(t["review_status"] or "—" for t in targets)
        print("  Breakdown by review_status:")
        for status, count in sorted(status_counts.items()):
            print(f"    {status:<24} {count:>6,}")
        print()

        if not commit:
            print(f"DRY RUN — would dismiss {len(targets):,} flag(s) and recompute those "
                  f"session verdicts (now CLEAN). No changes written. "
                  f"Re-run with --commit to apply.")
            return len(targets)

        for t in targets:
            conn.execute(
                "UPDATE audio_flags SET status = 'DISMISSED' WHERE flag_id = ?",
                (t["flag_id"],),
            )
            conn.execute(
                """INSERT INTO audio_review_log (s_id, flag_id, action, reviewer_id, note)
                   VALUES (?, ?, 'DISMISSED', ?, ?)""",
                (t["s_id"], t["flag_id"], REVIEWER_ID, NOTE),
            )
            recompute_audio_session_verdict(t["s_id"], conn)
        conn.commit()
        print(f"Done. Dismissed {len(targets):,} flag(s); "
              f"{len(targets):,} session(s) recomputed (now CLEAN).")
        return len(targets)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dismiss the single flag of audio sessions that have exactly one active flag."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually dismiss (default is dry-run preview only).")
    parser.add_argument("--status", default=",".join(DEFAULT_STATUSES),
                        help="Comma-separated review_status values to clean. "
                             "Default: PENDING,LOCKED. Pass 'ALL' to consider every status.")
    args = parser.parse_args()

    if str(args.status).strip().upper() == "ALL":
        statuses = None
    else:
        statuses = tuple(s.strip().upper() for s in args.status.split(",") if s.strip())

    print("=" * 64)
    print("  Dismiss single flag of one-flag audio sessions")
    print("=" * 64)
    print(f"  Database : {AUDIO_DB_PATH}")
    print(f"  Scope    : {', '.join(statuses) if statuses else 'ALL statuses'}")
    print(f"  Mode     : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    run(commit=args.commit, statuses=statuses)


if __name__ == "__main__":
    main()
