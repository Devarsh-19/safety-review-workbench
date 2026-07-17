"""
dismiss_lowconf_user_flags_chat.py

Chat review database (store/astrotalk.db) only. Cleans up "user flags" — flags
that sit on a USER-spoken turn (turns.speaker = 'USER') — by confidence:

  * confidence_score >= 0.9  -> KEEP, left exactly as is (severity unchanged).
  * confidence_score <  0.9  -> DISMISS the flag AND set its severity = 'LOW'.
  * confidence_score IS NULL -> SKIPPED and reported (a missing confidence is not
    dismissed on missing data; re-run intent must be explicit).

Only ACTIVE flags are considered — already-CONFIRMED or already-DISMISSED rows,
and amended originals (whose amendment row is the live one), are left alone. Only
flags whose turn_id resolves to a turn with speaker = 'USER' qualify; a flag with
no matching turn is not a "user flag" and is ignored.

Dismissal follows the repo convention (soft dismiss, see
dismiss_low_confidence_flags.py): the flag row is set to status = 'DISMISSED'
(restorable), a 'DISMISSED' row is written to review_log, and each affected
session's overall_verdict is recomputed via engine.verdict_rules (a session left
with no active flag becomes CLEAN). Flags are never hard-deleted. The severity =
'LOW' write is applied to the same dismissed rows and noted in the log.

SCOPE: by default only PENDING, SUBMITTED_FOR_REVIEW and LOCKED sessions are
cleaned. Override with --status (e.g. --status ALL).

DRY-RUN BY DEFAULT — pass --commit to actually change anything.

Usage:
  python scripts/dismiss_lowconf_user_flags_chat.py              # preview (dry-run)
  python scripts/dismiss_lowconf_user_flags_chat.py --commit     # apply
  python scripts/dismiss_lowconf_user_flags_chat.py --min-confidence 0.85 --commit
  python scripts/dismiss_lowconf_user_flags_chat.py --status ALL --commit
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

DEFAULT_STATUSES = ("PENDING", "SUBMITTED_FOR_REVIEW", "LOCKED")
KEEP_THRESHOLD = 0.9          # conf >= this is kept; below is dismissed
DISMISS_SEVERITY = "LOW"      # severity written onto the dismissed flags
REVIEWER_ID = "AUTO_DISMISS"
NOTE = "Auto-dismiss: USER-turn flag, confidence < threshold; severity set to LOW"


def find_user_flags(conn, statuses):
    """Active flags on USER-spoken turns within scope, with their confidence and
    severity. Excludes CONFIRMED/DISMISSED rows and amended originals.
    """
    params = []
    if statuses:
        status_clause = "AND s.review_status IN (%s)" % ",".join("?" for _ in statuses)
        params.extend(statuses)
    else:
        status_clause = ""

    return conn.execute(
        f"""
        SELECT f.flag_id, f.session_id, f.category_code,
               f.confidence_score AS confidence, f.severity, f.status,
               s.review_status
        FROM flags f
        JOIN sessions s ON s.session_id = f.session_id
        JOIN turns t ON t.session_id = f.session_id AND t.turn_id = f.turn_id
        WHERE t.speaker = 'USER'
          {status_clause}
          AND (f.status IS NULL OR f.status NOT IN ('CONFIRMED', 'DISMISSED'))
          AND (
              f.parent_flag_id IS NOT NULL
              OR f.flag_id NOT IN (
                  SELECT parent_flag_id FROM flags WHERE parent_flag_id IS NOT NULL
              )
          )
        ORDER BY f.session_id, f.flag_id
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


def classify(flags, threshold):
    """Split active user flags into (keep, dismiss, skipped_null)."""
    keep, dismiss, skipped_null = [], [], []
    for f in flags:
        conf = f["confidence"]
        if conf is None:
            skipped_null.append(f)
        elif conf >= threshold:
            keep.append(f)
        else:
            dismiss.append(f)
    return keep, dismiss, skipped_null


def run(commit: bool, statuses, threshold) -> dict:
    conn = get_connection()
    try:
        flags = find_user_flags(conn, statuses)
        keep, dismiss, skipped_null = classify(flags, threshold)

        touched_sessions = sorted({f["session_id"] for f in dismiss})

        print(f"  USER-turn active flags in scope : {len(flags):,}")
        print(f"  KEEP    (conf >= {threshold}, unchanged): {len(keep):,}")
        print(f"  DISMISS (conf <  {threshold}, +severity {DISMISS_SEVERITY}): "
              f"{len(dismiss):,}  across {len(touched_sessions):,} session(s)")
        print(f"  SKIPPED (confidence is NULL)    : {len(skipped_null):,}")
        print()

        if dismiss:
            print("  WOULD DISMISS + set severity LOW:")
            print(f"    {'SESSION':<18} {'FLAG':<8} {'CATEGORY':<28} {'CONF':<6} {'SEV':<8} {'STATUS':<20}")
            print(f"    {'-'*18} {'-'*8} {'-'*28} {'-'*6} {'-'*8} {'-'*20}")
            for f in dismiss:
                print(f"    {str(f['session_id']):<18} {f['flag_id']:<8} "
                      f"{str(f['category_code'] or '-')[:28]:<28} {f['confidence']!s:<6} "
                      f"{str(f['severity'] or '-'):<8} {f['review_status'] or '-':<20}")
            print()
            print("  Dismissals by review_status:")
            for st, n in sorted(Counter(f["review_status"] or "-" for f in dismiss).items()):
                print(f"    {st:<22} {n:>6,}")
            print()

        if skipped_null:
            print("  SKIPPED (NULL confidence — not dismissed):")
            for f in skipped_null:
                print(f"    {str(f['session_id']):<18} flag {f['flag_id']} "
                      f"({f['category_code'] or '-'})")
            print()

        # Predict how many touched sessions become CLEAN once the dismissals apply.
        dismiss_ids = {f["flag_id"] for f in dismiss}
        now_clean = 0
        for sid in touched_sessions:
            rows = conn.execute(
                "SELECT flag_id, category_code, status, parent_flag_id FROM flags WHERE session_id = ?",
                (sid,),
            ).fetchall()
            amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}
            active_after = [
                r["category_code"] for r in rows
                if (r["status"] or "") != "DISMISSED"
                and r["flag_id"] not in dismiss_ids
                and (r["parent_flag_id"] is not None or r["flag_id"] not in amended_parents)
            ]
            if get_db_verdict_for_flags(active_after) == "CLEAN":
                now_clean += 1

        if not commit:
            print(f"DRY RUN — would dismiss {len(dismiss):,} flag(s) (and set severity "
                  f"{DISMISS_SEVERITY}) across {len(touched_sessions):,} session(s); "
                  f"{now_clean:,} would become CLEAN. {len(keep):,} kept unchanged. "
                  f"No changes written. Re-run with --commit to apply.")
            return {"dismissed": len(dismiss), "kept": len(keep),
                    "sessions": len(touched_sessions), "now_clean": now_clean,
                    "skipped_null": len(skipped_null)}

        with conn:
            for f in dismiss:
                conn.execute(
                    "UPDATE flags SET status = 'DISMISSED', severity = ? WHERE flag_id = ?",
                    (DISMISS_SEVERITY, f["flag_id"]),
                )
                conn.execute(
                    """INSERT INTO review_log (session_id, flag_id, action, reviewer_id, note)
                       VALUES (?, ?, 'DISMISSED', ?, ?)""",
                    (f["session_id"], f["flag_id"], REVIEWER_ID, NOTE),
                )
            for sid in touched_sessions:
                recompute_chat_session_verdict(sid, conn)

        print(f"Done. Dismissed {len(dismiss):,} USER-turn flag(s) (severity set to "
              f"{DISMISS_SEVERITY}) across {len(touched_sessions):,} session(s); "
              f"{now_clean:,} session(s) are now CLEAN. {len(keep):,} kept unchanged.")
        return {"dismissed": len(dismiss), "kept": len(keep),
                "sessions": len(touched_sessions), "now_clean": now_clean,
                "skipped_null": len(skipped_null)}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Chat DB: dismiss low-confidence USER-turn flags (and set their severity LOW); "
                    "keep conf >= threshold flags unchanged."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually apply (default is dry-run preview only).")
    parser.add_argument("--min-confidence", type=float, default=KEEP_THRESHOLD,
                        help=f"Flags with conf >= this are kept; below it are dismissed "
                             f"(default: {KEEP_THRESHOLD}).")
    parser.add_argument("--status", default=",".join(DEFAULT_STATUSES),
                        help="Comma-separated review_status values to clean. "
                             "Default: PENDING,SUBMITTED_FOR_REVIEW,LOCKED. "
                             "Pass 'ALL' to consider every status.")
    args = parser.parse_args()

    if str(args.status).strip().upper() == "ALL":
        statuses = None
    else:
        statuses = tuple(s.strip().upper() for s in args.status.split(",") if s.strip())

    print("=" * 74)
    print("  Chat DB: dismiss low-confidence USER-turn flags (+severity LOW), keep the rest")
    print("=" * 74)
    print(f"  Database    : {DB_PATH}")
    print(f"  Keep if conf >= {args.min_confidence}; else dismiss + severity {DISMISS_SEVERITY}")
    print(f"  Scope       : {', '.join(statuses) if statuses else 'ALL statuses'}")
    print(f"  Mode        : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    run(commit=args.commit, statuses=statuses, threshold=args.min_confidence)


if __name__ == "__main__":
    main()
