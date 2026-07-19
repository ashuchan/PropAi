"""Camden REIT (__NEXT_DATA__ suggestedFloorPlans) adapter tests (2026-07-19, gap #14).

Pins the static Next.js island parse that recovers the whole camdenliving.com
portfolio. Fixtures wrap the REAL suggestedFloorPlans array captured 2026-07-19
(fallsgrove 6 / buckhead 9).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.camden import CamdenAdapter, parse_camden_units
from ma_poc.pms.detector import _STRATEGY_BY_PMS, detect_pms

FIX = Path(__file__).parent / "fixtures" / "camden"


def _body(name: str) -> str:
    return (FIX / f"{name}.html").read_text(encoding="utf-8")


# --- parse_camden_units (real fixtures) ------------------------------------


def test_parse_fallsgrove_6_units() -> None:
    rows = parse_camden_units(_body("fallsgrove"), "u")
    assert len(rows) == 6
    assert all(r["unit_number"] for r in rows)
    assert all(r["market_rent_low"] for r in rows)
    assert all(r["sqft"] for r in rows)
    first = next(r for r in rows if r["unit_number"] == "9040")
    assert first["market_rent_low"] == 2199
    assert first["sqft"] == "799"
    assert first["floor_plan_name"] == "1.1D"
    assert first["availability_status"] == "AVAILABLE"
    assert first["availability_date"] == "2026-09-25"
    assert first["source_ids"]["realpage_unit_id"] == 248
    assert first["extraction_tier"] == "TIER_1_DOM_CAMDEN"


def test_parse_buckhead_9_units() -> None:
    rows = parse_camden_units(_body("buckhead"), "u")
    assert len(rows) == 9


def test_parse_no_next_data_returns_empty() -> None:
    assert parse_camden_units("<html>no next data</html>", "u") == []


def test_parse_no_suggested_floorplans_returns_empty() -> None:
    body = '<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{}}}</script>'
    assert parse_camden_units(body, "u") == []


def test_parse_non_json_next_data_returns_empty() -> None:
    body = '<script id="__NEXT_DATA__" type="application/json">not json</script>'
    assert parse_camden_units(body, "u") == []


def test_parse_skips_units_without_number() -> None:
    body = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"suggestedFloorPlans":['
        '{"monthlyRent":1000,"squareFeet":500,"available":true},'
        '{"unitNumber":"12A","monthlyRent":1200,"squareFeet":600,"available":true}'
        ']}}}</script>'
    )
    rows = parse_camden_units(body, "u")
    assert len(rows) == 1
    assert rows[0]["unit_number"] == "12A"


# --- detector routing (flag-gated host) ------------------------------------


def test_detector_routes_camden_host_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_CAMDEN_ADAPTER", "true")
    r = detect_pms("https://www.camdenliving.com/apartments/rockville-md/camden-fallsgrove")
    assert r.pms == "camden"
    assert r.confidence >= 0.90


def test_detector_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_CAMDEN_ADAPTER", "false")
    r = detect_pms("https://www.camdenliving.com/apartments/rockville-md/camden-fallsgrove")
    assert r.pms != "camden"


def test_strategy_is_dom_first() -> None:
    assert _STRATEGY_BY_PMS["camden"] == "dom_first"


# --- adapter integration ----------------------------------------------------


class _Page:
    url = "https://www.camdenliving.com/apartments/rockville-md/camden-fallsgrove"


class _FR:
    def __init__(self, body: str) -> None:
        self.body = body


def _ctx(body: str) -> AdapterContext:
    base = "https://www.camdenliving.com/apartments/rockville-md/camden-fallsgrove"
    return AdapterContext(
        base_url=base,
        detected=detect_pms(base),
        profile=None,
        expected_total_units=None,
        property_id="P_C",
        fetch_result=_FR(body),
    )


@pytest.mark.asyncio
async def test_adapter_extracts_from_fetch_body() -> None:
    result = await CamdenAdapter().extract(_Page(), _ctx(_body("fallsgrove")))  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert result.tier_used == "TIER_1_DOM_CAMDEN"
    assert len(result.units) == 6
    assert result.confidence >= 0.7


@pytest.mark.asyncio
async def test_adapter_no_body_zero_confidence() -> None:
    class _NoContentPage:
        url = "https://x/"

    ctx = AdapterContext(
        base_url="https://x/",
        detected=detect_pms("https://x/"),
        profile=None,
        expected_total_units=None,
        property_id="P",
        fetch_result=_FR(""),
    )
    result = await CamdenAdapter().extract(_NoContentPage(), ctx)  # type: ignore[arg-type]
    assert result.confidence == 0.0


def test_adapter_registered() -> None:
    assert type(get_adapter("camden")).__name__ == "CamdenAdapter"
