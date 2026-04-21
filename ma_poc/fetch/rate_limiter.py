"""Per-host token bucket rate limiter.

Async-safe. robots.txt Crawl-delay sets the refill rate per host;
default is 2 requests/second.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

log = logging.getLogger(__name__)


class HostRateLimiter:
    """Async token bucket rate limiter, one bucket per host.

    Args:
        default_rps: Default requests per second per host.
        clock: Callable returning current time (for testing).
    """

    def __init__(
        self,
        default_rps: float = 2.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._default_rps = default_rps
        self._clock = clock or time.monotonic
        self._buckets: dict[str, _Bucket] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def set_crawl_delay(self, host: str, delay_sec: float) -> None:
        """Override the refill rate for a host based on robots.txt Crawl-delay.

        Args:
            host: The hostname.
            delay_sec: Minimum seconds between requests.
        """
        rps = 1.0 / max(delay_sec, 0.1)
        bucket = self._get_bucket(host)
        bucket.refill_interval = 1.0 / rps
        log.info("Set crawl delay for %s: %.1fs (%.2f rps)", host, delay_sec, rps)

    async def acquire(self, host: str) -> None:
        """Block until the host's bucket has a token.

        Args:
            host: The hostname to rate-limit.
        """
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            bucket = self._get_bucket(host)
            now = self._clock()
            elapsed = now - bucket.last_refill
            bucket.tokens = min(
                bucket.capacity,
                bucket.tokens + elapsed / bucket.refill_interval,
            )
            bucket.last_refill = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return
            # Need to wait for a token
            wait_sec = (1.0 - bucket.tokens) * bucket.refill_interval
            bucket.tokens = 0.0
            await asyncio.sleep(wait_sec)
            bucket.last_refill = self._clock()

    def _get_bucket(self, host: str) -> _Bucket:
        """Get or create the token bucket for a host."""
        if host not in self._buckets:
            refill_interval = 1.0 / self._default_rps
            self._buckets[host] = _Bucket(
                tokens=self._default_rps,  # Start with a burst allowance
                capacity=self._default_rps,
                refill_interval=refill_interval,
                last_refill=self._clock(),
            )
        return self._buckets[host]


class _Bucket:
    """Internal token bucket state."""

    __slots__ = ("tokens", "capacity", "refill_interval", "last_refill")

    def __init__(
        self,
        tokens: float,
        capacity: float,
        refill_interval: float,
        last_refill: float,
    ) -> None:
        self.tokens = tokens
        self.capacity = capacity
        self.refill_interval = refill_interval
        self.last_refill = last_refill
