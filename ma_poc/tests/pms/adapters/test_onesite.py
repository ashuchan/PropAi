"""Phase 3 — OneSite (RealPage) adapter tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.onesite import (
    _ONESITE_AVAIL_DATE_ALIASES,
    OneSiteAdapter,
    parse_realpage_floorplans,
)
from ma_poc.pms.detector import detect_pms

FIXTURES = Path(__file__).parent / "fixtures" / "onesite"


def _load_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _make_ctx(api_responses: list[dict]) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://8756399.onlineleasing.realpage.com/",
        detected=detect_pms("https://8756399.onlineleasing.realpage.com/"),
        profile=None,
        expected_total_units=None,
        property_id="293707",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


class _DummyPage:
    pass


@pytest.mark.asyncio
async def test_onesite_extract_happy_path() -> None:
    responses = _load_fixture("293707.json")
    adapter = OneSiteAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) >= 1
    first = result.units[0]
    assert first["rent_range"]
    assert "ONESITE" in first["extraction_tier"]


@pytest.mark.asyncio
async def test_onesite_extract_from_stored_fixture() -> None:
    for fixture_path in FIXTURES.glob("*.json"):
        responses = json.loads(fixture_path.read_text(encoding="utf-8"))
        adapter = OneSiteAdapter()
        ctx = _make_ctx(responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        assert isinstance(result, AdapterResult)


@pytest.mark.asyncio
async def test_onesite_extract_returns_empty_on_no_data() -> None:
    responses = [
        {
            "url": "https://api.ws.realpage.com/v2/property/999/floorplans",
            "body": {"status": 200, "response": {"floorplans": []}},
        }
    ]
    adapter = OneSiteAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []


def test_parse_realpage_handles_null_units_response() -> None:
    """Null response object returns empty list."""
    body = {"status": 200, "response": None}
    assert parse_realpage_floorplans(body, "test") == []


def test_parse_realpage_beds_baths_extraction() -> None:
    body = {
        "status": 200,
        "response": {
            "floorplans": [
                {
                    "id": "1",
                    "name": "2/1",
                    "bedRooms": "2",
                    "bathRooms": "1",
                    "minimumSquareFeet": "750",
                    "maximumSquareFeet": "750",
                    "minimumMarketRent": 1895.0,
                    "maximumMarketRent": 1995.0,
                }
            ]
        },
    }
    units = parse_realpage_floorplans(body, "test")
    assert len(units) == 1
    assert units[0]["bedrooms"] == "2"
    assert units[0]["bathrooms"] == "1"
    assert "$1,895" in units[0]["rent_range"]


def test_static_fingerprints_nonempty() -> None:
    assert OneSiteAdapter().static_fingerprints()


def test_tier_used_label_is_pms_specific() -> None:
    body = {
        "status": 200,
        "response": {
            "floorplans": [
                {
                    "id": "1",
                    "name": "A",
                    "bedRooms": "1",
                    "bathRooms": "1",
                    "minimumSquareFeet": "500",
                    "minimumMarketRent": 1000.0,
                    "maximumMarketRent": 1200.0,
                }
            ]
        },
    }
    units = parse_realpage_floorplans(body, "test")
    assert "ONESITE" in units[0]["extraction_tier"]


def test_rent_within_sanity_range() -> None:
    responses = _load_fixture("293707.json")
    import re

    for resp in responses:
        body = resp.get("body")
        if isinstance(body, dict):
            units = parse_realpage_floorplans(body, "test")
            for u in units:
                if u["rent_range"]:
                    nums = re.findall(r"\d[\d,]*", u["rent_range"])
                    for n in nums:
                        val = int(n.replace(",", ""))
                        assert 200 <= val <= 50000


# ────────────────────────────────────────────────────────────────────
# 2026-05-13 port (Commit 6 of MAY13_API_TIER_PORT_PLAN.md):
# - 7-alias available_date lookup on RealPage /floorplans payloads
# - Three-label outcome (SUCCESS / _EMPTY / _NO_RESPONSE) for Phase-B
# ────────────────────────────────────────────────────────────────────


def _fp_body(extras: dict) -> dict:
    """Minimal valid RealPage /floorplans payload with one floorplan."""
    base_fp = {
        "name": "A1",
        "bedRooms": 1,
        "bathRooms": 1,
        "id": 99,
        "minimumMarketRent": 1500,
        "maximumMarketRent": 1500,
    }
    base_fp.update(extras)
    return {"response": {"floorplans": [base_fp]}}


class TestOneSiteAliasLookup:
    def test_alias_firstAvailableDate_threaded_through(self):
        out = parse_realpage_floorplans(_fp_body({"firstAvailableDate": "2026-07-01"}), "https://x/floorplans")
        assert out[0]["availability_date"] == "2026-07-01"
        # Dual emission (Commit 4) populates both keys.
        assert out[0]["available_date"] == "2026-07-01"

    def test_alias_availableDate(self):
        out = parse_realpage_floorplans(_fp_body({"availableDate": "2026-08-01"}), "https://x")
        assert out[0]["availability_date"] == "2026-08-01"

    def test_alias_minimumAvailableDate(self):
        out = parse_realpage_floorplans(_fp_body({"minimumAvailableDate": "2026-09-01"}), "https://x")
        assert out[0]["availability_date"] == "2026-09-01"

    def test_no_date_alias_yields_empty(self):
        out = parse_realpage_floorplans(_fp_body({}), "https://x")
        assert out[0]["availability_date"] == ""

    def test_all_7_aliases_present(self):
        expected = {
            "availableDate", "firstAvailableDate", "dateAvailable",
            "minimumAvailableDate", "availabilityDate", "available_date",
            "minAvailableDate",
        }
        assert set(_ONESITE_AVAIL_DATE_ALIASES) == expected


@pytest.mark.asyncio
class TestOneSiteThreeLabelOutcomes:
    """Branch-port: split TIER_1_API_ONESITE into three distinct labels
    so Phase-B retry can fire on the "empty" / "no response" paths.

    Pre-port all three terminal states shipped TIER_1_API_ONESITE which
    falsely indicated success and blocked the cascade fallback."""

    async def test_success_keeps_bare_tier_label(self):
        responses = _load_fixture("293707.json")
        ctx = _make_ctx(responses)
        result = await OneSiteAdapter().extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        assert result.tier_used == "TIER_1_API_ONESITE"
        assert len(result.units) > 0

    async def test_no_response_path_emits_no_response_label(self):
        # Empty response list -> No RealPage-shaped data ever seen.
        ctx = _make_ctx([])
        result = await OneSiteAdapter().extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        assert result.tier_used == "TIER_1_API_ONESITE_NO_RESPONSE"
        assert len(result.units) == 0
        # Errors should reflect the no-response state.
        assert any("No RealPage" in e for e in result.errors)

    async def test_empty_path_emits_empty_label_when_validity_rejects_all(self):
        # Floorplans response present but ALL rows fail validity gate
        # (no beds/baths/sqft -- forcing rejection at the dim-less gate).
        bad_body = {"response": {"floorplans": [{
            "name": "Junk",
            "id": "1",
            # No bedRooms / bathRooms / squareFeet -> validity gate rejects.
        }]}}
        ctx = _make_ctx([{"url": "https://api.ws.realpage.com/v2/property/1/floorplans",
                          "body": bad_body}])
        result = await OneSiteAdapter().extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        assert result.tier_used == "TIER_1_API_ONESITE_EMPTY"
        assert len(result.units) == 0
        assert any("VALIDITY_REJECTED" in e for e in result.errors)
