"""Swifty WordPress native unit-roster recovery."""

from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest

from ma_poc.pms.adapters._swifty_floorplans import (
    SWIFTY_TIER,
    SwiftyFloorplan,
    extract_swifty_ajax_url,
    has_swifty_unit_ajax,
    parse_swifty_floorplans,
    parse_swifty_unit_rows,
    recover_swifty_floorplans,
)
from ma_poc.pms.adapters._universal_recovery import recover_universal_embed
from ma_poc.pms.adapters.generic import GenericAdapter

_ENTRY = """
<link href="/wp-content/plugins/swifty-frontend/x.css">
<div class="siteajaxurl" data-url="https://example.test/wp-admin/admin-ajax.php"></div>
<script>
const a = 'swifty_floorplan_section_details_with_ajax';
const b = 'swifty_load_available_units';
</script>
"""

_PLANS = """
<div class="single-floorplan" data_id="2525">
  <a data-name="A3" data-bed="1" data-bath="1" data-sqft="544"></a>
</div>
<div class="single-floorplan" data_id="2535">
  <a data-name="B3" data-bed="3" data-bath="2" data-sqft="1594"></a>
</div>
"""

_UNITS_FIVE = """
<table><tr class="single-flp-unit-row">
  <td>308</td><td>$1,200.00</td><td>3</td><td>09-09-2026</td><td>Apply Now</td>
</tr></table>
"""

_UNITS_FOUR = """
<table><tr class="single-flp-unit-row">
  <td>168 (Renovated)</td><td>$1,261.00 One Time Fees $100</td>
  <td>Available Now</td><td>Apply Now</td>
</tr></table>
"""


def test_exact_marker_and_same_origin_ajax_url() -> None:
    assert has_swifty_unit_ajax(_ENTRY) is True
    assert (
        extract_swifty_ajax_url(_ENTRY, "https://www.example.test/")
        == "https://example.test/wp-admin/admin-ajax.php"
    )
    hostile = _ENTRY.replace("https://example.test", "https://evil.test")
    assert extract_swifty_ajax_url(hostile, "https://example.test/") == ""


def test_parse_floorplan_metadata() -> None:
    plans = parse_swifty_floorplans(_PLANS)
    assert plans == [
        SwiftyFloorplan("2525", "A3", 1, "1", "544"),
        SwiftyFloorplan("2535", "B3", 3, "2", "1594"),
    ]


def test_parse_five_column_future_date_row() -> None:
    [row] = parse_swifty_unit_rows(
        _UNITS_FIVE,
        SwiftyFloorplan("2525", "A3", 1, "1", "544"),
        "https://example.test/wp-admin/admin-ajax.php",
    )
    assert row["unit_number"] == "308"
    assert row["market_rent_low"] == 1200
    assert row["floor"] == "3"
    assert row["availability_date"] == "09-09-2026"
    assert row["extraction_tier"] == SWIFTY_TIER


def test_parse_four_column_available_now_and_variant_suffix() -> None:
    [row] = parse_swifty_unit_rows(
        _UNITS_FOUR,
        SwiftyFloorplan("2569", "Murray", 1, "1", "700"),
        "https://example.test/wp-admin/admin-ajax.php",
    )
    assert row["unit_number"] == "168"
    assert row["unit_name"] == "168 (Renovated)"
    assert row["market_rent_low"] == 1261
    assert row["availability_date"] == "Available Now"


@pytest.mark.asyncio
async def test_recover_fetches_listing_then_each_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str]] = []

    def _post(url: str, data: dict[str, str], **kw: object) -> object:
        calls.append(dict(data))
        body = _PLANS if "section_details" in data["action"] else _UNITS_FIVE
        return types.SimpleNamespace(status_code=200, text=body)

    monkeypatch.setattr("ma_poc.pms.adapters._swifty_floorplans.probe_post", _post)
    ctx = types.SimpleNamespace(
        base_url="https://example.test/",
        fetch_result=types.SimpleNamespace(body=_ENTRY, final_url="https://example.test/"),
    )
    rows = await recover_swifty_floorplans(ctx)

    assert len(rows) == 1  # duplicate unit label across the two synthetic plans
    assert calls[0]["action"] == "swifty_floorplan_section_details_with_ajax"
    assert [call["flp_id"] for call in calls[1:]] == ["2525", "2535"]


@pytest.mark.asyncio
async def test_universal_recovery_prefers_native_swifty_roster_before_portal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An undated linked portal must not hide the site's dated unit table."""
    swifty_rows = [
        {
            "unit_number": "308",
            "market_rent_low": 1200,
            "availability_date": "09-09-2026",
            "extraction_tier": SWIFTY_TIER,
        }
    ]
    portal = AsyncMock(return_value=[{"unit_number": "plan-only"}])
    monkeypatch.setattr(
        "ma_poc.pms.adapters._appfolio_embed.recover_appfolio_embed",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "ma_poc.pms.adapters._leaseleads_embed.recover_leaseleads_embed",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "ma_poc.pms.adapters._swifty_floorplans.recover_swifty_floorplans",
        AsyncMock(return_value=swifty_rows),
    )
    monkeypatch.setattr(
        "ma_poc.pms.adapters._pms_portal_hop.recover_pms_portal",
        portal,
    )
    ctx = types.SimpleNamespace(
        base_url="https://example.test/",
        property_id="TEST-SWIFTY",
        fetch_result=types.SimpleNamespace(body=_ENTRY.encode(), final_url="https://example.test/"),
    )

    rows, tier, winner = await recover_universal_embed(None, ctx)

    assert rows == swifty_rows
    assert tier == SWIFTY_TIER
    assert winner == "swifty_floorplans"
    portal.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_fallback_recovers_swifty_when_page_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production's page-less generic fallback must not depend on a flag."""
    rows = parse_swifty_unit_rows(
        _UNITS_FIVE,
        SwiftyFloorplan("2525", "A3", 1, "1", "544"),
        "https://example.test/wp-admin/admin-ajax.php",
    )
    recover = AsyncMock(return_value=rows)
    monkeypatch.setattr(
        "ma_poc.pms.adapters._swifty_floorplans.recover_swifty_floorplans",
        recover,
    )
    ctx = types.SimpleNamespace(
        base_url="https://example.test/",
        property_id="TEST-SWIFTY",
        fetch_result=types.SimpleNamespace(body=_ENTRY.encode(), final_url="https://example.test/"),
    )

    result = await GenericAdapter().extract(None, ctx)

    assert result.tier_used == SWIFTY_TIER
    assert [row["unit_number"] for row in result.units] == ["308"]
    assert result.units[0]["availability_date"] == "09-09-2026"
    recover.assert_awaited_once_with(ctx)
