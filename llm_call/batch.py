"""
batch.py
========
Async, concurrent Gemini 3 Flash moderation runner.

Sends many session requests at once (bounded by a semaphore) and keeps running
across prompt-cache TTL expiry: the cache is recreated automatically (proactively
before TTL, reactively on an expiry error) and every cache created is cleaned up
at the end.

Like the notebook's Section 3, this collects RAW responses only — parsing stays
a separate step (parser.py / notebook Section 4). Output is written after EVERY
session (atomic) so an expiry, rate-limit, or crash never loses earlier work.
Re-running resumes: a session counts as done only when its stored status is
"raw" (call succeeded); failed sessions are retried.

Usage (CLI):
    cd llm_call
    python batch.py --input to_check.csv
    python batch.py --input to_check.csv --concurrency 5
    python batch.py --input to_check.csv --session-ids SESS_1001,SESS_1003

Usage (notebook):
    from batch import run_batch
    results = await run_batch(Path("to_check.csv"), Path("moderation_raw.json"))
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from pathlib import Path

from prompts import SYSTEM_INSTRUCTION, USER_MESSAGE_TMPL
from gemini_api import call_gemini_model_async, GOOGLE_API_KEY
from caching import CacheManager, is_cache_expired_error
from moderate import (
    build_sessions_from_csv,
    format_session_text,
    load_results,
    save_results,
)

CONCURRENCY = 5          # max in-flight requests
MAX_TRANSIENT_ATTEMPTS = 4   # retries for 429 / 5xx per session
MAX_CACHE_RETRIES = 6        # cache-expiry retries per session (separate budget)


async def _moderate_one(
    sid: str,
    session: dict,
    manager: CacheManager,
    sem: asyncio.Semaphore,
    results: dict,
    results_lock: asyncio.Lock,
    output_path: Path,
    progress: dict,
) -> None:
    """Call one session, with cache-expiry + transient retries, then persist."""
    async with sem:
        messages = session.get("messages", [])
        user_prompt = USER_MESSAGE_TMPL.format(
            session_id=sid,
            num_messages=len(messages),
            session_text=format_session_text(messages),
        )

        raw_text = ""
        in_tok = out_tok = cache_tok = think_tok = 0
        latency = 0.0
        err = ""
        transient = 0
        cache_retries = 0
        cache_name = None

        while True:
            cache_name = await manager.get_cache()
            start = time.perf_counter()
            try:
                raw_text, in_tok, out_tok, cache_tok, think_tok = await call_gemini_model_async(
                    user_prompt, cache_name
                )
                latency = time.perf_counter() - start
                manager.note_success()
                err = ""
                break
            except Exception as exc:
                latency = time.perf_counter() - start
                err = f"{type(exc).__name__}: {exc}"

                # 1) Cache expired -> recreate and retry (own budget)
                if is_cache_expired_error(exc):
                    cache_retries += 1
                    if cache_retries > MAX_CACHE_RETRIES:
                        break
                    try:
                        await manager.refresh(cache_name)
                    except Exception as rexc:
                        err = f"CacheRefreshFailed: {rexc}"
                        break
                    continue

                # 2) Transient (rate-limit / 5xx) -> backoff and retry
                transient += 1
                if transient >= MAX_TRANSIENT_ATTEMPTS:
                    break
                await asyncio.sleep(min(2 ** transient, 30) + random.random())

        entry = {
            "session_id": sid,
            "num_messages": len(messages),
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cached_tokens": cache_tok,
            "thinking_tokens": think_tok,
            "cache_id": cache_name,
            "latency_s": round(latency, 2),
            "status": "api_error" if err else "raw",
            "error": err,
            "raw_response": raw_text,
        }

        # Persist immediately after every session (serialised, atomic)
        async with results_lock:
            results[str(sid)] = entry
            save_results(output_path, results)
            progress["done"] += 1
            i = progress["done"]

        n = progress["total"]
        if err:
            print(f"    [{i}/{n}] {sid}: ERROR {err[:60]}")
        else:
            print(f"    [{i}/{n}] {sid}: {out_tok} out tok | cache={cache_tok} | {latency:.1f}s")


async def run_batch(
    input_path: Path,
    output_path: Path,
    session_ids: list[str] | None = None,
    concurrency: int = CONCURRENCY,
) -> dict:
    """Run the async batch. Returns the full results dict (raw responses)."""
    all_sessions = build_sessions_from_csv(input_path)
    ids = session_ids if session_ids else list(all_sessions.keys())
    ids = [sid for sid in ids if sid in all_sessions]
    if not ids:
        print("  [X] No valid session IDs found in the CSV!")
        return {}

    # Resume: a session is done only if its stored call succeeded ("raw").
    results = load_results(output_path)
    todo = [sid for sid in ids if results.get(str(sid), {}).get("status") != "raw"]
    skipped = len(ids) - len(todo)

    print("=" * 90)
    print("  GEMINI 3 FLASH — ASYNC BATCH MODERATION")
    print(f"  Output: {output_path.name} | concurrency: {concurrency}")
    print(f"  To run: {len(todo)} sessions"
          + (f"  (resuming — {skipped} already done)" if skipped else ""))
    print("=" * 90)
    if not todo:
        print("  Nothing to do.")
        return results

    manager = CacheManager(SYSTEM_INSTRUCTION)
    # Fail fast if caching is unsupported / key invalid.
    await manager.get_cache()

    sem = asyncio.Semaphore(concurrency)
    results_lock = asyncio.Lock()
    progress = {"done": 0, "total": len(todo)}

    try:
        tasks = [
            asyncio.create_task(
                _moderate_one(sid, all_sessions[sid], manager, sem,
                              results, results_lock, output_path, progress)
            )
            for sid in todo
        ]
        await asyncio.gather(*tasks)
    finally:
        await manager.cleanup()

    ok = sum(1 for sid in todo if results.get(str(sid), {}).get("status") == "raw")
    failed = len(todo) - ok
    print("\n" + "=" * 90)
    print(f"  Done. raw: {ok} | failed: {failed} | cache refreshes: {manager.refresh_count}")
    print(f"  Results: {output_path}")
    print("=" * 90)
    return results


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Async batched Gemini 3 Flash moderation (raw output)"
    )
    parser.add_argument("--input", type=str, required=True,
                        help="Path to the input CSV of session messages.")
    parser.add_argument("--output", type=str, default="moderation_raw.json",
                        help="Raw results JSON (default: moderation_raw.json).")
    parser.add_argument("--session-id", type=str, default=None,
                        help="Single session ID (e.g. SESS_1001).")
    parser.add_argument("--session-ids", type=str, default=None,
                        help="Comma-separated session IDs.")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY,
                        help=f"Max in-flight requests (default: {CONCURRENCY}).")
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
    if not GOOGLE_API_KEY or GOOGLE_API_KEY.startswith("PASTE_"):
        print("  [X] GOOGLE_API_KEY not set (edit gemini_api.py, caching.py).")
        sys.exit(1)

    if args.session_ids:
        wanted = [s.strip() for s in args.session_ids.split(",") if s.strip()]
    elif args.session_id:
        wanted = [args.session_id.strip()]
    else:
        wanted = None

    asyncio.run(run_batch(input_path, output_path, wanted, args.concurrency))


if __name__ == "__main__":
    main()
