"""
violation_breakdown.py

Extracts the same Violation Breakdown shown in the UI (/stats/violations):
distinct sessions per violation category, over sessions whose overall_verdict
is not CLEAN. Read-only.

Prints twelve breakdowns:
   1. Overall                          (same numbers as the UI panel)
   2. astrotalk_flagged = 0            (AstroTalk did NOT flag the session)
   3. astrotalk_flagged = 1            (AstroTalk DID flag the session)
   4. Violations by USER only          (flagged turns spoken only by the user)
   5. Violations by ASTROLOGER only    (flagged turns spoken only by the astrologer)
   6. Violations by BOTH               (flagged turns from both speakers)
   7. astrotalk_flagged = 0 x USER only
   8. astrotalk_flagged = 0 x ASTROLOGER only
   9. astrotalk_flagged = 0 x BOTH
  10. astrotalk_flagged = 1 x USER only
  11. astrotalk_flagged = 1 x ASTROLOGER only
  12. astrotalk_flagged = 1 x BOTH

Who committed a violation comes from the speaker of the flagged turn
(flags.turn_id -> turns.speaker). The speaker buckets are mutually
exclusive: a session goes to USER only, ASTROLOGER only, or BOTH, never
more than one, so USER only + ASTROLOGER only + BOTH never exceeds the
overall count. Sessions whose flags have no linked turn are unattributed
and appear only in the overall/astrotalk breakdowns.

Counts are DISTINCT sessions, so a session with the same category on multiple
turns counts once. A session with several different categories appears once
under each of them, so column totals can exceed the number of sessions.

Export (--out) writes everything shown on screen:
  .xlsx  -> two sheets: "Summary" (total sessions + sessions-with-violations
            for every section) and "Breakdown" (per-category table).
  .csv   -> two files: <name>.csv (breakdown) and <name>_summary.csv (totals).

Usage:
  python scripts/violation_breakdown.py
  python scripts/violation_breakdown.py --out exports/violation_breakdown.xlsx
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


# Same base query as GET /stats/violations, with per-astrotalk_flagged and
# per-speaker splits. Speaker comes from the turn the flag points at.
# The inner query collapses each (session, category) to one row with
# has_user/has_astrologer, so the speaker buckets below are mutually
# exclusive (USER only / ASTROLOGER only / BOTH) and each session counts once.
BREAKDOWN_SQL = """
    WITH sc AS (
        SELECT
            f.session_id,
            f.category_code,
            s.astrotalk_flagged,
            MAX(CASE WHEN t.speaker = 'USER'       THEN 1 ELSE 0 END) AS has_user,
            MAX(CASE WHEN t.speaker = 'ASTROLOGER' THEN 1 ELSE 0 END) AS has_astrologer
        FROM flags f
        JOIN sessions s ON s.session_id = f.session_id
        LEFT JOIN turns t ON t.session_id = f.session_id AND t.turn_id = f.turn_id
        WHERE s.overall_verdict != 'CLEAN'
        GROUP BY f.session_id, f.category_code
    )
    SELECT
        category_code,
        COUNT(*) AS overall,
        COUNT(CASE WHEN astrotalk_flagged = 0 THEN 1 END) AS astro_0,
        COUNT(CASE WHEN astrotalk_flagged = 1 THEN 1 END) AS astro_1,
        COUNT(CASE WHEN has_user = 1 AND has_astrologer = 0 THEN 1 END) AS by_user,
        COUNT(CASE WHEN has_user = 0 AND has_astrologer = 1 THEN 1 END) AS by_astrologer,
        COUNT(CASE WHEN has_user = 1 AND has_astrologer = 1 THEN 1 END) AS by_both,
        COUNT(CASE WHEN astrotalk_flagged = 0 AND has_user = 1 AND has_astrologer = 0 THEN 1 END) AS astro_0_user,
        COUNT(CASE WHEN astrotalk_flagged = 0 AND has_user = 0 AND has_astrologer = 1 THEN 1 END) AS astro_0_astrologer,
        COUNT(CASE WHEN astrotalk_flagged = 0 AND has_user = 1 AND has_astrologer = 1 THEN 1 END) AS astro_0_both,
        COUNT(CASE WHEN astrotalk_flagged = 1 AND has_user = 1 AND has_astrologer = 0 THEN 1 END) AS astro_1_user,
        COUNT(CASE WHEN astrotalk_flagged = 1 AND has_user = 0 AND has_astrologer = 1 THEN 1 END) AS astro_1_astrologer,
        COUNT(CASE WHEN astrotalk_flagged = 1 AND has_user = 1 AND has_astrologer = 1 THEN 1 END) AS astro_1_both
    FROM sc
    GROUP BY category_code
    ORDER BY overall DESC
"""

SESSION_TOTALS_SQL = """
    WITH ss AS (
        SELECT
            f.session_id,
            s.astrotalk_flagged,
            MAX(CASE WHEN t.speaker = 'USER'       THEN 1 ELSE 0 END) AS has_user,
            MAX(CASE WHEN t.speaker = 'ASTROLOGER' THEN 1 ELSE 0 END) AS has_astrologer
        FROM flags f
        JOIN sessions s ON s.session_id = f.session_id
        LEFT JOIN turns t ON t.session_id = f.session_id AND t.turn_id = f.turn_id
        WHERE s.overall_verdict != 'CLEAN'
        GROUP BY f.session_id
    )
    SELECT
        COUNT(*) AS overall,
        COUNT(CASE WHEN astrotalk_flagged = 0 THEN 1 END) AS astro_0,
        COUNT(CASE WHEN astrotalk_flagged = 1 THEN 1 END) AS astro_1,
        COUNT(CASE WHEN has_user = 1 AND has_astrologer = 0 THEN 1 END) AS by_user,
        COUNT(CASE WHEN has_user = 0 AND has_astrologer = 1 THEN 1 END) AS by_astrologer,
        COUNT(CASE WHEN has_user = 1 AND has_astrologer = 1 THEN 1 END) AS by_both,
        COUNT(CASE WHEN has_user = 0 AND has_astrologer = 0 THEN 1 END) AS unattributed,
        COUNT(CASE WHEN astrotalk_flagged = 0 AND has_user = 1 AND has_astrologer = 0 THEN 1 END) AS astro_0_user,
        COUNT(CASE WHEN astrotalk_flagged = 0 AND has_user = 0 AND has_astrologer = 1 THEN 1 END) AS astro_0_astrologer,
        COUNT(CASE WHEN astrotalk_flagged = 0 AND has_user = 1 AND has_astrologer = 1 THEN 1 END) AS astro_0_both,
        COUNT(CASE WHEN astrotalk_flagged = 1 AND has_user = 1 AND has_astrologer = 0 THEN 1 END) AS astro_1_user,
        COUNT(CASE WHEN astrotalk_flagged = 1 AND has_user = 0 AND has_astrologer = 1 THEN 1 END) AS astro_1_astrologer,
        COUNT(CASE WHEN astrotalk_flagged = 1 AND has_user = 1 AND has_astrologer = 1 THEN 1 END) AS astro_1_both
    FROM ss
"""

# Total sessions in the DB per astrotalk_flagged value (regardless of verdict).
ALL_SESSIONS_SQL = """
    SELECT
        COUNT(*) AS total,
        SUM(CASE WHEN astrotalk_flagged = 0 THEN 1 ELSE 0 END) AS astro_0,
        SUM(CASE WHEN astrotalk_flagged = 1 THEN 1 ELSE 0 END) AS astro_1
    FROM sessions
"""


def _print_table(title, rows, col, total_sessions=None, violation_sessions=None):
    print("-" * 64)
    print(f"  {title}")
    print("-" * 64)
    if total_sessions is not None:
        print(f"  Total sessions                           {total_sessions:>8,}")
    if violation_sessions is not None:
        print(f"  Sessions with violations                 {violation_sessions:>8,}")
        print()
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
    all_sessions = conn.execute(ALL_SESSIONS_SQL).fetchone()
    conn.close()

    print("=" * 64)
    print("  Violation Breakdown (distinct sessions per category)")
    print(f"  DB: {DB_PATH}")
    print("=" * 64)
    print(f"  Total sessions in DB                         : {all_sessions['total']:>8,}")
    print(f"  ... of which astrotalk_flagged = 0           : {all_sessions['astro_0'] or 0:>8,}")
    print(f"  ... of which astrotalk_flagged = 1           : {all_sessions['astro_1'] or 0:>8,}")
    print(f"  Sessions with violations (overall)           : {totals['overall']:>8,}")
    print(f"  ... of which astrotalk_flagged = 0           : {totals['astro_0']:>8,}")
    print(f"  ... of which astrotalk_flagged = 1           : {totals['astro_1']:>8,}")
    print(f"  ... violations by USER only                  : {totals['by_user']:>8,}")
    print(f"  ... violations by ASTROLOGER only            : {totals['by_astrologer']:>8,}")
    print(f"  ... violations by BOTH speakers              : {totals['by_both']:>8,}")
    print(f"  ... unattributed (flag has no linked turn)   : {totals['unattributed']:>8,}")
    print()

    _print_table("Overall (matches the UI Violation Breakdown)", rows, "overall",
                 total_sessions=all_sessions["total"], violation_sessions=totals["overall"])
    _print_table("astrotalk_flagged = 0  (AstroTalk did NOT flag)", rows, "astro_0",
                 total_sessions=all_sessions["astro_0"] or 0, violation_sessions=totals["astro_0"])
    _print_table("astrotalk_flagged = 1  (AstroTalk DID flag)", rows, "astro_1",
                 total_sessions=all_sessions["astro_1"] or 0, violation_sessions=totals["astro_1"])
    _print_table("Violations by USER only (flagged turns spoken only by user)", rows, "by_user",
                 violation_sessions=totals["by_user"])
    _print_table("Violations by ASTROLOGER only (flagged turns spoken only by astrologer)", rows, "by_astrologer",
                 violation_sessions=totals["by_astrologer"])
    _print_table("Violations by BOTH (flagged turns from both speakers)", rows, "by_both",
                 violation_sessions=totals["by_both"])
    _print_table("astrotalk_flagged = 0  x  USER only", rows, "astro_0_user",
                 violation_sessions=totals["astro_0_user"])
    _print_table("astrotalk_flagged = 0  x  ASTROLOGER only", rows, "astro_0_astrologer",
                 violation_sessions=totals["astro_0_astrologer"])
    _print_table("astrotalk_flagged = 0  x  BOTH speakers", rows, "astro_0_both",
                 violation_sessions=totals["astro_0_both"])
    _print_table("astrotalk_flagged = 1  x  USER only", rows, "astro_1_user",
                 violation_sessions=totals["astro_1_user"])
    _print_table("astrotalk_flagged = 1  x  ASTROLOGER only", rows, "astro_1_astrologer",
                 violation_sessions=totals["astro_1_astrologer"])
    _print_table("astrotalk_flagged = 1  x  BOTH speakers", rows, "astro_1_both",
                 violation_sessions=totals["astro_1_both"])

    if args.out:
        breakdown_header = ["category_code", "overall", "astrotalk_flagged_0",
                            "astrotalk_flagged_1", "user_only", "astrologer_only", "both",
                            "flagged_0_user_only", "flagged_0_astrologer_only", "flagged_0_both",
                            "flagged_1_user_only", "flagged_1_astrologer_only", "flagged_1_both"]
        breakdown_rows = [
            [r["category_code"], r["overall"], r["astro_0"], r["astro_1"],
             r["by_user"], r["by_astrologer"], r["by_both"],
             r["astro_0_user"], r["astro_0_astrologer"], r["astro_0_both"],
             r["astro_1_user"], r["astro_1_astrologer"], r["astro_1_both"]]
            for r in rows
        ]
        summary_rows = [
            ("Total sessions in DB",                              all_sessions["total"]),
            ("Total sessions astrotalk_flagged = 0",              all_sessions["astro_0"] or 0),
            ("Total sessions astrotalk_flagged = 1",              all_sessions["astro_1"] or 0),
            ("Sessions with violations (overall)",                totals["overall"]),
            ("Sessions with violations astrotalk_flagged = 0",    totals["astro_0"]),
            ("Sessions with violations astrotalk_flagged = 1",    totals["astro_1"]),
            ("Sessions with USER-only violations",                totals["by_user"]),
            ("Sessions with ASTROLOGER-only violations",          totals["by_astrologer"]),
            ("Sessions with violations by BOTH speakers",         totals["by_both"]),
            ("Sessions with unattributed violations (no linked turn)", totals["unattributed"]),
            ("Sessions astrotalk_flagged = 0 x USER only",        totals["astro_0_user"]),
            ("Sessions astrotalk_flagged = 0 x ASTROLOGER only",  totals["astro_0_astrologer"]),
            ("Sessions astrotalk_flagged = 0 x BOTH",             totals["astro_0_both"]),
            ("Sessions astrotalk_flagged = 1 x USER only",        totals["astro_1_user"]),
            ("Sessions astrotalk_flagged = 1 x ASTROLOGER only",  totals["astro_1_astrologer"]),
            ("Sessions astrotalk_flagged = 1 x BOTH",             totals["astro_1_both"]),
        ]

        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if out_path.suffix.lower() == ".csv":
            with open(out_path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(breakdown_header)
                writer.writerows(breakdown_rows)
            summary_path = out_path.with_name(out_path.stem + "_summary.csv")
            with open(summary_path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["metric", "session_count"])
                writer.writerows(summary_rows)
            print(f"  CSV written: {out_path}")
            print(f"  CSV written: {summary_path}")
        else:
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "Summary"
            ws.append(["metric", "session_count"])
            for row in summary_rows:
                ws.append(list(row))
            ws.column_dimensions["A"].width = 52
            ws.column_dimensions["B"].width = 14
            ws2 = wb.create_sheet("Breakdown")
            ws2.append(breakdown_header)
            for row in breakdown_rows:
                ws2.append(row)
            ws2.column_dimensions["A"].width = 36
            wb.save(out_path)
            print(f"  Excel written: {out_path}  (sheets: Summary, Breakdown)")


if __name__ == "__main__":
    main()
