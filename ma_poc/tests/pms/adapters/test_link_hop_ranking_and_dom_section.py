"""Tests for link-hop URL ranking and DOM-section selection.

Covers:
  * Tracker / consent-banner host blacklist (Optimizely, Cookiebot, OneTrust
    iframes are NOT queued as inventory portals).
  * Composable URL-shape scoring on the link-hop ranker — canonical
    inventory page > slugged plan-detail > per-unit pricing page > bare
    index > anti-signal (gallery / tour / news).
  * Priority pre-scan inside ``_extract_rent_dom_section`` so the LLM
    sees the multi-unit container instead of being chopped off by the
    ``<main>`` byte cap.
  * SecureCafe URL synthesis prefers a direct ``*.securecafe.com`` href
    on the page over a hostname-slug guess.
"""
from __future__ import annotations

import pytest


# ── Tracker / consent / ad-tech hosts are NOT inventory portals ────────────

class TestTrackerHostBlacklist:
    """Common A/B-test / consent / ad-tech hosts no longer pass the unknown-portal probe."""

    def test_optimizely_blacklisted(self) -> None:
        from ma_poc.pms.adapters._html_extract import _is_portal_infra_blacklisted
        assert _is_portal_infra_blacklisted(
            "https://a19749172433.cdn.optimizely.com/client_storage/a19749172433.html"
        ) is True

    def test_optimizely_root_blacklisted(self) -> None:
        from ma_poc.pms.adapters._html_extract import _is_portal_infra_blacklisted
        assert _is_portal_infra_blacklisted("https://www.optimizely.com/x.html") is True

    def test_cookiebot_blacklisted(self) -> None:
        from ma_poc.pms.adapters._html_extract import _is_portal_infra_blacklisted
        assert _is_portal_infra_blacklisted("https://consent.cookiebot.com/banner.html") is True

    def test_onetrust_blacklisted(self) -> None:
        from ma_poc.pms.adapters._html_extract import _is_portal_infra_blacklisted
        assert _is_portal_infra_blacklisted("https://cdn.onetrust.com/cookie/abc.js") is True

    def test_real_portals_still_pass(self) -> None:
        """sightmap / rentcafe / appfolio iframes still get through to be queued."""
        from ma_poc.pms.adapters._html_extract import _is_portal_infra_blacklisted
        assert _is_portal_infra_blacklisted("https://sightmap.com/embed/abc123") is False
        assert _is_portal_infra_blacklisted("https://hickorymilloh.securecafe.com/onlineleasing/x/") is False
        assert _is_portal_infra_blacklisted("https://franklin.appfolio.com/listings") is False


# ── URL-shape scoring: structural pattern drives the rank, not anchor text ──

class TestURLShapeScoring:
    """URL pattern (path shape) drives link ranking; anchor text is corroborative.

    The canary on 2026-05-16 found `/available-apartments/` and per-plan URLs
    losing to an Optimizely tracker iframe (10_000) because the floor-lift
    flattened all "floorplans-family" anchors to 5_600 — index and detail tied.
    """

    def test_canonical_inventory_path(self) -> None:
        from ma_poc.pms.scraper import _url_shape_score
        score, label = _url_shape_score("/apartments/cortland-on-pike/available-apartments/")
        assert label == "canonical_inventory"
        assert score >= 5_000  # must outrank PMS_PRIOR_SCORE (5_000)

    def test_per_unit_detail_path(self) -> None:
        """`/available-apartments/1n-131/pricing/` is a per-unit detail page.
        D11 (2026-05-16): score lowered to 4_700 — per-unit pages yield 1
        unit each, so a hop budget should prefer canonical_inventory
        (1 fetch → all units) and per-plan pages over per-unit pages."""
        from ma_poc.pms.scraper import _url_shape_score
        score, label = _url_shape_score("/apartments/cortland-on-pike/available-apartments/1n-131/pricing/")
        assert label == "per_unit_detail"
        assert score == 4_700

    def test_rentcafe_per_plan(self) -> None:
        from ma_poc.pms.scraper import _url_shape_score
        score, label = _url_shape_score("/oklahoma-city/the-izzy/floorplans/1x1-reno-1301657/fp_name/occupancy_type/conventional/")
        assert label == "rentcafe_per_plan"
        assert score >= 5_000

    def test_slugged_plan_detail(self) -> None:
        """PID 3483 Brook `/floorplans/1-bed-1-bath` is a slugged child of the
        index — must outrank `/floorplans/` (bare index) AND PMS_PRIOR (5_000)."""
        from ma_poc.pms.scraper import _url_shape_score
        score, label = _url_shape_score("/floorplans/1-bed-1-bath")
        assert label == "slugged_plan_detail"
        assert score >= 5_000

    def test_bare_index_ranks_below_detail(self) -> None:
        from ma_poc.pms.scraper import _url_shape_score
        idx_score, idx_label = _url_shape_score("/floorplans/")
        det_score, det_label = _url_shape_score("/floorplans/1-bed-1-bath")
        assert idx_label == "bare_index"
        assert det_label == "slugged_plan_detail"
        assert det_score > idx_score  # detail must outrank index

    @pytest.mark.parametrize(
        "path,expected_label",
        [
            ("/schedule-a-tour/", "anti_signal_non_inventory"),
            ("/schedule_tour", "anti_signal_non_inventory"),
            ("/gallery/", "anti_signal_non_inventory"),
            ("/photos/", "anti_signal_non_inventory"),
            ("/amenities/", "anti_signal_non_inventory"),
            ("/reviews", "anti_signal_non_inventory"),
            ("/contact/", "anti_signal_non_inventory"),
            ("/sitemap", "anti_signal_non_inventory"),
            ("/news/2026/01-launch", "anti_signal_non_inventory"),
            # 2026-05-24 additions — low-value paths previously missed.
            ("/community/", "anti_signal_non_inventory"),
            ("/lifestyle/", "anti_signal_non_inventory"),
            ("/location/", "anti_signal_non_inventory"),
            ("/directions/", "anti_signal_non_inventory"),
            ("/map/", "anti_signal_non_inventory"),
            ("/pets/", "anti_signal_non_inventory"),
            ("/specials/", "anti_signal_non_inventory"),
            # Nested anti-signals — the canonical run 2026-05-24 case
            # (PID 257570 moderacherrycreek) had a property-slugged
            # amenities path.
            (
                "/denver-co-apartments/modera-cherry-creek/amenities/",
                "anti_signal_non_inventory",
            ),
        ],
    )
    def test_anti_signal_paths(self, path: str, expected_label: str) -> None:
        from ma_poc.pms.scraper import _url_shape_score
        score, label = _url_shape_score(path)
        assert label == expected_label
        assert score < 0  # explicit penalty

    def test_unknown_path_no_score(self) -> None:
        from ma_poc.pms.scraper import _url_shape_score
        score, label = _url_shape_score("/some-random-page")
        assert label == ""
        assert score == 0

    # End-to-end ranker tests ───────────────────────────────────────────────

    def test_rank_canonical_inventory_beats_per_unit_and_index(self) -> None:
        """End-to-end (post-D11): canonical inventory must rank FIRST so
        the cascade fetches the one URL holding all units before spending
        hops on per-unit pricing pages.

        Cortland scenario: 7-hop budget previously consumed by 7 per-unit
        pricing pages (= 7 units extracted) missed the canonical page
        which holds all 79 data-unit-id cards in a single fetch."""
        from ma_poc.pms.scraper import _rank_internal_links
        html = """
        <html><body>
          <a href="/floorplans/">Floor Plans</a>
          <a href="/available-apartments/">Available Apartments</a>
          <a href="/available-apartments/1n-131/pricing/">View Apartment Details</a>
        </body></html>
        """
        ranked = _rank_internal_links(html, "https://example.com/", limit=10)
        paths = [u.split("example.com")[-1] for u, _s, _a in ranked]
        # Canonical inventory ranks #1 — that's the D11 invariant.
        assert paths[0] == "/available-apartments/", paths

    def test_rank_slugged_plan_beats_tour_and_index(self) -> None:
        """PID 3483 Brook shape: /floorplans/1-bed-1-bath should outrank
        /floorplans/ AND /scheduletour/."""
        from ma_poc.pms.scraper import _rank_internal_links
        html = """
        <html><body>
          <a href="/floorplans/">Floor Plans</a>
          <a href="/floorplans/1-bed-1-bath">View Details</a>
          <a href="/floorplans/2-bedroom-1-bath">View Details</a>
          <a href="/scheduletour/">Schedule Tour</a>
        </body></html>
        """
        ranked = _rank_internal_links(html, "https://example.com/", limit=10)
        paths = [u.split("example.com")[-1] for u, _s, _a in ranked]
        # the 2 per-plan URLs must be in the top 2 slots
        assert "/floorplans/1-bed-1-bath" in paths[:2]
        assert "/floorplans/2-bedroom-1-bath" in paths[:2]
        # /scheduletour should be filtered (anti-signal makes total ≤ 0)
        assert "/scheduletour/" not in paths


# ── _extract_rent_dom_section: priority pre-scan over known listing containers ─

class TestDomSectionPriorityScan:
    """The 20 KB <main> truncation chopped the unit table off the bottom on
    PID 5822 The Izzy. New design pre-scans for known listing containers and
    caps at 60 KB."""

    def test_option_row_block_preferred_over_main(self) -> None:
        from ma_poc.pms.adapters.generic import _extract_rent_dom_section
        html = (
            "<html><body><main>"
            + "<p>Plan summary. " + "filler text. " * 1000 + "</p>"
            + "</main>"
            + "<div class='option-row'>Unit 138 $1,115 1,025 sqft Available Now</div>"
            + "<div class='option-row'>Unit 161 $1,025 1,025 sqft Available Now</div>"
            + "</body></html>"
        )
        section = _extract_rent_dom_section(html)
        assert section is not None
        # priority scan should pick option-row blocks, not <main>
        assert "Unit 138" in section
        assert "Unit 161" in section
        # both should be wrapped together
        assert section.count("option-row") >= 2

    def test_cortland_apartments_card_preferred(self) -> None:
        from ma_poc.pms.adapters.generic import _extract_rent_dom_section
        html = """
        <html><body><main><p>marketing filler</p></main>
        <a data-js-hook="apartment" data-unit-id="2604014">
          <span class="apartments__card-floorplan">S2</span>
          <span class="apartments__card-number">Apt # C-113</span>
          <span class="apartments__card-price">Starting at $1,513</span>
        </a>
        </body></html>
        """
        section = _extract_rent_dom_section(html)
        assert section is not None
        assert "data-unit-id" in section or "apartments__card" in section

    def test_max_bytes_raised_to_60k(self) -> None:
        """The default cap is now 60 KB (was 20 KB)."""
        import inspect
        from ma_poc.pms.adapters.generic import _extract_rent_dom_section
        sig = inspect.signature(_extract_rent_dom_section)
        assert sig.parameters["max_bytes"].default == 60_000

    def test_fallback_to_main_when_no_priority_selector_matches(self) -> None:
        """Phase 3: legacy behaviour when no priority selectors match."""
        from ma_poc.pms.adapters.generic import _extract_rent_dom_section
        html = "<html><body><main><p>1 bed / 1 bath 750 sqft $1,500</p></main></body></html>"
        section = _extract_rent_dom_section(html)
        assert section is not None


# ── SecureCafe URL synthesis: prefer direct href over hostname-slug guess ───

class TestSecureCafeDirectHrefPreference:
    """When a direct securecafe href is already on the page (multi-property
    portfolio shape: hickorymilloh.securecafe.com), use it verbatim instead
    of synthesising morgan-properties.securecafe.com from the base hostname."""

    def test_direct_href_preferred(self) -> None:
        from ma_poc.pms.adapters._html_extract import detect_securecafe_portal_url
        html = (
            '<a href="https://hickorymilloh.securecafe.com/onlineleasing/'
            'hickory-mill0/floorplans.aspx">Available Floor Plans</a>'
        )
        url = detect_securecafe_portal_url(html, "https://www.morgan-properties.com/apartments/oh/hilliard/hickory-mill")
        assert url is not None
        assert "hickorymilloh.securecafe.com" in url
        # NOT the synthesised morgan-properties.securecafe.com
        assert "morgan-properties.securecafe.com" not in url

    def test_direct_href_userlogin_trimmed_to_floorplans(self) -> None:
        """When the direct href is /userlogin.aspx etc., trim to /floorplans.aspx."""
        from ma_poc.pms.adapters._html_extract import detect_securecafe_portal_url
        html = (
            '<a href="https://hickorymilloh.securecafe.com/onlineleasing/'
            'hickory-mill0/oleapplication.aspx?stepname=floorplan&myOlePropertyId=2143678">Apply</a>'
        )
        url = detect_securecafe_portal_url(html, "https://www.morgan-properties.com/x")
        assert url is not None
        assert "hickorymilloh.securecafe.com" in url
        assert "floorplans.aspx" in url
        assert "oleapplication" not in url

    def test_synthesis_fallback_when_no_direct_href(self) -> None:
        """When the page only has /onlineleasing/{slug} (JS SPA route),
        synthesise from base hostname as before."""
        from ma_poc.pms.adapters._html_extract import detect_securecafe_portal_url
        html = '<a href="/onlineleasing/the-brook-at-columbia">Apply Now</a>'
        url = detect_securecafe_portal_url(html, "https://www.thebrookatcolumbia.com/")
        assert url is not None
        assert "thebrookatcolumbia.securecafe.com" in url
        assert "/onlineleasing/the-brook-at-columbia/floorplans.aspx" in url


# ── R3 (2026-05-24) — anti-signal lift-leak fix ─────────────────────────────


class TestAntiSignalLiftLeak:
    """The PMS_PRIOR floor-lift previously used ``url_shape < 3_000`` as its
    upper gate, which accidentally matched anti-signal URLs whose shape
    score is ``-2_000``. A ``/amenities/`` link with anchor text "Apartments"
    (anchor_score=60) would land at 5_100 despite the explicit -2_000
    penalty. Run 2026-05-24 PID 257570 moderacherrycreek hopped to an
    amenities page through this leak; dom_scan then extracted a marketing
    tagline as a floor_plan_name.

    The fix gates the lift on ``0 <= url_shape < 3_000`` so anti-signal
    URLs cannot be lifted.
    """

    def test_amenities_link_with_apartment_anchor_does_not_lift(self) -> None:
        """The canonical run 2026-05-24 case. A property-slugged amenities
        URL with an anchor containing "apartment" must NOT outrank a
        floor-plan link on the same page."""
        from ma_poc.pms.scraper import _rank_internal_links
        html = """
        <html><body>
          <a href="/denver-co-apartments/modera-cherry-creek/amenities/">Amenities at Modera Apartment Homes</a>
          <a href="/floor-plans/">Floor Plans</a>
        </body></html>
        """
        ranked = _rank_internal_links(html, "https://www.moderacherrycreek.com/", limit=10)
        # Floor-plans link must rank first; amenities must either be
        # filtered out entirely (score <= 0) or rank strictly below.
        if not ranked:
            # All filtered — anti-signal worked, no false-positive admission.
            return
        top_url = ranked[0][0]
        assert "amenities" not in top_url, (
            f"Anti-signal lift leak regression: amenities ranked first. "
            f"ranked={ranked}"
        )

    def test_anti_signal_url_below_universal_prior(self) -> None:
        """An amenities link that survives the score>0 filter must rank
        below ``_UNIVERSAL_PRIOR`` (4500). Anchor-text bonuses must not
        be allowed to lift it past the floor-lift gate."""
        from ma_poc.pms.scraper import _rank_internal_links
        # Anchor "Tour Our Apartments" — gets anchor_score from "tour" (40)
        # and "apartment" (60) — sum 100. Path matches "/apartments" path
        # keyword (80) AND triggers anti_signal_non_inventory (-2_000).
        html = """
        <html><body>
          <a href="/our-apartments/amenities/">Tour Our Apartments</a>
        </body></html>
        """
        ranked = _rank_internal_links(html, "https://example.com/", limit=10)
        for url, score, _anchor in ranked:
            if "amenities" in url:
                assert score < 4_500, (
                    f"Anti-signal URL {url} got score {score}, above "
                    f"_UNIVERSAL_PRIOR=4500. Lift-leak regression."
                )

    def test_legit_floor_plan_still_lifts_above_universal(self) -> None:
        """Sanity check: the lift still fires for legitimate floor-plan
        URLs that have anchor+path keywords but no detail-class shape."""
        from ma_poc.pms.scraper import _rank_internal_links
        html = """
        <html><body>
          <a href="/floorplans/">View Our Floor Plans</a>
        </body></html>
        """
        ranked = _rank_internal_links(html, "https://example.com/", limit=10)
        assert ranked, "Floor-plans link should rank, not be filtered out"
        top_url, top_score, _ = ranked[0]
        assert "floorplans" in top_url
        # Bare index gets url_shape=1_500 (not anti-signal, not detail);
        # with anchor+path keywords the lift should bring it to >= 5_100.
        assert top_score >= 5_000, (
            f"Legitimate floor-plan index lost its lift. score={top_score}"
        )
