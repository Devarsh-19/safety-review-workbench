"""
auto_process_clean_sessions.py

Auto-submits and auto-locks ALL sessions (both Chat and Audio) that are
fully CLEAN (i.e. clean by both LLM and Astrotalk). 

This script targets sessions that are currently PENDING or SUBMITTED_FOR_REVIEW,
and updates them to LOCKED, setting both the submit and lock audit fields.

Usage:
  python scripts/auto_process_clean_sessions.py            # dry-run
  python scripts/auto_process_clean_sessions.py --commit   # apply changes
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

def process_chat_db(commit: bool = False):
    print(f"\n--- Processing Chat DB ({DB_PATH}) ---")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT session_id, review_status
            FROM sessions
            WHERE overall_verdict = 'CLEAN'
              AND astrotalk_flagged = 0
              AND review_status IN ('PENDING', 'SUBMITTED_FOR_REVIEW')
        """).fetchall()
        
        total = len(rows)
        if total == 0:
            print("  No eligible Chat sessions found.")
            return

        status_counts = Counter(r["review_status"] for r in rows)
        print(f"  Found {total} eligible Chat session(s) clean by both LLM and Astrotalk:")
        for status, count in status_counts.items():
            print(f"    {status:<22} : {count}")
            
        if not commit:
            print("  DRY RUN: no changes written.")
            return
            
        conn.executemany("""
            UPDATE sessions
            SET review_status = 'LOCKED',
                locked_by = ?,
                locked_at = datetime('now'),
                submitted_by = COALESCE(submitted_by, ?),
                submitted_at = COALESCE(submitted_at, datetime('now')),
                reviewer_id = ?,
                reviewer_note = 'Auto-locked: clean by both LLM and Astrotalk',
                reviewed_at = datetime('now')
            WHERE session_id = ?
        """, [(LOCKED_BY, SUBMITTED_BY, LOCKED_BY, r["session_id"]) for r in rows])
        conn.commit()
        print(f"  COMMIT: Locked {total} Chat session(s).")
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
    args = parser.parse_args()

    print("=" * 60)
    print("  Auto-submit and Auto-lock Fully Clean Sessions")
    print(f"  Mode: {'COMMIT' if args.commit else 'DRY-RUN'}")
    print("=" * 60)

    process_chat_db(commit=args.commit)
    process_audio_db(commit=args.commit)

if __name__ == "__main__":
    main()
