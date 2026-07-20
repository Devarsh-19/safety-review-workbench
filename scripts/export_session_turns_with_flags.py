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

Performance: all flag aggregation is done in SQL (one query), and CSV output is
streamed straight from the cursor with csv.writerows — no per-row Python work and
nothing buffered in memory, so it stays fast on a large production DB.

Output (--out): .csv (utf-8-sig, opens cleanly in Excel; the fast default) or
.xlsx (needs openpyxl; slower — openpyxl builds every cell in memory).
Defaults to exports/session_turns_with_flags.csv.

Usage:
  python scripts/export_session_turns_with_flags.py
  python scripts/export_session_turns_with_flags.py --flagged-only        # skip CLEAN sessions
  python scripts/export_session_turns_with_flags.py --out exports/session_turns_with_flags.xlsx
  python scripts/export_session_turns_with_flags.py --report-unlinked
"""

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import DB_PATH  # noqa: E402


def get_readonly_connection() -> sqlite3.Connection:
    """Open the chat DB strictly read-only (mode=ro): SQLite rejects any write,
    so this export can never modify the database. A live app is undisturbed.
    No row_factory — plain tuples stream fastest into csv.writerows.

    Tuned for a large (multi-GB) DB: memory-map the file so reads come from the
    OS page cache instead of syscalls, and give SQLite a bigger page cache. Both
    are session-local PRAGMAs — they change nothing on disk. Failures (e.g. mmap
    unsupported) are non-fatal; the export just runs a little slower."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    for pragma in (
        "PRAGMA mmap_size = 8000000000",  # up to ~8 GB memory-mapped reads
        "PRAGMA cache_size = -262144",    # 256 MB page cache (negative = KiB)
    ):
        try:
            conn.execute(pragma)
        except sqlite3.Error:
            pass
    return conn


HEADER = [
    # session-level context (repeated on every turn of the session)
    "session_id", "overall_verdict", "review_status", "astrotalk_flagged",
    # turn-level
    "turn_id", "speaker", "is_automated", "timestamp", "message_text",
    # flag summary for THIS turn (active flags only)
    "has_active_flag", "active_flag_count", "active_flag_categories",
]

# A "flagged" session = overall_verdict is not CLEAN — the same definition the
# violation-breakdown scripts use. (A CLEAN session has no active flags anyway,
# so this only drops turns that would all be has_active_flag = 0.) NULL verdicts
# are excluded by != 'CLEAN', which is intended: only genuinely-flagged sessions.
_FLAGGED_WHERE = "WHERE s.overall_verdict != 'CLEAN'"


def build_export_sql(flagged_only: bool = False) -> str:
    """One query does everything: aggregate each turn's ACTIVE flags in SQL, then
    LEFT JOIN onto every turn so turns with no flag still appear (has_active_flag
    = 0). Columns come out in HEADER order, ready to stream. With flagged_only,
    CLEAN sessions are dropped."""
    where = _FLAGGED_WHERE if flagged_only else ""
    return f"""
    WITH active AS (
        SELECT f.session_id, f.turn_id, f.category_code
        FROM flags f
        WHERE (f.status IS NULL OR f.status != 'DISMISSED')
          AND f.turn_id IS NOT NULL
          AND f.flag_id NOT IN (
              SELECT parent_flag_id FROM flags WHERE parent_flag_id IS NOT NULL
          )
    ),
    agg AS (
        SELECT session_id, turn_id,
               COUNT(*)                        AS cnt,
               group_concat(category_code, ', ') AS cats
        FROM active
        GROUP BY session_id, turn_id
    )
    SELECT s.session_id, s.overall_verdict, s.review_status, s.astrotalk_flagged,
           t.turn_id, t.speaker, t.is_automated, t.timestamp, t.message_text,
           CASE WHEN a.cnt IS NULL THEN 0 ELSE 1 END AS has_active_flag,
           COALESCE(a.cnt, 0)                        AS active_flag_count,
           COALESCE(a.cats, '')                      AS active_flag_categories
    FROM turns t
    JOIN sessions s ON s.session_id = t.session_id
    LEFT JOIN agg a ON a.session_id = t.session_id AND a.turn_id = t.turn_id
    {where}
    ORDER BY s.session_id, t.turn_id
"""


# Unfiltered base query (all sessions) — kept as a module constant so the JSON
# export script can import it unchanged.
EXPORT_SQL = build_export_sql()


def build_export_count_sql(flagged_only: bool = False) -> str:
    """Row count for the summary line. Unfiltered, a plain COUNT(*) over turns
    walks the smallest index once. Flagged-only needs the sessions join to test
    the verdict, but only over the (smaller) flagged subset."""
    if not flagged_only:
        # Equals the exported row count under referential integrity (every turn
        # has a session, per the FK); orphan turns would be the only skew.
        return "SELECT COUNT(*) FROM turns"
    return f"""
        SELECT COUNT(*)
        FROM turns t
        JOIN sessions s ON s.session_id = t.session_id
        {_FLAGGED_WHERE}
    """

COUNT_UNLINKED_SQL = """
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


def write_csv(conn, out_path, flagged_only=False):
    """Stream the export cursor straight to CSV. csv.writerows consumes the
    cursor at C speed — no Python per-row loop."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cur = conn.execute(build_export_sql(flagged_only))
    # utf-8-sig so Excel renders Hindi/other non-ASCII correctly.
    # 1 MB buffer so a multi-million-row write isn't dominated by tiny syscalls.
    with open(out_path, "w", newline="", encoding="utf-8-sig", buffering=1 << 20) as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(cur)
    return conn.execute(build_export_count_sql(flagged_only)).fetchone()[0]


def write_xlsx(conn, out_path, flagged_only=False):
    """Slower path — openpyxl materialises every cell. Prefer CSV for big data."""
    try:
        from openpyxl import Workbook
    except ImportError:
        sys.exit("  xlsx mode needs openpyxl (pip install openpyxl), or use a .csv --out.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("session_turns")
    ws.append(HEADER)
    n = 0
    for row in conn.execute(build_export_sql(flagged_only)):
        ws.append(list(row))
        n += 1
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
    parser.add_argument("--flagged-only", action="store_true",
                        help="Export only flagged sessions (overall_verdict != 'CLEAN'); "
                             "skip CLEAN sessions entirely.")
    args = parser.parse_args()

    out_path = Path(args.out)
    conn = get_readonly_connection()
    try:
        unlinked = conn.execute(COUNT_UNLINKED_SQL).fetchone()[0] if args.report_unlinked else None
        if out_path.suffix.lower() == ".xlsx":
            n = write_xlsx(conn, out_path, args.flagged_only)
        else:
            n = write_csv(conn, out_path, args.flagged_only)
    finally:
        conn.close()

    print("=" * 64)
    print("  Session turns + active-flag markers")
    print(f"  DB    : {DB_PATH}")
    print(f"  Scope : {'flagged sessions only (verdict != CLEAN)' if args.flagged_only else 'all sessions'}")
    print(f"  Out   : {out_path}  ({n:,} turn rows)")
    if unlinked is not None:
        print(f"  Active flags with no resolvable turn (not in export): {unlinked:,}")
    print("=" * 64)


if __name__ == "__main__":
    main()
