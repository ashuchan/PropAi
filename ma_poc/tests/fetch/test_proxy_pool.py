"""Tests for proxy_pool — health-weighted proxy selection."""

from __future__ import annotations

import random

import pytest

from ma_poc.fetch.proxy_pool import ProxyPool


def test_proxy_pool_empty_returns_none() -> None:
    pool = ProxyPool([])
    assert pool.pick() is None


_DEGRADED = "http://a:a@proxy1:8080"
_HEALTHY = "http://b:b@proxy2:8080"


def _degraded_pair(seed: int) -> ProxyPool:
    """Two-proxy pool with proxy1 degraded to health 0.5, drawing from a seeded RNG."""
    pool = ProxyPool([_DEGRADED, _HEALTHY], rng=random.Random(seed))
    pool.mark_failure(_DEGRADED, "test")  # 1.00 -> 0.75
    pool.mark_failure(_DEGRADED, "test")  # 0.75 -> 0.50
    return pool


# Seeded, so this is a fixed outcome rather than a 20-draw sample from an
# unseeded global RNG. The old version drew from ``random`` (seeded per process
# from os.urandom) and failed ~4% of runs — P(X < 10 | n=20, p=2/3) — which
# showed up as a phantom regression when diffing failure SETS against baseline.
# Several seeds, because one passing seed proves nothing about the weighting.
@pytest.mark.parametrize("seed", [0, 1, 2, 7, 42, 1337])
def test_proxy_pool_picks_healthiest(seed: int) -> None:
    pool = _degraded_pair(seed)
    # proxy2 has 2x the health of proxy1, so it must win the weighted draw
    # decisively — threshold held at the original 10/20, not relaxed.
    picks = [pool.pick() for _ in range(20)]
    assert picks.count(_HEALTHY) >= 10


def test_proxy_pool_pick_is_deterministic_under_seeded_rng() -> None:
    """Guard the injected seam: identical seeds must yield identical draws.

    If ``pick`` ever reverts to the module-level ``random``, the two sequences
    diverge and the flake is back — this fails instead of silently sampling.
    """
    pool_a, pool_b = _degraded_pair(12345), _degraded_pair(12345)
    seq_a = [pool_a.pick() for _ in range(50)]
    seq_b = [pool_b.pick() for _ in range(50)]
    assert seq_a == seq_b
    # And the degraded proxy is genuinely picked less often, not merely tied.
    assert seq_a.count(_DEGRADED) < seq_a.count(_HEALTHY)


def test_proxy_pool_failure_drops_health() -> None:
    pool = ProxyPool(["http://u:p@proxy:8080"])
    pool.mark_failure("http://u:p@proxy:8080", "test")
    health = pool._proxies["http://u:p@proxy:8080"].health
    assert health == 0.75  # 1.0 - 0.25


def test_proxy_pool_success_raises_health() -> None:
    pool = ProxyPool(["http://u:p@proxy:8080"])
    pool.mark_failure("http://u:p@proxy:8080", "test")  # 0.75
    pool.mark_success("http://u:p@proxy:8080")  # 0.80
    health = pool._proxies["http://u:p@proxy:8080"].health
    assert health == 0.80


def test_proxy_pool_quarantines_after_low_health() -> None:
    pool = ProxyPool(["http://u:p@proxy:8080"])
    # Drop health below 0.25
    for _ in range(4):
        pool.mark_failure("http://u:p@proxy:8080", "test")
    # Health should be at 0.1 (min), quarantined
    assert pool.pick() is None


def test_proxy_pool_sticky_key_returns_same_proxy_twice() -> None:
    pool = ProxyPool(["http://u:p@proxy1:8080", "http://u:p@proxy2:8080"])
    p1 = pool.pick(sticky_key="property_123")
    p2 = pool.pick(sticky_key="property_123")
    assert p1 == p2


def test_proxy_pool_repr_redacts_credentials() -> None:
    pool = ProxyPool(["http://user:secret@proxy:8080"])
    snapshot = pool.health_snapshot()
    assert "secret" not in snapshot[0]["url"]
    assert "***" in snapshot[0]["url"]
