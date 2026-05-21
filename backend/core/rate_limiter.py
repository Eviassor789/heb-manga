"""
Async retry utility with exponential backoff.

Designed for the Gemini free tier (15 RPM) but generic enough to wrap any
coroutine that may raise HTTP 429 / quota-exhausted errors.

Delay schedule with default settings (base_delay=60 s, max_delay=120 s, jitter=5 s):

  attempt 1  →  ~60  s
  attempt 2  →  ~120 s  (capped — would be 120 without cap)
  attempt 3  →  ~120 s  (capped — would be 240)
  attempt 4  →  ~120 s  (capped — would be 480)

The base delay of 60 s is aligned with Gemini's 1-minute RPM reset window.
max_delay=120 s keeps total wait time sane for long chapters.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

_DEFAULT_BASE_DELAY  = 60.0    # seconds — matches Gemini's RPM reset window
_DEFAULT_MAX_DELAY   = 120.0   # hard ceiling — never wait longer than this
_DEFAULT_MAX_RETRIES = 5
_DEFAULT_JITTER      = 5.0     # random seconds added to avoid thundering herd


class RateLimitError(Exception):
    """Raised when all retry attempts are exhausted."""


async def call_with_backoff(
    coro_fn: Callable[[], Coroutine[Any, Any, T]],
    *,
    max_retries: int  = _DEFAULT_MAX_RETRIES,
    base_delay:  float = _DEFAULT_BASE_DELAY,
    max_delay:   float = _DEFAULT_MAX_DELAY,
    jitter:      float = _DEFAULT_JITTER,
) -> T:
    """
    Call coro_fn() and retry on 429 / quota errors with exponential backoff.

    Parameters
    ----------
    coro_fn     : Zero-argument callable that returns a fresh coroutine each
                  call — e.g. ``lambda: model.generate_content_async(msg)``.
                  A new coroutine is needed for each attempt because a consumed
                  coroutine cannot be awaited again.
    max_retries : Maximum number of *retry* attempts (not counting the first).
    base_delay  : Seconds for the first retry delay.
    max_delay   : Hard ceiling on any single delay (before jitter).
    jitter      : Maximum random seconds added to each delay to spread load.

    Raises
    ------
    RateLimitError  if all retries are exhausted.
    Any other exception is re-raised immediately without retrying.
    """
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()

        except Exception as exc:
            if not _is_rate_limit(exc):
                raise   # non-429 errors bubble up immediately

            last_exc = exc

            if attempt == max_retries:
                break   # fall through to RateLimitError

            delay = min(base_delay * (2 ** attempt), max_delay) + random.uniform(0, jitter)
            log.warning(
                "[rate_limiter] 429 / quota error (attempt %d/%d). "
                "Retrying in %.0f s …",
                attempt + 1,
                max_retries,
                delay,
            )
            await asyncio.sleep(delay)

    raise RateLimitError(
        f"Gemini rate limit hit on every attempt ({max_retries + 1} total). "
        "Consider reducing the number of pages per job or upgrading to a "
        "paid API tier."
    ) from last_exc


def _is_rate_limit(exc: Exception) -> bool:
    """
    Return True if exc looks like an HTTP 429 / quota-exhausted error.

    The Gemini SDK raises different types across versions:
      • google.api_core.exceptions.ResourceExhausted  (most common)
      • grpc.StatusCode.RESOURCE_EXHAUSTED
      • Plain exceptions whose message contains '429' or 'quota'

    We check both type name and string representation for robustness.
    """
    type_name = type(exc).__name__
    exc_str   = str(exc).lower()

    return (
        type_name in {"ResourceExhausted", "TooManyRequests"}
        or "429"    in exc_str
        or "quota"  in exc_str
        or "rate"   in exc_str
        or "exhausted" in exc_str
    )
