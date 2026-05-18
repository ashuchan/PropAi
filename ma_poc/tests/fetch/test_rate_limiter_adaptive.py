"""Tests for Bug #5 Layer 1 (2026-05-18) — HostRateLimiter adaptive decay.

The pre-fix rate limiter was purely proactive (fixed default_rps, no
feedback from server). With 12 shards each running at 2 rps to
essexapartmenthomes.com, fleet-wide ~24 rps overwhelmed the CDN -> all 27
PIDs RATE_LIMITED. Layer 1 has each shard halve its host-specific rps on
every observed 429; after 2-3 calls the shard rate decays to ~0.5 rps.
"""

from __future__ import annotations

import pytest

from ma_poc.fetch.rate_limiter import HostRateLimiter


def test_default_rps_unchanged_for_fresh_host() -> None:
    rl = HostRateLimiter(default_rps=2.0)
    assert rl.current_rps("example.com") == pytest.approx(2.0)


def test_on_rate_limited_halves_rps() -> None:
    rl = HostRateLimiter(default_rps=2.0)
    new_rps = rl.on_rate_limited("essexapartmenthomes.com")
    assert new_rps == pytest.approx(1.0)
    assert rl.current_rps("essexapartmenthomes.com") == pytest.approx(1.0)


def test_repeated_decay_compounds() -> None:
    rl = HostRateLimiter(default_rps=2.0)
    rl.on_rate_limited("essex.com")  # -> 1.0
    rl.on_rate_limited("essex.com")  # -> 0.5
    rl.on_rate_limited("essex.com")  # -> 0.25
    assert rl.current_rps("essex.com") == pytest.approx(0.25)


def test_decay_floor_at_0_1_rps() -> None:
    """Floor at 0.1 rps so we never deadlock the shard."""
    rl = HostRateLimiter(default_rps=2.0)
    for _ in range(20):
        rl.on_rate_limited("hostile.com")
    assert rl.current_rps("hostile.com") == pytest.approx(0.1)


def test_decay_is_per_host() -> None:
    """Decaying example.com must not affect other.com."""
    rl = HostRateLimiter(default_rps=2.0)
    rl.on_rate_limited("example.com")
    assert rl.current_rps("example.com") == pytest.approx(1.0)
    assert rl.current_rps("other.com") == pytest.approx(2.0)


def test_decay_persists_across_acquires() -> None:
    """After decay, acquire must honour the slower rate. We don't time
    the actual sleep here (would slow the test); just check the bucket
    state is consistent with the decayed rps."""
    rl = HostRateLimiter(default_rps=2.0)
    rl.on_rate_limited("essex.com")  # rps -> 1.0
    rl.on_rate_limited("essex.com")  # rps -> 0.5
    # Internal bucket should reflect the new rate.
    bucket = rl._get_bucket("essex.com")
    assert bucket.refill_interval == pytest.approx(2.0)  # 1.0 / 0.5
    # Capacity also shrunk so accumulated burst doesn't undo the decay.
    assert bucket.capacity == pytest.approx(0.5) or bucket.capacity == pytest.approx(1.0)


def test_current_rps_returns_default_for_unseen_host() -> None:
    rl = HostRateLimiter(default_rps=2.0)
    assert rl.current_rps("never-touched.com") == pytest.approx(2.0)


def test_crawl_delay_and_decay_compose() -> None:
    """robots.txt Crawl-delay sets the initial rate; subsequent 429s
    still halve from THAT base, not from default_rps."""
    rl = HostRateLimiter(default_rps=2.0)
    rl.set_crawl_delay("slowsite.com", 5.0)  # -> 0.2 rps
    assert rl.current_rps("slowsite.com") == pytest.approx(0.2)
    rl.on_rate_limited("slowsite.com")  # -> 0.1 (floor)
    assert rl.current_rps("slowsite.com") == pytest.approx(0.1)
