"""
violation_breakdown.py

Extracts the same Violation Breakdown shown in the UI (/stats/violations):
distinct sessions per violation category, over sessions whose overall_verdict
is not CLEAN. Read-only.

Prints three breakdowns:
  1. Overall                          (same numbers as the UI panel)
  2. astrotalk_flagged = 0            (AstroTalk did NOT flag the session)
  3. astrotalk_flagged = 1            (AstroTalk DID flag the session)

Counts are DISTINCT sessions, so a session with the same category on multiple
turns counts once. A session with several different categories appears once
under each of them, so column totals can exceed the number of sessions.

Usage:
  python scripts/violation_breakdown.py
  python scripts/violation_breakdown.py --out exports/violation_breakdown.csv
"""

import argparse
import csv
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import get_connection, DB_PATH  # noqa: E402


# Same base query as GET /stats/violations, with per-astrotalk_flagged splits.
BREAKDOWN_SQL = """
    SELECT
        f.category_code,
        COUNT(DISTINCT f.session_id) AS overall,
        COUNT(DISTINCT CASE WHEN s.astrotalk_flagged = 0 THEN f.session_id END) AS astro_0,
        COUNT(DISTINCT CASE WHEN s.astrotalk_flagged = 1 THEN f.session_id END) AS astro_1
    FROM flags f
    JOIN sessions s ON s.session_id = f.session_id
    WHERE s.overall_verdict != 'CLEAN'
    GROUP BY f.category_code
    ORDER BY overall DESC
"""

SESSION_TOTALS_SQL = """
    SELECT
        COUNT(DISTINCT f.session_id) AS overall,
        COUNT(DISTINCT CASE WHEN s.astrotalk_flagged = 0 THEN f.session_id END) AS astro_0,
        COUNT(DISTINCT CASE WHEN s.astrotalk_flagged = 1 THEN f.session_id END) AS astro_1
    FROM flags f
    JOIN sessions s ON s.session_id = f.session_id
    WHERE s.overall_verdict != 'CLEAN'
"""


def _print_table(title, rows, col):
    print("-" * 64)
    print(f"  {title}")
    print("-" * 64)
    shown = [(r["category_code"], r[col]) for r in rows if r[col] > 0]
    if not shown:
        print("  (no violation data)")
        print()
        return
    for cat, n in shown:
        print(f"  {cat:<40} {n:>8,}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Violation breakdown by astrotalk_flagged.")
    parser.add_argument("--out", help="Optional CSV output path.")
    args = parser.parse_args()

    conn = get_connection()
    rows = conn.execute(BREAKDOWN_SQL).fetchall()
    totals = conn.execute(SESSION_TOTALS_SQL).fetchone()
    conn.close()

    print("=" * 64)
    print("  Violation Breakdown (distinct sessions per category)")
    print(f"  DB: {DB_PATH}")
    print("=" * 64)
    print(f"  Sessions with violations (overall)           : {totals['overall']:>8,}")
    print(f"  ... of which astrotalk_flagged = 0           : {totals['astro_0']:>8,}")
    print(f"  ... of which astrotalk_flagged = 1           : {totals['astro_1']:>8,}")
    print()

    _print_table("Overall (matches the UI Violation Breakdown)", rows, "overall")
    _print_table("astrotalk_flagged = 0  (AstroTalk did NOT flag)", rows, "astro_0")
    _print_table("astrotalk_flagged = 1  (AstroTalk DID flag)", rows, "astro_1")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["category_code", "overall", "astrotalk_flagged_0", "astrotalk_flagged_1"])
            for r in rows:
                writer.writerow([r["category_code"], r["overall"], r["astro_0"], r["astro_1"]])
        print(f"  CSV written: {out_path}")


if __name__ == "__main__":
    main()
