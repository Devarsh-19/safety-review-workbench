"""SQLite connection and query helpers for the audio review database.

Separate universe from the chat DB (store/db.py): its own file, own env var
(AUDIO_DB_PATH), same workflow model (PENDING -> SUBMITTED_FOR_REVIEW -> LOCKED).
"""

import os
import re
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIO_DB_PATH = str(_PROJECT_ROOT / os.getenv("AUDIO_DB_PATH", "store/audio_review.db"))
_SCHEMA_PATH = Path(__file__).parent / "audio_schema.sql"

SPEAKER_ROLES = ("ASTROLOGER", "USER")
SESSION_RISKS = ("HIGH", "MEDIUM", "LOW")


def _normalize_audio_verdict(verdict: str | None) -> str | None:
    """Audio session verdicts are binary: FLAGGED or CLEAN.
    Legacy SEVERE values are treated as FLAGGED."""
    if verdict is None:
        return None
    normalized = str(verdict).strip().upper()
    if not normalized:
        return None
    if normalized == "SEVERE":
        return "FLAGGED"
    return normalized


def _audio_verdict_sql(column: str) -> str:
    return f"CASE WHEN {column} = 'SEVERE' THEN 'FLAGGED' ELSE {column} END"


AUDIO_OVERALL_VERDICT_SQL = _audio_verdict_sql("s.overall_verdict")
AUDIO_ASTROTALK_VERDICT_SQL = _audio_verdict_sql("s.astrotalk_verdict")


# --- Language classification -------------------------------------------------
# The audio `lang` column is free-text and may hold a single language ("hindi",
# "tamil") or a combination ("hindi, english", "hindi-english"). These helpers
# are the single source of truth for what counts as an allowed language, shared
# by the DB (Multilingual reviewer filter) and scripts/assign_multilingual_audio.py.
KEEP_LANGUAGES = {"hindi", "english", "hinglish"}

# Connector words that can appear between languages in a compound value; ignored
# when tokenising ("hindi and english" -> {hindi, english}).
_LANG_CONNECTORS = {"and", "mix", "mixed", "with"}


def language_tokens(lang: str) -> list[str]:
    """Split a free-text lang value into recognised language tokens (lower-cased,
    split on any run of non-letters, connector words dropped). [] when nothing
    meaningful remains (NULL / empty / punctuation only)."""
    norm = (lang or "").strip().lower()
    return [t for t in re.split(r"[^a-z]+", norm) if t and t not in _LANG_CONNECTORS]


def is_all_allowed_language(lang) -> bool:
    """True when lang has >=1 token and EVERY token is an allowed language."""
    tokens = language_tokens(lang)
    return bool(tokens) and all(t in KEEP_LANGUAGES for t in tokens)


def is_multilingual_language(lang) -> int:
    """1 when lang has >=1 recognised token AND at least one token is NOT an
    allowed (Hindi/English/Hinglish) language; else 0. Used at assignment time
    (scripts/assign_audio_sessions.py) to route regional sessions to the
    "Multilingual" reviewer. Sessions with no recognisable language return 0."""
    tokens = language_tokens(lang)
    return int(bool(tokens) and any(t not in KEEP_LANGUAGES for t in tokens))


def get_audio_connection() -> sqlite3.Connection:
    # timeout + WAL + busy_timeout: many reviewers hit the API concurrently
    # from different laptops. WAL lets readers and the writer proceed in
    # parallel; busy_timeout makes a second writer wait instead of failing
    # with "database is locked".
    conn = sqlite3.connect(AUDIO_DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def initialise_audio_db() -> None:
    from engine.verdict_rules import get_db_confidence_for_verdict

    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    with get_audio_connection() as conn:
        conn.executescript(schema)

    # Columns added after the initial schema — same pattern as store/db.py:
    # they live here (not in audio_schema.sql) so existing DBs get them too.
    migrations = [
        "ALTER TABLE audio_sessions ADD COLUMN audio_url TEXT",         # HLS (.m3u8) recording URL
        "ALTER TABLE audio_flags ADD COLUMN reasoning TEXT",            # reviewer note on amendments
        "ALTER TABLE audio_sessions ADD COLUMN confidence_score REAL",  # verdict confidence, same scale as chat
        "ALTER TABLE audio_sessions ADD COLUMN astrotalk_verdict TEXT", # verdict from original astrotalk pipeline
        "ALTER TABLE audio_sessions ADD COLUMN has_video INTEGER",      # source media contains a video stream
        "ALTER TABLE audio_flags ADD COLUMN ts_start REAL",             # exact flagged span start
        "ALTER TABLE audio_flags ADD COLUMN ts_end REAL",               # exact flagged span end
        # Flag-level audit trail, same as chat's flags table
        "ALTER TABLE audio_flags ADD COLUMN confirmed_by TEXT",
        "ALTER TABLE audio_flags ADD COLUMN confirmed_at TEXT",
        "ALTER TABLE audio_flags ADD COLUMN created_by TEXT",           # reviewer who made an amendment
        "ALTER TABLE audio_sessions ADD COLUMN duration_seconds REAL",  # real audio duration from the pipeline (ffprobe)
        "ALTER TABLE audio_sessions ADD COLUMN manual_risk_level TEXT", # L1's whole-session risk rating: HIGH / MEDIUM / LOW
        "ALTER TABLE audio_sessions ADD COLUMN session_note TEXT",      # reviewer's overall observation note (chat parity)
        "ALTER TABLE audio_sessions ADD COLUMN astrotalk_severity TEXT",# auto-derived HIGH / MEDIUM / LOW from the Severity Criteria (chat parity)
    ]
    with get_audio_connection() as conn:
        for migration in migrations:
            try:
                conn.execute(migration)
                conn.commit()
            except Exception:
                pass  # Column already exists — safe to ignore

        # Audio session verdicts are binary: any legacy SEVERE rows collapse to
        # FLAGGED so the DB, API, and frontend stay aligned.
        conn.execute(
            """UPDATE audio_sessions
               SET overall_verdict = 'FLAGGED',
                   confidence_score = ?
               WHERE overall_verdict = 'SEVERE'""",
            (get_db_confidence_for_verdict("FLAGGED"),),
        )
        conn.execute(
            """UPDATE audio_sessions
               SET astrotalk_verdict = 'FLAGGED'
               WHERE astrotalk_verdict = 'SEVERE'"""
        )
        conn.commit()

    print(f"Audio database initialised at {AUDIO_DB_PATH}")


def _normalize_audio_session_row(row: dict) -> dict:
    normalized = dict(row)
    if "has_video" in normalized and normalized["has_video"] is not None:
        normalized["has_video"] = bool(normalized["has_video"])
    if "overall_verdict" in normalized:
        normalized["overall_verdict"] = _normalize_audio_verdict(normalized["overall_verdict"])
    if "astrotalk_verdict" in normalized:
        normalized["astrotalk_verdict"] = _normalize_audio_verdict(normalized["astrotalk_verdict"])
    return normalized


def fetch_audio_sessions_page(
    *,
    status: str = None,
    search: str = None,
    reviewer_role: str = None,
    reviewer_name: str = None,
    assigned_to: str = None,
    has_video: str = None,
    lang: str = None,
    duration_min: float = None,
    duration_max: float = None,
    flags_min: int = None,
    flags_max: int = None,
    pauses_min: int = None,
    pauses_max: int = None,
    roles: str = None,
    reviewer: str = None,
    verdict: str = None,
    astrotalk_verdict: str = None,
    flag_category: str = None,
    sort_col: str = None,
    sort_dir: str = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Paginated audio session list with per-session flag/segment counts.
    Every queue column is filterable server-side so a filter applies to the
    whole table, not just the visible page.
    Role-based default visibility mirrors the chat DB (fetch_sessions_page):
    - L1 (no explicit status filter): submitted/locked sessions hidden
    - L2 (no explicit status filter): only SUBMITTED_FOR_REVIEW sessions shown
    - L1 with a name: only sessions assigned to them. "Multilingual" is a plain
      assignee like any other L1 reviewer — regional (non-Hindi/English/Hinglish)
      sessions are routed to it at assignment time (assign_audio_sessions.py).
    - "Astrotalk Review": read-only L1 client persona — only LOCKED sessions
      that were manually submitted for review (submitted_by not LLM/AUTO_LOCK)
      and carry an active NSFW_EXPLICIT flag
    """
    where, params = ["1=1"], []

    # "Astrotalk Review" is a read-only L1 client persona hard-restricted to
    # finalised (LOCKED) sessions that were MANUALLY submitted for review (by a
    # human L1 — not auto-submitted by the LLM ingest or the auto-lock scripts)
    # and carry an active NSFW_EXPLICIT flag. Enforced unconditionally so no
    # status/assignee filter can widen the view; other filters (language,
    # duration …) still narrow within this subset. The NSFW_EXPLICIT EXISTS
    # mirrors the flag_category filter below: active flags only (DISMISSED rows
    # and amended originals excluded).
    if reviewer_name == "Astrotalk Review":
        where.append("s.review_status = 'LOCKED'")
        where.append("s.submitted_by IS NOT NULL AND s.submitted_by NOT IN ('LLM', 'AUTO_LOCK')")
        where.append(
            """EXISTS (SELECT 1 FROM audio_flags af
                       WHERE af.s_id = s.s_id
                         AND UPPER(REPLACE(REPLACE(af.intent,'-','_'),' ','_')) = 'NSFW_EXPLICIT'
                         AND (af.status IS NULL OR af.status != 'DISMISSED')
                         AND af.flag_id NOT IN (
                             SELECT parent_flag_id FROM audio_flags
                             WHERE parent_flag_id IS NOT NULL))"""
        )
    elif status:
        where.append("s.review_status = ?"); params.append(status)
    elif not flag_category:
        # Role-based default visibility applies only when neither an explicit
        # status nor a flag-category filter is set: filtering by flag should
        # surface every matching session regardless of review status (chat parity).
        if reviewer_name == "Locked":
            where.append("s.review_status = 'LOCKED'")
        elif reviewer_role == "L1":
            where.append("s.review_status NOT IN ('SUBMITTED_FOR_REVIEW','LOCKED')")
        elif reviewer_role == "L2":
            where.append("s.review_status = 'SUBMITTED_FOR_REVIEW'")

    # L1 reviewers see sessions of ALL languages: the dashboard is no longer
    # scoped to the reviewer's own assignments. Regional-language sessions were
    # previously routed to the "Multilingual" assignee and therefore hidden from
    # everyone else's dashboard; dropping the per-reviewer assignee scope makes
    # every language display for every L1 reviewer. Status-based visibility above
    # still applies (L1 sees only reviewable sessions, not others' submitted/locked).
    # The explicit "Assigned To" filter (L2 dropdown) is still honoured below.
    if assigned_to:
        where.append("s.assigned_to LIKE ?"); params.append(f"%{assigned_to}%")

    if search:
        where.append("CAST(s.s_id AS TEXT) LIKE ?"); params.append(f"%{search}%")
    if has_video in ("1", "true", "yes"):
        where.append("s.has_video = 1")
    elif has_video in ("0", "false", "no"):
        where.append("(s.has_video = 0 OR s.has_video IS NULL)")
    if lang:
        lang_values = [v.strip() for v in lang.split(',') if v.strip()]
        if lang_values:
            like_clauses = []
            for lv in lang_values:
                like_clauses.append("s.lang LIKE ?")
                params.append(f"%{lv}%")
            where.append(f"({' OR '.join(like_clauses)})")
    if duration_min is not None:
        where.append("COALESCE(s.duration_seconds, sc.max_ts_end, 0) >= ?"); params.append(duration_min)
    if duration_max is not None:
        where.append("COALESCE(s.duration_seconds, sc.max_ts_end, 0) <= ?"); params.append(duration_max)
    if flags_min is not None:
        where.append("COALESCE(fc.flag_count, 0) >= ?"); params.append(flags_min)
    if flags_max is not None:
        where.append("COALESCE(fc.flag_count, 0) <= ?"); params.append(flags_max)
    if pauses_min is not None:
        where.append("CASE WHEN s.pauses IS NOT NULL AND s.pauses != '' THEN COALESCE(json_array_length(s.pauses), 0) ELSE 0 END >= ?")
        params.append(pauses_min)
    if pauses_max is not None:
        where.append("CASE WHEN s.pauses IS NOT NULL AND s.pauses != '' THEN COALESCE(json_array_length(s.pauses), 0) ELSE 0 END <= ?")
        params.append(pauses_max)
    if roles == "assigned":
        where.append("s.speaker1_role IS NOT NULL AND s.speaker2_role IS NOT NULL")
    elif roles == "unassigned":
        where.append("(s.speaker1_role IS NULL OR s.speaker2_role IS NULL)")
    if reviewer:
        where.append("(s.submitted_by LIKE ? OR s.reviewer_id LIKE ?)")
        params.extend([f"%{reviewer}%", f"%{reviewer}%"])
    normalized_verdict = _normalize_audio_verdict(verdict)
    normalized_astrotalk_verdict = _normalize_audio_verdict(astrotalk_verdict)
    if normalized_verdict:
        where.append(f"{AUDIO_OVERALL_VERDICT_SQL} = ?"); params.append(normalized_verdict)
    if normalized_astrotalk_verdict:
        where.append(f"{AUDIO_ASTROTALK_VERDICT_SQL} = ?"); params.append(normalized_astrotalk_verdict)
    if flag_category:
        # Only sessions carrying an ACTIVE flag of this intent: DISMISSED rows
        # and amended originals are excluded, matching the flag_count column and
        # the violation heatmap. Intents are stored uppercase (ABUSIVE_LANGUAGE).
        where.append(
            """EXISTS (SELECT 1 FROM audio_flags af
                       WHERE af.s_id = s.s_id
                         AND UPPER(REPLACE(REPLACE(af.intent,'-','_'),' ','_')) = ?
                         AND (af.status IS NULL OR af.status != 'DISMISSED')
                         AND af.flag_id NOT IN (
                             SELECT parent_flag_id FROM audio_flags
                             WHERE parent_flag_id IS NOT NULL))"""
        )
        params.append(flag_category.strip().upper().replace("-", "_").replace(" ", "_"))
    where_sql = " AND ".join(where)

    # flag_count counts ACTIVE flags only, matching the viewer and the submit
    # gate: DISMISSED rows are excluded (chat parity — chat hard-deletes them),
    # and originals that have an amendment are excluded so an edited flag
    # counts once (the amendment row), not twice.
    base = f"""
        FROM audio_sessions s
        LEFT JOIN (
            SELECT s_id, COUNT(*) AS flag_count FROM audio_flags
            WHERE (status != 'DISMISSED' OR status IS NULL)
              AND flag_id NOT IN (
                  SELECT parent_flag_id FROM audio_flags
                  WHERE parent_flag_id IS NOT NULL
              )
            GROUP BY s_id
        ) fc ON fc.s_id = s.s_id
        LEFT JOIN (
            SELECT s_id, COUNT(*) AS segment_count, MAX(ts_end) AS max_ts_end
            FROM audio_segments GROUP BY s_id
        ) sc ON sc.s_id = s.s_id
        WHERE {where_sql}
    """
    # Safe columns for sorting
    valid_cols = {
        's_id': 's.s_id',
        'duration': 'COALESCE(s.duration_seconds, sc.max_ts_end, 0)',
        'segments': 'segment_count',
        'flags': 'flag_count',
        'pauses': 'pause_count',
        'verdict': AUDIO_OVERALL_VERDICT_SQL,
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
                       COALESCE(s.duration_seconds, sc.max_ts_end, 0) AS duration_seconds,
                       CASE WHEN s.pauses IS NOT NULL AND s.pauses != '' THEN COALESCE(json_array_length(s.pauses), 0) ELSE 0 END AS pause_count
                {base}
                {order_clause}
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
    return [_normalize_audio_session_row(dict(r)) for r in rows], total


def fetch_audio_session_detail(s_id: int) -> dict:
    with get_audio_connection() as conn:
        session = conn.execute(
            "SELECT * FROM audio_sessions WHERE s_id = ?", (s_id,)
        ).fetchone()
        segments = conn.execute(
            "SELECT * FROM audio_segments WHERE s_id = ? ORDER BY seg_id", (s_id,)
        ).fetchall()
        flags = conn.execute(
            "SELECT * FROM audio_flags WHERE s_id = ? ORDER BY COALESCE(ts_start, 1e18), seg_id, flag_id", (s_id,)
        ).fetchall()
    return {
        "session": _normalize_audio_session_row(dict(session)) if session else None,
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
    session's active flags.
    Audio LLM session verdicts are binary: any active non-dismissed flag makes
    the session FLAGGED; otherwise it is CLEAN."""
    from engine.verdict_rules import get_db_confidence_for_verdict

    rows = conn.execute(
        "SELECT flag_id, intent, status, parent_flag_id FROM audio_flags WHERE s_id = ?", (s_id,)
    ).fetchall()
    has_active_flags = any(
        r["status"] != "DISMISSED"
        for r in _active_audio_flag_rows(rows)
    )
    verdict    = "FLAGGED" if has_active_flags else "CLEAN"
    confidence = get_db_confidence_for_verdict(verdict)
    conn.execute(
        "UPDATE audio_sessions SET overall_verdict = ?, confidence_score = ? WHERE s_id = ?",
        (verdict, confidence, s_id),
    )
    return verdict


def get_audio_flag_summary(s_id: int) -> dict:
    """Flag counts for the submit gate — same semantics as the chat workflow:
    a session can be submitted only when every active flag is actioned.
    Unlike chat (where dismissal hard-deletes the row), audio keeps dismissed
    flags with status = 'DISMISSED' so they can be restored; both CONFIRMED
    and DISMISSED count as actioned."""
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


def set_speaker_roles(s_id: int, speaker1_role: str, speaker2_role: str,
                      reviewer_id: str = None) -> None:
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
        conn.execute(
            """INSERT INTO audio_review_log (s_id, action, reviewer_id, note)
               VALUES (?, 'SET_SPEAKER_ROLES', ?, ?)""",
            (s_id, reviewer_id, f"speaker1={speaker1_role}, speaker2={speaker2_role}"),
        )


def set_audio_session_risk(s_id: int, risk: str) -> None:
    """Persist the L1 reviewer's whole-session risk rating."""
    if risk not in SESSION_RISKS:
        raise ValueError(f"Risk must be one of {SESSION_RISKS}")
    with get_audio_connection() as conn:
        conn.execute(
            "UPDATE audio_sessions SET manual_risk_level = ? WHERE s_id = ?",
            (risk, s_id),
        )


def submit_audio_session(s_id: int, reviewer_id: str, note: str = None) -> None:
    with get_audio_connection() as conn:
        # Recompute the verdict from active flags first — same as the chat DB's
        # submit_session_for_review, so the stored verdict always reflects the
        # flag state at the moment of submission.
        recompute_audio_session_verdict(s_id, conn)
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
        conn.execute(
            """INSERT INTO audio_review_log (s_id, action, reviewer_id, note)
               VALUES (?, 'SUBMIT', ?, ?)""",
            (s_id, reviewer_id, note or ""),
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
        conn.execute(
            """INSERT INTO audio_review_log (s_id, action, reviewer_id, note)
               VALUES (?, 'LOCK', ?, '')""",
            (s_id, reviewer_id),
        )


def lock_all_submitted_audio_sessions(reviewer_id: str) -> int:
    """Bulk-lock every audio session currently SUBMITTED_FOR_REVIEW.

    Mirrors lock_audio_session (including one 'LOCK' audio_review_log row per
    session); returns how many were locked so the L2 'Lock all submitted'
    action can report the count.
    """
    with get_audio_connection() as conn:
        s_ids = [
            r["s_id"] for r in conn.execute(
                "SELECT s_id FROM audio_sessions WHERE review_status = 'SUBMITTED_FOR_REVIEW'"
            ).fetchall()
        ]
        if not s_ids:
            return 0
        conn.execute(
            """UPDATE audio_sessions
               SET review_status = 'LOCKED',
                   locked_by     = ?,
                   locked_at     = datetime('now')
               WHERE review_status = 'SUBMITTED_FOR_REVIEW'""",
            (reviewer_id,),
        )
        conn.executemany(
            """INSERT INTO audio_review_log (s_id, action, reviewer_id, note)
               VALUES (?, 'LOCK', ?, '')""",
            [(s_id, reviewer_id) for s_id in s_ids],
        )
        return len(s_ids)


def unlock_audio_session(s_id: int, reviewer_id: str = None) -> None:
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
        conn.execute(
            """INSERT INTO audio_review_log (s_id, action, reviewer_id, note)
               VALUES (?, 'UNLOCK', ?, '')""",
            (s_id, reviewer_id),
        )
