"""
confirm_lock_or_dismiss_pending_chat.py

Chat review database (store/astrotalk.db). Decisively triage PENDING chat
sessions by flag confidence:

  * flag confidence >= 0.9  -> CONFIRM the flag; its session is then LOCKED.
  * flag confidence <  0.9  -> DISMISS the flag ("rest all dismiss"). A NULL
    confidence is treated as the "rest" too (not >= 0.9), so it is dismissed.

Per session the net effect:
  - A session with at least one confirmed (>= 0.9) flag ends up with those flags
    CONFIRMED, its weaker flags DISMISSED, and the session LOCKED.
  - A session whose flags are all below 0.9 has them all dismissed and, with no
    active flag left, recomputes to CLEAN (it is not locked).

Already-CONFIRMED flags are respected: they stay confirmed (and keep their
session lockable) and are never dismissed, even if below 0.9. Only the ACTIVE row
of a flag is touched (amendment row if edited, else original); DISMISSED rows and
amended originals are ignored.

Actions mirror the live review app (review_interface/api/main.py):
  - confirm: status = 'CONFIRMED' + a 'CONFIRM_FLAG' row in review_log.
  - dismiss: status = 'DISMISSED' + a 'DISMISSED' row in review_log.
  - lock:    review_status = 'LOCKED', locked_by/locked_at (store.db lock_session).
  - each touched session's verdict is recomputed via engine.verdict_rules.
Flags are never hard-deleted.

SCOPE: PENDING sessions only by default (per the request). Override with --status.

DRY-RUN BY DEFAULT — previews confirmations, dismissals and locks. Pass --commit
to apply.

Usage:
  python scripts/confirm_lock_or_dismiss_pending_chat.py            # preview (dry-run)
  python scripts/confirm_lock_or_dismiss_pending_chat.py --commit   # apply
  python scripts/confirm_lock_or_dismiss_pending_chat.py --threshold 0.85 --commit
  python scripts/confirm_lock_or_dismiss_pending_chat.py --status PENDING,SUBMITTED_FOR_REVIEW --commit
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import DB_PATH, get_connection  # noqa: E402
from engine.verdict_rules import (  # noqa: E402
    get_db_confidence_for_verdict,
    get_db_verdict_for_flags,
)

DEFAULT_STATUSES = ("PENDING",)
THRESHOLD = 0.9
REVIEWER_ID = "AUTO_CONFIRM_LOCK"
CONFIRM_NOTE = "Auto-confirm: confidence >= threshold"
DISMISS_NOTE = "Auto-dismiss: confidence < threshold (rest)"


def find_active_flags(conn, statuses):
    """Active flags on in-scope sessions with confidence/status/source.

    Active = not DISMISSED and not an amended original (the amendment row counts).
    Includes already-CONFIRMED rows so they can keep a session lockable.
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
        SELECT f.flag_id, f.session_id, f.category_code,
               f.confidence_score AS confidence, f.status, f.source,
               s.review_status
        FROM flags f
        JOIN sessions s ON s.session_id = f.session_id
        WHERE {status_clause}
          AND (f.status IS NULL OR f.status != 'DISMISSED')
          AND (
              f.parent_flag_id IS NOT NULL
              OR f.flag_id NOT IN (
                  SELECT parent_flag_id FROM flags WHERE parent_flag_id IS NOT NULL)
          )
        ORDER BY f.session_id, f.flag_id
        """,
        params,
    ).fetchall()


def recompute_chat_session_verdict(session_id: str, conn) -> str:
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


def run(commit: bool, statuses, threshold) -> dict:
    conn = get_connection()
    try:
        rows = find_active_flags(conn, statuses)

        # Decide an action per flag; already-CONFIRMED flags are left as confirmed.
        to_confirm, to_dismiss = [], []
        confirmed_by_session = defaultdict(int)   # sessions that will hold >=1 confirmed flag
        for r in rows:
            if (r["status"] or "") == "CONFIRMED":
                confirmed_by_session[r["session_id"]] += 1
                continue
            conf = r["confidence"]
            if conf is not None and conf >= threshold:
                to_confirm.append(r)
                confirmed_by_session[r["session_id"]] += 1
            else:
                to_dismiss.append(r)

        touched_sessions = sorted({r["session_id"] for r in rows})
        to_lock = sorted(sid for sid in confirmed_by_session if confirmed_by_session[sid] > 0)
        # Sessions where every flag is being dismissed (no confirmed flag) -> will go CLEAN.
        to_clean = sorted(set(touched_sessions) - set(to_lock))

        if not rows:
            print("No active flags on in-scope sessions. Nothing to do.")
            return {"confirmed": 0, "dismissed": 0, "locked": 0, "cleaned": 0, "sessions": 0}

        print(f"In-scope sessions with active flags : {len(touched_sessions):,}")
        print(f"  Flags to CONFIRM (conf >= {threshold}) : {len(to_confirm):,}")
        print(f"  Flags to DISMISS (conf <  {threshold}) : {len(to_dismiss):,}")
        print(f"  Sessions to LOCK  (have a confirmed flag) : {len(to_lock):,}")
        print(f"  Sessions to CLEAN (all flags dismissed)   : {len(to_clean):,}")
        print()

        if to_confirm:
            print("  CONFIRM + LOCK:")
            print(f"    {'SESSION':<16} {'FLAG':<6} {'CATEGORY':<26} {'CONF':<6} {'SRC':<7}")
            print(f"    {'-'*16} {'-'*6} {'-'*26} {'-'*6} {'-'*7}")
            for r in to_confirm:
                print(f"    {str(r['session_id']):<16} {r['flag_id']:<6} "
                      f"{str(r['category_code'] or '—')[:26]:<26} {r['confidence']!s:<6} "
                      f"{str(r['source'] or '—'):<7}")
            print()
        if to_dismiss:
            print("  DISMISS (rest):")
            print(f"    {'SESSION':<16} {'FLAG':<6} {'CATEGORY':<26} {'CONF':<6} {'SRC':<7}")
            print(f"    {'-'*16} {'-'*6} {'-'*26} {'-'*6} {'-'*7}")
            for r in to_dismiss:
                print(f"    {str(r['session_id']):<16} {r['flag_id']:<6} "
                      f"{str(r['category_code'] or '—')[:26]:<26} "
                      f"{('NULL' if r['confidence'] is None else r['confidence'])!s:<6} "
                      f"{str(r['source'] or '—'):<7}")
            print()

        if not commit:
            print(f"DRY RUN — would confirm {len(to_confirm):,} flag(s), dismiss "
                  f"{len(to_dismiss):,} flag(s), LOCK {len(to_lock):,} session(s) and "
                  f"CLEAN {len(to_clean):,} session(s). No changes written. "
                  f"Re-run with --commit to apply.")
            return {"confirmed": len(to_confirm), "dismissed": len(to_dismiss),
                    "locked": len(to_lock), "cleaned": len(to_clean),
                    "sessions": len(touched_sessions)}

        with conn:
            for r in to_confirm:
                conn.execute("UPDATE flags SET status = 'CONFIRMED' WHERE flag_id = ?", (r["flag_id"],))
                conn.execute(
                    """INSERT INTO review_log (session_id, flag_id, action, reviewer_id, note)
                       VALUES (?, ?, 'CONFIRM_FLAG', ?, ?)""",
                    (r["session_id"], r["flag_id"], REVIEWER_ID, CONFIRM_NOTE),
                )
            for r in to_dismiss:
                conn.execute("UPDATE flags SET status = 'DISMISSED' WHERE flag_id = ?", (r["flag_id"],))
                conn.execute(
                    """INSERT INTO review_log (session_id, flag_id, action, reviewer_id, note)
                       VALUES (?, ?, 'DISMISSED', ?, ?)""",
                    (r["session_id"], r["flag_id"], REVIEWER_ID, DISMISS_NOTE),
                )
            for sid in touched_sessions:
                recompute_chat_session_verdict(sid, conn)
            for sid in to_lock:
                conn.execute(
                    """UPDATE sessions
                       SET review_status = 'LOCKED', locked_by = ?, locked_at = datetime('now')
                       WHERE session_id = ?""",
                    (REVIEWER_ID, sid),
                )

        print(f"Done. Confirmed {len(to_confirm):,} flag(s), dismissed {len(to_dismiss):,} "
              f"flag(s); LOCKED {len(to_lock):,} session(s), CLEANED {len(to_clean):,} session(s).")
        return {"confirmed": len(to_confirm), "dismissed": len(to_dismiss),
                "locked": len(to_lock), "cleaned": len(to_clean),
                "sessions": len(touched_sessions)}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Chat DB: for PENDING sessions, confirm+lock flags with confidence >= threshold "
                    "and dismiss the rest."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually apply (default is dry-run preview only).")
    parser.add_argument("--threshold", type=float, default=THRESHOLD,
                        help=f"Confidence pivot: >= confirms (and locks the session), below dismisses "
                             f"(default: {THRESHOLD}).")
    parser.add_argument("--status", default=",".join(DEFAULT_STATUSES),
                        help="Comma-separated review_status values to handle. "
                             "Default: PENDING. Pass 'ALL' for every status.")
    args = parser.parse_args()

    if str(args.status).strip().upper() == "ALL":
        statuses = None
    else:
        statuses = tuple(s.strip().upper() for s in args.status.split(",") if s.strip())

    print("=" * 72)
    print("  Chat DB: PENDING sessions — confirm+lock (conf >= threshold), dismiss the rest")
    print("=" * 72)
    print(f"  Database   : {DB_PATH}")
    print(f"  Threshold  : {args.threshold}  (>= confirm+lock, < dismiss)")
    print(f"  Scope      : {', '.join(statuses) if statuses else 'ALL statuses'}")
    print(f"  Mode       : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    result = run(commit=args.commit, statuses=statuses, threshold=args.threshold)

    print()
    print(f"  Flags confirmed{'' if args.commit else ' (would)'} : {result['confirmed']:,}")
    print(f"  Flags dismissed{'' if args.commit else ' (would)'} : {result['dismissed']:,}")
    print(f"  Sessions locked{'' if args.commit else ' (would)'} : {result['locked']:,}")
    print(f"  Sessions cleaned{'' if args.commit else ' (would)'}: {result['cleaned']:,}")


if __name__ == "__main__":
    main()
