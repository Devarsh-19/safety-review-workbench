"""
confirm_abusive_language_locked_audio.py

For the audio review database: in sessions that are already LOCKED, confirm any
active ABUSIVE_LANGUAGE flag that has not yet been confirmed.

"Active flag" means the amendment row if a flag was edited, otherwise the
original row. DISMISSED rows are ignored (a dismissed flag is a reviewer's
decision and is never re-confirmed). Among active ABUSIVE_LANGUAGE flags, only
those whose status is not already CONFIRMED are updated to
status = 'CONFIRMED' with confirmed_by / confirmed_at set and a 'CONFIRM_FLAG'
row written to audio_review_log.

The sessions stay LOCKED — this only fills in flag-level confirmation. Session
verdicts are unaffected: the stored verdict is computed from active
(non-dismissed) flags regardless of their confirmed state, and confirming an
already-active flag does not change which flags are active.

DRY-RUN BY DEFAULT — running with no flags only previews what would change.
Pass --commit to apply.

Usage:
  python scripts/confirm_abusive_language_locked_audio.py
  python scripts/confirm_abusive_language_locked_audio.py --commit
  python scripts/confirm_abusive_language_locked_audio.py --confirmed-by Amogh --commit
"""

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import AUDIO_DB_PATH, get_audio_connection  # noqa: E402


TARGET_INTENT = "ABUSIVE_LANGUAGE"
REVIEWER_ID = "AUTO_CONFIRM_ABUSIVE"
NOTE = "Auto-confirm: abusive-language flag on a locked session"
SAMPLE_LIMIT = 30


def normalise(value: str) -> str:
    return (value or "").strip().upper().replace("-", "_").replace(" ", "_")


def active_audio_flag_rows(rows) -> list:
    """Amendment rows + original rows that were never amended (chat parity)."""
    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}
    return [
        r for r in rows
        if r["parent_flag_id"] is not None or r["flag_id"] not in amended_parents
    ]


def fetch_locked_flag_rows(conn):
    return conn.execute(
        """
        SELECT f.flag_id, f.s_id, f.parent_flag_id, f.intent, f.status
        FROM audio_flags f
        JOIN audio_sessions s ON s.s_id = f.s_id
        WHERE s.review_status = 'LOCKED'
        ORDER BY f.s_id, f.flag_id
        """
    ).fetchall()


def find_targets(conn):
    rows_by_sid = defaultdict(list)
    for row in fetch_locked_flag_rows(conn):
        rows_by_sid[row["s_id"]].append(row)

    targets = []
    skipped = Counter()

    for s_id, rows in rows_by_sid.items():
        active = active_audio_flag_rows(rows)

        # Active, non-dismissed abusive-language flags that are not yet confirmed.
        to_confirm = [
            r for r in active
            if normalise(r["intent"]) == TARGET_INTENT
            and (r["status"] or "") not in ("CONFIRMED", "DISMISSED")
        ]
        if not to_confirm:
            skipped["no_unconfirmed_abusive_flag"] += 1
            continue

        targets.append({
            "s_id": s_id,
            "to_confirm_ids": [r["flag_id"] for r in to_confirm],
        })

    targets.sort(key=lambda t: t["s_id"])
    return targets, skipped


def print_preview(targets, skipped: Counter) -> None:
    to_confirm_total = sum(len(t["to_confirm_ids"]) for t in targets)
    print(f"Found {len(targets):,} locked session(s) with unconfirmed "
          f"abusive-language flags.")
    print(f"  Abusive-language flags to confirm : {to_confirm_total:,}")
    print()
    if targets:
        print(f"  {'SESSION':<12} {'FLAGS TO CONFIRM':>16}")
        print(f"  {'-'*12} {'-'*16}")
        for t in targets[:SAMPLE_LIMIT]:
            print(f"  {t['s_id']:<12} {len(t['to_confirm_ids']):>16}")
        if len(targets) > SAMPLE_LIMIT:
            print(f"  ... {len(targets) - SAMPLE_LIMIT:,} more")
        print()

    print("  Skipped session counts:")
    for key in ("no_unconfirmed_abusive_flag",):
        print(f"    {key:<32} {skipped[key]:>6,}")
    print()


def apply_changes(conn, targets, confirmed_by: str) -> None:
    for target in targets:
        for flag_id in target["to_confirm_ids"]:
            conn.execute(
                """UPDATE audio_flags
                   SET status = 'CONFIRMED',
                       confirmed_by = ?,
                       confirmed_at = datetime('now')
                   WHERE flag_id = ?""",
                (confirmed_by, flag_id),
            )
            conn.execute(
                """INSERT INTO audio_review_log (s_id, flag_id, action, reviewer_id, note)
                   VALUES (?, ?, 'CONFIRM_FLAG', ?, ?)""",
                (target["s_id"], flag_id, confirmed_by, NOTE),
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Confirm unconfirmed abusive-language flags on locked audio sessions."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually confirm (default is dry-run preview only).")
    parser.add_argument("--confirmed-by", default=REVIEWER_ID,
                        help=f"Reviewer id/name for confirmed_by (default: {REVIEWER_ID}).")
    args = parser.parse_args()

    print("=" * 78)
    print("  Confirm abusive-language flags on locked audio sessions")
    print("=" * 78)
    print(f"  Database     : {AUDIO_DB_PATH}")
    print(f"  Confirmed by : {args.confirmed_by}")
    print(f"  Mode         : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    conn = get_audio_connection()
    try:
        targets, skipped = find_targets(conn)
        print_preview(targets, skipped)

        if not args.commit:
            print(f"DRY RUN — would confirm flags in {len(targets):,} locked session(s). "
                  "No changes written. Re-run with --commit to apply.")
            return

        if not targets:
            print("Nothing to confirm.")
            return

        with conn:
            apply_changes(conn, targets, args.confirmed_by)
        print(f"Done. Confirmed abusive-language flags in {len(targets):,} locked session(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
