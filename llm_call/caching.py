"""
caching.py
==========
Gemini server-side prompt caching for the content-moderation runner.

Holds ONLY the cache lifecycle (create / delete). The per-request model call
lives separately in `gemini_api.py`.

Usage:
    from caching import create_gemini_cache, delete_gemini_cache
"""

from __future__ import annotations

import os

from config import MODEL_ID, API_KEY_ENV


def create_gemini_cache(system_prompt: str) -> str | None:
    """Create a server-side cache holding the system prompt (1h TTL).

    Returns the cache name (passed to `call_gemini_model`), or None on failure.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ.get(API_KEY_ENV))
    try:
        cache = client.caches.create(
            model=MODEL_ID,
            config=types.CreateCachedContentConfig(
                display_name="moderation_gemini-3-flash",
                system_instruction=system_prompt,
                ttl="3600s",
            ),
        )
        return cache.name
    except Exception as exc:
        print(f"    [WARN] Cache creation failed: {exc}")
        return None


def delete_gemini_cache(cache_name: str):
    """Delete the Gemini server-side cache."""
    from google import genai
    client = genai.Client(api_key=os.environ.get(API_KEY_ENV))
    try:
        client.caches.delete(name=cache_name)
    except Exception:
        pass
