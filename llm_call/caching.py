"""
caching.py
==========
Gemini server-side prompt caching for the content-moderation runner.

Holds the cache lifecycle (create / delete), an async-safe `CacheManager` that
recreates the cache when its TTL is about to (or has) expired, and a helper to
recognise cache-expiry errors. The per-request model call lives separately in
`gemini_api.py`.

Usage:
    from caching import create_gemini_cache, delete_gemini_cache
    from caching import CacheManager, SyncCacheManager, is_cache_expired_error
"""

from __future__ import annotations

import asyncio
import time

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))
import config

# WARNING: do not commit a real key — this file is tracked in git.
MODEL_ID = config.GEMINI_MODEL
GOOGLE_API_KEY = "PASTE_YOUR_GOOGLE_API_KEY_HERE"

CACHE_TTL_SECONDS = 3600        # how long the server keeps the cache
CACHE_SAFETY_MARGIN = 300       # recreate this many seconds before TTL


def create_gemini_cache(system_prompt: str) -> str | None:
    """Create a server-side cache holding the system prompt.

    Returns the cache name (passed to `call_gemini_model`), or None on failure.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GOOGLE_API_KEY)
    try:
        cache = client.caches.create(
            model=MODEL_ID,
            config=types.CreateCachedContentConfig(
                display_name="moderation_gemini-3-flash",
                system_instruction=system_prompt,
                ttl=f"{CACHE_TTL_SECONDS}s",
            ),
        )
        return cache.name
    except Exception as exc:
        print(f"    [WARN] Cache creation failed: {exc}")
        return None


def delete_gemini_cache(cache_name: str):
    """Delete the Gemini server-side cache."""
    from google import genai
    client = genai.Client(api_key=GOOGLE_API_KEY)
    try:
        client.caches.delete(name=cache_name)
    except Exception:
        pass


def is_cache_expired_error(exc: Exception) -> bool:
    """Best-effort: does this exception mean the cached content is gone/expired?

    Gemini raises generic client errors; we look for a cache-related resource
    combined with a not-found / invalid / permission signal so we only trigger
    a recreation for genuine expiry, not for every 4xx.
    """
    msg = str(exc).lower()
    mentions_cache = ("cachedcontent" in msg or "cached content" in msg
                      or "cached_content" in msg or "cache" in msg)
    if not mentions_cache:
        return False
    return any(s in msg for s in (
        "not found", "not_found", "expired", "invalid", "permission",
        "does not exist", "404", "403", "400",
    ))


class CacheManager:
    """Async-safe holder for the current prompt cache.

    - Proactive refresh: hands out a cache, recreating it first if it is within
      CACHE_SAFETY_MARGIN of its TTL.
    - Reactive refresh: `refresh(stale_name)` recreates the cache ONCE even if
      many coroutines hit the expiry at the same time (the others see that the
      name already changed and reuse the fresh one).
    - Tracks every cache name created so they can all be deleted at the end.
    - Aborts if too many reactive recreations happen without a successful call
      in between (guards against an error that is misread as expiry).
    """

    def __init__(self, system_prompt: str, max_reactive_streak: int = 6):
        self.system_prompt = system_prompt
        self.max_reactive_streak = max_reactive_streak
        self._lock = asyncio.Lock()
        self._cache_name: str | None = None
        self._created_at = 0.0
        self._reactive_streak = 0
        self.created_caches: list[str] = []
        self.refresh_count = 0

    async def _create_locked(self) -> str:
        name = await asyncio.to_thread(create_gemini_cache, self.system_prompt)
        if not name:
            raise RuntimeError("Cache creation failed (model may not support caching)")
        self._cache_name = name
        self._created_at = time.monotonic()
        self.created_caches.append(name)
        return name

    async def get_cache(self) -> str:
        """Return a usable cache name, refreshing proactively near TTL."""
        async with self._lock:
            stale = (
                self._cache_name is None
                or (time.monotonic() - self._created_at)
                >= (CACHE_TTL_SECONDS - CACHE_SAFETY_MARGIN)
            )
            if stale:
                if self._cache_name is not None:
                    self.refresh_count += 1
                    print(f"    [cache] proactive refresh (#{self.refresh_count})")
                await self._create_locked()
            return self._cache_name  # type: ignore[return-value]

    async def refresh(self, stale_name: str | None) -> str:
        """Reactively recreate the cache after an expiry error.

        No-op (returns the current name) if another coroutine already refreshed
        past `stale_name`.
        """
        async with self._lock:
            if self._cache_name is not None and self._cache_name != stale_name:
                return self._cache_name  # someone else already refreshed
            self._reactive_streak += 1
            if self._reactive_streak > self.max_reactive_streak:
                raise RuntimeError(
                    f"Cache recreated {self._reactive_streak} times with no "
                    f"successful call — aborting (error likely not real expiry)."
                )
            self.refresh_count += 1
            print(f"    [cache] reactive refresh (#{self.refresh_count})")
            await self._create_locked()
            return self._cache_name  # type: ignore[return-value]

    def note_success(self):
        """Reset the reactive streak after any successful call."""
        self._reactive_streak = 0

    async def cleanup(self):
        """Delete every cache this manager created."""
        for name in self.created_caches:
            await asyncio.to_thread(delete_gemini_cache, name)


class SyncCacheManager:
    """Synchronous counterpart to `CacheManager` for the (sync) moderate runner.

    Single-threaded, so no lock or refresh deduplication is needed. Provides the
    same two refresh paths:
      - Proactive: `get_cache()` recreates the cache when it is within
        CACHE_SAFETY_MARGIN of its TTL.
      - Reactive:  `refresh()` recreates the cache after a cache-expiry error,
        with a streak guard so a misclassified error can't trigger endless
        recreation.
    Tracks every cache name created so they can all be deleted at the end.
    """

    def __init__(self, system_prompt: str, max_reactive_streak: int = 6):
        self.system_prompt = system_prompt
        self.max_reactive_streak = max_reactive_streak
        self._cache_name: str | None = None
        self._created_at = 0.0
        self._reactive_streak = 0
        self.created_caches: list[str] = []
        self.refresh_count = 0

    def _create(self) -> str:
        name = create_gemini_cache(self.system_prompt)
        if not name:
            raise RuntimeError("Cache creation failed (model may not support caching)")
        self._cache_name = name
        self._created_at = time.monotonic()
        self.created_caches.append(name)
        return name

    def get_cache(self) -> str:
        """Return a usable cache name, recreating proactively near TTL."""
        stale = (
            self._cache_name is None
            or (time.monotonic() - self._created_at)
            >= (CACHE_TTL_SECONDS - CACHE_SAFETY_MARGIN)
        )
        if stale:
            if self._cache_name is not None:
                self.refresh_count += 1
                print(f"    [cache] proactive refresh (#{self.refresh_count})")
            self._create()
        return self._cache_name  # type: ignore[return-value]

    def refresh(self) -> str:
        """Reactively recreate the cache after an expiry error.

        Raises if recreated more than `max_reactive_streak` times without a
        successful call in between (guards against an error misread as expiry).
        Call `note_success()` after any successful call to reset the streak.
        """
        self._reactive_streak += 1
        if self._reactive_streak > self.max_reactive_streak:
            raise RuntimeError(
                f"Cache recreated {self._reactive_streak} times with no "
                f"successful call — aborting (error likely not real expiry)."
            )
        self.refresh_count += 1
        print(f"    [cache] reactive refresh (#{self.refresh_count})")
        self._create()
        return self._cache_name  # type: ignore[return-value]

    def note_success(self):
        """Reset the reactive streak after any successful call."""
        self._reactive_streak = 0

    def cleanup(self):
        """Delete every cache this manager created."""
        for name in self.created_caches:
            delete_gemini_cache(name)
