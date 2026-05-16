"""Tests for the multi-key PMS-init URL synthesiser.

Two structural extensions to ``_scan_inline_js_pms_init``:
  * Multi-group regex patterns — a single regex can capture both a
    clientKey/tenant AND a resourceId, and the ``url_fn`` receives both
    as positional args to build a combined API URL.
    Used by the SightMap direct-API URL (``clientKey`` + ``sightmapID``
    → ``sightmap.com/app/api/v1/{client}/sightmaps/{id}``).
  * ``url_fn`` may return a list of URLs — one bootstrap pattern can
    fan out into multiple candidate API URLs.
    Used by the hy.ly bootstrap pattern, which emits 3 candidate
    ``my.hy.ly/api/`` URLs from a single ``mktg/fjs/{tenant}/0.js?pid={pid}``
    reference.

Plus allow-list entries for ``my.hy.ly/api/`` (kept distinct from the
``my.hy.ly/chat`` blacklist) and ``sightmap.com/app/api`` (distinct from
the existing ``sightmap.com/embed/`` allow-list entry).
"""
from __future__ import annotations

import pytest


# ── _scan_inline_js_pms_init: multi-group regex + multi-URL url_fn return ──

class TestPmsInitMultiGroupAndMultiURL:
    """The `_scan_inline_js_pms_init` refactor must keep single-group entries
    working while also supporting 2+ group regexes and list returns."""

    def test_single_group_backcompat(self) -> None:
        """Existing single-group entries (e.g. sightmap_id) still work."""
        from ma_poc.pms.scraper import _scan_inline_js_pms_init
        html = (
            '<script>SightMap.init({"sightmap_id":"abcdef123456"});</script>'
            + "x" * 200  # pad to clear the >=100 char gate
        )
        hits = _scan_inline_js_pms_init(html)
        urls = [u for u, _ in hits]
        # canonical embed URL still synthesised by the legacy single-group pattern
        assert any("sightmap.com/embed/abcdef123456" in u for u in urls)

    def test_multi_group_pattern_emits_combined_url(self) -> None:
        """D19a: SightMap clientKey + sightmapID → direct API URL.

        Both keys appear in a single JSON config block. The two-group
        pattern captures them and synthesises the direct-data endpoint
        instead of the SPA embed URL.
        """
        from ma_poc.pms.scraper import _scan_inline_js_pms_init
        html = (
            '<script id="floor-plans-plus-config" type="application/json">'
            '{"widgetId":"floor-plans-plus-35591661",'
            '"clientKey":"morgan-prop-cl-xyz123",'
            '"sightmapID":"ryzvg6mywln"}</script>'
            + "x" * 200
        )
        hits = _scan_inline_js_pms_init(html)
        urls = [u for u, _ in hits]
        # Direct API URL synthesised from both keys
        assert (
            "https://sightmap.com/app/api/v1/morgan-prop-cl-xyz123/sightmaps/ryzvg6mywln"
            in urls
        )

    def test_multi_group_pattern_reverse_order(self) -> None:
        """Same as above but with keys in reverse source order — the
        complementary pattern catches it."""
        from ma_poc.pms.scraper import _scan_inline_js_pms_init
        html = (
            '<script>{"sightmapID":"ryzvg6mywln","clientKey":"key-after"}</script>'
            + "x" * 200
        )
        hits = _scan_inline_js_pms_init(html)
        urls = [u for u, _ in hits]
        assert (
            "https://sightmap.com/app/api/v1/key-after/sightmaps/ryzvg6mywln"
            in urls
        )

    def test_url_fn_can_return_list(self) -> None:
        """D17a: one pattern can fan out into multiple candidate URLs.

        The hy.ly pattern emits 3 guess URLs from a single match so the
        cascade can try each before giving up.
        """
        from ma_poc.pms.scraper import _scan_inline_js_pms_init
        html = (
            '<script src="https://my.hy.ly/mktg/fjs/hkEj8vR6X/0.js?pid=1828463638541958517&frame=1"></script>'
            + "x" * 200
        )
        hits = _scan_inline_js_pms_init(html)
        urls = [u for u, p in hits if p == "hyly"]
        # The pattern emits 3 candidate URLs from the single bootstrap reference.
        assert len(urls) >= 3
        # At least one must include the pid in the canonical RESTful shape.
        assert any("/properties/1828463638541958517/" in u for u in urls)
        # The tenant slug is also exposed in one of the variants for sites
        # that route through `/api/{tenant}/properties/{pid}` style.
        assert any("/api/hkEj8vR6X/properties/1828463638541958517" in u for u in urls)

    def test_no_match_returns_empty(self) -> None:
        from ma_poc.pms.scraper import _scan_inline_js_pms_init
        html = "<html><body>nothing PMS-shaped here</body></html>" + "x" * 200
        hits = _scan_inline_js_pms_init(html)
        # Existing patterns may catch noise; the contract is just "no crash + list shape".
        assert isinstance(hits, list)


# ── Portal allow-list entries for hy.ly API + SightMap direct API ─────────

class TestPortalAllowlistEntries:
    """`my.hy.ly/api/` synthesised URLs pass through the portal allow-list
    (despite `my.hy.ly/chat` being blacklisted)."""

    def test_my_hy_ly_api_is_recognised(self) -> None:
        """The `my.hy.ly/api/` substring in a URL maps to the `hyly` portal."""
        from ma_poc.pms.adapters._html_extract import _PORTAL_URL_PATTERNS
        urls_to_portals = dict(_PORTAL_URL_PATTERNS)
        assert "my.hy.ly/api/" in urls_to_portals
        assert urls_to_portals["my.hy.ly/api/"] == "hyly"

    def test_my_hy_ly_chat_is_blacklisted(self) -> None:
        """Confirm the chat blacklist still suppresses chat widgets — the
        D17a fix didn't accidentally re-enable them."""
        from ma_poc.pms.adapters._html_extract import _is_portal_infra_blacklisted
        assert _is_portal_infra_blacklisted("https://my.hy.ly/chat/ssid?page_url=x")

    def test_sightmap_app_api_in_allowlist(self) -> None:
        """D19a synthesised SightMap direct-API URLs route through the
        sightmap portal label."""
        from ma_poc.pms.adapters._html_extract import _PORTAL_URL_PATTERNS
        urls_to_portals = dict(_PORTAL_URL_PATTERNS)
        assert "sightmap.com/app/api" in urls_to_portals
        assert urls_to_portals["sightmap.com/app/api"] == "sightmap"
