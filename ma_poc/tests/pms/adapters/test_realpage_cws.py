"""RealPage CWS RPFP widget adapter (2026-05-21, HAR-validation greenfield).

Card text captured live from
  - www.liveatpenthouse.com/Floor-Plans.aspx (CWS/2256871)
  - www.liveatshadowglen.com/Floor-Plans.aspx (CWS/2267476)
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.realpage_cws import (
    RealPageCwsAdapter,
    _parse_card_text,
    parse_realpage_cws_plans,
)
from ma_poc.pms.detector import detect_pms


# Live-captured liveatshadowglen card texts (2026-05-21):
_ONE_BEDROOM_CARD = (
    "One Bedroom 1 Bed1 Bath 566 - 784 Sqft $2,100 - $2,120 "
    "Brochure Contact Us"
)
_TWO_BEDROOM_CARD = (
    "Two Bedroom 2 Bed1.5 Bath 870 - 965 Sqft $2,495 - $2,620 "
    "Brochure Contact Us"
)
# Synthetic studio for the bed-mapping test.
_STUDIO_CARD = (
    "Studio Loft Studio Bed 1 Bath 450 Sqft $1,800 Brochure Contact Us"
)
# Single-price card (no range).
_SINGLE_PRICE_CARD = (
    "A2 1 Bed1 Bath 650 Sqft $1,950 Brochure Contact Us"
)


def test_parse_card_one_bedroom_range() -> None:
    p = _parse_card_text(_ONE_BEDROOM_CARD)
    assert p["beds"] == 1
    assert p["baths"] == "1"
    assert p["sqft_low"] == "566"
    assert p["sqft_high"] == "784"
    assert p["rent_low"] == 2100
    assert p["rent_high"] == 2120


def test_parse_card_two_bedroom_half_bath() -> None:
    p = _parse_card_text(_TWO_BEDROOM_CARD)
    assert p["beds"] == 2
    assert p["baths"] == "1.5"
    assert p["rent_low"] == 2495
    assert p["rent_high"] == 2620


def test_parse_card_studio() -> None:
    p = _parse_card_text(_STUDIO_CARD)
    assert p["beds"] == 0  # studio → 0
    assert p["sqft_low"] == "450"
    assert p["sqft_high"] == "450"  # single value when no range


def test_parse_card_single_price_no_range() -> None:
    p = _parse_card_text(_SINGLE_PRICE_CARD)
    assert p["rent_low"] == 1950
    assert p["rent_high"] == 1950


def test_parse_plans_produces_plan_level_rows() -> None:
    plans = [
        {"planName": "One Bedroom", "cardText": _ONE_BEDROOM_CARD, "classes": "rpfp-card"},
        {"planName": "Two Bedroom", "cardText": _TWO_BEDROOM_CARD, "classes": "rpfp-card"},
    ]
    rows = parse_realpage_cws_plans(plans, "u")
    assert len(rows) == 2
    one_br = rows[0]
    assert one_br["floor_plan_name"] == "One Bedroom"
    assert one_br["unit_number"] == ""  # plan-level
    assert one_br["bedrooms"] == "1"
    assert one_br["bathrooms"] == "1"
    assert one_br["sqft"] == "784"  # high end of range
    assert one_br["market_rent_low"] == 2100
    assert one_br["market_rent_high"] == 2120
    assert one_br["availability_status"] == "AVAILABLE"
    assert one_br["extraction_tier"] == "TIER_1_DOM_REALPAGE_CWS"

    two_br = rows[1]
    assert two_br["bedrooms"] == "2"
    assert two_br["bathrooms"] == "1.5"
    assert two_br["market_rent_low"] == 2495


def test_parse_plans_skips_cards_with_no_name_or_rent() -> None:
    bad = [{"planName": "", "cardText": "Brochure Contact Us", "classes": "rpfp-card"}]
    rows = parse_realpage_cws_plans(bad, "u")
    assert rows == []


# ── adapter end-to-end ──


class _FakePage:
    def __init__(self, payload, url="https://www.liveatshadowglen.com/Floor-Plans.aspx"):
        self._payload = payload
        self.url = url

    async def evaluate(self, _js):
        return self._payload


@pytest.mark.asyncio
async def test_adapter_extracts_rpfp_cards() -> None:
    payload = {
        "ok": True,
        "plans": [
            {"planName": "One Bedroom", "cardText": _ONE_BEDROOM_CARD, "classes": "rpfp-card"},
            {"planName": "Two Bedroom", "cardText": _TWO_BEDROOM_CARD, "classes": "rpfp-card"},
        ],
    }
    ctx = AdapterContext(
        base_url="https://www.liveatshadowglen.com/",
        detected=detect_pms("https://www.liveatshadowglen.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    result = await RealPageCwsAdapter().extract(_FakePage(payload), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_REALPAGE_CWS"
    assert len(result.units) == 2
    assert result.confidence > 0.6


@pytest.mark.asyncio
async def test_adapter_bails_when_no_rpfp_container() -> None:
    payload = {"ok": False, "reason": "no .rpfp-container present"}
    ctx = AdapterContext(
        base_url="https://x.test/",
        detected=detect_pms("https://x.test/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    result = await RealPageCwsAdapter().extract(_FakePage(payload), ctx)  # type: ignore[arg-type]
    assert result.confidence == 0.0


def test_detector_routes_via_cs_cdn_realpage() -> None:
    """``cs-cdn.realpage.com/CWS/`` asset host alone is a sufficient
    marker — distinct from leasing.realpage.com (OLL adapter)."""
    from ma_poc.pms.detector import _iter_html_markers
    html = """
    <html><body>
      <link href="https://cs-cdn.realpage.com/cws/2267476/cmsscripts/x.css">
    </body></html>
    """
    markers = list(_iter_html_markers(html.lower()))
    assert any(m[0] == "realpage_cws" for m in markers), markers


def test_detector_routes_via_rpfp_container_and_card() -> None:
    """Both classes present in HTML body is sufficient for routing
    (catches sites whose CDN assets aren't in the captured snippet)."""
    from ma_poc.pms.detector import _iter_html_markers
    html = """
    <html><body>
      <div class="rpfp-container floorplans-widget-1">
        <div class="rpfp-cards">
          <div class="rpfp-card">One Bedroom 1 Bed1 Bath 500 Sqft $1,500</div>
        </div>
      </div>
    </body></html>
    """
    markers = list(_iter_html_markers(html.lower()))
    assert any(m[0] == "realpage_cws" for m in markers)


def test_detector_does_not_false_fire_on_leasing_realpage() -> None:
    """leasing.realpage.com (OLL workflow URL) is a different RealPage
    product. CWS detector must not fire just because realpage.com
    appears somewhere."""
    from ma_poc.pms.detector import _iter_html_markers
    html = """
    <html><body>
      <a href="https://leasing.realpage.com/RP.Leasing.AppService/...">apply</a>
    </body></html>
    """
    markers = list(_iter_html_markers(html.lower()))
    cws_markers = [m for m in markers if m[0] == "realpage_cws"]
    assert not cws_markers, (
        f"realpage_cws must NOT fire on leasing.realpage.com OLL pages; got {cws_markers}"
    )


def test_adapter_registered() -> None:
    a = get_adapter("realpage_cws")
    assert isinstance(a, RealPageCwsAdapter)


def test_strategy_is_dom_first() -> None:
    from ma_poc.pms.detector import _STRATEGY_BY_PMS
    assert _STRATEGY_BY_PMS["realpage_cws"] == "dom_first"
