"""
Batch Gemini audio safety evaluation for sample_audio.csv.

Reads M3U8/audio links from sample_audio.csv, converts each recording to MP3,
uploads the MP3 to Gemini, evaluates it with prompts.py, and writes an enriched
results dataframe with flagged diarized segments, review flags, response, token,
duration, latency, and pricing details.

Example:
    python test_gemini_multi/gemini_audio_batch.py --limit 1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import pandas as pd
from google import genai
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

import prompts


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DEFAULT_INPUT_CSV = BASE_DIR / "sample_audio.csv"
DEFAULT_AUDIO_DIR = BASE_DIR / "audio_files"
DEFAULT_OUTPUT_CSV = BASE_DIR / "data" / "gemini_audio_results.csv"
DEFAULT_OUTPUT_JSONL = BASE_DIR / "data" / "gemini_audio_results.jsonl"
DEFAULT_RAW_JSON_DIR = BASE_DIR / "data" / "raw_json"
DEFAULT_MODEL_ID = "gemini-3-flash-preview"
DEFAULT_THINKING_LEVEL = "minimal"
DEFAULT_MAX_OUTPUT_TOKENS = 65536
LONG_PAUSE_THRESHOLD_SECONDS = 60.0
SILENCE_NOISE_THRESHOLD = "-45dB"
OUTPUT_DROP_COLUMNS = {
    "month",
    "processed_at_utc",
    "audio_file_path",
    "audio_duration_seconds",
    "gemini_s_id",
    "pause_count",
    "pauses_json",
    "pauses_timestamp_valid",
    "pauses_timestamp_error_count",
    "pauses_max_ts_end",
    "invalid_pauses_json",
    "segments_json",
    "segments_timestamp_valid",
    "segments_timestamp_error_count",
    "segments_max_ts_end",
    "invalid_segments_json",
    "flags_timestamp_valid",
    "flags_max_ts_end",
    "invalid_flags_json",
    "raw_json_path",
    "candidate_output_tokens",
    "max_output_tokens",
    "model_id",
    "thinking_level",
}


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

INTENT_SEVERITY: dict[str, Literal["RED", "AMBER"]] = {
    "NSFW": "RED",
    "NSFW_EXPLICIT": "RED",
    "NSFW_GROOMING": "RED",
    "NSFW_APPEARANCE": "AMBER",
    "CSAM_RISK": "RED",
    "FINANCIAL_SOLICITATION": "RED",
    "IDENTITY_FRAUD": "RED",
    "ABUSIVE_LANGUAGE": "RED",
    "HATE_SPEECH": "RED",
    "FAKE_REMEDIES": "RED",
    "UNAUTHORIZED_MEDICAL_ADVICE": "RED",
    "SELF_HARM": "RED",
    "VIOLENCE": "RED",
    "INSTIGATION": "RED",
    "OFF_PLATFORM_SOLICITATION": "AMBER",
    "PERSONAL_DATA_COLLECTION": "AMBER",
    "FEAR_MANIPULATION": "AMBER",
    "COMPETITOR_PROMOTION": "AMBER",
}

MIN_CLEAR_SINGLE_TOKEN_CHARS = 4
CONTEXT_REQUIRED_SINGLE_TOKENS = {"bc", "mc", "w", "g", "chu"}
REQUEST_ACTION_TOKENS = {
    "abhi",
    "aao",
    "batao",
    "bhej",
    "bhejna",
    "bhejiye",
    "bhejo",
    "call",
    "check",
    "contact",
    "de",
    "dekho",
    "dekhiye",
    "dena",
    "dijiye",
    "dikhao",
    "dm",
    "donate",
    "do",
    "kar",
    "karna",
    "karwa",
    "karwao",
    "karwaunga",
    "karo",
    "kijiye",
    "jaao",
    "jao",
    "likho",
    "message",
    "milega",
    "milegi",
    "msg",
    "open",
    "pay",
    "scan",
    "search",
    "send",
    "share",
    "transfer",
    "try",
    "visit",
}
OFF_PLATFORM_TOKENS = {"call", "dm", "phone", "telegram", "whatsapp", "w"}
OFF_PLATFORM_REQUEST_TOKENS = (REQUEST_ACTION_TOKENS - {"call"}) | {"me", "unblock", "kyu", "kyun", "band", "chal", "chalu", "nahi", "nhi"}
OFF_PLATFORM_SAFE_MENTION_TOKENS = {"baad", "doston", "friend", "karta", "karunga", "karungi", "later"}
PAYMENT_REQUEST_TOKENS = {
    "bhej",
    "bhejna",
    "bhejo",
    "de",
    "dena",
    "dijiye",
    "do",
    "donate",
    "kar",
    "karna",
    "karo",
    "kijiye",
    "pay",
    "scan",
    "send",
    "transfer",
}
PAYMENT_TOKENS = {
    "account",
    "bank",
    "dakshina",
    "donate",
    "gpay",
    "money",
    "paise",
    "paisa",
    "paytm",
    "phonepe",
    "qr",
    "rs",
    "rupees",
    "upi",
}
EXTERNAL_PAYMENT_TOKENS = {"account", "bank", "dakshina", "donate", "gpay", "paytm", "phonepe", "qr", "upi"}
OFFICIAL_PAYMENT_SAFE_TOKENS = {"app", "official", "platform", "recharge"}
GENERIC_COST_TOKENS = {"cost", "ka", "lagta", "lagte", "market", "mein", "milega", "milegi", "price"}
PERSONAL_DATA_TOKENS = {"aadhaar", "account", "bank", "contact", "mobile", "number", "otp", "password", "phone", "upi"}
PERSONAL_DATA_REQUEST_TOKENS = {
    "batao",
    "bhej",
    "bhejna",
    "bhejiye",
    "bhejo",
    "de",
    "dena",
    "dijiye",
    "do",
    "likho",
    "send",
    "share",
}
APPEARANCE_TOKENS = {"body", "figure", "look", "looks", "photo", "pic", "picture", "selfie", "shape", "size", "video"}
APPEARANCE_REQUEST_TOKENS = REQUEST_ACTION_TOKENS | {"dikhti", "dikhta", "dikhte", "how", "kaisa", "kaisi", "what"}
ASTROLOGY_MEDIA_SAFE_TOKENS = {"birth", "chart", "face", "haath", "hand", "kundli", "palm", "reading"}
BODY_APPEARANCE_TOKENS = {"body", "figure", "look", "looks", "selfie", "shape", "size"}
GUARANTEE_TOKENS = {"100", "black", "blackmagic", "guarantee", "guaranteed", "magic", "pakka", "totka"}
TIMELINE_TOKENS = {"din", "day", "days", "ghante", "hour", "hours", "minute", "minutes"}
REMEDY_OUTCOME_TOKENS = {
    "fix",
    "fixed",
    "hoga",
    "jaayega",
    "jayega",
    "puja",
    "remedy",
    "result",
    "shaadi",
    "theek",
    "upay",
}
BLACK_MAGIC_TOKENS = {"blackmagic"}
BLACK_MAGIC_PAIR_TOKENS = ({"black", "kala"}, {"magic", "jadu", "jaadu"})
FEAR_TOKENS = {"accident", "barbad", "catastrophe", "death", "dikkat", "khatra", "mar", "marenge", "nuksan", "problem"}
STRONG_FEAR_TOKENS = {"accident", "barbad", "catastrophe", "death", "khatra", "mar", "marenge", "nuksan"}
FEAR_PRESSURE_TOKENS = {"abhi", "agar", "karwao", "nahi", "nahin", "paise", "puja", "rs", "rupees", "turant", "upay", "warna"}
MEDICAL_TOKENS = {
    "bimari",
    "bimaar",
    "blood",
    "bp",
    "cancer",
    "clinic",
    "dard",
    "dava",
    "dawai",
    "diabetes",
    "doctor",
    "health",
    "hospital",
    "ill",
    "medicine",
    "mri",
    "operation",
    "pain",
    "report",
    "sick",
    "surgery",
    "tablet",
    "test",
    "thyroid",
    "ultrasound",
    "xray",
}
MEDICAL_UNSAFE_TOKENS = {"avoid", "band", "chhod", "chhodo", "diagnose", "diagnosed", "mat", "nahi", "nahin", "stop"}
MEDICAL_DIAGNOSIS_TOKENS = {"bp", "cancer", "diabetes", "thyroid"}
MEDICAL_SAFE_TOKENS = {"clinic", "consult", "doctor", "hospital", "professional"}
SOFT_INSULT_TOKENS = {"bewakoof", "gadha", "gadhi", "pagal"}
HOSTILE_DIRECT_TOKENS = {"aap", "bilkul", "chup", "shut", "stupid", "tera", "tere", "teri", "tu", "tum"}
PLAYFUL_CONTEXT_TOKENS = {"arre", "arey", "haha", "hahaha", "yaar"}
HATE_GROUP_TOKENS = {
    "caste",
    "christian",
    "christians",
    "dalit",
    "gender",
    "hindu",
    "hindus",
    "muslim",
    "muslims",
    "religion",
    "women",
}
HATE_HOSTILE_TOKENS = {"bad", "bura", "ganda", "gandi", "hate", "kharab", "kharaab", "lower", "neech", "weak"}
HATE_BELITTLING_TOKENS = {"aukat", "bhikhari", "gareeb", "garib", "salman", "shakal"}
VIOLENCE_TOKENS = {
    "attack",
    "harm",
    "kill",
    "maar",
    "maaro",
    "marunga",
    "tod",
    "todu",
    "threaten",
}
VIOLENCE_LOCATION_TOKENS = {"ghar"}
VIOLENCE_APPROACH_TOKENS = {"aa", "aake", "aakar", "aaunga"}
SELF_HARM_STRONG_TOKENS = {"marna", "suicide"}
SELF_HARM_CONTEXT_TOKENS = {"end", "jaan", "khud", "life"}
INSTIGATION_TOKENS = {"complaint", "fight", "jhagda", "ladai", "police", "sabak", "threaten"}
FRAUD_CREDENTIAL_TOKENS = {"aadhaar", "otp", "password"}
FRAUD_AUTHORITY_TOKENS = {"income", "officer", "police", "tax"}
FRAUD_IMPERSONATION_TOKENS = {"adhikari", "hoon", "main", "officer"}
STRONG_COMPETITOR_TOKENS = {"astrologer", "channel", "guru", "instagram", "website", "youtube"}
GENERIC_COMPETITOR_TOKENS = {"app", "platform"}
COMPETITOR_SAFE_TOKENS = {"astrotalk", "official", "recharge"}
AMBIGUOUS_NSFW_SINGLE_TOKENS = {"bite", "lick", "suck"}
UNCLEAR_EVIDENCE_TOKENS = {"indistinct", "inaudible", "muffled", "unclear", "unintelligible"}
DESCRIPTIVE_EVIDENCE_TOKENS = {
    "abuse",
    "abusive",
    "background",
    "noise",
    "nsfw",
    "profane",
    "profanity",
    "sexual",
    "slang",
    "threat",
    "threatening",
    "vulgar",
    "vulgarity",
}
HALLUCINATED_TRANSCRIPT_DESCRIPTIONS = {
    "abuse",
    "abusive",
    "profanity",
    "swear",
    "swear word",
    "swearing",
    "unclear",
    "unclear profanity",
    "vulgar",
    "vulgarity",
}

CLEAR_ABUSIVE_SINGLE_WORDS = {
    "bsdk",
    "chutiya",
    "chut",
    "saali",
    "saala",
    "kamina",
    "kamini",
    "panauti",
    "manhus",
    "thevdiya",
    "thevidiya",
    "thevadiya",
    "otha",
    "soothu",
    "soothadi",
    "punda",
    "pundamavan",
    "pundek",
    "koothi",
    "loosu",
    "naaye",
    "naay",
    "sunni",
    "okka",
    "ombhu",
    "lanja",
    "lanjodaka",
    "lanjodaki",
    "dengey",
    "dengu",
    "dengina",
    "pukumunda",
    "gudda",
    "kukka",
    "kukkanayyala",
    "donga",
    "sulle",
    "zavla",
    "zhavla",
    "zhav",
    "bhadvya",
    "gandya",
    "chinal",
    "bhikarchot",
    "haramkhor",
    "pencho",
    "penchod",
    "kutti",
    "kuttiya",
    "ghudchad",
    "khassi",
    "chudail",
    "banchod",
    "banchot",
    "magi",
    "shala",
    "shali",
    "bokachoda",
    "sule",
    "sulemaga",
    "naayi",
    "mundedi",
    "thayoli",
    "kunna",
    "kundan",
    "myre",
    "myru",
    "poorr",
    "poorimol",
    "thendi",
    "patti",
    "ghelo",
    "gheli",
    "gando",
    "gandi",
    "chodyu",
    "bhosdi",
}

SPEAKER_LABEL_RE = re.compile(r"^Speaker [1-9]\d*$")
SPEAKER_LABEL_INPUT_RE = re.compile(r"^speaker\s*0*([1-9]\d*)$", re.IGNORECASE)
HHMMSS_RE = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)$")


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


def seconds_to_hhmmss(value: float | int | str) -> str:
    total_seconds = max(0, int(round(float(value))))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def timestamp_to_seconds(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass

    match = HHMMSS_RE.fullmatch(text)
    if not match:
        parts = re.split(r"[:\s-]+", text)
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            hours, minutes, seconds = (int(part) for part in parts)
            if 0 <= minutes <= 59 and 0 <= seconds <= 59:
                return float((hours * 3600) + (minutes * 60) + seconds)
    if not match:
        raise ValueError("timestamp must be seconds or HH:MM:SS")

    hours, minutes, seconds = (int(part) for part in match.groups())
    return float((hours * 3600) + (minutes * 60) + seconds)


def normalize_timestamp(value: Any) -> str:
    return seconds_to_hhmmss(timestamp_to_seconds(value))


class TimestampedModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ts_start: str = Field(description="Start timestamp in HH:MM:SS.")
    ts_end: str = Field(description="End timestamp in HH:MM:SS.")

    @field_validator("ts_start", "ts_end", mode="before")
    @classmethod
    def validate_timestamp_format(cls, value: Any) -> str:
        return normalize_timestamp(value)

    @model_validator(mode="after")
    def validate_timestamp_order(self) -> "TimestampedModel":
        if timestamp_to_seconds(self.ts_end) < timestamp_to_seconds(self.ts_start):
            raise ValueError("ts_end must be greater than or equal to ts_start")
        return self


class LongPause(TimestampedModel):
    duration_seconds: float = Field(
        alias="duration",
        ge=0.0,
        description="Length of the no-speech/silence span in seconds.",
    )


class SegmentFlag(BaseModel):
    intent: IntentId = Field(description="The matching Intent ID string from the taxonomy.")
    s: Literal["RED", "AMBER"] = Field(description="Severity label.")
    conf: float = Field(ge=0.0, le=1.0, description="Confidence rating from 0.0 to 1.0.")
    transcript: str = Field(
        description="Short exact words or ambient event that triggered the flag; not a full transcript."
    )


class DiarizedSegment(TimestampedModel):
    segment_id: int = Field(
        alias="seg_id",
        ge=1,
        description="Sequential flagged segment identifier within this audio.",
    )
    speaker: str = Field(description="Stable numbered speaker label such as Speaker 1, Speaker 2, etc.")
    tone: ToneLabel = Field(description="Dominant tone heard in this flagged segment.")
    flags: list[SegmentFlag] = Field(
        default_factory=list,
        description="One or more policy flags found inside this timestamped segment.",
    )

    @field_validator("speaker")
    @classmethod
    def validate_speaker(cls, value: str) -> str:
        return normalize_speaker_label(value)


class AstroTalkAudioReport(BaseModel):
    s_id: str = Field(description="The identifier of the processed session.")
    lang: str = Field(description="Auto-detected language(s).")
    review: bool = Field(description="True when any pause/no-speech span is at least 60 seconds.")
    pauses: list[LongPause] = Field(description="All pause/no-speech spans of 60 seconds or more.")
    segments: list[DiarizedSegment] = Field(
        description="Only flagged diarized segments, each with embedded flag evidence."
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_top_level_flags(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value

        migrated = dict(value)
        if "pauses" not in migrated and "long_pauses" in migrated:
            migrated["pauses"] = migrated.pop("long_pauses")

        normalized_pauses = []
        for pause in migrated.get("pauses") or []:
            if isinstance(pause, dict) and "duration" not in pause and "duration_seconds" in pause:
                pause = {**pause, "duration": pause.get("duration_seconds")}
            normalized_pauses.append(pause)
        migrated["pauses"] = normalized_pauses

        normalized_segments = []
        for segment in migrated.get("segments") or []:
            if not isinstance(segment, dict):
                normalized_segments.append(segment)
                continue
            normalized_segment = dict(segment)
            if "seg_id" not in normalized_segment and "segment_id" in normalized_segment:
                normalized_segment["seg_id"] = normalized_segment.get("segment_id")
            normalized_flags = []
            for flag in normalized_segment.get("flags") or []:
                if isinstance(flag, dict) and "transcript" not in flag and "transcript_excerpt" in flag:
                    flag = {**flag, "transcript": flag.get("transcript_excerpt", "")}
                normalized_flags.append(flag)
            normalized_segment["flags"] = normalized_flags
            normalized_segments.append(normalized_segment)
        migrated["segments"] = normalized_segments

        value = migrated
        legacy_flags = value.get("flags") or []
        if not legacy_flags:
            return value

        existing_segments = value.get("segments") or []
        if any(isinstance(segment, dict) and segment.get("flags") for segment in existing_segments):
            migrated = dict(value)
            migrated.pop("flags", None)
            return migrated

        segment_lookup = {
            segment.get("seg_id") or segment.get("segment_id"): segment
            for segment in existing_segments
            if isinstance(segment, dict) and (segment.get("seg_id") is not None or segment.get("segment_id") is not None)
        }
        grouped_segments: dict[Any, dict[str, Any]] = {}

        for index, flag in enumerate(legacy_flags):
            if not isinstance(flag, dict):
                continue

            segment_id = flag.get("seg_id") or flag.get("segment_id")
            base_segment = segment_lookup.get(segment_id, {}) if segment_id is not None else {}
            key = segment_id if segment_id is not None else ("flag", index)
            if key not in grouped_segments:
                grouped_segments[key] = {
                    "seg_id": segment_id or len(grouped_segments) + 1,
                    "speaker": flag.get("speaker") or base_segment.get("speaker") or "Speaker 1",
                    "ts_start": base_segment.get("ts_start", flag.get("ts_start", 0.0)),
                    "ts_end": base_segment.get("ts_end", flag.get("ts_end", flag.get("ts_start", 0.0))),
                    "tone": flag.get("tone") or base_segment.get("tone") or "UNCLEAR",
                    "flags": [],
                }
            grouped_segments[key]["flags"].append(
                {
                    "intent": flag.get("intent"),
                    "s": flag.get("s"),
                    "conf": flag.get("conf"),
                    "transcript": flag.get("transcript") or flag.get("transcript_excerpt", ""),
                }
            )

        migrated = dict(value)
        migrated["segments"] = list(grouped_segments.values())
        migrated.pop("flags", None)
        return migrated


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


def probe_has_video(source: str) -> bool:
    """Return True if the source URL/file contains at least one video stream."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        source,
    ]
    result = _run_subprocess(cmd)
    if result.returncode != 0:
        # If ffprobe fails (e.g. network issue), log and assume no video.
        print(f"[WARN] ffprobe video check failed for {source}: {result.stderr.strip()}")
        return False
    return "video" in result.stdout.lower()


def detect_pauses(
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


def pauses_to_json(pauses: list[LongPause]) -> str:
    return json.dumps([pause.model_dump(by_alias=True) for pause in pauses], ensure_ascii=False)


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
        abs(timestamp_to_seconds(a.ts_start) - timestamp_to_seconds(b.ts_start)) <= tolerance_seconds
        and abs(timestamp_to_seconds(a.ts_end) - timestamp_to_seconds(b.ts_end)) <= tolerance_seconds
    )


def is_review_pause(pause: LongPause, threshold_seconds: float = LONG_PAUSE_THRESHOLD_SECONDS) -> bool:
    timestamp_duration = max(0.0, timestamp_to_seconds(pause.ts_end) - timestamp_to_seconds(pause.ts_start))
    return timestamp_duration + 0.001 >= threshold_seconds


def normalize_long_pause_duration(pause: LongPause) -> LongPause:
    timestamp_duration = round(max(0.0, timestamp_to_seconds(pause.ts_end) - timestamp_to_seconds(pause.ts_start)), 3)
    return pause.model_copy(update={"duration_seconds": timestamp_duration})


def transcript_tokens(transcript: str) -> list[str]:
    return [token.strip("_") for token in re.findall(r"\w+", transcript.lower(), flags=re.UNICODE) if token.strip("_")]


def has_any_token(tokens: list[str], expected: set[str]) -> bool:
    return any(token in expected for token in tokens)


def has_token_from_each(tokens: list[str], left: set[str], right: set[str]) -> bool:
    token_values = set(tokens)
    return bool(token_values & left) and bool(token_values & right)


def has_context_request(tokens: list[str]) -> bool:
    return has_any_token(tokens, REQUEST_ACTION_TOKENS)


def has_intent_specific_evidence(flag: SegmentFlag, tokens: list[str]) -> bool:
    token_values = set(tokens)

    if flag.intent in {"NSFW", "NSFW_EXPLICIT"}:
        return not (len(tokens) == 1 and tokens[0] in AMBIGUOUS_NSFW_SINGLE_TOKENS)

    if flag.intent == "ABUSIVE_LANGUAGE":
        if has_any_token(tokens, CLEAR_ABUSIVE_SINGLE_WORDS):
            return True
        if has_any_token(tokens, SOFT_INSULT_TOKENS):
            if has_any_token(tokens, PLAYFUL_CONTEXT_TOKENS) and not has_any_token(tokens, HOSTILE_DIRECT_TOKENS):
                return False
            return has_any_token(tokens, HOSTILE_DIRECT_TOKENS)
        return True

    if flag.intent == "OFF_PLATFORM_SOLICITATION":
        if has_any_token(tokens, OFF_PLATFORM_SAFE_MENTION_TOKENS) and not has_any_token(
            tokens, {"aao", "dm", "karo", "message", "msg", "send", "telegram", "whatsapp"}
        ):
            return False
        return has_any_token(tokens, OFF_PLATFORM_TOKENS) and has_any_token(tokens, OFF_PLATFORM_REQUEST_TOKENS)

    if flag.intent == "FINANCIAL_SOLICITATION":
        if {"app", "recharge"} <= token_values or {"platform", "recharge"} <= token_values:
            return False
        if has_any_token(tokens, OFFICIAL_PAYMENT_SAFE_TOKENS) and not has_any_token(tokens, EXTERNAL_PAYMENT_TOKENS):
            return False
        if has_any_token(tokens, GENERIC_COST_TOKENS) and not has_any_token(tokens, EXTERNAL_PAYMENT_TOKENS):
            return False
        return has_any_token(tokens, PAYMENT_TOKENS) and has_any_token(tokens, PAYMENT_REQUEST_TOKENS)

    if flag.intent == "PERSONAL_DATA_COLLECTION":
        return has_any_token(tokens, PERSONAL_DATA_TOKENS) and has_any_token(tokens, PERSONAL_DATA_REQUEST_TOKENS)

    if flag.intent == "NSFW_APPEARANCE":
        if has_any_token(tokens, ASTROLOGY_MEDIA_SAFE_TOKENS) and not has_any_token(tokens, BODY_APPEARANCE_TOKENS):
            return False
        return has_any_token(tokens, APPEARANCE_TOKENS) and has_any_token(tokens, APPEARANCE_REQUEST_TOKENS)

    if flag.intent == "FAKE_REMEDIES":
        if has_any_token(tokens, BLACK_MAGIC_TOKENS) or has_token_from_each(tokens, *BLACK_MAGIC_PAIR_TOKENS):
            return True
        if has_any_token(tokens, {"100", "guarantee", "guaranteed"}) and len(tokens) >= 2:
            return True
        if "pakka" in token_values and has_any_token(tokens, REMEDY_OUTCOME_TOKENS):
            return True
        return has_any_token(tokens, TIMELINE_TOKENS) and has_any_token(tokens, REMEDY_OUTCOME_TOKENS)

    if flag.intent == "FEAR_MANIPULATION":
        if has_any_token(tokens, STRONG_FEAR_TOKENS):
            return has_any_token(tokens, FEAR_PRESSURE_TOKENS)
        return has_any_token(tokens, FEAR_TOKENS) and has_any_token(tokens, FEAR_PRESSURE_TOKENS)

    if flag.intent == "UNAUTHORIZED_MEDICAL_ADVICE":
        if not has_any_token(tokens, MEDICAL_TOKENS):
            return False
        if has_any_token(tokens, MEDICAL_UNSAFE_TOKENS):
            return True
        return has_any_token(tokens, MEDICAL_DIAGNOSIS_TOKENS) and not has_any_token(tokens, MEDICAL_SAFE_TOKENS)

    if flag.intent == "HATE_SPEECH":
        if has_any_token(tokens, HATE_GROUP_TOKENS):
            return has_any_token(tokens, HATE_HOSTILE_TOKENS) or len(tokens) >= 3
        if has_any_token(tokens, HATE_BELITTLING_TOKENS):
            return has_any_token(tokens, HOSTILE_DIRECT_TOKENS) or len(tokens) >= 3
        return False

    if flag.intent == "VIOLENCE":
        return has_any_token(tokens, VIOLENCE_TOKENS) or has_token_from_each(
            tokens, VIOLENCE_LOCATION_TOKENS, VIOLENCE_APPROACH_TOKENS
        )

    if flag.intent == "SELF_HARM":
        return has_any_token(tokens, SELF_HARM_STRONG_TOKENS) or has_token_from_each(
            tokens, SELF_HARM_CONTEXT_TOKENS, {"end", "marna"}
        )

    if flag.intent == "INSTIGATION":
        return has_any_token(tokens, INSTIGATION_TOKENS) and has_context_request(tokens)

    if flag.intent == "IDENTITY_FRAUD":
        if has_any_token(tokens, FRAUD_CREDENTIAL_TOKENS):
            return has_any_token(tokens, PERSONAL_DATA_REQUEST_TOKENS)
        return has_any_token(tokens, FRAUD_AUTHORITY_TOKENS) and has_any_token(tokens, FRAUD_IMPERSONATION_TOKENS)

    if flag.intent == "COMPETITOR_PROMOTION":
        if has_any_token(tokens, COMPETITOR_SAFE_TOKENS):
            return False
        if has_any_token(tokens, STRONG_COMPETITOR_TOKENS):
            return has_context_request(tokens)
        return has_any_token(tokens, GENERIC_COMPETITOR_TOKENS) and has_any_token(tokens, {"external", "outside"})

    return True


def has_enough_evidence(flag: SegmentFlag) -> bool:
    tokens = transcript_tokens(flag.transcript)
    if not tokens:
        return False

    if has_any_token(tokens, UNCLEAR_EVIDENCE_TOKENS):
        return False

    if has_any_token(tokens, DESCRIPTIVE_EVIDENCE_TOKENS):
        concrete_tokens = set(tokens) - DESCRIPTIVE_EVIDENCE_TOKENS
        if not concrete_tokens or flag.intent == "ABUSIVE_LANGUAGE":
            return False

    if len(tokens) == 1:
        token = tokens[0]
        if token.isascii() and (len(token) < MIN_CLEAR_SINGLE_TOKEN_CHARS or token in CONTEXT_REQUIRED_SINGLE_TOKENS):
            return False
        if flag.intent == "ABUSIVE_LANGUAGE" and token.isascii() and token not in CLEAR_ABUSIVE_SINGLE_WORDS:
            return False
        if flag.intent == "OFF_PLATFORM_SOLICITATION" and token in {"w", "whatsapp", "call", "number"}:
            return False
        if flag.intent == "FINANCIAL_SOLICITATION" and token in {"pay", "rupees", "upi", "money", "donate"}:
            return False
        if flag.intent in {"NSFW", "NSFW_EXPLICIT"} and token in AMBIGUOUS_NSFW_SINGLE_TOKENS:
            return False

    return has_intent_specific_evidence(flag, tokens)


def normalize_segment_flag(flag: SegmentFlag) -> SegmentFlag | None:
    if not has_enough_evidence(flag):
        return None

    threshold = 0.2 if flag.intent == "CSAM_RISK" else 0.5

    complex_intents = {"FEAR_MANIPULATION", "NSFW_GROOMING", "INSTIGATION", "IDENTITY_FRAUD"}
    if flag.intent in complex_intents:
        tokens = transcript_tokens(flag.transcript)
        if len(tokens) <= 2:
            threshold = 0.7

    if float(flag.conf) + 0.001 < threshold:
        return None
    expected_severity = INTENT_SEVERITY.get(flag.intent)
    if expected_severity and flag.s != expected_severity:
        return flag.model_copy(update={"s": expected_severity})
    return flag


def normalize_audio_report(
    report: AstroTalkAudioReport,
    session_id: str,
    local_pauses: list[LongPause],
) -> AstroTalkAudioReport:
    flagged_segments = []
    for segment in report.segments:
        normalized_flags = [
            normalized_flag
            for flag in segment.flags
            if (normalized_flag := normalize_segment_flag(flag)) is not None
        ]
        if normalized_flags:
            flagged_segments.append(segment.model_copy(update={"flags": normalized_flags}))

    merged_pauses = [
        normalize_long_pause_duration(pause)
        for pause in report.pauses
        if is_review_pause(pause)
    ]
    for local_pause in local_pauses:
        normalized_local_pause = normalize_long_pause_duration(local_pause)
        if not any(_same_pause(normalized_local_pause, existing_pause) for existing_pause in merged_pauses):
            merged_pauses.append(normalized_local_pause)
    merged_pauses.sort(key=lambda pause: (pause.ts_start, pause.ts_end))

    return report.model_copy(
        update={
            "s_id": str(session_id),
            "review": bool(merged_pauses),
            "pauses": merged_pauses,
            "segments": flagged_segments,
        }
    )


class GeminiResponseParseError(RuntimeError):
    """Raised when Gemini returns text that cannot be parsed into the report schema."""


def response_finish_reason(response: Any) -> str:
    try:
        finish_reason = response.candidates[0].finish_reason
    except (IndexError, AttributeError):
        return "UNKNOWN"
    return getattr(finish_reason, "name", str(finish_reason))


def response_output_token_count(response: Any) -> int | None:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None
    value = getattr(usage, "candidates_token_count", None)
    return int(value) if value is not None else None


def is_cache_unavailable_error(exc: Exception) -> bool:
    """Return True when Gemini says cached content is missing or inaccessible."""
    message = str(exc).lower()
    mentions_cache = any(token in message for token in ("cachedcontent", "cached content", "cached_content"))
    if not mentions_cache:
        return False
    return any(token in message for token in ("not found", "permission denied", "permission_denied", "403"))


def parse_audio_report(response: Any, session_id: str) -> AstroTalkAudioReport:
    report = getattr(response, "parsed", None)
    if report is None:
        response_text = getattr(response, "text", "") or ""
        try:
            report = AstroTalkAudioReport.model_validate_json(response_text)
        except ValidationError as exc:
            finish_reason = response_finish_reason(response)
            output_tokens = response_output_token_count(response)
            if finish_reason == "MAX_TOKENS" or "Invalid JSON" in str(exc):
                raise GeminiResponseParseError(
                    "Gemini returned incomplete or invalid JSON "
                    f"for session {session_id} "
                    f"(finish_reason={finish_reason}, "
                    f"output_tokens={output_tokens}, "
                    f"output_chars={len(response_text)}). "
                    "This usually means the JSON was cut off before completion. "
                    "Rerun with a higher --max-output-tokens value or keep the prompt output more compact."
                ) from exc
            raise
    elif not isinstance(report, AstroTalkAudioReport):
        report = AstroTalkAudioReport.model_validate(report)
    return report.model_copy(update={"s_id": str(session_id)})


THINKING_BUDGET_MAP: dict[str, int] = {
    "minimal": 0,
    "low": 1024,
    "medium": 8192,
    "high": 32768,
}


def evaluate_audio(
    client: genai.Client,
    model_id: str,
    audio_path: Path,
    session_id: str,
    audio_duration_seconds: float,
    local_pauses: list[LongPause],
    cache_name: str | None,
    thinking_level: str = "minimal",
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> tuple[AstroTalkAudioReport, dict[str, int | float], float, str]:
    uploaded_file = None
    try:
        uploaded_file = client.files.upload(file=str(audio_path))
        while uploaded_file.state.name == "PROCESSING":
            time.sleep(2)
            uploaded_file = client.files.get(name=uploaded_file.name)
        if uploaded_file.state.name == "FAILED":
            raise RuntimeError("Gemini file asset processing failed.")

        thinking_budget = THINKING_BUDGET_MAP.get(thinking_level, 0)
        config_kwargs = {
            "temperature": 0,
            "safety_settings": gemini_safety_settings(),
            "max_output_tokens": max_output_tokens,
            "response_mime_type": "application/json",
            "response_schema": AstroTalkAudioReport,
            "thinking_config": types.ThinkingConfig(thinking_budget=thinking_budget),
        }
        if cache_name:
            config_kwargs["cached_content"] = cache_name

        config = types.GenerateContentConfig(**config_kwargs)

        prompt = (
            f"audio_duration_hhmmss: {seconds_to_hhmmss(audio_duration_seconds)}\n"
            "All ts_start and ts_end values must be HH:MM:SS timestamps from the beginning "
            "of this audio and must be within audio_duration_hhmmss.\n"
            f"local_long_pause_candidates_json: {pauses_to_json(local_pauses)}\n"
            "If local_long_pause_candidates_json is not empty, include those spans in pauses "
            "and set review to true.\n\n"
            + prompts.USER_MESSAGE.replace("{s_id}", session_id)
        )
        start_time = time.perf_counter()
        response = client.models.generate_content(
            model=model_id,
            contents=[uploaded_file, prompt],
            config=config,
        )
        latency_seconds = round(time.perf_counter() - start_time, 3)

        # Warn if the response was truncated due to output token limit.
        try:
            finish_reason = response_finish_reason(response)
            if finish_reason == "MAX_TOKENS":
                print(
                    f"[WARN] session {session_id}: response truncated at "
                    f"max_output_tokens ({config_kwargs['max_output_tokens']})"
                )
        except AttributeError:
            pass

        report = normalize_audio_report(parse_audio_report(response, session_id), session_id, local_pauses)
        telemetry = usage_to_telemetry(response.usage_metadata) if response.usage_metadata else {}
        response_json = report.model_dump_json(by_alias=True)
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
            ts_start = timestamp_to_seconds(record.get("ts_start", -1))
            ts_end = timestamp_to_seconds(record.get("ts_end", -1))
        except (TypeError, ValueError):
            invalid_records.append(record)
            continue
        if not (0 <= ts_start <= ts_end <= audio_duration_seconds + 1.0):
            invalid_records.append(record)
    return invalid_records


def max_ts_end(records: list[dict[str, Any]]) -> float:
    ts_end_values = []
    for record in records:
        try:
            ts_end_values.append(timestamp_to_seconds(record.get("ts_end", 0)))
        except (TypeError, ValueError):
            continue
    return max(ts_end_values, default=0.0)


def elapsed_seconds(start: float, end: float) -> float:
    return round(max(0.0, end - start), 3)


def flatten_segment_flags(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    for segment in segments:
        for flag in segment.get("flags", []) or []:
            flag_record = dict(flag)
            seg_id = segment.get("seg_id", segment.get("segment_id"))
            flag_record.update(
                {
                    "seg_id": seg_id,
                    "speaker": segment.get("speaker"),
                    "tone": segment.get("tone"),
                    "ts_start": segment.get("ts_start"),
                    "ts_end": segment.get("ts_end"),
                }
            )
            flags.append(flag_record)
    return flags


def flatten_result(
    row: pd.Series,
    audio_duration_seconds: float | None,
    report: AstroTalkAudioReport | None,
    detected_pauses: list[LongPause] | None,
    telemetry: dict[str, int | float] | None,
    latency_seconds: float | None,
    response_json: str | None,
    status: str,
    error: str | None = None,
    has_video: bool = False,
) -> dict[str, Any]:
    segments = [segment.model_dump(by_alias=True) for segment in report.segments] if report else []
    flags = flatten_segment_flags(segments)
    if report:
        pauses = [pause.model_dump(by_alias=True) for pause in report.pauses]
        review = report.review or has_video
    else:
        pauses = [pause.model_dump(by_alias=True) for pause in detected_pauses or []]
        review = bool(pauses) or has_video

    invalid_flags = invalid_timestamp_records(flags, audio_duration_seconds)

    result = row.to_dict()
    result.update(
        {
            "status": status,
            "error": error or "",
            "detected_languages": report.lang if report else "",
            "review": review,
            "has_video": has_video,
            "long_pause_count": len(pauses),
            "segment_count": len(segments),
            "flag_count": len(flags),
            "flags_json": json.dumps(flags, ensure_ascii=False),
            "flags_timestamp_error_count": len(invalid_flags),
            "response_json": response_json or "",
            "latency_seconds": latency_seconds,
        }
    )
    result.update(telemetry or {})
    return result


def truthy_export_value(value: Any) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "0", "false", "no", "n", "none", "nan"}:
            return False
        if normalized in {"1", "true", "yes", "y"}:
            return True
    return bool(value)


def positive_export_count(value: Any) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def is_flagged_result(record: dict[str, Any]) -> bool:
    return (
        positive_export_count(record.get("flag_count"))
        or positive_export_count(record.get("long_pause_count"))
        or positive_export_count(record.get("pause_count"))
        or truthy_export_value(record.get("has_video"))
    )


def clean_export_value(value: Any) -> Any:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return value


def export_record(record: dict[str, Any]) -> dict[str, Any]:
    cleaned = {
        key: clean_export_value(value)
        for key, value in record.items()
        if key not in OUTPUT_DROP_COLUMNS and key != "flagged"
    }
    cleaned["flagged"] = is_flagged_result(record)
    return cleaned


def output_dataframe(results: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame([export_record(record) for record in results])
    if "flagged" in df:
        return df[[column for column in df.columns if column != "flagged"] + ["flagged"]]
    return df


def write_outputs(results: list[dict[str, Any]], output_csv: Path, output_jsonl: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    output_records = [export_record(record) for record in results]
    df = output_dataframe(results)
    df.to_csv(output_csv, index=False, encoding="utf-8-sig")
    with output_jsonl.open("w", encoding="utf-8") as handle:
        for record in output_records:
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")


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
    cache_cleanup_name: str | None = None
    cache_storage_cost_recorded = False

    try:
        if client and not args.no_cache:
            cache_info = create_gemini_cache(client, args.model_id, args.cache_ttl)
        cache_name = cache_info.name if cache_info else None
        cache_cleanup_name = cache_name

        for index, row in df.iterrows():
            session_id = str(row["session_id"])
            audio_stem = _safe_audio_stem(row)
            audio_path = audio_dir / f"{audio_stem}.mp3"

            if session_id in completed_sessions:
                print(f"[{index + 1}/{len(df)}] skip completed session {session_id}")
                continue

            print(f"[{index + 1}/{len(df)}] session {session_id}: converting/downloading audio")
            has_video = False

            t_start = time.perf_counter()
            t_video = t_start
            t_convert = t_start
            t_probe = t_start
            t_pauses = t_start
            t_gemini = t_start

            try:
                recording_url = str(row["recording_url"])
                has_video = probe_has_video(recording_url)
                t_video = time.perf_counter()

                if has_video:
                    print(
                        f"[{index + 1}/{len(df)}] session {session_id}: "
                        f"video stream detected - marking for review"
                    )
                convert_to_mp3(recording_url, audio_path, force=args.force_download)
                t_convert = time.perf_counter()

                duration_seconds = probe_audio_duration(audio_path)
                t_probe = time.perf_counter()

                local_pauses = detect_pauses(audio_path, duration_seconds)
                t_pauses = time.perf_counter()

                if local_pauses:
                    print(
                        f"[{index + 1}/{len(df)}] session {session_id}: "
                        f"{len(local_pauses)} long pause(s) need review"
                    )

                if args.skip_gemini:
                    record = flatten_result(
                        row,
                        duration_seconds,
                        report=None,
                        detected_pauses=local_pauses,
                        telemetry={},
                        latency_seconds=None,
                        response_json=None,
                        status="downloaded",
                        has_video=has_video,
                    )
                    t_gemini = time.perf_counter()
                else:
                    print(f"[{index + 1}/{len(df)}] session {session_id}: uploading to Gemini")
                    if client is None:
                        raise RuntimeError("Gemini client was not initialized.")
                    try:
                        report, telemetry, latency, response_json = evaluate_audio(
                            client,
                            args.model_id,
                            audio_path,
                            session_id,
                            duration_seconds,
                            local_pauses,
                            cache_name,
                            thinking_level=args.thinking_level or "minimal",
                            max_output_tokens=args.max_output_tokens,
                        )
                    except Exception as exc:
                        if cache_name and is_cache_unavailable_error(exc):
                            print(
                                f"[WARN] session {session_id}: cache unavailable ({exc}). "
                                "Retrying without cache and disabling cache for remaining sessions."
                            )
                            cache_name = None
                            cache_info = None
                            report, telemetry, latency, response_json = evaluate_audio(
                                client,
                                args.model_id,
                                audio_path,
                                session_id,
                                duration_seconds,
                                local_pauses,
                                cache_name=None,
                                thinking_level=args.thinking_level or "minimal",
                                max_output_tokens=args.max_output_tokens,
                            )
                        else:
                            raise
                    t_gemini = time.perf_counter()

                    write_raw_response(raw_json_dir, session_id, response_json)
                    record = flatten_result(
                        row,
                        duration_seconds,
                        report,
                        detected_pauses=local_pauses,
                        telemetry=telemetry,
                        latency_seconds=latency,
                        response_json=response_json,
                        status="success",
                        has_video=has_video,
                    )
                    cost = record.get("estimated_cost_usd", 0.0)
                    print(
                        f"[{index + 1}/{len(df)}] session {session_id}: "
                        f"{record['flag_count']} flags, ${float(cost):.6f}, LLM latency: {latency:.1f}s"
                    )
            except Exception as exc:
                t_gemini = time.perf_counter()
                record = flatten_result(
                    row,
                    audio_duration_seconds=None,
                    report=None,
                    detected_pauses=None,
                    telemetry={},
                    latency_seconds=None,
                    response_json=None,
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                    has_video=has_video,
                )
                print(f"[ERROR] session {session_id}: {record['error']}")

            t_end = time.perf_counter()
            step_timing = {
                "time_video_probe_s": elapsed_seconds(t_start, t_video),
                "time_convert_s": elapsed_seconds(t_video, t_convert),
                "time_duration_probe_s": elapsed_seconds(t_convert, t_probe),
                "time_pause_detect_s": elapsed_seconds(t_probe, t_pauses),
                "time_gemini_s": elapsed_seconds(t_pauses, t_gemini),
                "time_total_s": elapsed_seconds(t_start, t_end),
            }
            record.update(step_timing)

            print(
                f"[{index + 1}/{len(df)}] session {session_id} timing: "
                f"video={step_timing['time_video_probe_s']:.1f}s, "
                f"convert={step_timing['time_convert_s']:.1f}s, "
                f"probe={step_timing['time_duration_probe_s']:.1f}s, "
                f"pauses={step_timing['time_pause_detect_s']:.1f}s, "
                f"gemini={step_timing['time_gemini_s']:.1f}s, "
                f"total={step_timing['time_total_s']:.1f}s"
            )

            record["cache_name"] = cache_name or ""
            record["cache_storage_tokens"] = cache_info.token_count if cache_info else 0
            if cache_info and not cache_storage_cost_recorded:
                record["cache_storage_cost_usd"] = cache_info.storage_cost_usd
                cache_storage_cost_recorded = True
            else:
                record["cache_storage_cost_usd"] = 0.0
            results.append(record)
            write_outputs(results, output_csv, output_jsonl)
    finally:
        if client and args.delete_cache:
            delete_gemini_cache(client, cache_cleanup_name)
        elif client and cache_cleanup_name:
            print(f"[INFO] Cache retained until TTL expires: {cache_cleanup_name}")

    return output_dataframe(results)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download sample audio and evaluate it with Gemini.")
    parser.add_argument("--input-csv", default=str(DEFAULT_INPUT_CSV), help="CSV with session_id and recording_url.")
    parser.add_argument("--audio-dir", default=str(DEFAULT_AUDIO_DIR), help="Directory where MP3 files are saved.")
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV), help="Enriched result dataframe CSV.")
    parser.add_argument("--output-jsonl", default=str(DEFAULT_OUTPUT_JSONL), help="Line-delimited JSON results.")
    parser.add_argument("--raw-json-dir", default=str(DEFAULT_RAW_JSON_DIR), help="Directory for per-session raw JSON responses.")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID, help="Gemini model ID.")
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=DEFAULT_MAX_OUTPUT_TOKENS,
        help="Maximum Gemini output tokens for the structured JSON response.",
    )
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
        request_cost_series = pd.to_numeric(
            final_df.get("estimated_cost_usd", pd.Series(dtype=float)),
            errors="coerce",
        )
        request_cost = request_cost_series.fillna(0).sum()
        cache_storage_cost_series = pd.to_numeric(
            final_df.get("cache_storage_cost_usd", pd.Series(dtype=float)),
            errors="coerce",
        )
        cache_storage_cost = cache_storage_cost_series.fillna(0).sum()
        total_cost = request_cost + cache_storage_cost
        duration_series = pd.to_numeric(final_df.get("duration_sec", pd.Series(dtype=float)), errors="coerce")
        total_duration = duration_series.fillna(0).sum()
        print(f"Processed rows: {len(final_df)}")
        print(f"Total audio duration seconds: {total_duration:.3f}")
        print(f"Estimated Gemini request cost USD: {request_cost:.6f}")
        print(f"Estimated Gemini cache storage cost USD: {cache_storage_cost:.6f}")
        print(f"Estimated total Gemini cost USD: {total_cost:.6f}")
