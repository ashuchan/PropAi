"""Tests for ma_poc.fetch.proxy.base — ProxyTier / ProxyConfig helpers."""

from __future__ import annotations

from ma_poc.fetch.proxy.base import ProxyConfig, ProxyTier


def test_next_tier_walks_up_the_ladder() -> None:
    assert ProxyTier.DIRECT.next_tier() is ProxyTier.DATACENTER
    assert ProxyTier.DATACENTER.next_tier() is ProxyTier.RESIDENTIAL
    assert ProxyTier.RESIDENTIAL.next_tier() is ProxyTier.UNBLOCKER


def test_next_tier_at_top_returns_none() -> None:
    assert ProxyTier.UNBLOCKER.next_tier() is None


def test_direct_config_is_direct() -> None:
    cfg = ProxyConfig(tier=ProxyTier.DIRECT)
    assert cfg.is_direct is True
    assert cfg.to_playwright() is None
    assert cfg.to_httpx() is None
    assert cfg.to_httpx_url() is None


def test_to_playwright_format() -> None:
    cfg = ProxyConfig(
        tier=ProxyTier.DATACENTER,
        server="http://brd.superproxy.io:33335",
        username="brd-customer-x-zone-dc",
        password="secret",
    )
    pw = cfg.to_playwright()
    assert pw == {
        "server": "http://brd.superproxy.io:33335",
        "username": "brd-customer-x-zone-dc",
        "password": "secret",
    }


def test_to_httpx_format_urlencodes_credentials() -> None:
    # Password contains special chars that must be %-encoded for URL form.
    cfg = ProxyConfig(
        tier=ProxyTier.RESIDENTIAL,
        server="http://brd.superproxy.io:33335",
        username="user@name",
        password="p@ss:w/rd",
    )
    mapping = cfg.to_httpx()
    assert mapping is not None
    expected = "http://user%40name:p%40ss%3Aw%2Frd@brd.superproxy.io:33335"
    assert mapping["http://"] == expected
    assert mapping["https://"] == expected
    assert cfg.to_httpx_url() == expected
