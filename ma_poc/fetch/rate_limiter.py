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

    def on_rate_limited(self, host: str) -> float:
        """Halve the effective rate for *host* on every 429 / RATE_LIMITED.

        Bug #5 Layer 1 (2026-05-18): the bucket is purely proactive — it
        enforces a fixed default rps regardless of server feedback. When the
        CDN starts returning 429s, the bucket keeps firing at the same rate
        and the shard's quota burns instantly. With 12 shards collectively
        hitting one domain (Essex 2026-05-18 case), per-shard 2 rps × 12 ≈
        24 rps trips the CDN's global limit.

        This method halves the per-host rate on every observed 429. After
        2-3 calls a shard drops from 2 rps -> 0.5 rps -> 0.125 rps. Each
        shard learns independently — no shared state required. Floored at
        0.1 rps so we never deadlock.

        Returns the new rps so callers can log / observe the decay.
        """
        bucket = self._get_bucket(host)
        current_rps = 1.0 / bucket.refill_interval if bucket.refill_interval > 0 else self._default_rps
        new_rps = max(0.1, current_rps / 2.0)
        bucket.refill_interval = 1.0 / new_rps
        # Bucket capacity also shrinks so burst-allowance can't undo the
        # decay on the next acquire (capacity controls how many tokens
        # accumulate during a quiet period). Floor at 1.0 token so the
        # very-first request after a quiet window can still go through.
        bucket.capacity = max(1.0, new_rps)
        # Drain any accumulated tokens so the next acquire respects the
        # new rate immediately. Otherwise a host that was idle for 30s
        # would still have a full burst sitting in the bucket.
        bucket.tokens = min(bucket.tokens, bucket.capacity)
        log.warning(
            "Host %s rate-limited (429): decayed %.3f -> %.3f rps",
            host, current_rps, new_rps,
        )
        return new_rps

    def current_rps(self, host: str) -> float:
        """Return the host's current effective rate (rps). For telemetry."""
        if host not in self._buckets:
            return self._default_rps
        bucket = self._buckets[host]
        return 1.0 / bucket.refill_interval if bucket.refill_interval > 0 else self._default_rps

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
