"""
auto_process_clean_sessions.py

Auto-submits and auto-locks ALL sessions (both Chat and Audio) that are
fully CLEAN (i.e. clean by both LLM and Astrotalk). 

This script targets sessions that are currently PENDING or SUBMITTED_FOR_REVIEW,
and updates them to LOCKED, setting both the submit and lock audit fields.

With --flagged, the Chat pass ALSO locks LLM-clean sessions that Astrotalk
flagged (astrotalk_flagged = 1) — the LLM found them clean, so Astrotalk's flag
is auto-cleared. Those rows get a distinct reviewer_note. Without --flagged only
clean-by-both sessions (astrotalk_flagged = 0) are locked. --flagged affects the
Chat pass only; the Audio pass always requires clean-by-both.

Usage:
  python scripts/auto_process_clean_sessions.py            # dry-run, clean-by-both
  python scripts/auto_process_clean_sessions.py --commit   # apply changes
  python scripts/auto_process_clean_sessions.py --flagged            # dry-run, also Astrotalk-flagged chat
  python scripts/auto_process_clean_sessions.py --flagged --commit   # apply, also Astrotalk-flagged chat
"""

import argparse
import sqlite3
import sys
from pathlib import Path
from collections import Counter

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import DB_PATH
from store.audio_db import AUDIO_DB_PATH

LOCKED_BY = "AUTO_LOCK"
SUBMITTED_BY = "AUTO_LOCK"

CLEAN_NOTE = "Auto-locked: clean by both LLM and Astrotalk"
FLAGGED_NOTE = "Auto-locked: LLM clean, Astrotalk flagged (auto-cleared)"


def process_chat_db(commit: bool = False, include_flagged: bool = False):
    print(f"\n--- Processing Chat DB ({DB_PATH}) ---")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # Always LLM-clean and in a lockable status. Default: only Astrotalk-clean
        # (astrotalk_flagged = 0). With --flagged, also include Astrotalk-flagged
        # sessions (astrotalk_flagged = 1); NULL (unknown) stays excluded either way.
        astrotalk_clause = "astrotalk_flagged IN (0, 1)" if include_flagged else "astrotalk_flagged = 0"
        rows = conn.execute(f"""
            SELECT session_id, review_status, astrotalk_flagged
            FROM sessions
            WHERE overall_verdict = 'CLEAN'
              AND {astrotalk_clause}
              AND review_status IN ('PENDING', 'SUBMITTED_FOR_REVIEW')
        """).fetchall()

        total = len(rows)
        if total == 0:
            print("  No eligible Chat sessions found.")
            return

        clean_n = sum(1 for r in rows if r["astrotalk_flagged"] == 0)
        flagged_n = total - clean_n
        status_counts = Counter(r["review_status"] for r in rows)
        label = ("LLM-clean (Astrotalk clean or flagged)" if include_flagged
                 else "clean by both LLM and Astrotalk")
        print(f"  Found {total} eligible Chat session(s) [{label}]:")
        for status, count in status_counts.items():
            print(f"    {status:<22} : {count}")
        if include_flagged:
            print(f"    (Astrotalk clean: {clean_n}, Astrotalk flagged: {flagged_n})")

        if not commit:
            print("  DRY RUN: no changes written.")
            return

        # Per-row note reflects whether Astrotalk had flagged the session.
        conn.executemany("""
            UPDATE sessions
            SET review_status = 'LOCKED',
                locked_by = ?,
                locked_at = datetime('now'),
                submitted_by = COALESCE(submitted_by, ?),
                submitted_at = COALESCE(submitted_at, datetime('now')),
                reviewer_id = ?,
                reviewer_note = ?,
                reviewed_at = datetime('now')
            WHERE session_id = ?
        """, [
            (LOCKED_BY, SUBMITTED_BY, LOCKED_BY,
             FLAGGED_NOTE if r["astrotalk_flagged"] == 1 else CLEAN_NOTE,
             r["session_id"])
            for r in rows
        ])
        conn.commit()
        print(f"  COMMIT: Locked {total} Chat session(s) "
              f"({clean_n} Astrotalk-clean, {flagged_n} Astrotalk-flagged).")
    finally:
        conn.close()

def process_audio_db(commit: bool = False):
    print(f"\n--- Processing Audio DB ({AUDIO_DB_PATH}) ---")
    conn = sqlite3.connect(AUDIO_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT s_id, review_status
            FROM audio_sessions
            WHERE overall_verdict = 'CLEAN'
              AND astrotalk_verdict = 'CLEAN'
              AND review_status IN ('PENDING', 'SUBMITTED_FOR_REVIEW')
        """).fetchall()
        
        total = len(rows)
        if total == 0:
            print("  No eligible Audio sessions found.")
            return

        status_counts = Counter(r["review_status"] for r in rows)
        print(f"  Found {total} eligible Audio session(s) clean by both LLM and Astrotalk:")
        for status, count in status_counts.items():
            print(f"    {status:<22} : {count}")
            
        if not commit:
            print("  DRY RUN: no changes written.")
            return
            
        conn.executemany("""
            UPDATE audio_sessions
            SET review_status = 'LOCKED',
                locked_by = ?,
                locked_at = datetime('now'),
                submitted_by = COALESCE(submitted_by, ?),
                submitted_at = COALESCE(submitted_at, datetime('now')),
                reviewer_id = ?,
                reviewer_note = 'Auto-locked: clean by both LLM and Astrotalk',
                reviewed_at = datetime('now')
            WHERE s_id = ?
        """, [(LOCKED_BY, SUBMITTED_BY, LOCKED_BY, r["s_id"]) for r in rows])
        conn.commit()
        print(f"  COMMIT: Locked {total} Audio session(s).")
    finally:
        conn.close()

def main():
    parser = argparse.ArgumentParser(
        description="Auto-submit and auto-lock sessions that are clean by both LLM and Astrotalk."
    )
    parser.add_argument("--commit", action="store_true", help="Apply changes")
    parser.add_argument("--flagged", action="store_true",
                        help="Also lock LLM-clean Chat sessions that Astrotalk flagged "
                             "(astrotalk_flagged = 1), not just the clean-by-both ones.")
    args = parser.parse_args()

    print("=" * 60)
    print("  Auto-submit and Auto-lock Fully Clean Sessions")
    print(f"  Mode: {'COMMIT' if args.commit else 'DRY-RUN'}"
          f"{'  |  including Astrotalk-flagged chat' if args.flagged else ''}")
    print("=" * 60)

    process_chat_db(commit=args.commit, include_flagged=args.flagged)
    process_audio_db(commit=args.commit)

if __name__ == "__main__":
    main()
