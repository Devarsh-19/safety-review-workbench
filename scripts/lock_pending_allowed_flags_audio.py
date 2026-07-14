"""
lock_pending_allowed_flags_audio.py

Audio counterpart of scripts/lock_pending_allowed_flags.py — operates on the
separate audio review database (store/audio_db.get_audio_connection), not the
chat DB.

Lock PENDING audio sessions whose flags fall ENTIRELY within an allowed set of
intents. A session is locked only when:

  * its review_status is 'PENDING', AND
  * it has at least one active flag, AND
  * EVERY active flag's intent is in ALLOWED_CATEGORIES (no out-of-list flag).

Any single flag outside the allowed set disqualifies the whole session — e.g.
{FEAR_MANIPULATION, HATE_SPEECH} locks, but {HATE_SPEECH, NSFW_EXPLICIT} does
not (NSFW_EXPLICIT is not in the list), and a session with no flags is left as is.

"Active flag" matches the review app (store/audio_db flag_count): the amendment
row if a flag was edited, else the original; DISMISSED rows are ignored. Locking
mirrors store/audio_db.lock_audio_session: review_status='LOCKED',
locked_by=<name>, locked_at=now.

DRY RUN BY DEFAULT — prints what would happen and changes nothing. Pass --apply
to actually lock and commit.

Usage:
  python scripts/lock_pending_allowed_flags_audio.py                 # dry run
  python scripts/lock_pending_allowed_flags_audio.py --apply         # perform the locks
  python scripts/lock_pending_allowed_flags_audio.py --locked-by Amogh --apply
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import get_audio_connection, AUDIO_DB_PATH  # noqa: E402

# Intents that are allowed to appear on a lockable session — kept in sync with
# the chat script's ALLOWED_CATEGORIES. Stored intents are uppercase with
# underscores; normalise() makes the comparison tolerant of spaces/hyphens/case.
# Some codes (e.g. PERSONAL_DATA_COLLECTION, COMPETITOR_PROMOTION) may have no
# flags in this DB yet; that is harmless.
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


def active_intents_by_pending_session(conn) -> dict[int, list[str]]:
    """Per PENDING session, the list of ACTIVE flag intents (normalised).

    Active = amendment row if a flag was edited, else the original; DISMISSED
    rows are ignored. Same rule as store/audio_db's flag_count column.
    """
    rows = conn.execute(
        """SELECT f.flag_id, f.parent_flag_id, f.s_id, f.intent, f.status
           FROM audio_flags f
           JOIN audio_sessions s ON s.s_id = f.s_id
           WHERE s.review_status = 'PENDING'"""
    ).fetchall()

    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}

    intents: dict[int, list[str]] = defaultdict(list)
    for r in rows:
        is_active = r["parent_flag_id"] is not None or r["flag_id"] not in amended_parents
        if not is_active or (r["status"] or "") == "DISMISSED":
            continue
        intents[r["s_id"]].append(normalise(r["intent"]))
    return intents


def classify(intents_by_session: dict[int, list[str]]) -> tuple[list[int], list[tuple[int, set[str]]]]:
    """Split pending-with-flags sessions into (lockable, skipped).

    lockable: s_ids where every active flag is in ALLOWED.
    skipped : (s_id, disqualifying_intents) for the rest.
    """
    lockable: list[int] = []
    skipped: list[tuple[int, set[str]]] = []
    for sid, intents in intents_by_session.items():
        if not intents:
            continue  # no active flags -> leave as is
        out_of_list = {c for c in intents if c not in ALLOWED}
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

    conn = get_audio_connection()
    try:
        intents_by_session = active_intents_by_pending_session(conn)
        lockable, skipped = classify(intents_by_session)

        print("=" * 66)
        print("  Lock PENDING audio sessions whose flags are all in the allowed set")
        print("=" * 66)
        print(f"  Database : {AUDIO_DB_PATH}")
        print(f"  Allowed intents : {', '.join(sorted(ALLOWED))}")
        print(f"  Pending sessions w/ flags : {len(intents_by_session)}")
        print(f"  Lockable (all flags allowed): {len(lockable)}")
        print(f"  Skipped  (has out-of-list flag): {len(skipped)}")
        print()

        if lockable:
            print("  WOULD LOCK:")
            for sid in lockable:
                print(f"    {str(sid):<16} flags={sorted(set(intents_by_session[sid]))}")
        else:
            print("  WOULD LOCK: (none)")
        print()

        if skipped:
            print("  SKIPPED (left as is - has a flag outside the allowed set):")
            for sid, bad in skipped:
                print(f"    {str(sid):<16} disqualifying={sorted(bad)}  all={sorted(set(intents_by_session[sid]))}")
        print()

        if not args.apply:
            print("  DRY RUN - nothing changed. Re-run with --apply to lock the above.")
            return

        if not lockable:
            print("  Nothing to lock.")
            return

        placeholders = ",".join("?" for _ in lockable)
        conn.execute(
            f"""UPDATE audio_sessions
                SET review_status = 'LOCKED',
                    locked_by     = ?,
                    locked_at     = datetime('now')
                WHERE s_id IN ({placeholders})""",
            [args.locked_by, *lockable],
        )
        conn.commit()
        print(f"  LOCKED {len(lockable)} audio session(s) as '{args.locked_by}'.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
