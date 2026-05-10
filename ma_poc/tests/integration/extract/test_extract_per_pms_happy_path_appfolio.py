"""Extract layer: AppFolio per-PMS happy path.

Uses corpus fixture `corpus/appfolio/api_listings.json` to drive the AppFolio
adapter and asserts that at least one UnitRecord-shaped dict with required
fields is produced.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


from pms.adapters.appfolio import AppFolioAdapter
from pms.adapters.base import AdapterContext


@dataclass
class _StubDetected:
    pms: str = "appfolio"
    confidence: float = 0.95
    evidence: list = None  # type: ignore[assignment]
    recommended_strategy: str = "api_first"

    def __post_init__(self) -> None:
        if self.evidence is None:
            self.evidence = ["host ends in appfolio.com"]


def _make_ctx(api_responses: list[dict[str, Any]], property_id: str = "prop-af-001") -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://testprop.appfolio.com/listings",
        detected=_StubDetected(),  # type: ignore[arg-type]
        profile=None,
        expected_total_units=None,
        property_id=property_id,
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


def test_appfolio_corpus_extracts_units(corpus: Any) -> None:
    """AppFolio corpus fixture → at least 1 unit with beds and price fields."""
    api_responses = corpus.load_json("appfolio/api_listings.json")
    ctx = _make_ctx(api_responses)
    result = asyncio.run(AppFolioAdapter().extract(page=None, ctx=ctx))

    assert len(result.units) >= 1, (
        f"Expected at least 1 unit from AppFolio corpus but got 0. "
        f"tier_used={result.tier_used!r}, errors={result.errors}"
    )
    for unit in result.units:
        assert isinstance(unit, dict)
        has_beds = unit.get("bedrooms") is not None or unit.get("beds") is not None
        has_rent = (
            unit.get("market_rent_low") is not None
            or unit.get("rent_low") is not None
            or unit.get("rent_range") is not None
            or unit.get("asking_rent") is not None
        )
        assert has_beds or has_rent, (
            f"AppFolio unit missing beds AND rent fields: {unit}"
        )


def test_appfolio_corpus_tier_is_api(corpus: Any) -> None:
    """AppFolio extraction via API response wins the API tier."""
    api_responses = corpus.load_json("appfolio/api_listings.json")
    ctx = _make_ctx(api_responses)
    result = asyncio.run(AppFolioAdapter().extract(page=None, ctx=ctx))

    assert "TIER_1" in result.tier_used or "API" in result.tier_used, (
        f"Expected a tier-1 API win but tier_used={result.tier_used!r}"
    )


def test_appfolio_corpus_confidence_above_threshold(corpus: Any) -> None:
    """Corpus extraction should have confidence ≥ 0.7."""
    api_responses = corpus.load_json("appfolio/api_listings.json")
    ctx = _make_ctx(api_responses)
    result = asyncio.run(AppFolioAdapter().extract(page=None, ctx=ctx))

    assert len(result.units) >= 1
    assert result.confidence >= 0.7, (
        f"Expected confidence ≥ 0.7 but got {result.confidence}"
    )


def test_appfolio_empty_api_returns_no_units() -> None:
    """When no AppFolio-shaped responses are present, result is empty."""
    ctx = _make_ctx([])
    result = asyncio.run(AppFolioAdapter().extract(page=None, ctx=ctx))

    assert result.units == []


def test_appfolio_corpus_all_listings_extracted(corpus: Any) -> None:
    """The corpus has 3 listings; all 3 should be extracted."""
    api_responses = corpus.load_json("appfolio/api_listings.json")
    ctx = _make_ctx(api_responses)
    result = asyncio.run(AppFolioAdapter().extract(page=None, ctx=ctx))

    assert len(result.units) >= 3, (
        f"Expected ≥3 units from 3-entry corpus but got {len(result.units)}"
    )


def test_appfolio_objects_envelope_is_unwrapped(corpus: Any) -> None:
    """AppFolio responses with {objects: [...]} envelope are unwrapped correctly."""
    api_responses = corpus.load_json("appfolio/api_listings.json")
    # Verify the fixture itself uses the `objects` envelope
    assert isinstance(api_responses[0]["body"], dict)
    assert "objects" in api_responses[0]["body"]

    ctx = _make_ctx(api_responses)
    result = asyncio.run(AppFolioAdapter().extract(page=None, ctx=ctx))

    assert len(result.units) >= 1
