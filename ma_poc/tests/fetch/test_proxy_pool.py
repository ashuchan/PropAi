"""Tests for proxy_pool — health-weighted proxy selection."""
from __future__ import annotations

from ma_poc.fetch.proxy_pool import ProxyPool


def test_proxy_pool_empty_returns_none() -> None:
    pool = ProxyPool([])
    assert pool.pick() is None


def test_proxy_pool_picks_healthiest() -> None:
    pool = ProxyPool(["http://a:a@proxy1:8080", "http://b:b@proxy2:8080"])
    # Degrade proxy1
    pool.mark_failure("http://a:a@proxy1:8080", "test")
    pool.mark_failure("http://a:a@proxy1:8080", "test")
    # Proxy2 should be picked more often (higher health)
    picks = [pool.pick() for _ in range(20)]
    proxy2_count = picks.count("http://b:b@proxy2:8080")
    assert proxy2_count >= 10  # Should be heavily favoured


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
