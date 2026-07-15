from __future__ import annotations

import asyncio
import time
from typing import Any


class LogBroadcaster:
    """Simple async pub/sub for real-time log push to WebSocket clients."""

    def __init__(self, max_queue: int = 256):
        self._subscribers: set[asyncio.Queue] = set()
        self._max_queue = max_queue

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def push(self, event: dict[str, Any]) -> None:
        dead: list[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    def push_sync(self, event: dict[str, Any]) -> None:
        """Fire-and-forget sync push for use from sync code."""
        dead: list[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    def log(
        self,
        kind: str,
        message: str,
        *,
        method: str | None = None,
        path: str | None = None,
        status: int | None = None,
        account: str | None = None,
        account_id: str | None = None,
        duration_ms: float | None = None,
        tokens: int | None = None,
        model: str | None = None,
    ) -> None:
        """Create a structured log entry and broadcast."""
        entry: dict[str, Any] = {
            "kind": kind,
            "message": message,
            "ts": time.time(),
        }
        if method:
            entry["method"] = method
        if path:
            entry["path"] = path
        if status is not None:
            entry["status"] = status
        if account:
            entry["account"] = account
        if account_id:
            entry["account_id"] = account_id
        if duration_ms is not None:
            entry["duration_ms"] = round(duration_ms, 1)
        if tokens is not None:
            entry["tokens"] = tokens
        if model:
            entry["model"] = model
        self.push_sync(entry)
