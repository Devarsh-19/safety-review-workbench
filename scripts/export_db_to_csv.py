#!/usr/bin/env python3
"""Dump an entire SQLite database (all tables, all columns) to CSV/XLSX.

Two output modes:

  * ``--format csv``  (default) : one CSV file per table, written into an
                                  output directory.
  * ``--format xlsx``           : a single .xlsx workbook with one sheet
                                  per table (requires ``openpyxl``).

Examples
--------
    # every table of the chat DB -> one CSV each under ./db_export/
    python scripts/export_db_to_csv.py

    # a specific DB, into a chosen folder
    python scripts/export_db_to_csv.py store/audio_review.db -o audio_export

    # single workbook, one sheet per table
    python scripts/export_db_to_csv.py --format xlsx -o chat_dump.xlsx
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "store" / "astrotalk.db"

# sqlite bookkeeping tables that carry no user data
SKIP_TABLES = {"sqlite_sequence", "sqlite_stat1", "sqlite_stat4"}
# Excel sheet-name limits
SHEET_MAX_LEN = 31
SHEET_BAD_CHARS = set(r"[]:*?/\\")


def list_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows if r[0] not in SKIP_TABLES]


def fetch_table(conn: sqlite3.Connection, table: str):
    """Return (column_names, row_iterator) for a table, all columns."""
    cur = conn.execute(f'SELECT * FROM "{table}"')
    columns = [d[0] for d in cur.description]
    return columns, cur


def export_csv(conn: sqlite3.Connection, tables: list[str], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for table in tables:
        columns, cur = fetch_table(conn, table)
        path = out_dir / f"{table}.csv"
        n = 0
        # utf-8-sig so Excel opens non-ASCII (Hindi/etc.) correctly
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            for row in cur:
                writer.writerow(row)
                n += 1
        print(f"  {table:<20} {n:>8} rows  ->  {path}")


def _safe_sheet_name(name: str, used: set[str]) -> str:
    clean = "".join("_" if c in SHEET_BAD_CHARS else c for c in name)[:SHEET_MAX_LEN]
    candidate, i = clean, 1
    while candidate.lower() in used:
        suffix = f"_{i}"
        candidate = clean[: SHEET_MAX_LEN - len(suffix)] + suffix
        i += 1
    used.add(candidate.lower())
    return candidate


def export_xlsx(conn: sqlite3.Connection, tables: list[str], out_path: Path) -> None:
    try:
        from openpyxl import Workbook
    except ImportError:
        sys.exit(
            "  xlsx mode needs openpyxl. Install it with:\n"
            "      pip install openpyxl\n"
            "  or use the default CSV mode (--format csv)."
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)  # drop the default empty sheet
    used: set[str] = set()
    for table in tables:
        columns, cur = fetch_table(conn, table)
        ws = wb.create_sheet(_safe_sheet_name(table, used))
        ws.append(columns)
        n = 0
        for row in cur:
            # openpyxl can't write bytes; coerce to str
            ws.append([v.hex() if isinstance(v, bytes) else v for v in row])
            n += 1
        print(f"  {table:<20} {n:>8} rows  ->  sheet '{ws.title}'")
    wb.save(out_path)
    print(f"\n  Workbook saved: {out_path}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db", nargs="?", default=os.getenv("DB_PATH", str(DEFAULT_DB)),
                   help=f"path to SQLite DB (default: {DEFAULT_DB})")
    p.add_argument("-f", "--format", choices=["csv", "xlsx"], default="csv",
                   help="output format (default: csv = one file per table)")
    p.add_argument("-o", "--out",
                   help="output dir (csv) or .xlsx file (xlsx). "
                        "Defaults to '<dbname>_export/' or '<dbname>_export.xlsx'.")
    args = p.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"Database not found: {db_path}")

    stem = db_path.stem
    if args.format == "csv":
        out = Path(args.out) if args.out else PROJECT_ROOT / f"{stem}_export"
    else:
        out = Path(args.out) if args.out else PROJECT_ROOT / f"{stem}_export.xlsx"

    # read-only connection so a live app is never disturbed
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = list_tables(conn)
        if not tables:
            sys.exit("No user tables found in the database.")
        print(f"  DB      : {db_path}")
        print(f"  Tables  : {len(tables)} ({', '.join(tables)})")
        print(f"  Format  : {args.format}\n")
        if args.format == "csv":
            export_csv(conn, tables, out)
            print(f"\n  Done. CSVs written to: {out}")
        else:
            export_xlsx(conn, tables, out)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
