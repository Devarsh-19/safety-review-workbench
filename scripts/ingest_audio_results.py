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


def optional_seconds(value) -> float | None:
    """Like hms_to_seconds(), but preserves missing/blank values as None."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return hms_to_seconds(value)
    except (TypeError, ValueError):
        return None


def optional_bool(value) -> bool | None:
    """Parse booleans from JSON/native values while preserving missing values."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    return None


def interval_overlap_seconds(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def choose_best_segment(flag: dict, segments: list[dict]) -> dict | None:
    """Resolve a missing segment_id from timestamps first, then nearest speaker segment."""
    if not segments:
        return None

    speaker = flag.get("speaker")
    flag_start = optional_seconds(flag.get("ts_start"))
    flag_end = optional_seconds(flag.get("ts_end"))
    if flag_start is None and flag_end is None:
        flag_start = flag_end = None
    elif flag_start is None:
        flag_start = flag_end
    elif flag_end is None:
        flag_end = flag_start

    def sort_key(seg: dict) -> tuple[float, float, int]:
        seg_start = optional_seconds(seg.get("ts_start")) or 0.0
        seg_end = optional_seconds(seg.get("ts_end")) or seg_start
        overlap = 0.0
        start_distance = 0.0
        if flag_start is not None and flag_end is not None:
            overlap = interval_overlap_seconds(flag_start, flag_end, seg_start, seg_end)
            start_distance = min(abs(seg_start - flag_start), abs(seg_end - flag_start))
        return (-overlap, start_distance, int(seg.get("segment_id") or seg.get("seg_id") or 0))

    def ordered_candidates(pool: list[dict]) -> list[dict]:
        if not pool:
            return []
        if flag_start is not None:
            containing_start = [
                seg for seg in pool
                if (optional_seconds(seg.get("ts_start")) or 0.0) - 0.001
                <= flag_start
                <= (optional_seconds(seg.get("ts_end")) or 0.0) + 0.001
            ]
            if containing_start:
                return sorted(containing_start, key=sort_key)
        if flag_start is not None and flag_end is not None:
            overlapping = [
                seg for seg in pool
                if interval_overlap_seconds(
                    flag_start,
                    flag_end,
                    optional_seconds(seg.get("ts_start")) or 0.0,
                    optional_seconds(seg.get("ts_end")) or 0.0,
                ) > 0
            ]
            if overlapping:
                return sorted(overlapping, key=sort_key)
        return sorted(pool, key=sort_key)

    if speaker:
        same_speaker = [seg for seg in segments if seg.get("speaker") == speaker]
        speaker_matches = ordered_candidates(same_speaker)
        if speaker_matches:
            return speaker_matches[0]

    matches = ordered_candidates(segments)
    return matches[0] if matches else None


def localize_flag_span_to_segment(
    flag_ts_start: float | None,
    flag_ts_end: float | None,
    segment: dict | None,
) -> tuple[float | None, float | None]:
    """Clamp a flag span to the matched segment so only that segment stays flagged."""
    if segment is None:
        return flag_ts_start, flag_ts_end

    seg_start = optional_seconds(segment.get("ts_start"))
    seg_end = optional_seconds(segment.get("ts_end"))
    if seg_start is None and seg_end is None:
        return flag_ts_start, flag_ts_end
    if seg_start is None:
        seg_start = seg_end
    if seg_end is None:
        seg_end = seg_start

    if flag_ts_start is None and flag_ts_end is None:
        return seg_start, seg_end

    localized_start = seg_start if flag_ts_start is None else max(flag_ts_start, seg_start)
    localized_end = seg_end if flag_ts_end is None else min(flag_ts_end, seg_end)
    if localized_end < localized_start:
        return seg_start, seg_end
    return localized_start, localized_end


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
    astrotalk_val = obj.get("at_flag")
    astrotalk_verdict = None
    if astrotalk_val == 0:
        astrotalk_verdict = 'CLEAN'
    elif astrotalk_val == 1:
        astrotalk_verdict = 'FLAGGED'
    has_video = optional_bool(obj.get("has_video"))
    has_video_value = None if has_video is None else int(has_video)

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
                   audio_url = COALESCE(?, audio_url),
                   has_video = COALESCE(?, has_video)
               WHERE s_id = ?""",
            (obj.get("lang"), json.dumps(obj.get("long_pauses") or obj.get("pauses") or []),
             1 if obj.get("review") else 0, verdict, confidence, astrotalk_verdict,
             obj.get("audio_url"), has_video_value, s_id),
        )
        conn.execute("DELETE FROM audio_flags WHERE s_id = ?", (s_id,))
        conn.execute("DELETE FROM audio_segments WHERE s_id = ?", (s_id,))
    else:
        conn.execute(
            """INSERT INTO audio_sessions
                   (s_id, lang, pauses, needs_review, overall_verdict, confidence_score, astrotalk_verdict, audio_url, has_video)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (s_id, obj.get("lang"), json.dumps(obj.get("long_pauses") or obj.get("pauses") or []),
             1 if obj.get("review") else 0, verdict, confidence, astrotalk_verdict, obj.get("audio_url"), has_video_value),
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
        matched_segment = None

        if seg_id is not None and segments:
            matched_segment = next(
                (
                    seg for seg in segments
                    if int(seg.get("segment_id") or seg.get("seg_id") or 0) == int(seg_id)
                ),
                None,
            )
        if matched_segment is None and segments:
            matched_segment = choose_best_segment(flag, segments)
            if matched_segment is not None:
                seg_id = matched_segment.get("segment_id") or matched_segment.get("seg_id")

        if seg_id is not None:
            seg_id = int(seg_id)
        flag_ts_start = optional_seconds(flag.get("ts_start"))
        flag_ts_end = optional_seconds(flag.get("ts_end"))
        flag_ts_start, flag_ts_end = localize_flag_span_to_segment(
            flag_ts_start,
            flag_ts_end,
            matched_segment,
        )

        conn.execute(
            """INSERT INTO audio_flags (s_id, seg_id, ts_start, ts_end, intent, severity, conf, transcript, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'LLM')""",
            (s_id, seg_id, flag_ts_start, flag_ts_end, flag.get("intent"), flag.get("s"),
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
