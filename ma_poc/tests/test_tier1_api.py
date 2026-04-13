"""Tests for extraction/tier1_api.py — 6+ tests."""
from __future__ import annotations

import json
from pathlib import Path

from extraction import tier1_api
from extraction.tier1_api import matches_catalogue
from models.extraction_result import ExtractionStatus
from scraper.browser import BrowserSession, InterceptedResponse

CATALOGUE = {
    "patterns": [
        {"id": "generic-api", "url_regex": "/api/.*(availability|floorplans|units|apartments|pricing)"},
    ],
    "discovered": {},
}


def _session_with(responses: list[InterceptedResponse]) -> BrowserSession:
    s = BrowserSession(property_id="P1", url="https://x.example.com/")
    s.intercepted_api_responses.extend(responses)
    return s


async def test_matched_url_extracts_units() -> None:
    body = (Path(__file__).parent / "fixtures" / "api_response_sample.json").read_text(encoding="utf-8")
    resp = InterceptedResponse(
        url="https://x.example.com/api/floorplans",
        method="GET",
        status=200,
        content_type="application/json",
        body=body.encode("utf-8"),
    )
    result = await tier1_api.extract(_session_with([resp]), CATALOGUE)
    assert result.status == ExtractionStatus.SUCCESS or result.confidence_score > 0
    units = result.raw_fields["units"]
    assert any(u["unit_number"] == "501" for u in units)
    assert any(u["asking_rent"] == 2750 for u in units)


async def test_non_matching_url_ignored() -> None:
    resp = InterceptedResponse(
        url="https://x.example.com/static/main.js",
        method="GET",
        status=200,
        content_type="application/javascript",
        body=b"console.log('x')",
    )
    result = await tier1_api.extract(_session_with([resp]), CATALOGUE)
    assert result.status == ExtractionStatus.FAILED
    assert "miss" in (result.error_message or "")


async def test_malformed_json_silently_discarded() -> None:
    bad = InterceptedResponse(
        url="https://x.example.com/api/units",
        method="GET", status=200, content_type="application/json",
        body=b"{not valid json",
    )
    result = await tier1_api.extract(_session_with([bad]), CATALOGUE)
    assert result.status == ExtractionStatus.FAILED
    assert "no parseable units" in (result.error_message or "")


async def test_confidence_score_calculation() -> None:
    payload = {"units": [{"unitNumber": "1", "rent": 1000, "availability": "available", "squareFeet": 500, "bedBath": "1/1"}]}
    resp = InterceptedResponse(
        url="https://x.example.com/api/units",
        method="GET", status=200, content_type="application/json",
        body=json.dumps(payload).encode("utf-8"),
    )
    result = await tier1_api.extract(_session_with([resp]), CATALOGUE)
    assert 0.7 <= result.confidence_score <= 1.0


async def test_api_catalogue_miss_returns_failed() -> None:
    resp = InterceptedResponse(
        url="https://x.example.com/some/other/endpoint",
        method="GET", status=200, content_type="application/json",
        body=b'{"x":1}',
    )
    result = await tier1_api.extract(_session_with([resp]), CATALOGUE)
    assert result.status == ExtractionStatus.FAILED


async def test_field_confidences_populated() -> None:
    body = (Path(__file__).parent / "fixtures" / "api_response_sample.json").read_text(encoding="utf-8")
    resp = InterceptedResponse(
        url="https://x.example.com/api/floorplans",
        method="GET", status=200, content_type="application/json",
        body=body.encode("utf-8"),
    )
    result = await tier1_api.extract(_session_with([resp]), CATALOGUE)
    assert "unit_number" in result.field_confidences
    assert result.field_confidences["unit_number"] > 0


def test_matches_catalogue_helper() -> None:
    assert matches_catalogue("https://x.example.com/api/units", CATALOGUE)
    assert not matches_catalogue("https://x.example.com/static/x.js", CATALOGUE)
