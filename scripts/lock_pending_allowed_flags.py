"""
lock_pending_allowed_flags.py

Lock PENDING chat sessions whose flags fall ENTIRELY within an allowed set of
categories. A session is locked only when:

  * its review_status is 'PENDING', AND
  * it has at least one active flag, AND
  * EVERY active flag's category is in ALLOWED_CATEGORIES (no out-of-list flag).

Any single flag outside the allowed set disqualifies the whole session — e.g.
{FEAR_MANIPULATION, HATE_SPEECH} locks, but {HATE_SPEECH, NSFW_EXPLICIT} does
not (NSFW_EXPLICIT is not in the list), and a session with no flags is left as is.

"Active flag" matches the review app: the amendment row if a flag was edited,
else the original; DISMISSED rows are ignored. Locking mirrors store/db.py
lock_session: review_status='LOCKED', locked_by=<name>, locked_at=now.

DRY RUN BY DEFAULT — prints what would happen and changes nothing. Pass --apply
to actually lock and commit.

Usage:
  python scripts/lock_pending_allowed_flags.py                 # dry run
  python scripts/lock_pending_allowed_flags.py --apply         # perform the locks
  python scripts/lock_pending_allowed_flags.py --locked-by Amogh --apply
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import get_connection  # noqa: E402

# Categories that are allowed to appear on a lockable session, as the user
# specified. Stored codes are uppercase with underscores; normalise() makes the
# comparison tolerant of spaces/hyphens/case. PERSONAL_DATA_COLLECTION and
# COMPETITOR_PROMOTION are valid taxonomy codes with no flags in this DB yet.
ALLOWED_CATEGORIES = {
    "HATE_SPEECH",
    "FAKE_REMEDIES",
    "FINANCIAL_SOLICITATION",
    "IDENTITY_FRAUD",
    "INSTIGATION",
    "OFF_PLATFORM_SOLICITATION",
    "RE_ENGAGEMENT_SOLICITATION",
    "UNAUTHORIZED_MEDICAL_ADVICE",
    "PERSONAL_DATA_COLLECTION",
    "FEAR_MANIPULATION",
    "COMPETITOR_PROMOTION",
}


def normalise(code: str) -> str:
    return (code or "").strip().upper().replace("-", "_").replace(" ", "_")


ALLOWED = {normalise(c) for c in ALLOWED_CATEGORIES}


def active_categories_by_pending_session(conn) -> dict[str, list[str]]:
    """Per PENDING session, the list of ACTIVE flag categories (normalised).

    Active = amendment row if a flag was edited, else the original; DISMISSED
    rows are ignored. Same rule as export_all_ingested_sessions.py.
    """
    rows = conn.execute(
        """SELECT f.flag_id, f.parent_flag_id, f.session_id, f.category_code, f.status
           FROM flags f
           JOIN sessions s ON s.session_id = f.session_id
           WHERE s.review_status = 'PENDING'"""
    ).fetchall()

    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}

    cats: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        is_active = r["parent_flag_id"] is not None or r["flag_id"] not in amended_parents
        if not is_active or (r["status"] or "") == "DISMISSED":
            continue
        cats[r["session_id"]].append(normalise(r["category_code"]))
    return cats


def classify(cats_by_session: dict[str, list[str]]) -> tuple[list[str], list[tuple[str, set[str]]]]:
    """Split pending-with-flags sessions into (lockable, skipped).

    lockable: session_ids where every active flag is in ALLOWED.
    skipped : (session_id, disqualifying_categories) for the rest.
    """
    lockable: list[str] = []
    skipped: list[tuple[str, set[str]]] = []
    for sid, cats in cats_by_session.items():
        if not cats:
            continue  # no active flags -> leave as is
        out_of_list = {c for c in cats if c not in ALLOWED}
        if out_of_list:
            skipped.append((sid, out_of_list))
        else:
            lockable.append(sid)
    return sorted(lockable), sorted(skipped)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--locked-by", default="Amogh", help="Name to record in locked_by (default: Amogh).")
    ap.add_argument("--apply", action="store_true",
                    help="Actually lock the sessions and commit. Without it, dry run only.")
    args = ap.parse_args()

    conn = get_connection()
    try:
        cats_by_session = active_categories_by_pending_session(conn)
        lockable, skipped = classify(cats_by_session)

        print("=" * 66)
        print("  Lock PENDING sessions whose flags are all in the allowed set")
        print("=" * 66)
        print(f"  Allowed categories : {', '.join(sorted(ALLOWED))}")
        print(f"  Pending sessions w/ flags : {len(cats_by_session)}")
        print(f"  Lockable (all flags allowed): {len(lockable)}")
        print(f"  Skipped  (has out-of-list flag): {len(skipped)}")
        print()

        if lockable:
            print("  WOULD LOCK:")
            for sid in lockable:
                print(f"    {sid:<16} flags={sorted(set(cats_by_session[sid]))}")
        else:
            print("  WOULD LOCK: (none)")
        print()

        if skipped:
            print("  SKIPPED (left as is - has a flag outside the allowed set):")
            for sid, bad in skipped:
                print(f"    {sid:<16} disqualifying={sorted(bad)}  all={sorted(set(cats_by_session[sid]))}")
        print()

        if not args.apply:
            print("  DRY RUN - nothing changed. Re-run with --apply to lock the above.")
            return

        if not lockable:
            print("  Nothing to lock.")
            return

        placeholders = ",".join("?" for _ in lockable)
        conn.execute(
            f"""UPDATE sessions
                SET review_status = 'LOCKED',
                    locked_by     = ?,
                    locked_at     = datetime('now')
                WHERE session_id IN ({placeholders})""",
            [args.locked_by, *lockable],
        )
        conn.commit()
        print(f"  LOCKED {len(lockable)} session(s) as '{args.locked_by}'.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
