"""Phase 3 — OneSite (RealPage) adapter tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.onesite import OneSiteAdapter, parse_realpage_floorplans
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


# ─────────────────────────────────────────────────────────────────────
# 2026-05-20 feature_fail_1429 cluster #6 — OneSite empty-exit labels.
#
# 30 props tagged TIER_1_API_ONESITE with 0 strict units in the canary
# feature run. Live-probe of top 3 (toapts.com, riverpointeliving,
# acadiabysdk) on the OneSite numeric-subdomain hosts (e.g.
# 9141461.onlineleasing.realpage.com) showed page bodyLen ≈ 386 — the
# page is an empty OLL-widget shell. The RealPage floorplans/units API
# was never hit on the canary, so the OneSite adapter saw zero
# RealPage-shaped responses and emitted the bare success label —
# which blocked both Path B/C retry AND Step 8 generic fallback.
#
# Fix: emit ``_NO_RESPONSE`` when nothing RealPage-shaped came back,
# and ``_EMPTY`` when responses came back but everything failed the
# validity gate. Both labels are in the empty-exit registry so the
# retry mechanism + Step 8 fallback can now do their job.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_onesite_emits_no_response_when_no_realpage_responses() -> None:
    """No RealPage-shaped response captured (OLL widget shell pattern) —
    emit ``TIER_1_API_ONESITE_NO_RESPONSE`` so Path B/C and Step 8
    fallback can engage."""
    # All non-RealPage responses (analytics, static assets, etc.)
    responses = [
        {
            "url": "https://www.google-analytics.com/g/collect?v=2",
            "body": {"event": "page_view"},
        },
        {
            "url": "https://cs-cdn.realpage.com/CWS/9999/CMSScripts/main.js",
            "body": "console.log('ok');",
        },
    ]
    adapter = OneSiteAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.tier_used == "TIER_1_API_ONESITE_NO_RESPONSE", (
        f"expected NO_RESPONSE label so retry/fallback fires; "
        f"got {result.tier_used!r}"
    )


@pytest.mark.asyncio
async def test_onesite_emits_empty_when_floorplans_list_is_empty() -> None:
    """RealPage response shape matched but the floorplans list was
    empty — emit ``TIER_1_API_ONESITE_EMPTY``."""
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
    assert result.tier_used == "TIER_1_API_ONESITE_EMPTY", (
        f"expected EMPTY label (responses captured but no rows); "
        f"got {result.tier_used!r}"
    )


@pytest.mark.asyncio
async def test_onesite_real_data_keeps_bare_success_label() -> None:
    """Regression guard — real /floorplans data (the 293707 fixture
    shape) still produces the bare ``TIER_1_API_ONESITE`` label so
    Path B/C does NOT retry on a successful extraction."""
    responses = _load_fixture("293707.json")
    adapter = OneSiteAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) >= 1
    assert result.tier_used == "TIER_1_API_ONESITE", (
        f"real data must keep the bare success label; got {result.tier_used!r}"
    )


@pytest.mark.asyncio
async def test_onesite_empty_label_in_empty_exit_registry() -> None:
    """The two new empty-exit labels must both be recognized by
    ``is_empty_exit`` so the Path B/C retry hook in scraper.py
    actually fires on them."""
    from ma_poc.pms.empty_exit import is_empty_exit
    assert is_empty_exit("TIER_1_API_ONESITE_NO_RESPONSE") is True
    assert is_empty_exit("TIER_1_API_ONESITE_EMPTY") is True
    # Bare success label must NOT trigger retry.
    assert is_empty_exit("TIER_1_API_ONESITE") is False
