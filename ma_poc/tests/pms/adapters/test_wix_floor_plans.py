"""Wix floor-plans adapter (2026-05-21, HAR-validation greenfield).

Plan-card text captured live from
www.liveatarcos.com/phoenix-apartment-floor-plans — 3 Wix component
cards, each with the same templated text pattern.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.wix_floor_plans import (
    WixFloorPlansAdapter,
    _parse_card_text,
    _split_plan_title_and_code,
    parse_wix_floor_plans,
)
from ma_poc.pms.detector import detect_pms


# Live-captured card texts from liveatarcos.com (2026-05-21):
_STUDIO_CARD_TEXT = (
    "Studio Bedroom S1 Studio | 1 Bath | 424 Sq. Ft. $500 Starting at $999 Request More Info"
)
_ONE_BR_A1_TEXT = (
    "One Bedroom A1 1 Bed | 1 Bath | 534 Sq. Ft. $500 Starting at $1,199 Request More Info"
)
_ONE_BR_A2_TEXT = (
    "One Bedroom A2 1 Bed | 1 Bath | 560 Sq. Ft. $500 Starting at $1,199 Request More Info"
)


# ── _parse_card_text + _split_plan_title_and_code ──────────────────


def test_parse_studio_card() -> None:
    p = _parse_card_text(_STUDIO_CARD_TEXT)
    assert p is not None
    assert p["title"] == "Studio Bedroom"
    assert p["code"] == "S1"
    assert p["beds"] == 0  # studio → 0
    assert p["baths"] == "1"
    assert p["sqft"] == "424"
    assert p["rent"] == 999
    assert p["deposit"] == "500"


def test_parse_one_bedroom_card() -> None:
    p = _parse_card_text(_ONE_BR_A1_TEXT)
    assert p is not None
    assert p["title"] == "One Bedroom"
    assert p["code"] == "A1"
    assert p["beds"] == 1
    assert p["sqft"] == "534"
    assert p["rent"] == 1199


def test_parse_returns_none_on_unrelated_text() -> None:
    """A card without the specs+rent pattern should not match."""
    assert _parse_card_text("Welcome to our community! Contact us today.") is None
    assert _parse_card_text("Studio | 1 Bath | 424 Sq. Ft. with no rent") is None  # no "Starting at"


def test_split_plan_title_and_code_short_code() -> None:
    title, code = _split_plan_title_and_code("Studio Bedroom S1")
    assert title == "Studio Bedroom"
    assert code == "S1"


def test_split_plan_title_no_code() -> None:
    title, code = _split_plan_title_and_code("Studio Bedroom")
    # "Bedroom" is too long to be a plan code → all-title
    assert title == "Studio Bedroom"
    assert code == ""


# ── parse_wix_floor_plans ──


def test_parse_all_three_real_liveatarcos_cards() -> None:
    cards = [
        {"tag": "DIV", "text": _STUDIO_CARD_TEXT},
        {"tag": "DIV", "text": _ONE_BR_A1_TEXT},
        {"tag": "DIV", "text": _ONE_BR_A2_TEXT},
    ]
    rows = parse_wix_floor_plans(cards, "https://www.liveatarcos.com/")
    assert len(rows) == 3
    codes = sorted(r["floor_plan_name"] for r in rows)
    assert codes == ["A1", "A2", "S1"]
    studio = next(r for r in rows if r["floor_plan_name"] == "S1")
    assert studio["bedrooms"] == "0"
    assert studio["market_rent_low"] == 999
    assert studio["extraction_tier"] == "TIER_1_DOM_WIX_FLOOR_PLANS"


def test_parse_dedupes_duplicate_card_renders() -> None:
    """Wix sometimes renders the same plan card twice in different
    containers (mobile/desktop variants); dedupe by (title, code, rent)."""
    cards = [
        {"tag": "DIV", "text": _ONE_BR_A1_TEXT},
        {"tag": "DIV", "text": _ONE_BR_A1_TEXT},  # exact duplicate
    ]
    rows = parse_wix_floor_plans(cards, "u")
    assert len(rows) == 1


def test_parse_skips_cards_without_starting_at() -> None:
    """Cards missing the 'Starting at $X' marker (e.g. "Contact for pricing")
    are skipped; downstream WixNoPmsAdapter handles plan-less Wix sites."""
    cards = [
        {"tag": "DIV", "text": "One Bedroom A1 1 Bed | 1 Bath | 534 Sq. Ft. Contact for pricing"},
    ]
    rows = parse_wix_floor_plans(cards, "u")
    assert rows == []


# ── adapter end-to-end ──


class _FakePage:
    def __init__(self, payload, url="https://www.liveatarcos.com/phoenix-apartment-floor-plans"):
        self._payload = payload
        self.url = url

    async def evaluate(self, _js):
        return self._payload


@pytest.mark.asyncio
async def test_adapter_extracts_three_plans() -> None:
    payload = {
        "ok": True,
        "cards": [
            {"tag": "DIV", "text": _STUDIO_CARD_TEXT},
            {"tag": "DIV", "text": _ONE_BR_A1_TEXT},
            {"tag": "DIV", "text": _ONE_BR_A2_TEXT},
        ],
    }
    ctx = AdapterContext(
        base_url="https://www.liveatarcos.com/",
        detected=detect_pms("https://www.liveatarcos.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    result = await WixFloorPlansAdapter().extract(_FakePage(payload), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_WIX_FLOOR_PLANS"
    assert len(result.units) == 3
    assert result.confidence > 0.6


@pytest.mark.asyncio
async def test_adapter_bails_when_no_cards() -> None:
    payload = {"ok": False}
    ctx = AdapterContext(
        base_url="https://x.test/",
        detected=detect_pms("https://x.test/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    result = await WixFloorPlansAdapter().extract(_FakePage(payload), ctx)  # type: ignore[arg-type]
    assert result.confidence == 0.0


# ── detector tests ──


def test_detector_routes_wix_with_starting_at_to_wix_floor_plans() -> None:
    """Wix host marker + 'Starting at $X' text → wix_floor_plans (not
    the plan-less wix_nopms fallback)."""
    from ma_poc.pms.detector import _iter_html_markers
    html = """
    <html><body>
      <script src="https://static.parastorage.com/services/santa-resolver.bundle.min.js"></script>
      <div>Studio Bedroom S1 Studio | 1 Bath | 424 Sq. Ft. $500 Starting at $999</div>
    </body></html>
    """
    markers = list(_iter_html_markers(html.lower()))
    assert any(m[0] == "wix_floor_plans" for m in markers), markers
    # And wix_nopms must NOT fire when wix_floor_plans does.
    assert not [m for m in markers if m[0] == "wix_nopms"]


def test_detector_routes_wix_without_starting_at_to_wix_nopms() -> None:
    """A Wix site that just embeds Wix runtime but has no plan-card
    text pattern should keep routing to the existing wix_nopms
    fallback (no regression)."""
    from ma_poc.pms.detector import _iter_html_markers
    html = """
    <html><body>
      <script src="https://static.parastorage.com/services/santa-resolver.bundle.min.js"></script>
      <p>Welcome to our community. Contact us for pricing.</p>
    </body></html>
    """
    markers = list(_iter_html_markers(html.lower()))
    assert any(m[0] == "wix_nopms" for m in markers)
    assert not [m for m in markers if m[0] == "wix_floor_plans"]


def test_adapter_registered() -> None:
    a = get_adapter("wix_floor_plans")
    assert isinstance(a, WixFloorPlansAdapter)


def test_strategy_is_dom_first() -> None:
    from ma_poc.pms.detector import _STRATEGY_BY_PMS
    assert _STRATEGY_BY_PMS["wix_floor_plans"] == "dom_first"
