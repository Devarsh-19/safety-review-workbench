"""
export_session_turns_with_flags.py

Chat review database (store/astrotalk.db). Flat export of EVERY session with ALL
its turns — one row per turn, in (session_id, turn_id) order — with extra columns
marking which turns carry an ACTIVE flag.

Strictly read-only: the DB is opened with mode=ro, so SQLite rejects any write —
this script cannot modify the database.

"Active" flag = the same rule the app and the other scripts use:
  - status is not DISMISSED, and
  - the row is an amendment, or an original that has no amendment (an amended
    original is superseded by its amendment row and does not count).
A turn "has an active flag" when an active flag's turn_id points at it.

Per-turn columns added after the turn's own fields:
  - has_active_flag        1 / 0
  - active_flag_count      number of active flags on that turn
  - active_flag_categories comma-separated category_code list (active only)

Turns with no flag get has_active_flag = 0, count 0, empty categories. Active
flags whose turn_id does not resolve to a turn (NULL / stale) are not attached to
any turn row; pass --report-unlinked to print how many there are.

Output (--out): .csv (utf-8-sig, opens cleanly in Excel) or .xlsx (needs openpyxl).
Defaults to exports/session_turns_with_flags.csv.

Usage:
  python scripts/export_session_turns_with_flags.py
  python scripts/export_session_turns_with_flags.py --out exports/session_turns_with_flags.xlsx
  python scripts/export_session_turns_with_flags.py --report-unlinked
"""

import argparse
import csv
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import DB_PATH  # noqa: E402


def get_readonly_connection() -> sqlite3.Connection:
    """Open the chat DB strictly read-only (mode=ro): SQLite rejects any write,
    so this export can never modify the database. A live app is undisturbed."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


HEADER = [
    # session-level context (repeated on every turn of the session)
    "session_id", "overall_verdict", "review_status", "astrotalk_flagged",
    # turn-level
    "turn_id", "speaker", "is_automated", "timestamp", "message_text",
    # flag summary for THIS turn (active flags only)
    "has_active_flag", "active_flag_count", "active_flag_categories",
]


def active_flags_by_turn(conn):
    """Map (session_id, turn_id) -> [category_code, ...] over ACTIVE flags only.
    Amendment rows win over amended originals; DISMISSED rows are excluded."""
    rows = conn.execute(
        """
        SELECT f.session_id, f.turn_id, f.category_code
        FROM flags f
        WHERE (f.status IS NULL OR f.status != 'DISMISSED')
          AND f.turn_id IS NOT NULL
          AND f.flag_id NOT IN (
              SELECT parent_flag_id FROM flags WHERE parent_flag_id IS NOT NULL
          )
        """
    ).fetchall()
    by_turn = defaultdict(list)
    for r in rows:
        by_turn[(r["session_id"], r["turn_id"])].append(r["category_code"] or "")
    return by_turn


def count_unlinked_active_flags(conn):
    """Active flags with a NULL or non-resolving turn_id — they can't be attached
    to any turn row, so report them separately. LEFT JOIN on the turns PK is
    cheaper than a correlated NOT EXISTS."""
    return conn.execute(
        """
        SELECT COUNT(*)
        FROM flags f
        LEFT JOIN turns t
          ON t.session_id = f.session_id AND t.turn_id = f.turn_id
        WHERE (f.status IS NULL OR f.status != 'DISMISSED')
          AND f.flag_id NOT IN (
              SELECT parent_flag_id FROM flags WHERE parent_flag_id IS NOT NULL
          )
          AND t.turn_id IS NULL
        """
    ).fetchone()[0]


def build_rows(conn):
    """Stream one row per turn in (session_id, turn_id) order.

    A single JOIN drives the whole export (not one query per session), and the
    cursor is iterated lazily so memory stays flat regardless of DB size. The
    per-turn flag map is a small in-memory dict keyed by (session_id, turn_id).
    """
    by_turn = active_flags_by_turn(conn)

    cur = conn.execute(
        """
        SELECT s.session_id, s.overall_verdict, s.review_status, s.astrotalk_flagged,
               t.turn_id, t.speaker, t.is_automated, t.timestamp, t.message_text
        FROM turns t
        JOIN sessions s ON s.session_id = t.session_id
        ORDER BY s.session_id, t.turn_id
        """
    )
    for r in cur:
        cats = by_turn.get((r["session_id"], r["turn_id"]), [])
        yield [
            r["session_id"], r["overall_verdict"], r["review_status"], r["astrotalk_flagged"],
            r["turn_id"], r["speaker"], r["is_automated"], r["timestamp"], r["message_text"],
            1 if cats else 0, len(cats), ", ".join(cats),
        ]


def write_csv(rows, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    # utf-8-sig so Excel renders Hindi/other non-ASCII correctly.
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        for row in rows:
            writer.writerow(row)
            n += 1
    return n


def write_xlsx(rows, out_path):
    try:
        from openpyxl import Workbook
    except ImportError:
        sys.exit("  xlsx mode needs openpyxl (pip install openpyxl), or use a .csv --out.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "session_turns"
    ws.append(HEADER)
    n = 0
    for row in rows:
        ws.append(row)
        n += 1
    ws.freeze_panes = "A2"
    wb.save(out_path)
    return n


def main():
    parser = argparse.ArgumentParser(
        description="Chat DB: export every session's turns with a per-turn active-flag marker."
    )
    parser.add_argument("--out", default="exports/session_turns_with_flags.csv",
                        help="Output .csv or .xlsx path (default: exports/session_turns_with_flags.csv).")
    parser.add_argument("--report-unlinked", action="store_true",
                        help="Also print how many active flags have no resolvable turn.")
    args = parser.parse_args()

    out_path = Path(args.out)
    conn = get_readonly_connection()
    try:
        unlinked = count_unlinked_active_flags(conn) if args.report_unlinked else None
        # Stream the cursor straight to disk — rows are never all held in memory.
        if out_path.suffix.lower() == ".xlsx":
            n = write_xlsx(build_rows(conn), out_path)
        else:
            n = write_csv(build_rows(conn), out_path)
    finally:
        conn.close()

    print("=" * 64)
    print("  Session turns + active-flag markers")
    print(f"  DB  : {DB_PATH}")
    print(f"  Out : {out_path}  ({n:,} turn rows)")
    if unlinked is not None:
        print(f"  Active flags with no resolvable turn (not in export): {unlinked:,}")
    print("=" * 64)


if __name__ == "__main__":
    main()
