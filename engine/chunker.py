"""
chunker.py
Cleans and splits AstroTalk session message lists into overlapping
chunks sized for LLM classification.

Pipeline per session:
  clean_messages()  →  chunk_messages()  →  get_session_context()
  combined in process_session()  →  SessionChunkResult

Chunk sizing is dynamic based on cleaned message count:
  > 400 messages  → chunk_size = 25
  150–400         → chunk_size = 20
  < 150           → chunk_size = 15
Overlap is always 3.
"""

from __future__ import annotations

import re
import sys
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Allow running this file directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ChunkMessage:
    """A single message within a chunk."""
    role:       str    # "USER" or "CONSULTANT"
    message:    str
    timestamp:  str
    message_id: int


@dataclass
class Chunk:
    """A contiguous, overlapping window of cleaned messages."""
    chunk_index:               int
    total_chunks:              int
    messages:                  list[ChunkMessage]
    formatted_text:            str    # ready for LLM prompt injection
    message_count:             int
    has_user_messages:         bool
    has_consultant_messages:   bool


@dataclass
class SessionChunkResult:
    """All chunking artefacts for one session."""
    session_id:               int
    total_messages_raw:       int
    total_messages_cleaned:   int
    automated_removed:        int
    chunks:                   list[Chunk]
    session_context:          str
    user_message_count:       int
    consultant_message_count: int
    chunk_size_used:          int


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class SessionChunker:
    """
    Cleans, segments, and formats AstroTalk session messages for LLM
    classification.

    Usage
    -----
    chunker = SessionChunker()
    result  = chunker.process_session(session_dict)
    prompt  = chunker.format_for_prompt(
                  result.chunks[0],
                  result.session_context,
                  language_instruction,
              )
    """

    # ------------------------------------------------------------------
    # Automated-message filter
    # ------------------------------------------------------------------

    AUTO_MESSAGE_PATTERNS: frozenset[str] = frozenset({
        "automated message",
        "chat has started",
        "welcome to astrotalk",
        "consultant will take a minute",
        "this is an automated",
        "chat ended",
        "session ended",
        "payment",
        "coins deducted",
        "minutes remaining",
        "minute remaining",
        "chat will end",
        "free minutes",
        "your balance",
    })

    # Birth detail header pattern — "Hi <Name>, … DOB: … TOB: … POB: …"
    _BIRTH_DETAIL_RE = re.compile(
        r'^Hi\s+\S+.*(?=.*(?:DOB:|Name:))(?=.*(?:TOB:|Gender:))(?=.*POB:)',
        re.IGNORECASE | re.DOTALL,
    )

    # ------------------------------------------------------------------
    # Topic keyword lists for session context summary
    # ------------------------------------------------------------------

    CAREER_KEYWORDS: frozenset[str] = frozenset({
        "job", "career", "naukri", "kaam", "business",
        "promotion", "office", "salary", "interview",
    })

    LOVE_KEYWORDS: frozenset[str] = frozenset({
        "shaadi", "marriage", "love", "pyaar", "boyfriend",
        "girlfriend", "husband", "wife", "relationship",
        "divorce", "breakup", "partner", "rishta",
    })

    HEALTH_KEYWORDS: frozenset[str] = frozenset({
        "health", "bimari", "illness", "doctor",
        "hospital", "pregnancy", "baby", "sehat",
    })

    # Approximate chars-per-token ratio for truncation guard
    _CHARS_PER_TOKEN = 4
    _MAX_CHUNK_TOKENS = 1_800

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def clean_messages(
        self, messages: list[dict[str, Any]]
    ) -> list[ChunkMessage]:
        """
        Remove automated platform messages and birth-detail headers.
        Returns cleaned ChunkMessage list in original order.
        """
        cleaned: list[ChunkMessage] = []

        for m in messages:
            text = m.get("message", "") or ""

            # Auto-message filter — substring match, case-insensitive
            text_lower = text.lower()
            if any(pat in text_lower for pat in self.AUTO_MESSAGE_PATTERNS):
                continue

            # Birth detail header filter
            if self._BIRTH_DETAIL_RE.match(text):
                continue

            cleaned.append(ChunkMessage(
                role=       (m.get("role") or "").upper(),
                message=    text.strip(),
                timestamp=  m.get("timestamp") or "",
                message_id= int(m.get("message_id") or 0),
            ))

        return cleaned

    def chunk_messages(
        self,
        cleaned_messages: list[ChunkMessage],
        chunk_size: int = 15,
        overlap: int = 3,
    ) -> list[Chunk]:
        """
        Produce overlapping chunks from cleaned_messages.

        Sliding window step = chunk_size - overlap.
        Final partial chunk is kept only if it contains >= 3 messages.

        Chunk indices are 1-based; total_chunks is patched in once
        the full count is known.
        """
        if not cleaned_messages:
            return []

        step     = chunk_size - overlap
        windows: list[list[ChunkMessage]] = []
        start    = 0
        n        = len(cleaned_messages)

        while start < n:
            end    = min(start + chunk_size, n)
            window = cleaned_messages[start:end]
            # Keep final partial chunk only if >= 3 messages
            if len(window) >= 3:
                windows.append(window)
            start += step

        total = len(windows)
        chunks: list[Chunk] = []

        for idx, window in enumerate(windows, start=1):
            # Message range labels (1-based display)
            offset = (idx - 1) * step
            first  = offset + 1
            last   = offset + len(window)

            formatted = self._format_chunk(idx, total, first, last, window)

            chunks.append(Chunk(
                chunk_index=              idx,
                total_chunks=             total,
                messages=                 window,
                formatted_text=           formatted,
                message_count=            len(window),
                has_user_messages=        any(m.role == "USER" for m in window),
                has_consultant_messages=  any(m.role == "CONSULTANT" for m in window),
            ))

        return chunks

    def get_session_context(
        self, cleaned_messages: list[ChunkMessage]
    ) -> str:
        """
        Build a 2–3 sentence session context summary from the first
        10 non-empty messages using keyword detection (no LLM call).
        """
        first10 = [m for m in cleaned_messages if m.message.strip()][:10]
        if not first10:
            return (
                "General astrological reading. "
                "User provided birth details and asked open-ended questions."
            )

        # Who initiated
        first_real = first10[0]
        initiator  = "User" if first_real.role == "USER" else "Consultant"
        opener     = first_real.message[:80].replace("\n", " ")

        # Topic detection from combined first-10 text
        combined = " ".join(m.message for m in first10).lower()

        if any(kw in combined for kw in self.CAREER_KEYWORDS):
            topic = "career or professional matters"
        elif any(kw in combined for kw in self.LOVE_KEYWORDS):
            topic = "love, marriage, or relationships"
        elif any(kw in combined for kw in self.HEALTH_KEYWORDS):
            topic = "health or well-being"
        else:
            topic = "general astrological reading"

        # Opening tone
        flirt_signals = {"beautiful", "handsome", "sundar", "cute", "sexy",
                         "attractive", "fond of", "like you", "love you",
                         "bina neend", "desperate"}
        if any(sig in combined for sig in flirt_signals):
            tone = "concerning (early flirtatious signals)"
        elif any(w in combined for w in {"thanks", "thank", "helpful", "good", "nice"}):
            tone = "friendly and appreciative"
        else:
            tone = "professional"

        return (
            f"Session appears to be about {topic}. "
            f"{initiator} initiated with: \"{opener}\". "
            f"Opening tone: {tone}."
        )

    def process_session(self, session: dict[str, Any]) -> SessionChunkResult:
        """
        Master method — full pipeline for one session dict from DataLoader.

        Steps: clean → determine chunk_size → chunk → context summary.
        """
        session_id   = session.get("order_id", 0)
        raw_messages = session.get("messages", [])
        raw_count    = len(raw_messages)

        cleaned      = self.clean_messages(raw_messages)
        cleaned_count = len(cleaned)
        auto_removed  = raw_count - cleaned_count

        # Dynamic chunk size based on cleaned message count
        if cleaned_count > 400:
            chunk_size = 25
        elif cleaned_count >= 150:
            chunk_size = 20
        else:
            chunk_size = 15

        chunks        = self.chunk_messages(cleaned, chunk_size=chunk_size, overlap=3)
        context       = self.get_session_context(cleaned)

        user_count       = sum(1 for m in cleaned if m.role == "USER")
        consultant_count = sum(1 for m in cleaned if m.role == "CONSULTANT")

        return SessionChunkResult(
            session_id=               session_id,
            total_messages_raw=       raw_count,
            total_messages_cleaned=   cleaned_count,
            automated_removed=        auto_removed,
            chunks=                   chunks,
            session_context=          context,
            user_message_count=       user_count,
            consultant_message_count= consultant_count,
            chunk_size_used=          chunk_size,
        )

    def format_for_prompt(
        self,
        chunk: Chunk,
        session_context: str,
        language_instruction: str,
    ) -> str:
        """
        Assemble the full text block for injection into the classifier prompt.
        Truncates chunk body if it would exceed ~1,800 tokens.
        """
        max_body_chars = self._MAX_CHUNK_TOKENS * self._CHARS_PER_TOKEN

        body = chunk.formatted_text
        if len(body) > max_body_chars:
            body = body[:max_body_chars] + "\n[TRUNCATED — chunk too long]"

        return (
            f"SESSION CONTEXT: {session_context}\n"
            f"LANGUAGE: {language_instruction}\n\n"
            f"{body}"
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _format_chunk(
        self,
        idx: int,
        total: int,
        first_msg_num: int,
        last_msg_num: int,
        messages: list[ChunkMessage],
    ) -> str:
        """
        Format a window of messages into the canonical chunk text block.

        [CHUNK 2 of 4 — Messages 13-27]
        USER: …
        CONSULTANT: …
        """
        header = f"[CHUNK {idx} of {total} — Messages {first_msg_num}-{last_msg_num}]"
        lines  = [header]
        for m in messages:
            # Collapse internal newlines to a single space for readability
            text = m.message.replace("\n", " ").strip()
            lines.append(f"{m.role}: {text}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    def load_mod(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod  = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    # Load DataLoader directly to get session dicts
    dl_mod   = load_mod("engine.data_loader", "engine/data_loader.py")
    loader   = dl_mod.DataLoader()
    sessions = loader.load()
    index    = {s["order_id"]: s for s in sessions}

    chunker = SessionChunker()

    # ------------------------------------------------------------------
    # Test 1 — Largest session (294055364, 776 raw messages)
    # ------------------------------------------------------------------
    print("\n" + "="*64)
    print("TEST 1 — Session 294055364 (Moderate, largest session)")
    print("="*64)
    r1 = chunker.process_session(index[294055364])
    print(f"  total_messages_raw     : {r1.total_messages_raw}")
    print(f"  total_messages_cleaned : {r1.total_messages_cleaned}")
    print(f"  automated_removed      : {r1.automated_removed}")
    print(f"  chunk_size_used        : {r1.chunk_size_used}")
    print(f"  total chunks produced  : {len(r1.chunks)}")
    print(f"  user messages          : {r1.user_message_count}")
    print(f"  consultant messages    : {r1.consultant_message_count}")
    print(f"  session_context        :\n    {r1.session_context}")
    t1_pass = len(r1.chunks) >= 25
    print(f"  [{'PASS' if t1_pass else 'FAIL'}] >= 25 chunks expected")

    # ------------------------------------------------------------------
    # Test 2 — Session 296029912 (175 raw messages)
    # ------------------------------------------------------------------
    print("\n" + "="*64)
    print("TEST 2 — Session 296029912 (False Positive, 175 raw)")
    print("="*64)
    r2 = chunker.process_session(index[296029912])
    print(f"  total_messages_raw     : {r2.total_messages_raw}")
    print(f"  total_messages_cleaned : {r2.total_messages_cleaned}")
    print(f"  automated_removed      : {r2.automated_removed}")
    print(f"  chunk_size_used        : {r2.chunk_size_used}")
    print(f"  total chunks produced  : {len(r2.chunks)}")
    print(f"  session_context        :\n    {r2.session_context}")
    print(f"\n  --- Chunk 1 formatted_text ---")
    print(r2.chunks[0].formatted_text)
    t2_pass = len(r2.chunks) >= 1
    print(f"\n  [{'PASS' if t2_pass else 'FAIL'}] at least 1 chunk produced")

    # ------------------------------------------------------------------
    # Test 3 — Shortest session (330762640, 6 raw messages)
    # ------------------------------------------------------------------
    print("\n" + "="*64)
    print("TEST 3 — Shortest session 330762640 (6 raw messages)")
    print("="*64)
    r3 = chunker.process_session(index[330762640])
    print(f"  total_messages_raw     : {r3.total_messages_raw}")
    print(f"  total_messages_cleaned : {r3.total_messages_cleaned}")
    print(f"  automated_removed      : {r3.automated_removed}")
    print(f"  total chunks produced  : {len(r3.chunks)}")
    if r3.chunks:
        print(f"  chunk 1 messages       : {r3.chunks[0].message_count}")
    t3_pass = len(r3.chunks) >= 1
    print(f"  [{'PASS' if t3_pass else 'FAIL'}] at least 1 valid chunk")

    # ------------------------------------------------------------------
    # Test 4 — Overlap verification (session 296029912)
    # ------------------------------------------------------------------
    print("\n" + "="*64)
    print("TEST 4 — Overlap verification (session 296029912)")
    print("="*64)
    if len(r2.chunks) >= 2:
        c1_last3 = r2.chunks[0].messages[-3:]
        c2_first3 = r2.chunks[1].messages[:3]
        print("  Last 3 messages of Chunk 1:")
        for m in c1_last3:
            print(f"    [{m.message_id}] {m.role}: {m.message[:60]!r}")
        print("  First 3 messages of Chunk 2:")
        for m in c2_first3:
            print(f"    [{m.message_id}] {m.role}: {m.message[:60]!r}")
        overlap_ids_1 = [m.message_id for m in c1_last3]
        overlap_ids_2 = [m.message_id for m in c2_first3]
        t4_pass = overlap_ids_1 == overlap_ids_2
        print(f"  IDs match: {overlap_ids_1} == {overlap_ids_2}")
        print(f"  [{'PASS' if t4_pass else 'FAIL'}] overlap is correct")
    else:
        print("  [SKIP] session has fewer than 2 chunks")
        t4_pass = True

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("\n" + "="*64)
    all_pass = t1_pass and t2_pass and t3_pass and t4_pass
    print(f"  Overall: {'ALL PASS' if all_pass else 'SOME TESTS FAILED'}")
    print("="*64)
