"""
false_negative_breakdown.py

Per-flag-category breakdown of the dashboard's False Negative sessions.
Read-only.

A False Negative uses exactly the GET /stats logic in
review_interface/api/main.py:

  1. AstroTalk did NOT flag the session
     (astrotalk_flagged IS NULL OR astrotalk_flagged != 1), AND
  2. the session is "flagged by us":
       - at least one ACTIVE (non-amended) MANUAL/LLM flag whose category
         is not excluded, AND
       - at least one ACTIVE flag (any source) whose category is neither
         excluded nor low-signal (drop-only).

Flags counted in the breakdown are the ones that logic considers: active
flags on False Negative sessions, excluding the excluded categories
(RE_ENGAGEMENT_SOLICITATION, PERSONAL_DATA_COLLECTION). Low-signal
categories (FAKE_REMEDIES, INSTIGATION, FEAR_MANIPULATION,
FINANCIAL_SOLICITATION, OFF_PLATFORM_SOLICITATION) appear in the table --
they exist on qualifying sessions -- but are marked, since they can never
qualify a session by themselves.

Counts are DISTINCT sessions per category, so column totals can exceed the
number of False Negative sessions (a session can carry several categories).

Export (--out) writes everything shown on screen:
  .xlsx  -> two sheets: "Summary" and "Breakdown".
  .csv   -> two files: <name>.csv (breakdown) and <name>_summary.csv (totals).

Usage:
  python scripts/false_negative_breakdown.py
  python scripts/false_negative_breakdown.py --out exports/false_negative_breakdown.xlsx
  python scripts/false_negative_breakdown.py --out exports/false_negative_breakdown.csv
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
# Mirrors review_interface/api/main.py — keep in sync with the /stats logic.
# ---------------------------------------------------------------------------
_EXCLUDED_CATEGORIES = (
    "re_engagement_solicitation",
    "personal_data_collection",
)
_LOW_SIGNAL_CATEGORIES = (
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
_LS_LIST     = ",".join(f"'{c}'" for c in _LOW_SIGNAL_CATEGORIES)

_FLAGGED_BY_US_SQL = f"""(
    EXISTS (SELECT 1 FROM flags f
            WHERE f.session_id = s.session_id
              AND f.source IN ('MANUAL','LLM')
              AND {_NORM_CAT} NOT IN ({_EXCL_LIST})
              AND {_ACTIVE_FLAG})
    AND EXISTS (SELECT 1 FROM flags f
            WHERE f.session_id = s.session_id
              AND {_NORM_CAT} NOT IN ({_EXCL_LIST})
              AND {_NORM_CAT} NOT IN ({_LS_LIST})
              AND {_ACTIVE_FLAG})
)"""

# Dashboard False Negative sessions.
_FALSE_NEGATIVE_SESSIONS_SQL = f"""
    SELECT s.session_id
    FROM sessions s
    WHERE (s.astrotalk_flagged IS NULL OR s.astrotalk_flagged != 1)
      AND {_FLAGGED_BY_US_SQL}
"""

# Distinct False Negative sessions per category, over the flags the /stats
# logic considers: active, non-excluded. Split by flag source as well.
BREAKDOWN_SQL = f"""
    WITH fn AS ({_FALSE_NEGATIVE_SESSIONS_SQL})
    SELECT
        UPPER({_NORM_CAT}) AS category_code,
        MAX(CASE WHEN {_NORM_CAT} IN ({_LS_LIST}) THEN 1 ELSE 0 END) AS low_signal,
        COUNT(DISTINCT f.session_id) AS sessions,
        COUNT(DISTINCT CASE WHEN f.source = 'LLM'    THEN f.session_id END) AS llm,
        COUNT(DISTINCT CASE WHEN f.source = 'MANUAL' THEN f.session_id END) AS manual,
        COUNT(DISTINCT CASE WHEN f.source NOT IN ('LLM','MANUAL') THEN f.session_id END) AS other_source
    FROM flags f
    JOIN fn ON fn.session_id = f.session_id
    WHERE {_NORM_CAT} NOT IN ({_EXCL_LIST})
      AND {_ACTIVE_FLAG}
    GROUP BY UPPER({_NORM_CAT})
    ORDER BY sessions DESC, category_code
"""

SUMMARY_SQL = f"""
    WITH fn AS ({_FALSE_NEGATIVE_SESSIONS_SQL})
    SELECT
        (SELECT COUNT(*) FROM sessions)                                   AS total_sessions,
        (SELECT COUNT(*) FROM sessions s
          WHERE s.astrotalk_flagged IS NULL OR s.astrotalk_flagged != 1)  AS astrotalk_not_flagged,
        (SELECT COUNT(*) FROM fn)                                         AS false_negative
"""


def main():
    parser = argparse.ArgumentParser(
        description="False Negative breakdown by flag category (dashboard /stats logic).")
    parser.add_argument("--out", help="Optional .csv or .xlsx output path.")
    args = parser.parse_args()

    conn = get_connection()
    rows = conn.execute(BREAKDOWN_SQL).fetchall()
    summary = conn.execute(SUMMARY_SQL).fetchone()
    conn.close()

    fn_total = summary["false_negative"]
    denom = summary["astrotalk_not_flagged"]
    pct = round(100 * fn_total / denom, 1) if denom else 0

    print("=" * 72)
    print("  False Negative Breakdown by flag category (dashboard /stats logic)")
    print(f"  DB: {DB_PATH}")
    print("=" * 72)
    print(f"  Total sessions in DB                         : {summary['total_sessions']:>8,}")
    print(f"  Sessions AstroTalk did NOT flag (0 or NULL)  : {denom:>8,}")
    print(f"  False Negative sessions (matches dashboard)  : {fn_total:>8,}  ({pct}%)")
    print()
    print("-" * 72)
    print(f"  {'category':<38} {'sessions':>8} {'LLM':>6} {'MANUAL':>7} {'other':>6}")
    print("-" * 72)
    if not rows:
        print("  (no false negative sessions)")
    for r in rows:
        tag = "  (low-signal)" if r["low_signal"] else ""
        print(f"  {r['category_code']:<38} {r['sessions']:>8,} {r['llm']:>6,}"
              f" {r['manual']:>7,} {r['other_source']:>6,}{tag}")
    print()
    print("  sessions = distinct False Negative sessions carrying that category")
    print("  (a session with several categories appears under each of them)")
    print("  low-signal categories never qualify a session by themselves")
    print()

    if args.out:
        breakdown_header = ["category_code", "sessions", "llm", "manual",
                            "other_source", "low_signal"]
        breakdown_rows = [
            [r["category_code"], r["sessions"], r["llm"], r["manual"],
             r["other_source"], r["low_signal"]]
            for r in rows
        ]
        summary_rows = [
            ("Total sessions in DB",                        summary["total_sessions"]),
            ("Sessions AstroTalk did NOT flag (0 or NULL)", denom),
            ("False Negative sessions (matches dashboard)", fn_total),
            ("False Negative % of not-flagged",             pct),
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
                writer.writerow(["metric", "value"])
                writer.writerows(summary_rows)
            print(f"  CSV written: {out_path}")
            print(f"  CSV written: {summary_path}")
        else:
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "Summary"
            ws.append(["metric", "value"])
            for row in summary_rows:
                ws.append(list(row))
            ws.column_dimensions["A"].width = 48
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
