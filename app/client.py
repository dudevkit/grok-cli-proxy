from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

log = logging.getLogger("grok-cli-proxy.client")


class HttpClient:
    """Shared httpx client with connection pooling and 429 backoff."""

    def __init__(self, config: dict[str, Any]):
        timeout = float(config.get("request_timeout_sec", 120))
        limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
        self.client = httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
        )
        log.info(
            "http client created timeout=%s max_connections=100", timeout
        )

    async def close(self) -> None:
        await self.client.aclose()


async def backoff_sleep(attempt: int, max_sec: int = 30) -> None:
    """Exponential backoff with jitter for 429 handling."""
    wait = min(2**attempt + random.random(), max_sec)
    log.debug("429 backoff attempt=%d wait=%.1fs", attempt, wait)
    await asyncio.sleep(wait)
