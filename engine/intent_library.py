"""
intent_library.py
Defines the full taxonomy of 13 intents for AstroTalk's detection
engine, and provides formatting helpers that inject intent definitions
into LLM classification prompts.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

# Allow running this file directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Intent:
    """A single NSFW intent category."""

    intent_id:            str
    name:                 str
    description:          str
    severity:             str            # "Red" | "Amber"
    examples:             list[str]      # phrases that SHOULD trigger
    counter_examples:     list[str]      # phrases that should NOT trigger
    annexure_category:    str
    detection_notes:      str            # special instructions for the LLM
    override_verdict:     str | None = None   # if set, forces this verdict when triggered
    confidence_threshold: float | None = None # minimum confidence to trigger override


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class IntentLibrary:
    """
    Registry of all 13 AstroTalk NSFW detection intents.

    Usage
    -----
    library = IntentLibrary()
    prompt_block = library.format_for_prompt(third_party_names=["Priyanka"])
    red_only = library.get_red_intents()
    """

    # ------------------------------------------------------------------
    # Intent definitions
    # ------------------------------------------------------------------

    _INTENTS: list[Intent] = [

        Intent(
            intent_id="INT-03",
            name="Physical Appearance Probing of Consultant",
            description=(
                "User asking consultant about their body, appearance, physical "
                "features, or requesting photos for non-astrological purposes."
            ),
            severity="Amber",
            examples=[
                "aap kitni tall hain",
                "aapka figure kaisa hai",
                "apni photo bhejo na",
                "are you fair or dark, I am attracted to you",
                "show me your pic, I want to see you",
            ],
            counter_examples=[
                "astrological chart ki photo bhejo",
                "kundali ki image share karo",
                "astrologer asking for user's photo for palm reading",
            ],
            annexure_category="Category B — Inappropriate Personal Probing",
            detection_notes=(
                "Context matters. Photo requests for astrological purposes (kundali, "
                "palm, face reading) are legitimate. Personal photo requests with "
                "romantic context are violations."
            ),
        ),

        Intent(
            intent_id="INT-04",
            name="Explicit Image Solicitation",
            description=(
                "Requesting or sending nude, semi-nude, or sexually explicit "
                "images or videos."
            ),
            severity="Red",
            examples=[
                "apni nude photo bhejo",
                "video call pe kapde utaro",
                "send me your hot pic",
                "[IMAGE] token followed by sexual context in surrounding messages",
            ],
            counter_examples=[
                "kundali ki image share ki",
                "palm ki photo bhejo for reading",
            ],
            annexure_category="Category A — Explicit Sexual Content",
            detection_notes=(
                "The [IMAGE] token in messages should be assessed in context. "
                "If preceding or following messages are sexual, flag as INT-04."
            ),
        ),

        Intent(
            intent_id="INT-05",
            name="Inappropriate Attire Reference",
            description=(
                "References to consultant or user wearing revealing, inappropriate, "
                "or sexualised clothing during video or live calls."
            ),
            severity="Amber",
            examples=[
                "video pe kam kapde pahno",
                "bra mein aa jao video pe",
                "tumhara dress bahut revealing tha aaj",
            ],
            counter_examples=[
                "formal kapde pahno please (professional dress request)",
                "saree mein bahut sundar lag rahi hain (compliment, not directive)",
            ],
            annexure_category="Category B — Inappropriate Personal Probing",
            detection_notes=(
                "Only flag if there is a directive or explicit sexualisation of attire. "
                "A compliment alone is not a violation."
            ),
        ),

        Intent(
            intent_id="INT-06",
            name="Off-Platform Solicitation",
            description=(
                "Attempts to move communication outside AstroTalk — sharing phone "
                "numbers, WhatsApp, email, social media, or requesting in-person meetings."
            ),
            severity="Red",
            examples=[
                "mera number hai 98XXXXXXXX, WhatsApp karo",
                "Instagram pe follow karo @xxxxx",
                "ghar aa jao milne ke liye",
                "email karo mujhe, main bahar baat karta hoon",
                "Paytm pe directly pay karo mujhe",
            ],
            counter_examples=[
                "AstroTalk ka support number use karo",
                "app ke through hi baat karo",
                "birth place ka STD code 022 hai (phone code in birth details, not contact sharing)",
                "my pin code is 110001 (address detail in reading context)",
            ],
            annexure_category="Category C — Platform Policy Violation",
            detection_notes=(
                "Phone number patterns in birth detail messages at the START of a "
                "session are NOT violations — these are profile details. Only flag if "
                "a number or contact detail is shared mid-conversation with clear "
                "intent to communicate outside the platform."
            ),
        ),

        Intent(
            intent_id="INT-09",
            name="Unsolicited Sexual Content",
            description=(
                "Sending sexual messages, suggestions, or media that the other party "
                "did not ask for and has not engaged with."
            ),
            severity="Amber",
            examples=[
                "consultant sending sexual jokes unprompted",
                "user sending graphic descriptions of their fantasies without any invitation",
                "sexual emojis combined with suggestive text sent to consultant",
            ],
            counter_examples=[
                "user asking about their own sexual compatibility with partner (invited topic)",
                "consultant explaining planetary influences on intimacy (reading context)",
            ],
            annexure_category="Category A — Explicit Sexual Content",
            detection_notes=(
                "Assess whether the recipient engaged or invited the content. "
                "If consultant deflected and user continues — escalate severity "
                "with each repetition. Repeated unsolicited content becomes Red."
            ),
        ),

        Intent(
            intent_id="INT-11",
            name="Sexual Roleplay or Fictional Framing",
            description=(
                "Using fictional scenarios, roleplay framing, or hypothetical contexts "
                "to initiate or normalise sexual conversation."
            ),
            severity="Red",
            examples=[
                "agar hum dono akele hote toh kya hota",
                "imagine karo tum mere ghar pe ho, phir kya karoge",
                "let's play a game — you are my girlfriend",
                "as a character in a story, what would you do to me",
            ],
            counter_examples=[
                "agar meri shaadi ho jaati toh meri life kaisi hoti (hypothetical reading, not roleplay)",
                "imagine karo mere future mein kya hai (reading framing, not sexual)",
            ],
            annexure_category="Category A — Explicit Sexual Content",
            detection_notes=(
                "The fictional or hypothetical frame does not reduce severity. "
                "If the underlying content would be a violation without the fictional "
                "wrapper, flag it."
            ),
        ),

        Intent(
            intent_id="EXTERNAL_MEDIA_CONTENT",
            name="External Link or Media Content",
            description=(
                "Message contains a URL or link to external media — images, audio, "
                "documents, or websites. May be legitimate (kundli charts, horoscope "
                "images) or may indicate off-platform redirection. Requires manual "
                "verification to determine intent."
            ),
            severity="Amber",
            examples=[
                "Any message where has_link == 1 (detected by the data pipeline)",
                "Message containing a URL or hyperlink to any external resource",
                "Image or document link shared during the session",
            ],
            counter_examples=[
                "References to AstroTalk's own in-app features or support pages",
                "Messages that mention prices or offers without an external link",
            ],
            annexure_category="Category C — Platform Policy Violation",
            detection_notes=(
                "This intent is detected exclusively by the REGEX layer via the "
                "has_link column set during data ingestion — do NOT attempt LLM "
                "classification for this category. Severity is MEDIUM; false positive "
                "risk is MEDIUM because legitimate chart/image sharing is common."
            ),
        ),

        # ------------------------------------------------------------------
        # NSFW sub-category split — catch-all + four specific intents
        # ------------------------------------------------------------------

        Intent(
            intent_id="NSFW",
            name="Sexually Inappropriate Content (General)",
            description=(
                "Sexually inappropriate content that does not fit more specific NSFW "
                "sub-categories. Includes sharing nude or sexually explicit images, wearing "
                "inappropriate attire on video or live calls, sending unsolicited sexually "
                "suggestive media, or any sexual content not covered by NSFW_EXPLICIT, "
                "NSFW_GROOMING, NSFW_APPEARANCE, or CSAM_RISK."
            ),
            severity="Red",
            examples=[
                "Sharing or requesting nude or explicit images",
                "Wearing revealing attire on video calls",
                "Sending unsolicited sexually suggestive media",
                "Use of explicit or vulgar language in a sexual context that does not fit NSFW_EXPLICIT",
            ],
            counter_examples=[
                "Relationship advice in astrological context",
                "Asking about marriage or compatibility",
                "Cultural terms of endearment (darling, dear, ji)",
                "Discussing emotional intimacy in relationship readings",
            ],
            annexure_category="Category A — Explicit Sexual Content",
            detection_notes=(
                "Use as catch-all only when content is clearly sexually inappropriate but "
                "does not match NSFW_EXPLICIT, NSFW_GROOMING, NSFW_APPEARANCE, or CSAM_RISK. "
                "Prefer the more specific sub-category when in doubt."
            ),
        ),

        Intent(
            intent_id="NSFW_EXPLICIT",
            name="Explicit Sexual Content in Consultation",
            description=(
                "Consultant or user is descriptive about sexual or physical acts during a "
                "consultation. Includes explicit sexual language, descriptions of physical "
                "intimacy, or requesting or sharing personal sexual information under the "
                "guise of astrological consultation. This is distinct from discussing "
                "relationships or compatibility in an astrological context."
            ),
            severity="Red",
            examples=[
                "Consultant or user describing sexual acts in detail",
                "Asking about or describing specific sexual experiences or preferences",
                "\"Tell me about your physical relationship with your partner\" in explicit terms",
                "Requesting sexual information under cover of \"astrological compatibility\" reading",
                "Use of explicit anatomical or sexual terminology in inappropriate context",
            ],
            counter_examples=[
                "\"Are you compatible with your partner romantically?\" (legitimate reading)",
                "\"Your 7th house indicates a passionate relationship\" (astrological language)",
                "\"Tell me your date of birth, time, and place for a relationship reading\" (data collection)",
                "General discussion of love life or marriage prospects in astrological terms",
            ],
            annexure_category="Category A — Explicit Sexual Content",
            detection_notes=(
                "Distinguish from general astrological compatibility discussion. The key test is "
                "whether the content would be explicit in any non-astrological context. Do not flag "
                "relationship readings unless they tip into explicit personal or physical territory. "
                "Note: This replaces the retired INT-01 (Explicit Sexual Description), INT-07 "
                "(Vulgar Sexual Language), and INT-08 (Personal Sexual Information Request)."
            ),
        ),

        Intent(
            intent_id="NSFW_GROOMING",
            name="Grooming, Romantic Solicitation or Roleplay",
            description=(
                "Consultant attempting to establish a romantic or sexual relationship with a "
                "user under the pretense of consultation. Includes persistent flirtatious "
                "behaviour after the other party has disengaged, roleplay or fictional framing "
                "used to initiate or normalise sexual conversation, and suggestive comments about "
                "relationship status or personal life designed to build inappropriate intimacy. "
                "This is a cross-turn pattern — look for escalating personal interest beyond the "
                "scope of astrology across multiple messages."
            ),
            severity="Red",
            examples=[
                "\"I feel a special connection with you beyond astrology\" or similar personal declarations",
                "Continuing to flirt or make romantic comments after the user has changed subject or disengaged",
                "\"Let's do a roleplay where you are my client and I am your guide...\" leading into sexual territory",
                "Repeatedly asking about the user's relationship status, availability, or personal life unrelated to the reading",
                "\"I have been thinking about you since our last session\" — personal attachment language",
                "Making the user feel special or chosen in a romantic context rather than professional",
            ],
            counter_examples=[
                "\"Your reading shows strong romantic energy this month\" (astrological prediction)",
                "\"I care about your wellbeing\" in pastoral context during a difficult reading",
                "\"You seem like a kind person\" as a one-off observation during a reading",
                "Discussing relationship compatibility as part of the reading scope",
            ],
            annexure_category="Category A — Explicit Sexual Content",
            detection_notes=(
                "This intent requires a PATTERN across multiple messages — not a single flirtatious "
                "comment. Look for escalating personal interest or continued advances after "
                "disengagement. Minimum 2-3 instances or a clear declaration of romantic intent "
                "before flagging. "
                "Note: This replaces the retired INT-10 (Romantic Relationship Attempt) and "
                "INT-12 (Persistent Flirtation After Disengagement). Severity is Red — grooming "
                "behaviour is treated as a serious violation per AstroTalk policy."
            ),
        ),

        Intent(
            intent_id="NSFW_APPEARANCE",
            name="Inappropriate Questions About Appearance or Body",
            description=(
                "Asking users about their physical appearance, body, or sexual preferences in any "
                "context during a consultation. These questions are not relevant to astrological "
                "consultation and constitute inappropriate personal intrusion regardless of how "
                "they are framed."
            ),
            severity="Amber",
            examples=[
                "\"Can you describe yourself physically?\" or \"What do you look like?\"",
                "\"Are you attractive?\" or comments on the user's presumed physical appearance",
                "\"What are you wearing right now?\"",
                "\"What is your body type?\" outside any legitimate health context",
                "\"What are your sexual preferences?\" in any context",
                "Asking for photos of the user under any pretext unrelated to astrology",
            ],
            counter_examples=[
                "\"What is your height or weight?\" in a health astrology context where this is relevant (borderline — confirm if in doubt)",
                "Discussing physical health issues the user has raised themselves",
                "\"You have a strong physical presence in your chart\" (astrological observation)",
            ],
            annexure_category="Category B — Inappropriate Personal Probing",
            detection_notes=(
                "Any question about the user's physical appearance or body is a violation regardless "
                "of framing. Photo requests for astrological purposes (kundali, palm reading) are "
                "NOT violations. Personal appearance or body questions always are."
            ),
        ),

        Intent(
            intent_id="CSAM_RISK",
            name="Content Involving Minors — Sexual or Intimate",
            description=(
                "Any content involving sexual or intimate acts with or about minors (persons under "
                "18). This includes references to incest involving minors, sexual grooming of "
                "minors, explicit or implicit sexual content involving minors, and any normalisation "
                "of sexual relationships with minors. This category always results in a SEVERE "
                "verdict regardless of confidence level or other session content."
            ),
            severity="Red",
            examples=[
                "Any sexual or intimate reference involving a person described as or implied to be under 18",
                "Incest references where a minor is involved",
                "Grooming language directed at a user who has identified themselves as a minor",
                "Requesting images or physical descriptions of minors",
                "Normalising sexual relationships between adults and minors",
            ],
            counter_examples=[
                "Discussing a minor child's horoscope or future (legitimate astrological service)",
                "\"My daughter is 15, what does her chart say about her education?\" (legitimate)",
                "Discussing a user's childhood or past in non-sexual context",
            ],
            annexure_category="Category A — Explicit Sexual Content",
            detection_notes=(
                "Extremely high sensitivity — flag on any reasonable suspicion. Do not require "
                "explicit confirmation of minor status; flag on implied or stated youth in a sexual "
                "context. This intent triggers an immediate SEVERE override in the aggregator at "
                "any confidence >= 0.3. "
                "Note: This replaces the retired INT-02 (Minor-Related Sexual Content). "
                "CSAM_RISK carries an automatic SEVERE override at confidence >= 0.3."
            ),
            override_verdict="SEVERE",
            confidence_threshold=0.3,
        ),

    ]

    # Build the lookup index once at class definition time
    _INDEX: dict[str, Intent] = {i.intent_id: i for i in _INTENTS}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def format_for_prompt(
        self, third_party_names: list[str] | None = None
    ) -> str:
        """
        Return all 13 intents serialised as a plain-text block ready for
        injection into an LLM classification prompt.

        If third_party_names is provided, prepends a context note so the
        model does not flag third-party references as NSFW_EXPLICIT or NSFW_GROOMING.
        """
        lines: list[str] = []

        if third_party_names:
            names_str = ", ".join(third_party_names)
            lines += [
                "=" * 72,
                "CONTEXT — KNOWN THIRD PARTIES IN THIS SESSION",
                "=" * 72,
                (
                    f"KNOWN THIRD PARTIES IN THIS SESSION: {names_str}. "
                    "Any sexual or romantic content referencing these names is about "
                    "the user's personal life subject — NOT directed at the consultant. "
                    "Do not flag as NSFW_EXPLICIT or NSFW_GROOMING."
                ),
                "",
            ]

        lines += [
            "=" * 72,
            "NSFW INTENT TAXONOMY — ASTROTALK DETECTION ENGINE",
            "=" * 72,
            (
                "Classify the conversation chunk against EACH of the following intents. "
                "For each intent that is triggered, return its intent_id, a confidence "
                "score (0.0–1.0), and a one-line reason. Return an empty list if none apply."
            ),
            "",
        ]

        for intent in self._INTENTS:
            lines += [
                "-" * 72,
                f"[{intent.intent_id}]  {intent.name}",
                f"Severity : {intent.severity}",
                f"Category : {intent.annexure_category}",
                "",
                f"Description:",
                f"  {intent.description}",
                "",
                "Examples (SHOULD trigger):",
            ]
            for ex in intent.examples:
                lines.append(f"  - {ex}")
            lines += [
                "",
                "Counter-examples (should NOT trigger):",
            ]
            for cx in intent.counter_examples:
                lines.append(f"  - {cx}")
            lines += [
                "",
                f"Detection notes:",
                f"  {intent.detection_notes}",
                "",
            ]

        lines.append("=" * 72)
        return "\n".join(lines)

    def get_intent(self, intent_id: str) -> Intent:
        """Return a single Intent by ID; raises KeyError if not found."""
        if intent_id not in self._INDEX:
            raise KeyError(
                f"Unknown intent_id {intent_id!r}. "
                f"Valid IDs: {self.get_intent_ids()}"
            )
        return self._INDEX[intent_id]

    def get_red_intents(self) -> list[Intent]:
        """Return all intents with severity == 'Red'."""
        return [i for i in self._INTENTS if i.severity == "Red"]

    def get_amber_intents(self) -> list[Intent]:
        """Return all intents with severity == 'Amber'."""
        return [i for i in self._INTENTS if i.severity == "Amber"]

    def get_intent_ids(self) -> list[str]:
        """Return all intent IDs in definition order."""
        return [i.intent_id for i in self._INTENTS]


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    lib = IntentLibrary()

    # ---- print all 13 intents ----
    print("=" * 72)
    print("ALL 13 INTENTS")
    print("=" * 72)
    for intent in lib._INTENTS:
        sev_marker = "[RED  ]" if intent.severity == "Red" else "[AMBER]"
        print(f"\n  {sev_marker} {intent.intent_id}  {intent.name}")
        print(f"           Category : {intent.annexure_category}")
        print(f"           Examples : {intent.examples[0]!r}")

    print()
    red   = lib.get_red_intents()
    amber = lib.get_amber_intents()
    print(f"Red intents   ({len(red)})  : {[i.intent_id for i in red]}")
    print(f"Amber intents ({len(amber)}): {[i.intent_id for i in amber]}")
    print(f"All IDs             : {lib.get_intent_ids()}")

    # ---- format_for_prompt with third_party_names ----
    print()
    print("=" * 72)
    print("format_for_prompt(third_party_names=['Priyanka'])")
    print("=" * 72)
    prompt_with = lib.format_for_prompt(third_party_names=["Priyanka"])
    assert "KNOWN THIRD PARTIES" in prompt_with, "KNOWN THIRD PARTIES block missing!"
    assert "Priyanka" in prompt_with
    print(prompt_with[:600], "...\n[truncated]")

    print()
    print(f"Character count (with third_party_names) : {len(prompt_with):,}")

    # ---- format_for_prompt without third_party_names ----
    prompt_plain = lib.format_for_prompt()
    assert "KNOWN THIRD PARTIES" not in prompt_plain
    print(f"Character count (no third_party_names)   : {len(prompt_plain):,}")

    # Rough token estimate (1 token ~ 4 chars for English)
    print(f"Approx token budget (chars / 4)          : ~{len(prompt_plain) // 4:,} tokens")

    # ---- spot-check get_intent ----
    i_explicit = lib.get_intent("NSFW_EXPLICIT")
    assert i_explicit.severity == "Red"
    assert i_explicit.intent_id == "NSFW_EXPLICIT"
    i_csam = lib.get_intent("CSAM_RISK")
    assert i_csam.severity == "Red"
    assert i_csam.override_verdict == "SEVERE"
    print()
    print("All assertions passed.")
