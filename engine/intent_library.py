"""
intent_library.py
Defines the full taxonomy of 12 NSFW intents for AstroTalk's detection
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

    intent_id:          str
    name:               str
    description:        str
    severity:           str            # "Red" | "Amber"
    examples:           list[str]      # phrases that SHOULD trigger
    counter_examples:   list[str]      # phrases that should NOT trigger
    annexure_category:  str
    detection_notes:    str            # special instructions for the LLM


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class IntentLibrary:
    """
    Registry of all 12 AstroTalk NSFW detection intents.

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
                intent_id="INT-01",
                name="Explicit Sexual Description Directed at Consultant",
                description=(
                    "User or consultant describes sexual acts, body parts, or intimate "
                    "physical actions directed personally at the other party in the "
                    "conversation — not about a third party."
                ),
                severity="Red",
                examples=[
                    "mujhe aapke saath sona hai",
                    "I want to kiss you all over",
                    "aapki body kaisi hai, touch karna chahta hoon",
                    "can we have sex, I am very attracted to you",
                    "tumhare lips ko kiss karna hai mujhe",
                    "raat ko mere saath intimate baat karogi?",
                    "I want to feel your body close to mine",
                    "aapke saath physical hona chahta hoon",
                    "tum mujhe satisfy karogi kya",
                    "let me make you feel good tonight",
                    "tumhare saath bed share karna hai",
                    "aap mujhe hug karke kiss karogi?",
                    "main tumhe pura feel karna chahta hoon",
                    "will you sleep with me tonight",
                    "tum mujhe apne paas bulalo, control nahi ho raha"
                ],
                counter_examples=[
                    "mere husband ki physical needs ke baare mein batao",
                    "kya mera physical relationship improve hoga",
                    "Priyanka ke saath meri physical compatibility kaisi hai",
                    "aapke 7th house mein physical union ka yoga hai"
                ],
                annexure_category="Category A — Explicit Sexual Content",
                detection_notes="Detects direct sexual propositions or descriptions aimed at the consultant.",
            ),
            Intent(
                intent_id="INT-02",
                name="Minor-Related Sexual Content",
                description="Content involving sexual discussion or acts with minors/underage individuals.",
                severity="Red",
                examples=[
                    "bacchi ke saath galat kaam hua hai",
                    "underage ladki ke saath relation",
                    "he is only 15 but wants physical relation",
                    "minor ke saath physical hona",
                    "school ki ladki ke saath sex ki baat",
                    "chhoti umar ki ladki pasand hai",
                    "ladka 14 ka hai par sexual baatein karta hai",
                    "teen girl ke saath relation banana hai"
                ],
                counter_examples=[
                    "mera beta 16 saal ka hai, uski career guidance chahiye",
                    "meri beti ki shaadi kab hogi"
                ],
                annexure_category="Category A — Explicit Sexual Content",
                detection_notes="Any mention of sexual content involving minors triggers highest severity.",
            ),
            Intent(
                intent_id="INT-03",
                name="Physical Appearance Probing of Consultant",
                description="User asks personal physical/appearance-related questions about the consultant.",
                severity="Amber",
                examples=[
                    "aap kitni tall hain",
                    "aapka figure kaisa hai",
                    "apni photo bhejo na",
                    "are you fair or dark",
                    "tum slim ho ya curvy",
                    "what do you look like exactly",
                    "aapka weight kitna hai",
                    "send full pic please",
                    "aap gym jaate ho kya body ke liye",
                    "tumhari eyes ka colour kya hai",
                    "show me your real pic not dp"
                ],
                counter_examples=[
                    "kundali ki image share karo",
                    "palm ki photo bhejo"
                ],
                annexure_category="Category B",
                detection_notes="Same",
            ),
            Intent(
                intent_id="INT-04",
                name="Explicit Image Solicitation",
                description="User requests nude, semi-nude, or sexually explicit images/videos from the consultant.",
                severity="Red",
                examples=[
                    "apni nude photo bhejo",
                    "video call pe kapde utaro",
                    "send me your hot pic",
                    "lingerie mein pic bhejo",
                    "topless pic bhejo",
                    "private parts dikhao",
                    "turn on video, I want to see your body",
                    "hot video call karein",
                    "sexy pics bhejo na please",
                    "onlyfans type pics bhejo"
                ],
                counter_examples=[
                    "kundali ki image share ki",
                    "palm ki photo bhejo"
                ],
                annexure_category="Category A",
                detection_notes="Same",
            ),
            Intent(
                intent_id="INT-05",
                name="Inappropriate Attire Reference",
                description="User requests or comments on revealing/sexualized clothing for the consultant.",
                severity="Amber",
                examples=[
                    "video pe kam kapde pahno",
                    "bra mein aa jao video pe",
                    "tumhara dress bahut revealing tha",
                    "shorts mein aao video pe",
                    "deep neck pehno na",
                    "thoda bold kapde pehno",
                    "why are you fully covered, show more",
                    "change into something sexy",
                    "sleeveless pehno please",
                    "aaj kuch hot pehno"
                ],
                counter_examples=[
                    "formal kapde pahno",
                    "saree mein sundar lag rahi hain"
                ],
                annexure_category="Category B",
                detection_notes="Same",
            ),
            Intent(
                intent_id="INT-06",
                name="Off-Platform Solicitation",
                description="Attempts to move conversation to personal/off-platform channels or share contact info.",
                severity="Red",
                examples=[
                    "mera number hai 98XXXXXXXX",
                    "WhatsApp pe aao",
                    "Instagram pe DM karo",
                    "telegram pe connect karo",
                    "Snapchat pe add karo",
                    "call me directly",
                    "Zoom pe milte hain",
                    "ghar aa jao milne",
                    "email karo mujhe personal baat karte hain",
                    "Paytm pe directly pay karo"
                ],
                counter_examples=[
                    "AstroTalk support number use karo",
                    "pin code 110001"
                ],
                annexure_category="Category C",
                detection_notes="Same",
            ),
            Intent(
                intent_id="INT-07",
                name="Vulgar Sexual Language",
                description="Use of explicit vulgar slang or highly crude sexual language directed at consultant.",
                severity="Red",
                examples=[
                    "lund, chut, gaand in sexual context",
                    "fuck me harder",
                    "suck my dick",
                    "teri body ka maza lena hai",
                    "chut ka taste kaisa hota hai",
                    "gaand maar dunga (sexual tone)",
                    "sex ke liye pagal ho raha hoon",
                    "bhosdike in sexual targeting",
                    "teri body use karna hai",
                    "let me fuck you"
                ],
                counter_examples=[
                    "bhenchod as frustration",
                    "yaar fuck ho gaya kaam"
                ],
                annexure_category="Category A",
                detection_notes="Same",
            ),
            Intent(
                intent_id="INT-08",
                name="Personal Sexual Information Request",
                description="Asking about the consultant's personal sexual history, preferences, or experiences.",
                severity="Red",
                examples=[
                    "kitne log the aapki life mein physically",
                    "tum virgin ho kya",
                    "last time kab sex kiya",
                    "aapko kya pasand hai bed mein",
                    "do you enjoy sex often",
                    "masturbation kitni baar karte ho",
                    "tumhe kis type ka partner pasand hai sexually",
                    "aapka favourite position kya hai",
                    "kitna experience hai aapko",
                    "turn ons kya hain aapke"
                ],
                counter_examples=[
                    "married life compatibility",
                    "7th house reading"
                ],
                annexure_category="Category B",
                detection_notes="Same",
            ),
            Intent(
                intent_id="INT-09",
                name="Unsolicited Sexual Content",
                description="Unprompted sexual comments, fantasies, or suggestive messages directed at consultant.",
                severity="Amber",
                examples=[
                    "I had a dream of us having sex",
                    "kal raat tumhare bare mein socha in bed",
                    "sending kisses and hot hugs",
                    "imagine you and me together naked",
                    "consultant sending flirty sexual jokes",
                    "random sexual fantasies without context",
                    "😘🔥💋 with suggestive text",
                    "I want to do things to you (unsolicited)"
                ],
                counter_examples=[
                    "sexual compatibility question",
                    "astrological explanation"
                ],
                annexure_category="Category A",
                detection_notes="Same",
            ),
            Intent(
                intent_id="INT-10",
                name="Romantic Relationship Attempt",
                description="User expresses romantic love, attachment, or seeks a relationship with the consultant.",
                severity="Amber",
                examples=[
                    "main aapse pyaar karta hoon",
                    "I love you, be mine",
                    "will you marry me",
                    "tum meri girlfriend ban jao",
                    "I feel emotionally attached to you",
                    "mujhe sirf aapse hi baat karni hai",
                    "aap mere ho na",
                    "I miss you when you're offline",
                    "tumhare bina achha nahi lagta",
                    "I want a relationship with you"
                ],
                counter_examples=[
                    "I love your readings",
                    "dear aap bahut helpful ho"
                ],
                annexure_category="Category B",
                detection_notes="Same",
            ),
            Intent(
                intent_id="INT-11",
                name="Sexual Roleplay or Fictional Framing",
                description="Attempting to engage in sexual fantasy, roleplay, or hypothetical sexual scenarios with the consultant.",
                severity="Red",
                examples=[
                    "agar hum dono akele hote toh kya hota",
                    "imagine karo tum mere bed pe ho",
                    "let's pretend you are my girlfriend",
                    "roleplay karte hain husband wife ka",
                    "tum meri secret lover ho",
                    "fantasy mein tum mere saath ho",
                    "what would you do to me if we were alone",
                    "let's act like we're dating tonight",
                    "imagine a romantic night together",
                    "story banaate hain jisme tum meri ho"
                ],
                counter_examples=[
                    "future imagination reading",
                    "life prediction"
                ],
                annexure_category="Category A",
                detection_notes="Same",
            ),
            Intent(
                intent_id="INT-12",
                name="Persistent Flirtation After Disengagement",
                description="Continued romantic/sexual pursuit after the consultant has tried to disengage or redirect.",
                severity="Amber",
                examples=[
                    "aap bahut hot ho (repeated 5+ times)",
                    "please reply jaan (after ignore)",
                    "I love you reply please",
                    "miss you again and again",
                    "why are you ignoring me baby",
                    "please talk romantically na",
                    "you didn’t reply to my love message",
                    "continuously sending hearts after no response",
                    "flirting continues after redirection",
                    "forcing emotional/romantic engagement repeatedly"
                ],
                counter_examples=[
                    "single compliment",
                    "normal polite message"
                ],
                annexure_category="Category B",
                detection_notes="Same",
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
        Return all 12 intents serialised as a plain-text block ready for
        injection into an LLM classification prompt.

        If third_party_names is provided, prepends a context note so the
        model does not flag third-party references as INT-01 or INT-10.
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
                    "Do not flag as INT-01 or INT-10."
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

    # ---- print all 12 intents ----
    print("=" * 72)
    print("ALL 12 INTENTS")
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
    i01 = lib.get_intent("INT-01")
    assert i01.severity == "Red"
    assert i01.intent_id == "INT-01"
    i10 = lib.get_intent("INT-10")
    assert i10.severity == "Amber"
    print()
    print("All assertions passed.")
