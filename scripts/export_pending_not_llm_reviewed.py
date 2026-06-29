"""
export_pending_not_llm_reviewed.py

Read-only. Finds sessions still PENDING that the LLM has NOT reviewed and
exports their full chat to a CSV in the LLM-pipeline input format
(session_id, message_seq, sender, message_text, is_automated_message), so the
file can be fed straight into batch.py / moderate.py.

A session counts as LLM-reviewed if it has any flag with source='LLM' OR
submitted_by='LLM' (clean sessions the LLM auto-submitted). So the target set:

    review_status = 'PENDING'
    AND COALESCE(submitted_by,'') <> 'LLM'
    AND session_id NOT IN (SELECT session_id FROM flags WHERE source = 'LLM')

--dry-run prints the counts (total PENDING + PENDING-not-LLM-reviewed) without
writing the CSV.

Usage:
  python scripts/export_pending_not_llm_reviewed.py
  python scripts/export_pending_not_llm_reviewed.py --dry-run
  python scripts/export_pending_not_llm_reviewed.py --out C:/path/file.csv
"""

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import get_connection  # noqa: E402

CSV_COLUMNS = ["session_id", "message_seq", "sender", "message_text", "is_automated_message"]

# Subquery defining "PENDING and not reviewed by the LLM".
_TARGET_SUBQUERY = """
    SELECT session_id FROM sessions
    WHERE review_status = 'PENDING'
      AND COALESCE(submitted_by, '') <> 'LLM'
      AND session_id NOT IN (SELECT session_id FROM flags WHERE source = 'LLM')
"""


def export(out_path: Path, dry_run: bool = False) -> None:
    conn = get_connection()
    try:
        total_pending = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE review_status = 'PENDING'"
        ).fetchone()[0]
        target_count = conn.execute(
            f"SELECT COUNT(*) FROM ({_TARGET_SUBQUERY})"
        ).fetchone()[0]

        print(f"  Total PENDING sessions       : {total_pending}")
        print(f"  PENDING & not LLM reviewed   : {target_count}")

        if dry_run:
            print("\nDRY RUN — no CSV written.")
            return

        if target_count == 0:
            print("\nNothing to export.")
            return

        # Use a subquery (not an IN list) so we never hit SQLite's variable cap.
        rows = conn.execute(
            f"""SELECT session_id, turn_id, speaker, message_text, is_automated
                FROM turns
                WHERE session_id IN ({_TARGET_SUBQUERY})
                ORDER BY session_id, turn_id"""
        ).fetchall()

        out_path.parent.mkdir(parents=True, exist_ok=True)
        n_turns = 0
        with out_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for t in rows:
                writer.writerow({
                    "session_id":           t["session_id"],
                    "message_seq":          t["turn_id"],
                    "sender":               t["speaker"],
                    "message_text":         t["message_text"],
                    "is_automated_message": t["is_automated"],
                })
                n_turns += 1

        print(f"  Turns written                : {n_turns}")
        print(f"  CSV written                  : {out_path}")
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(
        description="Export PENDING sessions not yet reviewed by the LLM (pipeline-input CSV)"
    )
    p.add_argument("--out", default=None, help="Output CSV path")
    p.add_argument("--dry-run", action="store_true",
                   help="Print counts only (total PENDING + not-LLM-reviewed); write nothing.")
    args = p.parse_args()

    if args.out:
        out_path = Path(args.out)
    else:
        stamp = datetime.now().strftime("%Y%m%d")
        out_path = Path(__file__).resolve().parents[1] / "exports" / f"pending_not_llm_reviewed_{stamp}.csv"

    print("=" * 60)
    print("  Export PENDING sessions not reviewed by the LLM")
    print(f"  DB  : {os.getenv('DB_PATH', 'store/results.db')}")
    print(f"  Mode: {'DRY-RUN' if args.dry_run else 'COMMIT'}")
    print("=" * 60)

    export(out_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
