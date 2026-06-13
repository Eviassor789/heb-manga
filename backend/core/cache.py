"""
Tiny async in-memory TTL cache.

Used to cache responses from external scrape/search endpoints (WeebCentral,
MangaDex) for a few minutes so that:

  • repeated visitors to the discover/home page don't trigger a fresh upstream
    request every time (which would risk WeebCentral/MangaDex rate-limiting or
    IP-blocking our server), and
  • the discover/home page feels instant on a warm cache.

Single-instance only — the cache lives in process memory. This is intentional:
the whole backend is designed to run as ONE long-lived process (see
DEPLOYMENT.md). If you ever scale to multiple instances, swap this for Redis.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class TTLCache:
    """Async cache with per-key TTL and single-flight stampede protection."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, object]] = {}   # key -> (expires_at, value)
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get_or_set(
        self,
        key: str,
        ttl: float,
        factory: Callable[[], Awaitable[T]],
    ) -> T:
        """
        Return the cached value for `key` if it's still fresh, otherwise call
        `factory()` (awaited), store its result for `ttl` seconds, and return it.

        If `factory()` raises, nothing is cached and the exception propagates —
        so transient upstream failures don't get pinned in the cache.

        Concurrent callers for the same cold key wait on a per-key lock, so the
        upstream request fires exactly once (no thundering herd).
        """
        now = time.monotonic()
        hit = self._data.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]  # type: ignore[return-value]

        # Acquire (or create) a per-key lock so only one caller does the work.
        async with self._guard:
            lock = self._locks.setdefault(key, asyncio.Lock())

        async with lock:
            # Re-check: another caller may have populated it while we waited.
            now = time.monotonic()
            hit = self._data.get(key)
            if hit is not None and hit[0] > now:
                return hit[1]  # type: ignore[return-value]

            value = await factory()
            self._data[key] = (now + ttl, value)
            return value

    def invalidate(self, key: str) -> None:
        """Drop a single key (e.g. after a write that changes its result)."""
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()
