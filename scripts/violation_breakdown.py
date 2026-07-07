"""
violation_breakdown.py

Violation breakdown over the sessions the dashboard counts as "Flagged by us":
at least one active (non-amended) MANUAL/LLM flag outside the excluded
categories, and not ALL remaining flags in the low-signal (drop-only)
categories. Same definition as GET /stats in review_interface/api/main.py,
so the totals reconcile with the dashboard:

  overall                          = dashboard "Flagged by us"
  astrotalk_flagged != 1 (or NULL) = dashboard "False Negative"
  astrotalk_flagged  = 1           = dashboard "Flagged by both"

(The UI Violation Breakdown panel itself uses a looser filter --
overall_verdict != 'CLEAN' over all flags -- so its per-category numbers
can be slightly higher than the ones here.)

Prints twelve breakdowns:
   1. Overall                          (= dashboard "Flagged by us")
   2. astrotalk_flagged != 1           (AstroTalk missed it -> False Negative)
   3. astrotalk_flagged  = 1           (AstroTalk flagged too -> Flagged by both)
   4. Violations by USER only          (flagged turns spoken only by the user)
   5. Violations by ASTROLOGER only    (flagged turns spoken only by the astrologer)
   6. Violations by BOTH               (flagged turns from both speakers)
   7. astrotalk_flagged != 1 x USER only
   8. astrotalk_flagged != 1 x ASTROLOGER only
   9. astrotalk_flagged != 1 x BOTH
  10. astrotalk_flagged  = 1 x USER only
  11. astrotalk_flagged  = 1 x ASTROLOGER only
  12. astrotalk_flagged  = 1 x BOTH

Per-category rows count only active (non-amended) flags outside the excluded
categories, over the qualifying sessions above.

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


# ---------------------------------------------------------------------------
# "Flagged by us" definition — mirrors review_interface/api/main.py /stats
# (which in turn is based on scripts/diag_export_funnel.py): only active
# (non-amended-parent) MANUAL/LLM flags count, excluded categories never
# count at all, and a session whose remaining flags are ALL low-signal
# categories is not counted.
# ---------------------------------------------------------------------------
_EXCLUDED_CATEGORIES = (
    "re_engagement_solicitation",
    "personal_data_collection",
)
_DROP_ONLY_CATEGORIES = (
    "fake_remedies",
    "instigation",
    "fear_manipulation",
    "financial_solicitation",
    "off_platform_solicitation",
)
_NORM_CAT    = "LOWER(REPLACE(REPLACE(f.category_code,'-','_'),' ','_'))"
_ACTIVE_FLAG = ("f.flag_id NOT IN "
                "(SELECT parent_flag_id FROM flags WHERE parent_flag_id IS NOT NULL)")
_EXCL_LIST   = ",".join(f"'{c}'" for c in _EXCLUDED_CATEGORIES)
_DROP_LIST   = ",".join(f"'{c}'" for c in _DROP_ONLY_CATEGORIES)
_FLAGGED_BY_US_SQL = f"""(
    EXISTS (SELECT 1 FROM flags f
            WHERE f.session_id = s.session_id
              AND f.source IN ('MANUAL','LLM')
              AND {_NORM_CAT} NOT IN ({_EXCL_LIST})
              AND {_ACTIVE_FLAG})
    AND EXISTS (SELECT 1 FROM flags f
            WHERE f.session_id = s.session_id
              AND {_NORM_CAT} NOT IN ({_EXCL_LIST})
              AND {_NORM_CAT} NOT IN ({_DROP_LIST})
              AND {_ACTIVE_FLAG})
)"""

# Qualifying sessions with a normalised astro bucket: 1 = AstroTalk flagged,
# 0 = AstroTalk did not flag (astrotalk_flagged 0 or NULL — same bucketing as
# the dashboard False Negative).
_QUALIFYING_SQL = f"""
    SELECT s.session_id,
           CASE WHEN s.astrotalk_flagged = 1 THEN 1 ELSE 0 END AS astro
    FROM sessions s
    WHERE {_FLAGGED_BY_US_SQL}
"""

# Per (session, category) speaker attribution over the qualifying sessions.
# Only active, non-excluded flags contribute rows. The speaker buckets are
# mutually exclusive (USER only / ASTROLOGER only / BOTH), so each session
# counts exactly once per category.
BREAKDOWN_SQL = f"""
    WITH qualifying AS ({_QUALIFYING_SQL}),
    sc AS (
        SELECT
            f.session_id,
            f.category_code,
            q.astro,
            MAX(CASE WHEN t.speaker = 'USER'       THEN 1 ELSE 0 END) AS has_user,
            MAX(CASE WHEN t.speaker = 'ASTROLOGER' THEN 1 ELSE 0 END) AS has_astrologer
        FROM flags f
        JOIN qualifying q ON q.session_id = f.session_id
        LEFT JOIN turns t ON t.session_id = f.session_id AND t.turn_id = f.turn_id
        WHERE {_NORM_CAT} NOT IN ({_EXCL_LIST})
          AND {_ACTIVE_FLAG}
        GROUP BY f.session_id, f.category_code
    )
    SELECT
        category_code,
        COUNT(*) AS overall,
        COUNT(CASE WHEN astro = 0 THEN 1 END) AS astro_0,
        COUNT(CASE WHEN astro = 1 THEN 1 END) AS astro_1,
        COUNT(CASE WHEN has_user = 1 AND has_astrologer = 0 THEN 1 END) AS by_user,
        COUNT(CASE WHEN has_user = 0 AND has_astrologer = 1 THEN 1 END) AS by_astrologer,
        COUNT(CASE WHEN has_user = 1 AND has_astrologer = 1 THEN 1 END) AS by_both,
        COUNT(CASE WHEN astro = 0 AND has_user = 1 AND has_astrologer = 0 THEN 1 END) AS astro_0_user,
        COUNT(CASE WHEN astro = 0 AND has_user = 0 AND has_astrologer = 1 THEN 1 END) AS astro_0_astrologer,
        COUNT(CASE WHEN astro = 0 AND has_user = 1 AND has_astrologer = 1 THEN 1 END) AS astro_0_both,
        COUNT(CASE WHEN astro = 1 AND has_user = 1 AND has_astrologer = 0 THEN 1 END) AS astro_1_user,
        COUNT(CASE WHEN astro = 1 AND has_user = 0 AND has_astrologer = 1 THEN 1 END) AS astro_1_astrologer,
        COUNT(CASE WHEN astro = 1 AND has_user = 1 AND has_astrologer = 1 THEN 1 END) AS astro_1_both
    FROM sc
    GROUP BY category_code
    ORDER BY overall DESC
"""

SESSION_TOTALS_SQL = f"""
    WITH qualifying AS ({_QUALIFYING_SQL}),
    ss AS (
        SELECT
            q.session_id,
            q.astro,
            MAX(CASE WHEN t.speaker = 'USER'       THEN 1 ELSE 0 END) AS has_user,
            MAX(CASE WHEN t.speaker = 'ASTROLOGER' THEN 1 ELSE 0 END) AS has_astrologer
        FROM qualifying q
        JOIN flags f ON f.session_id = q.session_id
        LEFT JOIN turns t ON t.session_id = f.session_id AND t.turn_id = f.turn_id
        WHERE {_NORM_CAT} NOT IN ({_EXCL_LIST})
          AND {_ACTIVE_FLAG}
        GROUP BY q.session_id
    )
    SELECT
        COUNT(*) AS overall,
        COUNT(CASE WHEN astro = 0 THEN 1 END) AS astro_0,
        COUNT(CASE WHEN astro = 1 THEN 1 END) AS astro_1,
        COUNT(CASE WHEN has_user = 1 AND has_astrologer = 0 THEN 1 END) AS by_user,
        COUNT(CASE WHEN has_user = 0 AND has_astrologer = 1 THEN 1 END) AS by_astrologer,
        COUNT(CASE WHEN has_user = 1 AND has_astrologer = 1 THEN 1 END) AS by_both,
        COUNT(CASE WHEN has_user = 0 AND has_astrologer = 0 THEN 1 END) AS unattributed,
        COUNT(CASE WHEN astro = 0 AND has_user = 1 AND has_astrologer = 0 THEN 1 END) AS astro_0_user,
        COUNT(CASE WHEN astro = 0 AND has_user = 0 AND has_astrologer = 1 THEN 1 END) AS astro_0_astrologer,
        COUNT(CASE WHEN astro = 0 AND has_user = 1 AND has_astrologer = 1 THEN 1 END) AS astro_0_both,
        COUNT(CASE WHEN astro = 1 AND has_user = 1 AND has_astrologer = 0 THEN 1 END) AS astro_1_user,
        COUNT(CASE WHEN astro = 1 AND has_user = 0 AND has_astrologer = 1 THEN 1 END) AS astro_1_astrologer,
        COUNT(CASE WHEN astro = 1 AND has_user = 1 AND has_astrologer = 1 THEN 1 END) AS astro_1_both
    FROM ss
"""

# Total sessions in the DB per astro bucket (regardless of flags).
# NULL astrotalk_flagged falls in the "not flagged" bucket, like the dashboard.
ALL_SESSIONS_SQL = """
    SELECT
        COUNT(*) AS total,
        SUM(CASE WHEN astrotalk_flagged = 1 THEN 0 ELSE 1 END) AS astro_0,
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
    print(f"  ... of which AstroTalk did NOT flag          : {all_sessions['astro_0'] or 0:>8,}")
    print(f"  ... of which AstroTalk flagged (= 1)         : {all_sessions['astro_1'] or 0:>8,}")
    print(f"  Flagged by us (overall, matches dashboard)   : {totals['overall']:>8,}")
    print(f"  ... AstroTalk did NOT flag -> False Negative : {totals['astro_0']:>8,}")
    print(f"  ... AstroTalk flagged too  -> Flagged by both: {totals['astro_1']:>8,}")
    print(f"  ... violations by USER only                  : {totals['by_user']:>8,}")
    print(f"  ... violations by ASTROLOGER only            : {totals['by_astrologer']:>8,}")
    print(f"  ... violations by BOTH speakers              : {totals['by_both']:>8,}")
    print(f"  ... unattributed (flag has no linked turn)   : {totals['unattributed']:>8,}")
    print()

    _print_table("Overall (= dashboard 'Flagged by us')", rows, "overall",
                 total_sessions=all_sessions["total"], violation_sessions=totals["overall"])
    _print_table("AstroTalk did NOT flag (= dashboard False Negative)", rows, "astro_0",
                 total_sessions=all_sessions["astro_0"] or 0, violation_sessions=totals["astro_0"])
    _print_table("AstroTalk DID flag (= dashboard Flagged by both)", rows, "astro_1",
                 total_sessions=all_sessions["astro_1"] or 0, violation_sessions=totals["astro_1"])
    _print_table("Violations by USER only (flagged turns spoken only by user)", rows, "by_user",
                 violation_sessions=totals["by_user"])
    _print_table("Violations by ASTROLOGER only (flagged turns spoken only by astrologer)", rows, "by_astrologer",
                 violation_sessions=totals["by_astrologer"])
    _print_table("Violations by BOTH (flagged turns from both speakers)", rows, "by_both",
                 violation_sessions=totals["by_both"])
    _print_table("AstroTalk NOT flagged  x  USER only", rows, "astro_0_user",
                 violation_sessions=totals["astro_0_user"])
    _print_table("AstroTalk NOT flagged  x  ASTROLOGER only", rows, "astro_0_astrologer",
                 violation_sessions=totals["astro_0_astrologer"])
    _print_table("AstroTalk NOT flagged  x  BOTH speakers", rows, "astro_0_both",
                 violation_sessions=totals["astro_0_both"])
    _print_table("AstroTalk flagged  x  USER only", rows, "astro_1_user",
                 violation_sessions=totals["astro_1_user"])
    _print_table("AstroTalk flagged  x  ASTROLOGER only", rows, "astro_1_astrologer",
                 violation_sessions=totals["astro_1_astrologer"])
    _print_table("AstroTalk flagged  x  BOTH speakers", rows, "astro_1_both",
                 violation_sessions=totals["astro_1_both"])

    if args.out:
        breakdown_header = ["category_code", "overall", "astrotalk_not_flagged",
                            "astrotalk_flagged", "user_only", "astrologer_only", "both",
                            "not_flagged_user_only", "not_flagged_astrologer_only", "not_flagged_both",
                            "flagged_user_only", "flagged_astrologer_only", "flagged_both"]
        breakdown_rows = [
            [r["category_code"], r["overall"], r["astro_0"], r["astro_1"],
             r["by_user"], r["by_astrologer"], r["by_both"],
             r["astro_0_user"], r["astro_0_astrologer"], r["astro_0_both"],
             r["astro_1_user"], r["astro_1_astrologer"], r["astro_1_both"]]
            for r in rows
        ]
        summary_rows = [
            ("Total sessions in DB",                              all_sessions["total"]),
            ("Total sessions AstroTalk did NOT flag",             all_sessions["astro_0"] or 0),
            ("Total sessions AstroTalk flagged",                  all_sessions["astro_1"] or 0),
            ("Flagged by us (overall, matches dashboard)",        totals["overall"]),
            ("AstroTalk did NOT flag (dashboard False Negative)", totals["astro_0"]),
            ("AstroTalk flagged too (dashboard Flagged by both)", totals["astro_1"]),
            ("Sessions with USER-only violations",                totals["by_user"]),
            ("Sessions with ASTROLOGER-only violations",          totals["by_astrologer"]),
            ("Sessions with violations by BOTH speakers",         totals["by_both"]),
            ("Sessions with unattributed violations (no linked turn)", totals["unattributed"]),
            ("AstroTalk NOT flagged x USER only",                 totals["astro_0_user"]),
            ("AstroTalk NOT flagged x ASTROLOGER only",           totals["astro_0_astrologer"]),
            ("AstroTalk NOT flagged x BOTH",                      totals["astro_0_both"]),
            ("AstroTalk flagged x USER only",                     totals["astro_1_user"]),
            ("AstroTalk flagged x ASTROLOGER only",               totals["astro_1_astrologer"]),
            ("AstroTalk flagged x BOTH",                          totals["astro_1_both"]),
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
