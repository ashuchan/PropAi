"""Tests for extraction/tier2_jsonld.py — 5+ tests."""
from __future__ import annotations

from extraction import tier2_jsonld
from models.extraction_result import ExtractionStatus
from scraper.browser import BrowserSession


def _session(html: str) -> BrowserSession:
    s = BrowserSession(property_id="P1", url="https://example.com/")
    s.html = html
    return s


async def test_valid_apartment_schema_extracts_fields(jsonld_html: str) -> None:
    result = await tier2_jsonld.extract(_session(jsonld_html))
    units = result.raw_fields.get("units", [])
    assert len(units) == 2
    by_name = {u["unit_number"]: u for u in units}
    assert by_name["Unit 12A"]["asking_rent"] == 3450
    assert by_name["Unit 12A"]["sqft"] == 720


async def test_apartmentcomplex_with_offers_yields_unit_records(jsonld_html: str) -> None:
    result = await tier2_jsonld.extract(_session(jsonld_html))
    assert any(u["availability_status"] == "AVAILABLE" for u in result.raw_fields["units"])
    assert any(u["availability_status"] == "UNAVAILABLE" for u in result.raw_fields["units"])


async def test_missing_sqft_degrades_confidence() -> None:
    html = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Apartment","name":"X1",
     "offers":{"@type":"Offer","price":2000,"availability":"https://schema.org/InStock"}}
    </script></head><body></body></html>"""
    result = await tier2_jsonld.extract(_session(html))
    assert result.confidence_score < 1.0


async def test_invalid_schema_returns_failed() -> None:
    html = "<html><body>no schema here</body></html>"
    result = await tier2_jsonld.extract(_session(html))
    assert result.status == ExtractionStatus.FAILED


async def test_multiple_apartment_objects_yield_multiple_units(jsonld_html: str) -> None:
    result = await tier2_jsonld.extract(_session(jsonld_html))
    assert len(result.raw_fields["units"]) >= 2
