"""
generate_audio_detection_logs.py

Generates detection logs for audio sessions directly from the audio review database.
Outputs both a session-level summary and a detailed segment-level breakdown of flags.

Modes:
  --mode session   : Session-level summary (1 row per session)
  --mode detailed  : Segment-level detail (1 row per flagged segment)
  --mode both      : Generate both CSVs in one run (default)

Usage:
  python scripts/generate_audio_detection_logs.py
  python scripts/generate_audio_detection_logs.py --out-dir exports/
"""

import argparse
import csv
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
import sqlite3

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import AUDIO_DB_PATH

VALID_MODES = ("session", "detailed", "both")

SESSION_COLUMNS = [
    "session_id",
    "language",
    "astrotalk_verdict",
    "overall_verdict",
    "review_status",
    "match_status",
    "n_segments",
    "n_flags",
    "n_confirmed_flags",
    "n_dismissed_flags",
    "n_amended_flags",
    "flag_categories",
    "max_confidence",
]

DETAILED_COLUMNS = [
    "session_id",
    "segment_id",
    "ts_start",
    "ts_end",
    "speaker",
    "tone",
    "transcript",
    "intent",
    "severity",
    "confidence_score",
    "source",
    "status",
    "resolution",
    "astrotalk_verdict",
    "session_overall_verdict",
]


def _format_flag_categories(counter: Counter) -> str:
    if not counter:
        return ""
    parts = sorted(f"{cat}:{cnt}" for cat, cnt in counter.most_common())
    return "{" + ", ".join(parts) + "}"


def _determine_match_status(astrotalk_verdict: str, overall_verdict: str) -> str:
    orig = (astrotalk_verdict or "").strip().upper() == "FLAGGED"
    llm = (overall_verdict or "").strip().upper() in ("FLAGGED", "SEVERE")
    
    if orig and llm:
        return "BOTH_FLAGGED"
    elif orig and not llm:
        return "ASTROTALK_ONLY"
    elif not orig and llm:
        return "LLM_ONLY"
    else:
        return "BOTH_CLEAN"


def generate_session_log(conn: sqlite3.Connection, out_path: Path, flagged_only: bool = False) -> int:
    where_clause = ""
    if flagged_only:
        where_clause = "WHERE s.astrotalk_verdict = 'FLAGGED' OR s.overall_verdict IN ('FLAGGED', 'SEVERE')"

    query = f"""
    SELECT 
        s.s_id, s.lang, s.astrotalk_verdict, s.overall_verdict, s.review_status,
        (SELECT COUNT(*) FROM audio_segments WHERE s_id = s.s_id) as n_segments
    FROM audio_sessions s
    {where_clause}
    ORDER BY s.s_id
    """
    
    rows = conn.execute(query).fetchall()
    
    # Pre-fetch all flags to compute counts and categories in-memory
    flags_query = "SELECT s_id, intent, status, flag_id, parent_flag_id, conf FROM audio_flags"
    flags_data = conn.execute(flags_query).fetchall()
    
    amended_parents = {r[4] for r in flags_data if r[4] is not None}
    
    session_flags = {}
    for r in flags_data:
        s_id = r[0]
        intent = r[1]
        status = (r[2] or "").upper()
        flag_id = r[3]
        parent_flag_id = r[4]
        conf = r[5]
        
        is_amended = flag_id in amended_parents
        is_active = (status != "DISMISSED") and (parent_flag_id is not None or not is_amended)
        
        stats = session_flags.setdefault(s_id, {
            "n_flags": 0,
            "n_confirmed_flags": 0,
            "n_dismissed_flags": 0,
            "n_amended_flags": 0,
            "max_conf": -1.0,
            "categories": Counter()
        })
        
        if is_amended:
            stats["n_amended_flags"] += 1
        elif status == "DISMISSED":
            stats["n_dismissed_flags"] += 1
        elif status == "CONFIRMED":
            stats["n_confirmed_flags"] += 1
            
        if is_active:
            stats["n_flags"] += 1
            if intent:
                stats["categories"][intent] += 1
            if conf is not None and conf > stats["max_conf"]:
                stats["max_conf"] = conf

    out_rows = []
    for r in rows:
        sid = r[0]
        astro_verdict = r[2] or "CLEAN"
        llm_verdict = r[3] or "CLEAN"
        
        stats = session_flags.get(sid, {
            "n_flags": 0,
            "n_confirmed_flags": 0,
            "n_dismissed_flags": 0,
            "n_amended_flags": 0,
            "max_conf": None,
            "categories": Counter()
        })
        
        out_rows.append({
            "session_id": sid,
            "language": r[1] or "",
            "astrotalk_verdict": astro_verdict,
            "overall_verdict": llm_verdict,
            "review_status": r[4] or "",
            "match_status": _determine_match_status(astro_verdict, llm_verdict),
            "n_segments": r[5],
            "n_flags": stats["n_flags"],
            "n_confirmed_flags": stats["n_confirmed_flags"],
            "n_dismissed_flags": stats["n_dismissed_flags"],
            "n_amended_flags": stats["n_amended_flags"],
            "flag_categories": _format_flag_categories(stats["categories"]),
            "max_confidence": f"{stats['max_conf']:.2f}" if stats['max_conf'] is not None and stats['max_conf'] >= 0 else "",
        })
        
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SESSION_COLUMNS)
        writer.writeheader()
        writer.writerows(out_rows)
        
    return len(out_rows)


def generate_detailed_log(conn: sqlite3.Connection, out_path: Path, flagged_only: bool = False) -> int:
    where_clause = ""
    if flagged_only:
        where_clause = "WHERE s.astrotalk_verdict = 'FLAGGED' OR s.overall_verdict IN ('FLAGGED', 'SEVERE')"

    query = f"""
    SELECT 
        f.s_id, f.seg_id, f.ts_start, f.ts_end, f.intent, f.severity, f.conf, 
        f.transcript, f.source, f.status,
        seg.speaker, seg.tone,
        s.astrotalk_verdict, s.overall_verdict,
        f.flag_id
    FROM audio_flags f
    LEFT JOIN audio_segments seg ON f.s_id = seg.s_id AND f.seg_id = seg.seg_id
    LEFT JOIN audio_sessions s ON f.s_id = s.s_id
    {where_clause}
    ORDER BY f.s_id, f.ts_start, f.seg_id
    """
    
    rows = conn.execute(query).fetchall()
    
    flags_query = "SELECT flag_id, parent_flag_id FROM audio_flags"
    flags_data = conn.execute(flags_query).fetchall()
    amended_parents = {r[1] for r in flags_data if r[1] is not None}
    
    out_rows = []
    for r in rows:
        db_status = (r[9] or "").upper()
        flag_id = r[14]
        is_amended = flag_id in amended_parents
        
        if is_amended:
            resolution = "AMENDED"
        elif db_status == "DISMISSED":
            resolution = "DISMISSED"
        elif db_status == "CONFIRMED":
            resolution = "CONFIRMED"
        else:
            resolution = "ACTIVE"

        out_rows.append({
            "session_id": r[0],
            "segment_id": r[1] if r[1] is not None else "",
            "ts_start": f"{r[2]:.2f}" if r[2] is not None else "",
            "ts_end": f"{r[3]:.2f}" if r[3] is not None else "",
            "speaker": r[10] or "",
            "tone": r[11] or "",
            "transcript": r[7] or "",
            "intent": r[4] or "",
            "severity": r[5] or "",
            "confidence_score": f"{r[6]:.2f}" if r[6] is not None else "",
            "source": r[8] or "",
            "status": r[9] or "",
            "resolution": resolution,
            "astrotalk_verdict": r[12] or "CLEAN",
            "session_overall_verdict": r[13] or "CLEAN",
        })
        
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=DETAILED_COLUMNS)
        writer.writeheader()
        writer.writerows(out_rows)
        
    return len(out_rows)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Generate audio detection logs directly from the audio review DB."
    )
    p.add_argument(
        "--mode",
        default="both",
        choices=VALID_MODES,
        help="Which log(s) to generate: session, detailed, or both (default: both)",
    )
    p.add_argument(
        "--out-dir",
        default=None,
        help="Output directory (default: exports/)",
    )
    p.add_argument(
        "--flagged-only",
        action="store_true",
        help="Only output logs for sessions that were flagged (astrotalk_verdict or overall_verdict is flagged)",
    )
    args = p.parse_args()

    out_dir = (
        Path(args.out_dir) if args.out_dir
        else Path(__file__).resolve().parents[1] / "exports"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  Audio Detection Log Generator")
    print("=" * 60)
    
    db_path = Path(__file__).resolve().parents[1] / AUDIO_DB_PATH
    if not db_path.exists():
        print(f"ERROR: Audio DB not found at {db_path}")
        sys.exit(1)
        
    conn = sqlite3.connect(db_path)
    stamp = datetime.now().strftime("%Y%m%d")
    
    if args.mode in ("session", "both"):
        suffix = "_flagged" if args.flagged_only else ""
        out_path = out_dir / f"audio_detection_logs{suffix}_{stamp}.csv"
        print(f"Generating session-level log -> {out_path.name}...")
        n = generate_session_log(conn, out_path, args.flagged_only)
        print(f"  Written {n} rows")
        
    if args.mode in ("detailed", "both"):
        suffix = "_flagged" if args.flagged_only else ""
        out_path = out_dir / f"audio_detection_logs_detailed{suffix}_{stamp}.csv"
        print(f"Generating detailed log -> {out_path.name}...")
        n = generate_detailed_log(conn, out_path, args.flagged_only)
        print(f"  Written {n} flag rows")
        
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
