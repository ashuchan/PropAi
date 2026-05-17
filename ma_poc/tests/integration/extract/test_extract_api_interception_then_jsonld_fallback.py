"""Extract layer: empty API → JSON-LD fallback.

Verifies that when intercepted API responses contain no usable unit data,
the JSON-LD tier picks up the property and wins.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from pms.adapters.base import AdapterContext
from pms.adapters.generic import GenericAdapter


@dataclass
class _StubDetected:
    pms: str = "unknown"
    confidence: float = 0.5
    evidence: list = None  # type: ignore[assignment]
    recommended_strategy: str = "generic"

    def __post_init__(self) -> None:
        if self.evidence is None:
            self.evidence = []


def _make_fetch_result(html: str) -> FetchResult:
    return FetchResult(
        url="https://example.com/apartments",
        outcome=FetchOutcome.OK,
        status=200,
        body=html.encode("utf-8"),
        headers={},
        render_mode=RenderMode.GET,
        final_url="https://example.com/apartments",
        attempts=1,
        elapsed_ms=1,
    )


def _make_ctx(
    api_responses: list[dict[str, Any]],
    fetch_result: FetchResult,
    property_id: str = "prop-jsonld-001",
) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://example.com/apartments",
        detected=_StubDetected(),  # type: ignore[arg-type]
        profile=None,
        expected_total_units=None,
        property_id=property_id,
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    ctx.fetch_result = fetch_result
    return ctx


_JSONLD_LISTING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<title>Parkview Apartments</title>
<script type="application/ld+json">
[
  {
    "@context": "https://schema.org",
    "@type": "Apartment",
    "name": "1BR Classic",
    "numberOfRooms": 1,
    "floorSize": {"@type": "QuantitativeValue", "value": 720},
    "offers": {"@type": "Offer", "lowPrice": 1550, "highPrice": 1700}
  },
  {
    "@context": "https://schema.org",
    "@type": "Apartment",
    "name": "2BR Deluxe",
    "numberOfRooms": 2,
    "floorSize": {"@type": "QuantitativeValue", "value": 1050},
    "offers": {"@type": "Offer", "lowPrice": 2100, "highPrice": 2400}
  }
]
</script>
</head>
<body>
  <h1>Parkview Apartments</h1>
  <p>Rent from $1,550/mo. 1BR and 2BR floor plans available.</p>
</body>
</html>"""


def test_empty_api_falls_through_to_jsonld() -> None:
    """Empty API responses → JSON-LD tier extracts plan summaries from HTML.

    D16: the synthetic Schema.org ``Apartment`` entries carry no per-
    apartment identity (no unit_number / no availability_date / no
    floor), so the post-process partition correctly routes them to
    ``plan_summaries`` — assert specifically against that partition to
    catch regressions in either direction.
    """
    ctx = _make_ctx(
        api_responses=[],
        fetch_result=_make_fetch_result(_JSONLD_LISTING_HTML),
    )
    result = asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))

    assert result.tier_used == "TIER_2_JSONLD", (
        f"Expected TIER_2_JSONLD but got {result.tier_used!r}"
    )
    # Tight assertion — the synthetic JSON-LD fixture is plan-level by
    # construction (no per-apartment fields), so plan_summaries must be
    # populated and units must be empty.
    assert len(result.plan_summaries) >= 1
    assert len(result.units) == 0


def test_noise_api_response_falls_through_to_jsonld() -> None:
    """An API response that is not unit-shaped (e.g. a chatbot config) → JSON-LD wins."""
    noise_api = [
        {
            "url": "https://example.com/api/chatbot-config",
            "body": {"type": "chatbot", "apiKey": "xyz", "messages": []},
        }
    ]
    ctx = _make_ctx(
        api_responses=noise_api,
        fetch_result=_make_fetch_result(_JSONLD_LISTING_HTML),
    )
    result = asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))

    assert result.tier_used == "TIER_2_JSONLD"
    # D16: see test_empty_api_falls_through_to_jsonld — plan-level routing.
    assert len(result.plan_summaries) >= 1
    assert len(result.units) == 0


def test_empty_api_list_body_falls_through_to_jsonld() -> None:
    """API responds with an empty list body → JSON-LD wins."""
    ctx = _make_ctx(
        api_responses=[{"url": "https://example.com/api/units", "body": []}],
        fetch_result=_make_fetch_result(_JSONLD_LISTING_HTML),
    )
    result = asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))

    assert result.tier_used == "TIER_2_JSONLD"
    # D16: see test_empty_api_falls_through_to_jsonld — plan-level routing.
    assert len(result.plan_summaries) >= 1
    assert len(result.units) == 0


def test_jsonld_fallback_units_have_required_fields() -> None:
    """Rows extracted via JSON-LD have at least beds or rent_low populated.

    D16: walks both partitions because plan-level Apartment entries route
    to ``plan_summaries`` while genuine unit-level rows route to ``units``.
    """
    ctx = _make_ctx(
        api_responses=[],
        fetch_result=_make_fetch_result(_JSONLD_LISTING_HTML),
    )
    result = asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))

    assert result.tier_used == "TIER_2_JSONLD"
    for unit in list(result.units) + list(result.plan_summaries):
        assert isinstance(unit, dict)
        has_beds = unit.get("bedrooms") is not None or unit.get("beds") is not None
        has_rent = (
            unit.get("market_rent_low") is not None
            or unit.get("rent_low") is not None
            or unit.get("rent_range") is not None
        )
        assert has_beds or has_rent, (
            f"JSON-LD row missing both beds and rent: {unit}"
        )
