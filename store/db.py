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
        # Flag confirmation columns
        "ALTER TABLE flags ADD COLUMN confirmed_by TEXT",
        "ALTER TABLE flags ADD COLUMN confirmed_at TEXT",
        "ALTER TABLE flags ADD COLUMN is_confirmed INTEGER DEFAULT 0",
        # Session submission columns
        "ALTER TABLE sessions ADD COLUMN submitted_by TEXT",
        "ALTER TABLE sessions ADD COLUMN submitted_at TEXT",
        "ALTER TABLE sessions ADD COLUMN needs_final_review INTEGER DEFAULT 0",
    ]

    with get_connection() as conn:
        for migration in migrations:
            try:
                conn.execute(migration)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists — safe to skip

    print(f"Database initialised at {DB_PATH}")


def fetch_sessions(
    verdict_filter: str = None,
    status_filter: str = None,
    language_filter: str = None,
    reviewer_filter: str = None,
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
    if reviewer_filter:
        query += " AND reviewer_id = ?"
        params.append(reviewer_filter)

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
    if action == "CLEAR":
        query = """
            UPDATE sessions
            SET review_status = ?,
                overall_verdict = 'CLEAN',
                confidence_score = 0.0,
                reviewer_id   = ?,
                reviewer_note = ?,
                reviewed_at   = datetime('now')
            WHERE session_id = ?
        """
    else:
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
    with get_connection() as conn:
        total_flags = conn.execute(
            """SELECT COUNT(*) FROM flags
               WHERE session_id = ?
                 AND detection_layer NOT IN ('DISMISSED')""",
            (session_id,),
        ).fetchone()[0]

        actioned_flags = conn.execute(
            """SELECT COUNT(*) FROM flags
               WHERE session_id = ?
                 AND detection_layer NOT IN ('DISMISSED')
                 AND (is_confirmed = 1 OR detection_layer = 'AMENDED')""",
            (session_id,),
        ).fetchone()[0]

    unactioned_flags = total_flags - actioned_flags
    return {
        "total_flags":      total_flags,
        "actioned_flags":   actioned_flags,
        "unactioned_flags": unactioned_flags,
        "can_submit":       unactioned_flags == 0,
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
