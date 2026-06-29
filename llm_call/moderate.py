"""
moderate.py
===========
Single-model content-moderation runner for AstroTalk sessions.

Calls Gemini 3 Flash on every session in an input CSV and writes the RAW model
response to a JSON file (parsing is a separate step — parser.py / merge.py).
The JSON is saved after EVERY session (atomic write) so an API failure or crash
never loses earlier work, and a re-run resumes by skipping sessions already
present in the file.

There is NO benchmarking and NO CSV merge here — merging the JSON back onto
the source rows is a separate, manually triggered step.

Usage:
    cd llm_call
    python moderate.py --input to_check.csv
    python moderate.py --input to_check.csv --session-id SESS_1001
    python moderate.py --input to_check.csv --session-ids SESS_1001,SESS_1003
    python moderate.py --input to_check.csv --output results.json

The model ID and GOOGLE_API_KEY are hardcoded in each module
(gemini_api.py, caching.py, moderate.py).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

logger = logging.getLogger("moderate")

from prompts import SYSTEM_INSTRUCTION, USER_MESSAGE_TMPL
# MODEL_ID + GOOGLE_API_KEY come from gemini_api so the key lives in ONE place
# (gemini_api.py / caching.py) — no third hardcoded copy to keep in sync.
from gemini_api import call_gemini_model, MODEL_ID, GOOGLE_API_KEY
from caching import create_gemini_cache, delete_gemini_cache

MAX_RETRIES = 2  # API call retries per session


# ═══════════════════════════════════════════════════════════════════════════
#  SECTION 1 — Session loading from CSV
# ═══════════════════════════════════════════════════════════════════════════

def format_session_text(messages: list[dict]) -> str:
    """Format all session messages into a single text block with turn IDs."""
    lines = []
    for msg in messages:
        role = msg.get("role", "USER")
        turn_id = msg.get("message_id", 0)
        text = msg.get("message", "").strip()
        if text:
            lines.append(f"[Turn {turn_id}] {role}: {text}")
    return "\n".join(lines)


def build_sessions_from_csv(input_csv_path: Path) -> dict[int, dict]:
    """Build session dicts from the input CSV.

    Auto-detects column format:
      - Old GT format: speaker, turn_text, turn_id
      - New to_check format: sender, message_text, message_seq, is_automated_message

    Rows with is_automated_message == '1' are SKIPPED (not sent to the LLM).

    session_id is treated as a unique string key (e.g. "SESS_1001" or
    "321490380"); each session groups its multiple message rows together.
    """
    all_sessions: dict[str, dict] = {}

    with open(input_csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = set(reader.fieldnames or [])

        # Detect format
        is_new_format = "message_text" in headers

        for row in reader:
            sid = (row.get("session_id") or "").strip()
            if not sid:
                continue

            # Skip automated messages (only present in new format)
            if row.get("is_automated_message", "0") == "1":
                continue

            if sid not in all_sessions:
                all_sessions[sid] = {"order_id": sid, "messages": []}

            if is_new_format:
                # to_check.csv format
                sender = row.get("sender", "").lower()
                role = "CONSULTANT" if sender == "consultant" else "USER"
                text = re.sub(r"<br\s*/?>", "\n", row.get("message_text", ""))
                text = re.sub(r"<[^>]+>", "", text)
                turn_id = int(row.get("message_seq", 0))
            else:
                # Old GT format (submitted_flags)
                role = "CONSULTANT" if row.get("speaker", "").upper() == "ASTROLOGER" else "USER"
                text = re.sub(r"<br\s*/?>", "\n", row.get("turn_text", ""))
                text = re.sub(r"<[^>]+>", "", text)
                turn_id = int(row.get("turn_id", 0))

            all_sessions[sid]["messages"].append({
                "role": role,
                "message": text,
                "timestamp": "",
                "message_id": turn_id,
            })

    # Sort messages by turn_id
    for sess in all_sessions.values():
        sess["messages"].sort(key=lambda m: m["message_id"])

    return all_sessions


# ═══════════════════════════════════════════════════════════════════════════
#  SECTION 2 — Incremental JSON results store
# ═══════════════════════════════════════════════════════════════════════════

def load_results(output_path: Path) -> dict[str, dict]:
    """Load existing results (keyed by session_id as string), or empty dict."""
    if output_path.exists():
        try:
            with open(output_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            print(f"  [WARN] Could not read existing {output_path.name}; starting fresh.")
    return {}


def save_results(output_path: Path, results: dict[str, dict]):
    """Atomically write results to disk (temp file + replace)."""
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, output_path)


# ═══════════════════════════════════════════════════════════════════════════
#  SECTION 3 — Per-session moderation
# ═══════════════════════════════════════════════════════════════════════════

def moderate_session(
    sid: int,
    session: dict,
    cache_name: str | None,
) -> dict:
    """Call Gemini on one session and return a RAW result entry.

    The entry always carries a `status`:
      - "raw"         the call succeeded; raw_response holds the model output
      - "api_error"   the API call failed after retries
    """
    messages = session.get("messages", [])
    user_prompt = USER_MESSAGE_TMPL.format(
        session_id=sid,
        num_messages=len(messages),
        session_text=format_session_text(messages),
    )

    raw_response = ""
    input_tokens = output_tokens = cached_tokens = thinking_tokens = 0
    latency = 0.0
    api_error = ""

    for attempt in range(1, MAX_RETRIES + 1):
        start = time.perf_counter()
        try:
            raw_response, in_tok, out_tok, cache_tok, think_tok = call_gemini_model(
                user_prompt, cache_name
            )
            latency = time.perf_counter() - start
            input_tokens, output_tokens, cached_tokens, thinking_tokens = (
                in_tok, out_tok, cache_tok, think_tok
            )
            api_error = ""
            break
        except Exception as exc:
            latency = time.perf_counter() - start
            api_error = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_RETRIES:
                time.sleep(2.0)

    # Raw-only output — parsing is a separate step (parser.py / merge.py).
    return {
        "session_id": sid,
        "num_messages": len(messages),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "thinking_tokens": thinking_tokens,
        "cache_id": cache_name,
        "latency_s": round(latency, 2),
        "status": "api_error" if api_error else "raw",
        "error": api_error,
        "raw_response": raw_response,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  SECTION 4 — Main entry point
# ═══════════════════════════════════════════════════════════════════════════

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(message)s")
    # Silence noisy third-party HTTP/SDK logs (else they bury per-session output).
    for _noisy in ("google_genai", "google", "httpx", "httpcore", "urllib3"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
    run_start = time.perf_counter()

    parser = argparse.ArgumentParser(
        description="Gemini 3 Flash content-moderation runner (JSON output)"
    )
    parser.add_argument(
        "--input", type=str, required=True,
        help="Path to the input CSV of session messages.",
    )
    parser.add_argument(
        "--output", type=str, default="moderation_sequential.json",
        help="Path to the JSON results file (default: moderation_sequential.json).",
    )
    parser.add_argument(
        "--session-id", type=str, default=None,
        help="Single session ID to moderate (e.g. SESS_1001).",
    )
    parser.add_argument(
        "--session-ids", type=str, default=None,
        help="Comma-separated session IDs (e.g. SESS_1001,SESS_1003).",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = Path(__file__).parent / input_path
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = Path(__file__).parent / output_path

    if not input_path.exists():
        print(f"  [X] Input CSV not found: {input_path}")
        sys.exit(1)

    # API key check (key is hardcoded in gemini_api.py / caching.py)
    if not GOOGLE_API_KEY or GOOGLE_API_KEY.startswith("PASTE_"):
        print("  [X] GOOGLE_API_KEY not set (edit gemini_api.py and caching.py).")
        sys.exit(1)

    # ── Load sessions ─────────────────────────────────────────────────
    print(f"\n  Building sessions from {input_path.name}...")
    all_sessions = build_sessions_from_csv(input_path)
    print(f"  Sessions in CSV: {len(all_sessions)}")

    # ── Determine which session IDs to run ────────────────────────────
    if args.session_ids:
        wanted = [s.strip() for s in args.session_ids.split(",") if s.strip()]
    elif args.session_id:
        wanted = [args.session_id.strip()]
    else:
        wanted = list(all_sessions.keys())

    session_ids = [sid for sid in wanted if sid in all_sessions]
    if not session_ids:
        print("  [X] No valid session IDs found in the CSV!")
        sys.exit(1)

    # ── Resume: skip sessions already in the results file ─────────────
    results = load_results(output_path)
    todo = [sid for sid in session_ids if str(sid) not in results]
    skipped = len(session_ids) - len(todo)

    print("=" * 90)
    print("  GEMINI 3 FLASH CONTENT MODERATION")
    print(f"  Model: {MODEL_ID}")
    print(f"  Output: {output_path.name}")
    print(f"  To run: {len(todo)} sessions"
          + (f"  (resuming — {skipped} already done)" if skipped else ""))
    print("=" * 90)

    if not todo:
        print("\n  Nothing to do — all requested sessions already in results.")
        return

    # ── Create server-side cache for the system prompt (required) ─────
    print("  Creating cache...", end=" ", flush=True)
    cache_name = create_gemini_cache(SYSTEM_INSTRUCTION)
    if not cache_name:
        print("FAILED")
        print("  [X] Cache creation failed — caching is required, aborting.")
        sys.exit(1)
    print("OK")

    # ── Run ───────────────────────────────────────────────────────────
    try:
        for i, sid in enumerate(todo, 1):
            print(f"  [{i}/{len(todo)}] Session {sid}...", end=" ", flush=True)

            entry = moderate_session(sid, all_sessions[sid], cache_name)

            # Persist immediately after every single session (crash-safe).
            results[str(sid)] = entry
            save_results(output_path, results)

            cache_str = (f"cache={entry['cached_tokens']}"
                         if entry.get("cached_tokens") else "no-cache")
            if entry["status"] == "raw":
                print(f"{entry['output_tokens']} out tok | {entry['latency_s']}s | {cache_str}")
            else:
                print(f"{entry['status'].upper()}: {entry.get('error', '')[:60]}")

            time.sleep(0.5)  # gentle rate limit
    finally:
        if cache_name:
            delete_gemini_cache(cache_name)

    # ── Summary ───────────────────────────────────────────────────────
    ran = [results[str(sid)] for sid in todo]
    ok = sum(1 for e in ran if e["status"] == "raw")
    failed = sum(1 for e in ran if e["status"] != "raw")
    elapsed = time.perf_counter() - run_start
    print("\n" + "=" * 90)
    print(f"  Done. raw: {ok} | failed: {failed}")
    print(f"  Results: {output_path}")
    print("=" * 90)
    logger.info("Total run time: %.1fs (%.2f min) for %d session(s) — avg %.2fs/session",
                elapsed, elapsed / 60, len(todo), elapsed / max(len(todo), 1))


if __name__ == "__main__":
    main()
