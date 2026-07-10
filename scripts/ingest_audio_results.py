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

Timestamps are converted from HH:MM:SS to seconds.

Duplicate protection (mirrors the chat ingester + pipeline/checkpoint.py):
- A checkpoint file (logs/audio_ingest_checkpoint.json) records every s_id
  already ingested; those sessions are skipped on later runs. Use --force to
  re-ingest checkpointed sessions, --reset-checkpoint to start fresh.
- Sessions whose review_status is no longer PENDING are never touched.
- Re-ingesting a PENDING session refreshes segments and untouched LLM flags
  only: MANUAL flags, amendments, confirmed and dismissed flags are preserved,
  and incoming LLM flags that duplicate a kept (seg_id, intent) are skipped.

Each session is written in its own transaction, so one bad session is
reported and skipped without losing the rest of the run.

CLEAN sessions with no flags are auto-submitted for L2 review as reviewer
'LLM' (chat parity). Disable with --no-auto-submit.

Usage:
  python scripts/ingest_audio_results.py path/to/file.json
  python scripts/ingest_audio_results.py path/to/dir/ [--force] [--no-auto-submit]
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import (  # noqa: E402
    get_audio_connection,
    initialise_audio_db,
    recompute_audio_session_verdict,
    AUDIO_DB_PATH,
)

_CHECKPOINT_FILE = Path(__file__).resolve().parents[1] / "logs" / "audio_ingest_checkpoint.json"


def load_audio_checkpoint() -> set[int]:
    if not _CHECKPOINT_FILE.exists():
        return set()
    try:
        data = json.loads(_CHECKPOINT_FILE.read_text(encoding="utf-8"))
        return {int(s) for s in data.get("ingested_s_ids", [])}
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return set()


def save_audio_checkpoint(ingested_s_ids: set[int]) -> None:
    _CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CHECKPOINT_FILE.write_text(
        json.dumps({"ingested_s_ids": sorted(ingested_s_ids)}, indent=2),
        encoding="utf-8",
    )


def clear_audio_checkpoint() -> None:
    if _CHECKPOINT_FILE.exists():
        _CHECKPOINT_FILE.unlink()


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


def resolve_segment_ids(segments: list[dict]) -> None:
    """Assign a collision-free seg_id to every segment (stored in '_seg_id').

    audio_segments has PRIMARY KEY (s_id, seg_id); LLM output with missing or
    repeated segment_ids must not abort the insert with an IntegrityError.
    """
    used: set[int] = set()
    for seg in segments:
        raw = seg.get("segment_id") or seg.get("seg_id")
        try:
            seg_id = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            seg_id = 0
        if seg_id <= 0 or seg_id in used:
            seg_id = max(used, default=0) + 1
        used.add(seg_id)
        seg["_seg_id"] = seg_id


def parse_astrotalk_verdict(value) -> str | None:
    """'CLEAN'/'FLAGGED' from the at_flag field; accepts 0/1, "0"/"1", bools."""
    flagged = optional_bool(value)
    if flagged is None:
        return None
    return "FLAGGED" if flagged else "CLEAN"


def ingest_session(conn, obj: dict, auto_submit: bool = True) -> tuple[str, int, int]:
    """Insert or refresh one session object. Returns (outcome, n_segments, n_flags).

    outcome: 'inserted' | 'refreshed' | 'skipped' (session exists and is no
    longer PENDING — reviewer work is never touched).
    """
    s_id = int(obj["s_id"])
    segments = [dict(seg) for seg in (obj.get("segments") or [])]
    resolve_segment_ids(segments)

    # Flags may be top-level (flattened batch output) or nested inside
    # segments (raw Gemini JSON).  Support both shapes so either source
    # can be ingested without silent data loss.
    flags = list(obj.get("flags") or [])
    if not flags and segments:
        for seg in segments:
            for sf in seg.get("flags") or []:
                sf = dict(sf)
                sf.setdefault("segment_id", seg["_seg_id"])
                sf.setdefault("speaker", seg.get("speaker"))
                sf.setdefault("ts_start", seg.get("ts_start"))
                sf.setdefault("ts_end", seg.get("ts_end"))
                flags.append(sf)

    astrotalk_verdict = parse_astrotalk_verdict(obj.get("at_flag"))
    has_video = optional_bool(obj.get("has_video"))
    has_video_value = None if has_video is None else int(has_video)
    duration = optional_seconds(
        obj.get("audio_duration_seconds") or obj.get("duration_seconds")
    )

    existing = conn.execute(
        "SELECT review_status FROM audio_sessions WHERE s_id = ?", (s_id,)
    ).fetchone()
    if existing and existing["review_status"] != "PENDING":
        # Chat parity: re-ingesting never clobbers submitted/reviewed/locked
        # sessions or their flags.
        return "skipped", 0, 0

    kept_flag_keys: set[tuple[int | None, str | None]] = set()
    if existing:
        # Refresh the LLM-derived columns; keep review workflow state.
        # audio_url/has_video/duration only overwrite when the new JSON
        # provides a value.
        conn.execute(
            """UPDATE audio_sessions
               SET lang = ?, pauses = ?, needs_review = ?,
                   astrotalk_verdict = COALESCE(?, astrotalk_verdict),
                   audio_url = COALESCE(?, audio_url),
                   has_video = COALESCE(?, has_video),
                   duration_seconds = COALESCE(?, duration_seconds)
               WHERE s_id = ?""",
            (obj.get("lang"), json.dumps(obj.get("long_pauses") or obj.get("pauses") or []),
             1 if obj.get("review") else 0, astrotalk_verdict,
             obj.get("audio_url"), has_video_value, duration, s_id),
        )
        # Replace only untouched LLM flags. MANUAL flags, amendments,
        # confirmed and dismissed flags are reviewer work — keep them
        # (chat parity: the chat ingester only de-dupe-inserts, never deletes).
        conn.execute(
            """DELETE FROM audio_flags
               WHERE s_id = ?
                 AND source = 'LLM'
                 AND status = 'ACTIVE'
                 AND parent_flag_id IS NULL
                 AND flag_id NOT IN (
                     SELECT parent_flag_id FROM audio_flags
                     WHERE s_id = ? AND parent_flag_id IS NOT NULL
                 )""",
            (s_id, s_id),
        )
        kept_flag_keys = {
            (r["seg_id"], r["intent"])
            for r in conn.execute(
                "SELECT seg_id, intent FROM audio_flags WHERE s_id = ?", (s_id,)
            ).fetchall()
        }
        # Segments carry no reviewer state — safe to replace wholesale.
        conn.execute("DELETE FROM audio_segments WHERE s_id = ?", (s_id,))
    else:
        conn.execute(
            """INSERT INTO audio_sessions
                   (s_id, lang, pauses, needs_review, astrotalk_verdict, audio_url, has_video, duration_seconds)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (s_id, obj.get("lang"), json.dumps(obj.get("long_pauses") or obj.get("pauses") or []),
             1 if obj.get("review") else 0, astrotalk_verdict, obj.get("audio_url"),
             has_video_value, duration),
        )

    for seg in segments:
        conn.execute(
            """INSERT INTO audio_segments (s_id, seg_id, ts_start, ts_end, speaker, tone)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (s_id, seg["_seg_id"], hms_to_seconds(seg.get("ts_start")),
             hms_to_seconds(seg.get("ts_end")), seg.get("speaker"), seg.get("tone")),
        )

    n_flags = 0
    for flag in flags:
        raw_seg_id = flag.get("segment_id") or flag.get("seg_id")
        matched_segment = None

        if raw_seg_id is not None and segments:
            matched_segment = next(
                (
                    seg for seg in segments
                    if int(seg.get("segment_id") or seg.get("seg_id") or 0) == int(raw_seg_id)
                    or seg["_seg_id"] == int(raw_seg_id)
                ),
                None,
            )
        if matched_segment is None and segments:
            matched_segment = choose_best_segment(flag, segments)

        seg_id = matched_segment["_seg_id"] if matched_segment is not None else (
            int(raw_seg_id) if raw_seg_id is not None else None
        )
        if (seg_id, flag.get("intent")) in kept_flag_keys:
            continue  # same LLM flag already exists (confirmed/amended) — don't duplicate
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

    # Verdict from the flags now in the DB (respects amendments/dismissals) —
    # same rules as the chat DB, including flagged-combination escalations.
    verdict = recompute_audio_session_verdict(s_id, conn)

    # Chat parity: a session with no flags at all is CLEAN and goes straight
    # to the L2 queue as reviewer 'LLM' instead of clogging the L1 queue.
    if auto_submit and verdict == "CLEAN":
        remaining = conn.execute(
            "SELECT COUNT(*) FROM audio_flags WHERE s_id = ?", (s_id,)
        ).fetchone()[0]
        if remaining == 0:
            conn.execute(
                """UPDATE audio_sessions
                   SET review_status = 'SUBMITTED_FOR_REVIEW',
                       submitted_by  = 'LLM',
                       submitted_at  = datetime('now'),
                       reviewer_id   = 'LLM',
                       reviewer_note = 'Auto-submitted by LLM ingest: no flags',
                       reviewed_at   = datetime('now')
                   WHERE s_id = ? AND review_status = 'PENDING'""",
                (s_id,),
            )

    return ("refreshed" if existing else "inserted"), len(segments), n_flags


def main():
    parser = argparse.ArgumentParser(description="Ingest audio LLM results into the audio review DB.")
    parser.add_argument("path", help="JSON file or directory of .json files.")
    parser.add_argument("--force", action="store_true",
                        help="Re-ingest sessions already recorded in the checkpoint.")
    parser.add_argument("--reset-checkpoint", action="store_true",
                        help="Clear the checkpoint before ingesting.")
    parser.add_argument("--no-auto-submit", action="store_true",
                        help="Do not auto-submit CLEAN zero-flag sessions for L2 review.")
    args = parser.parse_args()

    src = Path(args.path)
    files = sorted(src.glob("*.json")) if src.is_dir() else [src]
    if not files:
        print(f"No .json files found at {src}")
        sys.exit(1)

    if args.reset_checkpoint:
        clear_audio_checkpoint()
        print("Checkpoint cleared.")
    ingested_ids = load_audio_checkpoint()

    initialise_audio_db()
    conn = get_audio_connection()
    counts = {"inserted": 0, "refreshed": 0, "skipped": 0}
    n_checkpoint_skips = n_errors = 0
    try:
        for fp in files:
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                print(f"[ERROR] {fp.name}: unreadable JSON — {exc}")
                n_errors += 1
                continue
            objects = data if isinstance(data, list) else [data]
            for obj in objects:
                try:
                    s_id = int(obj["s_id"])
                except (KeyError, TypeError, ValueError):
                    print(f"[ERROR] {fp.name}: object without a valid s_id — skipped")
                    n_errors += 1
                    continue

                if s_id in ingested_ids and not args.force:
                    n_checkpoint_skips += 1
                    continue

                try:
                    # One transaction per session: a bad session rolls back
                    # alone and the rest of the run is preserved.
                    with conn:
                        outcome, n_seg, n_flag = ingest_session(
                            conn, obj, auto_submit=not args.no_auto_submit
                        )
                except Exception as exc:
                    print(f"[ERROR] s_id {s_id}: {type(exc).__name__}: {exc}  ({fp.name})")
                    n_errors += 1
                    continue

                counts[outcome] += 1
                ingested_ids.add(s_id)
                if outcome == "skipped":
                    print(f"  s_id {s_id}: already reviewed — untouched  ({fp.name})")
                else:
                    print(f"  s_id {s_id}: {outcome}, {n_seg} segments, {n_flag} flags  ({fp.name})")
            save_audio_checkpoint(ingested_ids)
    finally:
        conn.close()
        save_audio_checkpoint(ingested_ids)

    print(
        f"\nDone: {counts['inserted']} inserted, {counts['refreshed']} refreshed, "
        f"{counts['skipped']} already-reviewed, {n_checkpoint_skips} checkpoint skips, "
        f"{n_errors} errors -> {AUDIO_DB_PATH}"
    )
    print(f"Checkpoint: {_CHECKPOINT_FILE}")


if __name__ == "__main__":
    main()
