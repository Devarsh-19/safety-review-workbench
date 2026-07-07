"""
Batch Gemini audio safety evaluation for sample_audio.csv.

Reads M3U8/audio links from sample_audio.csv, converts each recording to MP3,
uploads the MP3 to Gemini, evaluates it with prompts.py, and writes an enriched
results dataframe with diarized segments, review flags, response, token,
duration, latency, and pricing details.

Example:
    python test_transcription/gemini_audio_batch.py --limit 1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import pandas as pd
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

import prompts


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DEFAULT_INPUT_CSV = BASE_DIR / "sample_audio.csv"
DEFAULT_AUDIO_DIR = BASE_DIR / "audio_files"
DEFAULT_OUTPUT_CSV = BASE_DIR / "data" / "gemini_audio_results.csv"
DEFAULT_OUTPUT_JSONL = BASE_DIR / "data" / "gemini_audio_results.jsonl"
DEFAULT_RAW_JSON_DIR = BASE_DIR / "data" / "raw_json"
DEFAULT_JSON_DIR = BASE_DIR / "data"
DEFAULT_MODEL_ID = "gemini-3-flash-preview"
DEFAULT_THINKING_LEVEL = "minimal"
LONG_PAUSE_THRESHOLD_SECONDS = 60.0
SILENCE_NOISE_THRESHOLD = "-45dB"
FALLBACK_RETRY_USER_MESSAGE = (
    "Your previous response did not validate. Retry once with a compact reply and return only one valid JSON "
    "object that matches the schema exactly, with no markdown or extra commentary."
)


# Gemini 3 Flash Preview standard paid rates, USD per 1M tokens.
TEXT_INPUT_RATE = 0.50 / 1_000_000
AUDIO_INPUT_RATE = 1.00 / 1_000_000
CACHED_TEXT_INPUT_RATE = 0.05 / 1_000_000
CACHED_AUDIO_INPUT_RATE = 0.10 / 1_000_000
CACHE_STORAGE_RATE_PER_HOUR = 1.00 / 1_000_000
OUTPUT_RATE = 3.00 / 1_000_000


@dataclass(frozen=True)
class GeminiCacheInfo:
    name: str
    token_count: int
    storage_cost_usd: float


IntentId = Literal[
    "NSFW",
    "NSFW_EXPLICIT",
    "NSFW_GROOMING",
    "NSFW_APPEARANCE",
    "CSAM_RISK",
    "FINANCIAL_SOLICITATION",
    "IDENTITY_FRAUD",
    "ABUSIVE_LANGUAGE",
    "HATE_SPEECH",
    "FAKE_REMEDIES",
    "UNAUTHORIZED_MEDICAL_ADVICE",
    "SELF_HARM",
    "VIOLENCE",
    "INSTIGATION",
    "OFF_PLATFORM_SOLICITATION",
    "PERSONAL_DATA_COLLECTION",
    "FEAR_MANIPULATION",
    "COMPETITOR_PROMOTION",
]
ToneLabel = Literal[
    "NEUTRAL",
    "CALM",
    "PROFESSIONAL",
    "DISTRESSED",
    "ANGRY",
    "AGGRESSIVE",
    "FLIRTATIOUS",
    "UNCLEAR",
]

SPEAKER_LABEL_RE = re.compile(r"^Speaker [1-9]\d*$")
SPEAKER_LABEL_INPUT_RE = re.compile(r"^speaker\s*0*([1-9]\d*)$", re.IGNORECASE)


def normalize_speaker_label(value: str) -> str:
    normalized = " ".join(str(value).strip().split())
    match = SPEAKER_LABEL_INPUT_RE.fullmatch(normalized.replace(" ", "", 1))
    if not match:
        match = SPEAKER_LABEL_INPUT_RE.fullmatch(normalized)
    if match:
        normalized = f"Speaker {int(match.group(1))}"
    if not SPEAKER_LABEL_RE.fullmatch(normalized):
        raise ValueError("speaker must be a stable numbered label like 'Speaker 1'")
    return normalized


def parse_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            parts = [float(p) for p in value.strip().split(":")]
            seconds = 0.0
            for p in parts:
                seconds = seconds * 60 + p
            return seconds
        except (ValueError, TypeError):
            pass
    return float(value)


class TimestampedModel(BaseModel):
    ts_start: float = Field(ge=0.0, description="Start time in seconds.")
    ts_end: float = Field(ge=0.0, description="End time in seconds.")

    @model_validator(mode="before")
    @classmethod
    def parse_timestamps(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "ts_start" in data and data["ts_start"] is not None:
                data["ts_start"] = parse_timestamp(data["ts_start"])
            if "ts_end" in data and data["ts_end"] is not None:
                data["ts_end"] = parse_timestamp(data["ts_end"])
        return data

    @model_validator(mode="after")
    def validate_timestamp_order(self) -> "TimestampedModel":
        if self.ts_end < self.ts_start:
            raise ValueError("ts_end must be greater than or equal to ts_start")
        return self


class LongPause(TimestampedModel):
    duration_seconds: float = Field(ge=0.0, description="Length of the no-speech/silence span in seconds.")


class SegmentFlag(BaseModel):
    intent: IntentId = Field(description="The matching Intent ID string from the taxonomy.")
    s: Literal["RED", "AMBER"] = Field(description="Severity label.")
    conf: float = Field(ge=0.0, le=1.0, description="Confidence rating from 0.0 to 1.0.")
    transcript_excerpt: str = Field(
        description="Short exact words or ambient event that triggered the flag; not a full transcript."
    )
    ts_start: float | None = Field(default=None, description="Exact start time of the violation in seconds.")
    ts_end: float | None = Field(default=None, description="Exact end time of the violation in seconds.")

    @model_validator(mode="before")
    @classmethod
    def parse_timestamps(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "ts_start" in data and data["ts_start"] is not None:
                data["ts_start"] = parse_timestamp(data["ts_start"])
            if "ts_end" in data and data["ts_end"] is not None:
                data["ts_end"] = parse_timestamp(data["ts_end"])
        return data


class DiarizedSegment(TimestampedModel):
    segment_id: int = Field(ge=1, description="Sequential segment identifier within this audio.")
    speaker: str = Field(description="Stable numbered speaker label such as Speaker 1, Speaker 2, etc.")
    flags: list[SegmentFlag] = Field(default_factory=list, description="Violation flags for this segment.")
    tone: ToneLabel = Field(description="Dominant tone heard in this segment.")

    @field_validator("speaker")
    @classmethod
    def validate_speaker(cls, value: str) -> str:
        return normalize_speaker_label(value)


class AstroTalkAudioReport(BaseModel):
    s_id: str = Field(description="The identifier of the processed session.")
    lang: str = Field(description="Auto-detected language(s).")
    review: bool = Field(description="Always false. Pause-based review is computed externally after LLM evaluation.")
    long_pauses: list[LongPause] = Field(description="Always empty. Long pause detection is handled externally.")
    segments: list[DiarizedSegment] = Field(description="Diarized timestamp/tone/intent metadata segments in order.")


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE pairs without overriding the process environment."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def require_google_api_key() -> str:
    load_env_file(PROJECT_ROOT / ".env")
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Set GOOGLE_API_KEY in the environment or project .env file.")
    return api_key


def _run_subprocess(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _safe_audio_stem(row: pd.Series) -> str:
    recording_url = str(row["recording_url"])
    path_name = Path(urlparse(recording_url).path).name
    if path_name:
        return Path(path_name).stem
    return str(row["session_id"])


def convert_to_mp3(recording_url: str, output_path: Path, force: bool = False) -> None:
    """Convert an M3U8/audio URL to a mono 16k MP3 using ffmpeg."""
    if output_path.exists() and output_path.stat().st_size > 0 and not force:
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        recording_url,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "64k",
        str(output_path),
    ]
    result = _run_subprocess(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {result.stderr.strip()}")


def probe_has_video(media_source: str) -> bool | None:
    """Best-effort video probe for the source media."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        media_source,
    ]
    result = _run_subprocess(cmd)
    if result.returncode != 0:
        print(f"[WARN] ffprobe video probe failed for {media_source}: {result.stderr.strip()}")
        return None
    return any(line.strip() for line in result.stdout.splitlines())


def probe_audio_duration(audio_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    result = _run_subprocess(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe duration failed: {result.stderr.strip()}")
    return round(float(result.stdout.strip()), 3)


def detect_long_pauses(
    audio_path: Path,
    audio_duration_seconds: float,
    threshold_seconds: float = LONG_PAUSE_THRESHOLD_SECONDS,
) -> list[LongPause]:
    """Detect continuous silence/no-speech spans that need manual review."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(audio_path),
        "-af",
        f"silencedetect=noise={SILENCE_NOISE_THRESHOLD}:d={threshold_seconds}",
        "-f",
        "null",
        "-",
    ]
    result = _run_subprocess(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg silence detection failed: {result.stderr.strip()}")

    pauses: list[LongPause] = []
    pending_start: float | None = None
    output = f"{result.stderr}\n{result.stdout}"
    for line in output.splitlines():
        start_match = re.search(r"silence_start:\s*([0-9.]+)", line)
        if start_match:
            pending_start = float(start_match.group(1))

        end_match = re.search(
            r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)",
            line,
        )
        if end_match and pending_start is not None:
            ts_end = float(end_match.group(1))
            duration_seconds = float(end_match.group(2))
            if duration_seconds + 0.001 >= threshold_seconds:
                pauses.append(
                    LongPause(
                        ts_start=round(pending_start, 3),
                        ts_end=round(ts_end, 3),
                        duration_seconds=round(duration_seconds, 3),
                    )
                )
            pending_start = None

    if pending_start is not None:
        duration_seconds = max(0.0, audio_duration_seconds - pending_start)
        if duration_seconds + 0.001 >= threshold_seconds:
            pauses.append(
                LongPause(
                    ts_start=round(pending_start, 3),
                    ts_end=round(audio_duration_seconds, 3),
                    duration_seconds=round(duration_seconds, 3),
                )
            )

    return pauses


def long_pauses_to_json(long_pauses: list[LongPause]) -> str:
    return json.dumps([pause.model_dump() for pause in long_pauses], ensure_ascii=False)


def build_audio_prompt(
    session_id: str,
    audio_duration_seconds: float,
    *,
    retry_compact: bool = False,
) -> str:
    prompt = (
        f"audio_duration_seconds: {audio_duration_seconds}\n"
        "All ts_start and ts_end values must be seconds from the beginning "
        "of this audio and must be within the audio duration.\n\n"
        + prompts.USER_MESSAGE.replace("{s_id}", session_id)
    )
    if retry_compact:
        retry_user_message = getattr(prompts, "RETRY_USER_MESSAGE", "").strip() or FALLBACK_RETRY_USER_MESSAGE
        prompt += "\n\n" + retry_user_message
    return prompt



def gemini_safety_settings() -> list[types.SafetySetting]:
    return [
        types.SafetySetting(category=cat, threshold="OFF")
        for cat in [
            "HARM_CATEGORY_HARASSMENT",
            "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "HARM_CATEGORY_DANGEROUS_CONTENT",
            "HARM_CATEGORY_CIVIC_INTEGRITY",
        ]
    ]


def parse_ttl_seconds(ttl: str) -> float:
    value = ttl.strip().lower()
    multipliers = {"s": 1.0, "m": 60.0, "h": 3600.0}
    suffix = value[-1:] if value else ""
    if suffix in multipliers:
        return float(value[:-1]) * multipliers[suffix]
    return float(value)


def calculate_cache_storage_cost_usd(token_count: int, ttl: str) -> float:
    try:
        ttl_hours = parse_ttl_seconds(ttl) / 3600
    except ValueError:
        print(f"[WARN] Could not parse cache TTL {ttl!r}; cache storage cost set to 0.")
        ttl_hours = 0.0
    return token_count * ttl_hours * CACHE_STORAGE_RATE_PER_HOUR


def build_thinking_config(
    model_id: str,
    thinking_level: str | None = DEFAULT_THINKING_LEVEL,
    thinking_budget: int | None = None,
    include_thoughts: bool = False,
) -> types.ThinkingConfig:
    del model_id

    config_kwargs: dict[str, Any] = {"include_thoughts": include_thoughts}
    normalized_level = (thinking_level or "").strip().lower()

    if thinking_budget is not None:
        config_kwargs["thinking_budget"] = int(thinking_budget)
    elif normalized_level == "minimal":
        config_kwargs["thinking_budget"] = 0
    elif normalized_level:
        config_kwargs["thinking_level"] = normalized_level
    else:
        config_kwargs["thinking_budget"] = 0

    return types.ThinkingConfig(**config_kwargs)


def create_gemini_cache(client: genai.Client, model_id: str, ttl: str) -> GeminiCacheInfo | None:
    try:
        cache = client.caches.create(
            model=model_id,
            config=types.CreateCachedContentConfig(
                display_name="astrotalk_audio_batch_prompt",
                system_instruction=prompts.SYSTEM_PROMPT,
                ttl=ttl,
            ),
        )
        usage = getattr(cache, "usage_metadata", None)
        token_count = int(getattr(usage, "total_token_count", 0) or 0)
        storage_cost = calculate_cache_storage_cost_usd(token_count, ttl)
        return GeminiCacheInfo(
            name=str(cache.name),
            token_count=token_count,
            storage_cost_usd=storage_cost,
        )
    except Exception as exc:
        print(f"[WARN] Cache creation failed; continuing without cache: {exc}")
        return None


def delete_gemini_cache(client: genai.Client, cache_name: str | None) -> None:
    if not cache_name:
        return
    try:
        client.caches.delete(name=cache_name)
    except Exception as exc:
        print(f"[WARN] Cache cleanup failed for {cache_name}: {exc}")


def delete_uploaded_file(client: genai.Client, file_name: str, display_name: str) -> None:
    for attempt in range(1, 4):
        try:
            client.files.delete(name=file_name)
            return
        except Exception as exc:
            if attempt == 3:
                print(f"[WARN] Uploaded file cleanup failed for {display_name}: {exc}")
            else:
                time.sleep(attempt)


def tokens_by_modality(details: Any) -> dict[str, int]:
    counts = {"text": 0, "audio": 0}
    for item in details or []:
        modality = getattr(item, "modality", "")
        if hasattr(modality, "value"):
            modality = modality.value
        token_count = int(getattr(item, "token_count", 0) or 0)
        modality_key = str(modality).lower()
        if "audio" in modality_key:
            counts["audio"] += token_count
        elif "text" in modality_key:
            counts["text"] += token_count
    return counts


def calculate_cost_usd(
    text_input_tokens: int,
    audio_input_tokens: int,
    billable_output_tokens: int,
    cached_text_tokens: int,
    cached_audio_tokens: int,
) -> float:
    non_cached_text = max(0, text_input_tokens - cached_text_tokens)
    non_cached_audio = max(0, audio_input_tokens - cached_audio_tokens)
    return (
        (non_cached_text * TEXT_INPUT_RATE)
        + (non_cached_audio * AUDIO_INPUT_RATE)
        + (cached_text_tokens * CACHED_TEXT_INPUT_RATE)
        + (cached_audio_tokens * CACHED_AUDIO_INPUT_RATE)
        + (billable_output_tokens * OUTPUT_RATE)
    )


def usage_to_telemetry(usage: Any) -> dict[str, int | float]:
    prompt_modalities = tokens_by_modality(getattr(usage, "prompt_tokens_details", None))
    cache_modalities = tokens_by_modality(getattr(usage, "cache_tokens_details", None))

    total_prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    input_audio_tokens = prompt_modalities["audio"]
    input_text_tokens = prompt_modalities["text"] or max(0, total_prompt_tokens - input_audio_tokens)

    cached_audio_tokens = min(input_audio_tokens, cache_modalities["audio"])
    cached_text_tokens = min(input_text_tokens, cache_modalities["text"])
    if cached_text_tokens == 0 and cached_audio_tokens == 0:
        cached_text_tokens = min(input_text_tokens, int(getattr(usage, "cached_content_token_count", 0) or 0))

    candidate_output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
    thinking_tokens = int(getattr(usage, "thoughts_token_count", 0) or 0)
    billable_output_tokens = candidate_output_tokens + thinking_tokens
    total_tokens = int(getattr(usage, "total_token_count", 0) or 0)

    return {
        "input_text_tokens": input_text_tokens,
        "input_audio_tokens": input_audio_tokens,
        "cached_text_tokens": cached_text_tokens,
        "cached_audio_tokens": cached_audio_tokens,
        "candidate_output_tokens": candidate_output_tokens,
        "thinking_tokens": thinking_tokens,
        "billable_output_tokens": billable_output_tokens,
        "output_tokens": billable_output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": calculate_cost_usd(
            input_text_tokens,
            input_audio_tokens,
            billable_output_tokens,
            cached_text_tokens,
            cached_audio_tokens,
        ),
    }


def _same_pause(a: LongPause, b: LongPause, tolerance_seconds: float = 1.0) -> bool:
    return (
        abs(float(a.ts_start) - float(b.ts_start)) <= tolerance_seconds
        and abs(float(a.ts_end) - float(b.ts_end)) <= tolerance_seconds
    )


def is_review_pause(pause: LongPause, threshold_seconds: float = LONG_PAUSE_THRESHOLD_SECONDS) -> bool:
    timestamp_duration = max(0.0, float(pause.ts_end) - float(pause.ts_start))
    reported_duration = float(pause.duration_seconds)
    return max(timestamp_duration, reported_duration) + 0.001 >= threshold_seconds


def normalize_long_pause_duration(pause: LongPause) -> LongPause:
    timestamp_duration = round(max(0.0, float(pause.ts_end) - float(pause.ts_start)), 3)
    if abs(timestamp_duration - float(pause.duration_seconds)) <= 1.0:
        return pause.model_copy(update={"duration_seconds": timestamp_duration})
    return pause


def _segment_overlap_seconds(
    flag_ts_start: float,
    flag_ts_end: float,
    segment: DiarizedSegment,
) -> float:
    return max(0.0, min(flag_ts_end, float(segment.ts_end)) - max(flag_ts_start, float(segment.ts_start)))


def normalize_audio_report(
    report: AstroTalkAudioReport,
    session_id: str,
    local_long_pauses: list[LongPause],
) -> AstroTalkAudioReport:
    ordered_segments = sorted(
        report.segments,
        key=lambda segment: (float(segment.ts_start), float(segment.ts_end), int(segment.segment_id)),
    )
    normalized_segments = ordered_segments
    merged_pauses = [
        normalize_long_pause_duration(pause)
        for pause in report.long_pauses
        if is_review_pause(pause)
    ]
    for local_pause in local_long_pauses:
        if not any(_same_pause(local_pause, existing_pause) for existing_pause in merged_pauses):
            merged_pauses.append(local_pause)
    merged_pauses.sort(key=lambda pause: (pause.ts_start, pause.ts_end))

    return report.model_copy(
        update={
            "s_id": str(session_id),
            "review": bool(merged_pauses),
            "long_pauses": merged_pauses,
            "segments": normalized_segments,
        }
    )


def parse_audio_report(response: Any, session_id: str) -> AstroTalkAudioReport:
    report = getattr(response, "parsed", None)
    if report is None:
        report = AstroTalkAudioReport.model_validate_json(response.text)
    elif not isinstance(report, AstroTalkAudioReport):
        report = AstroTalkAudioReport.model_validate(report)
    return report.model_copy(update={"s_id": str(session_id)})


def evaluate_audio(
    client: genai.Client,
    model_id: str,
    audio_path: Path,
    session_id: str,
    audio_duration_seconds: float,
    local_long_pauses: list[LongPause],
    cache_name: str | None,
    thinking_level: str | None,
) -> tuple[AstroTalkAudioReport, dict[str, int | float], float, str]:
    uploaded_file = None
    try:
        uploaded_file = client.files.upload(file=str(audio_path))
        while uploaded_file.state.name == "PROCESSING":
            time.sleep(2)
            uploaded_file = client.files.get(name=uploaded_file.name)
        if uploaded_file.state.name == "FAILED":
            raise RuntimeError("Gemini file asset processing failed.")

        config_kwargs = {
            "temperature": 0,
            "safety_settings": gemini_safety_settings(),
            "max_output_tokens": 16384,
            "response_mime_type": "application/json",
            "response_schema": AstroTalkAudioReport,
            "thinking_config": build_thinking_config(model_id, thinking_level),
        }
        if cache_name:
            config_kwargs["cached_content"] = cache_name
        config = types.GenerateContentConfig(**config_kwargs)

        def generate_with_prompt(prompt_text: str) -> tuple[Any, float]:
            start_time = time.perf_counter()
            response = client.models.generate_content(
                model=model_id,
                contents=[uploaded_file, prompt_text],
                config=config,
            )
            return response, round(time.perf_counter() - start_time, 3)

        response, latency_seconds = generate_with_prompt(
            build_audio_prompt(session_id, audio_duration_seconds)
        )
        try:
            parsed_report = parse_audio_report(response, session_id)
        except ValidationError:
            print(
                f"[WARN] session {session_id}: Gemini returned invalid/truncated JSON; "
                "retrying once with compact-output instructions"
            )
            response, retry_latency_seconds = generate_with_prompt(
                build_audio_prompt(
                    session_id,
                    audio_duration_seconds,
                    retry_compact=True,
                )
            )
            latency_seconds = round(latency_seconds + retry_latency_seconds, 3)
            parsed_report = parse_audio_report(response, session_id)

        report = normalize_audio_report(parsed_report, session_id, local_long_pauses)
        telemetry = usage_to_telemetry(response.usage_metadata) if response.usage_metadata else {}
        response_json = report.model_dump_json()
        return report, telemetry, latency_seconds, response_json
    finally:
        if uploaded_file:
            delete_uploaded_file(client, uploaded_file.name, audio_path.name)


def invalid_timestamp_records(records: list[dict[str, Any]], audio_duration_seconds: float | None) -> list[dict[str, Any]]:
    if audio_duration_seconds is None:
        return []

    invalid_records = []
    for record in records:
        try:
            ts_start = float(record.get("ts_start", -1))
            ts_end = float(record.get("ts_end", -1))
        except (TypeError, ValueError):
            invalid_records.append(record)
            continue
        if not (0 <= ts_start <= ts_end <= audio_duration_seconds):
            invalid_records.append(record)
    return invalid_records


def max_ts_end(records: list[dict[str, Any]]) -> float:
    ts_end_values = []
    for record in records:
        try:
            ts_end_values.append(float(record.get("ts_end", 0)))
        except (TypeError, ValueError):
            continue
    return max(ts_end_values, default=0.0)


def flatten_result(
    row: pd.Series,
    audio_path: Path,
    audio_duration_seconds: float | None,
    report: AstroTalkAudioReport | None,
    detected_long_pauses: list[LongPause] | None,
    telemetry: dict[str, int | float] | None,
    latency_seconds: float | None,
    response_json: str | None,
    raw_json_path: Path | None,
    status: str,
    error: str | None = None,
    has_video: bool | None = None,
) -> dict[str, Any]:
    flags = []
    if report:
        for seg in report.segments:
            for flag in seg.flags:
                f_dump = flag.model_dump()
                if f_dump.get("ts_start") is None:
                    f_dump["ts_start"] = seg.ts_start
                if f_dump.get("ts_end") is None:
                    f_dump["ts_end"] = seg.ts_end
                f_dump["segment_id"] = seg.segment_id
                f_dump["speaker"] = seg.speaker
                f_dump["tone"] = seg.tone
                flags.append(f_dump)
    segments = [segment.model_dump() for segment in report.segments] if report else []
    if report:
        long_pauses = [pause.model_dump() for pause in report.long_pauses]
        review = report.review
    else:
        long_pauses = [pause.model_dump() for pause in detected_long_pauses or []]
        review = bool(long_pauses)

    invalid_flags = invalid_timestamp_records(flags, audio_duration_seconds)
    invalid_segments = invalid_timestamp_records(segments, audio_duration_seconds)
    invalid_long_pauses = invalid_timestamp_records(long_pauses, audio_duration_seconds)

    result = row.to_dict()
    result.update(
        {
            "status": status,
            "error": error or "",
            "processed_at_utc": datetime.now(UTC).isoformat(),
            "audio_file_path": str(audio_path),
            "audio_duration_seconds": audio_duration_seconds,
            "has_video": has_video,
            "gemini_s_id": report.s_id if report else "",
            "detected_languages": report.lang if report else "",
            "review": review,
            "long_pause_count": len(long_pauses),
            "long_pauses_json": json.dumps(long_pauses, ensure_ascii=False),
            "long_pauses_timestamp_valid": not invalid_long_pauses,
            "long_pauses_timestamp_error_count": len(invalid_long_pauses),
            "long_pauses_max_ts_end": max_ts_end(long_pauses),
            "invalid_long_pauses_json": json.dumps(invalid_long_pauses, ensure_ascii=False),
            "segment_count": len(segments),
            "segments_json": json.dumps(segments, ensure_ascii=False),
            "segments_timestamp_valid": not invalid_segments,
            "segments_timestamp_error_count": len(invalid_segments),
            "segments_max_ts_end": max_ts_end(segments),
            "invalid_segments_json": json.dumps(invalid_segments, ensure_ascii=False),
            "flag_count": len(flags),
            "flags_json": json.dumps(flags, ensure_ascii=False),
            "flags_timestamp_valid": not invalid_flags,
            "flags_timestamp_error_count": len(invalid_flags),
            "flags_max_ts_end": max_ts_end(flags),
            "invalid_flags_json": json.dumps(invalid_flags, ensure_ascii=False),
            "response_json": response_json or "",
            "raw_json_path": str(raw_json_path) if raw_json_path else "",
            "latency_seconds": latency_seconds,
        }
    )
    result.update(telemetry or {})
    return result


def write_outputs(results: list[dict[str, Any]], output_csv: Path, output_jsonl: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(results)
    df.to_csv(output_csv, index=False, encoding="utf-8-sig")
    with output_jsonl.open("w", encoding="utf-8") as handle:
        for record in results:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")



def append_raw_json(response_json: str, recording_url: str, at_flag: Any, has_video: bool | None) -> None:
    output_file = DEFAULT_JSON_DIR / "audio_response.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if output_file.exists():
        try:
            with output_file.open("r", encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, list):
                data = []
        except (json.JSONDecodeError, FileNotFoundError):
            data = []
    else:
        data = []

    try:
        new_record = json.loads(response_json)
    except json.JSONDecodeError:
        new_record = {"raw_response": response_json}
    if isinstance(new_record, dict):
        new_record["audio_url"] = recording_url
        new_record["at_flag"] = at_flag
        new_record["has_video"] = has_video
    elif isinstance(new_record, list):
        new_record = {
            "audio_url": recording_url,
            "at_flag": at_flag,
            "has_video": has_video,
            "response_data": new_record
        }

    data.append(new_record)

    with output_file.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)



def write_raw_response(raw_json_dir: Path, session_id: str, response_json: str) -> Path:
    raw_json_dir.mkdir(parents=True, exist_ok=True)
    raw_json_path = raw_json_dir / f"{session_id}.json"
    parsed = json.loads(response_json)
    raw_json_path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")
    return raw_json_path


def load_completed_sessions(output_csv: Path) -> set[str]:
    if not output_csv.exists():
        return set()
    try:
        previous = pd.read_csv(output_csv, dtype={"session_id": str})
    except Exception:
        return set()
    if "session_id" not in previous or "status" not in previous:
        return set()
    completed = previous[previous["status"].eq("success")]
    return set(completed["session_id"].astype(str))


def load_previous_records(output_csv: Path) -> list[dict[str, Any]]:
    if not output_csv.exists():
        return []
    try:
        return pd.read_csv(output_csv, dtype={"session_id": str}).to_dict("records")
    except Exception:
        return []


def run_batch(args: argparse.Namespace) -> pd.DataFrame:
    client = None if args.skip_gemini else genai.Client(api_key=require_google_api_key())

    input_csv = Path(args.input_csv)
    audio_dir = Path(args.audio_dir)
    output_csv = Path(args.output_csv)
    output_jsonl = Path(args.output_jsonl)
    raw_json_dir = Path(args.raw_json_dir)

    df = pd.read_csv(input_csv, dtype={"session_id": str})
    if args.session_id:
        df = df[df["session_id"].astype(str).eq(str(args.session_id))]
        if df.empty:
            raise ValueError(f"No row found for session_id={args.session_id}")
    if args.limit:
        df = df.head(args.limit)
    df = df.reset_index(drop=True)

    completed_sessions = load_completed_sessions(output_csv) if args.skip_existing else set()
    results: list[dict[str, Any]] = load_previous_records(output_csv) if args.skip_existing else []
    cache_info: GeminiCacheInfo | None = None
    cache_name: str | None = None
    cache_storage_cost_recorded = False

    try:
        if client and not args.no_cache:
            cache_info = create_gemini_cache(client, args.model_id, args.cache_ttl)
        cache_name = cache_info.name if cache_info else None

        for index, row in df.iterrows():
            session_id = str(row["session_id"])
            recording_url = str(row["recording_url"])
            audio_stem = _safe_audio_stem(row)
            audio_path = audio_dir / f"{audio_stem}.mp3"
            has_video: bool | None = None

            if session_id in completed_sessions:
                print(f"[{index + 1}/{len(df)}] skip completed session {session_id}")
                continue

            print(f"[{index + 1}/{len(df)}] session {session_id}: converting/downloading audio")
            try:
                has_video = probe_has_video(recording_url)
                if has_video:
                    print(f"[{index + 1}/{len(df)}] session {session_id}: source media contains video")

                convert_to_mp3(recording_url, audio_path, force=args.force_download)
                duration_seconds = probe_audio_duration(audio_path)
                local_long_pauses = detect_long_pauses(audio_path, duration_seconds)
                if local_long_pauses:
                    print(
                        f"[{index + 1}/{len(df)}] session {session_id}: "
                        f"{len(local_long_pauses)} long pause(s) need review"
                    )

                if args.skip_gemini:
                    record = flatten_result(
                        row,
                        audio_path,
                        duration_seconds,
                        report=None,
                        detected_long_pauses=local_long_pauses,
                        telemetry={},
                        latency_seconds=None,
                        response_json=None,
                        raw_json_path=None,
                        status="downloaded",
                        has_video=has_video,
                    )
                else:
                    print(f"[{index + 1}/{len(df)}] session {session_id}: uploading to Gemini")
                    if client is None:
                        raise RuntimeError("Gemini client was not initialized.")
                    report, telemetry, latency, response_json = evaluate_audio(
                        client,
                        args.model_id,
                        audio_path,
                        session_id,
                        duration_seconds,
                        local_long_pauses,
                        cache_name,
                        args.thinking_level,
                    )
                    raw_json_path = write_raw_response(raw_json_dir, session_id, response_json)
                    append_raw_json(
                        response_json=response_json,
                        recording_url=recording_url,
                        at_flag=row.get("flagged", ""),
                        has_video=has_video,
                    )
                    record = flatten_result(
                        row,
                        audio_path,
                        duration_seconds,
                        report,
                        detected_long_pauses=local_long_pauses,
                        telemetry=telemetry,
                        latency_seconds=latency,
                        response_json=response_json,
                        raw_json_path=raw_json_path,
                        status="success",
                        has_video=has_video,
                    )
                    cost = record.get("estimated_cost_usd", 0.0)
                    print(
                        f"[{index + 1}/{len(df)}] session {session_id}: "
                        f"{record['flag_count']} flags, ${float(cost):.6f}, {latency:.1f}s"
                    )
            except Exception as exc:
                record = flatten_result(
                    row,
                    audio_path,
                    audio_duration_seconds=None,
                    report=None,
                    detected_long_pauses=None,
                    telemetry={},
                    latency_seconds=None,
                    response_json=None,
                    raw_json_path=None,
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                    has_video=has_video,
                )
                print(f"[ERROR] session {session_id}: {record['error']}")

            record["model_id"] = args.model_id
            record["cache_name"] = cache_name or ""
            record["cache_storage_tokens"] = cache_info.token_count if cache_info else 0
            if cache_info and not cache_storage_cost_recorded:
                record["cache_storage_cost_usd"] = cache_info.storage_cost_usd
                cache_storage_cost_recorded = True
            else:
                record["cache_storage_cost_usd"] = 0.0
            record["thinking_level"] = args.thinking_level or ""
            results.append(record)
            write_outputs(results, output_csv, output_jsonl)
    finally:
        if client and args.delete_cache:
            delete_gemini_cache(client, cache_name)
        elif client and cache_name:
            print(f"[INFO] Cache retained until TTL expires: {cache_name}")

    return pd.DataFrame(results)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download sample audio and evaluate it with Gemini.")
    parser.add_argument("--input-csv", default=str(DEFAULT_INPUT_CSV), help="CSV with session_id and recording_url.")
    parser.add_argument("--audio-dir", default=str(DEFAULT_AUDIO_DIR), help="Directory where MP3 files are saved.")
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV), help="Enriched result dataframe CSV.")
    parser.add_argument("--output-jsonl", default=str(DEFAULT_OUTPUT_JSONL), help="Line-delimited JSON results.")
    parser.add_argument("--raw-json-dir", default=str(DEFAULT_RAW_JSON_DIR), help="Directory for per-session raw JSON responses.")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID, help="Gemini model ID.")
    parser.add_argument("--cache-ttl", default="3600s", help="Gemini cached prompt TTL.")
    parser.add_argument("--session-id", default=None, help="Process only this session_id from the input CSV.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N rows.")
    parser.add_argument("--force-download", action="store_true", help="Redownload/reconvert MP3 files.")
    parser.add_argument("--skip-existing", action="store_true", help="Skip sessions already marked success in output CSV.")
    parser.add_argument("--skip-gemini", action="store_true", help="Only download/probe audio; do not call Gemini.")
    parser.add_argument("--no-cache", action="store_true", help="Do not create a Gemini cached system prompt.")
    parser.add_argument("--delete-cache", action="store_true", help="Delete the Gemini cache after this run.")
    parser.add_argument(
        "--thinking-level",
        choices=["low", "medium", "high", "minimal"],
        default=DEFAULT_THINKING_LEVEL,
        help="Gemini 3 thinking effort. Defaults to 'minimal' for the lowest reasoning setting.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    final_df = run_batch(parse_args())
    if not final_df.empty:
        request_cost = final_df.get("estimated_cost_usd", pd.Series(dtype=float)).fillna(0).sum()
        cache_storage_cost = final_df.get("cache_storage_cost_usd", pd.Series(dtype=float)).fillna(0).sum()
        total_cost = request_cost + cache_storage_cost
        total_duration = final_df.get("audio_duration_seconds", pd.Series(dtype=float)).fillna(0).sum()
        print(f"Processed rows: {len(final_df)}")
        print(f"Total audio duration seconds: {total_duration:.3f}")
        print(f"Estimated Gemini request cost USD: {request_cost:.6f}")
        print(f"Estimated Gemini cache storage cost USD: {cache_storage_cost:.6f}")
        print(f"Estimated total Gemini cost USD: {total_cost:.6f}")
