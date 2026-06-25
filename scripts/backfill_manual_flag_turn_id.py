"""
backfill_manual_flag_turn_id.py

Backfills turn_id on existing MANUAL flags that have turn_id = NULL, by matching
each flag's pattern_matched text against the message_text of turns in the same
session (the same substring logic the UI uses to associate a flag to a turn).

Usage:
  python scripts/backfill_manual_flag_turn_id.py --dry-run
  python scripts/backfill_manual_flag_turn_id.py
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import get_connection  # noqa: E402


def _match_turn(pattern: str, turns: list) -> int | None:
    """Return the turn_id whose message_text matches pattern, or None."""
    pm = (pattern or "").strip().lower()
    if len(pm) < 4:
        return None
    for t in turns:
        tm = (t["message_text"] or "").strip().lower()
        if not tm:
            continue
        if tm in pm or pm in tm:        # short msg == pm, or pm contained in long msg
            return t["turn_id"]
    return None


def backfill(dry_run: bool = False) -> None:
    conn = get_connection()

    flags = conn.execute(
        """SELECT flag_id, session_id, pattern_matched
           FROM flags
           WHERE source = 'MANUAL' AND turn_id IS NULL"""
    ).fetchall()

    print(f"Found {len(flags)} MANUAL flags with NULL turn_id")
    if not flags:
        conn.close()
        return

    # Cache turns per session
    turns_by_session: dict[str, list] = {}

    updates: list[tuple[int, int]] = []   # (turn_id, flag_id)
    unmatched = 0
    for f in flags:
        sid = f["session_id"]
        if sid not in turns_by_session:
            turns_by_session[sid] = conn.execute(
                "SELECT turn_id, message_text FROM turns WHERE session_id = ? ORDER BY turn_id",
                (sid,),
            ).fetchall()
        tid = _match_turn(f["pattern_matched"], turns_by_session[sid])
        if tid is not None:
            updates.append((tid, f["flag_id"]))
        else:
            unmatched += 1

    print(f"  Matched   : {len(updates)}")
    print(f"  Unmatched : {unmatched} (no turn text overlap — left as NULL)")

    if dry_run:
        print("\nDRY RUN — no changes written.")
        for tid, fid in updates[:20]:
            print(f"    flag {fid} -> turn_id {tid}")
        if len(updates) > 20:
            print(f"    ... and {len(updates) - 20} more")
        conn.close()
        return

    conn.executemany(
        "UPDATE flags SET turn_id = ? WHERE flag_id = ?",
        updates,
    )
    conn.commit()
    print(f"\nDone. Updated {len(updates)} flags.")
    conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Backfill turn_id on MANUAL flags")
    p.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = p.parse_args()
    print("=" * 55)
    print("  Backfill MANUAL flag turn_id")
    print(f"  DB: {os.getenv('DB_PATH', 'store/astrotalk.db')}")
    print("=" * 55)
    backfill(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
