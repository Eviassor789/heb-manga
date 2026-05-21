from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Callable, Coroutine, Any

# Type alias for the emit callable passed into pipeline modules
EmitFn = Callable[[dict], Coroutine[Any, Any, None]]

_TERMINAL_STAGES = {"done", "error"}


class JobManager:
    """
    In-process SSE event bus.

    Each job has:
    - An event history (so clients that connect after events are emitted get a replay).
    - A set of live subscriber queues (one per active SSE connection).

    Thread-safety: all access is via asyncio — no locks needed.
    """

    def __init__(self) -> None:
        self._history: dict[str, list[dict]] = {}
        self._subscribers: dict[str, list[asyncio.Queue[dict | None]]] = {}
        self._done: dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def register_job(self, job_id: str) -> None:
        self._history[job_id] = []
        self._subscribers[job_id] = []
        self._done[job_id] = False

    def remove_job(self, job_id: str) -> None:
        self._history.pop(job_id, None)
        self._done.pop(job_id, None)
        for q in self._subscribers.pop(job_id, []):
            q.put_nowait(None)  # unblock any waiting subscriber

    # ------------------------------------------------------------------
    # Emit
    # ------------------------------------------------------------------

    async def emit(self, job_id: str, event: dict) -> None:
        if job_id not in self._history:
            return
        self._history[job_id].append(event)
        if event.get("stage") in _TERMINAL_STAGES:
            self._done[job_id] = True
        for q in self._subscribers.get(job_id, []):
            await q.put(event)

    def get_emitter(self, job_id: str) -> EmitFn:
        """Return a bound async callable for use inside pipeline modules."""
        async def _emit(event: dict) -> None:
            await self.emit(job_id, event)
        return _emit

    # ------------------------------------------------------------------
    # Subscribe (SSE)
    # ------------------------------------------------------------------

    async def subscribe(self, job_id: str) -> AsyncGenerator[str, None]:
        """
        Yield SSE-formatted text/event-stream chunks.
        Replays the full event history first so late-connecting clients
        don't miss events that fired before they subscribed.
        """
        history = self._history.get(job_id, [])
        for event in history:
            yield _sse(event)

        # If the job already finished, we're done
        if self._done.get(job_id):
            return

        q: asyncio.Queue[dict | None] = asyncio.Queue()
        self._subscribers.setdefault(job_id, []).append(q)
        try:
            while True:
                event = await q.get()
                if event is None:  # sentinel from remove_job
                    break
                yield _sse(event)
                if event.get("stage") in _TERMINAL_STAGES:
                    break
        finally:
            subs = self._subscribers.get(job_id, [])
            if q in subs:
                subs.remove(q)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"
