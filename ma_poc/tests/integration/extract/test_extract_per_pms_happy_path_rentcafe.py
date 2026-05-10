"""Extract layer: RentCafe per-PMS happy path.

Uses corpus fixture `corpus/rentcafe/api_units.json` to drive the RentCafe
adapter and asserts that at least one UnitRecord-shaped dict with required
fields (beds, rent) is produced.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


from pms.adapters.base import AdapterContext
from pms.adapters.rentcafe import RentCafeAdapter
from pms.detector import DetectedPMS


@dataclass
class _StubDetected:
    pms: str = "rentcafe"
    confidence: float = 0.95
    evidence: list = None  # type: ignore[assignment]
    recommended_strategy: str = "api_first"

    def __post_init__(self) -> None:
        if self.evidence is None:
            self.evidence = ["host ends in rentcafe.com"]


def _make_ctx(api_responses: list[dict[str, Any]], property_id: str = "prop-rc-001") -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://testprop.rentcafe.com/apartments/",
        detected=_StubDetected(),  # type: ignore[arg-type]
        profile=None,
        expected_total_units=None,
        property_id=property_id,
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


def test_rentcafe_corpus_extracts_units(corpus: Any) -> None:
    """RentCafe corpus fixture → at least 1 unit with beds and rent fields."""
    api_responses = corpus.load_json("rentcafe/api_units.json")
    ctx = _make_ctx(api_responses)
    result = asyncio.run(RentCafeAdapter().extract(page=None, ctx=ctx))

    assert len(result.units) >= 1, (
        f"Expected at least 1 unit from RentCafe corpus but got 0. "
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
            f"RentCafe unit missing beds AND rent fields: {unit}"
        )


def test_rentcafe_corpus_tier_is_api(corpus: Any) -> None:
    """RentCafe extraction via API responses wins the API tier."""
    api_responses = corpus.load_json("rentcafe/api_units.json")
    ctx = _make_ctx(api_responses)
    result = asyncio.run(RentCafeAdapter().extract(page=None, ctx=ctx))

    assert "TIER_1" in result.tier_used or "API" in result.tier_used, (
        f"Expected a tier-1 API win but tier_used={result.tier_used!r}"
    )


def test_rentcafe_corpus_confidence_above_threshold(corpus: Any) -> None:
    """Corpus extraction should have confidence ≥ 0.7."""
    api_responses = corpus.load_json("rentcafe/api_units.json")
    ctx = _make_ctx(api_responses)
    result = asyncio.run(RentCafeAdapter().extract(page=None, ctx=ctx))

    assert len(result.units) >= 1
    assert result.confidence >= 0.7, (
        f"Expected confidence ≥ 0.7 but got {result.confidence}"
    )


def test_rentcafe_empty_api_returns_no_units() -> None:
    """When no RentCafe-shaped responses are present, result is empty."""
    ctx = _make_ctx([])
    result = asyncio.run(RentCafeAdapter().extract(page=None, ctx=ctx))

    assert result.units == []
    assert result.confidence == 0.0


def test_rentcafe_corpus_all_units_have_floor_plan_name(corpus: Any) -> None:
    """RentCafe corpus units should have a floor_plan_name set."""
    api_responses = corpus.load_json("rentcafe/api_units.json")
    ctx = _make_ctx(api_responses)
    result = asyncio.run(RentCafeAdapter().extract(page=None, ctx=ctx))

    assert len(result.units) >= 1
    for unit in result.units:
        has_name = (
            unit.get("floor_plan_name") is not None
            or unit.get("floorplanName") is not None
            or unit.get("name") is not None
        )
        assert has_name, f"RentCafe unit missing floor plan name: {unit}"
