"""Generic SSR floor-plan DOM fallback (2026-05-19).

Catches the long-tail custom-CMS sites where plan-level data is rendered
in repeated containers one labelled nav-hop deep. Conservative against
false positives: ≥2 of {bed/bath, sqft, $} per card, container text <800
chars, ≥2 admitted cards total, sqft-or-rent required.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters._generic_dom_floorplans import (
    parse_generic_floorplan_cards,
    recover_generic_floorplans,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import detect_pms


# Real-shape cards (mckinley-style "The Birch $1,179-$1,349" + similar)
_GOOD_CARDS = [
    {"name": "The Birch", "text": "The Birch 1 Bed 1 Bath 850 sq ft $1,179 - $1,349 Available Now", "klass": "fp-card"},
    {"name": "Schooner Cove", "text": "Schooner Cove 2 Bed 1 Bath 950 sq ft $1,746 Available Jun 1, 2026", "klass": "fp-card"},
    {"name": "Studio Loft", "text": "Studio Loft Studio 1 Bath 500 sq ft $895 - $950 2 Units Available", "klass": "fp-card"},
]

# False-positive guards
_NAV_LIKE_CARDS = [
    # No $, no sqft — just bd/ba mention → reject
    {"name": "Browse", "text": "Browse our 1 bed and 2 bed apartments", "klass": "nav-card"},
    # $ but no bed/bath/sqft → reject
    {"name": "Deposit", "text": "Security deposit $500 per unit", "klass": "info-card"},
]


class _FakePage:
    def __init__(self, scan: object, url: str = "https://www.example.com/floorplans/") -> None:
        self._scan = scan
        self.url = url

    async def evaluate(self, _js: str, *_a: object) -> object:
        return self._scan


def _ctx() -> AdapterContext:
    return AdapterContext(
        base_url="https://www.example.com/",
        detected=detect_pms("https://www.example.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )


def test_parse_good_cards() -> None:
    units = parse_generic_floorplan_cards(_GOOD_CARDS, "u")
    assert len(units) == 3
    birch = units[0]
    assert birch["floor_plan_name"] == "The Birch"
    assert birch["bedrooms"] == "1"
    assert birch["bathrooms"] == "1"
    assert birch["sqft"] == "850"
    assert birch["market_rent_low"] == 1179
    assert birch["market_rent_high"] == 1349
    assert birch["availability_status"] == "AVAILABLE"
    assert birch["extraction_tier"] == "TIER_3_DOM_GENERIC"

    # studio
    s = units[2]
    assert s["bedrooms"] == "0"
    assert s["bed_label"]
    assert s["market_rent_low"] == 895
    assert s["market_rent_high"] == 950
    assert s["available_units"] == "2"


def test_parse_filters_low_signal_cards() -> None:
    """Cards with neither sqft nor rent should be dropped."""
    units = parse_generic_floorplan_cards(_NAV_LIKE_CARDS, "u")
    assert units == []


def test_parse_filters_below_money_threshold() -> None:
    """$ below 3 digits ($1, $50) is not rent — rejected by regex."""
    cards = [{"name": "Plan", "text": "Plan 1 Bed 1 Bath 700 sq ft only $1 application fee", "klass": "c"}]
    units = parse_generic_floorplan_cards(cards, "u")
    # No real $ rent → no rent_low/high; sqft is present so it IS admitted
    # (sqft alone keeps it; final guard requires sqft OR rent).
    assert len(units) == 1
    assert units[0]["market_rent_low"] is None
    assert units[0]["sqft"] == "700"


def test_parse_waitlist_status() -> None:
    cards = [{"name": "A", "text": "Plan A 1 Bed 1 Bath 600 sq ft $1,200 Join the Waitlist", "klass": "c"}]
    units = parse_generic_floorplan_cards(cards, "u")
    assert units[0]["availability_status"] == "UNAVAILABLE"


def test_parse_call_for_pricing_no_rent() -> None:
    cards = [{"name": "B", "text": "Plan B 1 Bed 1 Bath 700 sq ft Call for pricing", "klass": "c"}]
    units = parse_generic_floorplan_cards(cards, "u")
    # No $, has bd/bath/sqft. Call-for-pricing → UNAVAILABLE.
    assert units[0]["availability_status"] == "UNAVAILABLE"
    assert units[0]["market_rent_low"] is None


@pytest.mark.asyncio
async def test_recover_returns_units_when_scan_yields_cards() -> None:
    scan = {"cards": _GOOD_CARDS, "winningPath": "/floorplans/", "count": 3}
    page = _FakePage(scan)
    units, path = await recover_generic_floorplans(page, _ctx())  # type: ignore[arg-type]
    assert len(units) == 3
    assert path == "/floorplans/"


@pytest.mark.asyncio
async def test_recover_rejects_single_card() -> None:
    """Single card → reject (need ≥2 for confidence)."""
    scan = {"cards": _GOOD_CARDS[:1], "winningPath": "/floorplans/", "count": 1}
    page = _FakePage(scan)
    units, _ = await recover_generic_floorplans(page, _ctx())  # type: ignore[arg-type]
    assert units == []


@pytest.mark.asyncio
async def test_recover_empty_scan() -> None:
    page = _FakePage({"cards": [], "winningPath": "", "count": 0})
    units, _ = await recover_generic_floorplans(page, _ctx())  # type: ignore[arg-type]
    assert units == []


@pytest.mark.asyncio
async def test_recover_pageless_stub() -> None:
    class _Bare:
        url = "https://x.com/"

    units, _ = await recover_generic_floorplans(_Bare(), _ctx())  # type: ignore[arg-type]
    assert units == []


# ─────────────────────────────────────────────────────────────────────
# 2026-05-20 brand-CMS URL discovery: `/apartments/{state}/{city}/floor-plans`
# The TIER_3_DOM + TIER_MERGED ALL_fail probes found ~16-18% of failed
# properties use a multi-property brand template (Lincoln, McKinley,
# HG Living, MG Properties) where `/floorplans` returns 404 but the real
# floor-plans page lives at `/apartments/{state}/{city}/floor-plans`.
# The JS discovery scans landing-page hrefs for this pattern.
# ─────────────────────────────────────────────────────────────────────


def test_js_has_brand_cms_url_pattern() -> None:
    """The JS source contains the brand-CMS regex + the scan + probe loop.

    Structural test only — we can't execute the JS in unit tests; this
    pins the source-shape contract so accidental removal during refactor
    fails CI loudly.
    """
    from ma_poc.pms.adapters._generic_dom_floorplans import _GENERIC_DOM_JS

    js = _GENERIC_DOM_JS
    # Pattern matches `/apartments/{state-slug}/{city-slug}/(floor-plans|floorplans)`
    assert "/apartments/" in js
    assert "floor-plans|floorplans" in js
    # Scan source: anchors on the landing page
    assert "a[href]" in js
    # The scan dedupes via Set
    assert "new Set()" in js
    # The fall-through ordering: brand-CMS scan is LAST (after standard
    # subpaths) so common /floorplans paths still win first
    assert js.index("SUBPATHS") < js.index("BRAND_HREF_RE")


def test_brand_cms_pattern_matches_real_examples() -> None:
    """Regression: the regex used in the JS source must match the real-world
    brand-CMS URLs observed during the 2026-05-20 TIER_3_DOM probe."""
    import re

    # Mirror the JS regex literally (case-insensitive, anchored)
    py_re = re.compile(
        r"^(\/apartments\/[a-z-]+\/[a-z0-9-]+\/(?:floor-plans|floorplans))(?:[?#].*)?$",
        re.IGNORECASE,
    )
    # Live-verified URLs from the TIER_3_DOM probe
    matches = [
        "/apartments/tx/san-antonio/floor-plans",       # Fairways 5
        "/apartments/ca/los-angeles/floor-plans",       # Museum Terrace
        "/apartments/ca/san-jose/floor-plans",          # Villas Willow Glen
        "/apartments/tx/lubbock/floor-plans",           # Renaissance at Northpark
        "/apartments/wa/burien/alcove-at-seahurst/floor-plans",  # HG Living
        "/apartments/michigan/ypsilanti/roundtree/floorplans",   # McKinley (no dash)
        "/apartments/tx/san-antonio/floor-plans?utm=x",  # with query string
        "/apartments/tx/san-antonio/floor-plans#section",  # with fragment
    ]
    for url in matches:
        assert py_re.match(url), f"expected brand-CMS regex to match {url!r}"


def test_brand_cms_pattern_rejects_non_brand_urls() -> None:
    """The regex must NOT match URLs that aren't the brand-CMS shape —
    avoid false-positive probes that waste fetches and could surface
    irrelevant scan results."""
    import re

    py_re = re.compile(
        r"^(\/apartments\/[a-z-]+\/[a-z0-9-]+\/(?:floor-plans|floorplans))(?:[?#].*)?$",
        re.IGNORECASE,
    )
    rejects = [
        "/floorplans",                              # plain root
        "/apartments",                              # too shallow
        "/apartments/tx",                           # missing city
        "/apartments/tx/san-antonio",               # missing /floor-plans
        "/apartments/tx/san-antonio/amenities",     # wrong tail
        "/apartments/tx/san-antonio/floor-plans/the-birch",  # nested deeper
        "https://example.com/apartments/tx/san-antonio/floor-plans",  # absolute (not relative)
    ]
    for url in rejects:
        assert not py_re.match(url), f"expected regex to REJECT {url!r}"


@pytest.mark.asyncio
async def test_recover_accepts_brand_cms_winning_path() -> None:
    """Full recovery flow: scan result reports a brand-CMS winning path
    with valid cards → admit. Same code path as standard /floorplans win;
    the brand path just looks different in the source_api_url provenance.
    """
    scan = {
        "cards": _GOOD_CARDS,
        "winningPath": "/apartments/tx/san-antonio/floor-plans",
        "count": 3,
    }
    page = _FakePage(scan, url="https://www.fairways5.com/")
    units, path = await recover_generic_floorplans(page, _ctx())  # type: ignore[arg-type]
    assert len(units) == 3
    assert path == "/apartments/tx/san-antonio/floor-plans"
    # Provenance is stamped on each unit dict via the recover wrapper
    assert all(
        "fairways5.com/apartments/tx/san-antonio/floor-plans" in (u.get("source_api_url") or "")
        for u in units
    )
