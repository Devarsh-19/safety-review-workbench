"""
confirm_lock_nsfw_family_chat.py

Chat review database (store/astrotalk.db). For NSFW-family flags —
    NSFW, NSFW_APPEARANCE, NSFW_GROOMING, NSFW_EXPLICIT, CSAM_RISK
— that carry a confidence_score >= 0.85, CONFIRM the flag and LOCK its session.

Nothing is dismissed: this is a confirm-and-lock pass only. Flags below the
threshold, non-NSFW flags, and sessions with no qualifying NSFW flag are left
exactly as they are.

Matching:
  - category: category_code in the five NSFW-family codes above (case-insensitive).
  - confidence: confidence_score >= 0.85 (configurable via --min-confidence).
    Flags with a NULL confidence are excluded (a missing score is never ">=").
  - flag: only the ACTIVE row is confirmed — the amendment row if the flag was
    edited, else the original. Already-CONFIRMED flags are skipped (idempotent),
    and DISMISSED rows are ignored.

Actions mirror the live review app (review_interface/api/main.py):
  - confirm: UPDATE flags SET status = 'CONFIRMED', plus a 'CONFIRM_FLAG' row in
    review_log; the session verdict is then recomputed (confirming keeps the flag
    active, so a flagged session stays flagged).
  - lock:    review_status = 'LOCKED', locked_by/locked_at set (store.db
    lock_session). A session already LOCKED is left locked (flags still confirmed).

SCOPE: by default only PENDING and SUBMITTED_FOR_REVIEW sessions are handled
(the un-finalised ones where confirm-and-lock makes sense). Override with
--status (e.g. --status ALL to also touch LOCKED/REVIEWED).

DRY-RUN BY DEFAULT — previews the flags that would be confirmed and the sessions
that would be locked. Pass --commit to apply.

Usage:
  python scripts/confirm_lock_nsfw_family_chat.py            # preview (dry-run)
  python scripts/confirm_lock_nsfw_family_chat.py --commit   # apply
  python scripts/confirm_lock_nsfw_family_chat.py --min-confidence 0.8 --commit
  python scripts/confirm_lock_nsfw_family_chat.py --status ALL --commit
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

NSFW_FAMILY = ("NSFW", "NSFW_APPEARANCE", "NSFW_GROOMING", "NSFW_EXPLICIT", "CSAM_RISK")
DEFAULT_STATUSES = ("PENDING", "SUBMITTED_FOR_REVIEW")
MIN_CONFIDENCE = 0.85
REVIEWER_ID = "AUTO_NSFW_LOCK"


def find_targets(conn, statuses, min_conf):
    """Active NSFW-family flags with confidence >= min_conf, in scope.

    Active = the amendment row if the flag was edited (else the original),
    excluding already-CONFIRMED and DISMISSED rows. Rows:
    (flag_id, session_id, category_code, confidence, status, review_status).
    """
    params = list(NSFW_FAMILY)
    if statuses:
        status_clause = "AND s.review_status IN (%s)" % ",".join("?" for _ in statuses)
        params.extend(statuses)
    else:
        status_clause = ""
    params.append(min_conf)

    return conn.execute(
        f"""
        SELECT f.flag_id, f.session_id, f.category_code,
               f.confidence_score AS confidence, f.status, s.review_status
        FROM flags f
        JOIN sessions s ON s.session_id = f.session_id
        WHERE UPPER(f.category_code) IN ({",".join("?" for _ in NSFW_FAMILY)})
          {status_clause}
          AND (f.status IS NULL OR f.status NOT IN ('CONFIRMED', 'DISMISSED'))
          AND (
              f.parent_flag_id IS NOT NULL
              OR f.flag_id NOT IN (
                  SELECT parent_flag_id FROM flags WHERE parent_flag_id IS NOT NULL)
          )
          AND f.confidence_score IS NOT NULL AND f.confidence_score >= ?
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


def run(commit: bool, statuses, min_conf) -> dict:
    conn = get_connection()
    try:
        targets = find_targets(conn, statuses, min_conf)
        if not targets:
            print(f"No active NSFW-family flags (conf >= {min_conf}) in scope. Nothing to do.")
            return {"confirmed": 0, "locked": 0, "sessions": 0}

        sessions = sorted({t["session_id"] for t in targets})
        to_lock = sorted({t["session_id"] for t in targets if (t["review_status"] or "") != "LOCKED"})

        print(f"Found {len(targets):,} NSFW-family flag(s) (conf >= {min_conf}) to confirm "
              f"across {len(sessions):,} session(s); {len(to_lock):,} would be locked "
              f"({len(sessions) - len(to_lock):,} already LOCKED).\n")
        print(f"  {'SESSION':<18} {'FLAG':<6} {'CATEGORY':<18} {'CONF':<6} {'SESSION STATUS':<20}")
        print(f"  {'-'*18} {'-'*6} {'-'*18} {'-'*6} {'-'*20}")
        for t in targets:
            print(f"  {str(t['session_id']):<18} {t['flag_id']:<6} "
                  f"{str(t['category_code'] or '—'):<18} {t['confidence']!s:<6} "
                  f"{t['review_status'] or '—':<20}")
        print()
        print("  Flags by category:")
        for cat, n in sorted(Counter(t["category_code"] for t in targets).items()):
            print(f"    {cat:<18} {n:>6,}")
        print()

        if not commit:
            print(f"DRY RUN — would confirm {len(targets):,} flag(s) and lock "
                  f"{len(to_lock):,} session(s). No changes written. "
                  f"Re-run with --commit to apply.")
            return {"confirmed": len(targets), "locked": len(to_lock), "sessions": len(sessions)}

        with conn:
            for t in targets:
                conn.execute(
                    "UPDATE flags SET status = 'CONFIRMED' WHERE flag_id = ?",
                    (t["flag_id"],),
                )
                conn.execute(
                    """INSERT INTO review_log (session_id, flag_id, action, reviewer_id, note)
                       VALUES (?, ?, 'CONFIRM_FLAG', ?, '')""",
                    (t["session_id"], t["flag_id"], REVIEWER_ID),
                )
            for session_id in sessions:
                recompute_chat_session_verdict(session_id, conn)
            for session_id in to_lock:
                conn.execute(
                    """UPDATE sessions
                       SET review_status = 'LOCKED',
                           locked_by = ?, locked_at = datetime('now')
                       WHERE session_id = ?""",
                    (REVIEWER_ID, session_id),
                )

        print(f"Done. Confirmed {len(targets):,} flag(s); locked {len(to_lock):,} session(s) "
              f"({len(sessions) - len(to_lock):,} already LOCKED).")
        return {"confirmed": len(targets), "locked": len(to_lock), "sessions": len(sessions)}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Chat DB: confirm NSFW-family flags with confidence >= threshold and lock their sessions."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually apply (default is dry-run preview only).")
    parser.add_argument("--min-confidence", type=float, default=MIN_CONFIDENCE,
                        help=f"Only confirm/lock flags with confidence >= this value (default: {MIN_CONFIDENCE}).")
    parser.add_argument("--status", default=",".join(DEFAULT_STATUSES),
                        help="Comma-separated review_status values to handle. "
                             "Default: PENDING,SUBMITTED_FOR_REVIEW. Pass 'ALL' for every status.")
    args = parser.parse_args()

    if str(args.status).strip().upper() == "ALL":
        statuses = None
    else:
        statuses = tuple(s.strip().upper() for s in args.status.split(",") if s.strip())

    print("=" * 72)
    print("  Chat DB: confirm NSFW-family flags (conf >= threshold) and lock sessions")
    print("=" * 72)
    print(f"  Database   : {DB_PATH}")
    print(f"  Categories : {', '.join(NSFW_FAMILY)}")
    print(f"  Min conf   : >= {args.min_confidence}")
    print(f"  Scope      : {', '.join(statuses) if statuses else 'ALL statuses'}")
    print(f"  Mode       : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    result = run(commit=args.commit, statuses=statuses, min_conf=args.min_confidence)

    print()
    print(f"  Flags {'confirmed' if args.commit else 'to confirm'} : {result['confirmed']:,}")
    print(f"  Sessions {'locked' if args.commit else 'to lock'}  : {result['locked']:,}")


if __name__ == "__main__":
    main()
