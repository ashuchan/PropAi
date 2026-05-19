"""D24 (2026-05-19) — G5 Marketing Cloud floor-plans-plus widget URL synthesis.

PID 75340 liveatvcu.com canonical (also affects every Landmark Properties /
Cardinal Group / G5-platform tenant — ~10-30 properties in the 500-CSV).

The G5 widget ships its property identity inside a JSON config block:

    <script id="floor-plans-plus-config" type="application/json">
    {"widgetId": "...",
     "locationUrn": "g5-cl-1nmaxwdhcg-landmark-property-services-inc-richmond-va",
     "inventoryHost": "https://inventory.g5marketingcloud.com", ...}
    </script>

Real per-unit data lives at:
    {inventoryHost}/api/v1/apartment_complexes/{locationUrn}/floorplans

(This URL shape was confirmed by ``config/profiles/61377.json``, a Tier-4
LLM discovery that the self-learning loop persisted as ``known_endpoints``.)

The two new inline-JS patterns capture the keys in either source order
(locationUrn-first AND inventoryHost-first) and synthesise the canonical
inventory REST URL. The companion portal allow-list entry
``inventory.g5marketingcloud.com`` ensures the synthesised URL routes
through the known-portal pass — taking precedence over the
``g5marketingcloud.com`` infra-blacklist entry that prevents unknown-portal
scan from queueing G5 chat / asset hosts.
"""
from __future__ import annotations


class TestG5MarketingCloudInlineJsSynth:
    def test_canonical_widget_config_emits_inventory_url(self) -> None:
        """The canonical floor-plans-plus-config JSON yields the inventory
        REST URL with both keys substituted in the right places."""
        from ma_poc.pms.scraper import _scan_inline_js_pms_init
        html = (
            '<script id="floor-plans-plus-config" type="application/json">'
            '{"widgetId":"floor-plans-plus-29088414",'
            '"locationUrn":"g5-cl-1nmaxwdhcg-landmark-property-services-inc-richmond-va",'
            '"locationName":"1005 Grove Ave Apartments",'
            '"inventoryHost":"https://inventory.g5marketingcloud.com",'
            '"vlsHost":"https://vendor-leads.g5marketingcloud.com"}'
            "</script>"
            + "x" * 200
        )
        hits = _scan_inline_js_pms_init(html)
        urls = [u for u, p in hits if p == "g5_marketing_cloud"]
        assert (
            "https://inventory.g5marketingcloud.com/api/v1/apartment_complexes/"
            "g5-cl-1nmaxwdhcg-landmark-property-services-inc-richmond-va/floorplans"
        ) in urls

    def test_reverse_key_order_also_fires(self) -> None:
        """JSON key order is not guaranteed by spec — the reverse-order
        pattern catches sites whose config emits inventoryHost first."""
        from ma_poc.pms.scraper import _scan_inline_js_pms_init
        html = (
            '<script id="floor-plans-plus-config" type="application/json">'
            '{"inventoryHost":"https://inventory.g5marketingcloud.com",'
            '"locationUrn":"g5-cl-abcdefgh-some-property"}'
            "</script>"
            + "x" * 200
        )
        hits = _scan_inline_js_pms_init(html)
        urls = [u for u, p in hits if p == "g5_marketing_cloud"]
        assert (
            "https://inventory.g5marketingcloud.com/api/v1/apartment_complexes/"
            "g5-cl-abcdefgh-some-property/floorplans"
        ) in urls

    def test_trailing_slash_on_inventory_host_normalised(self) -> None:
        """``inventoryHost`` may or may not have a trailing slash — the
        synthesiser strips it so the result has exactly one slash between
        host and path."""
        from ma_poc.pms.scraper import _scan_inline_js_pms_init
        html = (
            '<script>{"locationUrn":"g5-cl-x12345-foo-bar",'
            '"inventoryHost":"https://inventory.g5marketingcloud.com/"}</script>'
            + "x" * 200
        )
        hits = _scan_inline_js_pms_init(html)
        urls = [u for u, _ in hits]
        assert "https://inventory.g5marketingcloud.com//api" not in " ".join(urls)
        assert any(
            u.startswith(
                "https://inventory.g5marketingcloud.com/api/v1/apartment_complexes/"
            )
            for u in urls
        )

    def test_short_urn_does_not_match(self) -> None:
        """URN with fewer than 8 chars after the ``g5-cl-`` prefix is most
        likely a placeholder or template token, not a real property URN."""
        from ma_poc.pms.scraper import _scan_inline_js_pms_init
        html = (
            '<script>{"locationUrn":"g5-cl-1",'
            '"inventoryHost":"https://inventory.g5marketingcloud.com"}</script>'
            + "x" * 200
        )
        hits = _scan_inline_js_pms_init(html)
        # No g5_marketing_cloud synthesised URL for the short URN
        urls = [u for u, p in hits if p == "g5_marketing_cloud"]
        assert not urls

    def test_missing_inventory_host_does_not_match(self) -> None:
        """``locationUrn`` alone (no ``inventoryHost``) yields no URL — the
        two-group pattern needs both keys."""
        from ma_poc.pms.scraper import _scan_inline_js_pms_init
        html = (
            '<script>{"locationUrn":"g5-cl-x12345-foo-bar"}</script>'
            + "x" * 200
        )
        hits = _scan_inline_js_pms_init(html)
        urls = [u for u, p in hits if p == "g5_marketing_cloud"]
        assert not urls

    def test_other_g5_keys_do_not_false_match(self) -> None:
        """``vlsHost`` looks similar to ``inventoryHost`` but should not
        substitute for it — only the explicit ``inventoryHost`` key counts."""
        from ma_poc.pms.scraper import _scan_inline_js_pms_init
        html = (
            '<script>{"locationUrn":"g5-cl-x12345-foo-bar",'
            '"vlsHost":"https://vendor-leads.g5marketingcloud.com"}</script>'
            + "x" * 200
        )
        hits = _scan_inline_js_pms_init(html)
        urls = [u for u, p in hits if p == "g5_marketing_cloud"]
        assert not urls


class TestG5PortalAllowlistEntry:
    def test_inventory_host_in_portal_allowlist(self) -> None:
        from ma_poc.pms.adapters._html_extract import _PORTAL_URL_PATTERNS
        urls_to_portals = dict(_PORTAL_URL_PATTERNS)
        assert "inventory.g5marketingcloud.com" in urls_to_portals
        assert urls_to_portals["inventory.g5marketingcloud.com"] == "g5_marketing_cloud"

    def test_bare_g5marketingcloud_still_blacklisted(self) -> None:
        """The companion blacklist entry for ``g5marketingcloud.com`` and
        ``g5-c-`` must stay in place — only the explicit
        ``inventory.g5marketingcloud.com`` subdomain is whitelisted.
        Without this, the 6th-pass unknown-portal discovery would re-enable
        every G5 chat / asset host as a hop candidate.
        """
        from ma_poc.pms.adapters._html_extract import _is_portal_infra_blacklisted
        # Generic / unknown G5 host is still rejected
        assert _is_portal_infra_blacklisted(
            "https://www.g5marketingcloud.com/widgets/123"
        )

    def test_inventory_subdomain_routes_to_known_portal(self) -> None:
        """When the synthesised inventory URL flows through the
        known-portal allow-list, it should be classified as a
        ``g5_marketing_cloud`` portal — NOT filtered as infra noise.
        """
        from ma_poc.pms.adapters._html_extract import _PORTAL_URL_PATTERNS
        host_pattern = "inventory.g5marketingcloud.com"
        url = (
            f"https://{host_pattern}/api/v1/apartment_complexes/"
            "g5-cl-1nmaxwdhcg-landmark-richmond-va/floorplans"
        )
        # First-match wins; check the synthesised URL hits the expected entry
        matched = next(
            (portal for needle, portal in _PORTAL_URL_PATTERNS if needle in url),
            None,
        )
        assert matched == "g5_marketing_cloud"
