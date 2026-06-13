"""
Lightweight per-IP sliding-window rate limiter (FastAPI dependency).

Protects the public, no-auth endpoints (WeebCentral scrape/search, library
reads) from abuse. The main risk is NOT our wallet (translation cost is BYOK)
but getting our server's IP throttled or blocked by WeebCentral / MangaDex /
Cloudflare when someone hammers the scrape endpoints.

In-memory, single-instance only — matches the rest of the backend (one
long-lived process). For multi-instance scaling, move this to Redis.

Usage:

    from core.ratelimit import RateLimiter
    scrape_rl = RateLimiter(calls=30, window=60)   # 30 requests / minute / IP

    @app.get("/api/scrape", dependencies=[Depends(scrape_rl)])
    async def scrape(): ...
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request


def client_ip(request: Request) -> str:
    """
    Best-effort real client IP.

    Behind a reverse proxy / platform load balancer the socket peer is the
    proxy, so prefer the left-most X-Forwarded-For entry (the original client).
    Falls back to the direct socket peer, then to a constant bucket.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """
    A callable FastAPI dependency enforcing `calls` requests per `window`
    seconds per client IP, using a sliding window of recent timestamps.
    """

    def __init__(self, calls: int, window: float) -> None:
        self.calls = max(1, calls)
        self.window = float(window)
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def __call__(self, request: Request) -> None:
        ip = client_ip(request)
        now = time.monotonic()
        cutoff = now - self.window

        dq = self._hits[ip]
        while dq and dq[0] < cutoff:
            dq.popleft()

        if len(dq) >= self.calls:
            retry_after = int(dq[0] + self.window - now) + 1
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please slow down and try again shortly.",
                headers={"Retry-After": str(max(1, retry_after))},
            )

        dq.append(now)

        # Opportunistic cleanup so the dict doesn't grow unbounded with the
        # number of distinct IPs seen over the process lifetime.
        if not dq:
            self._hits.pop(ip, None)
        elif len(self._hits) > 4096:
            self._prune(cutoff)

    def _prune(self, cutoff: float) -> None:
        """Drop IP buckets whose most recent hit is older than the window."""
        stale = [ip for ip, dq in self._hits.items() if not dq or dq[-1] < cutoff]
        for ip in stale:
            self._hits.pop(ip, None)
