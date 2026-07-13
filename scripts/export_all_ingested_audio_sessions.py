"""
export_all_ingested_audio_sessions.py

Audio counterpart of export_all_ingested_sessions.py: exports EVERY ingested
audio session as ONE ROW PER SESSION with a clean/flagged indicator. It is a
session-level inventory of the whole audio review database (store/audio_review.db),
not limited to submitted sessions and not expanded to per-segment rows.

A session counts as flagged if it has at least one ACTIVE flag. Audio active-flag
logic mirrors store/audio_db.py: the amendment row if a flag was edited, else the
original, and DISMISSED flags never count (unlike chat, audio keeps dismissed rows
rather than hard-deleting them).

Columns are the audio analogue of the chat export's fields so the two inventories
line up (session_id / session_type / duration / count / review_status / verdict /
platform verdict / is_flagged / n_active_flags / flag_categories / flag_sources),
with the naturally audio-native columns added (lang, has_video, manual_risk_level):

    session_id            - audio_sessions.s_id
    session_type          - constant 'audio'
    duration_seconds      - duration_seconds, fallback max segment ts_end
    duration_minutes      - duration_seconds / 60 (same fallback)
    n_segments            - number of diarized segments (audio's per-turn analogue)
    lang                  - detected language(s)
    has_video             - 1 if source media had a video stream, 0 if audio-only, blank if unknown
    review_status         - PENDING / SUBMITTED_FOR_REVIEW / LOCKED / REVIEWED
    verdict               - overall_verdict (CLEAN / FLAGGED / SEVERE, blank if unreviewed)
    astrotalk_verdict     - platform's own verdict (CLEAN / FLAGGED), blank if not marked
    manual_risk_level     - L1's whole-session risk rating (HIGH / MEDIUM / LOW)
    reviewed_by           - submitted_by, fallback reviewer_id
    is_flagged            - 1 if the session has any active flag, else 0
    n_active_flags        - number of active flags on the session
    flag_categories       - active flag counts per intent, e.g. {ABUSIVE_LANGUAGE:3,VIOLENCE:1}
    flag_sources          - distinct active flag sources (LLM/MANUAL), ';'-joined

Read-only — never modifies the database.

Usage:
  python scripts/export_all_ingested_audio_sessions.py
  python scripts/export_all_ingested_audio_sessions.py --out C:/path/to/file.csv
"""

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import get_audio_connection, AUDIO_DB_PATH  # noqa: E402

CSV_COLUMNS = [
    "session_id", "session_type", "duration_seconds", "duration_minutes", "n_segments",
    "lang", "has_video",
    "review_status", "verdict", "astrotalk_verdict", "manual_risk_level", "reviewed_by",
    "is_flagged", "n_active_flags", "flag_categories", "flag_sources",
]


def _active_flag_summaries(conn) -> dict:
    """Per-session summary of ACTIVE flags in one pass over audio_flags.

    Active = the amendment row if a flag was edited, else the original, minus
    any DISMISSED row. Matches _active_audio_flag_rows in store/audio_db.py.
    """
    rows = conn.execute(
        """SELECT flag_id, parent_flag_id, s_id, source, status, intent
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
        s = summaries.setdefault(r["s_id"], {"n": 0, "sources": set(), "categories": {}})
        s["n"] += 1
        if r["source"]:
            s["sources"].add(r["source"])
        cat = r["intent"] or "UNKNOWN"
        s["categories"][cat] = s["categories"].get(cat, 0) + 1
    return summaries


def _segment_stats(conn) -> dict:
    """Per-session (segment count, max ts_end) in one pass over audio_segments."""
    stats: dict = {}
    for r in conn.execute(
        "SELECT s_id, COUNT(*) AS n, MAX(ts_end) AS max_ts_end FROM audio_segments GROUP BY s_id"
    ).fetchall():
        stats[r["s_id"]] = (r["n"], r["max_ts_end"])
    return stats


def _format_categories(categories: dict) -> str:
    """Render intent counts as a compact dict string, e.g. {ABUSIVE_LANGUAGE:3,VIOLENCE:1}."""
    if not categories:
        return ""
    return "{" + ",".join(f"{k}:{v}" for k, v in sorted(categories.items())) + "}"


def _duration(session: dict, seg_stats: tuple | None) -> float | None:
    """Real duration if the pipeline stored it, else the last segment end."""
    dur = session.get("duration_seconds")
    if dur:
        return float(dur)
    if seg_stats and seg_stats[1] is not None:
        return float(seg_stats[1])
    return None


def export(out_path: Path) -> None:
    conn = get_audio_connection()

    print("  Loading segment counts...")
    seg_stats = _segment_stats(conn)

    print("  Loading active flags...")
    flag_summary = _active_flag_summaries(conn)

    print("  Exporting sessions...")
    n_sessions = 0
    n_flagged = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()

        for row in conn.execute("SELECT * FROM audio_sessions ORDER BY s_id"):
            s = dict(row)
            n_sessions += 1
            s_id = s["s_id"]
            fs = flag_summary.get(s_id)
            if fs:
                n_flagged += 1

            stats = seg_stats.get(s_id)
            n_segments = stats[0] if stats else 0
            duration_seconds = _duration(s, stats)
            duration_minutes = round(duration_seconds / 60, 3) if duration_seconds else None
            has_video = s.get("has_video")

            writer.writerow({
                "session_id":        s_id,
                "session_type":      "audio",
                "duration_seconds":  duration_seconds,
                "duration_minutes":  duration_minutes,
                "n_segments":        n_segments,
                "lang":              s.get("lang") or "",
                "has_video":         int(bool(has_video)) if has_video is not None else "",
                "review_status":     s.get("review_status") or "",
                "verdict":           s.get("overall_verdict") or "",
                "astrotalk_verdict": s.get("astrotalk_verdict") or "",
                "manual_risk_level": s.get("manual_risk_level") or "",
                "reviewed_by":       s.get("submitted_by") or s.get("reviewer_id") or "",
                "is_flagged":        1 if fs else 0,
                "n_active_flags":    fs["n"] if fs else 0,
                "flag_categories":   _format_categories(fs["categories"]) if fs else "",
                "flag_sources":      ";".join(sorted(fs["sources"])) if fs else "",
            })

    conn.close()
    print(f"  Sessions exported  : {n_sessions}")
    print(f"  Flagged (>=1 flag) : {n_flagged}")
    print(f"  Clean (no flags)   : {n_sessions - n_flagged}")
    print(f"  CSV written        : {out_path}")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Export ALL ingested audio sessions (one row per session) with clean/flagged status"
    )
    p.add_argument("--out", default=None, help="Output CSV path")
    args = p.parse_args()

    if args.out:
        out_path = Path(args.out)
    else:
        stamp = datetime.now().strftime("%Y%m%d")
        out_path = Path(__file__).resolve().parents[1] / "exports" / f"all_ingested_audio_sessions_{stamp}.csv"

    print("=" * 60)
    print("  Export ALL ingested audio sessions (clean vs flagged)")
    print(f"  DB: {AUDIO_DB_PATH}")
    print("=" * 60)
    export(out_path)


if __name__ == "__main__":
    main()
