"""
dismiss_long_pauses_abusive_language_audio.py

For the audio review database (store/audio_review.db): dismiss all cases (sessions) 
where BOTH long pauses AND ABUSIVE_LANGUAGE are detected.

A session is IN SCOPE when BOTH hold:
  1. "Has Long Pauses" — the session's `pauses` column is not null and holds a
     non-empty interval list (TRIM is not '' or '[]').
  2. "Has ABUSIVE_LANGUAGE" — the session has at least one LIVE flag whose intent is
     ABUSIVE_LANGUAGE. Live = active (amendment row, or an original with no amendment)
     and not already DISMISSED.

What gets dismissed: ALL live flags on the in-scope sessions, effectively making
the session overall_verdict = CLEAN.

The report prints the AFFECTED SESSIONS first (status, current verdict, pause
count, number of live flags, and the intents that would be dismissed), followed
by the individual flag detail. In dry-run nothing is written.

Dismissal follows the audio script convention (soft dismiss): each targeted flag
row is set to status = 'DISMISSED' (restorable via undismiss), a 'DISMISSED' row
is written to audio_review_log, and the session verdict is recomputed. Flags are
NEVER hard-deleted.

SCOPE: by default only PENDING and LOCKED sessions are cleaned. Override with --status.

DRY-RUN BY DEFAULT — pass --commit to apply.

Usage:
  python scripts/dismiss_long_pauses_abusive_language_audio.py            # preview PENDING+LOCKED (dry-run)
  python scripts/dismiss_long_pauses_abusive_language_audio.py --commit   # apply to PENDING+LOCKED
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import (  # noqa: E402
    get_audio_connection,
    recompute_audio_session_verdict,
    _normalize_audio_verdict,
    AUDIO_DB_PATH,
)

DEFAULT_STATUSES = ("PENDING", "LOCKED")
REVIEWER_ID = "AUTO_DISMISS"
NOTE = "Auto-dismiss: Session has long pauses and ABUSIVE_LANGUAGE flag(s)"


def find_targets(conn, statuses):
    """
    Returns rows of (flag_id, s_id, intent, review_status) for ALL active flags
    on sessions that have BOTH long pauses and an active ABUSIVE_LANGUAGE flag.
    """
    params = []
    if statuses:
        status_clause = "AND s.review_status IN (%s)" % ",".join("?" for _ in statuses)
        params += list(statuses)
    else:
        status_clause = ""

    return conn.execute(
        f"""
        SELECT f.flag_id AS flag_id, f.s_id AS s_id, f.intent AS intent,
               s.review_status AS review_status
        FROM audio_flags f
        JOIN audio_sessions s ON s.s_id = f.s_id
        WHERE s.pauses IS NOT NULL
          AND TRIM(s.pauses) NOT IN ('', '[]')
          {status_clause}
          AND (f.status IS NULL OR f.status != 'DISMISSED')
          AND f.flag_id NOT IN (
              SELECT parent_flag_id FROM audio_flags WHERE parent_flag_id IS NOT NULL)
          AND EXISTS (
              SELECT 1 FROM audio_flags af 
              WHERE af.s_id = s.s_id 
                AND UPPER(REPLACE(REPLACE(af.intent,'-','_'),' ','_')) = 'ABUSIVE_LANGUAGE'
                AND (af.status IS NULL OR af.status != 'DISMISSED')
                AND af.flag_id NOT IN (
                    SELECT parent_flag_id FROM audio_flags WHERE parent_flag_id IS NOT NULL)
          )
        ORDER BY f.s_id, f.flag_id
        """,
        params,
    ).fetchall()


def _pause_count(pauses) -> object:
    """Number of pause intervals in the JSON `pauses` value; '?' if unparseable."""
    if not pauses:
        return 0
    try:
        return len(json.loads(pauses))
    except (ValueError, TypeError):
        return "?"


def summarize_sessions(conn, targets):
    """One record per affected session: status, current verdict, pause count,
    how many live flags would be dismissed, and which intents."""
    session_ids = sorted({t["s_id"] for t in targets})
    placeholders = ",".join("?" for _ in session_ids)
    info = {
        r["s_id"]: r
        for r in conn.execute(
            f"SELECT s_id, review_status, overall_verdict, pauses "
            f"FROM audio_sessions WHERE s_id IN ({placeholders})",
            session_ids,
        ).fetchall()
    }
    flags_by_sid = defaultdict(list)
    for t in targets:
        flags_by_sid[t["s_id"]].append(t)

    rows = []
    for s_id in session_ids:
        r = info.get(s_id)
        flags = flags_by_sid[s_id]
        rows.append({
            "s_id": s_id,
            "review_status": (r["review_status"] if r else None) or "—",
            "verdict": _normalize_audio_verdict(r["overall_verdict"]) if r else None,
            "pause_count": _pause_count(r["pauses"]) if r else 0,
            "flag_count": len(flags),
            "intents": sorted({(f["intent"] or "—") for f in flags}),
        })
    return rows


def print_affected_sessions(session_rows):
    print(f"AFFECTED SESSIONS ({len(session_rows):,}) — all live flags on each would be "
          f"dismissed, making the session verdict CLEAN:\n")
    print(f"  {'SESSION':<10} {'STATUS':<20} {'VERDICT':<9} {'#PAUSES':<8} "
          f"{'#FLAGS':<7} INTENTS DISMISSED")
    print(f"  {'-'*10} {'-'*20} {'-'*9} {'-'*8} {'-'*7} {'-'*40}")
    for s in session_rows:
        print(f"  {str(s['s_id']):<10} {s['review_status']:<20} "
              f"{(s['verdict'] or '—'):<9} {str(s['pause_count']):<8} "
              f"{s['flag_count']:<7} {', '.join(s['intents'])}")
    print()


def run(commit: bool, statuses) -> int:
    conn = get_audio_connection()
    try:
        targets = find_targets(conn, statuses)
        if not targets:
            print("No sessions with both long pauses and ABUSIVE_LANGUAGE flags found in scope. Nothing to dismiss.")
            return 0

        session_ids = sorted({t["s_id"] for t in targets})
        session_rows = summarize_sessions(conn, targets)

        print(f"Found {len(targets):,} live flag(s) to dismiss across {len(session_ids):,} "
              f"session(s) with long pauses & ABUSIVE_LANGUAGE.\n")

        # Primary report: the affected sessions.
        print_affected_sessions(session_rows)

        # Secondary detail: individual flags (capped).
        print(f"  {'SESSION':<10} {'FLAG ID':<10} {'INTENT':<25} {'STATUS':<24}")
        print(f"  {'-'*10} {'-'*10} {'-'*25} {'-'*24}")
        for t in targets[:20]:
            print(f"  {t['s_id']:<10} {t['flag_id']:<10} {(t['intent'] or '—'):<25} "
                  f"{t['review_status'] or '—':<24}")
        if len(targets) > 20:
            print(f"  ... and {len(targets) - 20:,} more flags.")
        print()

        status_counts = Counter(t["review_status"] or "—" for t in targets)
        print("  Flags by session review_status:")
        for status, count in sorted(status_counts.items()):
            print(f"    {status:<24} {count:>6,}")
        print()

        if not commit:
            print(f"DRY RUN — would dismiss {len(targets):,} flag(s) across "
                  f"{len(session_ids):,} session(s) and recompute their verdicts "
                  f"(sessions will become CLEAN). No changes written. Re-run with --commit to apply.")
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

        now_clean = 0
        for s_id in session_ids:
            if recompute_audio_session_verdict(s_id, conn) == "CLEAN":
                now_clean += 1
        conn.commit()

        print(f"Done. Dismissed {len(targets):,} flag(s) across {len(session_ids):,} session(s); "
              f"{now_clean:,} session(s) are now CLEAN.")
        return len(targets)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dismiss all cases (sessions) in audio DB where there are long pauses AND abusive language."
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

    print("=" * 80)
    print("  Dismiss all cases (sessions) with long pauses AND ABUSIVE_LANGUAGE flags")
    print("=" * 80)
    print(f"  Database   : {AUDIO_DB_PATH}")
    print(f"  Scope      : {', '.join(statuses) if statuses else 'ALL statuses'}")
    print(f"  Mode       : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    run(commit=args.commit, statuses=statuses)


if __name__ == "__main__":
    main()
