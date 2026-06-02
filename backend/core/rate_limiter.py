"""
Async retry utility with exponential backoff.

Handles two distinct Gemini error categories:

  429 / quota-exhausted  — rate limit; base_delay=60 s (aligned with Gemini's
                           1-minute RPM window).
  503 / UNAVAILABLE      — server overload spike; base_delay=15 s (spikes
                           usually resolve within 30–60 s).

Delay schedule for 429 (base=60 s, max=120 s, jitter=5 s):
  attempt 1  →  ~60 s
  attempt 2  →  ~120 s  (capped)
  attempt 3+ →  ~120 s  (capped)

Delay schedule for 503 (base=15 s, max=120 s, jitter=5 s):
  attempt 1  →  ~15 s
  attempt 2  →  ~30 s
  attempt 3  →  ~60 s
  attempt 4  →  ~120 s  (capped)
  attempt 5+ →  ~120 s  (capped)
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

_DEFAULT_BASE_DELAY         = 60.0    # seconds for 429 rate-limit errors
_DEFAULT_SERVER_BASE_DELAY  = 15.0    # seconds for 503 server-overload errors
_DEFAULT_MAX_DELAY          = 120.0   # hard ceiling for all retries
_DEFAULT_MAX_RETRIES        = 5
_DEFAULT_JITTER             = 5.0     # random seconds added to avoid thundering herd


class RateLimitError(Exception):
    """Raised when all retry attempts are exhausted."""


async def call_with_backoff(
    coro_fn: Callable[[], Coroutine[Any, Any, T]],
    *,
    max_retries:       int   = _DEFAULT_MAX_RETRIES,
    base_delay:        float = _DEFAULT_BASE_DELAY,
    server_base_delay: float = _DEFAULT_SERVER_BASE_DELAY,
    max_delay:         float = _DEFAULT_MAX_DELAY,
    jitter:            float = _DEFAULT_JITTER,
) -> T:
    """
    Call coro_fn() and retry on transient Gemini errors with exponential backoff.

    Retries on:
      • 429 / quota-exhausted  — uses base_delay (default 60 s)
      • 503 / UNAVAILABLE      — uses server_base_delay (default 15 s)

    All other exceptions are re-raised immediately without retrying.

    Parameters
    ----------
    coro_fn           : Zero-argument callable returning a fresh coroutine each call.
    max_retries       : Maximum *retry* attempts (not counting the first call).
    base_delay        : First-retry delay for 429 rate-limit errors.
    server_base_delay : First-retry delay for 503 server-overload errors.
    max_delay         : Hard ceiling on any single delay (before jitter).
    jitter            : Max random seconds added per delay to spread load.

    Raises
    ------
    RateLimitError  if all retries are exhausted.
    """
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()

        except Exception as exc:
            is_rate   = _is_rate_limit(exc)
            is_server = _is_server_overloaded(exc)

            if not is_rate and not is_server:
                raise   # unrecognised error — bubble up immediately

            last_exc = exc

            if attempt == max_retries:
                break   # fall through to RateLimitError

            effective_base = base_delay if is_rate else server_base_delay
            delay = min(effective_base * (2 ** attempt), max_delay) + random.uniform(0, jitter)
            kind  = "429/quota" if is_rate else "503/unavailable"
            log.warning(
                "[rate_limiter] %s (attempt %d/%d). Retrying in %.0f s …",
                kind, attempt + 1, max_retries, delay,
            )
            await asyncio.sleep(delay)

    raise RateLimitError(
        f"Gemini API unavailable after {max_retries + 1} attempts. "
        "This is usually a temporary spike — try resuming the job from the "
        "Translate step in the job progress page."
    ) from last_exc


def _is_rate_limit(exc: Exception) -> bool:
    """Return True for HTTP 429 / quota-exhausted errors."""
    type_name = type(exc).__name__
    exc_str   = str(exc).lower()
    return (
        type_name in {"ResourceExhausted", "TooManyRequests"}
        or "429"       in exc_str
        or "quota"     in exc_str
        or "rate"      in exc_str
        or "exhausted" in exc_str
    )


def _is_server_overloaded(exc: Exception) -> bool:
    """Return True for HTTP 503 / ServiceUnavailable (temporary high-demand) errors."""
    type_name = type(exc).__name__
    exc_str   = str(exc).lower()
    return (
        type_name in {"ServiceUnavailable", "Unavailable"}
        or "503"         in exc_str
        or "unavailable" in exc_str
        or "high demand" in exc_str
        or "overloaded"  in exc_str
    )
