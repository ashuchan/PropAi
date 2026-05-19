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
