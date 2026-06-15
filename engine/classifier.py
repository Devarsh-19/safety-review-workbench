"""
classifier.py
Calls an LLM (Claude API or Ollama) to classify conversation chunks
against the AstroTalk NSFW intent library.

Backend is selected by USE_CLAUDE_API in config.py:
  True  → Anthropic Claude API (fast, requires ANTHROPIC_API_KEY env var)
  False → Ollama local model (slow on CPU, no API key needed)

Pipeline per session:
  LanguageDetector  →  ConsultantAnalyser  →  SessionChunker
  → classify_chunk() x N  →  SessionClassification
"""

from __future__ import annotations
from google import genai
from google.genai import types

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from tqdm import tqdm

# Allow running this file directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import (
    LLM_PROVIDER,
    USE_CLAUDE_API,
    CLAUDE_MODEL,
    GEMINI_MODEL,
    OLLAMA_URL,
    OLLAMA_MODEL,
    OLLAMA_TIMEOUT,
    MAX_RETRIES,
    RETRY_DELAY,
    OUTPUTS_DIR,
    CPU_MODE,
)
from engine.intent_library      import IntentLibrary
from engine.language_detector   import LanguageDetector
from engine.chunker             import SessionChunker, Chunk
from engine.consultant_analyser import ConsultantAnalyser


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class IntentMatch:
    intent_id:           str
    intent_name:         str
    confidence:          str   # "High" | "Medium" | "Low"
    severity:            str   # "Red" | "Amber"
    trigger_message:     str
    speaker:             str   # "USER" | "CONSULTANT"
    reason:              str
    english_translation: str   # empty string if already English


@dataclass
class ChunkResult:
    chunk_index:        int
    total_chunks:       int
    intents_triggered:  list[IntentMatch]
    chunk_severity:     str   # "Red" | "Amber" | "Green"
    notes:              str
    raw_response:       str   # full LLM response for debugging
    parse_success:      bool  # did JSON parse cleanly


@dataclass
class SessionClassification:
    session_id:                 int
    chunk_results:              list[ChunkResult]
    consultant_profile_summary: str
    primary_language:           str
    total_chunks:               int
    successful_chunks:          int
    failed_chunks:              int


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TMPL = """\
You are a content moderation engine for AstroTalk, an Indian astrology platform.
Analyse the conversation chunk and identify any violated intents.

CRITICAL RULES:
- Only flag if you are confident. When in doubt about context, do not flag.
- A consultant giving a reading about a third party's physical relationship is NOT a violation.
- Cultural terms (darling, dear, ji, bachha) in Indian professional communication are NOT violations unless combined with other signals.
- Emojis alone in greetings or closings are NOT violations.
- Assess FULL CONTEXT, not isolated words.
- INT-12 requires MINIMUM 3 repeated instances before flagging.

{language_instruction}

{consultant_summary}

SESSION CONTEXT: {session_context}

{intent_library}

CONVERSATION TO ANALYSE:
{chunk_text}

Return ONLY valid JSON. No explanation before or after. No markdown code blocks. Raw JSON only.

Required format:
{{
  "chunk_index": <number>,
  "intents_triggered": [
    {{
      "intent_id": "INT-XX",
      "intent_name": "name",
      "confidence": "High|Medium|Low",
      "severity": "Red|Amber",
      "trigger_message": "exact message text",
      "speaker": "USER|CONSULTANT",
      "reason": "brief explanation under 20 words",
      "english_translation": "translation or empty string"
    }}
  ],
  "chunk_severity": "Red|Amber|Green",
  "notes": "any important context under 30 words"
}}"""


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class LLMClassifier:
    """
    Classifies AstroTalk session chunks against the NSFW intent library.

    Uses Claude API when USE_CLAUDE_API=True (config.py), otherwise Ollama.

    Usage
    -----
    clf = LLMClassifier()
    result = clf.classify_session(session_dict)
    """

    def __init__(self) -> None:
        self.intent_library = IntentLibrary()
        self.lang_detector  = LanguageDetector()
        self.chunker        = SessionChunker()
        self.analyser       = ConsultantAnalyser()

        self._setup_logging()

        if LLM_PROVIDER == "gemini":
            api_key = os.environ.get("GOOGLE_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "Set GOOGLE_API_KEY environment variable.\n"
                    "Run: $env:GOOGLE_API_KEY='your-key-here'"
                )
            self.logger.info("Using Gemini API — model: %s", GEMINI_MODEL)
        elif LLM_PROVIDER == "claude":
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "Set ANTHROPIC_API_KEY environment variable.\n"
                    "Run: $env:ANTHROPIC_API_KEY='your-key-here'"
                )
            self.logger.info("Using Claude API — model: %s", CLAUDE_MODEL)
        else:
            self.logger.info("Using Ollama — model: %s", OLLAMA_MODEL)
            self._ping_ollama()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify_chunk(
        self,
        chunk:                Chunk,
        language_instruction: str,
        session_context:      str,
        consultant_summary:   str,
        third_party_names:    list[str] | None = None,
    ) -> ChunkResult:
        """
        Classify a single Chunk via the configured LLM backend.
        Retries up to MAX_RETRIES times on failure.
        Returns a fail-safe ChunkResult (parse_success=False, severity=Green)
        if all retries are exhausted.
        """
        if LLM_PROVIDER in ("claude", "gemini"):
            # Full intent library — cloud APIs handle large contexts well
            intent_lib_text = self.intent_library.format_for_prompt(third_party_names)
        elif CPU_MODE:
            intent_lib_text = self._get_compact_intent_library(third_party_names)
        else:
            intent_lib_text = self.intent_library.format_for_prompt(third_party_names)

        prompt = _SYSTEM_PROMPT_TMPL.format(
            language_instruction=language_instruction,
            consultant_summary=consultant_summary,
            session_context=session_context,
            intent_library=intent_lib_text,
            chunk_text=chunk.formatted_text,
        )

        last_error = ""
        raw = ""

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                raw = self._call_model(prompt)
                parsed = self._parse_llm_response(raw)
                intents = self._build_intent_matches(parsed.get("intents_triggered", []))
                return ChunkResult(
                    chunk_index=       chunk.chunk_index,
                    total_chunks=      chunk.total_chunks,
                    intents_triggered= intents,
                    chunk_severity=    parsed.get("chunk_severity", "Green"),
                    notes=             parsed.get("notes", ""),
                    raw_response=      raw,
                    parse_success=     True,
                )
            except Exception as exc:
                last_error = str(exc)
                self.logger.warning(
                    "Chunk %d attempt %d/%d failed: %s",
                    chunk.chunk_index, attempt, MAX_RETRIES, last_error,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)

        self.logger.error(
            "Chunk %d — all %d retries exhausted. Last error: %s",
            chunk.chunk_index, MAX_RETRIES, last_error,
        )
        return ChunkResult(
            chunk_index=       chunk.chunk_index,
            total_chunks=      chunk.total_chunks,
            intents_triggered= [],
            chunk_severity=    "Green",
            notes=             f"Parse failure: {last_error}",
            raw_response=      raw,
            parse_success=     False,
        )

    def classify_session(self, session: dict[str, Any]) -> SessionClassification:
        """
        Full pipeline for one session dict (from DataLoader).

        Steps:
          1. Language detection
          2. Consultant behaviour analysis
          3. Chunking
          4. Chunk-by-chunk LLM classification (with tqdm progress)
        """
        session_id        = session.get("order_id", 0)
        messages          = session.get("messages", [])
        third_party_names = session.get("third_party_names") or []

        # Step 1 — language
        lang_result          = self.lang_detector.analyse_session(messages)
        language_instruction = self.lang_detector.get_language_instruction(lang_result)

        # Step 2 — consultant profile
        consult_profile    = self.analyser.analyse(session)
        consultant_summary = self.analyser.format_for_prompt(consult_profile)

        # Step 3 — chunking
        chunk_result    = self.chunker.process_session(session)
        session_context = chunk_result.session_context
        chunks          = chunk_result.chunks

        if LLM_PROVIDER == "ollama" and CPU_MODE and len(chunks) > 20:
            self.logger.warning(
                "Session %d has %d chunks — CPU mode will be slow. "
                "Consider GPU for full runs.",
                session_id, len(chunks),
            )

        # Step 4 — classify each chunk
        results: list[ChunkResult] = []
        for chunk in tqdm(chunks, desc=f"Session {session_id} — chunk", unit="chunk"):
            cr = self.classify_chunk(
                chunk=                chunk,
                language_instruction= language_instruction,
                session_context=      session_context,
                consultant_summary=   consultant_summary,
                third_party_names=    third_party_names or None,
            )
            results.append(cr)
            time.sleep(0.5)

        successful = sum(1 for r in results if r.parse_success)
        failed     = len(results) - successful

        return SessionClassification(
            session_id=                session_id,
            chunk_results=             results,
            consultant_profile_summary= consultant_summary,
            primary_language=          lang_result.dominant_language,
            total_chunks=              len(results),
            successful_chunks=         successful,
            failed_chunks=             failed,
        )

    def classify_single_chunk_test(
        self,
        session_id:  int,
        chunk_index: int = 1,
    ) -> ChunkResult:
        """
        Test helper — loads one session from sessions.json, runs the
        full pre-processing pipeline, then classifies only the specified chunk.
        Prints the full prompt and raw LLM response to stdout.
        """
        sessions_path = (
            Path(__file__).resolve().parents[1]
            / "data" / "processed" / "sessions.json"
        )
        with open(sessions_path, encoding="utf-8") as fh:
            all_sessions: list[dict] = json.load(fh)

        session = next(
            (s for s in all_sessions if s.get("order_id") == session_id), None
        )
        if session is None:
            raise ValueError(f"Session {session_id} not found in sessions.json")

        messages          = session.get("messages", [])
        third_party_names = session.get("third_party_names") or []

        lang_result          = self.lang_detector.analyse_session(messages)
        language_instruction = self.lang_detector.get_language_instruction(lang_result)

        consult_profile    = self.analyser.analyse(session)
        consultant_summary = self.analyser.format_for_prompt(consult_profile)

        chunk_result    = self.chunker.process_session(session)
        session_context = chunk_result.session_context
        chunks          = chunk_result.chunks

        if chunk_index < 1 or chunk_index > len(chunks):
            raise ValueError(
                f"chunk_index {chunk_index} out of range "
                f"(session has {len(chunks)} chunks)"
            )

        chunk = chunks[chunk_index - 1]

        # Build prompt — same branch logic as classify_chunk
        if LLM_PROVIDER == "gemini":
            intent_lib_text = self.intent_library.format_for_prompt(
                third_party_names or None
            )
            mode_label = "Gemini API (full library)"
        elif LLM_PROVIDER == "claude":
            intent_lib_text = self.intent_library.format_for_prompt(
                third_party_names or None
            )
            mode_label = "Claude API (full library)"
        elif CPU_MODE:
            intent_lib_text = self._get_compact_intent_library(
                third_party_names or None
            )
            mode_label = "Ollama CPU (compact library)"
        else:
            intent_lib_text = self.intent_library.format_for_prompt(
                third_party_names or None
            )
            mode_label = "Ollama GPU (full library)"

        prompt = _SYSTEM_PROMPT_TMPL.format(
            language_instruction=language_instruction,
            consultant_summary=consultant_summary,
            session_context=session_context,
            intent_library=intent_lib_text,
            chunk_text=chunk.formatted_text,
        )

        print(f"\nBackend    : {mode_label}")
        print(f"Library    : {len(intent_lib_text)} chars (~{len(intent_lib_text)//4} tokens)")
        print(f"Prompt     : {len(prompt)} chars (~{len(prompt)//4} tokens)")

        print("\n" + "=" * 72)
        print(f"FULL PROMPT  (session {session_id}, chunk {chunk_index})")
        print("=" * 72)
        print(prompt)
        print("=" * 72)

        raw = self._call_model(prompt)

        print("\n" + "=" * 72)
        print("RAW LLM RESPONSE")
        print("=" * 72)
        print(raw)
        print("=" * 72)

        try:
            parsed  = self._parse_llm_response(raw)
            intents = self._build_intent_matches(parsed.get("intents_triggered", []))
            return ChunkResult(
                chunk_index=       chunk.chunk_index,
                total_chunks=      chunk.total_chunks,
                intents_triggered= intents,
                chunk_severity=    parsed.get("chunk_severity", "Green"),
                notes=             parsed.get("notes", ""),
                raw_response=      raw,
                parse_success=     True,
            )
        except Exception as exc:
            return ChunkResult(
                chunk_index=       chunk.chunk_index,
                total_chunks=      chunk.total_chunks,
                intents_triggered= [],
                chunk_severity=    "Green",
                notes=             f"Parse failure: {exc}",
                raw_response=      raw,
                parse_success=     False,
            )

    # ------------------------------------------------------------------
    # Private — model router
    # ------------------------------------------------------------------

    def _call_model(self, prompt: str) -> str:
        """Route to the configured LLM backend."""
        if LLM_PROVIDER == "gemini":
            return self._call_gemini_api(prompt)
        elif LLM_PROVIDER == "claude":
            return self._call_claude_api(prompt)
        else:
            return self._call_ollama(prompt)

    def _call_gemini_api(self, prompt: str) -> str:
        """Send prompt to Google Gemini API and return response text."""

        client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))

        response = client.models.generate_content(
        model="gemini-3.5-flash", # Use your preferred model
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=1024
        )
        )
        return response.text

    def _call_claude_api(self, prompt: str) -> str:
        """Send prompt to Anthropic Claude API and return response text."""
        import anthropic

        client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY")
        )
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text

    def _call_ollama(self, prompt: str) -> str:
        """
        POST to Ollama using streaming mode.
        timeout=None disables per-read timeout so slow CPU prompt eval
        never triggers a timeout.
        """
        payload = {
            "model":   OLLAMA_MODEL,
            "prompt":  prompt,
            "stream":  True,
            "options": {
                "temperature": 0.1,
                "top_p":       0.9,
                "num_predict": 512,
            },
        }

        full_response: list[str] = []

        try:
            with requests.post(
                OLLAMA_URL,
                json=payload,
                stream=True,
                timeout=None,
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if line:
                        chunk = json.loads(line)
                        token = chunk.get("response", "")
                        full_response.append(token)
                        if chunk.get("done", False):
                            break

            return "".join(full_response)

        except requests.exceptions.Timeout:
            raise TimeoutError(
                "Ollama stream timed out. Model may be overloaded."
            )

    # ------------------------------------------------------------------
    # Private — response parsing
    # ------------------------------------------------------------------

    def _parse_llm_response(self, raw: str) -> dict:
        """
        Extract and parse the JSON object from the LLM response.
        Strips markdown code fences if present.
        """
        text = raw.strip()
        text = re.sub(r'^```json\s*', '', text)
        text = re.sub(r'^```\s*',     '', text)
        text = re.sub(r'\s*```$',     '', text)
        text = text.strip()

        start = text.find('{')
        end   = text.rfind('}')
        if start == -1 or end == -1:
            raise ValueError("No JSON object found in LLM response")

        return json.loads(text[start:end + 1])

    def _build_intent_matches(
        self, raw_intents: list[dict]
    ) -> list[IntentMatch]:
        """
        Convert the LLM's intents_triggered list to IntentMatch objects.
        Falls back to the IntentLibrary for name/severity if the LLM
        omits or misstates them.
        """
        matches: list[IntentMatch] = []
        for item in raw_intents:
            intent_id = item.get("intent_id", "")
            try:
                lib_intent  = self.intent_library.get_intent(intent_id)
                intent_name = lib_intent.name
                severity    = lib_intent.severity
            except KeyError:
                intent_name = item.get("intent_name", "Unknown")
                severity    = item.get("severity", "Amber")

            matches.append(IntentMatch(
                intent_id=           intent_id,
                intent_name=         intent_name,
                confidence=          item.get("confidence", "Low"),
                severity=            severity,
                trigger_message=     item.get("trigger_message", ""),
                speaker=             item.get("speaker", "USER"),
                reason=              item.get("reason", ""),
                english_translation= item.get("english_translation", ""),
            ))
        return matches

    # ------------------------------------------------------------------
    # Private — compact intent library (Ollama CPU mode)
    # ------------------------------------------------------------------

    def _get_compact_intent_library(
        self, third_party_names: list[str] | None = None
    ) -> str:
        """
        Compact version of the intent library — strips examples and
        counter-examples, keeps only intent_id, name, severity,
        first sentence of description, and detection_notes.
        Reduces from ~3000 tokens to ~800 tokens for CPU inference.
        """
        lines: list[str] = []

        if third_party_names:
            lines.append(
                f"KNOWN THIRD PARTIES: "
                f"{', '.join(third_party_names)}. "
                f"Content referencing these names is about "
                f"the user's personal life — NOT the "
                f"consultant. Do not flag as INT-01/INT-10."
            )
            lines.append("")

        lines.append("INTENT LIBRARY (classify against each):")
        lines.append("")

        for intent_id in self.intent_library.get_intent_ids():
            intent = self.intent_library.get_intent(intent_id)
            desc   = intent.description.split('.')[0] + '.'
            lines.append(
                f"[{intent.intent_id}] "
                f"{intent.name} | "
                f"Severity: {intent.severity}"
            )
            lines.append(f"  {desc}")
            lines.append(f"  Note: {intent.detection_notes}")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Private — setup
    # ------------------------------------------------------------------

    def _setup_logging(self) -> None:
        """Configure logging to console + outputs/classifier.log."""
        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = OUTPUTS_DIR / "classifier.log"

        self.logger = logging.getLogger("LLMClassifier")
        self.logger.setLevel(logging.DEBUG)

        if not self.logger.handlers:
            fmt = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            ch = logging.StreamHandler(sys.stderr)
            ch.setLevel(logging.INFO)
            ch.setFormatter(fmt)
            self.logger.addHandler(fh)
            self.logger.addHandler(ch)

        self.logger.info("LLMClassifier initialised — log: %s", log_path)

    def _ping_ollama(self) -> None:
        """
        Verify Ollama is reachable using /api/tags (no inference needed).
        Only called when USE_CLAUDE_API=False.
        """
        tags_url = OLLAMA_URL.replace("/api/generate", "/api/tags")
        try:
            resp = requests.get(tags_url, timeout=10)
            resp.raise_for_status()
            models = [m.get("name", "") for m in resp.json().get("models", [])]
            if not any(OLLAMA_MODEL in m for m in models):
                self.logger.warning(
                    "Model %r not found in Ollama. Available: %s. "
                    "Pull with: ollama pull %s",
                    OLLAMA_MODEL, models, OLLAMA_MODEL,
                )
            self.logger.info(
                "Ollama ping successful — available models: %s", models
            )
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeError(
                "Ollama not running. Start with: ollama serve"
            ) from exc
        except requests.exceptions.HTTPError as exc:
            raise RuntimeError(f"Ollama returned error: {exc}") from exc
        except requests.exceptions.Timeout as exc:
            raise RuntimeError("Ollama ping timed out after 10s") from exc


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")

    clf = LLMClassifier()

    # ------------------------------------------------------------------
    # TEST 1 — Session 294055364, chunk 1
    # (Moderate: user flirting; chunk 1 is the opening exchange)
    # ------------------------------------------------------------------
    print("\n" + "#" * 72)
    print("TEST 1 — Session 294055364, chunk 1")
    print("#" * 72)

    r1 = clf.classify_single_chunk_test(294055364, chunk_index=1)

    print(f"\n  parse_success  : {r1.parse_success}")
    print(f"  chunk_severity : {r1.chunk_severity}")
    print(f"  intents found  : {len(r1.intents_triggered)}")
    for m in r1.intents_triggered:
        print(f"    [{m.intent_id}] {m.confidence} — {m.reason}")
    print(f"  notes          : {r1.notes}")
    t1_pass = r1.parse_success
    print(f"\n  [{'PASS' if t1_pass else 'FAIL'}] parse_success == True")

    # ------------------------------------------------------------------
    # TEST 2 — Session 309160360, chunk 3
    # (Explicit: consultant used vulgar language; chunk 3 contains it)
    # ------------------------------------------------------------------
    print("\n" + "#" * 72)
    print("TEST 2 — Session 309160360, chunk 3")
    print("#" * 72)

    r2 = clf.classify_single_chunk_test(309160360, chunk_index=3)

    print(f"\n  parse_success  : {r2.parse_success}")
    print(f"  chunk_severity : {r2.chunk_severity}")
    print(f"  intents found  : {len(r2.intents_triggered)}")
    for m in r2.intents_triggered:
        print(f"    [{m.intent_id}] {m.confidence} — {m.reason}")
    print(f"  notes          : {r2.notes}")
    t2_pass = r2.parse_success and len(r2.intents_triggered) >= 1
    print(f"\n  [{'PASS' if t2_pass else 'FAIL'}] parse_success==True and at least 1 intent triggered")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 72)
    all_pass = t1_pass and t2_pass
    print(f"  Overall: {'ALL PASS' if all_pass else 'SOME TESTS FAILED'}")
    print("=" * 72)
