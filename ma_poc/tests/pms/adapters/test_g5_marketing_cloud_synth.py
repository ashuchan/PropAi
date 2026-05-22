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


# ── 2026-05-22 — deterministic URN selection regression tests ──────────
#
# Pre-2026-05-22 ``find_g5_urn`` used ``max(matches, key=len)`` — picked
# the longest URN string, which on multi-property operators is reliably
# the parent-company switcher URN or a sibling property's URN (verified
# live 5/5 wrong on Anson, Central Park, Brook Hollow, Ten68 West,
# Westgate Village). The new ``find_g5_urn_for_property`` anchors on the
# Cloudinary asset-folder path ``/g5/g5-c-<co>/g5-cl-<prop>/uploads/``
# which is guaranteed unique-to-property by G5's CMS, and falls back to
# most-frequent g5-cl-* when the CDN regex misses. Below are minimal
# fixtures that exercise both code paths.


class TestG5DeterministicUrnSelection:
    """Regression tests for the 2026-05-22 deterministic URN picker."""

    def test_cdn_anchor_picks_property_urn_over_siblings(self):
        """The favicon CDN path always points at the property's own
        folder; sibling URNs in nav menus must NOT win even when they
        appear in more places overall. Mirrors live evidence on
        Anson Apartments (5 sibling URN refs vs 1 favicon ref)."""
        from ma_poc.pms.adapters.g5 import find_g5_urn_for_property

        html = (
            '<html><head>'
            # Favicon CDN path points at the property's own folder.
            '<link rel="shortcut icon" href="'
            'https://g5-assets-cld-res.cloudinary.com/image/upload/g5/'
            'g5-c-ifo7vzy-anson-residential-llc/'
            'g5-cl-1n33lxfrno-anson-burlingame-ca/uploads/anson-fav.png">'
            '</head><body>'
            # Sibling switcher menu — 5 different URNs, all wrong.
            '<a href="/properties/g5-cl-1mr81ug5ko-anson-gaithersburg-md">MD</a>'
            '<a href="/properties/g5-cl-2abc-anson-austin-tx">TX</a>'
            '<a href="/properties/g5-cl-3def-anson-portland-or">OR</a>'
            '<a href="/properties/g5-cl-4ghi-anson-denver-co">CO</a>'
            '<a href="/properties/g5-cl-5jkl-anson-seattle-wa">WA</a>'
            '</body></html>'
        )
        result = find_g5_urn_for_property(html, "https://ansonburlingame.com/")
        assert result == "g5-cl-1n33lxfrno-anson-burlingame-ca", (
            f"Expected CDN-anchored URN; got {result!r}. The longest "
            "match heuristic would have picked one of the siblings."
        )

    def test_cdn_anchor_resists_longest_match_trap(self):
        """The favicon URN is shorter than the parent-company switcher
        URN. The CDN anchor MUST win over length-based selection.
        """
        from ma_poc.pms.adapters.g5 import find_g5_urn_for_property

        html = (
            '<head><link rel="icon" href="https://res.cloudinary.com/'
            'g5/g5-c-foo/g5-cl-short-prop/uploads/icon.png"></head>'
            '<body>'
            # Way longer URN that doesn't belong to this property
            '<a href="/g5-cl-1longerthantheproperty-parent-company-marketing-hub-corporate-portal">Hub</a>'
            '</body>'
        )
        result = find_g5_urn_for_property(html, "")
        assert result == "g5-cl-short-prop"

    def test_fallback_to_most_frequent_when_no_cdn_anchor(self):
        """When the favicon doesn't render to the property's Cloudinary
        folder (uncommon but possible — pages with custom favicon
        configuration), fall back to most-frequent g5-cl-* on the page.
        On a real property page, the property's own URN appears 50-150+
        times (in templates, scripts, CSS class names); siblings appear
        once each in a switcher menu.
        """
        from ma_poc.pms.adapters.g5 import find_g5_urn_for_property

        html = (
            '<html><body>'
            # The property's URN appears many times (templated content)
            + '<div class="g5-cl-myproperty-fl">x</div>' * 50
            # Sibling URNs appear once each (switcher menu)
            + '<a href="/g5-cl-sibling1-ny">NY</a>'
            + '<a href="/g5-cl-sibling2-tx">TX</a>'
            '</body></html>'
        )
        result = find_g5_urn_for_property(html, "")
        assert result == "g5-cl-myproperty-fl"

    def test_backward_compat_find_g5_urn_routes_to_new_impl(self):
        """The old function name still works (legacy imports) — same
        deterministic behaviour as the new function.
        """
        from ma_poc.pms.adapters.g5 import find_g5_urn

        html = (
            '<head><link rel="icon" href="https://cdn/g5/g5-c-test/'
            'g5-cl-correct-prop/uploads/icon.png"></head>'
            '<a href="/g5-cl-1nlongerthantheproperty-sibling-far-away">x</a>'
        )
        assert find_g5_urn(html) == "g5-cl-correct-prop"

    def test_returns_none_on_empty_or_no_g5_markers(self):
        from ma_poc.pms.adapters.g5 import find_g5_urn_for_property

        assert find_g5_urn_for_property("", "") is None
        assert find_g5_urn_for_property("<html>nothing here</html>", "") is None
