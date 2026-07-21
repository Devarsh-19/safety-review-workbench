"""
export_all_ingested_sessions.py

Exports EVERY ingested session (~4 lakh) as ONE ROW PER SESSION with a
clean/flagged indicator. Unlike export_submitted_flags.py this is not limited
to submitted sessions and does not expand to per-turn rows — it is a
session-level inventory of the whole database.

A session counts as flagged if it has at least one ACTIVE flag (amendment if
edited, else original — same active-flag logic as export_submitted_flags.py).

Output columns (one row per session):
    session_id
    session_date
    session_type          - 'chat' or 'voice'
    duration_minutes
    n_turns               - total turns in the session
    review_status         - PENDING / SUBMITTED_FOR_REVIEW / LOCKED / ...
    verdict               - overall_verdict (CLEAN / FLAGGED / SEVERE, blank if unreviewed)
    reviewed_by           - submitted_by, fallback reviewer_id
    astrotalk_flagged     - platform's own flag (0/1)
    is_flagged            - 1 if the session has any active flag, else 0
    n_active_flags        - number of active flags on the session
    flag_categories       - active flag counts per category, e.g. {NSFW:3,CSAM:4}
    flag_sources          - distinct active flag sources (LLM/REGEX/MANUAL), ';'-joined

Covers BOTH databases (one row per session in each):
  chat  (store/astrotalk.db)     sessions / turns    / flags
  audio (store/audio_review.db)  audio_sessions / audio_segments / audio_flags
Use --db to run only one. reviewed_at is date-only and falls back to
locked_at then submitted_at when the real reviewed_at is missing.

Read-only — never modifies either database.

Usage:
  python scripts/export_all_ingested_sessions.py                 # chat + audio
  python scripts/export_all_ingested_sessions.py --db audio      # audio only
  python scripts/export_all_ingested_sessions.py --out C:/path/to/chat.csv
  python scripts/export_all_ingested_sessions.py --audio-out C:/path/to/audio.csv
"""

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import get_connection, DB_PATH  # noqa: E402
from store.audio_db import get_audio_connection, AUDIO_DB_PATH  # noqa: E402
from engine.language_detector import LANGUAGE_MAP  # noqa: E402

CSV_COLUMNS = [
    "session_id", "session_date", "session_type", "language", "duration_minutes",
    "n_turns",
    "review_status", "verdict", "reviewed_by", "reviewed_at", "locked_by",
    "session_note",
    "astrotalk_flagged",
    "is_flagged", "n_active_flags", "user_flag_count", "astrologer_flag_count",
    "flag_categories", "flag_sources",
]


def _language(code) -> str:
    """Human-readable language taken from the INPUT language_code (1-24) mapped
    via LANGUAGE_MAP (English/Hindi/...); '' when the input has no code. The
    langdetect-derived language_detected is intentionally not used."""
    try:
        return LANGUAGE_MAP.get(int(code), "") if code not in (None, "") else ""
    except (TypeError, ValueError):
        return ""

# Audio inventory — same shape mapped onto the audio schema. Audio has no
# session_date/session_type; it carries lang + a real duration in seconds, and
# its platform signal is astrotalk_verdict (CLEAN/FLAGGED) not astrotalk_flagged.
AUDIO_CSV_COLUMNS = [
    "s_id", "language", "session_type", "duration_minutes", "n_segments",
    "review_status", "verdict", "reviewed_by", "reviewed_at", "locked_by",
    "session_note",
    "astrotalk_verdict",
    "is_flagged", "n_active_flags", "user_flag_count", "astrologer_flag_count",
    "flag_categories", "flag_sources",
]


def _active_flag_summaries(conn, speaker_by_turn: dict) -> dict[str, dict]:
    """Per-session summary of ACTIVE flags (amendment if present, else original).

    Single pass over the whole flags table — no per-session queries. Each active
    flag is also bucketed by the speaker of its turn (user vs astrologer) via
    speaker_by_turn[(session_id, turn_id)]; flags with a NULL/unresolvable turn
    fall into neither bucket, so user_n + astro_n <= n.
    """
    rows = conn.execute(
        """SELECT flag_id, parent_flag_id, session_id, turn_id, source,
                  detection_layer, category_code
           FROM flags"""
    ).fetchall()

    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}

    summaries: dict[str, dict] = {}
    for r in rows:
        is_active = (
            r["parent_flag_id"] is not None
            or r["flag_id"] not in amended_parents
        )
        if not is_active:
            continue
        s = summaries.setdefault(
            r["session_id"],
            {"n": 0, "sources": set(), "categories": {}, "user_n": 0, "astro_n": 0},
        )
        s["n"] += 1
        src = r["source"] or r["detection_layer"]
        if src:
            s["sources"].add(src)
        cat = r["category_code"] or "UNKNOWN"
        s["categories"][cat] = s["categories"].get(cat, 0) + 1
        speaker = speaker_by_turn.get((r["session_id"], r["turn_id"]))
        if speaker == "USER":
            s["user_n"] += 1
        elif speaker == "ASTROLOGER":
            s["astro_n"] += 1
    return summaries


def _active_audio_flag_summaries(conn, seg_speaker: dict, session_roles: dict) -> dict:
    """Audio counterpart of _active_flag_summaries over audio_flags.

    Active = amendment row, or original with no amendment; DISMISSED rows are
    excluded (audio keeps them, unlike chat). category = intent, source = LLM/
    MANUAL. Each active flag is bucketed by speaker role: its segment's raw
    diarization label (seg_speaker[(s_id, seg_id)]) is mapped to USER/ASTROLOGER
    via the session's speaker1_role/speaker2_role (session_roles[s_id][label]).
    Flags on an unresolvable segment, or whose role is unassigned, fall into
    neither bucket, so user_n + astro_n <= n.
    """
    rows = conn.execute(
        """SELECT flag_id, parent_flag_id, s_id, seg_id, source, intent, status
           FROM audio_flags"""
    ).fetchall()

    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}

    summaries: dict = {}
    for r in rows:
        is_active = (
            r["parent_flag_id"] is not None
            or r["flag_id"] not in amended_parents
        )
        if not is_active or (r["status"] or "") == "DISMISSED":
            continue
        s = summaries.setdefault(
            r["s_id"],
            {"n": 0, "sources": set(), "categories": {}, "user_n": 0, "astro_n": 0},
        )
        s["n"] += 1
        if r["source"]:
            s["sources"].add(r["source"])
        cat = r["intent"] or "UNKNOWN"
        s["categories"][cat] = s["categories"].get(cat, 0) + 1
        label = seg_speaker.get((r["s_id"], r["seg_id"]))
        role = session_roles.get(r["s_id"], {}).get(label)
        if role == "USER":
            s["user_n"] += 1
        elif role == "ASTROLOGER":
            s["astro_n"] += 1
    return summaries


def _format_categories(categories: dict[str, int]) -> str:
    """Render category counts as a compact dict string, e.g. {NSFW:3,CSAM:4}."""
    if not categories:
        return ""
    return "{" + ",".join(f"{k}:{v}" for k, v in sorted(categories.items())) + "}"


def export_chat(out_path: Path) -> None:
    conn = get_connection()

    print("  Loading turn counts...")
    n_turns_by_session = {
        r["session_id"]: r["n"]
        for r in conn.execute(
            "SELECT session_id, COUNT(*) AS n FROM turns GROUP BY session_id"
        ).fetchall()
    }

    print("  Loading turn speakers...")
    speaker_by_turn = {
        (r["session_id"], r["turn_id"]): r["speaker"]
        for r in conn.execute("SELECT session_id, turn_id, speaker FROM turns").fetchall()
    }

    print("  Loading active flags...")
    flag_summary = _active_flag_summaries(conn, speaker_by_turn)

    print("  Exporting sessions...")
    n_sessions = 0
    n_flagged = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()

        for s in conn.execute(
            """SELECT session_id, session_date, session_type,
                      language_code, duration_minutes,
                      review_status, overall_verdict, submitted_by, reviewer_id,
                      DATE(COALESCE(reviewed_at, locked_at, submitted_at)) AS reviewed_at,
                      locked_by, session_note,
                      astrotalk_flagged
               FROM sessions ORDER BY session_id"""
        ):
            n_sessions += 1
            fs = flag_summary.get(s["session_id"])
            if fs:
                n_flagged += 1
            writer.writerow({
                "session_id":              s["session_id"],
                "session_date":            s["session_date"],
                "session_type":            s["session_type"],
                "language":                _language(s["language_code"]),
                "duration_minutes":        s["duration_minutes"],
                "n_turns":                 n_turns_by_session.get(s["session_id"], 0),
                "review_status":           s["review_status"] or "",
                "verdict":                 s["overall_verdict"] or "",
                "reviewed_by":             s["submitted_by"] or s["reviewer_id"] or "",
                "reviewed_at":             s["reviewed_at"] or "",
                "locked_by":               s["locked_by"] or "",
                "session_note":            s["session_note"] or "",
                "astrotalk_flagged":       s["astrotalk_flagged"],
                "is_flagged":              1 if fs else 0,
                "n_active_flags":          fs["n"] if fs else 0,
                "user_flag_count":         fs["user_n"] if fs else 0,
                "astrologer_flag_count":   fs["astro_n"] if fs else 0,
                "flag_categories":         _format_categories(fs["categories"]) if fs else "",
                "flag_sources":            ";".join(sorted(fs["sources"])) if fs else "",
            })

    conn.close()
    print(f"  Sessions exported  : {n_sessions}")
    print(f"  Flagged (>=1 flag) : {n_flagged}")
    print(f"  Clean (no flags)   : {n_sessions - n_flagged}")
    print(f"  CSV written        : {out_path}")


def export_audio(out_path: Path) -> None:
    conn = get_audio_connection()

    print("  Loading segment counts...")
    n_segments_by_session = {
        r["s_id"]: r["n"]
        for r in conn.execute(
            "SELECT s_id, COUNT(*) AS n FROM audio_segments GROUP BY s_id"
        ).fetchall()
    }

    print("  Loading segment speakers...")
    seg_speaker = {
        (r["s_id"], r["seg_id"]): r["speaker"]
        for r in conn.execute("SELECT s_id, seg_id, speaker FROM audio_segments").fetchall()
    }

    print("  Loading speaker roles...")
    # Map each session's raw diarization labels to the reviewer-assigned role.
    session_roles = {
        r["s_id"]: {"SPEAKER_1": r["speaker1_role"], "SPEAKER_2": r["speaker2_role"]}
        for r in conn.execute(
            "SELECT s_id, speaker1_role, speaker2_role FROM audio_sessions"
        ).fetchall()
    }

    print("  Loading active flags...")
    flag_summary = _active_audio_flag_summaries(conn, seg_speaker, session_roles)

    print("  Exporting audio sessions...")
    n_sessions = 0
    n_flagged = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=AUDIO_CSV_COLUMNS)
        writer.writeheader()

        for s in conn.execute(
            """SELECT s_id, lang, duration_seconds, review_status,
                      CASE WHEN overall_verdict = 'SEVERE' THEN 'FLAGGED'
                           ELSE overall_verdict END AS verdict,
                      CASE WHEN astrotalk_verdict = 'SEVERE' THEN 'FLAGGED'
                           ELSE astrotalk_verdict END AS astrotalk_verdict,
                      submitted_by, reviewer_id,
                      DATE(COALESCE(reviewed_at, locked_at, submitted_at)) AS reviewed_at,
                      locked_by, session_note
               FROM audio_sessions ORDER BY s_id"""
        ):
            n_sessions += 1
            fs = flag_summary.get(s["s_id"])
            if fs:
                n_flagged += 1
            dur = s["duration_seconds"]
            writer.writerow({
                "s_id":                    s["s_id"],
                "language":                s["lang"] or "",
                "session_type":            "voice",
                "duration_minutes":        round(dur / 60, 2) if dur else "",
                "n_segments":              n_segments_by_session.get(s["s_id"], 0),
                "review_status":           s["review_status"] or "",
                "verdict":                 s["verdict"] or "",
                "reviewed_by":             s["submitted_by"] or s["reviewer_id"] or "",
                "reviewed_at":             s["reviewed_at"] or "",
                "locked_by":               s["locked_by"] or "",
                "session_note":            s["session_note"] or "",
                "astrotalk_verdict":       s["astrotalk_verdict"] or "",
                "is_flagged":              1 if fs else 0,
                "n_active_flags":          fs["n"] if fs else 0,
                "user_flag_count":         fs["user_n"] if fs else 0,
                "astrologer_flag_count":   fs["astro_n"] if fs else 0,
                "flag_categories":         _format_categories(fs["categories"]) if fs else "",
                "flag_sources":            ";".join(sorted(fs["sources"])) if fs else "",
            })

    conn.close()
    print(f"  Sessions exported  : {n_sessions}")
    print(f"  Flagged (>=1 flag) : {n_flagged}")
    print(f"  Clean (no flags)   : {n_sessions - n_flagged}")
    print(f"  CSV written        : {out_path}")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Export ALL ingested sessions (one row per session) with clean/flagged "
                    "status, for the chat and/or audio database."
    )
    p.add_argument("--db", choices=("both", "chat", "audio"), default="both",
                   help="Which database(s) to export (default: both).")
    p.add_argument("--out", default=None, help="Chat output CSV path")
    p.add_argument("--audio-out", default=None, help="Audio output CSV path")
    args = p.parse_args()

    stamp = datetime.now().strftime("%Y%m%d")
    exports_dir = Path(__file__).resolve().parents[1] / "exports"

    if args.db in ("both", "chat"):
        out_path = Path(args.out) if args.out else exports_dir / f"all_ingested_sessions_{stamp}.csv"
        print("=" * 60)
        print("  Export ALL ingested CHAT sessions (clean vs flagged)")
        print(f"  DB: {DB_PATH}")
        print("=" * 60)
        export_chat(out_path)

    if args.db in ("both", "audio"):
        out_path = Path(args.audio_out) if args.audio_out else exports_dir / f"all_ingested_audio_sessions_{stamp}.csv"
        print("=" * 60)
        print("  Export ALL ingested AUDIO sessions (clean vs flagged)")
        print(f"  DB: {AUDIO_DB_PATH}")
        print("=" * 60)
        export_audio(out_path)


if __name__ == "__main__":
    main()
