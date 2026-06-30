"""
fix_flag_case.py

Normalises lowercase flag values to UPPER CASE in the flags table. Only rows
that actually contain a lowercase letter are touched (idempotent — safe to
re-run). By default it fixes flags.category_code; pass --all to also normalise
source, status, severity and false_positive_risk.

DRY-RUN BY DEFAULT — running with no flags only previews what would change.
Pass --commit to actually write the updates.

Usage:
  python scripts/fix_flag_case.py                 # preview category_code (dry-run)
  python scripts/fix_flag_case.py --commit        # apply category_code fix
  python scripts/fix_flag_case.py --all           # preview all flag columns
  python scripts/fix_flag_case.py --all --commit  # apply to all flag columns
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import get_connection, DB_PATH  # noqa: E402

# Flag columns that should always be stored in UPPER CASE.
DEFAULT_COLUMNS = ["category_code"]
ALL_COLUMNS = ["category_code", "source", "status", "severity", "false_positive_risk"]


def fix_case(columns: list[str], commit: bool = False) -> int:
    conn = get_connection()
    try:
        grand_total = 0
        for col in columns:
            # Rows with at least one lowercase letter.
            rows = conn.execute(
                f"""SELECT flag_id, {col} AS val
                    FROM flags
                    WHERE {col} IS NOT NULL AND {col} GLOB '*[a-z]*'
                    ORDER BY flag_id"""
            ).fetchall()
            n = len(rows)
            grand_total += n

            if n == 0:
                print(f"  {col:<22} no lowercase values — nothing to fix.")
                continue

            print(f"  {col:<22} {n:,} row(s) to fix:")
            # Show the distinct value -> UPPER mappings.
            seen = {}
            for r in rows:
                seen.setdefault(r["val"], 0)
                seen[r["val"]] += 1
            for val, count in sorted(seen.items()):
                print(f"      {val!r} -> {val.upper()!r}  (x{count})")

            if commit:
                conn.execute(
                    f"UPDATE flags SET {col} = UPPER({col}) "
                    f"WHERE {col} IS NOT NULL AND {col} GLOB '*[a-z]*'"
                )
        if commit:
            conn.commit()
        return grand_total
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Uppercase lowercase flag values in the flags table."
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="Actually write the updates (default is dry-run preview only).",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Fix all flag columns, not just category_code.",
    )
    args = parser.parse_args()

    columns = ALL_COLUMNS if args.all else DEFAULT_COLUMNS

    print("=" * 60)
    print("  Fix flag case (lowercase -> UPPERCASE)")
    print("=" * 60)
    print(f"  Database : {DB_PATH}")
    print(f"  Columns  : {', '.join(columns)}")
    print(f"  Mode     : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    total = fix_case(columns, commit=args.commit)
    print()
    if total == 0:
        print("Nothing to fix. All values already uppercase.")
    elif args.commit:
        print(f"Done. Normalised {total:,} value(s) to UPPER CASE.")
    else:
        print(f"DRY RUN — {total:,} value(s) would be fixed. "
              f"Re-run with --commit to apply.")


if __name__ == "__main__":
    main()
