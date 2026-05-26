"""Tests for cookie-mint reuse opt-in + CF IUAM cookie-strip retry.

2026-05-26 (blockwall-v2 A/B): the 300-property A/B showed cookie reuse
helped 0 and hurt 10 (UA-binding mismatch). These tests verify:
  1. _with_clearance does NOT attach minted cookies by default.
  2. _with_clearance DOES attach when host is in the allowlist.
  3. probe_get retries without cookies when response is CF-blocked and
     cookies were attached.
  4. Explicit per-call cookies= are always passed through (not gated).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import ma_poc.pms.adapters._probe as _probe_mod
from ma_poc.pms.adapters._probe import (
    _CLEARANCE_REUSE_HOST_ALLOWLIST,
    _with_clearance,
    reset_clearance_cookies,
    set_clearance_cookies,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _install_cookies(cookies: dict) -> None:
    """Install minted cookies in the module-level contextvar."""
    set_clearance_cookies(cookies)


# ---------------------------------------------------------------------------
# _with_clearance: default behaviour (no allowlist hit) — do NOT attach
# ---------------------------------------------------------------------------

def test_with_clearance_no_url_no_minted_cookies():
    """No cookies minted → opts returned unchanged."""
    set_clearance_cookies(None)
    opts = {"impersonate": "chrome120"}
    result = _with_clearance(opts.copy())
    assert "cookies" not in result


def test_with_clearance_minted_cookies_no_url_not_attached():
    """Minted cookies present but no url provided — host unknown → not attached."""
    token = set_clearance_cookies({"cf_clearance": "abc123"})
    try:
        opts = {"impersonate": "chrome120"}
        result = _with_clearance(opts.copy(), url=None)
        # No url → host unknown → opt-in gate doesn't know the host → not attached
        assert "cookies" not in result
    finally:
        reset_clearance_cookies(token)


def test_with_clearance_minted_cookies_url_not_in_allowlist():
    """URL provided but host NOT in allowlist → cookies not attached."""
    token = set_clearance_cookies({"cf_clearance": "abc123"})
    try:
        assert "example.com" not in _CLEARANCE_REUSE_HOST_ALLOWLIST
        opts = {"impersonate": "chrome120"}
        result = _with_clearance(opts.copy(), url="https://example.com/floorplans")
        assert "cookies" not in result
    finally:
        reset_clearance_cookies(token)


def test_with_clearance_minted_cookies_url_in_allowlist(monkeypatch):
    """URL host IS in the allowlist → minted cookies attached."""
    monkeypatch.setattr(
        _probe_mod, "_CLEARANCE_REUSE_HOST_ALLOWLIST",
        frozenset({"allowed-host.com"})
    )
    token = set_clearance_cookies({"cf_clearance": "abc123"})
    try:
        opts = {"impersonate": "chrome120"}
        result = _with_clearance(opts.copy(), url="https://allowed-host.com/apartments")
        assert result.get("cookies") == {"cf_clearance": "abc123"}
    finally:
        reset_clearance_cookies(token)


def test_with_clearance_explicit_cookies_always_passed_through():
    """Explicit per-call cookies= pass through regardless of minted state."""
    set_clearance_cookies(None)
    explicit = {"session": "tok123"}
    opts = {"impersonate": "chrome120", "cookies": explicit}
    result = _with_clearance(opts.copy(), url="https://some-host.com/")
    # Explicit cookies survive even when no minted cookies and host not in allowlist
    assert result.get("cookies") == explicit


def test_with_clearance_explicit_wins_over_minted_on_key_collision(monkeypatch):
    """Explicit per-call cookie value wins over minted on key collision."""
    monkeypatch.setattr(
        _probe_mod, "_CLEARANCE_REUSE_HOST_ALLOWLIST",
        frozenset({"collision-host.com"})
    )
    token = set_clearance_cookies({"cf_clearance": "minted-val", "shared_key": "minted"})
    try:
        explicit = {"shared_key": "explicit"}
        opts = {"cookies": explicit}
        result = _with_clearance(opts.copy(), url="https://collision-host.com/")
        # minted keys that don't collide come through; explicit wins on collision
        assert result["cookies"]["shared_key"] == "explicit"
        assert result["cookies"]["cf_clearance"] == "minted-val"
    finally:
        reset_clearance_cookies(token)


def test_with_clearance_empty_minted_cookies_noop():
    """Empty minted dict → no-op (same as no cookies minted)."""
    token = set_clearance_cookies({})
    try:
        opts = {"impersonate": "chrome120"}
        result = _with_clearance(opts.copy(), url="https://example.com/")
        assert "cookies" not in result
    finally:
        reset_clearance_cookies(token)


# ---------------------------------------------------------------------------
# probe_get: cookie-strip retry on CF IUAM when cookies were sent
# ---------------------------------------------------------------------------

def _make_resp(status: int, blocked: bool) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.content = b"<title>Just a moment...</title>" if blocked else b"<html>ok</html>"
    r.text = r.content.decode()
    return r


@patch("ma_poc.pms.adapters._probe._looks_blocked")
@patch("ma_poc.pms.adapters._probe.probe_proxies", return_value={})
@patch("ma_poc.pms.adapters._probe.web_unlocker_key", return_value="")
def test_probe_get_no_cookies_no_retry(mock_wuk, mock_px, mock_blocked, monkeypatch):
    """When no cookies were attached, no strip-retry fires even if blocked."""
    mock_blocked.return_value = True
    creq_mock = MagicMock()
    creq_mock.get.return_value = _make_resp(403, True)
    monkeypatch.setattr(_probe_mod, "_clearance_cookies",
                        type("CV", (), {"get": staticmethod(lambda: None)})())
    import importlib, sys
    curl_mod = MagicMock()
    curl_mod.requests = creq_mock
    with patch.dict(sys.modules, {"curl_cffi": curl_mod}):
        from ma_poc.pms.adapters._probe import probe_get
        probe_get("https://no-cookie-host.com/units")
    # Only one call — no retry without cookies
    assert creq_mock.get.call_count == 1


@patch("ma_poc.pms.adapters._probe.probe_proxies", return_value={})
@patch("ma_poc.pms.adapters._probe.web_unlocker_key", return_value="")
def test_probe_get_cookie_strip_retry_on_cf_iuam(mock_wuk, mock_px, monkeypatch):
    """When cookies attached + CF IUAM → retry without cookies → return clean resp."""
    import sys

    # Simulate allowlist hit so cookies DO get attached
    monkeypatch.setattr(
        _probe_mod, "_CLEARANCE_REUSE_HOST_ALLOWLIST",
        frozenset({"hurt-host.com"})
    )
    token = set_clearance_cookies({"cf_clearance": "abc"})
    try:
        first_resp = _make_resp(403, True)   # CF IUAM
        second_resp = _make_resp(200, False)  # clean after cookie strip

        creq_mock = MagicMock()
        creq_mock.get.side_effect = [first_resp, second_resp]

        # Patch _looks_blocked to match actual content
        def real_looks_blocked(resp: MagicMock) -> bool:
            return resp.status_code in (403, 429, 503) or b"Just a moment" in (resp.content or b"")

        monkeypatch.setattr(_probe_mod, "_looks_blocked", real_looks_blocked)

        curl_mod = MagicMock()
        curl_mod.requests = creq_mock
        with patch.dict(sys.modules, {"curl_cffi": curl_mod}):
            from importlib import reload
            import ma_poc.pms.adapters._probe as fresh
            result = fresh.probe_get("https://hurt-host.com/conventional/")

        assert creq_mock.get.call_count == 2
        # Second call must NOT have 'cookies' in kwargs
        second_call_kwargs = creq_mock.get.call_args_list[1][1]
        assert "cookies" not in second_call_kwargs
        assert result is second_resp
    finally:
        reset_clearance_cookies(token)


@patch("ma_poc.pms.adapters._probe.probe_proxies", return_value={})
@patch("ma_poc.pms.adapters._probe.web_unlocker_key", return_value="")
def test_probe_get_cookie_strip_retry_still_blocked_falls_through(mock_wuk, mock_px, monkeypatch):
    """If strip-retry is still blocked, fall through to original response (not raise)."""
    import sys

    monkeypatch.setattr(
        _probe_mod, "_CLEARANCE_REUSE_HOST_ALLOWLIST",
        frozenset({"still-blocked.com"})
    )
    token = set_clearance_cookies({"cf_clearance": "abc"})
    try:
        both_blocked = _make_resp(403, True)
        creq_mock = MagicMock()
        creq_mock.get.return_value = both_blocked

        def real_looks_blocked(resp: MagicMock) -> bool:
            return resp.status_code in (403, 429, 503)

        monkeypatch.setattr(_probe_mod, "_looks_blocked", real_looks_blocked)

        curl_mod = MagicMock()
        curl_mod.requests = creq_mock
        with patch.dict(sys.modules, {"curl_cffi": curl_mod}):
            from ma_poc.pms.adapters._probe import probe_get
            result = probe_get("https://still-blocked.com/units")

        # Two calls attempted (with cookies, without cookies), both blocked
        assert creq_mock.get.call_count == 2
        # Returns the original resp (not raises)
        assert result is both_blocked
    finally:
        reset_clearance_cookies(token)


# ---------------------------------------------------------------------------
# _CLEARANCE_REUSE_HOST_ALLOWLIST starts empty
# ---------------------------------------------------------------------------

def test_allowlist_starts_empty():
    """The allowlist must be empty at module load — per the A/B verdict."""
    assert len(_CLEARANCE_REUSE_HOST_ALLOWLIST) == 0
