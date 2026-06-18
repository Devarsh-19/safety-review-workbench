"""
verdict_rules.py

Shared flag-to-verdict rules for automated and manual review flags.

Flag model (post-refactor):
  source  = LLM | REGEX | MANUAL   — origin, set at creation, never changes
  status  = ACTIVE | CONFIRMED      — current state
  parent_flag_id = NULL on originals, points to original on amendments

Active flag resolution:
  If a flag has an amendment row (parent_flag_id set), the amendment is the
  active version. The original is retained as audit history only.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Any


# ---------------------------------------------------------------------------
# SEVERE single flags — any one of these alone → verdict = SEVERE
# ---------------------------------------------------------------------------
SEVERE_FLAGS = {
    # Sexual content
    "nsfw",
    "nsfw_explicit",
    "nsfw_grooming",
    "nsfw_appearance",
    # Child safety
    "csam_risk",
    # Financial & fraud
    "financial_solicitation",
    "identity_fraud",
    # Harmful content
    "abusive_language",
    "hate_speech",
    "fake_remedies",
    "unauthorized_medical_advice",
    "self_harm",
    "violence",
}

# ---------------------------------------------------------------------------
# FLAGGED single flags — any one → verdict = FLAGGED (unless escalated below)
# ---------------------------------------------------------------------------
FLAGGED_FLAGS = {
    "off_platform_solicitation",
    "re_engagement_solicitation",
    "personal_data_collection",
    "fear_manipulation",
    "competitor_promotion",
    "external_media_content",
    "other",
}

# ---------------------------------------------------------------------------
# FLAGGED combinations that escalate to SEVERE
# ---------------------------------------------------------------------------
SEVERE_COMBINATIONS = [
    {"off_platform_solicitation",  "personal_data_collection"},
    {"off_platform_solicitation",  "fear_manipulation"},
    {"personal_data_collection",   "fear_manipulation"},
    {"re_engagement_solicitation", "personal_data_collection"},
    {"re_engagement_solicitation", "fear_manipulation"},
    {"external_media_content",     "personal_data_collection"},
]

DB_VERDICT_MAP = {
    "severe":  "SEVERE",
    "flagged": "FLAGGED",
    "clean":   "CLEAN",
}

DB_CONFIDENCE_MAP = {
    "SEVERE":  0.9,
    "FLAGGED": 0.6,
    "CLEAN":   0.0,
}

# ---------------------------------------------------------------------------
# Engine output code → canonical policy flag (lowercase snake_case)
# Frontend sends UPPERCASE_SNAKE — normalize_flag() lowercases them so they
# hit SEVERE_FLAGS / FLAGGED_FLAGS directly without a map entry.
# ---------------------------------------------------------------------------
FLAG_CODE_MAP = {
    # LLM intent taxonomy (int_XX codes from classifier)
    "int_01": "nsfw",
    "int_02": "nsfw_explicit",
    "int_03": "personal_data_collection",
    "int_04": "nsfw_grooming",
    "int_05": "nsfw_appearance",
    "int_06": "off_platform_solicitation",
    "int_07": "abusive_language",
    "int_08": "hate_speech",
    "int_09": "nsfw",
    "int_10": "other",
    "int_11": "nsfw",
    "int_12": "other",

    # ConsultantAnalyser / ingestion flags
    "vulgar_language":           "abusive_language",
    "erotic_reading":            "nsfw_explicit",
    "reciprocated_flirt":        "nsfw",
    "personal_info_shared":      "personal_data_collection",
    "continued_after_violation": "other",

    # Legacy aliases
    "re_engagement":    "re_engagement_solicitation",
    "external_media":   "external_media_content",
    "csam":             "csam_risk",
    "financial":        "financial_solicitation",
    "medical_advice":   "unauthorized_medical_advice",
}


def normalize_flag(flag: str | None) -> str:
    """Normalize any flag code to lowercase snake_case."""
    if not flag:
        return ""
    return flag.strip().lower().replace("-", "_").replace(" ", "_")


def to_canonical_flag(flag: str | None) -> str:
    """
    Convert any engine/manual flag code to the canonical policy flag name.
    Unknown values pass through in normalized form so new flags work without
    code changes.
    """
    normalized = normalize_flag(flag)
    if not normalized:
        return ""
    return FLAG_CODE_MAP.get(normalized, normalized)


def get_final_verdict(flags: Iterable[str]) -> str:
    """
    Return the canonical final verdict: severe, flagged, or clean.
    flags: iterable of flag codes or canonical flag names.
    """
    canonical = {
        to_canonical_flag(f)
        for f in flags
        if f and str(f).strip()
    }

    if not canonical:
        return "clean"

    if canonical.intersection(SEVERE_FLAGS):
        return "severe"

    for combo in SEVERE_COMBINATIONS:
        if combo.issubset(canonical):
            return "severe"

    if canonical.intersection(FLAGGED_FLAGS):
        return "flagged"

    return "clean"


def get_db_verdict_for_flags(flags: Iterable[str]) -> str:
    """Return DB verdict vocabulary: SEVERE, FLAGGED, or CLEAN."""
    return DB_VERDICT_MAP[get_final_verdict(flags)]


def get_db_confidence_for_verdict(verdict: str) -> float:
    """Return a deterministic confidence score for a DB verdict."""
    return DB_CONFIDENCE_MAP.get(str(verdict).upper(), 0.0)


def get_active_flag_codes(flag_rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """
    Return the active category_codes from DB flag rows, respecting the new
    source/status/parent_flag_id model.

    Active flag resolution:
    - Rows with parent_flag_id set are amendments — they ARE the active version.
    - Rows with no parent_flag_id that have NO amendment child are active originals.
    - Rows with no parent_flag_id that DO have an amendment child are suppressed
      (the amendment takes over).

    This replaces the old DISMISSED detection_layer scan logic.
    """
    rows = list(flag_rows)

    # Find all flag_ids that have been amended (i.e. some row has parent_flag_id = that id)
    amended_parent_ids = {
        r.get("parent_flag_id")
        for r in rows
        if r.get("parent_flag_id") is not None
    }

    active_codes: list[str] = []
    for r in rows:
        parent_flag_id = r.get("parent_flag_id")
        flag_id        = r.get("flag_id")
        code           = str(r.get("category_code") or "")

        if not code:
            continue

        if parent_flag_id is not None:
            # This is an amendment — it is the active version; include it
            active_codes.append(code)
        elif flag_id not in amended_parent_ids:
            # Original with no amendment — it is the active version
            active_codes.append(code)
        # else: original that has been amended — skip

    return active_codes
