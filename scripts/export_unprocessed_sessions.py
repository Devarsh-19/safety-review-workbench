"""
export_unprocessed_sessions.py

Read-only. Pulls the sessions whose VERDICT is unprocessed straight from the DB
(no input CSV). A session's verdict is "unprocessed" when overall_verdict has
not been assigned yet — NULL or blank. (Assigned verdicts are CLEAN / FLAGGED /
SEVERE.)

    Target: overall_verdict IS NULL OR TRIM(overall_verdict) = ''

  * with --out   : writes those sessions' full chat in the LLM-pipeline INPUT
                   CSV format (session_id, message_seq, sender, message_text,
                   is_automated_message) — ready to feed straight into
                   ingest_llm_sessions.py / the batch runner.
  * without --out : prints the distinct unprocessed-verdict session count only.

Usage:
  # distinct count only
  python scripts/export_unprocessed_sessions.py

  # export the sessions in input CSV format
  python scripts/export_unprocessed_sessions.py --out exports/unprocessed.csv
"""

import argparse
import os
import sys
import csv
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import get_connection  # noqa: E402

# Input CSV format expected by ingest_llm_sessions.py / the batch runner.
CSV_COLUMNS = ["session_id", "message_seq", "sender", "message_text", "is_automated_message"]

# Subquery defining "verdict is unprocessed" (no verdict assigned yet).
_TARGET_SUBQUERY = """
    SELECT session_id FROM sessions
    WHERE overall_verdict IS NULL OR TRIM(overall_verdict) = ''
"""


def run(out_path: Path | None) -> None:
    conn = get_connection()
    try:
        n_target = conn.execute(
            f"SELECT COUNT(*) FROM ({_TARGET_SUBQUERY})"
        ).fetchone()[0]

        print(f"  Sessions with unprocessed verdict: {n_target}")

        if out_path is None:
            # Count-only mode.
            print(f"\n  UNPROCESSED-VERDICT SESSION COUNT: {n_target}")
            return

        if n_target == 0:
            print("\n  Nothing to export.")
            return

        # Pull every turn for the target sessions, in input CSV format.
        # Subquery (not an IN list) keeps us clear of SQLite's variable cap.
        rows = conn.execute(
            f"""SELECT session_id, turn_id, speaker, message_text, is_automated
                FROM turns
                WHERE session_id IN ({_TARGET_SUBQUERY})
                ORDER BY session_id, turn_id"""
        ).fetchall()

        out_path.parent.mkdir(parents=True, exist_ok=True)
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

        print(f"  Turns written                    : {len(rows)}")
        print(f"  CSV written                      : {out_path}")
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(
        description="Export (or count) sessions whose verdict is unprocessed, in input CSV format"
    )
    p.add_argument("--out", default=None,
                   help="Output CSV path. If given, sessions are written in input CSV "
                        "format. If omitted, only the distinct count is printed.")
    args = p.parse_args()

    out_path = Path(args.out) if args.out else None

    print("=" * 60)
    print("  Unprocessed-verdict sessions " + ("(EXPORT)" if out_path else "(COUNT ONLY)"))
    print(f"  DB    : {os.getenv('DB_PATH', 'store/results.db')}")
    print("=" * 60)

    run(out_path)


if __name__ == "__main__":
    main()
