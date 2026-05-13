"""Tests for _c1_is_relevant_request — the C1 quiescence filter.

This function determines which in-flight requests count toward the
quiescence counter.  Static assets and analytics/tracking requests are
excluded so they cannot prevent the counter from reaching zero.

A false-negative here (incorrectly filtering a unit-data API) would cause
the quiescence loop to exit too early, missing XHR responses that contain
apartment pricing.  These tests guard that boundary.
"""

from __future__ import annotations

import pytest

from ma_poc.fetch.fetcher import _c1_is_relevant_request


class TestC1IsRelevantRequest:
    """Parameterised coverage for the static-asset and analytics filters."""

    # ── Static assets — must be filtered ──────────────────────────────────────

    @pytest.mark.parametrize("url", [
        "https://cdn.example.com/bundle.js",
        "https://cdn.example.com/styles.css",
        "https://fonts.googleapis.com/css2?family=Roboto",
        "https://example.com/logo.png",
        "https://example.com/hero.jpg",
        "https://example.com/icon.svg",
        "https://example.com/favicon.ico",
        "https://example.com/animation.gif",
        "https://example.com/bg.webp",
        "https://cdn.example.com/font.woff2",
        "https://cdn.example.com/font.woff",
        "https://cdn.example.com/font.ttf",
        "https://cdn.example.com/font.otf",
        "https://example.com/video.mp4",
        "https://example.com/source.map",
    ])
    def test_static_asset_is_not_relevant(self, url: str) -> None:
        assert _c1_is_relevant_request(url) is False, f"Expected {url!r} to be filtered"

    # ── Analytics / tracking — must be filtered ────────────────────────────────

    @pytest.mark.parametrize("url", [
        "https://www.google-analytics.com/collect",
        "https://www.googletagmanager.com/gtag/js?id=G-XXXX",
        "https://www.facebook.com/tr/?id=123",
        "https://ad.doubleclick.net/ddm/trackimp",
        "https://script.hotjar.com/modules/track",
        "https://api.segment.io/v1/track",
        "https://api.mixpanel.com/track",
        "https://heapanalytics.heap.io/track",
        "https://api.intercom.io/events",
        "https://js.drift.com/drift.js",
        "https://js.hubspot.com/forms.js",
        "https://client.crisp.chat/l.js",
        "https://static.zdassets.com/zopim.js",
        "https://tawk.to/s1/loader.js",
        "https://chatlio.com/chatlio.js",
        "https://browser.sentry-cdn.com/7.x/bundle.js",
        "https://d2wy8f7a9ursnm.cloudfront.net/v7/bugsnag.min.js",
        "https://cdn.rollbar.com/rollbarjs/refs/tags/latest.js",
        "https://fonts.googleapis.com/css?family=Open+Sans",
        "https://fonts.gstatic.com/s/roboto/v30/KFO.woff2",
    ])
    def test_analytics_url_is_not_relevant(self, url: str) -> None:
        assert _c1_is_relevant_request(url) is False, f"Expected {url!r} to be filtered"

    # ── Unit-data API endpoints — must NOT be filtered ─────────────────────────

    @pytest.mark.parametrize("url", [
        # RentCafe / Yardi JSON API
        "https://www.rentcafe.com/api/apartmentavailability/GetFlatData",
        # Entrata widget API
        "https://api2.entrata.com/v1/apartments/module/widgets/",
        # SightMap REST endpoint
        "https://sightmap.com/app/api/v1/abckey123/sightmaps/456/units",
        # AppFolio
        "https://mycompany.appfolio.com/listings.json",
        # Generic property availability endpoint
        "https://www.luxuryapartments.com/api/availability?page=1",
        # ResMan portal
        "https://rwfmat.myresman.com/Portal/Applicants/Availability",
        # RealPage
        "https://api.ws.realpage.com/v2/property/1234/units",
        # SecureCafe
        "https://myplace.securecafe.com/onlineleasing/myplace/floorplans.aspx",
    ])
    def test_unit_data_api_is_relevant(self, url: str) -> None:
        assert _c1_is_relevant_request(url) is True, f"Expected {url!r} to be relevant"

    # ── Query-string handling ──────────────────────────────────────────────────

    def test_static_asset_with_query_string_is_filtered(self) -> None:
        """Versioned JS bundles should still be filtered."""
        assert _c1_is_relevant_request(
            "https://cdn.example.com/bundle.js?v=20260512"
        ) is False

    def test_api_with_query_string_is_relevant(self) -> None:
        """API endpoints with query params must not be filtered by suffix."""
        assert _c1_is_relevant_request(
            "https://www.rentcafe.com/api/units?propertyId=123&page=1"
        ) is True

    # ── Edge cases ─────────────────────────────────────────────────────────────

    def test_empty_string_is_relevant(self) -> None:
        """Empty URL has no suffix and is not an analytics host — treated as relevant."""
        assert _c1_is_relevant_request("") is True

    def test_url_with_no_extension_is_relevant(self) -> None:
        assert _c1_is_relevant_request("https://api.example.com/availability") is True
