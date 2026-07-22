"""
audio_json_to_csv.py

Convert audio-ingest LLM JSON (the response_json_schema shape consumed by
scripts/ingest_audio_results.py) into a flat CSV — WITHOUT touching any database.

It replicates exactly how ingest_audio_results.ingest_session bifurcates a
session object, so the CSV shows what would actually land in the DB:

  - segments[]  -> one CSV ROW per segment  (s_id, seg_id, ts_start, ts_end,
                   speaker, tone) with segment_id de-duplicated the same way
                   (resolve_segment_ids) and timestamps in seconds (hms_to_seconds).
  - flags[]     -> matched back to their segment (by segment_id, else by
                   timestamp/speaker via choose_best_segment, exactly like the
                   ingester) and AGGREGATED onto that segment's row:
                   flag_count / flag_intents / flag_severities / flag_confs /
                   flag_transcripts. Flags supplied nested inside segments
                   (segments[].flags[]) are picked up too.

Session-level fields (s_id, lang, astrotalk_verdict, has_video,
duration_seconds, needs_review, audio_url) are repeated on every segment row so
the CSV is self-contained. A flag that matches no segment is emitted as its own
trailing row with blank segment columns (seg_id from the flag if it had one).

Because it reuses the ingester's own helpers, the mapping stays in lockstep with
ingestion — no separate copy of the matching logic to drift.

Read-only: reads JSON, writes CSV. Never opens the database.

Usage:
  python scripts/audio_json_to_csv.py path/to/file.json
  python scripts/audio_json_to_csv.py path/to/dir/            # all *.json in dir
  python scripts/audio_json_to_csv.py path/to/file.json --out C:/path/out.csv
"""

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Reuse the ingester's own parsing/matching so the CSV matches the DB exactly.
from scripts.ingest_audio_results import (  # noqa: E402
    hms_to_seconds,
    optional_seconds,
    optional_bool,
    resolve_segment_ids,
    choose_best_segment,
    parse_astrotalk_verdict,
)

CSV_COLUMNS = [
    # session-level (repeated on every segment row)
    "s_id", "lang", "astrotalk_verdict", "has_video", "duration_seconds",
    "needs_review", "audio_url",
    # segment-level
    "seg_id", "ts_start", "ts_end", "speaker", "tone",
    # flags aggregated onto this segment (active LLM flags, as ingest would insert)
    "flag_count", "flag_intents", "flag_severities", "flag_confs", "flag_transcripts",
]


def _collect_flags(obj: dict, segments: list[dict]) -> list[dict]:
    """Flags may be top-level (flattened batch output) or nested inside segments
    (raw Gemini JSON). Mirror ingest_session: prefer top-level; if absent, lift
    each segment's nested flags and inherit the segment's id/speaker/timestamps."""
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
    return flags


def _resolve_flag_seg_id(flag: dict, segments: list[dict]):
    """Which seg_id this flag attaches to — identical rule to ingest_session:
    exact segment_id match first, else choose_best_segment, else the flag's own
    raw id (or None)."""
    raw_seg_id = flag.get("segment_id") or flag.get("seg_id")
    matched = None
    if raw_seg_id is not None and segments:
        matched = next(
            (
                seg for seg in segments
                if int(seg.get("segment_id") or seg.get("seg_id") or 0) == int(raw_seg_id)
                or seg["_seg_id"] == int(raw_seg_id)
            ),
            None,
        )
    if matched is None and segments:
        matched = choose_best_segment(flag, segments)
    if matched is not None:
        return matched["_seg_id"]
    return int(raw_seg_id) if raw_seg_id is not None else None


def _fmt(value) -> str:
    return "" if value is None else str(value)


def session_rows(obj: dict) -> list[dict]:
    """Flatten one session object into segment rows (+ trailing unmatched-flag rows)."""
    s_id = obj.get("s_id")
    segments = [dict(seg) for seg in (obj.get("segments") or [])]
    resolve_segment_ids(segments)  # assigns collision-free seg["_seg_id"]

    flags = _collect_flags(obj, segments)

    # Bucket flags by the seg_id they resolve to (same matching as ingest).
    flags_by_seg: dict = {}
    for flag in flags:
        seg_id = _resolve_flag_seg_id(flag, segments)
        flags_by_seg.setdefault(seg_id, []).append(flag)

    session_ctx = {
        "s_id": _fmt(s_id),
        "lang": _fmt(obj.get("lang")),
        "astrotalk_verdict": _fmt(parse_astrotalk_verdict(obj.get("at_flag"))),
        "has_video": _fmt(optional_bool(obj.get("has_video"))),
        "duration_seconds": _fmt(optional_seconds(
            obj.get("audio_duration_seconds") or obj.get("duration_seconds")
        )),
        "needs_review": 1 if obj.get("review") else 0,
        "audio_url": _fmt(obj.get("audio_url")),
    }

    def flag_cells(seg_flags: list[dict]) -> dict:
        return {
            "flag_count": len(seg_flags),
            "flag_intents": ", ".join(_fmt(f.get("intent")) for f in seg_flags),
            "flag_severities": ", ".join(_fmt(f.get("s") or f.get("severity")) for f in seg_flags),
            "flag_confs": ", ".join(_fmt(f.get("conf")) for f in seg_flags),
            "flag_transcripts": " | ".join(
                _fmt(f.get("transcript_excerpt") or f.get("transcript")) for f in seg_flags
            ),
        }

    rows = []
    for seg in segments:
        seg_flags = flags_by_seg.get(seg["_seg_id"], [])
        rows.append({
            **session_ctx,
            "seg_id": seg["_seg_id"],
            "ts_start": hms_to_seconds(seg.get("ts_start")),
            "ts_end": hms_to_seconds(seg.get("ts_end")),
            "speaker": _fmt(seg.get("speaker")),
            "tone": _fmt(seg.get("tone")),
            **(flag_cells(seg_flags) if seg_flags else
               {"flag_count": 0, "flag_intents": "", "flag_severities": "",
                "flag_confs": "", "flag_transcripts": ""}),
        })

    # Flags that matched no segment: emit as trailing rows with blank segment cols.
    seg_ids = {seg["_seg_id"] for seg in segments}
    for seg_id, seg_flags in flags_by_seg.items():
        if seg_id in seg_ids:
            continue
        rows.append({
            **session_ctx,
            "seg_id": _fmt(seg_id),
            "ts_start": "", "ts_end": "", "speaker": "", "tone": "",
            **flag_cells(seg_flags),
        })

    return rows


def iter_objects(files):
    """Yield every session object across the given JSON files (each file is one
    object or a list of them)."""
    for fp in files:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[ERROR] {fp.name}: unreadable JSON — {exc}")
            continue
        for obj in (data if isinstance(data, list) else [data]):
            yield fp, obj


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert audio-ingest JSON to a flat one-row-per-segment CSV "
                    "(no database access)."
    )
    parser.add_argument("path", help="JSON file or directory of .json files.")
    parser.add_argument("--out", default=None, help="Output CSV path.")
    args = parser.parse_args()

    src = Path(args.path)
    files = sorted(src.glob("*.json")) if src.is_dir() else [src]
    if not files:
        print(f"No .json files found at {src}")
        sys.exit(1)

    if args.out:
        out_path = Path(args.out)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = Path(__file__).resolve().parents[1] / "exports" / f"audio_ingest_segments_{stamp}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    n_sessions = n_rows = n_bad = 0
    with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for fp, obj in iter_objects(files):
            try:
                rows = session_rows(obj)
            except Exception as exc:  # one bad object shouldn't abort the file
                print(f"[ERROR] {fp.name}: s_id {obj.get('s_id')}: "
                      f"{type(exc).__name__}: {exc} — skipped")
                n_bad += 1
                continue
            writer.writerows(rows)
            n_sessions += 1
            n_rows += len(rows)

    print(f"Sessions : {n_sessions:,}")
    print(f"Rows     : {n_rows:,}  (segments + unmatched-flag rows)")
    if n_bad:
        print(f"Skipped  : {n_bad:,} malformed object(s)")
    print(f"CSV      : {out_path}")


if __name__ == "__main__":
    main()
