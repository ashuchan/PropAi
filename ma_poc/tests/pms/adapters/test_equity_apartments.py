"""Equity Apartments adapter (2026-05-21, HAR-validation greenfield).

Card data captured live from
www.equityapartments.com/los-angeles/financial-district/pegasus-apartments.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.equity_apartments import (
    EquityApartmentsAdapter,
    _parse_equity_unit,
    parse_equity_apartments,
)
from ma_poc.pms.detector import detect_pms

# Live-captured card text + href from Pegasus Apartments unit #718:
_REAL_UNIT_CARD = {
    "text": (
        "$1,660 What's my total cost? 0 Bed / 1 Bath 488 sq. ft. / Floor 7 "
        "Available 5/27/2026 This studio apartment home offers plenty of usable "
        "counter space in the kitchen. Eastern Exposure Hard Surface Flooring "
        "Kitchen Appl-Stainless"
    ),
    "unitFeesHref": "/UnitFees/29280/1/718",
    "priceText": "$1,660",
}

# One-bedroom variant (synthesized but matches the same DOM contract):
_ONE_BR_UNIT_CARD = {
    "text": (
        "$2,150 What's my total cost? 1 Bed / 1 Bath 650 sq. ft. / Floor 12 "
        "Available 6/15/2026 Spacious one-bedroom with downtown views."
    ),
    "unitFeesHref": "/UnitFees/29280/1/1205",
    "priceText": "$2,150",
}


# ── _parse_equity_unit ──


def test_parse_real_studio_unit() -> None:
    p = _parse_equity_unit(_REAL_UNIT_CARD)
    assert p is not None
    assert p["unit_number"] == "718"
    assert p["property_id"] == "29280"
    assert p["building_id"] == "1"
    assert p["rent"] == "1660"
    assert p["beds"] == "0"  # studio
    assert p["baths"] == "1"
    assert p["sqft"] == "488"
    assert p["floor"] == "7"
    assert p["availability_date"] == "5/27/2026"


def test_parse_one_bedroom_unit() -> None:
    p = _parse_equity_unit(_ONE_BR_UNIT_CARD)
    assert p["unit_number"] == "1205"
    assert p["beds"] == "1"
    assert p["sqft"] == "650"
    assert p["rent"] == "2150"


def test_parse_skips_card_with_no_unit_and_no_rent() -> None:
    bad = {"text": "Generic amenity blurb", "unitFeesHref": "", "priceText": ""}
    assert _parse_equity_unit(bad) is None


def test_parse_handles_missing_floor_or_avail() -> None:
    """Defensive: when Floor or Available is missing, parser still returns
    a row with what's available (rent + unit number).

    Note: real Equity unit IDs are numeric (e.g., 718, 1205). The
    /UnitFees/ regex requires digits in all three positions.
    """
    partial = {
        "text": "$1,200 1 Bed / 1 Bath 500 sq. ft.",  # no floor, no avail
        "unitFeesHref": "/UnitFees/100/1/200",
        "priceText": "$1,200",
    }
    p = _parse_equity_unit(partial)
    assert p["unit_number"] == "200"
    assert p["rent"] == "1200"
    assert p.get("floor", "") == ""
    assert p.get("availability_date", "") == ""


# ── parse_equity_apartments ──


def test_parse_full_payload() -> None:
    rows = parse_equity_apartments([_REAL_UNIT_CARD, _ONE_BR_UNIT_CARD], "u")
    assert len(rows) == 2
    studio = rows[0]
    assert studio["unit_number"] == "718"
    assert studio["bedrooms"] == "0"
    assert studio["bathrooms"] == "1"
    assert studio["sqft"] == "488"
    assert studio["floor"] == "7"
    assert studio["market_rent_low"] == 1660
    assert studio["availability_date"] == "5/27/2026"
    assert studio["availability_status"] == "AVAILABLE"
    assert studio["extraction_tier"] == "TIER_1_DOM_EQUITY_APARTMENTS"
    # Building id is preserved in the .building field for entity resolution.
    assert studio["building"] == "1"
    assert studio["unit_id"] == "1:718"
    assert studio["source_ids"] == {
        "equity_building_unit_id": "1:718",
        "equity_unit_id": "718",
        "equity_building_id": "1",
        "equity_property_id": "29280",
    }
    assert studio["source_property_id"] == "29280"
    assert studio["source_property_provenance"] == ("equity_unitfees_href.property_id")
    assert studio["source_response_provenance"] == "equity_visible_unit_card"


def test_dom_path_preserves_same_unit_number_in_two_buildings() -> None:
    second = dict(_REAL_UNIT_CARD)
    second["unitFeesHref"] = "/UnitFees/29280/2/718"

    rows = parse_equity_apartments([_REAL_UNIT_CARD, second], "u")

    assert [row["unit_number"] for row in rows] == ["718", "718"]
    assert [row["unit_id"] for row in rows] == ["1:718", "2:718"]


# ── adapter end-to-end ──


class _FakePage:
    def __init__(self, payload, url="https://www.equityapartments.com/x/y/z"):
        self._payload = payload
        self.url = url

    async def evaluate(self, _js):
        return self._payload


@pytest.mark.asyncio
async def test_adapter_extracts_visible_units() -> None:
    payload = {"ok": True, "units": [_REAL_UNIT_CARD, _ONE_BR_UNIT_CARD]}
    ctx = AdapterContext(
        base_url="https://www.equityapartments.com/los-angeles/financial-district/pegasus-apartments",
        detected=detect_pms("https://www.equityapartments.com/x/y/z"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    result = await EquityApartmentsAdapter().extract(_FakePage(payload), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_EQUITY_APARTMENTS"
    assert len(result.units) == 2
    assert result.confidence > 0.7
    assert result.api_responses[0]["via"] == "equity_apartments_dom"
    assert result.api_responses[0]["rows"] == 2


@pytest.mark.asyncio
async def test_adapter_bails_when_no_unit_cards() -> None:
    payload = {"ok": False, "reason": "no .unit-expanded-card elements"}
    ctx = AdapterContext(
        base_url="https://x.test/",
        detected=detect_pms("https://x.test/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    result = await EquityApartmentsAdapter().extract(_FakePage(payload), ctx)  # type: ignore[arg-type]
    assert result.confidence == 0.0


# ── detector ──


def test_detector_routes_equity_host() -> None:
    from ma_poc.pms.detector import _iter_html_markers

    html = '<link href="https://www.equityapartments.com/Content/Styles/x.css">'
    markers = list(_iter_html_markers(html.lower()))
    assert any(m[0] == "equity_apartments" for m in markers), markers


def test_detector_routes_unit_expanded_card_with_unitfees() -> None:
    """Belt-and-braces: even if the equityapartments.com host marker
    is not on a particular HAR sample, the .unit-expanded-card +
    /UnitFees/ combination is uniquely Equity's pattern."""
    from ma_poc.pms.detector import _iter_html_markers

    html = """
    <html><body>
      <div class="unit-expanded-card">
        <a href="/UnitFees/29280/1/718">fees</a>
      </div>
    </body></html>
    """
    markers = list(_iter_html_markers(html.lower()))
    assert any(m[0] == "equity_apartments" for m in markers)


def test_adapter_registered() -> None:
    a = get_adapter("equity_apartments")
    assert isinstance(a, EquityApartmentsAdapter)


def test_strategy_is_dom_first() -> None:
    from ma_poc.pms.detector import _STRATEGY_BY_PMS

    assert _STRATEGY_BY_PMS["equity_apartments"] == "dom_first"
