"""Polite HTTP fetching: descriptive User-Agent, ETag conditional requests,
per-host rate limiting, retry with exponential backoff."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

USER_AGENT = (
    "InternshipRadar/1.0 (personal internship alert tool; "
    "+https://github.com/sagarvarma1/internship-radar)"
)


@dataclass
class FetchResult:
    status: int  # 200, 304, or error status; 0 for network error
    data: Any = None
    etag: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (200, 304)


class HostLimiter:
    """At most one request per `min_interval` seconds to each host."""

    def __init__(self, min_interval: float = 1.0):
        self.min_interval = min_interval
        self._next: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            ready = self._next.get(host, 0.0)
            if ready > now:
                await asyncio.sleep(ready - now)
            self._next[host] = max(now, ready) + self.min_interval


class Fetcher:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        limiter: HostLimiter | None = None,
        retries: int = 3,
        backoff_base: float = 2.0,
    ):
        self.client = client or httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            http2=False,
        )
        self.limiter = limiter or HostLimiter()
        self.retries = retries
        self.backoff_base = backoff_base

    async def aclose(self) -> None:
        await self.client.aclose()

    async def get_json(self, url: str, etag: str | None = None) -> FetchResult:
        host = urlparse(url).netloc
        headers = {"If-None-Match": etag} if etag else {}
        last = FetchResult(status=0, error="not attempted")
        for attempt in range(self.retries + 1):
            await self.limiter.wait(host)
            try:
                r = await self.client.get(url, headers=headers)
            except httpx.HTTPError as e:
                last = FetchResult(status=0, error=f"{type(e).__name__}: {e}")
            else:
                if r.status_code == 304:
                    return FetchResult(status=304, etag=etag)
                if r.status_code == 200:
                    try:
                        return FetchResult(status=200, data=r.json(), etag=r.headers.get("etag"))
                    except ValueError as e:
                        return FetchResult(status=200, error=f"invalid JSON: {e}")
                last = FetchResult(status=r.status_code, error=f"HTTP {r.status_code}")
                # 4xx other than 429 won't fix itself by retrying (e.g. 404 = bad slug)
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    return last
                retry_after = r.headers.get("retry-after")
                if retry_after and retry_after.isdigit() and attempt < self.retries:
                    await asyncio.sleep(min(int(retry_after), 120))
                    continue
            if attempt < self.retries:
                delay = self.backoff_base * (2**attempt) + random.uniform(0, 1)
                await asyncio.sleep(delay)
        return last
