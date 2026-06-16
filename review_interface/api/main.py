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
    fetch_pending_review_sessions,
    update_review_status,
    confirm_flag,
    submit_session_for_review,
    mark_needs_final_review,
    get_session_flag_summary,
    lock_session,
    unlock_session,
    initialise_db,
)
from store.writer import write_review_action
from engine.verdict_rules import (
    get_active_flag_codes,
    get_db_confidence_for_verdict,
    get_db_verdict_for_flags,
)


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


def _is_final_approver(reviewer_id: str | None) -> bool:
    if not reviewer_id:
        return False
    parts = reviewer_id.strip().lower().replace(".", " ").replace("_", " ").split()
    return "amogh" in parts


def _flag_severity_for_code(category_code: str) -> str:
    verdict = get_db_verdict_for_flags([category_code])
    return {
        "SEVERE": "HIGH",
        "FLAGGED": "MEDIUM",
        "CLEAN": "LOW",
    }.get(verdict, "MEDIUM")


def _review_status_for_verdict(verdict: str) -> str:
    return "REVIEWED" if verdict == "CLEAN" else "NEEDS_FINAL_REVIEW"


def _recompute_session_verdict(
    conn,
    session_id: str,
    *,
    reviewer_id: str | None = None,
    note: str = "",
) -> tuple[str, str]:
    """
    Recompute the stored verdict from active LLM/REGEX/MANUAL/AMENDED flags.
    DISMISSED marker rows suppress their matching original category_code.
    """
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT category_code, detection_layer FROM flags WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    ]
    active_codes = get_active_flag_codes(rows)
    verdict = get_db_verdict_for_flags(active_codes)
    review_status = _review_status_for_verdict(verdict)

    if reviewer_id:
        conn.execute(
            """UPDATE sessions
               SET overall_verdict = ?,
                   confidence_score = ?,
                   review_status = ?,
                   reviewer_id = ?,
                   reviewer_note = ?,
                   reviewed_at = datetime('now')
               WHERE session_id = ?""",
            (
                verdict,
                get_db_confidence_for_verdict(verdict),
                review_status,
                reviewer_id,
                note,
                session_id,
            ),
        )
    else:
        conn.execute(
            """UPDATE sessions
               SET overall_verdict = ?,
                   confidence_score = ?
               WHERE session_id = ?""",
            (verdict, get_db_confidence_for_verdict(verdict), session_id),
        )
    return verdict, review_status


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
    note: str = ""


class BulkFinalApproveRequest(BaseModel):
    reviewer_id: str
    session_ids: list[str]


# ---------------------------------------------------------------------------
# Endpoints — health + aggregate stats
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "db": DB_PATH}


@app.get("/stats")
def stats():
    try:
        with get_connection() as conn:
            total       = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            pending     = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE review_status = 'PENDING'"
            ).fetchone()[0]
            reviewed    = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE review_status != 'PENDING'"
            ).fetchone()[0]
            severe      = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE overall_verdict = 'SEVERE'"
            ).fetchone()[0]
            flagged     = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE overall_verdict = 'FLAGGED'"
            ).fetchone()[0]
            clean       = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE overall_verdict = 'CLEAN'"
            ).fetchone()[0]
            unprocessed = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE overall_verdict = 'UNPROCESSED'"
            ).fetchone()[0]
            locked      = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE review_status = 'LOCKED'"
            ).fetchone()[0]
            submitted   = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE review_status = 'SUBMITTED_FOR_REVIEW'"
            ).fetchone()[0]
            needs_final = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE review_status = 'NEEDS_FINAL_REVIEW'"
            ).fetchone()[0]
    except Exception:
        return {
            "total_sessions": 0, "total_pending": 0, "total_reviewed": 0,
            "count_severe": 0, "count_flagged": 0, "count_clean": 0,
            "count_unprocessed": 0, "count_locked": 0,
            "count_submitted": 0, "count_needs_final_review": 0,
        }

    return {
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
                SELECT f.category_code, COUNT(*) AS count
                FROM flags f
                JOIN sessions s ON s.session_id = f.session_id
                WHERE s.overall_verdict != 'CLEAN'
                GROUP BY f.category_code
                ORDER BY count DESC
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
    verdict:           Optional[str] = None,
    status:            Optional[str] = None,
    language:          Optional[str] = None,
    reviewer:          Optional[str] = None,
    assigned_reviewer: Optional[str] = None,
):
    rows = fetch_sessions(
        verdict_filter=verdict,
        status_filter=status,
        language_filter=language,
        reviewer_filter=reviewer,
        assigned_reviewer_filter=assigned_reviewer,
    )

    # Enrich each row with flag counts (total, LLM/REGEX, manual) — excludes DISMISSED
    # and with turn_count from the turns table.
    try:
        with get_connection() as conn:
            flag_data = {}
            for r in conn.execute("""
                SELECT
                    session_id,
                    COUNT(*) AS flag_count,
                    SUM(CASE WHEN detection_layer IN ('LLM','REGEX') THEN 1 ELSE 0 END) AS llm_flag_count,
                    SUM(CASE WHEN detection_layer = 'MANUAL' THEN 1 ELSE 0 END) AS manual_flag_count
                FROM flags
                WHERE detection_layer NOT IN ('DISMISSED')
                GROUP BY session_id
            """).fetchall():
                flag_data[r["session_id"]] = dict(r)

            turn_data = {}
            for r in conn.execute("""
                SELECT session_id, COUNT(*) AS turn_count
                FROM turns
                GROUP BY session_id
            """).fetchall():
                turn_data[r["session_id"]] = r["turn_count"]

        for row in rows:
            fd = flag_data.get(row["session_id"], {})
            row["flag_count"]        = fd.get("flag_count",        0)
            row["llm_flag_count"]    = fd.get("llm_flag_count",    0)
            row["manual_flag_count"] = fd.get("manual_flag_count", 0)
            row["turn_count"]        = turn_data.get(row["session_id"], 0)
    except Exception:
        for row in rows:
            row["flag_count"]        = None
            row["llm_flag_count"]    = None
            row["manual_flag_count"] = None
            row["turn_count"]        = None

    return rows


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
    """
    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT f.*,
                       CASE WHEN f.detection_layer = 'MANUAL'
                            THEN rl.reviewer_id ELSE NULL END AS flagged_by
                FROM flags f
                LEFT JOIN review_log rl
                    ON rl.flag_id = f.flag_id AND rl.action = 'MANUAL_FLAG'
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
                       CASE WHEN f.detection_layer = 'MANUAL'
                            THEN rl.reviewer_id ELSE NULL END AS flagged_by
                FROM flags f
                LEFT JOIN review_log rl
                    ON rl.flag_id = f.flag_id AND rl.action = 'MANUAL_FLAG'
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
        with get_connection() as conn:
            row = conn.execute(
                """SELECT overall_verdict, review_status, reviewer_id
                   FROM sessions
                   WHERE session_id = ?""",
                (session_id,),
            ).fetchone()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "success": True,
        "session_id": session_id,
        "overall_verdict": row["overall_verdict"] if row else None,
        "review_status": row["review_status"] if row else None,
        "reviewer_id": row["reviewer_id"] if row else body.reviewer_id,
    }


@app.post("/sessions/{session_id}/manual-flag")
def manual_flag(session_id: str, body: ManualFlagRequest):
    """Insert a reviewer-created flag and record it in the review log."""
    conn = get_connection()
    try:
        flag_severity = _flag_severity_for_code(body.category_code)
        with conn:
            cur = conn.execute(
                """
                INSERT INTO flags
                    (session_id, turn_id, category_code, detection_layer, severity,
                     confidence_score, reasoning, false_positive_risk, pattern_matched)
                VALUES (?, ?, ?, 'MANUAL', ?, 1.0, ?, 'LOW', ?)
                """,
                (
                    session_id,
                    body.turn_id,
                    body.category_code,
                    flag_severity,
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
            new_verdict, review_status = _recompute_session_verdict(
                conn,
                session_id,
                reviewer_id=body.reviewer_id,
                note=body.note or "Manual flag added",
            )
        return {
            "success": True,
            "flag_id": flag_id,
            "overall_verdict": new_verdict,
            "review_status": review_status,
            "reviewer_id": body.reviewer_id,
        }
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


@app.post("/sessions/{session_id}/final-approve")
def final_approve_session(session_id: str, body: LockRequest):
    if not _is_final_approver(body.reviewer_id):
        raise HTTPException(
            status_code=403,
            detail="Only Amogh can final approve sessions.",
        )
    try:
        lock_session(session_id, body.reviewer_id)
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO review_log
                   (session_id, flag_id, action, reviewer_id, note)
                   VALUES (?, NULL, 'FINAL_APPROVE', ?, ?)""",
                (session_id, body.reviewer_id, "Final approved and locked"),
            )
        with get_connection() as conn:
            row = conn.execute(
                """SELECT session_id, overall_verdict, review_status,
                          reviewer_id, locked_by, locked_at
                   FROM sessions
                   WHERE session_id = ?""",
                (session_id,),
            ).fetchone()
        return {
            "success": True,
            "session": dict(row) if row else None,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/sessions/final-approve-bulk")
def final_approve_bulk(body: BulkFinalApproveRequest):
    if not _is_final_approver(body.reviewer_id):
        raise HTTPException(
            status_code=403,
            detail="Only Amogh can final approve sessions.",
        )

    session_ids = [str(s).strip() for s in body.session_ids if str(s).strip()]
    if not session_ids:
        return {"success": True, "approved_count": 0, "session_ids": []}

    eligible_statuses = {
        "REVIEWED", "CONFIRMED", "OVERRIDDEN", "NEEDS_FINAL_REVIEW",
    }
    placeholders = ", ".join("?" for _ in session_ids)

    try:
        with get_connection() as conn:
            rows = conn.execute(
                f"""SELECT session_id, review_status
                    FROM sessions
                    WHERE session_id IN ({placeholders})""",
                session_ids,
            ).fetchall()
            approved_ids = [
                row["session_id"]
                for row in rows
                if row["review_status"] in eligible_statuses
            ]

            if approved_ids:
                approved_placeholders = ", ".join("?" for _ in approved_ids)
                conn.execute(
                    f"""UPDATE sessions
                        SET review_status = 'LOCKED',
                            locked_by = ?,
                            locked_at = datetime('now')
                        WHERE session_id IN ({approved_placeholders})""",
                    [body.reviewer_id, *approved_ids],
                )
                conn.executemany(
                    """INSERT INTO review_log
                       (session_id, flag_id, action, reviewer_id, note)
                       VALUES (?, NULL, 'FINAL_APPROVE', ?, ?)""",
                    [
                        (sid, body.reviewer_id, "Bulk final approved and locked")
                        for sid in approved_ids
                    ],
                )

        return {
            "success": True,
            "approved_count": len(approved_ids),
            "session_ids": approved_ids,
        }
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
    conn = get_connection()
    try:
        with conn:
            original = conn.execute(
                "SELECT session_id, turn_id, category_code, pattern_matched FROM flags WHERE flag_id = ?", (flag_id,)
            ).fetchone()
            if original is None:
                raise HTTPException(status_code=404, detail=f"Flag {flag_id} not found")
            existing = conn.execute(
                """SELECT flag_id FROM flags
                   WHERE session_id = ? AND category_code = ? AND detection_layer = 'AMENDED'""",
                (original["session_id"], original["category_code"]),
            ).fetchone()
            if existing:
                return {"success": True, "message": "Already amended"}
            cur = conn.execute(
                """
                INSERT INTO flags
                    (session_id, turn_id, category_code, detection_layer, severity,
                     confidence_score, reasoning, false_positive_risk, pattern_matched)
                VALUES (?, ?, ?, 'AMENDED', ?, 1.0, ?, 'LOW', ?)
                """,
                (
                    original["session_id"],
                    original["turn_id"],
                    body.category_code,
                    body.severity,
                    body.reasoning,
                    original["pattern_matched"] if original["pattern_matched"] else f"Amended by {body.reviewer_id}",
                ),
            )
            new_verdict, review_status = _recompute_session_verdict(
                conn,
                original["session_id"],
                reviewer_id=body.reviewer_id,
                note=body.reasoning or "Flag amended",
            )
        return {
            "success": True,
            "new_flag_id": cur.lastrowid,
            "overall_verdict": new_verdict,
            "review_status": review_status,
            "reviewer_id": body.reviewer_id,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        conn.close()


@app.post("/flags/{flag_id}/confirm")
def confirm_flag_endpoint(flag_id: int, body: LockRequest):
    with get_connection() as conn:
        flag = conn.execute(
            "SELECT flag_id FROM flags WHERE flag_id = ?", (flag_id,)
        ).fetchone()
    if flag is None:
        raise HTTPException(status_code=404, detail=f"Flag {flag_id} not found")
    try:
        confirm_flag(flag_id, body.reviewer_id)
        return {"success": True, "flag_id": flag_id}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/flags/{flag_id}/dismiss")
def dismiss_flag(flag_id: int, body: DismissFlagRequest):
    conn = get_connection()
    try:
        with conn:
            original = conn.execute(
                "SELECT session_id, turn_id, category_code, pattern_matched FROM flags WHERE flag_id = ?",
                (flag_id,),
            ).fetchone()
            if original is None:
                raise HTTPException(status_code=404, detail=f"Flag {flag_id} not found")
            existing = conn.execute(
                """SELECT flag_id FROM flags
                   WHERE session_id = ? AND category_code = ? AND detection_layer = 'DISMISSED'""",
                (original["session_id"], original["category_code"]),
            ).fetchone()
            if existing:
                return {"success": True, "message": "Already dismissed"}
            conn.execute(
                """
                INSERT INTO flags
                    (session_id, turn_id, category_code, detection_layer, severity,
                     confidence_score, reasoning, false_positive_risk, pattern_matched)
                VALUES (?, ?, ?, 'DISMISSED', 'LOW', 0.0, ?, 'HIGH', ?)
                """,
                (
                    original["session_id"],
                    original["turn_id"],
                    original["category_code"],
                    f"Dismissed by reviewer: {body.note}",
                    original["pattern_matched"] if original["pattern_matched"] else f"Dismissed by {body.reviewer_id}",
                ),
            )
            conn.execute(
                """
                INSERT INTO review_log (session_id, flag_id, action, reviewer_id, note)
                VALUES (?, ?, 'FLAG_DISMISSED', ?, ?)
                """,
                (original["session_id"], flag_id, body.reviewer_id, body.note),
            )
            new_verdict, review_status = _recompute_session_verdict(
                conn,
                original["session_id"],
                reviewer_id=body.reviewer_id,
                note=body.note or "Flag dismissed",
            )
        return {
            "success": True,
            "overall_verdict": new_verdict,
            "review_status": review_status,
            "reviewer_id": body.reviewer_id,
        }
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
def export_csv():
    """Download all reviewed sessions as a CSV file."""
    date_str = datetime.now().strftime("%Y%m%d")

    try:
        with get_connection() as conn:
            rows = conn.execute("""
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
                WHERE s.review_status != 'PENDING'
                ORDER BY s.reviewed_at DESC
            """).fetchall()
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
