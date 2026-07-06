"""
ingest_audio_results.py

Ingests audio-processing LLM output (response_json_schema shape) into the audio
review database (store/audio_review.db). Accepts a single JSON file or a
directory of .json files; each file holds one session object or a list of them.

Expected object shape:
  {
    "s_id": 123, "lang": "hindi", "review": true, "pauses": [...],
    "audio_url": "https://cdn.example.com/recordings/123.m3u8",   # optional — HLS stream for the in-browser player
    "segments": [
      {"ts_start": 65.0, "ts_end": 72.0, "segment_id": 1,
       "speaker": "SPEAKER_1", "tone": "AGGRESSIVE", "intents": []}
    ],
    "flags": [{"intent": "...", "s": "RED", "conf": 0.91, "transcript_excerpt": "...", "segment_id": 1}]
  }

Timestamps are converted from HH:MM:SS to seconds. Re-ingesting an existing
s_id replaces its segments/flags but preserves review workflow state
(review_status, speaker roles, reviewer columns).

Usage:
  python scripts/ingest_audio_results.py path/to/file.json
  python scripts/ingest_audio_results.py path/to/dir/
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import get_audio_connection, initialise_audio_db, AUDIO_DB_PATH  # noqa: E402


def hms_to_seconds(value) -> float:
    """'HH:MM:SS' (or 'MM:SS', or numeric) -> seconds."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    parts = [float(p) for p in str(value).strip().split(":")]
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def ingest_session(conn, obj: dict) -> tuple[int, int, int]:
    """Insert/replace one session object. Returns (s_id, n_segments, n_flags)."""
    from engine.verdict_rules import get_db_verdict_for_flags, get_db_confidence_for_verdict

    s_id = int(obj["s_id"])
    segments = obj.get("segments") or []
    # Same verdict rules as the chat DB: SEVERE / FLAGGED / CLEAN from the
    # intent codes, including flagged-combination escalations.
    flags = obj.get("flags") or []
    intent_codes = [f.get("intent") for f in flags if f.get("intent")]
    verdict    = get_db_verdict_for_flags(intent_codes)
    confidence = get_db_confidence_for_verdict(verdict)

    # Parse astrotalk verdict
    astrotalk_val = obj.get("astrotalk")
    astrotalk_verdict = None
    if astrotalk_val == 0:
        astrotalk_verdict = 'CLEAN'
    elif astrotalk_val == 1:
        astrotalk_verdict = 'FLAGGED'

    existing = conn.execute(
        "SELECT s_id FROM audio_sessions WHERE s_id = ?", (s_id,)
    ).fetchone()
    if existing:
        # Refresh the LLM-derived columns, keep review workflow state.
        # audio_url only overwrites when the new JSON provides one.
        conn.execute(
            """UPDATE audio_sessions
               SET lang = ?, pauses = ?, needs_review = ?, overall_verdict = ?,
                   confidence_score = ?, astrotalk_verdict = COALESCE(?, astrotalk_verdict),
                   audio_url = COALESCE(?, audio_url)
               WHERE s_id = ?""",
            (obj.get("lang"), json.dumps(obj.get("long_pauses") or obj.get("pauses") or []),
             1 if obj.get("review") else 0, verdict, confidence, astrotalk_verdict,
             obj.get("audio_url"), s_id),
        )
        conn.execute("DELETE FROM audio_flags WHERE s_id = ?", (s_id,))
        conn.execute("DELETE FROM audio_segments WHERE s_id = ?", (s_id,))
    else:
        conn.execute(
            """INSERT INTO audio_sessions
                   (s_id, lang, pauses, needs_review, overall_verdict, confidence_score, astrotalk_verdict, audio_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (s_id, obj.get("lang"), json.dumps(obj.get("long_pauses") or obj.get("pauses") or []),
             1 if obj.get("review") else 0, verdict, confidence, astrotalk_verdict, obj.get("audio_url")),
        )

    for seg in segments:
        seg_id = int(seg.get("segment_id") or seg.get("seg_id") or 0)
        conn.execute(
            """INSERT INTO audio_segments (s_id, seg_id, ts_start, ts_end, speaker, tone)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (s_id, seg_id, hms_to_seconds(seg.get("ts_start")),
             hms_to_seconds(seg.get("ts_end")), seg.get("speaker"), seg.get("tone")),
        )

    n_flags = 0
    for flag in flags:
        seg_id = flag.get("segment_id") or flag.get("seg_id")
        
        # If seg_id is missing, try to link it using speaker or timestamp
        if not seg_id and segments:
            # Match by speaker first
            speaker = flag.get("speaker")
            if speaker:
                for seg in segments:
                    if seg.get("speaker") == speaker:
                        seg_id = seg.get("segment_id") or seg.get("seg_id")
                        break
            
            # If still no seg_id, match by timestamp overlap
            if not seg_id and flag.get("ts_start") is not None:
                f_ts = hms_to_seconds(flag.get("ts_start"))
                for seg in segments:
                    s_start = hms_to_seconds(seg.get("ts_start"))
                    s_end = hms_to_seconds(seg.get("ts_end"))
                    if s_start <= f_ts <= s_end:
                        seg_id = seg.get("segment_id") or seg.get("seg_id")
                        break
            
            # If still nothing, just attach to the first segment so it doesn't vanish
            if not seg_id:
                seg_id = segments[0].get("segment_id") or segments[0].get("seg_id")

        if seg_id is not None:
            seg_id = int(seg_id)
        
        conn.execute(
            """INSERT INTO audio_flags (s_id, seg_id, intent, severity, conf, transcript, source)
               VALUES (?, ?, ?, ?, ?, ?, 'LLM')""",
            (s_id, seg_id, flag.get("intent"), flag.get("s"),
             flag.get("conf"), flag.get("transcript_excerpt") or flag.get("transcript")),
        )
        n_flags += 1
    return s_id, len(segments), n_flags


def main():
    parser = argparse.ArgumentParser(description="Ingest audio LLM results into the audio review DB.")
    parser.add_argument("path", help="JSON file or directory of .json files.")
    args = parser.parse_args()

    src = Path(args.path)
    files = sorted(src.glob("*.json")) if src.is_dir() else [src]
    if not files:
        print(f"No .json files found at {src}")
        sys.exit(1)

    initialise_audio_db()
    conn = get_audio_connection()
    n_sessions = 0
    try:
        for fp in files:
            data = json.loads(fp.read_text(encoding="utf-8"))
            objects = data if isinstance(data, list) else [data]
            for obj in objects:
                s_id, n_seg, n_flag = ingest_session(conn, obj)
                n_sessions += 1
                print(f"  s_id {s_id}: {n_seg} segments, {n_flag} flags  ({fp.name})")
        conn.commit()
    finally:
        conn.close()

    print(f"\nIngested {n_sessions} session(s) into {AUDIO_DB_PATH}")


if __name__ == "__main__":
    main()
