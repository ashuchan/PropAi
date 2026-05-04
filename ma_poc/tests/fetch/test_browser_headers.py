"""S3 tests — full Chrome header set, no cross-brand client-hint leak."""
from __future__ import annotations

import pytest

from ma_poc.fetch.headers import chrome_header_set
from ma_poc.fetch.stealth import Identity, _IDENTITIES


def _chrome_id() -> Identity:
    return next(i for i in _IDENTITIES if i.browser_family == "chrome")


def _firefox_id() -> Identity:
    return next(i for i in _IDENTITIES if i.browser_family == "firefox")


def test_s3_chrome_identity_full_header_set() -> None:
    headers = chrome_header_set(_chrome_id(), cold_visit=False)
    required = {
        "User-Agent", "Accept", "Accept-Language", "Accept-Encoding",
        "Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-Dest", "Sec-Fetch-User",
        "Upgrade-Insecure-Requests", "Cache-Control",
        "Sec-Ch-Ua", "Sec-Ch-Ua-Mobile", "Sec-Ch-Ua-Platform",
    }
    missing = required - set(headers)
    assert not missing, f"Chrome identity missing required headers: {missing}"
    assert len(headers) >= 8, f"expected ≥ 8 headers, got {len(headers)}"


def test_s3_firefox_identity_no_client_hints() -> None:
    headers = chrome_header_set(_firefox_id(), cold_visit=False)
    forbidden = {h for h in headers if h.lower().startswith("sec-ch-ua")}
    assert not forbidden, f"Firefox identity emitted client-hint headers: {forbidden}"


def test_s3_no_cross_brand_leak() -> None:
    """Sec-CH-UA brand value must contain the identity's browser family."""
    for ident in _IDENTITIES:
        if ident.browser_family in ("chrome", "edge") and ident.sec_ch_ua:
            ua_lower = ident.sec_ch_ua.lower()
            assert (
                "chromium" in ua_lower
                or "google chrome" in ua_lower
                or "microsoft edge" in ua_lower
            ), (
                f"identity Sec-CH-UA brand list inconsistent with "
                f"browser_family={ident.browser_family}: {ident.sec_ch_ua}"
            )


def test_s3_cold_visit_sets_google_referer() -> None:
    headers = chrome_header_set(_chrome_id(), cold_visit=True)
    assert headers.get("Referer") == "https://www.google.com/"
    assert headers["Sec-Fetch-Site"] == "cross-site"


def test_s3_warm_visit_no_referer() -> None:
    headers = chrome_header_set(_chrome_id(), cold_visit=False)
    assert "Referer" not in headers
    assert headers["Sec-Fetch-Site"] == "same-origin"


def test_s3_accept_encoding_has_brotli() -> None:
    """Accept-Encoding must include br — absence is a bot signal for Chrome UAs."""
    for ident in _IDENTITIES:
        headers = chrome_header_set(ident, cold_visit=False)
        assert "br" in headers.get("Accept-Encoding", ""), (
            f"identity {ident.browser_family} missing 'br' in Accept-Encoding"
        )


def test_s3_header_consistency_per_identity() -> None:
    """Each identity's emitted headers must be self-consistent with its UA."""
    for ident in _IDENTITIES:
        headers = chrome_header_set(ident, cold_visit=False)
        assert headers["User-Agent"] == ident.user_agent
        if ident.browser_family == "chrome":
            assert f"Chrome/{ident.browser_major}" in ident.user_agent, (
                f"chrome identity UA doesn't contain Chrome/{ident.browser_major}"
            )
        # Firefox and Safari must NOT emit Sec-CH-UA
        if ident.browser_family in ("firefox", "safari"):
            ch_headers = [h for h in headers if h.lower().startswith("sec-ch-ua")]
            assert not ch_headers, (
                f"{ident.browser_family} identity emitted client-hint headers: {ch_headers}"
            )


def test_s3_edge_identity_sec_ch_ua_contains_edge() -> None:
    """Edge identity's Sec-CH-UA must reference Microsoft Edge."""
    edge_id = next((i for i in _IDENTITIES if i.browser_family == "edge"), None)
    if edge_id is None:
        pytest.skip("no Edge identity in pool")
    headers = chrome_header_set(edge_id, cold_visit=False)
    assert "Sec-Ch-Ua" in headers
    assert "microsoft edge" in headers["Sec-Ch-Ua"].lower()
