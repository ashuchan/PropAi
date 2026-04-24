"""Tests for ma_poc.fetch.proxy.none_provider."""

from __future__ import annotations

from ma_poc.fetch.proxy.base import ProxyTier
from ma_poc.fetch.proxy.none_provider import NoneProvider


def test_none_provider_returns_direct_for_every_tier() -> None:
    prov = NoneProvider()
    for tier in (
        ProxyTier.DIRECT,
        ProxyTier.DATACENTER,
        ProxyTier.RESIDENTIAL,
        ProxyTier.UNBLOCKER,
    ):
        cfg = prov.get_config(tier=tier, canonical_id="p1")
        assert cfg.is_direct
        assert cfg.tier is ProxyTier.DIRECT
        assert cfg.server is None
