"""SQLite connection and query helpers"""

import os
import sqlite3
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

DB_PATH = os.getenv("DB_PATH", "store/results.db")
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialise_db() -> None:
    # Run the base schema statement-by-statement so a single failing statement
    # (e.g. an index on a not-yet-migrated column) cannot abort the whole init.
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    with get_connection() as conn:
        for statement in schema.split(";"):
            stmt = statement.strip()
            if not stmt:
                continue
            try:
                conn.execute(stmt)
                conn.commit()
            except Exception:
                pass  # e.g. index on a column added later by migrations

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
        "CREATE INDEX IF NOT EXISTS idx_flags_parent_flag_id ON flags(parent_flag_id)",
        "CREATE INDEX IF NOT EXISTS idx_flags_status ON flags(status)",
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


def fetch_session_detail(session_id: str) -> dict:
    with get_connection() as conn:
        session = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        turns = conn.execute(
            "SELECT * FROM turns WHERE session_id = ? ORDER BY turn_id", (session_id,)
        ).fetchall()
        flags = conn.execute(
            "SELECT * FROM flags WHERE session_id = ?", (session_id,)
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
        conn.execute(
            """UPDATE sessions
               SET review_status = 'SUBMITTED_FOR_REVIEW',
                   submitted_by  = ?,
                   submitted_at  = datetime('now'),
                   reviewer_id   = ?,
                   reviewer_note = ?
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
        if r["parent_flag_id"] is not None  # amendment = active
        or r["flag_id"] not in amended_parent_ids  # original with no amendment = active
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
