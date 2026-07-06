"""FastAPI review interface for AstroTalk content safety workbench"""

from __future__ import annotations

import csv
import io
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Ensure project root is importable when running from any working directory
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv
load_dotenv()

from store.db import (
    DB_PATH,
    get_connection,
    fetch_sessions,
    fetch_sessions_page,
    fetch_pending_review_sessions,
    update_review_status,
    submit_session_for_review,
    mark_needs_final_review,
    get_session_flag_summary,
    lock_session,
    unlock_session,
    initialise_db,
    recompute_session_verdict,
)
from store.writer import write_review_action
from engine.verdict_rules import get_db_verdict_for_flags


# ---------------------------------------------------------------------------
# Helper — derive a flag's severity from its category's verdict class.
# SEVERE category -> HIGH, FLAGGED category -> MEDIUM, CLEAN -> LOW.
# Used so manually-tagged flags get the correct severity (NSFW etc. = HIGH)
# instead of a hardcoded MEDIUM.
# ---------------------------------------------------------------------------
def _severity_for_category(category_code: str) -> str:
    verdict = get_db_verdict_for_flags([category_code])
    return {"SEVERE": "HIGH", "FLAGGED": "MEDIUM", "CLEAN": "LOW"}.get(verdict, "MEDIUM")


# ---------------------------------------------------------------------------
# "Flagged by us" definition for /stats — based on scripts/diag_export_funnel.py:
# only active (non-amended-parent) MANUAL/LLM flags count, excluded categories
# never count at all, and a session whose remaining flags are ALL low-signal
# categories is not counted.
# ---------------------------------------------------------------------------
_EXCLUDED_CATEGORIES = (
    "re_engagement_solicitation",
    "personal_data_collection",
)
_DROP_ONLY_CATEGORIES = (
    "fake_remedies",
    "instigation",
    "fear_manipulation",
    "financial_solicitation",
    "off_platform_solicitation",
)
_NORM_CAT    = "LOWER(REPLACE(REPLACE(f.category_code,'-','_'),' ','_'))"
_ACTIVE_FLAG = ("f.flag_id NOT IN "
                "(SELECT parent_flag_id FROM flags WHERE parent_flag_id IS NOT NULL)")
_EXCL_LIST   = ",".join(f"'{c}'" for c in _EXCLUDED_CATEGORIES)
_DROP_LIST   = ",".join(f"'{c}'" for c in _DROP_ONLY_CATEGORIES)
_FLAGGED_BY_US_SQL = f"""(
    EXISTS (SELECT 1 FROM flags f
            WHERE f.session_id = sessions.session_id
              AND f.source IN ('MANUAL','LLM')
              AND {_NORM_CAT} NOT IN ({_EXCL_LIST})
              AND {_ACTIVE_FLAG})
    AND EXISTS (SELECT 1 FROM flags f
            WHERE f.session_id = sessions.session_id
              AND {_NORM_CAT} NOT IN ({_EXCL_LIST})
              AND {_NORM_CAT} NOT IN ({_DROP_LIST})
              AND {_ACTIVE_FLAG})
)"""


# ---------------------------------------------------------------------------
# Lifespan — initialise DB (including session_note migration) on startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        initialise_db()
    except Exception:
        pass
    yield


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AstroTalk Review Workbench",
    description="Content safety review interface — GT Bharat",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_VALID_ACTIONS = {"CONFIRM", "FALSE_POSITIVE", "NEEDS_FINAL_REVIEW", "CLEAR"}


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class ReviewRequest(BaseModel):
    action: str
    reviewer_id: str
    note: str = ""
    flag_id: Optional[int] = None


class ManualFlagRequest(BaseModel):
    turn_id: Optional[int] = None
    category_code: str
    note: str
    reviewer_id: str
    message_text: str


class SessionNoteRequest(BaseModel):
    note: str
    reviewer_id: str


class AmendFlagRequest(BaseModel):
    category_code: str
    severity: str
    reasoning: str
    reviewer_id: str


class DismissFlagRequest(BaseModel):
    reviewer_id: str
    note: str


class LockRequest(BaseModel):
    reviewer_id: str


class SubmitRequest(BaseModel):
    reviewer_id: str
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints — health + aggregate stats
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "db": DB_PATH}


@app.get("/stats")
def stats(
    reviewer_name: Optional[str] = None,
    reviewer_role: Optional[str] = None,
):
    # L1: scope all counts to sessions assigned to this reviewer only
    if reviewer_role == 'L1' and reviewer_name:
        scope  = " AND assigned_to = ?"
        params = (reviewer_name,)
    else:
        scope  = ""
        params = ()

    try:
        with get_connection() as conn:
            total       = conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE 1=1{scope}", params
            ).fetchone()[0]
            pending     = conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE review_status = 'PENDING'{scope}", params
            ).fetchone()[0]
            reviewed    = conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE review_status != 'PENDING'{scope}", params
            ).fetchone()[0]
            severe      = conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE overall_verdict = 'SEVERE'{scope}", params
            ).fetchone()[0]
            flagged     = conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE overall_verdict = 'FLAGGED'{scope}", params
            ).fetchone()[0]
            clean       = conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE overall_verdict = 'CLEAN'{scope}", params
            ).fetchone()[0]
            unprocessed = conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE overall_verdict = 'UNPROCESSED'{scope}", params
            ).fetchone()[0]
            locked      = conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE review_status = 'LOCKED'{scope}", params
            ).fetchone()[0]
            submitted   = conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE review_status = 'SUBMITTED_FOR_REVIEW'{scope}", params
            ).fetchone()[0]
            needs_final = conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE review_status = 'NEEDS_FINAL_REVIEW'{scope}", params
            ).fetchone()[0]

            # AstroTalk's own flag vs. our (LLM/REGEX/MANUAL) flags.
            astro_flagged = conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE astrotalk_flagged = 1{scope}", params
            ).fetchone()[0]
            astro_clean   = conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE (astrotalk_flagged IS NULL OR astrotalk_flagged != 1){scope}",
                params,
            ).fetchone()[0]
            flagged_by_us = conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE {_FLAGGED_BY_US_SQL}{scope}",
                params,
            ).fetchone()[0]
            # Distinct sessions (out of all) that carry at least one active
            # LLM flag / at least one active MANUAL flag.
            llm_flagged = conn.execute(
                f"""SELECT COUNT(*) FROM sessions
                    WHERE EXISTS (SELECT 1 FROM flags f
                                  WHERE f.session_id = sessions.session_id
                                    AND f.source = 'LLM' AND {_ACTIVE_FLAG}){scope}""",
                params,
            ).fetchone()[0]
            manual_flagged = conn.execute(
                f"""SELECT COUNT(*) FROM sessions
                    WHERE EXISTS (SELECT 1 FROM flags f
                                  WHERE f.session_id = sessions.session_id
                                    AND f.source = 'MANUAL' AND {_ACTIVE_FLAG}){scope}""",
                params,
            ).fetchone()[0]

            # Flagged by both AstroTalk AND us (intersection).
            flagged_by_both = conn.execute(
                f"""SELECT COUNT(*) FROM sessions
                    WHERE astrotalk_flagged = 1 AND {_FLAGGED_BY_US_SQL}{scope}""",
                params,
            ).fetchone()[0]
            # False positive: AstroTalk flagged it, but we marked it clean —
            # LLM verdict CLEAN or a human reviewer cleared it (REVIEWED).
            false_pos = conn.execute(
                f"""SELECT COUNT(*) FROM sessions
                    WHERE astrotalk_flagged = 1
                      AND (overall_verdict = 'CLEAN' OR review_status = 'REVIEWED'){scope}""",
                params,
            ).fetchone()[0]
            # False negative: AstroTalk missed it, but we flagged it.
            false_neg = conn.execute(
                f"""SELECT COUNT(*) FROM sessions
                    WHERE (astrotalk_flagged IS NULL OR astrotalk_flagged != 1)
                      AND {_FLAGGED_BY_US_SQL}{scope}""",
                params,
            ).fetchone()[0]

            # L2-only: per-reviewer assignment breakdown
            reviewer_stats = None
            if reviewer_role == 'L2':
                rs_rows = conn.execute("""
                    SELECT
                        assigned_to AS reviewer,
                        COUNT(*) AS total,
                        SUM(CASE WHEN review_status = 'PENDING' THEN 1 ELSE 0 END) AS pending,
                        SUM(CASE WHEN review_status = 'SUBMITTED_FOR_REVIEW' THEN 1 ELSE 0 END) AS submitted,
                        SUM(CASE WHEN review_status = 'LOCKED' THEN 1 ELSE 0 END) AS locked
                    FROM sessions
                    WHERE assigned_to IS NOT NULL
                    GROUP BY assigned_to
                    ORDER BY assigned_to
                """).fetchall()
                reviewer_stats = [dict(r) for r in rs_rows]

    except Exception:
        result = {
            "total_sessions": 0, "total_pending": 0, "total_reviewed": 0,
            "count_severe": 0, "count_flagged": 0, "count_clean": 0,
            "count_unprocessed": 0, "count_locked": 0,
            "count_submitted": 0, "count_needs_final_review": 0,
            "count_astrotalk_flagged": 0, "count_astrotalk_clean": 0,
            "count_flagged_by_us": 0,
            "count_flagged_by_both": 0,
            "count_llm_flagged": 0, "count_manual_flagged": 0,
            "count_false_positive": 0, "pct_false_positive": 0,
            "count_false_negative": 0, "pct_false_negative": 0,
        }
        if reviewer_role == 'L2':
            result["reviewer_stats"] = []
        return result

    result = {
        "total_sessions":           total,
        "total_pending":            pending,
        "total_reviewed":           reviewed,
        "count_severe":             severe,
        "count_flagged":            flagged,
        "count_clean":              clean,
        "count_unprocessed":        unprocessed,
        "count_locked":             locked,
        "count_submitted":          submitted,
        "count_needs_final_review": needs_final,
        "count_astrotalk_flagged":  astro_flagged,
        "count_astrotalk_clean":    astro_clean,
        "count_flagged_by_us":      flagged_by_us,
        "count_flagged_by_both":    flagged_by_both,
        "count_llm_flagged":    llm_flagged,
        "count_manual_flagged": manual_flagged,
        "count_false_positive":     false_pos,
        "pct_false_positive":       round(100 * false_pos / astro_flagged, 1) if astro_flagged else 0,
        "count_false_negative":     false_neg,
        "pct_false_negative":       round(100 * false_neg / astro_clean, 1) if astro_clean else 0,
    }
    if reviewer_stats is not None:
        result["reviewer_stats"] = reviewer_stats
    return result


@app.get("/stats/reviewer")
def reviewer_stats():
    """Per-reviewer activity breakdown — only sessions that have been reviewed."""
    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT
                    reviewer_id,
                    COUNT(*) AS sessions_reviewed,
                    SUM(CASE WHEN review_status = 'CONFIRMED'  THEN 1 ELSE 0 END) AS confirmed,
                    SUM(CASE WHEN review_status = 'OVERRIDDEN' THEN 1 ELSE 0 END) AS false_positives,
                    SUM(CASE WHEN review_status = 'ESCALATED'  THEN 1 ELSE 0 END) AS escalated,
                    SUM(CASE WHEN review_status = 'REVIEWED'   THEN 1 ELSE 0 END) AS cleared
                FROM sessions
                WHERE review_status != 'PENDING'
                  AND reviewer_id IS NOT NULL
                GROUP BY reviewer_id
                ORDER BY sessions_reviewed DESC
            """).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []


# Fixed display order for the violation breakdown — only these categories are
# shown, in exactly this order.
_VIOLATION_DISPLAY_ORDER = [
    "nsfw",
    "nsfw_explicit",
    "nsfw_grooming",
    "nsfw_appearance",
    "csam_risk",
    "abusive_language",
    "hate_speech",
    "self_harm",
    "violence",
    "fake_remedies",
    "unauthorized_medical_advice",
    "financial_solicitation",
    "identity_fraud",
    "instigation",
]


@app.get("/stats/violations")
def violation_stats():
    try:
        with get_connection() as conn:
            rows = conn.execute(f"""
                SELECT {_NORM_CAT} AS cat, COUNT(DISTINCT f.session_id) AS count
                FROM flags f
                JOIN sessions s ON s.session_id = f.session_id
                WHERE s.overall_verdict != 'CLEAN'
                GROUP BY cat
            """).fetchall()
        counts = {r["cat"]: r["count"] for r in rows}
        return [
            {"category_code": c.upper(), "count": counts.get(c, 0)}
            for c in _VIOLATION_DISPLAY_ORDER
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Endpoints — session list
# NOTE: /sessions/pending must be defined BEFORE /sessions/{session_id}
# ---------------------------------------------------------------------------

@app.get("/sessions/pending")
def pending_sessions(limit: int = Query(default=50, ge=1, le=500)):
    return fetch_pending_review_sessions(limit=limit)


@app.get("/sessions")
def sessions(
    verdict:        Optional[str] = None,
    status:         Optional[str] = None,
    language:       Optional[str] = None,
    reviewer_name:  Optional[str] = None,
    reviewer_role:  Optional[str] = None,
    assigned_to:    Optional[str] = None,
    search:         Optional[str] = None,
    session_type:   Optional[str] = None,
    astrotalk:      Optional[str] = None,          # 'flagged' | 'clean'
    min_confidence: float         = 0,
    min_duration:   Optional[float] = None,
    max_duration:   Optional[float] = None,
    min_turns:      Optional[int]   = None,
    max_turns:      Optional[int]   = None,
    sort_col:       Optional[str] = None,
    sort_dir:       Optional[str] = None,
    limit:          int = Query(default=50, ge=1, le=500),
    offset:         int = Query(default=0, ge=0),
):
    """
    Server-side paginated session list. All filtering + sorting happens in SQL,
    so `limit`/`offset` page over the FULL filtered set. Returns
    {"rows": [...page...], "total": <count of full filtered set>}.
    """
    rows, total = fetch_sessions_page(
        verdict=verdict,
        status=status,
        reviewer_role=reviewer_role,
        reviewer_name=reviewer_name,
        assigned_to=assigned_to,
        search=search,
        language=language,
        session_type=session_type,
        astrotalk=astrotalk,
        min_confidence=min_confidence,
        min_duration=min_duration,
        max_duration=max_duration,
        min_turns=min_turns,
        max_turns=max_turns,
        sort_col=sort_col,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
    )
    return {"rows": rows, "total": total}


# ---------------------------------------------------------------------------
# Endpoints — session detail and sub-resources
# NOTE: more-specific paths (/flags, /review, /manual-flag, /session-note)
# must be defined BEFORE the generic /{session_id} catch-all.
# ---------------------------------------------------------------------------

@app.get("/sessions/{session_id}/flags")
def get_session_flags(session_id: str):
    """
    Returns all flags for a session.
    MANUAL flags are enriched with flagged_by (reviewer_id from review_log).
    flagged_speaker (ASTROLOGER / USER) is derived via the flag's turn_id.
    """
    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT f.*,
                       CASE WHEN f.source = 'MANUAL'
                            THEN rl.reviewer_id ELSE NULL END AS flagged_by,
                       t.speaker AS flagged_speaker
                FROM flags f
                LEFT JOIN review_log rl
                    ON rl.flag_id = f.flag_id AND rl.action = 'MANUAL_FLAG'
                LEFT JOIN turns t
                    ON t.session_id = f.session_id AND t.turn_id = f.turn_id
                WHERE f.session_id = ?
                ORDER BY f.flag_id
            """, (session_id,)).fetchall()
        return [dict(row) for row in rows]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/sessions/{session_id}")
def session_detail(session_id: str):
    """
    Full session detail including turns and flags.
    MANUAL flags include flagged_by from review_log.
    """
    try:
        with get_connection() as conn:
            session = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise HTTPException(
                    status_code=404, detail=f"Session {session_id!r} not found"
                )
            turns = conn.execute(
                "SELECT * FROM turns WHERE session_id = ? ORDER BY turn_id", (session_id,)
            ).fetchall()
            flags = conn.execute("""
                SELECT f.*,
                       CASE WHEN f.source = 'MANUAL'
                            THEN rl.reviewer_id ELSE NULL END AS flagged_by,
                       t.speaker AS flagged_speaker
                FROM flags f
                LEFT JOIN review_log rl
                    ON rl.flag_id = f.flag_id AND rl.action = 'MANUAL_FLAG'
                LEFT JOIN turns t
                    ON t.session_id = f.session_id AND t.turn_id = f.turn_id
                WHERE f.session_id = ?
                ORDER BY f.flag_id
            """, (session_id,)).fetchall()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "session": dict(session),
        "turns":   [dict(t) for t in turns],
        "flags":   [dict(f) for f in flags],
    }


@app.post("/sessions/{session_id}/review")
def submit_review(session_id: str, body: ReviewRequest):
    if body.action not in _VALID_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action {body.action!r}. Must be one of: {sorted(_VALID_ACTIONS)}",
        )
    try:
        write_review_action(
            session_id, body.flag_id, body.action, body.reviewer_id, body.note
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {"success": True, "session_id": session_id}


@app.post("/sessions/{session_id}/manual-flag")
def manual_flag(session_id: str, body: ManualFlagRequest):
    """Insert a reviewer-created flag and record it in the review log."""
    conn = get_connection()
    try:
        with conn:
            severity = _severity_for_category(body.category_code)
            cur = conn.execute(
                """
                INSERT INTO flags
                    (session_id, turn_id, category_code, detection_layer, source, status,
                     severity, confidence_score, reasoning, false_positive_risk, pattern_matched)
                VALUES (?, ?, ?, 'MANUAL', 'MANUAL', 'ACTIVE', ?, 1.0, ?, 'LOW', ?)
                """,
                (
                    session_id,
                    body.turn_id,
                    body.category_code,
                    severity,
                    body.note,
                    body.message_text[:200],
                ),
            )
            flag_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO review_log (session_id, flag_id, action, reviewer_id, note)
                VALUES (?, ?, 'MANUAL_FLAG', ?, ?)
                """,
                (session_id, flag_id, body.reviewer_id, body.note),
            )
            recompute_session_verdict(session_id, conn)
        return {"success": True, "flag_id": flag_id}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        conn.close()


@app.post("/sessions/{session_id}/lock")
def lock_session_endpoint(session_id: str, body: LockRequest):
    if body.reviewer_id != "Amogh":
        raise HTTPException(
            status_code=403,
            detail="Only L2 reviewer can lock sessions",
        )
    try:
        lock_session(session_id, body.reviewer_id)
        return {"success": True, "locked_by": body.reviewer_id}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/sessions/{session_id}/unlock")
def unlock_session_endpoint(session_id: str):
    try:
        unlock_session(session_id)
        return {"success": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/sessions/{session_id}/session-note")
def save_session_note(session_id: str, body: SessionNoteRequest):
    """Persist a reviewer's overall observation note on the session."""
    # Belt-and-suspenders migration in case column is absent in legacy DB
    try:
        with get_connection() as conn:
            conn.execute("ALTER TABLE sessions ADD COLUMN session_note TEXT")
    except Exception:
        pass

    try:
        with get_connection() as conn:
            conn.execute(
                "UPDATE sessions SET session_note = ? WHERE session_id = ?",
                (body.note, session_id),
            )
        return {"success": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Endpoints — flag operations
# ---------------------------------------------------------------------------

@app.post("/flags/{flag_id}/amend")
def amend_flag(flag_id: int, body: AmendFlagRequest):
    """
    Edit a flag. Replaces any existing amendment for this flag with a new one.
    The original flag row is kept as silent audit history.
    The amendment resets to ACTIVE so the reviewer must re-confirm.
    Triggers session verdict recomputation.
    """
    conn = get_connection()
    try:
        with conn:
            # Find the target row (could be original or existing amendment)
            target = conn.execute(
                "SELECT flag_id, session_id, turn_id, pattern_matched, parent_flag_id FROM flags WHERE flag_id = ?",
                (flag_id,),
            ).fetchone()
            if target is None:
                raise HTTPException(status_code=404, detail=f"Flag {flag_id} not found")

            session_id = target["session_id"]

            # Determine the original flag_id
            # If editing an amendment, its parent_flag_id is the original
            # If editing an original, it IS the original
            original_flag_id = target["parent_flag_id"] if target["parent_flag_id"] else flag_id

            # Delete any existing amendment for this original (keep only one)
            conn.execute(
                "DELETE FROM flags WHERE parent_flag_id = ?",
                (original_flag_id,),
            )

            # Get the original row for context
            original = conn.execute(
                "SELECT turn_id, pattern_matched FROM flags WHERE flag_id = ?",
                (original_flag_id,),
            ).fetchone()

            # Insert new amendment row — source=MANUAL, status=ACTIVE (fresh flag)
            cur = conn.execute(
                """
                INSERT INTO flags
                    (session_id, turn_id, category_code, detection_layer, source, status,
                     parent_flag_id, severity, confidence_score, reasoning,
                     false_positive_risk, pattern_matched)
                VALUES (?, ?, ?, 'MANUAL', 'MANUAL', 'ACTIVE', ?, ?, 1.0, ?, 'LOW', ?)
                """,
                (
                    session_id,
                    original["turn_id"] if original else target["turn_id"],
                    body.category_code,
                    original_flag_id,
                    body.severity,
                    body.reasoning,
                    (original["pattern_matched"] if original and original["pattern_matched"]
                     else f"Amended by {body.reviewer_id}"),
                ),
            )
            new_flag_id = cur.lastrowid

            # Log the amendment
            conn.execute(
                """INSERT INTO review_log (session_id, flag_id, action, reviewer_id, note)
                   VALUES (?, ?, 'AMENDED', ?, ?)""",
                (session_id, new_flag_id, body.reviewer_id, body.reasoning),
            )

            # Recompute session verdict based on updated flags
            recompute_session_verdict(session_id, conn)

        return {"success": True, "new_flag_id": new_flag_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        conn.close()


@app.post("/flags/{flag_id}/dismiss")
def dismiss_flag(flag_id: int, body: DismissFlagRequest):
    """
    Hard-delete a flag and its amendment (if any) from the database.
    Only this specific flag is removed — other flags on the session are untouched.
    Triggers session verdict recomputation.
    """
    conn = get_connection()
    try:
        with conn:
            # Find the row being dismissed (could be original or amendment)
            target = conn.execute(
                "SELECT flag_id, session_id, parent_flag_id FROM flags WHERE flag_id = ?",
                (flag_id,),
            ).fetchone()
            if target is None:
                raise HTTPException(status_code=404, detail=f"Flag {flag_id} not found")

            session_id = target["session_id"]

            # Determine original flag_id
            original_flag_id = target["parent_flag_id"] if target["parent_flag_id"] else flag_id

            # Delete the amendment row (if exists)
            conn.execute("DELETE FROM flags WHERE parent_flag_id = ?", (original_flag_id,))
            # Delete the original row
            conn.execute("DELETE FROM flags WHERE flag_id = ?", (original_flag_id,))

            # Recompute verdict based on remaining flags
            recompute_session_verdict(session_id, conn)

        return {"success": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        conn.close()


@app.post("/flags/{flag_id}/confirm")
def confirm_flag_endpoint(flag_id: int, body: LockRequest):
    """
    Confirm a flag. Sets status = CONFIRMED on the active row:
    - If an amendment exists for this flag, confirm the amendment row.
    - Otherwise confirm the original row.
    Triggers session verdict recomputation.
    """
    conn = get_connection()
    try:
        with conn:
            target = conn.execute(
                "SELECT flag_id, session_id, parent_flag_id FROM flags WHERE flag_id = ?",
                (flag_id,),
            ).fetchone()
            if target is None:
                raise HTTPException(status_code=404, detail=f"Flag {flag_id} not found")

            session_id       = target["session_id"]
            original_flag_id = target["parent_flag_id"] if target["parent_flag_id"] else flag_id

            # Find amendment row if one exists
            amendment = conn.execute(
                "SELECT flag_id FROM flags WHERE parent_flag_id = ?",
                (original_flag_id,),
            ).fetchone()

            active_flag_id = amendment["flag_id"] if amendment else original_flag_id

            conn.execute(
                "UPDATE flags SET status = 'CONFIRMED' WHERE flag_id = ?",
                (active_flag_id,),
            )
            conn.execute(
                """INSERT INTO review_log (session_id, flag_id, action, reviewer_id, note)
                   VALUES (?, ?, 'CONFIRM_FLAG', ?, '')""",
                (session_id, active_flag_id, body.reviewer_id),
            )
            recompute_session_verdict(session_id, conn)

        return {"success": True, "confirmed_flag_id": active_flag_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/sessions/{session_id}/confirm-all-flags")
def confirm_all_flags_endpoint(session_id: str, body: LockRequest):
    """
    Confirm ALL active, not-yet-confirmed flags for a session in one request.

    Batched equivalent of /flags/{flag_id}/confirm: resolves each active row
    (amendment if present, else original), sets status='CONFIRMED', logs a
    CONFIRM_FLAG action per flag, and recomputes the session verdict ONCE.
    Idempotent — already-confirmed flags are skipped.
    """
    conn = get_connection()
    try:
        with conn:
            rows = conn.execute(
                "SELECT flag_id, parent_flag_id, status FROM flags WHERE session_id = ?",
                (session_id,),
            ).fetchall()

            # Active = amendment rows + original rows that have no amendment.
            amended_parents = {
                r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None
            }
            status_by_id = {r["flag_id"]: r["status"] for r in rows}
            active_ids = [
                r["flag_id"] for r in rows
                if (r["parent_flag_id"] is not None)
                or (r["flag_id"] not in amended_parents)
            ]
            to_confirm = [
                fid for fid in active_ids if status_by_id.get(fid) != "CONFIRMED"
            ]

            for fid in to_confirm:
                conn.execute(
                    "UPDATE flags SET status = 'CONFIRMED' WHERE flag_id = ?",
                    (fid,),
                )
                conn.execute(
                    """INSERT INTO review_log (session_id, flag_id, action, reviewer_id, note)
                       VALUES (?, ?, 'CONFIRM_FLAG', ?, '')""",
                    (session_id, fid, body.reviewer_id),
                )

            recompute_session_verdict(session_id, conn)

        return {"success": True, "confirmed_count": len(to_confirm)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Endpoints — session submission workflow
# ---------------------------------------------------------------------------

@app.post("/sessions/{session_id}/submit")
def submit_session(session_id: str, body: SubmitRequest):
    try:
        summary = get_session_flag_summary(session_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if not summary["can_submit"]:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Cannot submit — unactioned flags remain",
                "unactioned_count": summary["unactioned_flags"],
            },
        )

    try:
        submit_session_for_review(session_id, body.reviewer_id, body.note or None)
        return {"success": True, "session_id": session_id}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/sessions/{session_id}/needs-final-review")
def needs_final_review(session_id: str, body: LockRequest):
    if body.reviewer_id != "Amogh":
        raise HTTPException(
            status_code=403,
            detail="Only L2 reviewer can mark needs final review",
        )
    try:
        mark_needs_final_review(session_id, body.reviewer_id)
        return {"success": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@app.get("/export/csv")
def export_csv(
    reviewer_name: Optional[str] = None,
    reviewer_role: Optional[str] = None,
):
    """
    Download reviewed sessions as a CSV file.
    - L1 reviewers can export ONLY the sessions they submitted themselves.
    - L2 (and unspecified) can export all reviewed sessions.
    """
    date_str = datetime.now().strftime("%Y%m%d")

    # Role-based scope
    scope = ""
    params: tuple = ()
    if reviewer_role == "L1" and reviewer_name:
        # Only sessions this L1 reviewer submitted / reviewed
        scope = " AND (s.submitted_by = ? OR s.reviewer_id = ?)"
        params = (reviewer_name, reviewer_name)

    try:
        with get_connection() as conn:
            rows = conn.execute(f"""
                SELECT s.session_id, s.overall_verdict, s.language_detected,
                       s.duration_minutes, s.session_type, s.review_status,
                       s.reviewer_id, s.reviewer_note, s.session_note,
                       COALESCE(fc.flag_count, 0) AS flag_count,
                       s.astrotalk_flagged, s.astrotalk_flag_category, s.reviewed_at
                FROM sessions s
                LEFT JOIN (
                    SELECT session_id, COUNT(*) AS flag_count
                    FROM flags GROUP BY session_id
                ) fc ON fc.session_id = s.session_id
                WHERE s.review_status != 'PENDING'{scope}
                ORDER BY s.reviewed_at DESC
            """, params).fetchall()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    _HEADERS = [
        "session_id", "verdict", "language_detected", "duration_minutes",
        "session_type", "review_status", "reviewer_id", "reviewer_note",
        "session_note", "flag_count", "astrotalk_flagged",
        "astrotalk_flag_category", "reviewed_at",
    ]

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_HEADERS)
        for row in rows:
            rd = dict(row)
            writer.writerow([
                rd.get("session_id", ""),
                rd.get("overall_verdict", ""),
                rd.get("language_detected", ""),
                rd.get("duration_minutes", ""),
                rd.get("session_type", ""),
                rd.get("review_status", ""),
                rd.get("reviewer_id", ""),
                rd.get("reviewer_note", ""),
                rd.get("session_note", ""),
                rd.get("flag_count", 0),
                rd.get("astrotalk_flagged", ""),
                rd.get("astrotalk_flag_category", ""),
                rd.get("reviewed_at", ""),
            ])
        buf.seek(0)
        yield buf.read()

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={
            "Content-Disposition":
                f"attachment; filename=gt_review_export_{date_str}.csv"
        },
    )


# ---------------------------------------------------------------------------
# Static file serving — React frontend build
# Must be mounted AFTER all API routes so API paths take precedence.
# Skipped silently if the build folder does not yet exist.
# ---------------------------------------------------------------------------

frontend_build = Path(__file__).parent.parent / "frontend" / "build"
if frontend_build.exists():
    app.mount(
        "/",
        StaticFiles(directory=str(frontend_build), html=True),
        name="static",
    )
