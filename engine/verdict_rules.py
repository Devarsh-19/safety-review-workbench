"""
Shared flag-to-verdict rules for automated and manual review flags.

The canonical flag names in this module are the policy-level vocabulary.
Existing engine codes such as INT-06 or VULGAR_LANGUAGE are mapped into
that vocabulary before the final verdict is computed.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Any


SEVERE_FLAGS = {
    "abusive_language",
    "financial_solicitation",
    "hate_speech",
    "identity_fraud",
    "nsfw",
    "fake_remedies",
    "unauthorized_medical_advice",
}

FLAGGED_FLAGS = {
    "off_platform_solicitation",
    "personal_data_collection",
    "fear_manipulation",
    "competitor_promotion",
    "other",
}

SEVERE_COMBINATIONS = [
    {"off_platform_solicitation", "personal_data_collection"},
    {"off_platform_solicitation", "fear_manipulation"},
    {"personal_data_collection", "fear_manipulation"},
]

DB_VERDICT_MAP = {
    "severe": "SEVERE",
    "flagged": "FLAGGED",
    "clean": "CLEAN",
}

DB_CONFIDENCE_MAP = {
    "SEVERE": 0.9,
    "FLAGGED": 0.6,
    "CLEAN": 0.0,
}


# Engine output category_code -> canonical policy flag.
FLAG_CODE_MAP = {
    # LLM intent taxonomy.
    "int_01": "nsfw",
    "int_02": "nsfw",
    "int_03": "personal_data_collection",
    "int_04": "nsfw",
    "int_05": "nsfw",
    "int_06": "off_platform_solicitation",
    "int_07": "abusive_language",
    "int_08": "nsfw",
    "int_09": "nsfw",
    "int_10": "other",
    "int_11": "nsfw",
    "int_12": "other",

    # ConsultantAnalyser / ingestion flags.
    "vulgar_language": "abusive_language",
    "erotic_reading": "nsfw",
    "reciprocated_flirt": "nsfw",
    "personal_info_shared": "personal_data_collection",
    "continued_after_violation": "other",
    "re_engagement_solicitation": "off_platform_solicitation",
    "external_media_content": "other",
}


def normalize_flag(flag: str | None) -> str:
    """Normalize user/engine flag text to lowercase snake_case."""
    if not flag:
        return ""
    normalized = flag.strip().lower()
    normalized = normalized.replace("-", "_").replace(" ", "_")
    return normalized


def to_canonical_flag(flag: str | None) -> str:
    """
    Convert any known engine/manual flag code to the canonical policy flag.
    Unknown values are returned in normalized form so future canonical flags
    can be accepted without code changes.
    """
    normalized = normalize_flag(flag)
    if not normalized:
        return ""
    return FLAG_CODE_MAP.get(normalized, normalized)


def get_final_verdict(flags: Iterable[str]) -> str:
    """
    Return the canonical final verdict: severe, flagged, or clean.

    Parameters
    ----------
    flags:
        Iterable of detected flag codes or canonical flag names.
    """
    canonical_flags = {
        to_canonical_flag(flag)
        for flag in flags
        if flag and str(flag).strip()
    }

    if not canonical_flags:
        return "clean"

    if canonical_flags.intersection(SEVERE_FLAGS):
        return "severe"

    for combination in SEVERE_COMBINATIONS:
        if combination.issubset(canonical_flags):
            return "severe"

    if canonical_flags.intersection(FLAGGED_FLAGS):
        return "flagged"

    return "clean"


def get_db_verdict_for_flags(flags: Iterable[str]) -> str:
    """Return DB verdict vocabulary: SEVERE, FLAGGED, or CLEAN."""
    return DB_VERDICT_MAP[get_final_verdict(flags)]


def get_db_confidence_for_verdict(verdict: str) -> float:
    """Return a deterministic score for a DB verdict."""
    return DB_CONFIDENCE_MAP.get(verdict.upper(), 0.0)


def get_active_flag_codes(flag_rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """
    Return active category codes from DB flag rows.

    DISMISSED rows suppress flags with the same normalized category_code,
    because the original LLM/REGEX/MANUAL flag remains in the table as audit
    history.
    """
    rows = list(flag_rows)
    dismissed = {
        normalize_flag(str(row.get("category_code") or ""))
        for row in rows
        if str(row.get("detection_layer") or "").upper() == "DISMISSED"
    }

    active_codes: list[str] = []
    for row in rows:
        layer = str(row.get("detection_layer") or "").upper()
        code = str(row.get("category_code") or "")
        normalized = normalize_flag(code)
        if not normalized or layer == "DISMISSED" or normalized in dismissed:
            continue
        active_codes.append(code)
    return active_codes
