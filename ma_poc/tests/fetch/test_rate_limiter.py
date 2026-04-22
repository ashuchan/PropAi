"""Tests for rate_limiter — per-host token bucket."""

from __future__ import annotations

import asyncio

import pytest

from ma_poc.fetch.rate_limiter import HostRateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_allows_burst_within_capacity() -> None:
    limiter = HostRateLimiter(default_rps=5.0)
    # Should allow up to 5 immediate requests (burst capacity)
    for _ in range(5):
        await asyncio.wait_for(limiter.acquire("example.com"), timeout=1.0)


@pytest.mark.asyncio
async def test_rate_limiter_blocks_once_exhausted() -> None:
    limiter = HostRateLimiter(default_rps=1.0)
    # First request should go through
    await asyncio.wait_for(limiter.acquire("example.com"), timeout=1.0)
    # Second should complete but may take time
    await asyncio.wait_for(limiter.acquire("example.com"), timeout=3.0)


@pytest.mark.asyncio
async def test_rate_limiter_refills_over_time() -> None:
    clock_time = 0.0

    def mock_clock() -> float:
        return clock_time

    limiter = HostRateLimiter(default_rps=1.0, clock=mock_clock)
    # Exhaust tokens
    await limiter.acquire("example.com")
    # Advance clock by 1 second (should refill 1 token)
    clock_time = 1.0
    # Should succeed now with refilled token
    await asyncio.wait_for(limiter.acquire("example.com"), timeout=0.1)


def test_rate_limiter_crawl_delay_overrides_default() -> None:
    limiter = HostRateLimiter(default_rps=10.0)
    limiter.set_crawl_delay("slow.example.com", 5.0)
    bucket = limiter._get_bucket("slow.example.com")
    # 5s delay = 0.2 rps, interval = 5.0
    assert bucket.refill_interval == pytest.approx(5.0, rel=0.01)


@pytest.mark.asyncio
async def test_rate_limiter_two_hosts_independent() -> None:
    limiter = HostRateLimiter(default_rps=1.0)
    # Exhaust host A
    await limiter.acquire("host-a.com")
    # Host B should still be available
    await asyncio.wait_for(limiter.acquire("host-b.com"), timeout=0.1)
