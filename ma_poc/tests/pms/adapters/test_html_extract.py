"""Tests for HTML-based JSON-LD + embedded-JSON extractors (step 4)."""
from __future__ import annotations

import json

import pytest

from ma_poc.pms.adapters._html_extract import (
    extract_embedded_blobs_from_html,
    extract_jsonld_from_html,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.generic import GenericAdapter
from ma_poc.pms.detector import detect_pms


# ── JSON-LD ──────────────────────────────────────────────────────────────────

def test_jsonld_apartment_with_offers() -> None:
    html = """<html><head>
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "Apartment",
      "name": "The Parker 1x1",
      "numberOfRooms": 1,
      "floorSize": {"@type": "QuantitativeValue", "value": 650},
      "offers": {"@type": "Offer", "lowPrice": 1800, "highPrice": 2000}
    }
    </script></head><body></body></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/")
    assert len(units) == 1
    u = units[0]
    assert u["floor_plan_name"] == "The Parker 1x1"
    assert u["rent_range"] == "$1,800 - $2,000"
    assert u["sqft"] == "650"
    assert u["extraction_tier"] == "TIER_2_JSONLD"


def test_jsonld_skips_property_shell_with_no_offers() -> None:
    """ApartmentComplex with only name+address is not a unit — must be skipped."""
    html = """<html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"ApartmentComplex",
     "name":"Some Property","telephone":"555-1234"}
    </script></head></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/")
    assert units == []


def test_jsonld_offers_as_array_picks_min_max() -> None:
    html = """<html><script type="application/ld+json">
    {"@type":"Apartment","name":"A1","numberOfRooms":1,
     "offers":[{"@type":"Offer","price":1500},
               {"@type":"Offer","price":1600},
               {"@type":"Offer","price":1700}]}
    </script></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/")
    assert len(units) == 1
    assert units[0]["rent_range"] == "$1,500 - $1,700"


def test_jsonld_malformed_block_silently_skipped() -> None:
    html = """<html>
    <script type="application/ld+json">not valid json {{</script>
    <script type="application/ld+json">
    {"@type":"Apartment","name":"B1","numberOfRooms":2,
     "offers":{"price":2100}}
    </script></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/")
    # Bad block skipped, good block emitted.
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "B1"


def test_jsonld_empty_html_returns_empty() -> None:
    assert extract_jsonld_from_html("", "https://example.com/") == []
    assert extract_jsonld_from_html("<html></html>", "https://example.com/") == []


# ── Embedded JSON / SSR globals ──────────────────────────────────────────────

def test_embedded_next_data_block() -> None:
    # Pad payload over the 200-char length threshold — production pages are
    # always many KB; the threshold filters noise-scale inline configs.
    payload = {"props": {"pageProps": {
        "floorPlans": [
            {"id": i, "name": f"Plan{i}", "beds": 1 + (i % 3),
             "minRent": 1500 + 50 * i, "maxRent": 1600 + 50 * i,
             "sqft": 650 + 50 * i, "building": "Main", "floor": i // 4}
            for i in range(6)
        ]
    }}}
    html = f"""<html><body>
    <script id="__NEXT_DATA__" type="application/json">
    {json.dumps(payload)}
    </script></body></html>"""
    blobs = extract_embedded_blobs_from_html(html)
    assert len(blobs) >= 1
    assert any("__NEXT_DATA__" in b["url"] or "json-block" in b["url"] for b in blobs)


def test_embedded_script_var_assignment() -> None:
    plans = [
        {"id": i, "name": f"A{i}", "bedrooms": 1 + (i % 3),
         "rent": 1500 + 100 * i, "sqft": 650 + 50 * i,
         "building": "Main", "availableDate": "2026-05-01"}
        for i in range(8)
    ]
    html = f"""<html><body>
    <script>
    var floorPlans = {json.dumps(plans)};
    console.log('ok');
    </script></body></html>"""
    blobs = extract_embedded_blobs_from_html(html)
    assert len(blobs) >= 1
    assert any("floorPlans" in b["url"] for b in blobs)


def test_embedded_gates_unit_keyword_presence() -> None:
    """Random inline script without unit keywords must not be picked up."""
    html = """<html><body>
    <script>var trackingConfig = {"gtm_id": "GTM-ABC", "user_id": 42};</script>
    </body></html>"""
    blobs = extract_embedded_blobs_from_html(html)
    assert blobs == []


def test_embedded_window_nextdata_inline() -> None:
    payload = {"buildId": "x", "props": {"pageProps": {"floorplans": [
        {"id": i, "name": f"B{i}", "beds": 1 + (i % 2),
         "rent": 1400 + 100 * i, "sqft": 700 + 40 * i}
        for i in range(5)
    ]}}}
    html = f"""<html><body>
    <script>window.__NEXT_DATA__ = {json.dumps(payload)};</script>
    </body></html>"""
    blobs = extract_embedded_blobs_from_html(html)
    assert len(blobs) >= 1


# ── Generic adapter end-to-end (fetch_result.body only, no page) ─────────────

class _FetchResult:
    """Minimal stand-in for the Jugnu FetchResult (only .body is needed here)."""
    def __init__(self, body: bytes) -> None:
        self.body = body


@pytest.mark.asyncio
async def test_generic_adapter_recovers_units_from_jsonld_without_page() -> None:
    """No page, no API responses — just fetch_result.body with JSON-LD.

    This simulates the Jugnu L1 fetch-only path (page=None) where the only
    thing available is the raw HTTP response body.
    """
    html = """<html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Apartment","name":"Unit 101",
     "numberOfRooms":1,"floorSize":{"value":700},
     "offers":{"price":1750}}
    </script></head></html>"""
    fr = _FetchResult(html.encode("utf-8"))
    ctx = AdapterContext(
        base_url="https://example.com/",
        detected=detect_pms("https://example.com/"),
        profile=None,
        expected_total_units=None,
        property_id="test",
        fetch_result=fr,
    )
    ctx._api_responses = []  # type: ignore[attr-defined]

    result = await GenericAdapter().extract(None, ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) == 1
    assert result.units[0]["floor_plan_name"] == "Unit 101"
    assert result.tier_used == "TIER_2_JSONLD"
    assert result.confidence > 0.5


@pytest.mark.asyncio
async def test_generic_adapter_recovers_units_from_embedded_json() -> None:
    """Raw HTML with inline floorPlans assignment — no API, no JSON-LD."""
    plans = [
        {"id": f"A{i}", "name": f"A{i}", "bedrooms": 1 + (i % 2),
         "rent": 1500 + 50 * i, "sqft": 650 + 25 * i,
         "availableDate": "2026-05-01", "building": "Main"}
        for i in range(6)
    ]
    html = f"""<html><body>
    <script>
    var floorPlans = {json.dumps(plans)};
    </script></body></html>"""
    fr = _FetchResult(html.encode("utf-8"))
    ctx = AdapterContext(
        base_url="https://example.com/",
        detected=detect_pms("https://example.com/"),
        profile=None,
        expected_total_units=None,
        property_id="test",
        fetch_result=fr,
    )
    ctx._api_responses = []  # type: ignore[attr-defined]

    result = await GenericAdapter().extract(None, ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) >= 1, f"Expected units from embedded JSON; errors={result.errors}"
    assert result.tier_used == "TIER_1_5_EMBEDDED"
