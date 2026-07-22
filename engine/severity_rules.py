"""
severity_rules.py

Session-level SEVERITY classification from the "Severity Criteria" rulebook.
Shared by the DB recompute paths (store/db.py, store/audio_db.py) and the
scripts/backfill_severity.py exporter so all of them grade a session the same way.

A session is graded over its ACTIVE flags — for each flag we need only its
category and whether the ASTROLOGER sent it. See classify_severity for the rules.
"""
from __future__ import annotations

# Categories that, when entirely absent from a session, satisfy LOW rule L4.
HARMFUL_CATEGORIES = {
    "NSFW",
    "NSFW_EXPLICIT",
    "NSFW_GROOMING",
    "NSFW_APPEARANCE",
    "CSAM_RISK",
    "VIOLENCE",
    "ABUSIVE_LANGUAGE",
    "SELF_HARM",
}

# Confidence stored alongside the severity (replaces the old verdict-rules
# confidence for this column). Same 0-1 scale as before.
SEVERITY_CONFIDENCE = {
    "HIGH": 0.9,
    "MEDIUM": 0.6,
    "LOW": 0.3,
    "CLEAN": 0.0,
}


def classify_severity(flags: list[tuple[str, bool]]) -> tuple[str, str]:
    """Return ``(severity, rule)`` for one session.

    ``flags`` is a list of ``(category_code, is_astrologer)`` for the session's
    active flags. ``rule`` is the matched rule id (e.g. 'H1', 'L3', '-') and is
    returned for transparency/auditing.

    HIGH is checked first (it wins over LOW); then LOW; otherwise MEDIUM.
    A session with no active flags is CLEAN (nothing to grade).
    """
    total = len(flags)
    if total == 0:                                             # no active flags
        return "CLEAN", "no_flags"

    astro_count = sum(1 for _, is_astro in flags if is_astro)
    categories = {cat for cat, _ in flags}

    csam_astro = sum(1 for cat, a in flags if cat == "CSAM_RISK" and a)
    nsfwx_total = sum(1 for cat, _ in flags if cat == "NSFW_EXPLICIT")
    nsfwx_astro = sum(1 for cat, a in flags if cat == "NSFW_EXPLICIT" and a)

    # ---- HIGH (checked first; wins over LOW) ----
    if csam_astro >= 3:                                          # H1
        return "HIGH", "H1"
    if nsfwx_astro >= 5:                                         # H2
        return "HIGH", "H2"
    if (                                                        # H3
        nsfwx_total >= 1
        and any(cat != "NSFW_EXPLICIT" for cat, _ in flags)      # co-occurrence
        and total >= 20
        and nsfwx_astro >= 2
    ):
        return "HIGH", "H3"

    # ---- LOW ----
    if total >= 1 and astro_count == 0:                         # L1: user-only
        return "LOW", "L1"
    if total >= 10 and astro_count <= 3:                        # L2
        return "LOW", "L2"
    if total <= 5:                                             # L3
        return "LOW", "L3"
    if not (categories & HARMFUL_CATEGORIES):                   # L4
        return "LOW", "L4"

    # ---- MEDIUM (default bucket) ----
    return "MEDIUM", "-"
