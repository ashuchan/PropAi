"""Market Apartments CMS adapter (2026-05-21, greenfield).

Probe data captured live from the 13-site GoPrisma-tagged cohort plus
the 4 marketapts-tagged probe failures (results_deep.jsonl, 600-prop
grind). Two SSR template variants are pinned here:

  * Template A — inline ``.floorplan-unit-single`` rows. Captured byte-
    for-byte from sandpiperapartmentssaltlakecity.com/floorplans
    (property code ``623SAN``).
  * Template B — drill-to-``/unit/{plan-slug}`` with ``.unit-table-row``
    rows. Captured from aspirethunderbird.com/floorplans (``1073ATHB``)
    and the per-plan drill ``/unit/1x1-cp3``.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.marketapts import (
    MarketAptsAdapter,
    parse_marketapts_template_a,
    parse_marketapts_template_b,
)
from ma_poc.pms.detector import detect_pms


# ── Template A fixtures ─────────────────────────────────────────────
# Sandpiper Apts: ``/floorplans`` SSR — .floorplan-block with two
# inline .floorplan-unit-single rows (verified live; data-when /
# data-price / unit-number all byte-identical).
_PLAN_A_TWO_UNITS = {
    "template": "A",
    "beds": "2",
    "baths": "1",
    "name": "2 Bedroom 1 Bath",
    "startingPrice": "Starting at $1,749",
    "units": [
        {
            "unitNumber": "213",
            "dataWhen": "2026-05-21",
            "dataPrice": "1769",
            "dataBeds": "2",
            "dataBaths": "1",
            "priceText": "$1769",
            "availText": "May 21, 2026",
        },
        {
            "unitNumber": "117",
            "dataWhen": "2026-06-10",
            "dataPrice": "1749",
            "dataBeds": "2",
            "dataBaths": "1",
            "priceText": "$1749",
            "availText": "June 10, 2026",
        },
    ],
}
# Plan with no unit rows → plan-level row fallback.
_PLAN_A_PLAN_ONLY = {
    "template": "A",
    "beds": "3",
    "baths": "2",
    "name": "3 Bedroom 2 Bath",
    "startingPrice": "From $2,199",
    "units": [],
}


# ── Template B fixtures ─────────────────────────────────────────────
# Aspire Thunderbird /floorplans plan card → /unit/1x1-cp3 drill.
# 2 inline unit-table-row entries on the drill page (verified live;
# column order Unit / Rent / Available / Special / Features / Apply).
_PLAN_B_TWO_UNITS = {
    "template": "B",
    "title": "1X1-CP3",
    "features": "SQ FEET: 500\nBEDROOMS: 1\nBATHROOMS: 1\nDEPOSIT: $500",
    "startingPrice": "$949",
    "drillPath": "/unit/1x1-cp3",
    "units": [
        {
            "cells": ["1060", "$949", "Now", "", "First Floor, Prior Reno 3", "APPLY"],
            "dataAttrs": {},
        },
        {
            "cells": ["1063", "$949", "Now", "", "Prior Reno 3", "APPLY"],
            "dataAttrs": {},
        },
    ],
}
# Plan whose drill returned nothing or "Contact Us for More Details" —
# adapter must surface a plan-level row.
_PLAN_B_PLAN_ONLY = {
    "template": "B",
    "title": "1X1-CC",
    "features": "SQ FEET: 500\nBEDROOMS: 1\nBATHROOMS: 1\nDEPOSIT: $OAC",
    "startingPrice": "Contact Us for More Details",
    "drillPath": "/unit/1x1-cc",
    "units": [],
}


class _FakePage:
    """Tiny stand-in for a Playwright page. ``extract`` only calls
    ``page.evaluate`` and ``page.url``."""

    def __init__(
        self,
        payload: object,
        url: str = "https://www.sandpiperapartmentssaltlakecity.com/floorplans",
    ) -> None:
        self._payload = payload
        self.url = url

    async def evaluate(self, _js: str, *_a: object) -> object:
        return self._payload


def _ctx(base_url: str = "https://www.sandpiperapartmentssaltlakecity.com/") -> AdapterContext:
    return AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )


# ── Template A parser tests ─────────────────────────────────────────


def test_template_a_unit_level_two_rows() -> None:
    units = parse_marketapts_template_a([_PLAN_A_TWO_UNITS], "u")
    assert len(units) == 2
    u0 = units[0]
    assert u0["floor_plan_name"] == "2 Bedroom 1 Bath"
    assert u0["unit_number"] == "213"
    assert u0["bedrooms"] == "2"
    assert u0["bathrooms"] == "1"
    assert u0["market_rent_low"] == 1769
    assert u0["market_rent_high"] == 1769
    assert u0["availability_date"] == "2026-05-21"  # data-when (ISO) wins
    assert u0["availability_status"] == "AVAILABLE"
    assert u0["extraction_tier"] == "TIER_1_DOM_MARKETAPTS"

    u1 = units[1]
    assert u1["unit_number"] == "117"
    assert u1["market_rent_low"] == 1749
    assert u1["availability_date"] == "2026-06-10"


def test_template_a_plan_level_fallback_no_units() -> None:
    rows = parse_marketapts_template_a([_PLAN_A_PLAN_ONLY], "u")
    assert len(rows) == 1
    p = rows[0]
    assert p["unit_number"] == ""
    assert p["floor_plan_name"] == "3 Bedroom 2 Bath"
    assert p["bedrooms"] == "3"
    assert p["market_rent_low"] == 2199


def test_template_a_mixed_plans() -> None:
    rows = parse_marketapts_template_a(
        [_PLAN_A_TWO_UNITS, _PLAN_A_PLAN_ONLY], "u"
    )
    assert len(rows) == 3  # 2 unit-level + 1 plan-level fallback


# ── Template B parser tests ─────────────────────────────────────────


def test_template_b_unit_level_two_rows() -> None:
    units = parse_marketapts_template_b([_PLAN_B_TWO_UNITS], "u")
    assert len(units) == 2
    u0 = units[0]
    assert u0["floor_plan_name"] == "1X1-CP3"
    assert u0["unit_number"] == "1060"
    assert u0["market_rent_low"] == 949
    assert u0["availability_date"] == ""  # "Now" → blank date
    assert u0["bedrooms"] == "1"
    assert u0["bathrooms"] == "1"
    assert u0["sqft"] == "500"
    assert u0["extraction_tier"] == "TIER_1_DOM_MARKETAPTS"
    assert units[1]["unit_number"] == "1063"


def test_template_b_plan_level_when_drill_empty() -> None:
    rows = parse_marketapts_template_b([_PLAN_B_PLAN_ONLY], "u")
    assert len(rows) == 1
    p = rows[0]
    assert p["unit_number"] == ""
    assert p["floor_plan_name"] == "1X1-CC"
    assert p["bedrooms"] == "1"
    assert p["sqft"] == "500"
    # "Contact Us for More Details" has no $ → no starting price → UNAVAILABLE
    assert p["market_rent_low"] is None
    assert p["availability_status"] == "UNAVAILABLE"


def test_template_b_date_extraction_variants() -> None:
    """The drill table cell order isn't 100% stable; the parser must
    walk all cells positionally to find rent + availability date and not
    consume the wrong one (regression against the Apply-button cell
    sometimes interleaving)."""
    plan = {
        "template": "B",
        "title": "B2",
        "features": "SQ FEET: 900\nBEDROOMS: 2\nBATHROOMS: 2",
        "startingPrice": "$1,300",
        "drillPath": "/unit/b2",
        "units": [
            {
                "cells": ["2410", "$1,350", "May 21, 2026", "First Floor", "APPLY"],
                "dataAttrs": {},
            },
            {
                "cells": ["2412", "$1,400", "2026-07-01", "", "APPLY"],
                "dataAttrs": {},
            },
            {
                "cells": ["2414", "$1,425", "06/15/2026", "", "APPLY"],
                "dataAttrs": {},
            },
        ],
    }
    rows = parse_marketapts_template_b([plan], "u")
    assert len(rows) == 3
    assert rows[0]["availability_date"] == "May 21, 2026"
    assert rows[1]["availability_date"] == "2026-07-01"
    assert rows[2]["availability_date"] == "06/15/2026"


def test_template_b_studio_features_blob() -> None:
    """A studio plan has BEDROOMS: Studio (no integer); _parse_specs_blob
    must map this to ``bedrooms == "0"``."""
    plan = {
        "template": "B",
        "title": "ST1",
        "features": "SQ FEET: 450\nBEDROOMS: Studio\nBATHROOMS: 1\nDEPOSIT: $400",
        "startingPrice": "$1,125",
        "drillPath": "",
        "units": [],
    }
    rows = parse_marketapts_template_b([plan], "u")
    assert rows[0]["bedrooms"] == "0"
    assert rows[0]["sqft"] == "450"
    assert rows[0]["market_rent_low"] == 1125


# ── Adapter end-to-end ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_template_a_extract() -> None:
    payload = {"template": "A", "plans": [_PLAN_A_TWO_UNITS]}
    result = await MarketAptsAdapter().extract(_FakePage(payload), _ctx())  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_MARKETAPTS_A"
    assert len(result.units) == 2
    assert result.units[0]["unit_number"] == "213"
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_adapter_template_b_extract() -> None:
    payload = {"template": "B", "plans": [_PLAN_B_TWO_UNITS]}
    fake = _FakePage(payload, url="https://www.aspirethunderbird.com/floorplans")
    result = await MarketAptsAdapter().extract(fake, _ctx("https://www.aspirethunderbird.com/"))  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_MARKETAPTS_B"
    assert len(result.units) == 2
    assert result.units[0]["unit_number"] == "1060"
    assert result.units[0]["market_rent_low"] == 949
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_adapter_no_plans_returns_failure() -> None:
    payload = {"template": "NONE", "plans": []}
    result = await MarketAptsAdapter().extract(_FakePage(payload), _ctx())  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_adapter_non_dict_payload_returns_failure() -> None:
    """DOM JS returns None or a list under hostile pages — adapter must
    not crash and must surface confidence 0."""
    result = await MarketAptsAdapter().extract(_FakePage(None), _ctx())  # type: ignore[arg-type]
    assert result.confidence == 0.0
    assert "non-dict" in " ".join(result.errors)


# ── Detector + registry ─────────────────────────────────────────────


def test_detector_routes_marketapts_assets_marker() -> None:
    """The CMS asset host marker should route to the marketapts adapter
    (not the generic LLM cascade)."""
    from ma_poc.pms.detector import _iter_html_markers
    html = '<img src="https://assets.marketapts.com/assets/converted/623SAN/images/x.jpg">'
    markers = list(_iter_html_markers(html))
    assert any(m[0] == "marketapts" for m in markers), markers


def test_detector_routes_marketapts_api_marker() -> None:
    """The widget-config API host is the second canonical marker; also
    routes to marketapts."""
    from ma_poc.pms.detector import _iter_html_markers
    html = '<script src="https://api.marketapts.com/v1/widget-config/623SAN.json"></script>'
    markers = list(_iter_html_markers(html))
    assert any(m[0] == "marketapts" for m in markers), markers


def test_detector_routes_marketapts_powered_by_footer() -> None:
    """``Powered by MarketApts`` in the footer is the third canonical
    marker — works on properties whose marketing site forwards assets
    through a CDN that hides the marketapts host."""
    from ma_poc.pms.detector import _iter_html_markers
    html = "<footer>...Powered by MarketApts...</footer>"
    markers = list(_iter_html_markers(html.lower()))
    assert any(m[0] == "marketapts" for m in markers), markers


def test_adapter_registered() -> None:
    adapter = get_adapter("marketapts")
    assert isinstance(adapter, MarketAptsAdapter)
    assert adapter.pms_name == "marketapts"


def test_adapter_static_fingerprints_include_canonical_hosts() -> None:
    adapter = MarketAptsAdapter()
    fps = adapter.static_fingerprints()
    assert "marketapts.com" in fps
    assert "assets.marketapts.com" in fps
    assert "api.marketapts.com" in fps


def test_strategy_for_marketapts_is_dom_first() -> None:
    """marketapts must be classified ``dom_first`` so the orchestrator
    runs the DOM extractor before falling to API-first / LLM cascade."""
    from ma_poc.pms.detector import _STRATEGY_BY_PMS
    assert _STRATEGY_BY_PMS["marketapts"] == "dom_first"
