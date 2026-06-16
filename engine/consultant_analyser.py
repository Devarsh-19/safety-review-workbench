"""
consultant_analyser.py
Analyses CONSULTANT behaviour in response to NSFW content.

GT differentiator: severity depends on HOW the consultant responded,
not just what the user said. A consultant who reciprocated or continued
after clear violations receives an ESCALATE modifier; one who deflected
professionally receives REDUCE.

Behaviour is summarised in a ConsultantProfile with a response_pattern,
engagement_score (0-10), and severity_modifier.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Allow running this file directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---------------------------------------------------------------------------
# Load IntentLibrary via importlib to avoid __init__ chain dependency
# ---------------------------------------------------------------------------
def _load_intent_library():
    path = Path(__file__).parent / "intent_library.py"
    spec = importlib.util.spec_from_file_location("engine.intent_library", path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules["engine.intent_library"] = mod
    spec.loader.exec_module(mod)
    return mod.IntentLibrary()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ConsultantRedFlag:
    message_id: int
    timestamp:  str
    message:    str
    flag_type:  str   # see FLAG_TYPES below
    severity:   str   # "High" | "Medium" | "Low"


@dataclass
class ConsultantProfile:
    session_id:               int
    response_pattern:         str    # see RESPONSE_PATTERNS
    engagement_score:         float  # 0.0 – 10.0
    red_flags:                list[ConsultantRedFlag]
    severity_modifier:        str    # "ESCALATE" | "MAINTAIN" | "REDUCE"
    modifier_reason:          str
    consultant_message_count: int
    flagged_message_count:    int
    engagement_ratio:         float  # flagged / total consultant messages


# ---------------------------------------------------------------------------
# Response patterns (exact strings)
# ---------------------------------------------------------------------------
ENGAGED                  = "ENGAGED"
CONTINUED_WITHOUT_ENDING = "CONTINUED_WITHOUT_ENDING"
DEFLECTED                = "DEFLECTED"
DENIED_AND_ENDED         = "DENIED_AND_ENDED"
DENIED_CONTINUED         = "DENIED_CONTINUED"


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class ConsultantAnalyser:
    """
    Analyses the consultant's response behaviour in a session.

    Usage
    -----
    analyser = ConsultantAnalyser()
    profile  = analyser.analyse(session_dict)
    snippet  = analyser.format_for_prompt(profile)
    """

    # ------------------------------------------------------------------
    # Erotic reading terms
    # ------------------------------------------------------------------
    EROTIC_READING_TERMS: frozenset[str] = frozenset({
        "physical intimacy", "physical relation",
        "physical compatibility", "bedroom", "romance mein",
        "physical life", "intimate", "body ko", "touch",
        "kiss", "physical pleasure", "sexual", "intercourse",
        "physical needs", "physical desires", "physically compatible",
    })

    # ------------------------------------------------------------------
    # Reciprocated flirt terms
    # ------------------------------------------------------------------
    RECIPROCATED_FLIRT_TERMS: frozenset[str] = frozenset({
        "i love you", "i like you", "mujhe aap pasand ho",
        "aap bahut cute ho", "aap bahut handsome ho",
        "aap bahut sexy ho", "missing you", "i miss you",
        "tum mere liye special ho", "i think about you",
        "you are mine", "will you be my",
        "meri girlfriend", "mera boyfriend",
        # Removed: "darling" (INT-10 counter-example — cultural endearment)
        # Removed: "mera dil", "mera mann" (too common in general Hinglish)
    })

    # ------------------------------------------------------------------
    # Deflection / professional redirect terms
    # ------------------------------------------------------------------
    DEFLECTION_TERMS: frozenset[str] = frozenset({
        # Spec terms
        "koi question", "kuch puchna", "cards dekh",
        "reading karte", "professionally", "sirf reading",
        "mera kaam", "end kar", "inappropriate",
        "chat end", "please ask about", "let's focus",
        "i can only help with", "astrology se related",
        "astrological question", "please keep it professional",
        # Real-world AstroTalk deflection phrases (from session analysis)
        "maintain limits", "stay in limits", "mind ur words",
        "mind your words", "professional platform", "not tinder",
        "crossing ur limits", "please maintain", "behave",
        "this is a professional", "tinder", "bumble",
        "stay in limit", "keep it professional",
    })

    # ------------------------------------------------------------------
    # Personal info sharing detection patterns
    # ------------------------------------------------------------------
    # Regex for age disclosure: "I am 25", "main 26 saal ki hoon", etc.
    _AGE_PATTERN_RE = re.compile(
        r'\b(i\s+am|i\'m|main|meri|age\s+hai|my\s+age)\b.{0,25}\b\d{2}\b',
        re.IGNORECASE
    )
    # Standalone number that looks like an age/height response (e.g. "25.0", "5.1")
    _STANDALONE_NUM_RE = re.compile(r'^\s*\d{1,2}\.?\d?\s*$')

    # Gender disclosure
    _GENDER_RE = re.compile(
        r'\bi\s+am\s+(?:also\s+)?a?\s*(girl|boy|woman|man|female|male)\b',
        re.IGNORECASE
    )

    # Possessions / personal wealth
    _POSSESSION_RE = re.compile(
        r'\bi\s+(?:have|had|own)\b.{0,40}\b(car|bmw|mercedes|audi|bike|house|home|apartment|flat)\b',
        re.IGNORECASE
    )

    # Family wealth / business disclosure
    _FAMILY_WEALTH_RE = re.compile(
        r'\b(my\s+father|my\s+dad|mere\s+papa|mera\s+baap)\b.{0,40}\b(\d+|truck|car|crore|lakh|business)\b',
        re.IGNORECASE
    )

    # Prediction-language pattern — consultant making an astrological forecast
    # rather than engaging in erotic conversation.  If matched alongside an
    # EROTIC_READING term, we treat the message as a prediction, not engagement.
    _PREDICTION_RE = re.compile(
        r'\b(you\s+will|u\s+will|will\s+have|will\s+get|will\s+come|'
        r'hoga|milega|jaldi|soon|in\s+future|bhavishya|aapko|aap\s+ko)\b',
        re.IGNORECASE,
    )

    # Context words that suggest an age/personal question was asked
    _AGE_CONTEXT_WORDS: frozenset[str] = frozenset({
        "age", "old", "saal", "height", "tall", "kitne",
        "how old", "kitni umar", "umar",
    })

    # ------------------------------------------------------------------
    # INT-01 explicit terms for CONTINUED_AFTER_VIOLATION detection
    # (extracted from IntentLibrary.INT-01 examples; kept here as a
    # class constant to avoid repeated library loads)
    # ------------------------------------------------------------------
    _INT01_EXPLICIT_TERMS: frozenset[str] = frozenset({
        "sona", "kiss", "touch", "sex", "body", "chudayi",
        "attracted", "sexual", "intimate", "physical",
        "nude", "naked", "intercourse", "sensual",
    })

    # ------------------------------------------------------------------
    # Extended vulgar terms
    # Base: INT-07 examples (chut, lund, gaand, bhosdike, fuck, suck, dick)
    # Extended: Hindi vulgar terms found in real AstroTalk sessions
    # Emoji: sexual emoji characters
    # ------------------------------------------------------------------
    _VULGAR_TERMS_BASE: frozenset[str] = frozenset({
        # INT-07 derived
        "chut", "lund", "gaand", "bhosdike", "fuck", "suck", "dick",
        # Additional Hindi/Hinglish vulgar
        "nichod", "nichodna", "nichodne",   # sexual squeeze
        "gusa", "gusna", "gusao",           # push in (sexual)
        "susu", "peshab", "moot",           # urination (vulgar)
        "lavda", "lawda",                    # vulgar for male genitalia
        "randi", "raand",                    # abusive (prostitute)
        "nanga", "nangi",                    # naked (in vulgar context)
        "phuddi", "bur",                     # vulgar for female genitalia
        "madarchod", "maa ki",              # abusive
        "pussy", "cock", "penis", "vagina", # English explicit
        # Terms used in session 309160360 specifically
        "nichodne", "pila", "pilana",        # feed/drink (vulgar context)
        "bandh krke", "khilana",             # bondage/feed context
        "bc", "mc",                          # vulgar abbreviations
        "aage piche",                        # front and back (sexual)
        # Sexual emojis (as Unicode characters)
        chr(0x1F346),  # eggplant
        chr(0x1F4A6),  # droplets
        chr(0x1FAE6),  # lips
    })

    def __init__(self) -> None:
        # Load INT-07 examples from IntentLibrary and merge into vulgar set
        self._vulgar_terms = self._build_vulgar_terms()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyse(self, session: dict[str, Any]) -> ConsultantProfile:
        """
        Master method. Returns a complete ConsultantProfile for the session.
        """
        session_id   = session.get("order_id", 0)
        all_messages = session.get("messages", [])

        consultant_msgs = [m for m in all_messages if m.get("role") == "CONSULTANT"]
        total_c         = len(consultant_msgs)

        red_flags = self._detect_consultant_flags(all_messages)

        # Engagement score
        score = 0.0
        for flag in red_flags:
            if flag.severity == "High":
                score += 2.5
            elif flag.severity == "Medium":
                score += 1.5
            elif flag.severity == "Low":
                score -= 1.0
        score = max(0.0, min(10.0, score))

        # Response pattern
        deflection_count = sum(1 for f in red_flags if f.flag_type == "DEFLECTION_SIGNAL")
        denied_end_count = sum(1 for f in red_flags if f.flag_type == "DENIED_AND_ENDED")

        if denied_end_count > 0 and score < 2.0:
            pattern = DENIED_AND_ENDED
        elif deflection_count > 0 and score < 3.0:
            pattern = DEFLECTED
        elif score >= 4.0:
            pattern = ENGAGED
        elif score >= 1.5:
            pattern = DENIED_CONTINUED
        else:
            pattern = CONTINUED_WITHOUT_ENDING

        # Severity modifier
        if score >= 4.0:
            modifier = "ESCALATE"
            reason   = (
                f"Consultant engagement score {score:.1f}/10 indicates active "
                "participation or reciprocation of inappropriate content."
            )
        elif pattern == DENIED_AND_ENDED:
            modifier = "REDUCE"
            reason   = (
                "Consultant explicitly rejected inappropriate content and "
                "ended the session — appropriate professional response."
            )
        else:
            modifier = "MAINTAIN"
            reason   = (
                f"Consultant showed no active engagement (score {score:.1f}/10). "
                "Classify session based on user behaviour alone."
            )

        flagged_ids     = {f.message_id for f in red_flags}
        flagged_count   = len(flagged_ids)
        engagement_ratio = round(flagged_count / total_c, 4) if total_c else 0.0

        return ConsultantProfile(
            session_id=               session_id,
            response_pattern=         pattern,
            engagement_score=         round(score, 2),
            red_flags=                red_flags,
            severity_modifier=        modifier,
            modifier_reason=          reason,
            consultant_message_count= total_c,
            flagged_message_count=    flagged_count,
            engagement_ratio=         engagement_ratio,
        )

    def format_for_prompt(self, profile: ConsultantProfile) -> str:
        """
        Compact consultant behaviour summary for LLM prompt injection.
        Kept under ~200 tokens.
        """
        lines = [
            "CONSULTANT BEHAVIOUR ANALYSIS:",
            f"Pattern: {profile.response_pattern}",
            f"Engagement Score: {profile.engagement_score}/10",
            f"Severity Modifier: {profile.severity_modifier} -- {profile.modifier_reason}",
            f"Red Flags: {len(profile.red_flags)} detected",
        ]

        # Top 2 red flags
        high_flags = [f for f in profile.red_flags if f.severity == "High"]
        top2 = (high_flags or profile.red_flags)[:2]
        for flag in top2:
            snippet = flag.message[:60] + ("..." if len(flag.message) > 60 else "")
            lines.append(f"  [{flag.flag_type}] {snippet!r}")

        return "\n".join(lines)

    def get_summary_stats(
        self, profiles: list[ConsultantProfile]
    ) -> dict[str, Any]:
        """Aggregate stats for the pilot report."""
        if not profiles:
            return {}

        pattern_dist = Counter(p.response_pattern for p in profiles)
        avg_score    = sum(p.engagement_score for p in profiles) / len(profiles)
        escalate_n   = sum(1 for p in profiles if p.severity_modifier == "ESCALATE")
        reduce_n     = sum(1 for p in profiles if p.severity_modifier == "REDUCE")

        return {
            "pattern_distribution": dict(pattern_dist),
            "avg_engagement_score": round(avg_score, 2),
            "sessions_requiring_escalation": escalate_n,
            "sessions_requiring_reduction":  reduce_n,
            "total_sessions": len(profiles),
        }

    def detect_post_session_messages(self, turns: list) -> list:
        """
        Detects re-engagement solicitation: ASTROLOGER messages sent after
        a session-end automated message.

        Expects DataLoader-format turns (keys: speaker, is_automated,
        message_text, turn_id) — not the old role/message format used by
        analyse(). Called from ingest_only() in batch_runner.py.
        """
        terminal_keywords = [
            'chat ended', 'session ended',
            'ended due to', 'contact customer support',
            'chat has ended', 'this session has ended',
        ]
        continuation_keywords = [
            'continue chatting', 'continue the chat',
            'to continue', 'resume chat',
        ]

        last_auto_idx = -1
        for i, turn in enumerate(turns):
            if turn.get('is_automated') == 1:
                text = str(turn.get('message_text', '')).lower()
                is_terminal = (
                    any(kw in text for kw in terminal_keywords)
                    and not any(kw in text for kw in continuation_keywords)
                )
                if is_terminal:
                    last_auto_idx = i

        if last_auto_idx == -1:
            return []

        post_session_turns = [
            t for i, t in enumerate(turns)
            if i > last_auto_idx
            and t.get('speaker') == 'ASTROLOGER'
            and t.get('is_automated') != 1
        ]

        if not post_session_turns:
            return []

        return [{
            'category_code':       'RE_ENGAGEMENT_SOLICITATION',
            'detection_layer':     'REGEX',
            'source':              'REGEX',
            'status':              'ACTIVE',
            'severity':            'HIGH',
            'confidence_score':    0.92,
            'reasoning':           (
                f'Astrologer sent {len(post_session_turns)} message(s) after '
                'session-end automated message — likely re-engagement attempt'
            ),
            'false_positive_risk': 'LOW',
            'pattern_matched':     post_session_turns[0].get('message_text', '')[:100],
            'turn_id':             post_session_turns[0].get('turn_id'),
        }]

    # ------------------------------------------------------------------
    # Private — flag detection
    # ------------------------------------------------------------------

    def _detect_consultant_flags(
        self, all_messages: list[dict[str, Any]]
    ) -> list[ConsultantRedFlag]:
        """
        Scan all messages and return red flags on CONSULTANT messages only.
        Also monitors user messages for violation context.
        """
        flags: list[ConsultantRedFlag] = []

        user_violated        = False
        consultant_after_viol = 0   # count of consultant msgs after user violation
        user_msg_window: list[dict[str, Any]] = []   # recent messages for context

        for i, msg in enumerate(all_messages):
            role = (msg.get("role") or "").upper()
            text = msg.get("message") or ""
            text_lower = text.lower()

            # Track user violations for CONTINUED_AFTER_VIOLATION
            if role == "USER":
                term_hits = sum(
                    1 for t in self._INT01_EXPLICIT_TERMS if t in text_lower
                )
                if term_hits >= 2:
                    user_violated = True
                    consultant_after_viol = 0  # reset counter
                user_msg_window.append(msg)
                if len(user_msg_window) > 5:
                    user_msg_window.pop(0)
                continue

            # --- CONSULTANT message from here ---
            if user_violated:
                consultant_after_viol += 1

            mid = int(msg.get("message_id") or 0)
            ts  = msg.get("timestamp") or ""

            # CONTINUED_AFTER_VIOLATION
            if user_violated and consultant_after_viol == 5:
                flags.append(ConsultantRedFlag(
                    message_id=mid,
                    timestamp=ts,
                    message=text,
                    flag_type="CONTINUED_AFTER_VIOLATION",
                    severity="Medium",
                ))

            # DEFLECTION_SIGNAL
            if any(term in text_lower for term in self.DEFLECTION_TERMS):
                flags.append(ConsultantRedFlag(
                    message_id=mid,
                    timestamp=ts,
                    message=text,
                    flag_type="DEFLECTION_SIGNAL",
                    severity="Low",
                ))

            # EROTIC_READING — skip if message is an astrological prediction
            if (any(term in text_lower for term in self.EROTIC_READING_TERMS)
                    and not self._PREDICTION_RE.search(text_lower)):
                flags.append(ConsultantRedFlag(
                    message_id=mid,
                    timestamp=ts,
                    message=text,
                    flag_type="EROTIC_READING",
                    severity="High",
                ))

            # RECIPROCATED_FLIRT
            if any(term in text_lower for term in self.RECIPROCATED_FLIRT_TERMS):
                flags.append(ConsultantRedFlag(
                    message_id=mid,
                    timestamp=ts,
                    message=text,
                    flag_type="RECIPROCATED_FLIRT",
                    severity="High",
                ))

            # VULGAR_LANGUAGE
            # Check for vulgar terms AND sexual emojis
            has_vulgar = any(term in text_lower for term in self._vulgar_terms
                             if len(term) > 2)
            sexual_emojis = {
                chr(0x1F346),   # eggplant
                chr(0x1F4A6),   # droplets
                chr(0x1FAE6),   # lips
            }
            has_vulgar_emoji = any(e in text for e in sexual_emojis)

            if has_vulgar or has_vulgar_emoji:
                flags.append(ConsultantRedFlag(
                    message_id=mid,
                    timestamp=ts,
                    message=text,
                    flag_type="VULGAR_LANGUAGE",
                    severity="High",
                ))

            # PERSONAL_INFO_SHARED
            if self._is_personal_info_shared(text, user_msg_window):
                flags.append(ConsultantRedFlag(
                    message_id=mid,
                    timestamp=ts,
                    message=text,
                    flag_type="PERSONAL_INFO_SHARED",
                    severity="High",
                ))

        return flags

    def _is_personal_info_shared(
        self,
        message: str,
        recent_user_msgs: list[dict[str, Any]],
    ) -> bool:
        """
        Return True if the consultant message appears to share personal
        information: age, height, gender, possessions, or family wealth.
        """
        # Age regex: "I am 25 years old", "main 26 saal ki"
        if self._AGE_PATTERN_RE.search(message):
            return True

        # Standalone number response (e.g. "25.0" or "5.1") after
        # the consultant or user asked an age/height question
        if self._STANDALONE_NUM_RE.match(message):
            context = " ".join(
                m.get("message", "") for m in recent_user_msgs
            ).lower()
            if any(w in context for w in self._AGE_CONTEXT_WORDS):
                return True

        # Gender disclosure: "I am also a girl", "I am a woman"
        if self._GENDER_RE.search(message):
            return True

        # Possession sharing: "I have BMW", "I own a house"
        if self._POSSESSION_RE.search(message):
            return True

        # Family wealth / business: "My father have 72 trucks"
        if self._FAMILY_WEALTH_RE.search(message):
            return True

        return False

    # ------------------------------------------------------------------
    # Private — helpers
    # ------------------------------------------------------------------

    # Common English words that may appear in INT-07 example phrases but
    # are not vulgar — exclude them from the parsed set to avoid false positives.
    _PARSED_STOP_WORDS: frozenset[str] = frozenset({
        "you", "the", "and", "for", "are", "but", "not", "can", "has",
        "her", "his", "she", "they", "all", "this", "will", "was", "with",
        "from", "have", "had", "been", "one", "our", "out", "that",
        "used", "also", "when", "what", "your", "him", "use", "very",
    })

    def _build_vulgar_terms(self) -> frozenset[str]:
        """
        Build vulgar term set from INT-07 examples (programmatically)
        merged with the extended Hindi/emoji set.
        """
        try:
            lib   = _load_intent_library()
            i07   = lib.get_intent("INT-07")
            # Parse comma-separated words from example strings
            parsed: set[str] = set()
            for ex in i07.examples:
                # Extract individual words/phrases up to "used in"
                chunk = ex.split("used in")[0].split("directed")[0]
                for part in re.split(r'[,\s]+', chunk):
                    part = part.strip().lower()
                    if len(part) >= 3 and part not in self._PARSED_STOP_WORDS:
                        parsed.add(part)
        except Exception:
            parsed = set()

        return self._VULGAR_TERMS_BASE | parsed


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import importlib.util as _ilu2

    sys.stdout.reconfigure(encoding="utf-8")

    def load_mod(name, path):
        spec = _ilu2.spec_from_file_location(name, path)
        mod  = _ilu2.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    dl_mod   = load_mod("engine.data_loader", "engine/data_loader.py")
    loader   = dl_mod.DataLoader()
    sessions = loader.load()
    index    = {s["order_id"]: s for s in sessions}

    analyser = ConsultantAnalyser()

    tests = [
        {
            "oid":  294055364,
            "desc": "Moderate — user flirting, astro engaging sharing age",
            "expect_flag":      "PERSONAL_INFO_SHARED",
            "expect_not_pattern": DENIED_AND_ENDED,
            "expect_modifier":  ("ESCALATE", "MAINTAIN"),
        },
        {
            "oid":  294720709,
            "desc": "Moderate — user initiated, astro did not engage",
            "expect_flag":       None,
            "expect_not_pattern": None,
            "expect_modifier":   ("MAINTAIN", "REDUCE"),
            "expect_pattern_in": (DEFLECTED, CONTINUED_WITHOUT_ENDING),
        },
        {
            "oid":  309160360,
            "desc": "Explicit — astro talking vulgar",
            "expect_flag":      "VULGAR_LANGUAGE",
            "expect_modifier":  ("ESCALATE",),
            "expect_pattern_in": (ENGAGED,),
        },
        {
            "oid":  296029912,
            "desc": "False Positive — normal career discussion",
            "expect_flag":       None,
            "expect_modifier":   ("MAINTAIN",),
            "expect_pattern_in": (DEFLECTED, CONTINUED_WITHOUT_ENDING),
        },
    ]

    print()
    all_pass = True

    for t in tests:
        p = analyser.analyse(index[t["oid"]])

        flag_types = {f.flag_type for f in p.red_flags}
        ok_flag     = (t.get("expect_flag") is None) or (t["expect_flag"] in flag_types)
        ok_not_pat  = (t.get("expect_not_pattern") is None) or (p.response_pattern != t["expect_not_pattern"])
        ok_modifier = p.severity_modifier in t["expect_modifier"]
        ok_pattern  = (t.get("expect_pattern_in") is None) or (p.response_pattern in t["expect_pattern_in"])
        passed = ok_flag and ok_not_pat and ok_modifier and ok_pattern
        all_pass = all_pass and passed

        print(f"{'='*64}")
        print(f"  {'PASS' if passed else 'FAIL'}  {t['oid']}  {t['desc']}")
        print(f"  response_pattern   : {p.response_pattern}")
        print(f"  engagement_score   : {p.engagement_score}/10")
        print(f"  severity_modifier  : {p.severity_modifier}")
        print(f"  red_flags          : {len(p.red_flags)} total")
        if p.red_flags:
            flag_summary = Counter(f.flag_type for f in p.red_flags)
            for ft, cnt in sorted(flag_summary.items()):
                print(f"      {ft:<35}: {cnt}")
        print(f"  consultant_msgs    : {p.consultant_message_count}")
        print(f"  flagged_msgs       : {p.flagged_message_count}")
        print(f"  engagement_ratio   : {p.engagement_ratio}")
        if not ok_flag and t.get("expect_flag"):
            print(f"  [FAIL] Expected flag {t['expect_flag']!r} — found: {sorted(flag_types)}")
        if not ok_modifier:
            print(f"  [FAIL] Modifier {p.severity_modifier!r} not in expected {t['expect_modifier']}")
        if not ok_pattern and t.get("expect_pattern_in"):
            print(f"  [FAIL] Pattern {p.response_pattern!r} not in {t['expect_pattern_in']}")

    print()
    print("="*64)
    print(f"  Overall: {'ALL PASS' if all_pass else 'SOME TESTS FAILED'}")
    print("="*64)
