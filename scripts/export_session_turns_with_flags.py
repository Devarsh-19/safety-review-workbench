"""
export_session_turns_with_flags.py

Flat CSV export of EVERY session with ALL its turns — one row per turn — with
extra columns marking which turns carry an ACTIVE flag. Two databases:

  chat  (store/astrotalk.db)     sessions / turns    / flags       -> 6 CSV parts
  audio (store/audio_review.db)  audio_sessions / audio_segments / audio_flags
                                                                   -> 1 CSV file

By default a single run exports both: the chat DB split into --parts files (6 by
default) at whole-session boundaries, and the audio DB as one file. Use --db to
run only one of them.

CSV only (utf-8-sig, opens cleanly in Excel).

Strictly read-only: both DBs are opened with mode=ro, so SQLite rejects any
write — this script cannot modify either database.

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

Output: CSV (utf-8-sig, opens cleanly in Excel). Defaults to
exports/session_turns_with_flags.csv (chat) and
exports/audio_session_segments_with_flags.csv (audio).

Usage:
  python scripts/export_session_turns_with_flags.py                       # chat 6 parts + audio 1 file
  python scripts/export_session_turns_with_flags.py --flagged-only        # skip CLEAN sessions
  python scripts/export_session_turns_with_flags.py --parts 6             # chat into 6 CSVs
  python scripts/export_session_turns_with_flags.py --db chat             # only the chat DB
  python scripts/export_session_turns_with_flags.py --db audio            # only the audio DB
  python scripts/export_session_turns_with_flags.py --report-unlinked

--parts N splits the CHAT CSV output into N files (name_part1of6.csv ...),
dividing sessions evenly across the files at whole-session boundaries — every
turn of a session always lands in the same file, so no session is split across
two CSVs. The audio DB is always written as a single file.
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
from store.audio_db import AUDIO_DB_PATH  # noqa: E402


def get_readonly_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Open a DB strictly read-only (mode=ro): SQLite rejects any write, so this
    export can never modify the database. A live app is undisturbed. Defaults to
    the chat DB; pass AUDIO_DB_PATH for the audio DB.
    No row_factory — plain tuples stream fastest into csv.writerows.

    Tuned for a large (multi-GB) DB: memory-map the file so reads come from the
    OS page cache instead of syscalls, and give SQLite a bigger page cache. Both
    are session-local PRAGMAs — they change nothing on disk. Failures (e.g. mmap
    unsupported) are non-fatal; the export just runs a little slower."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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

def build_session_count_sql(flagged_only: bool = False) -> str:
    """Number of DISTINCT sessions in scope. Used to divide sessions evenly across
    --parts files. Matches the export's session set (turns JOIN sessions)."""
    if not flagged_only:
        return "SELECT COUNT(DISTINCT session_id) FROM turns"
    return f"""
        SELECT COUNT(DISTINCT t.session_id)
        FROM turns t
        JOIN sessions s ON s.session_id = t.session_id
        {_FLAGGED_WHERE}
    """


# --- Audio DB (store/audio_review.db) ---------------------------------------
# Same shape as the chat export, mapped onto the audio schema:
#   sessions -> audio_sessions (s_id), turns -> audio_segments (seg_id),
#   flags -> audio_flags (intent = category). Audio segments carry no message
#   text; the per-segment fields are speaker / tone / ts_start / ts_end. Audio
#   session verdicts are binary (SEVERE is legacy for FLAGGED), normalised here.
AUDIO_HEADER = [
    # session-level context (repeated on every segment of the session)
    "s_id", "overall_verdict", "astrotalk_verdict", "review_status",
    # segment-level ("turn" of an audio session)
    "seg_id", "speaker", "ts_start", "ts_end", "tone",
    # flag summary for THIS segment (active flags only)
    "has_active_flag", "active_flag_count", "active_flag_intents",
]

# Normalise the legacy SEVERE verdict to FLAGGED (store/audio_db.py parity).
_AUDIO_VERDICT = "CASE WHEN {c} = 'SEVERE' THEN 'FLAGGED' ELSE {c} END"
# A flagged audio session = overall_verdict is not CLEAN (SEVERE counts as
# flagged; NULL verdicts are excluded, same intent as the chat export).
_AUDIO_FLAGGED_WHERE = "WHERE s.overall_verdict != 'CLEAN'"


def build_audio_export_sql(flagged_only: bool = False) -> str:
    """Audio counterpart of build_export_sql: aggregate each segment's ACTIVE
    flags, then LEFT JOIN onto every segment so unflagged segments still appear.
    Active-flag rule matches store/audio_db.py: not DISMISSED and not an amended
    original (an original superseded by an amendment row does not count)."""
    where = _AUDIO_FLAGGED_WHERE if flagged_only else ""
    overall = _AUDIO_VERDICT.format(c="s.overall_verdict")
    astro = _AUDIO_VERDICT.format(c="s.astrotalk_verdict")
    return f"""
    WITH active AS (
        SELECT af.s_id, af.seg_id, af.intent
        FROM audio_flags af
        WHERE (af.status IS NULL OR af.status != 'DISMISSED')
          AND af.seg_id IS NOT NULL
          AND af.flag_id NOT IN (
              SELECT parent_flag_id FROM audio_flags WHERE parent_flag_id IS NOT NULL
          )
    ),
    agg AS (
        SELECT s_id, seg_id,
               COUNT(*)                    AS cnt,
               group_concat(intent, ', ')  AS intents
        FROM active
        GROUP BY s_id, seg_id
    )
    SELECT s.s_id, {overall} AS overall_verdict, {astro} AS astrotalk_verdict,
           s.review_status,
           t.seg_id, t.speaker, t.ts_start, t.ts_end, t.tone,
           CASE WHEN a.cnt IS NULL THEN 0 ELSE 1 END AS has_active_flag,
           COALESCE(a.cnt, 0)                        AS active_flag_count,
           COALESCE(a.intents, '')                   AS active_flag_intents
    FROM audio_segments t
    JOIN audio_sessions s ON s.s_id = t.s_id
    LEFT JOIN agg a ON a.s_id = t.s_id AND a.seg_id = t.seg_id
    {where}
    ORDER BY s.s_id, t.seg_id
"""


def build_audio_count_sql(flagged_only: bool = False) -> str:
    """Segment-row count for the summary line."""
    if not flagged_only:
        return "SELECT COUNT(*) FROM audio_segments"
    return f"""
        SELECT COUNT(*)
        FROM audio_segments t
        JOIN audio_sessions s ON s.s_id = t.s_id
        {_AUDIO_FLAGGED_WHERE}
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


def _part_paths(out_path, parts):
    """Per-part output paths, e.g. foo.csv -> foo_part1of6.csv ... foo_part6of6.csv."""
    if parts <= 1:
        return [out_path]
    return [
        out_path.with_name(f"{out_path.stem}_part{i + 1}of{parts}{out_path.suffix}")
        for i in range(parts)
    ]


def write_csv(conn, out_path, export_sql, header, count_sql,
              session_count_sql=None, parts=1):
    """Stream an export cursor to CSV, splitting into `parts` files at whole-
    session boundaries.

    parts == 1: csv.writerows consumes the cursor at C speed — no per-row Python.
    parts > 1 : the export is ORDER BY <session id>, so a session's rows are
    contiguous; we bump a session counter on each id change and route by
    (index * parts // total) — balanced, contiguous chunks with every row of a
    session in one file. Needs session_count_sql to know the divisor.

    Returns (total_rows, per_part_row_counts, paths).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cur = conn.execute(export_sql)

    # utf-8-sig so Excel renders Hindi/other non-ASCII correctly.
    # 1 MB buffer so a multi-million-row write isn't dominated by tiny syscalls.
    if parts <= 1:
        with open(out_path, "w", newline="", encoding="utf-8-sig", buffering=1 << 20) as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            writer.writerows(cur)
        total = conn.execute(count_sql).fetchone()[0]
        return total, [total], [out_path]

    total_sessions = conn.execute(session_count_sql).fetchone()[0]
    paths = _part_paths(out_path, parts)
    files, writers = [], []
    for p in paths:
        fh = open(p, "w", newline="", encoding="utf-8-sig", buffering=1 << 20)
        w = csv.writer(fh)
        w.writerow(header)
        files.append(fh)
        writers.append(w)

    counts = [0] * parts
    total_rows = 0
    session_index = -1
    _UNSET = object()
    prev_sid = _UNSET
    try:
        for row in cur:
            sid = row[0]
            if sid != prev_sid:
                session_index += 1
                prev_sid = sid
            part = (session_index * parts) // total_sessions if total_sessions else 0
            if part >= parts:
                part = parts - 1
            writers[part].writerow(row)
            counts[part] += 1
            total_rows += 1
    finally:
        for fh in files:
            fh.close()
    return total_rows, counts, paths


def _print_result(title, db_path, flagged_only, n, counts, paths, unit, unlinked=None):
    print("=" * 64)
    print(f"  {title}")
    print(f"  DB    : {db_path}")
    print(f"  Scope : {'flagged sessions only (verdict != CLEAN)' if flagged_only else 'all sessions'}")
    if len(paths) == 1:
        print(f"  Out   : {paths[0]}  ({n:,} {unit} rows)")
    else:
        print(f"  Out   : {len(paths)} parts, {n:,} {unit} rows total")
        for p, c in zip(paths, counts):
            print(f"          {p}  ({c:,} rows)")
    if unlinked is not None:
        print(f"  Active flags with no resolvable turn (not in export): {unlinked:,}")
    print("=" * 64)


def export_chat(out_path, flagged_only, parts, report_unlinked):
    """Chat DB -> per-turn CSV, split into `parts` files at session boundaries."""
    conn = get_readonly_connection(DB_PATH)
    try:
        unlinked = conn.execute(COUNT_UNLINKED_SQL).fetchone()[0] if report_unlinked else None
        n, counts, paths = write_csv(
            conn, out_path,
            export_sql=build_export_sql(flagged_only),
            header=HEADER,
            count_sql=build_export_count_sql(flagged_only),
            session_count_sql=build_session_count_sql(flagged_only),
            parts=parts,
        )
    finally:
        conn.close()
    _print_result("Chat: session turns + active-flag markers", DB_PATH,
                  flagged_only, n, counts, paths, "turn", unlinked)


def export_audio(out_path, flagged_only):
    """Audio DB -> per-segment CSV, always a single file."""
    conn = get_readonly_connection(AUDIO_DB_PATH)
    try:
        n, counts, paths = write_csv(
            conn, out_path,
            export_sql=build_audio_export_sql(flagged_only),
            header=AUDIO_HEADER,
            count_sql=build_audio_count_sql(flagged_only),
            parts=1,
        )
    finally:
        conn.close()
    _print_result("Audio: session segments + active-flag markers", AUDIO_DB_PATH,
                  flagged_only, n, counts, paths, "segment")


def main():
    parser = argparse.ArgumentParser(
        description="Export every session's turns/segments with a per-turn active-flag "
                    "marker (CSV only). Chat DB is split into --parts files; audio DB "
                    "is a single file."
    )
    parser.add_argument("--db", choices=("both", "chat", "audio"), default="both",
                        help="Which database(s) to export (default: both).")
    parser.add_argument("--out", default="exports/session_turns_with_flags.csv",
                        help="Chat CSV path (default: exports/session_turns_with_flags.csv).")
    parser.add_argument("--audio-out", default="exports/audio_session_segments_with_flags.csv",
                        help="Audio CSV path (default: exports/audio_session_segments_with_flags.csv).")
    parser.add_argument("--report-unlinked", action="store_true",
                        help="Also print how many active chat flags have no resolvable turn.")
    parser.add_argument("--flagged-only", action="store_true",
                        help="Export only flagged sessions (overall_verdict != 'CLEAN'); "
                             "skip CLEAN sessions entirely.")
    parser.add_argument("--parts", type=int, default=6,
                        help="Split the CHAT CSV into N files at whole-session boundaries "
                             "(default: 6). The audio CSV is always a single file.")
    args = parser.parse_args()

    if args.parts < 1:
        parser.error("--parts must be >= 1")

    if args.db in ("both", "chat"):
        export_chat(Path(args.out), args.flagged_only, args.parts, args.report_unlinked)
    if args.db in ("both", "audio"):
        export_audio(Path(args.audio_out), args.flagged_only)


if __name__ == "__main__":
    main()
