"""
l1_daily_submissions.py

Read-only report: distinct sessions each L1 reviewer submitted for review per
calendar day, whether the session is still SUBMITTED_FOR_REVIEW or has since been
LOCKED. A session counts only if a human L1 submitted it for review (it carries a
submitted_by / submitted_at stamp); sessions locked WITHOUT being submitted — e.g.
auto-processed clean sessions that went straight to LOCKED with submitted_by
'LLM' / 'AUTO_LOCK' — are NOT counted.

The day is date(submitted_at) (UTC, as stored). Because submitted_at holds one
timestamp per session, COUNT(DISTINCT session_id) is the number of sessions that
L1 reviewer submitted that day.

Works on both stores:
  * chat review DB  — table 'sessions',       id 'session_id'  (store/astrotalk.db)   [default]
  * audio review DB — table 'audio_sessions',  id 's_id'        (store/audio_review.db) [--audio]

Usage:
  python scripts/l1_daily_submissions.py                      # chat DB (default)
  python scripts/l1_daily_submissions.py --audio              # audio DB
  python scripts/l1_daily_submissions.py --db path/to.db      # explicit DB
  python scripts/l1_daily_submissions.py --csv out.csv        # also write long CSV
  python scripts/l1_daily_submissions.py --since 2026-07-01   # only days >= date

Read-only: never writes to the database.
"""

import argparse
import csv
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "store" / "astrotalk.db"
# --audio target: the audio review DB, honouring AUDIO_DB_PATH (see store/audio_db.py).
AUDIO_DB = PROJECT_ROOT / os.getenv("AUDIO_DB_PATH", "store/audio_review.db")

# Count sessions in either of these statuses (submitted for review, or locked
# after having been submitted). PENDING and any other status are ignored.
COUNTED_STATUSES = ("SUBMITTED_FOR_REVIEW", "LOCKED")

# Non-human submitters excluded from the L1 tally: 'LLM' (auto-submitted clean
# sessions) and 'AUTO_LOCK' (sessions the auto-process script locked without a
# human ever submitting them). Filtering these keeps only real L1 submissions —
# so a LOCKED session only counts if a human submitted it for review first.
NON_L1_SUBMITTERS = ("LLM", "AUTO_LOCK")


def detect_table(conn: sqlite3.Connection) -> tuple[str, str]:
    """Return (table, id_column) for whichever session table this DB has."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "sessions" in tables:
        return "sessions", "session_id"
    if "audio_sessions" in tables:
        return "audio_sessions", "s_id"
    raise SystemExit(
        "No 'sessions' or 'audio_sessions' table in this database.")


def fetch_rows(conn: sqlite3.Connection, table: str, id_col: str,
               since: str | None) -> list[tuple[str, str, int]]:
    """[(day, l1_user, distinct_session_count), ...] ordered by day, then user."""
    status_ph = ", ".join("?" for _ in COUNTED_STATUSES)
    sub_ph = ", ".join("?" for _ in NON_L1_SUBMITTERS)
    params: list = [*COUNTED_STATUSES, *NON_L1_SUBMITTERS]
    since_clause = ""
    if since:
        since_clause = "AND date(submitted_at) >= ?"
        params.append(since)
    sql = f"""
        SELECT date(submitted_at)         AS day,
               submitted_by               AS l1,
               COUNT(DISTINCT {id_col})   AS n
        FROM {table}
        WHERE review_status IN ({status_ph})
          AND submitted_by IS NOT NULL
          AND submitted_by NOT IN ({sub_ph})
          AND submitted_at IS NOT NULL
          {since_clause}
        GROUP BY day, l1
        ORDER BY day, l1
    """
    return [(r[0], r[1], r[2]) for r in conn.execute(sql, params).fetchall()]


def render_pivot(rows: list[tuple[str, str, int]]) -> None:
    """Days as rows, L1 users as columns; row/column/grand totals."""
    days = sorted({r[0] for r in rows})
    users = sorted({r[1] for r in rows})
    grid = {(r[0], r[1]): r[2] for r in rows}

    if not rows:
        print("  (no L1 submissions found)")
        return

    headers = ["DATE"] + users + ["TOTAL"]
    body: list[list[str]] = []
    col_totals = {u: 0 for u in users}
    grand = 0
    for day in days:
        cells = [grid.get((day, u), 0) for u in users]
        row_total = sum(cells)
        grand += row_total
        for u, c in zip(users, cells):
            col_totals[u] += c
        body.append([day] + [str(c) for c in cells] + [str(row_total)])
    body.append(["TOTAL"] + [str(col_totals[u]) for u in users] + [str(grand)])

    widths = [len(h) for h in headers]
    for row in body:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], len(c))

    def fmt(cells: list[str]) -> str:
        parts = [f"{c:<{widths[0]}}" if i == 0 else f"{c:>{widths[i]}}"
                 for i, c in enumerate(cells)]
        return "  " + "  ".join(parts)

    print(fmt(headers))
    print("  " + "-" * (sum(widths) + 2 * (len(widths) - 1)))
    for row in body[:-1]:
        print(fmt(row))
    print("  " + "-" * (sum(widths) + 2 * (len(widths) - 1)))
    print(fmt(body[-1]))


def render_per_user(rows: list[tuple[str, str, int]]) -> None:
    totals: dict[str, int] = {}
    active_days: dict[str, set] = {}
    for day, user, n in rows:
        totals[user] = totals.get(user, 0) + n
        active_days.setdefault(user, set()).add(day)
    if not totals:
        return
    print()
    print("Per L1 user (total submitted / active days / avg per active day)")
    print("-" * 62)
    name_w = max(len("L1 USER"), max(len(u) for u in totals))
    print(f"  {'L1 USER':<{name_w}}   SUBMITTED   DAYS   AVG/DAY")
    for user in sorted(totals):
        d = len(active_days[user])
        avg = totals[user] / d if d else 0
        print(f"  {user:<{name_w}}   {totals[user]:>9}   {d:>4}   {avg:>7.1f}")


def write_csv(rows: list[tuple[str, str, int]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "l1_user", "distinct_sessions_submitted"])
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Distinct sessions submitted per L1 reviewer per day (read-only).")
    ap.add_argument("--audio", action="store_true",
                    help="Use the audio review DB (store/audio_review.db or "
                         "$AUDIO_DB_PATH) instead of the chat DB.")
    ap.add_argument("--db",
                    help="Explicit SQLite DB path (overrides --audio and the default chat DB).")
    ap.add_argument("--since", metavar="YYYY-MM-DD",
                    help="Only include days on/after this date.")
    ap.add_argument("--csv", metavar="PATH",
                    help="Also write the long (date, user, count) rows to a CSV.")
    args = ap.parse_args()

    if args.db:
        db_path = Path(args.db)
    elif args.audio:
        db_path = Path(AUDIO_DB)
    else:
        db_path = Path(DEFAULT_DB)
    if not db_path.exists():
        sys.exit(f"Database not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        table, id_col = detect_table(conn)
        rows = fetch_rows(conn, table, id_col, args.since)
    finally:
        conn.close()

    print("=" * 62)
    print("  L1 daily submitted distinct sessions")
    print("=" * 62)
    print(f"  DB     : {db_path}")
    print(f"  Table  : {table} (id: {id_col})")
    print(f"  Status : {' + '.join(COUNTED_STATUSES)} (submitted by a human L1)")
    print(f"  Excludes submitted_by in {NON_L1_SUBMITTERS}"
          + (f"; since {args.since}" if args.since else ""))
    print()
    render_pivot(rows)
    render_per_user(rows)

    if args.csv:
        write_csv(rows, Path(args.csv))
        print(f"\nWrote long CSV to {args.csv}")


if __name__ == "__main__":
    main()
