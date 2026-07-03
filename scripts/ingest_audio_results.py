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
      {"ts_start": "00:01:05", "ts_end": "00:01:12", "seg_id": 1,
       "speaker": "SPEAKER_1", "tone": "aggressive",
       "flags": [{"intent": "...", "s": "HIGH", "conf": 0.91, "transcript": "..."}]}
    ]
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
    intent_codes = [
        f.get("intent")
        for seg in segments
        for f in (seg.get("flags") or [])
        if f.get("intent")
    ]
    verdict    = get_db_verdict_for_flags(intent_codes)
    confidence = get_db_confidence_for_verdict(verdict)

    existing = conn.execute(
        "SELECT s_id FROM audio_sessions WHERE s_id = ?", (s_id,)
    ).fetchone()
    if existing:
        # Refresh the LLM-derived columns, keep review workflow state.
        # audio_url only overwrites when the new JSON provides one.
        conn.execute(
            """UPDATE audio_sessions
               SET lang = ?, pauses = ?, needs_review = ?, overall_verdict = ?,
                   confidence_score = ?, audio_url = COALESCE(?, audio_url)
               WHERE s_id = ?""",
            (obj.get("lang"), json.dumps(obj.get("pauses") or []),
             1 if obj.get("review") else 0, verdict, confidence,
             obj.get("audio_url"), s_id),
        )
        conn.execute("DELETE FROM audio_flags WHERE s_id = ?", (s_id,))
        conn.execute("DELETE FROM audio_segments WHERE s_id = ?", (s_id,))
    else:
        conn.execute(
            """INSERT INTO audio_sessions
                   (s_id, lang, pauses, needs_review, overall_verdict, confidence_score, audio_url)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (s_id, obj.get("lang"), json.dumps(obj.get("pauses") or []),
             1 if obj.get("review") else 0, verdict, confidence, obj.get("audio_url")),
        )

    n_flags = 0
    for seg in segments:
        seg_id = int(seg["seg_id"])
        conn.execute(
            """INSERT INTO audio_segments (s_id, seg_id, ts_start, ts_end, speaker, tone)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (s_id, seg_id, hms_to_seconds(seg.get("ts_start")),
             hms_to_seconds(seg.get("ts_end")), seg.get("speaker"), seg.get("tone")),
        )
        for flag in seg.get("flags") or []:
            conn.execute(
                """INSERT INTO audio_flags (s_id, seg_id, intent, severity, conf, transcript, source)
                   VALUES (?, ?, ?, ?, ?, ?, 'LLM')""",
                (s_id, seg_id, flag.get("intent"), flag.get("s"),
                 flag.get("conf"), flag.get("transcript")),
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
