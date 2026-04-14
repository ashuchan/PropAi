"""Tests for the LLM extractor service — claude-scrapper-arch.md Step 6.3."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from services.llm_extractor import (
    _normalize_units,
    _parse_llm_response,
    _rank_api_responses,
    extract_with_llm,
    prepare_llm_input,
)


# ── prepare_llm_input tests ─────────────────────────────────────────────────


def test_html_trimming_removes_scripts_styles() -> None:
    html = """<html><head><style>body{color:red}</style></head>
    <body><script>alert(1)</script><main><div class="units">Unit 101</div></main>
    <footer>Footer</footer></body></html>"""
    result = prepare_llm_input(html, [], {"property_name": "Test"})
    content = result["trimmed_content"]
    assert "alert(1)" not in content
    assert "color:red" not in content
    assert "Unit 101" in content


def test_html_trimming_preserves_content() -> None:
    html = """<html><body><main><div class="pricing">$1,450/mo</div>
    <div class="unit">Unit 202 - 2BR/2BA</div></main></body></html>"""
    result = prepare_llm_input(html, [], {})
    content = result["trimmed_content"]
    assert "$1,450" in content
    assert "Unit 202" in content


def test_api_response_ranking() -> None:
    apis = [
        {"url": "/api/analytics", "body": {"page_views": 100}},
        {"url": "/api/units", "body": {"units": [{"rent": 1200, "sqft": 800}]}},
        {"url": "/api/chat", "body": {"message": "hello"}},
        {"url": "/api/floorplans", "body": {"floorPlan": "1BR", "price": 1500}},
        {"url": "/api/weather", "body": {"temp": 72}},
    ]
    ranked = _rank_api_responses(apis)
    assert len(ranked) <= 3
    urls = [r["url"] for r in ranked]
    assert "/api/units" in urls
    assert "/api/floorplans" in urls
    assert "/api/analytics" not in urls


# ── _parse_llm_response tests ───────────────────────────────────────────────


def test_parse_valid_json() -> None:
    text = json.dumps({"units": [{"unit_id": "101"}], "profile_hints": {}})
    result = _parse_llm_response(text)
    assert len(result["units"]) == 1


def test_parse_markdown_fenced_json() -> None:
    text = '```json\n{"units": [{"unit_id": "102"}], "profile_hints": {}}\n```'
    result = _parse_llm_response(text)
    assert result["units"][0]["unit_id"] == "102"


def test_parse_invalid_json_returns_empty() -> None:
    result = _parse_llm_response("This is not JSON at all")
    assert result["units"] == []


# ── _normalize_units tests ───────────────────────────────────────────────────


def test_normalize_units_valid() -> None:
    raw = [
        {
            "unit_id": "101",
            "floor_plan_name": "1BR/1BA",
            "bedrooms": 1,
            "bathrooms": 1,
            "sqft": 750,
            "market_rent_low": 1450,
            "market_rent_high": 1600,
            "available_date": "2026-05-01",
            "availability_status": "AVAILABLE",
            "confidence": 0.95,
        }
    ]
    units = _normalize_units(raw)
    assert len(units) == 1
    assert units[0]["unit_id"] == "101"
    assert units[0]["market_rent_low"] == 1450.0
    assert units[0]["availability_status"] == "AVAILABLE"


def test_normalize_units_rent_sanity_bounds() -> None:
    raw = [{"unit_id": "bad", "market_rent_low": 50, "market_rent_high": 100_000}]
    units = _normalize_units(raw)
    assert len(units) == 1
    assert units[0]["market_rent_low"] is None
    assert units[0]["market_rent_high"] is None


def test_normalize_units_filters_empty() -> None:
    raw = [{"bedrooms": 2}]  # No unit_id, no floor_plan, no rent
    units = _normalize_units(raw)
    assert len(units) == 0


# ── extract_with_llm integration test ────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_extraction_returns_units_and_hints() -> None:
    llm_response = json.dumps({
        "units": [
            {
                "unit_id": "201",
                "floor_plan_name": "Studio",
                "market_rent_low": 1200,
                "availability_status": "AVAILABLE",
                "confidence": 0.9,
            }
        ],
        "profile_hints": {
            "platform_guess": "entrata",
            "css_selectors": {"container": ".unit-card"},
        },
    })

    mock_provider = AsyncMock()
    mock_provider.complete = AsyncMock(return_value=llm_response)

    with patch("llm.factory.get_text_provider", return_value=mock_provider):
        llm_input = prepare_llm_input("<html><body>Units here</body></html>", [], {})
        units, hints, raw = await extract_with_llm(llm_input)

    assert len(units) == 1
    assert units[0]["unit_id"] == "201"
    assert hints.get("platform_guess") == "entrata"


@pytest.mark.asyncio
async def test_llm_returns_invalid_json_handled_gracefully() -> None:
    mock_provider = AsyncMock()
    mock_provider.complete = AsyncMock(return_value="Not valid JSON at all")

    with patch("llm.factory.get_text_provider", return_value=mock_provider):
        llm_input = prepare_llm_input("<html><body>Test</body></html>", [], {})
        units, hints, raw = await extract_with_llm(llm_input)

    assert units == []


@pytest.mark.asyncio
async def test_llm_returns_empty_units_array() -> None:
    llm_response = json.dumps({"units": [], "profile_hints": {"field_mapping_notes": "No data found"}})
    mock_provider = AsyncMock()
    mock_provider.complete = AsyncMock(return_value=llm_response)

    with patch("llm.factory.get_text_provider", return_value=mock_provider):
        llm_input = prepare_llm_input("<html><body>Empty</body></html>", [], {})
        units, hints, raw = await extract_with_llm(llm_input)

    assert units == []
    assert hints.get("field_mapping_notes") == "No data found"
