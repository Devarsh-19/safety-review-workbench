"""Writes engine output to the results store"""

import logging
import sqlite3

from store.db import get_connection, update_review_status

logger = logging.getLogger(__name__)


def write_session(session_data: dict) -> None:
    columns = ", ".join(session_data.keys())
    placeholders = ", ".join("?" * len(session_data))
    query = f"INSERT OR REPLACE INTO sessions ({columns}) VALUES ({placeholders})"
    with get_connection() as conn:
        conn.execute(query, list(session_data.values()))


def write_turns(session_id: str, turns: list[dict]) -> None:
    query = """
        INSERT OR REPLACE INTO turns
            (session_id, turn_id, speaker, message_text, timestamp, language_detected)
        VALUES (?, ?, ?, ?, ?, ?)
    """
    rows = [
        (
            session_id,
            t["turn_id"],
            t["speaker"],
            t["message_text"],
            t.get("timestamp"),
            t.get("language_detected"),
        )
        for t in turns
    ]
    with get_connection() as conn:
        conn.executemany(query, rows)


def write_flags(session_id: str, flags: list[dict]) -> None:
    query = """
        INSERT OR IGNORE INTO flags
            (session_id, turn_id, category_code, detection_layer, source, status,
             severity, confidence_score, reasoning, false_positive_risk, pattern_matched)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    rows = [
        (
            session_id,
            f.get("turn_id"),
            f.get("category_code"),
            f.get("detection_layer"),
            # source: prefer explicit field, fall back to detection_layer for compat
            f.get("source") or f.get("detection_layer"),
            f.get("status", "ACTIVE"),
            f.get("severity"),
            f.get("confidence_score"),
            f.get("reasoning"),
            f.get("false_positive_risk"),
            f.get("pattern_matched"),
        )
        for f in flags
    ]
    with get_connection() as conn:
        conn.executemany(query, rows)


def write_review_action(
    session_id: str,
    flag_id: int,
    action: str,
    reviewer_id: str,
    note: str,
) -> None:
    query = """
        INSERT INTO review_log (session_id, flag_id, action, reviewer_id, note)
        VALUES (?, ?, ?, ?, ?)
    """
    with get_connection() as conn:
        conn.execute(query, (session_id, flag_id, action, reviewer_id, note))
    update_review_status(session_id, action, reviewer_id, note)


def write_session_complete(
    session_id: str,
    session_data: dict,
    turns: list[dict],
    flags: list[dict] = None,
) -> None:
    if flags is None:
        flags = []
    conn = get_connection()
    try:
        with conn:
            # write session
            s_cols = ", ".join(session_data.keys())
            s_placeholders = ", ".join("?" * len(session_data))
            conn.execute(
                f"INSERT OR REPLACE INTO sessions ({s_cols}) VALUES ({s_placeholders})",
                list(session_data.values()),
            )

            # write turns
            conn.executemany(
                """
                INSERT OR REPLACE INTO turns
                    (session_id, turn_id, speaker, message_text, timestamp,
                     language_detected, is_automated, has_link)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        session_id,
                        t["turn_id"],
                        t["speaker"],
                        t["message_text"],
                        t.get("timestamp"),
                        t.get("language_detected"),
                        t.get("is_automated", 0),
                        t.get("has_link", 0),
                    )
                    for t in turns
                ],
            )

            # write flags — skipped when list is empty (ingestion-only mode)
            if flags:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO flags
                        (session_id, turn_id, category_code, detection_layer, source, status,
                         severity, confidence_score, reasoning, false_positive_risk, pattern_matched)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            session_id,
                            f.get("turn_id"),
                            f.get("category_code"),
                            f.get("detection_layer"),
                            f.get("source") or f.get("detection_layer"),
                            f.get("status", "ACTIVE"),
                            f.get("severity"),
                            f.get("confidence_score"),
                            f.get("reasoning"),
                            f.get("false_positive_risk"),
                            f.get("pattern_matched"),
                        )
                        for f in flags
                    ],
                )
    except sqlite3.Error as exc:
        logger.error("write_session_complete failed for session %s: %s", session_id, exc)
        raise
    finally:
        conn.close()
