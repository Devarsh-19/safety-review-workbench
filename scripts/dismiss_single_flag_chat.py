"""
dismiss_single_flag_chat.py

Chat counterpart of scripts/dismiss_single_flag_audio.py — operates on the chat
review database (store/astrotalk.db, flags.confidence_score), not the audio DB.

Triage every chat session that has exactly ONE active flag, splitting on that
flag's confidence at a threshold (default 0.9):

  * confidence <  threshold  -> DISMISS the flag. A single low-confidence flag is
    a likely false positive; dismissing its one flag leaves the session with no
    active flag, so its verdict recomputes to CLEAN.

  * confidence >= threshold  -> KEEP the flag but set its severity = 'LOW' and
    LOCK the session (finalize it). A single high-confidence flag is real but
    minor, so it is downgraded and the session is closed out.

"Active flag" uses the review-queue definition — DISMISSED rows are excluded and
an original that has an amendment is not counted (the amendment row is). A session
qualifies only when exactly one such flag remains.

CONFIRMED flags are respected in the DISMISS branch: if a session's single active
flag has already been CONFIRMED by a reviewer, it is reported but NOT dismissed
(a confirmation is a human decision we don't auto-override). The LOCK branch keeps
the flag, so it applies regardless of ACTIVE/CONFIRMED status. A flag with a NULL
confidence is skipped and reported (a missing score fits neither branch).

Actions follow the repo conventions:
  * dismiss  — soft dismiss (see dismiss_low_confidence_flags.py): status =
    'DISMISSED', a 'DISMISSED' row in review_log, verdict recomputed (now CLEAN).
  * lock     — severity set to 'LOW', an 'AMENDED' row in review_log, and the
    session locked the same way as auto_process_clean_sessions.py (review_status
    = 'LOCKED', locked_by/locked_at, reviewer fields). Sessions already LOCKED
    get the severity downgrade only. Flags are never hard-deleted.

SCOPE: by default only PENDING and LOCKED sessions are handled. Override with
--status.

DRY-RUN BY DEFAULT — previews affected sessions, how many would become CLEAN, and
how many would be locked. Pass --commit to apply.

Usage:
  python scripts/dismiss_single_flag_chat.py            # preview PENDING+LOCKED (dry-run)
  python scripts/dismiss_single_flag_chat.py --commit   # apply to PENDING+LOCKED
  python scripts/dismiss_single_flag_chat.py --threshold 0.85 --commit
  python scripts/dismiss_single_flag_chat.py --status ALL --commit
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import DB_PATH, get_connection  # noqa: E402
from engine.verdict_rules import (  # noqa: E402
    get_db_confidence_for_verdict,
    get_db_verdict_for_flags,
)

DEFAULT_STATUSES = ("PENDING", "LOCKED")
THRESHOLD = 0.9
LOCKED_BY = "AUTO_LOCK"
REVIEWER_ID = "AUTO_DISMISS"
DISMISS_NOTE = "Auto-dismiss: session had exactly one active flag, confidence < threshold"
LOCK_NOTE = "Auto: single high-confidence flag, severity set to LOW and session locked"
LOCK_SEVERITY = "LOW"


def find_single_flag_sessions(conn, statuses):
    """Sessions with exactly ONE active flag; returns that flag's fields.

    Active = not DISMISSED and not an amended original (the amendment row counts).
    With COUNT(*) = 1 the group has a single flag, so MIN(...) returns that flag's
    own values. Rows: (session_id, flag_id, review_status, confidence,
    category_code, status, severity).
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
        SELECT s.session_id AS session_id,
               MIN(f.flag_id) AS flag_id,
               s.review_status AS review_status,
               MIN(f.confidence_score) AS confidence,
               MIN(f.category_code) AS category_code,
               MIN(f.status) AS status,
               MIN(f.severity) AS severity
        FROM sessions s
        JOIN flags f ON f.session_id = s.session_id
        WHERE {status_clause}
          AND (f.status IS NULL OR f.status != 'DISMISSED')
          AND f.flag_id NOT IN (
              SELECT parent_flag_id FROM flags WHERE parent_flag_id IS NOT NULL)
        GROUP BY s.session_id
        HAVING COUNT(*) = 1
        ORDER BY s.session_id
        """,
        params,
    ).fetchall()


def recompute_chat_session_verdict(session_id: str, conn) -> str:
    """Recompute overall_verdict/confidence from the session's ACTIVE flags,
    ignoring dismissed rows and amended originals. Mirrors
    dismiss_low_confidence_flags.py.
    """
    rows = conn.execute(
        "SELECT flag_id, category_code, status, parent_flag_id FROM flags WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}
    active_codes = [
        r["category_code"] for r in rows
        if (r["status"] or "") != "DISMISSED"
        and (r["parent_flag_id"] is not None or r["flag_id"] not in amended_parents)
    ]
    verdict = get_db_verdict_for_flags(active_codes)
    confidence = get_db_confidence_for_verdict(verdict)
    conn.execute(
        "UPDATE sessions SET overall_verdict = ?, confidence_score = ? WHERE session_id = ?",
        (verdict, confidence, session_id),
    )
    return verdict


def _print_table(title, rows):
    print(f"  {title}")
    print(f"    {'SESSION':<18} {'STATUS':<12} {'FLAG':<6} {'CATEGORY':<26} {'CONF':<6} {'SEV':<7} {'FLAG ST':<10}")
    print(f"    {'-'*18} {'-'*12} {'-'*6} {'-'*26} {'-'*6} {'-'*7} {'-'*10}")
    for t in rows:
        print(f"    {str(t['session_id']):<18} {t['review_status'] or '—':<12} "
              f"{t['flag_id']:<6} {str(t['category_code'] or '—')[:26]:<26} "
              f"{t['confidence']!s:<6} {str(t['severity'] or '—'):<7} {t['status'] or '—':<10}")
    print()


def run(commit: bool, statuses, threshold) -> dict:
    conn = get_connection()
    try:
        targets = find_single_flag_sessions(conn, statuses)

        # Partition single-flag sessions on the confidence pivot.
        dismiss, dismiss_confirmed, lock, skipped_null = [], [], [], []
        for t in targets:
            conf = t["confidence"]
            if conf is None:
                skipped_null.append(t)
            elif conf < threshold:
                (dismiss_confirmed if (t["status"] or "") == "CONFIRMED" else dismiss).append(t)
            else:
                lock.append(t)

        if not targets:
            print("No sessions with exactly one active flag in scope. Nothing to do.")
            return {"dismissed": 0, "now_clean": 0, "locked": 0,
                    "severity_set": 0, "skipped_confirmed": 0, "skipped_null": 0}

        print(f"Found {len(targets):,} single-flag session(s): "
              f"{len(dismiss):,} to dismiss (conf < {threshold}), "
              f"{len(lock):,} to lock (conf >= {threshold}), "
              f"{len(dismiss_confirmed):,} CONFIRMED-skipped, "
              f"{len(skipped_null):,} NULL-confidence-skipped.\n")

        if dismiss:
            _print_table(f"DISMISS (conf < {threshold}) — session becomes CLEAN:", dismiss)
        if lock:
            _print_table(f"LOCK + severity {LOCK_SEVERITY} (conf >= {threshold}):", lock)
        if dismiss_confirmed:
            _print_table("SKIPPED — single flag already CONFIRMED (not dismissed):", dismiss_confirmed)
        if skipped_null:
            _print_table("SKIPPED — NULL confidence:", skipped_null)

        # Every dismissed single-flag session loses its only flag -> CLEAN.
        now_clean = len(dismiss)
        # Lock branch: sessions not already LOCKED get their status flipped;
        # already-LOCKED ones only get the severity downgrade.
        to_lock = [t for t in lock if (t["review_status"] or "") != "LOCKED"]

        print(f"  Dismiss by review_status: "
              f"{dict(Counter(t['review_status'] or '—' for t in dismiss))}")
        print(f"  Lock    by review_status: "
              f"{dict(Counter(t['review_status'] or '—' for t in lock))}")
        print()

        if not commit:
            print(f"DRY RUN — would DISMISS {len(dismiss):,} flag(s) "
                  f"({now_clean:,} session(s) become CLEAN); "
                  f"set severity {LOCK_SEVERITY} on {len(lock):,} flag(s) and LOCK "
                  f"{len(to_lock):,} session(s) ({len(lock) - len(to_lock):,} already LOCKED). "
                  f"No changes written. Re-run with --commit to apply.")
            return {"dismissed": len(dismiss), "now_clean": now_clean,
                    "locked": len(to_lock), "severity_set": len(lock),
                    "skipped_confirmed": len(dismiss_confirmed),
                    "skipped_null": len(skipped_null)}

        clean_count = 0
        with conn:
            # DISMISS branch.
            for t in dismiss:
                conn.execute(
                    """INSERT INTO review_log (session_id, flag_id, action, reviewer_id, note)
                       VALUES (?, ?, 'DISMISSED', ?, ?)""",
                    (t["session_id"], t["flag_id"], REVIEWER_ID, DISMISS_NOTE),
                )
                conn.execute(
                    "UPDATE flags SET status = 'DISMISSED' WHERE flag_id = ?",
                    (t["flag_id"],),
                )
            for t in dismiss:
                if recompute_chat_session_verdict(t["session_id"], conn) == "CLEAN":
                    clean_count += 1

            # LOCK branch: severity LOW for all; lock the not-yet-locked ones.
            for t in lock:
                conn.execute(
                    "UPDATE flags SET severity = ? WHERE flag_id = ?",
                    (LOCK_SEVERITY, t["flag_id"]),
                )
                conn.execute(
                    """INSERT INTO review_log (session_id, flag_id, action, reviewer_id, note)
                       VALUES (?, ?, 'AMENDED', ?, ?)""",
                    (t["session_id"], t["flag_id"], REVIEWER_ID, LOCK_NOTE),
                )
            for t in to_lock:
                conn.execute(
                    """UPDATE sessions
                       SET review_status = 'LOCKED',
                           locked_by = ?, locked_at = datetime('now'),
                           submitted_by = COALESCE(submitted_by, ?),
                           submitted_at = COALESCE(submitted_at, datetime('now')),
                           reviewer_id = ?,
                           reviewer_note = ?,
                           reviewed_at = datetime('now')
                       WHERE session_id = ?""",
                    (LOCKED_BY, LOCKED_BY, LOCKED_BY, LOCK_NOTE, t["session_id"]),
                )

        print(f"Done. Dismissed {len(dismiss):,} flag(s) -> {clean_count:,} session(s) CLEAN. "
              f"Set severity {LOCK_SEVERITY} on {len(lock):,} flag(s); LOCKED {len(to_lock):,} "
              f"session(s) ({len(lock) - len(to_lock):,} already LOCKED). "
              f"Left {len(dismiss_confirmed):,} CONFIRMED and {len(skipped_null):,} "
              f"NULL-confidence single-flag session(s) untouched.")
        return {"dismissed": len(dismiss), "now_clean": clean_count,
                "locked": len(to_lock), "severity_set": len(lock),
                "skipped_confirmed": len(dismiss_confirmed),
                "skipped_null": len(skipped_null)}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Chat DB: for single-flag sessions, dismiss the flag if confidence < threshold, "
                    "else set its severity LOW and lock the session."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually apply (default is dry-run preview only).")
    parser.add_argument("--threshold", type=float, default=THRESHOLD,
                        help=f"Confidence pivot: below it the flag is dismissed, at/above it the "
                             f"flag is downgraded to LOW and the session is locked (default: {THRESHOLD}).")
    parser.add_argument("--status", default=",".join(DEFAULT_STATUSES),
                        help="Comma-separated review_status values to handle. "
                             "Default: PENDING,LOCKED. Pass 'ALL' to consider every status.")
    args = parser.parse_args()

    if str(args.status).strip().upper() == "ALL":
        statuses = None
    else:
        statuses = tuple(s.strip().upper() for s in args.status.split(",") if s.strip())

    print("=" * 74)
    print("  Single-flag chat sessions: dismiss (conf < threshold) or downgrade+lock (>=)")
    print("=" * 74)
    print(f"  Database   : {DB_PATH}")
    print(f"  Threshold  : {args.threshold}  (< dismiss, >= severity {LOCK_SEVERITY} + lock)")
    print(f"  Scope      : {', '.join(statuses) if statuses else 'ALL statuses'}")
    print(f"  Mode       : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    result = run(commit=args.commit, statuses=statuses, threshold=args.threshold)

    print()
    print(f"  Flags dismissed{'' if args.commit else ' (would)'}         : {result['dismissed']:,}")
    print(f"  Sessions {'now' if args.commit else 'would be'} CLEAN      : {result['now_clean']:,}")
    print(f"  Flags downgraded to {LOCK_SEVERITY}{'' if args.commit else ' (would)'}    : {result['severity_set']:,}")
    print(f"  Sessions {'now' if args.commit else 'would be'} LOCKED     : {result['locked']:,}")


if __name__ == "__main__":
    main()
