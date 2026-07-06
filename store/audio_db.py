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

    # Columns added after the initial schema — same pattern as store/db.py:
    # they live here (not in audio_schema.sql) so existing DBs get them too.
    migrations = [
        "ALTER TABLE audio_sessions ADD COLUMN audio_url TEXT",         # HLS (.m3u8) recording URL
        "ALTER TABLE audio_flags ADD COLUMN reasoning TEXT",            # reviewer note on amendments
        "ALTER TABLE audio_sessions ADD COLUMN confidence_score REAL",  # verdict confidence, same scale as chat
        # Flag-level audit trail, same as chat's flags table
        "ALTER TABLE audio_flags ADD COLUMN confirmed_by TEXT",
        "ALTER TABLE audio_flags ADD COLUMN confirmed_at TEXT",
        "ALTER TABLE audio_flags ADD COLUMN created_by TEXT",           # reviewer who made an amendment
    ]
    with get_audio_connection() as conn:
        for migration in migrations:
            try:
                conn.execute(migration)
                conn.commit()
            except Exception:
                pass  # Column already exists — safe to ignore

    print(f"Audio database initialised at {AUDIO_DB_PATH}")


def fetch_audio_sessions_page(
    *,
    status: str = None,
    search: str = None,
    reviewer_role: str = None,
    reviewer_name: str = None,
    assigned_to: str = None,
    sort_col: str = None,
    sort_dir: str = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Paginated audio session list with per-session flag/segment counts.
    Role-based default visibility mirrors the chat DB (fetch_sessions_page):
    - L1 (no explicit status filter): submitted/locked sessions hidden
    - L2 (no explicit status filter): locked sessions hidden
    - L1 with a name: only sessions assigned to them
    """
    where, params = ["1=1"], []
    if status:
        where.append("s.review_status = ?"); params.append(status)
    else:
        if reviewer_role == "L1":
            where.append("s.review_status NOT IN ('SUBMITTED_FOR_REVIEW','LOCKED')")
        elif reviewer_role == "L2":
            where.append("s.review_status != 'LOCKED'")

    if reviewer_role == "L1" and reviewer_name:
        where.append("s.assigned_to = ?"); params.append(reviewer_name)
    elif assigned_to:
        where.append("s.assigned_to = ?"); params.append(assigned_to)

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
    # Safe columns for sorting
    valid_cols = {
        's_id': 's.s_id',
        'duration': 'duration_seconds',
        'segments': 'segment_count',
        'flags': 'flag_count',
        'verdict': 's.overall_verdict',
        'status': 's.review_status',
    }
    
    status_sort = "CASE WHEN s.review_status = 'SUBMITTED_FOR_REVIEW' THEN 0 ELSE 1 END, " if reviewer_role == "L2" else ""
    
    order_clause = f"ORDER BY {status_sort}s.s_id ASC"
    if sort_col and sort_col in valid_cols:
        col_expr = valid_cols[sort_col]
        direction = "DESC" if sort_dir == "desc" else "ASC"
        order_clause = f"ORDER BY {status_sort}{col_expr} {direction}, s.s_id ASC"

    with get_audio_connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT s.*,
                       COALESCE(fc.flag_count, 0)    AS flag_count,
                       COALESCE(sc.segment_count, 0) AS segment_count,
                       COALESCE(sc.duration_seconds, 0) AS duration_seconds
                {base}
                {order_clause}
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


def _active_audio_flag_rows(rows) -> list:
    """Active flags = amendment rows + original rows that have no amendment.
    Same model as the chat flags table."""
    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}
    return [
        r for r in rows
        if r["parent_flag_id"] is not None or r["flag_id"] not in amended_parents
    ]


def recompute_audio_session_verdict(s_id: int, conn) -> str:
    """Recompute and persist overall_verdict + confidence_score from the
    session's active flags, using the SAME rules as the chat DB
    (engine/verdict_rules.py): SEVERE / FLAGGED / CLEAN, including the
    flagged-combination escalations."""
    from engine.verdict_rules import get_db_verdict_for_flags, get_db_confidence_for_verdict

    rows = conn.execute(
        "SELECT flag_id, intent, status, parent_flag_id FROM audio_flags WHERE s_id = ?", (s_id,)
    ).fetchall()
    codes = [r["intent"] for r in _active_audio_flag_rows(rows) if r["intent"] and r["status"] != "DISMISSED"]
    verdict    = get_db_verdict_for_flags(codes)
    confidence = get_db_confidence_for_verdict(verdict)
    conn.execute(
        "UPDATE audio_sessions SET overall_verdict = ?, confidence_score = ? WHERE s_id = ?",
        (verdict, confidence, s_id),
    )
    return verdict


def get_audio_flag_summary(s_id: int) -> dict:
    """Flag counts for the submit gate — same semantics as the chat workflow:
    a session can be submitted only when every active flag is CONFIRMED
    (dismissed flags are deleted, so they don't count)."""
    with get_audio_connection() as conn:
        rows = conn.execute(
            "SELECT flag_id, status, parent_flag_id FROM audio_flags WHERE s_id = ?",
            (s_id,),
        ).fetchall()
    active = _active_audio_flag_rows(rows)
    total = len(active)
    actioned = sum(1 for r in active if r["status"] in ("CONFIRMED", "DISMISSED"))
    return {
        "total_flags":      total,
        "actioned_flags":   actioned,
        "unactioned_flags": total - actioned,
        "can_submit":       total == actioned,
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
    # Same transition as the chat DB: unlock lands on REVIEWED, not back on
    # SUBMITTED_FOR_REVIEW (see store/db.py unlock_session).
    with get_audio_connection() as conn:
        conn.execute(
            """UPDATE audio_sessions
               SET review_status = 'REVIEWED',
                   locked_by     = NULL,
                   locked_at     = NULL
               WHERE s_id = ?""",
            (s_id,),
        )
