"""
gemini_api.py
=============
Gemini API caller for the content-moderation runner.

Holds ONLY the per-request model call. Server-side prompt caching lives
separately in `caching.py`.

Usage:
    from gemini_api import call_gemini_model
"""

from __future__ import annotations

import os

from config import MODEL_ID, API_KEY_ENV, MAX_OUTPUT_TOKENS


def call_gemini_model(
    system_prompt: str,
    user_prompt: str,
    cache_name: str | None = None,
) -> tuple[str, int, int, int]:
    """Call Gemini via google-genai SDK.

    When `cache_name` is given the cached system prompt is used; otherwise the
    system prompt is sent inline. Returns:
        (response_text, input_tokens, output_tokens, cached_tokens)
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ.get(API_KEY_ENV))

    if cache_name:
        gen_config = types.GenerateContentConfig(
            cached_content=cache_name,
            temperature=0,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
    else:
        gen_config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )

    response = client.models.generate_content(
        model=MODEL_ID,
        contents=user_prompt,
        config=gen_config,
    )

    usage = response.usage_metadata
    input_tokens = getattr(usage, "prompt_token_count", 0) or 0
    output_tokens = getattr(usage, "candidates_token_count", 0) or 0
    cached_tokens = getattr(usage, "cached_content_token_count", 0) or 0

    text = ""
    if response.candidates:
        candidate = response.candidates[0]
        if hasattr(candidate, "content") and candidate.content and candidate.content.parts:
            for part in candidate.content.parts:
                if hasattr(part, "text") and part.text:
                    text += part.text
    if not text:
        try:
            text = response.text or ""
        except (ValueError, TypeError, AttributeError):
            text = ""

    return text, input_tokens, output_tokens, cached_tokens
