"""Tests for ma_poc.fetch.proxy.selector."""

from __future__ import annotations

from ma_poc.fetch.proxy.base import ProxyTier
from ma_poc.fetch.proxy.selector import ProxyOverride, ProxySelector


def test_new_property_gets_default_tier() -> None:
    sel = ProxySelector()
    assert sel.pick(profile_tier=None) is ProxyTier.DATACENTER


def test_existing_profile_tier_used() -> None:
    sel = ProxySelector()
    assert sel.pick(profile_tier="residential") is ProxyTier.RESIDENTIAL
    assert sel.pick(profile_tier="direct") is ProxyTier.DIRECT


def test_corrupt_profile_tier_falls_back_to_default() -> None:
    sel = ProxySelector(default_tier=ProxyTier.DIRECT)
    assert sel.pick(profile_tier="gibberish") is ProxyTier.DIRECT


def test_override_wins_over_profile() -> None:
    sel = ProxySelector()
    ovr = ProxyOverride(tier=ProxyTier.RESIDENTIAL)
    assert (
        sel.pick(profile_tier="datacenter", override=ovr)
        is ProxyTier.RESIDENTIAL
    )
