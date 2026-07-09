"""
Batch Gemini audio safety evaluation for sample_audio.csv.

Reads M3U8/audio links from sample_audio.csv, converts each recording to MP3,
uploads the MP3 to Gemini, evaluates it with audio_prompts.py, and writes an enriched
results dataframe with diarized segments, review flags, response, token,
duration, latency, and pricing details.

Example:
    python test_transcription/gemini_audio_batch.py --limit 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
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
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

import audio_prompts as prompts


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DEFAULT_INPUT_CSV = BASE_DIR / "sample_audio.csv"
DEFAULT_AUDIO_DIR = BASE_DIR / "audio_files"
DEFAULT_OUTPUT_CSV = BASE_DIR / "data" / "gemini_audio_results.csv"
DEFAULT_OUTPUT_JSONL = BASE_DIR / "data" / "gemini_audio_results.jsonl"
DEFAULT_RAW_JSON_DIR = BASE_DIR / "data" / "raw_json"
DEFAULT_JSON_DIR = BASE_DIR / "data"
DEFAULT_PROCESSING_LOG = BASE_DIR / "data" / "processing_log.json"
DEFAULT_MODEL_ID = "gemini-3-flash-preview"
DEFAULT_THINKING_LEVEL = "minimal"
LONG_PAUSE_THRESHOLD_SECONDS = 60.0
SILENCE_NOISE_THRESHOLD = "-45dB"
UPLOAD_PROCESSING_TIMEOUT_SECONDS = 600.0
# Rate-limit / transient-error backoff: 429 (quota) plus retryable 5xx.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
BACKOFF_MAX_RETRIES = 6
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0
# Per-channel speech-span detection for stereo (one-party-per-channel) calls.
CHANNEL_SPAN_MIN_SILENCE_SECONDS = 0.6
CHANNEL_SPAN_MIN_SPEECH_SECONDS = 0.4
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
    # Accept underscore diarization variants ("SPEAKER_1") alongside "Speaker 1".
    normalized = " ".join(str(value).strip().replace("_", " ").split())
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


# Wire models define the response schema sent to Gemini. Timestamps are
# "MM:SS"/"MM:SS.d" strings because Gemini's audio understanding is trained on
# MM:SS positions; forcing float seconds made it emit an ambiguous mix of
# formats. parse_timestamp() converts the strings into float seconds when the
# wire report is validated into the internal AstroTalkAudioReport.
_TS_STRING_DESCRIPTION = (
    'Timestamp as an "MM:SS" or "MM:SS.d" string measured from the beginning of the audio, e.g. "02:04.5".'
)


class WireSegmentFlag(BaseModel):
    intent: IntentId = Field(description="The matching Intent ID string from the taxonomy.")
    s: Literal["RED", "AMBER"] = Field(description="Severity label.")
    conf: float = Field(ge=0.0, le=1.0, description="Confidence rating from 0.0 to 1.0.")
    transcript_excerpt: str = Field(
        description="Short exact words or ambient event that triggered the flag; not a full transcript."
    )
    ts_start: str = Field(description=f"Exact violation start. {_TS_STRING_DESCRIPTION}")
    ts_end: str = Field(description=f"Exact violation end. {_TS_STRING_DESCRIPTION}")


class WireDiarizedSegment(BaseModel):
    segment_id: int = Field(ge=1, description="Sequential segment identifier within this audio.")
    speaker: str = Field(description="Stable numbered speaker label such as Speaker 1, Speaker 2, etc.")
    ts_start: str = Field(description=f"Segment start. {_TS_STRING_DESCRIPTION}")
    ts_end: str = Field(description=f"Segment end. {_TS_STRING_DESCRIPTION}")
    flags: list[WireSegmentFlag] = Field(description="Violation flags for this segment.")
    tone: ToneLabel = Field(description="Dominant tone heard in this segment.")


class WireAudioReport(BaseModel):
    s_id: str = Field(description="The identifier of the processed session.")
    lang: str = Field(description="Auto-detected language(s).")
    segments: list[WireDiarizedSegment] = Field(description="Diarized timestamp/tone/intent metadata segments in order.")


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


def convert_to_mp3(recording_url: str, output_path: Path, force: bool = False, channels: int = 1) -> None:
    """Convert an M3U8/audio URL to a 16k MP3 using ffmpeg.

    Stereo call recordings keep both channels (one party per channel) so
    speaker turns can be derived per channel; everything else is mono.
    """
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
        "2" if channels >= 2 else "1",
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


def probe_audio_channels(media_source: str) -> int | None:
    """Best-effort channel count for the first audio stream."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=channels",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        media_source,
    ]
    result = _run_subprocess(cmd)
    if result.returncode != 0:
        print(f"[WARN] ffprobe channel probe failed for {media_source}: {result.stderr.strip()}")
        return None
    try:
        return int(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


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


def seconds_to_mmss(seconds: float) -> str:
    total = round(max(0.0, float(seconds)), 1)
    minutes = int(total // 60)
    secs = round(total - minutes * 60, 1)
    if secs >= 60.0:
        minutes += 1
        secs = 0.0
    return f"{minutes:02d}:{secs:04.1f}"


def detect_channel_speech_spans(
    audio_path: Path,
    channel_index: int,
    audio_duration_seconds: float,
    min_silence_seconds: float = CHANNEL_SPAN_MIN_SILENCE_SECONDS,
    min_speech_seconds: float = CHANNEL_SPAN_MIN_SPEECH_SECONDS,
) -> list[tuple[float, float]]:
    """Speech spans for one channel of a stereo call, via inverted silencedetect."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(audio_path),
        "-af",
        (
            f"pan=mono|c0=c{channel_index},"
            f"silencedetect=noise={SILENCE_NOISE_THRESHOLD}:d={min_silence_seconds}"
        ),
        "-f",
        "null",
        "-",
    ]
    result = _run_subprocess(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg channel silence detection failed: {result.stderr.strip()}")

    silences: list[tuple[float, float]] = []
    pending_start: float | None = None
    output = f"{result.stderr}\n{result.stdout}"
    for line in output.splitlines():
        start_match = re.search(r"silence_start:\s*([0-9.]+)", line)
        if start_match:
            pending_start = float(start_match.group(1))
        end_match = re.search(r"silence_end:\s*([0-9.]+)", line)
        if end_match and pending_start is not None:
            silences.append((pending_start, float(end_match.group(1))))
            pending_start = None
    if pending_start is not None:
        silences.append((pending_start, audio_duration_seconds))

    spans: list[tuple[float, float]] = []
    cursor = 0.0
    for silence_start, silence_end in silences:
        if silence_start - cursor >= min_speech_seconds:
            spans.append((round(cursor, 3), round(silence_start, 3)))
        cursor = max(cursor, silence_end)
    if audio_duration_seconds - cursor >= min_speech_seconds:
        spans.append((round(cursor, 3), round(audio_duration_seconds, 3)))
    return spans


def build_speaker_turn_map(spans_by_channel: list[list[tuple[float, float]]]) -> str | None:
    """Render per-channel speech spans as the SPEAKER CHANNEL MAP prompt block."""
    if not any(spans_by_channel):
        return None
    channel_names = ("left channel", "right channel")
    lines = [
        "This map is AUTHORITATIVE; each party of this call was recorded on a separate channel:",
    ]
    for index, spans in enumerate(spans_by_channel):
        rendered = ", ".join(f"{seconds_to_mmss(start)}-{seconds_to_mmss(end)}" for start, end in spans)
        channel_name = channel_names[index] if index < len(channel_names) else f"channel {index + 1}"
        lines.append(f"Speaker {index + 1} ({channel_name}) speaks during: {rendered or 'never'}")
    lines.append(
        "Attribute each segment and flag to the speaker whose spans cover its time range; "
        "keep these speaker labels consistent for the whole audio."
    )
    return "\n".join(lines)


def build_audio_prompt(
    session_id: str,
    audio_duration_seconds: float,
    *,
    speaker_turn_map: str | None = None,
    retry_compact: bool = False,
) -> str:
    sections = [
        "=== AUDIO METADATA ===",
        f"audio_duration: {seconds_to_mmss(audio_duration_seconds)} "
        f"({audio_duration_seconds} seconds total)",
        'All ts_start and ts_end values must be "MM:SS" or "MM:SS.d" strings measured '
        "from the beginning of this audio and must not exceed the audio duration.",
    ]
    if speaker_turn_map:
        sections += ["", "=== SPEAKER CHANNEL MAP ===", speaker_turn_map.strip()]
    sections += ["", "=== TASK ===", prompts.USER_MESSAGE.replace("{s_id}", session_id).strip()]
    if retry_compact:
        retry_user_message = getattr(prompts, "RETRY_USER_MESSAGE", "").strip() or FALLBACK_RETRY_USER_MESSAGE
        sections += ["", "=== RETRY INSTRUCTIONS ===", retry_user_message]
    return "\n".join(sections) + "\n"



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


async def call_with_backoff(operation, description: str):
    """Run an async Gemini call, retrying rate-limit (429) and transient 5xx
    errors with exponential backoff + full jitter. Non-retryable errors and
    exhausted retries propagate to the caller, which records an error row."""
    for attempt in range(BACKOFF_MAX_RETRIES + 1):
        try:
            return await operation()
        except genai_errors.APIError as exc:
            code = getattr(exc, "code", None)
            if code not in RETRYABLE_STATUS_CODES or attempt == BACKOFF_MAX_RETRIES:
                raise
            delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** attempt))
            delay *= random.uniform(0.5, 1.0)
            print(
                f"[WARN] {description}: HTTP {code} ({getattr(exc, 'status', '')}) — "
                f"backing off {delay:.1f}s (attempt {attempt + 1}/{BACKOFF_MAX_RETRIES})"
            )
            await asyncio.sleep(delay)


async def delete_uploaded_file(client: genai.Client, file_name: str, display_name: str) -> None:
    for attempt in range(1, 4):
        try:
            await client.aio.files.delete(name=file_name)
            return
        except Exception as exc:
            if attempt == 3:
                print(f"[WARN] Uploaded file cleanup failed for {display_name}: {exc}")
            else:
                await asyncio.sleep(attempt)


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


def merge_telemetry(base: dict[str, int | float], extra: dict[str, int | float]) -> dict[str, int | float]:
    """Sum token/cost telemetry across multiple Gemini calls (e.g. a retry).
    Every field is additive: token counts and estimated_cost_usd."""
    merged = dict(base)
    for key, value in extra.items():
        merged[key] = merged.get(key, 0) + value
    return merged


def parse_audio_report(response: Any, session_id: str) -> AstroTalkAudioReport:
    wire = getattr(response, "parsed", None)
    if wire is None:
        wire = WireAudioReport.model_validate_json(response.text)
    elif not isinstance(wire, WireAudioReport):
        wire = WireAudioReport.model_validate(wire)
    # Internal validators convert the wire "MM:SS" strings into float seconds.
    data = wire.model_dump()
    data["review"] = False
    data["long_pauses"] = []
    report = AstroTalkAudioReport.model_validate(data)
    return report.model_copy(update={"s_id": str(session_id)})


async def evaluate_audio(
    client: genai.Client,
    model_id: str,
    audio_path: Path,
    session_id: str,
    audio_duration_seconds: float,
    local_long_pauses: list[LongPause],
    cache_name: str | None,
    thinking_level: str | None,
    speaker_turn_map: str | None = None,
) -> tuple[AstroTalkAudioReport, dict[str, int | float], float, str]:
    uploaded_file = None
    try:
        uploaded_file = await call_with_backoff(
            lambda: client.aio.files.upload(file=str(audio_path)),
            f"session {session_id}: file upload",
        )
        upload_wait_started = time.monotonic()
        while uploaded_file.state.name == "PROCESSING":
            if time.monotonic() - upload_wait_started > UPLOAD_PROCESSING_TIMEOUT_SECONDS:
                raise RuntimeError(
                    f"Gemini file asset stuck in PROCESSING for over "
                    f"{UPLOAD_PROCESSING_TIMEOUT_SECONDS:.0f}s."
                )
            await asyncio.sleep(2)
            uploaded_file = await call_with_backoff(
                lambda: client.aio.files.get(name=uploaded_file.name),
                f"session {session_id}: file status poll",
            )
        if uploaded_file.state.name == "FAILED":
            raise RuntimeError("Gemini file asset processing failed.")

        config_kwargs = {
            "temperature": 0,
            "safety_settings": gemini_safety_settings(),
            "max_output_tokens": 16384,
            "response_mime_type": "application/json",
            "response_schema": WireAudioReport,
            "thinking_config": build_thinking_config(model_id, thinking_level),
        }
        if cache_name:
            config_kwargs["cached_content"] = cache_name
        else:
            # No explicit cache: send the system prompt inline. It is a large
            # static block that is byte-identical across requests, so Gemini's
            # implicit caching discounts the shared prefix automatically.
            # This also guarantees the taxonomy is present when explicit cache
            # creation fails mid-run.
            config_kwargs["system_instruction"] = prompts.SYSTEM_PROMPT
        config = types.GenerateContentConfig(**config_kwargs)

        async def generate_with_prompt(prompt_text: str) -> tuple[Any, float]:
            start_time = time.perf_counter()
            response = await call_with_backoff(
                lambda: client.aio.models.generate_content(
                    model=model_id,
                    contents=[uploaded_file, prompt_text],
                    config=config,
                ),
                f"session {session_id}: generate_content",
            )
            return response, round(time.perf_counter() - start_time, 3)

        response, latency_seconds = await generate_with_prompt(
            build_audio_prompt(session_id, audio_duration_seconds, speaker_turn_map=speaker_turn_map)
        )
        # Telemetry is captured per call and merged, so a retry's cost includes
        # the failed first attempt's tokens instead of silently dropping them.
        telemetry = usage_to_telemetry(response.usage_metadata) if response.usage_metadata else {}
        try:
            parsed_report = parse_audio_report(response, session_id)
        except ValidationError:
            print(
                f"[WARN] session {session_id}: Gemini returned invalid/truncated JSON; "
                "retrying once with compact-output instructions"
            )
            response, retry_latency_seconds = await generate_with_prompt(
                build_audio_prompt(
                    session_id,
                    audio_duration_seconds,
                    speaker_turn_map=speaker_turn_map,
                    retry_compact=True,
                )
            )
            latency_seconds = round(latency_seconds + retry_latency_seconds, 3)
            if response.usage_metadata:
                telemetry = merge_telemetry(telemetry, usage_to_telemetry(response.usage_metadata))
            parsed_report = parse_audio_report(response, session_id)

        report = normalize_audio_report(parsed_report, session_id, local_long_pauses)
        response_json = report.model_dump_json()
        return report, telemetry, latency_seconds, response_json
    finally:
        if uploaded_file:
            await delete_uploaded_file(client, uploaded_file.name, audio_path.name)


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


def json_default(value: Any) -> Any:
    """Serialize numpy scalars (int64/float64/bool_) that json can't handle.
    pandas rows carry numpy types; .item() converts them to native Python."""
    item = getattr(value, "item", None)
    if callable(item):
        return item()
    return str(value)


def write_outputs(results: list[dict[str, Any]], output_csv: Path, output_jsonl: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(results)
    df.to_csv(output_csv, index=False, encoding="utf-8-sig")
    with output_jsonl.open("w", encoding="utf-8") as handle:
        for record in results:
            handle.write(json.dumps(record, ensure_ascii=False, default=json_default) + "\n")



def append_raw_json(
    response_json: str,
    recording_url: str,
    at_flag: Any,
    has_video: bool | None,
    audio_duration_seconds: float | None = None,
    cache_name: str | None = None,
) -> None:
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
        new_record["at_flag"] = True if at_flag == "Yes" else False
        new_record["has_video"] = has_video
        new_record["audio_duration_seconds"] = audio_duration_seconds
        new_record["cache_name"] = cache_name or ""
    elif isinstance(new_record, list):
        new_record = {
            "audio_url": recording_url,
            "at_flag": at_flag,
            "has_video": has_video,
            "audio_duration_seconds": audio_duration_seconds,
            "cache_name": cache_name or "",
            "response_data": new_record
        }

    data.append(new_record)

    with output_file.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False, default=json_default)



def _append_json_entry(log_path: Path, entry: dict[str, Any]) -> None:
    """Append one entry to a JSON-array log file (read, append, rewrite).
    Same pattern as append_raw_json: no awaits between read and write, so
    concurrent workers on the event loop can't interleave on the file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entries: list = []
    if log_path.exists():
        try:
            existing = json.loads(log_path.read_text(encoding="utf-8"))
            if isinstance(existing, list):
                entries = existing
        except (json.JSONDecodeError, OSError):
            entries = []
    entries.append(entry)
    log_path.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False, default=json_default),
        encoding="utf-8",
    )


def log_session_processing(log_path: Path, record: dict[str, Any]) -> None:
    """Append one per-session entry to the processing log the moment the
    session finishes: timing (start/end/duration) plus the session's output."""
    result = None
    if record.get("response_json"):
        try:
            result = json.loads(record["response_json"])
        except (json.JSONDecodeError, TypeError):
            result = record["response_json"]

    _append_json_entry(log_path, {
        "type": "session",
        "session_id": record.get("session_id"),
        "status": record.get("status"),
        "error": record.get("error") or None,
        "started_at_utc": record.get("processing_started_utc"),
        "ended_at_utc": record.get("processing_ended_utc"),
        "processing_seconds": record.get("processing_seconds"),
        "gemini_latency_seconds": record.get("latency_seconds"),
        "audio_duration_seconds": record.get("audio_duration_seconds"),
        "detected_languages": record.get("detected_languages"),
        "review": record.get("review"),
        "flag_count": record.get("flag_count"),
        "long_pause_count": record.get("long_pause_count"),
        "total_tokens": record.get("total_tokens"),
        "estimated_cost_usd": record.get("estimated_cost_usd"),
        "cache_mode": record.get("cache_mode"),
        "cache_name": record.get("cache_name"),
        "result": result,
    })


def log_run_summary(
    log_path: Path,
    batch_started_utc: str,
    batch_started_perf: float,
    results: list[dict[str, Any]],
    processed_session_ids: set[str],
) -> None:
    """Append the end-of-run entry: batch start/end times, wall duration, and
    totals over the sessions processed in THIS run."""
    processed = [r for r in results if str(r.get("session_id")) in processed_session_ids]
    status_counts: dict[str, int] = {}
    for r in processed:
        status_counts[str(r.get("status"))] = status_counts.get(str(r.get("status")), 0) + 1

    def _num(value) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    wall_seconds = round(time.perf_counter() - batch_started_perf, 3)
    total_processing_seconds = round(sum(_num(r.get("processing_seconds")) for r in processed), 3)
    total_audio_seconds = round(sum(_num(r.get("audio_duration_seconds")) for r in processed), 3)
    audio_minutes = total_audio_seconds / 60
    # Wall time is shared across concurrent sessions; processing time is the
    # per-session sum, so it exceeds wall time when concurrency > 1.
    wall_per_audio_minute = round(wall_seconds / audio_minutes, 3) if audio_minutes else None
    processing_per_audio_minute = round(total_processing_seconds / audio_minutes, 3) if audio_minutes else None

    _append_json_entry(log_path, {
        "type": "run_summary",
        "batch_started_utc": batch_started_utc,
        "batch_ended_utc": datetime.now(UTC).isoformat(),
        "batch_wall_seconds": wall_seconds,
        "sessions_processed": len(processed),
        "status_counts": status_counts,
        "total_processing_seconds": total_processing_seconds,
        "total_audio_duration_seconds": total_audio_seconds,
        "avg_processing_seconds_per_session": round(total_processing_seconds / len(processed), 3) if processed else None,
        "wall_seconds_per_audio_minute": wall_per_audio_minute,
        "processing_seconds_per_audio_minute": processing_per_audio_minute,
        "cache_name": next((str(r.get("cache_name")) for r in processed if r.get("cache_name")), ""),
        "total_estimated_cost_usd": round(sum(_num(r.get("estimated_cost_usd")) for r in processed), 6),
    })

    if processed:
        print(
            f"Total run time: {wall_seconds:.1f}s ({wall_seconds / 60:.2f} min) for "
            f"{len(processed)} session(s) - avg {wall_seconds / len(processed):.2f}s/session"
        )
        if audio_minutes:
            print(
                f"Throughput: {wall_per_audio_minute:.2f}s wall time per 1 min of audio "
                f"({processing_per_audio_minute:.2f}s per-session processing per 1 min of audio)"
            )


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


async def process_session(
    index: int,
    total: int,
    row: pd.Series,
    args: argparse.Namespace,
    client: genai.Client | None,
    cache_name: str | None,
    audio_dir: Path,
    raw_json_dir: Path,
) -> tuple[str, dict[str, Any]]:
    """Download, probe, and evaluate one session. Always returns a record —
    failures come back as status='error' rows, never as raised exceptions.

    ffmpeg/ffprobe subprocesses run in worker threads; the Gemini calls are
    truly async (client.aio), so sessions overlap up to the semaphore limit.
    """
    session_id = str(row["session_id"])
    recording_url = str(row["recording_url"])
    audio_stem = _safe_audio_stem(row)
    audio_path = audio_dir / f"{audio_stem}.mp3"
    has_video: bool | None = None
    tag = f"[{index + 1}/{total}] session {session_id}"
    started_at_utc = datetime.now(UTC).isoformat()
    started_perf = time.perf_counter()

    print(f"{tag}: converting/downloading audio")
    try:
        has_video = await asyncio.to_thread(probe_has_video, recording_url)
        if has_video:
            print(f"{tag}: source media contains video")

        source_channels = await asyncio.to_thread(probe_audio_channels, recording_url)
        await asyncio.to_thread(
            convert_to_mp3, recording_url, audio_path, args.force_download, source_channels or 1
        )
        duration_seconds = await asyncio.to_thread(probe_audio_duration, audio_path)
        local_long_pauses = await asyncio.to_thread(detect_long_pauses, audio_path, duration_seconds)
        if local_long_pauses:
            print(f"{tag}: {len(local_long_pauses)} long pause(s) need review")

        # Stereo call recordings carry one party per channel: derive the exact
        # speaker turn map locally and hand it to Gemini as ground truth
        # (probe the local file — cached files from older runs may be mono).
        speaker_turn_map: str | None = None
        local_channels = await asyncio.to_thread(probe_audio_channels, str(audio_path))
        if local_channels and local_channels >= 2:
            spans_by_channel = [
                await asyncio.to_thread(
                    detect_channel_speech_spans, audio_path, channel_index, duration_seconds
                )
                for channel_index in (0, 1)
            ]
            speaker_turn_map = build_speaker_turn_map(spans_by_channel)
            if speaker_turn_map:
                print(f"{tag}: stereo source — channel-based speaker map attached")

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
            print(f"{tag}: uploading to Gemini")
            if client is None:
                raise RuntimeError("Gemini client was not initialized.")
            report, telemetry, latency, response_json = await evaluate_audio(
                client,
                args.model_id,
                audio_path,
                session_id,
                duration_seconds,
                local_long_pauses,
                cache_name,
                args.thinking_level,
                speaker_turn_map,
            )
            raw_json_path = write_raw_response(raw_json_dir, session_id, response_json)
            # No await between the read and write inside append_raw_json, so
            # concurrent workers can't interleave on the shared JSON file.
            append_raw_json(
                response_json=response_json,
                recording_url=recording_url,
                at_flag=row.get("flagged", ""),
                has_video=has_video,
                audio_duration_seconds=duration_seconds,
                cache_name=cache_name,
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
            print(f"{tag}: {record['flag_count']} flags, ${float(cost):.6f}, {latency:.1f}s")
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

    record["processing_started_utc"] = started_at_utc
    record["processing_ended_utc"] = datetime.now(UTC).isoformat()
    record["processing_seconds"] = round(time.perf_counter() - started_perf, 3)
    return session_id, record


async def run_batch(args: argparse.Namespace) -> pd.DataFrame:
    client = None if args.skip_gemini else genai.Client(api_key=require_google_api_key())

    input_csv = Path(args.input_csv)
    audio_dir = Path(args.audio_dir)
    output_csv = Path(args.output_csv)
    output_jsonl = Path(args.output_jsonl)
    raw_json_dir = Path(args.raw_json_dir)
    processing_log = Path(args.processing_log)
    batch_started_utc = datetime.now(UTC).isoformat()
    batch_started_perf = time.perf_counter()
    processed_session_ids: set[str] = set()

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

    semaphore = asyncio.Semaphore(max(1, args.concurrency))

    async def bounded(index: int, row: pd.Series) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            return await process_session(
                index, len(df), row, args, client, cache_name, audio_dir, raw_json_dir
            )

    cache_mode = "implicit" if args.no_cache else args.cache_mode

    try:
        if client and cache_mode == "explicit":
            cache_info = create_gemini_cache(client, args.model_id, args.cache_ttl)
            if cache_info is None:
                cache_mode = "implicit"
                print("[INFO] Falling back to implicit caching (inline system prompt).")
        cache_name = cache_info.name if cache_info else None

        tasks: list[asyncio.Task] = []
        for index, row in df.iterrows():
            session_id = str(row["session_id"])
            if session_id in completed_sessions:
                print(f"[{index + 1}/{len(df)}] skip completed session {session_id}")
                continue
            tasks.append(asyncio.create_task(bounded(index, row)))

        # Results are appended and flushed as each session finishes, so an
        # interrupted run still leaves everything completed so far on disk.
        for finished in asyncio.as_completed(tasks):
            session_id, record = await finished
            record["model_id"] = args.model_id
            record["cache_mode"] = cache_mode
            record["cache_name"] = cache_name or ""
            record["cache_storage_tokens"] = cache_info.token_count if cache_info else 0
            if cache_info and not cache_storage_cost_recorded:
                record["cache_storage_cost_usd"] = cache_info.storage_cost_usd
                cache_storage_cost_recorded = True
            else:
                record["cache_storage_cost_usd"] = 0.0
            record["thinking_level"] = args.thinking_level or ""
            # Drop any stale record for this session (e.g. an old error row
            # kept by --skip-existing) so the outputs never hold duplicates.
            results = [r for r in results if str(r.get("session_id")) != session_id]
            results.append(record)
            write_outputs(results, output_csv, output_jsonl)
            # Session done -> append its timing + output to the processing log
            # immediately, so the log is complete up to the last finished
            # session even if the run is interrupted.
            processed_session_ids.add(session_id)
            log_session_processing(processing_log, record)
    finally:
        try:
            # Run-summary entry carries the batch end time; written in finally
            # so an interrupted run still records when and where it stopped.
            log_run_summary(
                processing_log, batch_started_utc, batch_started_perf,
                results, processed_session_ids,
            )
        except Exception as exc:
            print(f"[WARN] processing log summary failed: {exc}")
        if client and args.delete_cache:
            delete_gemini_cache(client, cache_name)
        elif client and cache_name:
            print(f"[INFO] Cache retained until TTL expires: {cache_name}")

    # Completion order is nondeterministic under concurrency — restore the
    # input CSV order for the final outputs (stable sort keeps records for
    # sessions outside this run, e.g. --skip-existing carryovers, in front).
    if results:
        input_order = {sid: i for i, sid in enumerate(df["session_id"].astype(str))}
        results.sort(key=lambda r: input_order.get(str(r.get("session_id")), -1))
        write_outputs(results, output_csv, output_jsonl)

    return pd.DataFrame(results)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download sample audio and evaluate it with Gemini.")
    parser.add_argument("--input-csv", default=str(DEFAULT_INPUT_CSV), help="CSV with session_id and recording_url.")
    parser.add_argument("--audio-dir", default=str(DEFAULT_AUDIO_DIR), help="Directory where MP3 files are saved.")
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV), help="Enriched result dataframe CSV.")
    parser.add_argument("--output-jsonl", default=str(DEFAULT_OUTPUT_JSONL), help="Line-delimited JSON results.")
    parser.add_argument("--raw-json-dir", default=str(DEFAULT_RAW_JSON_DIR), help="Directory for per-session raw JSON responses.")
    parser.add_argument(
        "--processing-log",
        default=str(DEFAULT_PROCESSING_LOG),
        help="Single JSON file that gets one timing+output entry appended per finished session, plus a run summary.",
    )
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID, help="Gemini model ID.")
    parser.add_argument("--cache-ttl", default="3600s", help="Gemini cached prompt TTL.")
    parser.add_argument("--session-id", default=None, help="Process only this session_id from the input CSV.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N rows.")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=16,
        help="Number of sessions processed in parallel (download + Gemini). Use 1 for the old sequential behaviour.",
    )
    parser.add_argument("--force-download", action="store_true", help="Redownload/reconvert MP3 files.")
    parser.add_argument("--skip-existing", action="store_true", help="Skip sessions already marked success in output CSV.")
    parser.add_argument("--skip-gemini", action="store_true", help="Only download/probe audio; do not call Gemini.")
    parser.add_argument(
        "--cache-mode",
        choices=["explicit", "implicit"],
        default="explicit",
        help=(
            "explicit: create a Gemini cached-content object for the system prompt "
            "(guaranteed discount + storage cost). implicit: send the system prompt "
            "inline and rely on Gemini's automatic implicit prefix caching "
            "(best-effort discount, no storage cost, no cache management)."
        ),
    )
    parser.add_argument("--no-cache", action="store_true",
                        help="Deprecated: same as --cache-mode implicit.")
    parser.add_argument("--delete-cache", action="store_true", help="Delete the Gemini cache after this run.")
    parser.add_argument(
        "--thinking-level",
        choices=["low", "medium", "high", "minimal"],
        default=DEFAULT_THINKING_LEVEL,
        help="Gemini 3 thinking effort. Defaults to 'minimal' for the lowest reasoning setting.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    start_time = time.perf_counter()
    final_df = asyncio.run(run_batch(parse_args()))
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

    end_time = time.perf_counter()
    print(f"Total time: {end_time - start_time:.2f} seconds")


    
