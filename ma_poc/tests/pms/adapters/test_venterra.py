"""Venterra in-house (vt_units island) adapter tests (2026-07-19, gap #4).

Pins the static ``var vt_units = [...]`` island parse that recovers Venterra
props the roster-confirmation sweep mis-routed to SightMap + needs_render.
Fixtures wrap the REAL island captured 2026-07-19 (forest-view 20 / canton-mill 19).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.venterra import (
    VenterraAdapter,
    _extract_js_array,
    parse_venterra_units,
)
from ma_poc.pms.detector import _STRATEGY_BY_PMS, detect_pms

FIX = Path(__file__).parent / "fixtures" / "venterra"


def _body(name: str) -> str:
    return (FIX / f"{name}.html").read_text(encoding="utf-8")


# --- parse_venterra_units (real fixtures) ----------------------------------


def test_parse_forest_view_20_units() -> None:
    rows = parse_venterra_units(_body("forest_view"), "u")
    assert len(rows) == 20
    assert all(r["unit_number"] for r in rows)
    assert all(r["market_rent_low"] for r in rows)
    assert all(r["sqft"] for r in rows)
    first = next(r for r in rows if r["unit_number"] == "0116")
    assert first["market_rent_low"] == 1159
    assert first["market_rent_high"] == 1222  # min..max lease-term range
    assert first["sqft"] == "684"
    assert first["floor_plan_name"] == "660-A"
    assert first["availability_status"] == "AVAILABLE"
    assert first["availability_date"] == "2026-09-29"
    assert first["concession_text"] == "$500 gift card. Limited time only"
    assert first["source_ids"]["venterra_unit_code"] == "TX4FV-01-0116"
    assert first["extraction_tier"] == "TIER_1_DOM_VENTERRA"


def test_parse_canton_mill_19_units() -> None:
    rows = parse_venterra_units(_body("canton_mill"), "u")
    assert len(rows) == 19
    assert all(r["concession_text"] for r in rows)


def test_parse_no_island_returns_empty() -> None:
    assert parse_venterra_units("<html>no island here</html>", "u") == []


def test_parse_empty_body_returns_empty() -> None:
    assert parse_venterra_units("", "u") == []


def test_parse_skips_units_without_name() -> None:
    body = '<script>var vt_units = [{"unit_rent_min":"1000","unit_sqft":"500"},{"unit_name":"12A","unit_rent_min":"1200","unit_sqft":"600"}];</script>'
    rows = parse_venterra_units(body, "u")
    assert len(rows) == 1
    assert rows[0]["unit_number"] == "12A"


# --- _extract_js_array ------------------------------------------------------


def test_extract_js_array_nested_array_in_value() -> None:
    text = 'x; vt_units = [{"a":[1,2],"b":"c"}]; y'
    assert _extract_js_array(text, "vt_units") == '[{"a":[1,2],"b":"c"}]'


def test_extract_js_array_bracket_inside_string() -> None:
    text = 'vt_units = [{"msg":"save ] now"}];'
    assert _extract_js_array(text, "vt_units") == '[{"msg":"save ] now"}]'


def test_extract_js_array_absent_returns_empty() -> None:
    assert _extract_js_array("no var here", "vt_units") == ""


def test_extract_js_array_unbalanced_returns_empty() -> None:
    assert _extract_js_array("vt_units = [{unclosed", "vt_units") == ""


# --- detector routing (flag-gated) -----------------------------------------


def test_detector_routes_venterra_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_VENTERRA_ADAPTER", "true")
    r = detect_pms(
        "https://venterraliving.com/apartments/forest-view/",
        page_html=_body("forest_view"),
    )
    assert r.pms == "venterra"
    assert r.confidence >= 0.90


def test_detector_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_VENTERRA_ADAPTER", "false")
    r = detect_pms(
        "https://venterraliving.com/apartments/forest-view/",
        page_html=_body("forest_view"),
    )
    assert r.pms != "venterra"


def test_detector_eonlinelease_marker_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_VENTERRA_ADAPTER", "true")
    html = (
        '<html><body><a href="https://online.venterraliving.com/eOnlineLease/'
        'portal/createApplication/TX4FV">Apply</a></body></html>'
    )
    r = detect_pms("https://custom-vanity.com/", page_html=html)
    assert r.pms == "venterra"


def test_strategy_is_dom_first() -> None:
    assert _STRATEGY_BY_PMS["venterra"] == "dom_first"


# --- adapter integration ----------------------------------------------------


class _DummyPage:
    url = "https://venterraliving.com/apartments/forest-view/"


class _FR:
    def __init__(self, body: str) -> None:
        self.body = body


def _ctx(body: str) -> AdapterContext:
    base = "https://venterraliving.com/apartments/forest-view/"
    return AdapterContext(
        base_url=base,
        detected=detect_pms(base),
        profile=None,
        expected_total_units=None,
        property_id="P_V",
        fetch_result=_FR(body),
    )


@pytest.mark.asyncio
async def test_adapter_extracts_from_fetch_body() -> None:
    result = await VenterraAdapter().extract(_DummyPage(), _ctx(_body("forest_view")))  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert result.tier_used == "TIER_1_DOM_VENTERRA"
    assert len(result.units) == 20
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
    result = await VenterraAdapter().extract(_NoContentPage(), ctx)  # type: ignore[arg-type]
    assert result.confidence == 0.0


def test_adapter_registered() -> None:
    assert type(get_adapter("venterra")).__name__ == "VenterraAdapter"
