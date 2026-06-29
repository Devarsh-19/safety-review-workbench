"""
gemini_api.py
=============
Gemini API caller for the content-moderation runner.

Holds ONLY the per-request model call (and its safety settings), in both a
synchronous and an asynchronous form. Server-side prompt caching lives
separately in `caching.py`.

Usage:
    from gemini_api import call_gemini_model          # sync
    from gemini_api import call_gemini_model_async     # async (for batch.py)
"""

from __future__ import annotations

# Hardcoded model + key (no config module).
# WARNING: do not commit a real key — this file is tracked in git.
# WARNING: MODEL_ID and GOOGLE_API_KEY are ALSO hardcoded in caching.py. The
#          prompt cache is created in caching.py and used here — these MUST
#          match in both files, or every cached call fails with a mismatch.
MODEL_ID = "gemini-3-flash-preview"
GOOGLE_API_KEY = "PASTE_YOUR_GOOGLE_API_KEY_HERE"
MAX_OUTPUT_TOKENS = 16384


def _gemini_safety_settings():
    """Return safety settings that disable all content filtering."""
    from google.genai import types
    return [
        types.SafetySetting(category=cat, threshold="OFF")
        for cat in [
            "HARM_CATEGORY_HARASSMENT",
            "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "HARM_CATEGORY_DANGEROUS_CONTENT",
            "HARM_CATEGORY_CIVIC_INTEGRITY",
        ]
    ]


def _gen_config(cache_name: str):
    """Build the GenerateContentConfig for a cached, safety-off call.

    thinking_budget=0 disables the model's internal "thinking" — this is a
    structured classification task that needs no reasoning, and thinking tokens
    are billed at the output rate (~1000+ tokens/call otherwise, all wasted).
    """
    from google.genai import types
    return types.GenerateContentConfig(
        cached_content=cache_name,
        temperature=0,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        safety_settings=_gemini_safety_settings(),
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )


def _extract(response) -> tuple[str, int, int, int]:
    """Pull text + token usage out of a generate_content response."""
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


def call_gemini_model(
    user_prompt: str,
    cache_name: str,
) -> tuple[str, int, int, int]:
    """Call Gemini (synchronous) using the cached system prompt.

    The system instruction is supplied via the required server-side cache
    (`cache_name`, created by `caching.create_gemini_cache`). Returns:
        (response_text, input_tokens, output_tokens, cached_tokens)
    """
    from google import genai

    client = genai.Client(api_key=GOOGLE_API_KEY)
    response = client.models.generate_content(
        model=MODEL_ID,
        contents=user_prompt,
        config=_gen_config(cache_name),
    )
    return _extract(response)


async def call_gemini_model_async(
    user_prompt: str,
    cache_name: str,
) -> tuple[str, int, int, int]:
    """Call Gemini (asynchronous) using the cached system prompt.

    Same contract as `call_gemini_model`, but awaitable — used by the async
    batch runner in `batch.py`. Returns:
        (response_text, input_tokens, output_tokens, cached_tokens)
    """
    from google import genai

    client = genai.Client(api_key=GOOGLE_API_KEY)
    response = await client.aio.models.generate_content(
        model=MODEL_ID,
        contents=user_prompt,
        config=_gen_config(cache_name),
    )
    return _extract(response)
