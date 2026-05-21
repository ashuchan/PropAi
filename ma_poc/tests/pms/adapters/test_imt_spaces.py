"""IMT Spaces adapter (2026-05-21, HAR-validation greenfield).

Captured live from www.imtresidential.com/properties/imt-sorrento-valley/
apartments/ — 57 ``<article class="spaces-plan">`` elements with
``data-spaces-*`` attributes. Plan-level only by design.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.imt_spaces import (
    ImtSpacesAdapter,
    parse_imt_spaces_plans,
)
from ma_poc.pms.detector import detect_pms


# Real IMT plan (verified live 2026-05-21).
_REAL_IMT_PLAN = {
    "classes": "spaces-plan spaces__plan upgraded 2bed 2bath price_2500plus tag-upgraded spaces-community-imt-sorrento-valley spaces-market-all-communities spaces-market-california floor_36248 patio-or-balcony washer-and-dryer wood-style-flooring",
    "titleAttr": "B2 Upgrade",
    "data": {
        "spacesAvailable": "true",
        "spacesBathCount": "2",
        "spacesObj": "plan",
        "spacesPlan": "147445",
        "spacesSoonest": "2026-07-07",
        "spacesSortArea": "878",
        "spacesSortBed": "2",
        "spacesSortDate": "1783382400",
        "spacesSortPlanName": "B2 Upgrade",
        "spacesSortPrice": "3195",
    },
}

# Studio plan variant (synthesized to test 0-bed).
_STUDIO_IMT_PLAN = {
    "classes": "spaces-plan studio 1bath",
    "titleAttr": "Studio Loft",
    "data": {
        "spacesAvailable": "true",
        "spacesBathCount": "1",
        "spacesPlan": "147600",
        "spacesSoonest": "2026-06-15",
        "spacesSortArea": "550",
        "spacesSortBed": "0",
        "spacesSortPlanName": "Studio Loft",
        "spacesSortPrice": "2100",
    },
}

# Unavailable plan — data-spaces-available="false" → UNAVAILABLE status.
_UNAVAIL_IMT_PLAN = {
    "classes": "spaces-plan 1bed 1bath",
    "titleAttr": "A1",
    "data": {
        "spacesAvailable": "false",
        "spacesBathCount": "1",
        "spacesPlan": "147601",
        "spacesSoonest": "",
        "spacesSortArea": "650",
        "spacesSortBed": "1",
        "spacesSortPlanName": "A1",
        "spacesSortPrice": "2350",
    },
}


def test_parse_real_imt_plan_extracts_all_fields() -> None:
    rows = parse_imt_spaces_plans([_REAL_IMT_PLAN], "u")
    assert len(rows) == 1
    r = rows[0]
    assert r["floor_plan_name"] == "B2 Upgrade"
    assert r["bedrooms"] == "2"
    assert r["bathrooms"] == "2"
    assert r["sqft"] == "878"
    assert r["market_rent_low"] == 3195
    assert r["market_rent_high"] == 3195
    assert r["availability_status"] == "AVAILABLE"
    assert r["availability_date"] == "2026-07-07"
    assert r["unit_number"] == ""  # plan-level only
    assert r["extraction_tier"] == "TIER_1_DOM_IMT_SPACES"


def test_parse_studio_plan_maps_bed_zero() -> None:
    rows = parse_imt_spaces_plans([_STUDIO_IMT_PLAN], "u")
    assert len(rows) == 1
    assert rows[0]["bedrooms"] == "0"
    assert rows[0]["bed_label"]


def test_parse_unavailable_plan_status() -> None:
    rows = parse_imt_spaces_plans([_UNAVAIL_IMT_PLAN], "u")
    assert len(rows) == 1
    assert rows[0]["availability_status"] == "UNAVAILABLE"


def test_parse_skips_plan_with_no_name_and_no_rent() -> None:
    """Defensive: a card with neither a title nor a price isn't useful."""
    bad = {"classes": "spaces-plan", "titleAttr": "", "data": {"spacesAvailable": "true"}}
    rows = parse_imt_spaces_plans([bad], "u")
    assert rows == []


def test_parse_falls_back_to_sort_plan_name_when_title_missing() -> None:
    """When article has no ``title`` attribute, use
    ``data-spaces-sort-plan-name`` as the plan name."""
    p = dict(_REAL_IMT_PLAN)
    p["titleAttr"] = ""
    p["data"] = dict(p["data"])
    rows = parse_imt_spaces_plans([p], "u")
    assert rows[0]["floor_plan_name"] == "B2 Upgrade"  # came from data-spaces-sort-plan-name


def test_parse_handles_half_baths() -> None:
    """Bathroom values like "1.5" must pass through as-is (not be cast to int)."""
    p = dict(_REAL_IMT_PLAN)
    p["data"] = dict(p["data"])
    p["data"]["spacesBathCount"] = "1.5"
    rows = parse_imt_spaces_plans([p], "u")
    assert rows[0]["bathrooms"] == "1.5"


# ── adapter end-to-end ─────────────────────────────────────────────


class _FakePage:
    def __init__(self, payload, url="https://www.imtresidential.com/properties/x/apartments/"):
        self._payload = payload
        self.url = url

    async def evaluate(self, _js):
        return self._payload


@pytest.mark.asyncio
async def test_adapter_extracts_imt_plan() -> None:
    payload = {"ok": True, "plans": [_REAL_IMT_PLAN, _STUDIO_IMT_PLAN]}
    ctx = AdapterContext(
        base_url="https://www.imtresidential.com/properties/imt-sorrento-valley/apartments/",
        detected=detect_pms("https://www.imtresidential.com/properties/imt-sorrento-valley/apartments/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    result = await ImtSpacesAdapter().extract(_FakePage(payload), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_IMT_SPACES"
    assert len(result.units) == 2
    names = sorted(r["floor_plan_name"] for r in result.units)
    assert names == ["B2 Upgrade", "Studio Loft"]
    assert result.confidence > 0.6


@pytest.mark.asyncio
async def test_adapter_bails_when_no_spaces_plan_articles() -> None:
    payload = {"ok": False, "reason": "no article.spaces-plan elements on page"}
    ctx = AdapterContext(
        base_url="https://x.test/",
        detected=detect_pms("https://x.test/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    result = await ImtSpacesAdapter().extract(_FakePage(payload), ctx)  # type: ignore[arg-type]
    assert result.confidence == 0.0


def test_detector_routes_imt_host_with_spaces_markers() -> None:
    from ma_poc.pms.detector import _iter_html_markers
    html = """
    <html><body>
      <article class="spaces-plan spaces-community-imt-sorrento-valley"
               data-spaces-plan="147445"
               data-spaces-sort-price="3195">x</article>
    </body></html>
    """
    markers = list(_iter_html_markers(html.lower()))
    assert any(m[0] == "imt_spaces" for m in markers), markers


def test_detector_routes_via_host_alone_when_spaces_markers_present() -> None:
    """imtresidential.com host + spaces-plan + data-spaces-plan together
    are enough to route, even without the spaces-community- class
    (which is per-property and varies across the portfolio)."""
    from ma_poc.pms.detector import _iter_html_markers
    html = """
    <html><body>
      <link href="https://www.imtresidential.com/x">x</link>
      <article class="spaces-plan"
               data-spaces-plan="1"
               data-spaces-sort-price="2000">x</article>
    </body></html>
    """
    markers = list(_iter_html_markers(html.lower()))
    assert any(m[0] == "imt_spaces" for m in markers)


def test_detector_does_NOT_route_when_spaces_plan_class_missing() -> None:
    """Page mentions 'spaces' in unrelated context (e.g. amenity name)
    but lacks the article.spaces-plan + data-spaces-plan combo. Must NOT
    route to imt_spaces."""
    from ma_poc.pms.detector import _iter_html_markers
    html = """
    <html><body>
      <p>Co-working spaces available!</p>
      <div class="amenity-spaces">Outdoor spaces</div>
    </body></html>
    """
    markers = list(_iter_html_markers(html.lower()))
    imt_markers = [m for m in markers if m[0] == "imt_spaces"]
    assert not imt_markers


def test_adapter_registered() -> None:
    a = get_adapter("imt_spaces")
    assert isinstance(a, ImtSpacesAdapter)


def test_strategy_is_dom_first() -> None:
    from ma_poc.pms.detector import _STRATEGY_BY_PMS
    assert _STRATEGY_BY_PMS["imt_spaces"] == "dom_first"
