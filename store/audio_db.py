"""SQLite connection and query helpers for the audio review database.

Separate universe from the chat DB (store/db.py): its own file, own env var
(AUDIO_DB_PATH), same workflow model (PENDING -> SUBMITTED_FOR_REVIEW -> LOCKED).
"""

import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIO_DB_PATH = str(_PROJECT_ROOT / os.getenv("AUDIO_DB_PATH", "store/audio_review.db"))
_SCHEMA_PATH = Path(__file__).parent / "audio_schema.sql"

SPEAKER_ROLES = ("ASTROLOGER", "USER")


def get_audio_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(AUDIO_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialise_audio_db() -> None:
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    with get_audio_connection() as conn:
        conn.executescript(schema)
    print(f"Audio database initialised at {AUDIO_DB_PATH}")


def fetch_audio_sessions_page(
    *,
    status: str = None,
    search: str = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Paginated audio session list with per-session flag/segment counts."""
    where, params = ["1=1"], []
    if status:
        where.append("s.review_status = ?"); params.append(status)
    if search:
        where.append("CAST(s.s_id AS TEXT) LIKE ?"); params.append(f"%{search}%")
    where_sql = " AND ".join(where)

    base = f"""
        FROM audio_sessions s
        LEFT JOIN (
            SELECT s_id, COUNT(*) AS flag_count FROM audio_flags GROUP BY s_id
        ) fc ON fc.s_id = s.s_id
        LEFT JOIN (
            SELECT s_id, COUNT(*) AS segment_count, MAX(ts_end) AS duration_seconds
            FROM audio_segments GROUP BY s_id
        ) sc ON sc.s_id = s.s_id
        WHERE {where_sql}
    """
    with get_audio_connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT s.*,
                       COALESCE(fc.flag_count, 0)    AS flag_count,
                       COALESCE(sc.segment_count, 0) AS segment_count,
                       COALESCE(sc.duration_seconds, 0) AS duration_seconds
                {base}
                ORDER BY s.s_id ASC
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
    return [dict(r) for r in rows], total


def fetch_audio_session_detail(s_id: int) -> dict:
    with get_audio_connection() as conn:
        session = conn.execute(
            "SELECT * FROM audio_sessions WHERE s_id = ?", (s_id,)
        ).fetchone()
        segments = conn.execute(
            "SELECT * FROM audio_segments WHERE s_id = ? ORDER BY seg_id", (s_id,)
        ).fetchall()
        flags = conn.execute(
            "SELECT * FROM audio_flags WHERE s_id = ? ORDER BY seg_id, flag_id", (s_id,)
        ).fetchall()
    return {
        "session": dict(session) if session else None,
        "segments": [dict(s) for s in segments],
        "flags": [dict(f) for f in flags],
    }


def set_speaker_roles(s_id: int, speaker1_role: str, speaker2_role: str) -> None:
    """Persist the reviewer's speaker->role assignment. Roles must be opposite."""
    if speaker1_role not in SPEAKER_ROLES or speaker2_role not in SPEAKER_ROLES:
        raise ValueError(f"Roles must be one of {SPEAKER_ROLES}")
    if speaker1_role == speaker2_role:
        raise ValueError("Speaker 1 and Speaker 2 cannot have the same role")
    with get_audio_connection() as conn:
        conn.execute(
            "UPDATE audio_sessions SET speaker1_role = ?, speaker2_role = ? WHERE s_id = ?",
            (speaker1_role, speaker2_role, s_id),
        )


def submit_audio_session(s_id: int, reviewer_id: str, note: str = None) -> None:
    with get_audio_connection() as conn:
        conn.execute(
            """UPDATE audio_sessions
               SET review_status = 'SUBMITTED_FOR_REVIEW',
                   submitted_by  = ?,
                   submitted_at  = datetime('now'),
                   reviewer_id   = ?,
                   reviewer_note = COALESCE(?, reviewer_note),
                   reviewed_at   = datetime('now')
               WHERE s_id = ?""",
            (reviewer_id, reviewer_id, note, s_id),
        )


def lock_audio_session(s_id: int, reviewer_id: str) -> None:
    with get_audio_connection() as conn:
        conn.execute(
            """UPDATE audio_sessions
               SET review_status = 'LOCKED',
                   locked_by     = ?,
                   locked_at     = datetime('now')
               WHERE s_id = ?""",
            (reviewer_id, s_id),
        )


def unlock_audio_session(s_id: int) -> None:
    with get_audio_connection() as conn:
        conn.execute(
            """UPDATE audio_sessions
               SET review_status = 'SUBMITTED_FOR_REVIEW',
                   locked_by     = NULL,
                   locked_at     = NULL
               WHERE s_id = ?""",
            (s_id,),
        )
