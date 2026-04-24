"""Tests for ma_poc.fetch.proxy.pricing."""

from __future__ import annotations

from ma_poc.fetch.proxy.base import ProxyTier
from ma_poc.fetch.proxy.pricing import USD_PER_GB, estimate_cost_usd


def test_direct_is_free() -> None:
    assert estimate_cost_usd(ProxyTier.DIRECT, 10 * 10**9) == 0.0


def test_datacenter_cost_is_proportional_to_gb() -> None:
    # 1 GB
    cost = estimate_cost_usd(ProxyTier.DATACENTER, 10**9)
    assert cost == USD_PER_GB[ProxyTier.DATACENTER]


def test_residential_is_more_than_datacenter() -> None:
    dc = estimate_cost_usd(ProxyTier.DATACENTER, 10**9)
    rs = estimate_cost_usd(ProxyTier.RESIDENTIAL, 10**9)
    assert rs > dc


def test_negative_or_zero_bytes_returns_zero() -> None:
    assert estimate_cost_usd(ProxyTier.RESIDENTIAL, 0) == 0.0
    assert estimate_cost_usd(ProxyTier.RESIDENTIAL, -100) == 0.0


def test_all_tiers_present() -> None:
    assert set(USD_PER_GB.keys()) == {
        ProxyTier.DIRECT,
        ProxyTier.DATACENTER,
        ProxyTier.RESIDENTIAL,
        ProxyTier.UNBLOCKER,
    }
