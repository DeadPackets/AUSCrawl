"""Rate-paced HTTP with jittered retry and block detection.

Throughput is governed by request-start pacing, not worker count: Banner throttles
on aggregate requests per second, so many concurrent workers plus a global limiter
is both faster and safer than few workers plus a per-worker sleep.
"""

import asyncio
import logging
import random
import time
from typing import Callable, Optional
from urllib.parse import urlencode

import httpx

from . import config

log = logging.getLogger("auscrawl")

FORM_CONTENT_TYPE = {"content-type": "application/x-www-form-urlencoded"}
BLOCK_SCAN_LIMIT = 65536
_BLOCK_MARKERS = (
    b"Just a moment...",
    b"cf-browser-verification",
    b"Attention Required! | Cloudflare",
    b"The requested URL was rejected",
)


def should_retry_status(code: int) -> bool:
    return code in config.RETRYABLE_STATUS


def backoff_delay(
    attempt: int,
    base: float = config.RETRY_BASE,
    jitter: Callable[[], float] = random.random,
) -> float:
    """Equal-jitter exponential backoff, so a fleet does not retry in lockstep."""
    full = base ** attempt
    return full / 2 + (full / 2) * jitter()


def retry_after_seconds(resp: httpx.Response) -> Optional[float]:
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def is_blocked(resp: httpx.Response) -> bool:
    """True for a Cloudflare or F5 interstitial rather than a real response."""
    if resp.headers.get("cf-mitigated") == "challenge":
        return True
    if "json" in resp.headers.get("content-type", ""):
        return False
    body = resp.content[:BLOCK_SCAN_LIMIT]
    return any(marker in body for marker in _BLOCK_MARKERS)


class RateLimiter:
    """Global token-bucket pacing of request starts, with AIMD feedback."""

    def __init__(
        self,
        rate: float,
        max_rate: Optional[float] = None,
        min_rate: float = config.MIN_RATE,
        decrease: float = 0.5,
        increase: float = 1.0,
        now: Callable[[], float] = time.monotonic,
    ):
        self.rate = float(rate)
        self.max_rate = float(max_rate if max_rate is not None else rate)
        self.min_rate = float(min_rate)
        self.decrease = decrease
        self.increase = increase
        self._now = now
        self._next_free: Optional[float] = None
        self._lock = asyncio.Lock()

    def _reserve(self, now: float) -> float:
        start = now if self._next_free is None else max(self._next_free, now)
        self._next_free = start + 1.0 / self.rate
        return max(0.0, start - now)

    async def acquire(self):
        async with self._lock:
            wait = self._reserve(self._now())
        if wait > 0:  # sleep outside the lock so waiters pace in parallel
            await asyncio.sleep(wait)

    def record_throttle(self):
        self.rate = max(self.min_rate, self.rate * self.decrease)

    def record_success(self):
        if self.rate < self.max_rate:
            self.rate = min(self.max_rate, self.rate + self.increase / self.rate)


def make_client(concurrency: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=10.0),
        follow_redirects=True,
        http2=True,
        headers=dict(config.BROWSER_HEADERS),
        limits=httpx.Limits(
            max_connections=concurrency + 5,
            max_keepalive_connections=concurrency + 5,
        ),
    )


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    form: dict[str, str] | list[tuple[str, str]] | None = None,
    params: dict | None = None,
    rate: Optional[RateLimiter] = None,
    max_retries: int | None = None,
) -> httpx.Response:
    kwargs: dict = {}
    if form is not None:
        kwargs["content"] = urlencode(form)
        kwargs["headers"] = FORM_CONTENT_TYPE
    if params is not None:
        kwargs["params"] = params

    tries = max_retries or config.MAX_RETRIES
    for attempt in range(1, tries + 1):
        last = attempt == tries
        if rate:
            await rate.acquire()
        try:
            resp = await client.request(method, url, **kwargs)
            resp.raise_for_status()

            if is_blocked(resp):
                if rate:
                    rate.record_throttle()
                if last:
                    break
                wait = backoff_delay(attempt)
                log.warning("Challenge page (attempt %d), retrying in %.0fs",
                            attempt, wait)
                await asyncio.sleep(wait)
                continue

            if rate:
                rate.record_success()
            return resp
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if rate and code in config.THROTTLE_STATUS:
                rate.record_throttle()
            if not should_retry_status(code):
                raise
            if last:
                break
            wait = retry_after_seconds(e.response) or backoff_delay(attempt)
            log.warning("HTTP %d (attempt %d), retrying in %.1fs", code, attempt, wait)
            await asyncio.sleep(wait)
        except httpx.RequestError as e:
            if last:
                raise
            wait = backoff_delay(attempt)
            log.warning("Network error (attempt %d): %s, retrying in %.0fs",
                        attempt, e, wait)
            await asyncio.sleep(wait)

    raise RuntimeError(f"Failed after {tries} retries: {method} {url}")
