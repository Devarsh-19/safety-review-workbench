"""FastAPI review interface for AstroTalk content safety workbench"""

from __future__ import annotations

import csv
import io
import os
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
from store.audio_db import (
    get_audio_connection,
    initialise_audio_db,
    fetch_audio_sessions_page,
    fetch_audio_session_detail,
    set_speaker_roles,
    submit_audio_session,
    set_audio_session_risk,
    lock_audio_session,
    unlock_audio_session,
    recompute_audio_session_verdict,
    get_audio_flag_summary,
)
from store.writer import write_review_action
from engine.verdict_rules import get_db_verdict_for_flags

# Reviewers allowed to lock/unlock/finalise sessions (L2). Comma-separated
# override via the L2_REVIEWERS env var; keep in sync with the frontend
# LoginScreen roster when adding a second L2.
L2_REVIEWERS = {
    name.strip()
    for name in os.getenv("L2_REVIEWERS", "Amogh").split(",")
    if name.strip()
}


def _require_l2(reviewer_id: str, action: str) -> None:
    if reviewer_id not in L2_REVIEWERS:
        raise HTTPException(status_code=403, detail=f"Only L2 reviewer can {action}")


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
# Lifespan — initialise DB (including session_note migration) on startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialise BOTH databases on startup. Failures must be visible — a
    # silent pass here leaves the app running against a missing DB.
    try:
        initialise_db()
    except Exception as exc:
        print(f"[startup] chat DB initialisation failed: {exc}")
    try:
        initialise_audio_db()
    except Exception as exc:
        print(f"[startup] audio DB initialisation failed: {exc}")
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


class SpeakerRolesRequest(BaseModel):
    speaker1_role: str          # 'ASTROLOGER' | 'USER'
    speaker2_role: str
    reviewer_id: str


class AmendAudioFlagRequest(BaseModel):
    intent: str
    severity: str
    reasoning: str = ""
    reviewer_id: str


class SessionRiskRequest(BaseModel):
    risk: str                   # 'HIGH' | 'MEDIUM' | 'LOW'
    reviewer_id: str


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


@app.get("/stats/violations")
def violation_stats():
    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT f.category_code, COUNT(DISTINCT f.session_id) AS count
                FROM flags f
                JOIN sessions s ON s.session_id = f.session_id
                WHERE s.overall_verdict != 'CLEAN'
                GROUP BY f.category_code
                ORDER BY count DESC, f.category_code ASC
            """).fetchall()
        return [dict(row) for row in rows]
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
    _require_l2(body.reviewer_id, "lock sessions")
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
    _require_l2(body.reviewer_id, "mark needs final review")
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
# Endpoints — audio review (separate DB: store/audio_review.db)
# Same L1 -> L2 workflow as chat; speakers are anonymous diarization labels
# until a reviewer assigns ASTROLOGER/USER roles on the session.
# ---------------------------------------------------------------------------

@app.get("/audio/stats")
def audio_stats(
    reviewer_name: Optional[str] = None,
    reviewer_role: Optional[str] = None,
):
    # L1: scope all counts to sessions assigned to this reviewer (chat parity)
    if reviewer_role == "L1" and reviewer_name:
        scope  = " WHERE assigned_to = ?"
        params = (reviewer_name,)
    else:
        scope  = ""
        params = ()

    try:
        with get_audio_connection() as conn:
            row = conn.execute(f"""
                SELECT
                    COUNT(*) AS total_sessions,
                    SUM(CASE WHEN review_status = 'PENDING'              THEN 1 ELSE 0 END) AS total_pending,
                    SUM(CASE WHEN review_status = 'SUBMITTED_FOR_REVIEW' THEN 1 ELSE 0 END) AS count_submitted,
                    SUM(CASE WHEN review_status = 'LOCKED'               THEN 1 ELSE 0 END) AS count_locked,
                    SUM(CASE WHEN overall_verdict = 'SEVERE'             THEN 1 ELSE 0 END) AS count_severe,
                    SUM(CASE WHEN overall_verdict = 'FLAGGED'            THEN 1 ELSE 0 END) AS count_flagged,
                    SUM(CASE WHEN overall_verdict = 'CLEAN'              THEN 1 ELSE 0 END) AS count_clean
                FROM audio_sessions{scope}
            """, params).fetchone()
            result = {k: (row[k] or 0) for k in row.keys()}
            result["total_reviewed"] = result["total_sessions"] - result["total_pending"]

            # L2-only: per-reviewer assignment breakdown (chat parity)
            if reviewer_role == "L2":
                rs_rows = conn.execute("""
                    SELECT
                        assigned_to AS reviewer,
                        COUNT(*) AS total,
                        SUM(CASE WHEN review_status = 'PENDING'              THEN 1 ELSE 0 END) AS pending,
                        SUM(CASE WHEN review_status = 'SUBMITTED_FOR_REVIEW' THEN 1 ELSE 0 END) AS submitted,
                        SUM(CASE WHEN review_status = 'LOCKED'               THEN 1 ELSE 0 END) AS locked
                    FROM audio_sessions
                    WHERE assigned_to IS NOT NULL
                    GROUP BY assigned_to
                    ORDER BY assigned_to
                """).fetchall()
                result["reviewer_stats"] = [dict(r) for r in rs_rows]
        return result
    except Exception:
        result = {
            "total_sessions": 0, "total_pending": 0, "count_submitted": 0,
            "count_locked": 0, "count_severe": 0, "count_flagged": 0,
            "count_clean": 0, "total_reviewed": 0,
        }
        if reviewer_role == "L2":
            result["reviewer_stats"] = []
        return result


@app.get("/audio/stats/violations")
def audio_violation_stats():
    """Violation breakdown for the audio queue heatmap — mirrors
    /stats/violations. Counts distinct non-clean sessions per intent,
    over active flags only (amendments replace their originals, and
    DISMISSED flags don't count — unlike chat, audio keeps them)."""
    try:
        with get_audio_connection() as conn:
            rows = conn.execute("""
                SELECT f.intent AS category_code, COUNT(DISTINCT f.s_id) AS count
                FROM audio_flags f
                JOIN audio_sessions s ON s.s_id = f.s_id
                WHERE s.overall_verdict != 'CLEAN'
                  AND f.intent IS NOT NULL
                  AND (f.status IS NULL OR f.status != 'DISMISSED')
                  AND f.flag_id NOT IN (
                      SELECT parent_flag_id FROM audio_flags
                      WHERE parent_flag_id IS NOT NULL
                  )
                GROUP BY f.intent
                ORDER BY count DESC, f.intent ASC
            """).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []


@app.get("/audio/sessions")
def audio_sessions(
    status: Optional[str] = None,
    search: Optional[str] = None,
    reviewer_name: Optional[str] = None,
    reviewer_role: Optional[str] = None,
    assigned_to:   Optional[str] = None,
    has_video: Optional[str] = None,
    lang: Optional[str] = None,
    duration_min: Optional[float] = Query(default=None, ge=0),
    duration_max: Optional[float] = Query(default=None, ge=0),
    flags_min: Optional[int] = Query(default=None, ge=0),
    flags_max: Optional[int] = Query(default=None, ge=0),
    roles: Optional[str] = None,
    reviewer: Optional[str] = None,
    verdict: Optional[str] = None,
    astrotalk_verdict: Optional[str] = None,
    sort_col: Optional[str] = None,
    sort_dir: Optional[str] = None,
    limit:  int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    rows, total = fetch_audio_sessions_page(
        status=status, search=search,
        reviewer_role=reviewer_role, reviewer_name=reviewer_name,
        assigned_to=assigned_to,
        has_video=has_video, lang=lang,
        duration_min=duration_min, duration_max=duration_max,
        flags_min=flags_min, flags_max=flags_max,
        roles=roles, reviewer=reviewer,
        verdict=verdict, astrotalk_verdict=astrotalk_verdict,
        sort_col=sort_col, sort_dir=sort_dir,
        limit=limit, offset=offset,
    )
    return {"rows": rows, "total": total}


@app.get("/audio/sessions/{s_id}")
def audio_session_detail(s_id: int):
    detail = fetch_audio_session_detail(s_id)
    if detail["session"] is None:
        raise HTTPException(status_code=404, detail=f"Audio session {s_id} not found")
    return detail


@app.post("/audio/sessions/{s_id}/speaker-roles")
def audio_speaker_roles(s_id: int, body: SpeakerRolesRequest):
    detail = fetch_audio_session_detail(s_id)
    if detail["session"] is None:
        raise HTTPException(status_code=404, detail=f"Audio session {s_id} not found")
    if detail["session"]["review_status"] == "LOCKED":
        raise HTTPException(status_code=400, detail="Session is locked — roles can no longer be changed")
    try:
        set_speaker_roles(s_id, body.speaker1_role, body.speaker2_role, body.reviewer_id)
        return {"success": True, "speaker1_role": body.speaker1_role,
                "speaker2_role": body.speaker2_role}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def _audio_session_or_404(s_id: int) -> dict:
    detail = fetch_audio_session_detail(s_id)
    if detail["session"] is None:
        raise HTTPException(status_code=404, detail=f"Audio session {s_id} not found")
    return detail


def _reject_if_audio_locked(detail: dict):
    if detail["session"]["review_status"] == "LOCKED":
        raise HTTPException(status_code=400, detail="Session is locked — flags can no longer be changed")


@app.post("/audio/flags/{flag_id}/confirm")
def audio_confirm_flag(flag_id: int, body: LockRequest):
    """Confirm an audio flag — sets status = CONFIRMED on the active row
    (the amendment if one exists, otherwise the original). Mirrors
    /flags/{flag_id}/confirm."""
    conn = get_audio_connection()
    try:
        with conn:
            target = conn.execute(
                "SELECT flag_id, s_id, parent_flag_id FROM audio_flags WHERE flag_id = ?",
                (flag_id,),
            ).fetchone()
            if target is None:
                raise HTTPException(status_code=404, detail=f"Audio flag {flag_id} not found")

            s_id = target["s_id"]
            _reject_if_audio_locked(_audio_session_or_404(s_id))
            original_flag_id = target["parent_flag_id"] if target["parent_flag_id"] else flag_id

            amendment = conn.execute(
                "SELECT flag_id FROM audio_flags WHERE parent_flag_id = ?",
                (original_flag_id,),
            ).fetchone()
            active_flag_id = amendment["flag_id"] if amendment else original_flag_id

            conn.execute(
                """UPDATE audio_flags
                   SET status = 'CONFIRMED', confirmed_by = ?, confirmed_at = datetime('now')
                   WHERE flag_id = ?""",
                (body.reviewer_id, active_flag_id),
            )
            conn.execute(
                """INSERT INTO audio_review_log (s_id, flag_id, action, reviewer_id, note)
                   VALUES (?, ?, 'CONFIRM_FLAG', ?, '')""",
                (s_id, active_flag_id, body.reviewer_id),
            )
            recompute_audio_session_verdict(s_id, conn)
        return {"success": True, "confirmed_flag_id": active_flag_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        conn.close()


@app.post("/audio/flags/{flag_id}/amend")
def audio_amend_flag(flag_id: int, body: AmendAudioFlagRequest):
    """Edit an audio flag. Replaces any existing amendment with a new one;
    the original row is kept as silent audit history. The amendment resets
    to ACTIVE so the reviewer must re-confirm. Mirrors /flags/{flag_id}/amend."""
    conn = get_audio_connection()
    try:
        with conn:
            target = conn.execute(
                "SELECT flag_id, s_id, seg_id, parent_flag_id FROM audio_flags WHERE flag_id = ?",
                (flag_id,),
            ).fetchone()
            if target is None:
                raise HTTPException(status_code=404, detail=f"Audio flag {flag_id} not found")

            s_id = target["s_id"]
            _reject_if_audio_locked(_audio_session_or_404(s_id))
            original_flag_id = target["parent_flag_id"] if target["parent_flag_id"] else flag_id

            # Keep only one amendment per original
            conn.execute("DELETE FROM audio_flags WHERE parent_flag_id = ?", (original_flag_id,))

            original = conn.execute(
                "SELECT seg_id, ts_start, ts_end, transcript, conf FROM audio_flags WHERE flag_id = ?",
                (original_flag_id,),
            ).fetchone()

            cur = conn.execute(
                """INSERT INTO audio_flags
                       (s_id, seg_id, ts_start, ts_end, intent, severity, conf, transcript,
                        source, status, parent_flag_id, reasoning, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'MANUAL', 'ACTIVE', ?, ?, ?)""",
                (
                    s_id,
                    original["seg_id"] if original else target["seg_id"],
                    original["ts_start"] if original else None,
                    original["ts_end"] if original else None,
                    body.intent,
                    body.severity,
                    1.0,
                    original["transcript"] if original else None,
                    original_flag_id,
                    body.reasoning,
                    body.reviewer_id,
                ),
            )
            new_flag_id = cur.lastrowid

            conn.execute(
                """INSERT INTO audio_review_log (s_id, flag_id, action, reviewer_id, note)
                   VALUES (?, ?, 'AMENDED', ?, ?)""",
                (s_id, new_flag_id, body.reviewer_id, body.reasoning),
            )
            recompute_audio_session_verdict(s_id, conn)
        return {"success": True, "new_flag_id": new_flag_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        conn.close()


@app.post("/audio/flags/{flag_id}/dismiss")
def audio_dismiss_flag(flag_id: int, body: DismissFlagRequest):
    """Dismiss (false positive): hard-delete the flag and its amendment, log
    the dismissal, recompute the verdict. Mirrors /flags/{flag_id}/dismiss."""
    conn = get_audio_connection()
    try:
        with conn:
            target = conn.execute(
                "SELECT flag_id, s_id, parent_flag_id FROM audio_flags WHERE flag_id = ?",
                (flag_id,),
            ).fetchone()
            if target is None:
                raise HTTPException(status_code=404, detail=f"Audio flag {flag_id} not found")

            s_id = target["s_id"]
            _reject_if_audio_locked(_audio_session_or_404(s_id))
            original_flag_id = target["parent_flag_id"] if target["parent_flag_id"] else flag_id

            # Hard delete, chat parity: the original and any amendment go
            # away entirely; the review log keeps the audit trail.
            conn.execute(
                "DELETE FROM audio_flags WHERE flag_id = ? OR parent_flag_id = ?",
                (original_flag_id, original_flag_id),
            )

            conn.execute(
                """INSERT INTO audio_review_log (s_id, flag_id, action, reviewer_id, note)
                   VALUES (?, ?, 'DISMISSED', ?, ?)""",
                (s_id, original_flag_id, body.reviewer_id, body.note),
            )
            recompute_audio_session_verdict(s_id, conn)
        return {"success": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        conn.close()


@app.post("/audio/sessions/{s_id}/session-risk")
def audio_session_risk(s_id: int, body: SessionRiskRequest):
    """Set the L1 reviewer's whole-session risk rating (HIGH/MEDIUM/LOW).
    Required before confirm-all and before submitting for L2 review."""
    _reject_if_audio_locked(_audio_session_or_404(s_id))
    try:
        set_audio_session_risk(s_id, (body.risk or "").upper())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    conn = get_audio_connection()
    try:
        with conn:
            conn.execute(
                """INSERT INTO audio_review_log (s_id, action, reviewer_id, note)
                   VALUES (?, 'SET_SESSION_RISK', ?, ?)""",
                (s_id, body.reviewer_id, body.risk.upper()),
            )
    finally:
        conn.close()
    return {"success": True, "manual_risk_level": body.risk.upper()}


@app.post("/audio/sessions/{s_id}/confirm-all-flags")
def audio_confirm_all_flags(s_id: int, body: LockRequest):
    """Confirm every active, not-yet-confirmed flag on the session at once.
    Mirrors /sessions/{session_id}/confirm-all-flags. Requires the session
    risk rating to be set first — bulk-confirming without assessing the
    session as a whole is exactly the shortcut this gate exists to prevent."""
    detail = _audio_session_or_404(s_id)
    _reject_if_audio_locked(detail)
    if not detail["session"].get("manual_risk_level"):
        raise HTTPException(
            status_code=400,
            detail="Set the session risk rating (high/medium/low) before confirming all flags",
        )
    conn = get_audio_connection()
    try:
        with conn:
            rows = conn.execute(
                "SELECT flag_id, parent_flag_id, status FROM audio_flags WHERE s_id = ?",
                (s_id,),
            ).fetchall()
            amended_parents = {
                r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None
            }
            to_confirm = [
                r["flag_id"] for r in rows
                if (r["parent_flag_id"] is not None or r["flag_id"] not in amended_parents)
                and r["status"] not in ("CONFIRMED", "DISMISSED")
            ]
            for fid in to_confirm:
                conn.execute(
                    """UPDATE audio_flags
                       SET status = 'CONFIRMED', confirmed_by = ?, confirmed_at = datetime('now')
                       WHERE flag_id = ?""",
                    (body.reviewer_id, fid),
                )
                conn.execute(
                    """INSERT INTO audio_review_log (s_id, flag_id, action, reviewer_id, note)
                       VALUES (?, ?, 'CONFIRM_FLAG', ?, '')""",
                    (s_id, fid, body.reviewer_id),
                )
            recompute_audio_session_verdict(s_id, conn)
        return {"success": True, "confirmed_count": len(to_confirm)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        conn.close()


@app.post("/audio/sessions/{s_id}/submit")
def audio_submit(s_id: int, body: SubmitRequest):
    detail = _audio_session_or_404(s_id)
    if detail["session"]["review_status"] == "LOCKED":
        raise HTTPException(status_code=400, detail="Session is locked")
    # Speaker roles must be assigned before an L1 can submit — otherwise the
    # flagged timestamps can't be attributed to astrologer vs user.
    if detail["flags"] and not (detail["session"]["speaker1_role"] and detail["session"]["speaker2_role"]):
        raise HTTPException(status_code=400,
                            detail="Assign speaker roles (astrologer/user) before submitting")
    # The L1 reviewer must rate the whole session's risk before it can go to
    # L2 — but only when flags remain. A session whose flags were all
    # dismissed (deleted) is clean and can be submitted directly.
    if detail["flags"] and not detail["session"].get("manual_risk_level"):
        raise HTTPException(status_code=400,
                            detail="Set the session risk rating (high/medium/low) before submitting")
    # Same gate as chat: every active flag must be actioned (confirmed after
    # any edits, or dismissed) before the session can be submitted.
    summary = get_audio_flag_summary(s_id)
    if not summary["can_submit"]:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot submit — {summary['unactioned_flags']} unactioned flag(s) remain",
        )
    try:
        submit_audio_session(s_id, body.reviewer_id, body.note or None)
        return {"success": True, "s_id": s_id}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/audio/sessions/{s_id}/lock")
def audio_lock(s_id: int, body: LockRequest):
    _require_l2(body.reviewer_id, "lock sessions")
    try:
        lock_audio_session(s_id, body.reviewer_id)
        return {"success": True, "locked_by": body.reviewer_id}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/audio/sessions/{s_id}/unlock")
def audio_unlock(s_id: int, body: LockRequest):
    _require_l2(body.reviewer_id, "unlock sessions")
    try:
        unlock_audio_session(s_id, body.reviewer_id)
        return {"success": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


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
