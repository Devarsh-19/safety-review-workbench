"""SQLite connection and query helpers"""

import os
import sqlite3
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = str(_PROJECT_ROOT / os.getenv("DB_PATH", "store/astrotalk.db"))
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection() -> sqlite3.Connection:
    # timeout + WAL + busy_timeout: concurrent reviewers on the LAN. WAL lets
    # readers and the writer proceed in parallel; busy_timeout makes a second
    # writer wait instead of failing with "database is locked".
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def initialise_db() -> None:
    # schema.sql contains ONLY base tables + indexes on always-present columns.
    # Indexes on migration-added columns (assigned_to, status, parent_flag_id)
    # live in the migrations list below, so executescript here never references
    # a not-yet-added column. executescript correctly handles SQL comments and
    # multi-statement scripts (a naive split on ';' would break on semicolons
    # inside comments).
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    with get_connection() as conn:
        conn.executescript(schema)

    migrations = [
        "ALTER TABLE sessions ADD COLUMN session_note TEXT",
        "ALTER TABLE turns ADD COLUMN is_automated INTEGER DEFAULT 0",
        "ALTER TABLE sessions ADD COLUMN session_date TEXT",
        "ALTER TABLE sessions ADD COLUMN month TEXT",
        "ALTER TABLE sessions ADD COLUMN language_code TEXT",
        "ALTER TABLE turns ADD COLUMN has_link INTEGER DEFAULT 0",
        "ALTER TABLE sessions ADD COLUMN locked_by TEXT",
        "ALTER TABLE sessions ADD COLUMN locked_at TEXT",
        # Session submission and assignment columns (from dev_new workflow)
        "ALTER TABLE sessions ADD COLUMN submitted_by TEXT",
        "ALTER TABLE sessions ADD COLUMN submitted_at TEXT",
        "ALTER TABLE sessions ADD COLUMN needs_final_review INTEGER DEFAULT 0",
        "ALTER TABLE sessions ADD COLUMN assigned_to TEXT",
        # Flag source/status refactor — replaces AMENDED/DISMISSED detection_layer
        "ALTER TABLE flags ADD COLUMN source TEXT",
        "ALTER TABLE flags ADD COLUMN status TEXT DEFAULT 'ACTIVE'",
        "ALTER TABLE flags ADD COLUMN parent_flag_id INTEGER",
        # Back-fill source from detection_layer for existing rows
        "UPDATE flags SET source = detection_layer WHERE source IS NULL AND detection_layer IN ('LLM','REGEX','MANUAL')",
        "UPDATE flags SET source = 'MANUAL', status = 'ACTIVE' WHERE source IS NULL AND detection_layer = 'AMENDED'",
        # Hard-delete any legacy DISMISSED rows — no longer kept
        "DELETE FROM flags WHERE detection_layer = 'DISMISSED'",
        # Default status for any remaining rows that have no status
        "UPDATE flags SET status = 'ACTIVE' WHERE status IS NULL",
        # Indexes on migration-added columns — created AFTER the column exists
        "CREATE INDEX IF NOT EXISTS idx_sessions_assigned_to ON sessions(assigned_to)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_assigned_status ON sessions(assigned_to, review_status)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_assigned_verdict ON sessions(assigned_to, overall_verdict)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_review_assigned ON sessions(review_status, assigned_to)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_astro_assigned ON sessions(astrotalk_flagged, assigned_to)",
        "CREATE INDEX IF NOT EXISTS idx_flags_parent_flag_id ON flags(parent_flag_id)",
        "CREATE INDEX IF NOT EXISTS idx_flags_status ON flags(status)",
        "CREATE INDEX IF NOT EXISTS idx_flags_session_source_status ON flags(session_id, source, status)",
        "CREATE INDEX IF NOT EXISTS idx_flags_session_category ON flags(session_id, category_code)",
    ]

    with get_connection() as conn:
        for migration in migrations:
            try:
                conn.execute(migration)
                conn.commit()
            except Exception:
                pass  # Column already exists or no-op — safe to ignore

    print(f"Database initialised at {DB_PATH}")


def recompute_session_verdict(session_id: str, conn) -> str:
    """
    Recompute and persist overall_verdict for a session based on its current
    active flags. Uses the amendment row if one exists for a flag, otherwise
    uses the original row. Returns the new verdict string.
    """
    from engine.verdict_rules import get_db_verdict_for_flags, get_db_confidence_for_verdict

    # Fetch all flags for this session — exclude amendment children from the
    # base query; we'll pick them up via parent_flag_id logic below.
    rows = conn.execute(
        """SELECT flag_id, category_code, source, status, parent_flag_id
           FROM flags WHERE session_id = ?""",
        (session_id,),
    ).fetchall()

    # Build active category list:
    # 1. Collect parent flags (no parent_flag_id)
    # 2. If a parent has an amendment child, use the child's category_code
    # 3. If a parent has no amendment, use the parent's own category_code
    parent_ids_with_amendment = {
        r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None
    }
    active_codes = []
    for r in rows:
        if (r["status"] or "") == "DISMISSED":
            continue
        if r["parent_flag_id"] is not None:
            # This is an amendment row — it is the active version; include it
            active_codes.append(r["category_code"])
        elif r["flag_id"] not in parent_ids_with_amendment:
            # Original row with no amendment — it is the active version
            active_codes.append(r["category_code"])
        # else: original row that has been amended — skip, amendment already included

    verdict    = get_db_verdict_for_flags(active_codes)
    confidence = get_db_confidence_for_verdict(verdict)

    conn.execute(
        "UPDATE sessions SET overall_verdict = ?, confidence_score = ? WHERE session_id = ?",
        (verdict, confidence, session_id),
    )
    return verdict


def fetch_sessions(
    verdict_filter: str = None,
    status_filter: str = None,
    language_filter: str = None,
) -> list[dict]:
    query = "SELECT * FROM sessions WHERE 1=1"
    params: list = []

    if verdict_filter:
        query += " AND overall_verdict = ?"
        params.append(verdict_filter)
    if status_filter:
        query += " AND review_status = ?"
        params.append(status_filter)
    if language_filter:
        query += " AND language_detected = ?"
        params.append(language_filter)

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Server-side paginated + filtered + sorted session list.
# All filtering/sorting is done in SQL so LIMIT/OFFSET applies to the FULL
# filtered set (not just the current page). Returns (rows, total_count).
# ---------------------------------------------------------------------------

# Whitelist of sortable columns -> SQL expression (guards against injection).
_SORT_COLUMNS = {
    "session_id":        "s.session_id",
    "duration_minutes":  "COALESCE(s.duration_minutes, 0)",
    "turn_count":        "turn_count",
    "flag_count":        "flag_count",
    "llm_flag_count":    "llm_flag_count",
    "manual_flag_count": "manual_flag_count",
}

_SESSION_FROM = "FROM sessions s"

_VISIBLE_FLAG_SQL = "(f.status IS NULL OR f.status != 'DISMISSED')"
_FLAG_COUNT_SQL = (
    "SELECT COUNT(*) FROM flags f "
    f"WHERE f.session_id = s.session_id AND {_VISIBLE_FLAG_SQL}"
)
_LLM_FLAG_COUNT_SQL = (
    "SELECT COUNT(*) FROM flags f "
    f"WHERE f.session_id = s.session_id AND {_VISIBLE_FLAG_SQL} "
    "AND f.source IN ('LLM','REGEX')"
)
_MANUAL_FLAG_COUNT_SQL = (
    "SELECT COUNT(*) FROM flags f "
    f"WHERE f.session_id = s.session_id AND {_VISIBLE_FLAG_SQL} "
    "AND f.source = 'MANUAL'"
)
_TURN_COUNT_SQL = "SELECT COUNT(*) FROM turns t WHERE t.session_id = s.session_id"


def fetch_sessions_page(
    *,
    verdict: str = None,
    status: str = None,
    reviewer_role: str = None,
    reviewer_name: str = None,
    assigned_to: str = None,
    search: str = None,
    language: str = None,
    session_type: str = None,
    astrotalk: str = None,          # 'flagged' | 'clean' | None
    flag_category: str = None,      # only sessions carrying this flag category
    min_confidence: float = 0,      # 0-100
    min_duration=None,
    max_duration=None,
    min_turns=None,
    max_turns=None,
    sort_col: str = None,
    sort_dir: str = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where: list[str] = ["1=1"]
    params: list = []

    if verdict:
        where.append("s.overall_verdict = ?"); params.append(verdict)
    if status:
        where.append("s.review_status = ?"); params.append(status)

    # Special "Locked" login: sees ONLY locked sessions, regardless of role
    # defaults or any explicit status filter.
    if reviewer_name == "Locked":
        where.append("s.review_status = 'LOCKED'")
    # Role-based default visibility — only when no explicit status filter is
    # set AND no flag-category filter is active (filtering by flag should show
    # every matching session regardless of review status).
    # L2 works the post-submission queue: no PENDING (still with L1), no LOCKED.
    elif not status and not flag_category:
        if reviewer_role == "L1":
            where.append("s.review_status NOT IN ('SUBMITTED_FOR_REVIEW','LOCKED')")
        elif reviewer_role == "L2":
            where.append("s.review_status NOT IN ('PENDING','LOCKED')")

    # L1 sees only sessions assigned to them; L2 may filter by a specific assignee.
    if reviewer_role == "L1" and reviewer_name and reviewer_name != "Locked":
        where.append("s.assigned_to = ?"); params.append(reviewer_name)
    elif assigned_to:
        where.append("s.assigned_to = ?"); params.append(assigned_to)

    if search:
        where.append("s.session_id LIKE ?"); params.append(f"%{search}%")

    if language:
        where.append("LOWER(s.language_detected) LIKE ?"); params.append(f"%{language.lower()}%")
    # No default language allowlist: sessions of ALL languages display. Previously
    # only english/hindi/hinglish (plus unknown) were shown, so other-language
    # sessions (e.g. marathi/tamil) were hidden from the dashboard unless the user
    # explicitly typed that language into the filter.

    if session_type:
        where.append("s.session_type = ?"); params.append(session_type)

    if astrotalk == "flagged":
        where.append("s.astrotalk_flagged = 1")
    elif astrotalk == "clean":
        where.append("(s.astrotalk_flagged IS NULL OR s.astrotalk_flagged != 1)")

    if flag_category:
        where.append(
            """EXISTS (SELECT 1 FROM flags f
                       WHERE f.session_id = s.session_id
                         AND (f.status IS NULL OR f.status != 'DISMISSED')
                         AND LOWER(REPLACE(REPLACE(f.category_code,'-','_'),' ','_')) = ?)"""
        )
        params.append(flag_category.strip().lower().replace("-", "_").replace(" ", "_"))

    if min_confidence:
        where.append("COALESCE(s.confidence_score, 0) * 100 >= ?"); params.append(min_confidence)

    if min_duration not in (None, ""):
        where.append("COALESCE(s.duration_minutes, 0) >= ?"); params.append(min_duration)
    if max_duration not in (None, ""):
        where.append("COALESCE(s.duration_minutes, 0) <= ?"); params.append(max_duration)

    if min_turns not in (None, ""):
        where.append(f"COALESCE(({_TURN_COUNT_SQL}), 0) >= ?"); params.append(min_turns)
    if max_turns not in (None, ""):
        where.append(f"COALESCE(({_TURN_COUNT_SQL}), 0) <= ?"); params.append(max_turns)

    where_sql = " AND ".join(where)

    col_expr  = _SORT_COLUMNS.get(sort_col, "s.session_id")
    direction = "DESC" if str(sort_dir).lower() == "desc" else "ASC"
    order_sql = f"{col_expr} {direction}, s.session_id ASC"

    data_sql = f"""
        SELECT s.*,
               COALESCE(({_FLAG_COUNT_SQL}), 0)        AS flag_count,
               COALESCE(({_LLM_FLAG_COUNT_SQL}), 0)    AS llm_flag_count,
               COALESCE(({_MANUAL_FLAG_COUNT_SQL}), 0) AS manual_flag_count,
               COALESCE(({_TURN_COUNT_SQL}), 0)        AS turn_count
        {_SESSION_FROM}
        WHERE {where_sql}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
    """
    count_sql = f"SELECT COUNT(*) {_SESSION_FROM} WHERE {where_sql}"

    with get_connection() as conn:
        total = conn.execute(count_sql, params).fetchone()[0]
        rows  = conn.execute(data_sql, params + [limit, offset]).fetchall()
    return [dict(r) for r in rows], total


def fetch_session_detail(session_id: str) -> dict:
    with get_connection() as conn:
        session = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        turns = conn.execute(
            "SELECT * FROM turns WHERE session_id = ? ORDER BY turn_id", (session_id,)
        ).fetchall()
        flags = conn.execute(
            """SELECT * FROM flags
               WHERE session_id = ?
                 AND (status IS NULL OR status != 'DISMISSED')""",
            (session_id,),
        ).fetchall()

    return {
        "session": dict(session) if session else None,
        "turns": [dict(t) for t in turns],
        "flags": [dict(f) for f in flags],
    }


def fetch_pending_review_sessions(limit: int = 50) -> list[dict]:
    query = """
        SELECT * FROM sessions
        WHERE review_status = 'PENDING'
        ORDER BY overall_verdict DESC, created_at ASC
        LIMIT ?
    """
    with get_connection() as conn:
        rows = conn.execute(query, (limit,)).fetchall()
    return [dict(row) for row in rows]


_ACTION_STATUS_MAP = {
    "CONFIRM":            "CONFIRMED",
    "FALSE_POSITIVE":     "OVERRIDDEN",
    "NEEDS_FINAL_REVIEW": "NEEDS_FINAL_REVIEW",
    "CLEAR":              "REVIEWED",
    "SUBMIT":             "SUBMITTED_FOR_REVIEW",
    "LOCK":               "LOCKED",
}


def update_review_status(
    session_id: str,
    action: str,
    reviewer_id: str,
    note: str,
) -> None:
    valid_actions = set(_ACTION_STATUS_MAP.keys())
    if action not in valid_actions:
        raise ValueError(
            f"Invalid action '{action}'. Valid actions: {valid_actions}"
        )
    new_status = _ACTION_STATUS_MAP[action]
    query = """
        UPDATE sessions
        SET review_status = ?,
            reviewer_id   = ?,
            reviewer_note = ?,
            reviewed_at   = datetime('now')
        WHERE session_id = ?
    """
    with get_connection() as conn:
        conn.execute(query, (new_status, reviewer_id, note, session_id))


def confirm_flag(flag_id: int, reviewer_id: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """UPDATE flags
               SET is_confirmed = 1,
                   confirmed_by = ?,
                   confirmed_at = datetime('now')
               WHERE flag_id = ?""",
            (reviewer_id, flag_id),
        )


def submit_session_for_review(
    session_id: str,
    reviewer_id: str,
    note: str = None,
) -> None:
    with get_connection() as conn:
        # Recompute the verdict from active flags first — a session with no
        # active flags moves off UNPROCESSED to CLEAN.
        recompute_session_verdict(session_id, conn)
        conn.execute(
            """UPDATE sessions
               SET review_status = 'SUBMITTED_FOR_REVIEW',
                   submitted_by  = ?,
                   submitted_at  = datetime('now'),
                   reviewer_id   = ?,
                   reviewer_note = ?,
                   reviewed_at   = datetime('now')
               WHERE session_id = ?""",
            (reviewer_id, reviewer_id, note, session_id),
        )


def mark_needs_final_review(
    session_id: str,
    reviewer_id: str,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """UPDATE sessions
               SET review_status      = 'NEEDS_FINAL_REVIEW',
                   needs_final_review = 1,
                   reviewer_id        = ?
               WHERE session_id = ?""",
            (reviewer_id, session_id),
        )


def get_session_flag_summary(session_id: str) -> dict:
    """
    Returns flag counts using the new source/status/parent_flag_id model.
    Active flags = amendment rows + original rows that have no amendment.
    Actioned flags = active flags with status = CONFIRMED.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT flag_id, status, parent_flag_id FROM flags WHERE session_id = ?",
            (session_id,),
        ).fetchall()

    # Determine which flags are active (same logic as get_active_flag_codes)
    amended_parent_ids = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}
    active_rows = [
        r for r in rows
        if (r["status"] or "") != "DISMISSED"
        and (
            r["parent_flag_id"] is not None  # amendment = active
            or r["flag_id"] not in amended_parent_ids  # original with no amendment = active
        )
    ]

    total_flags    = len(active_rows)
    actioned_flags = sum(1 for r in active_rows if r["status"] == "CONFIRMED")
    unactioned     = total_flags - actioned_flags

    return {
        "total_flags":      total_flags,
        "actioned_flags":   actioned_flags,
        "unactioned_flags": unactioned,
        "can_submit":       unactioned == 0,
    }


def lock_session(session_id: str, reviewer_id: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """UPDATE sessions
               SET review_status = 'LOCKED',
                   locked_by     = ?,
                   locked_at     = datetime('now')
               WHERE session_id = ?""",
            (reviewer_id, session_id),
        )


def lock_all_submitted_sessions(reviewer_id: str) -> int:
    """Bulk-lock every session currently SUBMITTED_FOR_REVIEW in one pass.

    Same per-session effect as lock_session; returns how many were locked so
    the caller (L2 'Lock all submitted' action) can report the count.
    """
    with get_connection() as conn:
        cur = conn.execute(
            """UPDATE sessions
               SET review_status = 'LOCKED',
                   locked_by     = ?,
                   locked_at     = datetime('now')
               WHERE review_status = 'SUBMITTED_FOR_REVIEW'""",
            (reviewer_id,),
        )
        return cur.rowcount


def unlock_session(session_id: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """UPDATE sessions
               SET review_status = 'REVIEWED',
                   locked_by     = NULL,
                   locked_at     = NULL
               WHERE session_id = ?""",
            (session_id,),
        )
