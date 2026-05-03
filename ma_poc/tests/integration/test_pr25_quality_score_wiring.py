"""PR25 B1 — quality_score wiring tests.

Verifies that LlmFieldMapping.quality_score actually influences runtime
behavior (replay confidence and field-rich short-circuit gate).

Note on json_paths keys: apply_saved_mapping uses hardcoded internal field names
("bedrooms", "bathrooms", "rent_low" → "market_rent_low", etc.). Tests must use
these exact keys to produce field-rich replayed units.
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass
from typing import Any

for _name in ("playwright", "playwright.async_api"):
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        _mod.BrowserContext = object  # type: ignore[attr-defined]
        _mod.Page = object  # type: ignore[attr-defined]
        _mod.async_playwright = object  # type: ignore[attr-defined]
        sys.modules[_name] = _mod

from ma_poc.models.scrape_profile import LlmFieldMapping, ScrapeProfile
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.generic import GenericAdapter


@dataclass
class _StubDetected:
    pms: str = "unknown"
    confidence: float = 0.5


def _make_ctx(profile: ScrapeProfile, api_responses: list[dict[str, Any]]) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://example.com/availability",
        detected=_StubDetected(),  # type: ignore[arg-type]
        profile=profile,
        expected_total_units=None,
        property_id="b1-test",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


# Body that applies_saved_mapping can produce field-rich units from.
# apply_saved_mapping uses "bedrooms"/"bathrooms" (not "beds"/"baths") as json_path keys.
_FIELD_RICH_API = [{
    "url": "https://example.com/api/units",
    "body": [
        {"id": "U1", "price": 1500, "br": 1, "ba": 1, "area": 750},
        {"id": "U2", "price": 1700, "br": 2, "ba": 1, "area": 900},
    ],
}]

# Mapping that produces identity + 2 physical + transactional → field-rich
_FIELD_RICH_JSON_PATHS = {
    "unit_id": "id",
    "rent_low": "price",
    "bedrooms": "br",
    "bathrooms": "ba",
    "sqft": "area",
}


def test_b1_quality_score_lowers_replay_confidence() -> None:
    """A mapping at quality_score=0.4 produces lower replay confidence than 1.0."""
    # High-quality mapping
    p_hi = ScrapeProfile(canonical_id="b1-hi")
    p_hi.api_hints.llm_field_mappings = [
        LlmFieldMapping(
            api_url_pattern="/api/units",
            json_paths=_FIELD_RICH_JSON_PATHS,
            quality_score=1.0,
        )
    ]
    r_hi = asyncio.run(GenericAdapter().extract(page=None, ctx=_make_ctx(p_hi, _FIELD_RICH_API)))

    # Low-quality mapping (same content)
    p_lo = ScrapeProfile(canonical_id="b1-lo")
    p_lo.api_hints.llm_field_mappings = [
        LlmFieldMapping(
            api_url_pattern="/api/units",
            json_paths=_FIELD_RICH_JSON_PATHS,
            quality_score=0.4,
        )
    ]
    r_lo = asyncio.run(GenericAdapter().extract(page=None, ctx=_make_ctx(p_lo, _FIELD_RICH_API)))

    # Both should produce units
    assert r_hi.units, "hi-quality replay must produce units"
    assert r_lo.units, "lo-quality replay must still produce units"

    # The hi-quality mapping short-circuits at TIER_1_PROFILE_MAPPING with a confidence
    # weighted by quality_score. The lo-quality mapping must NOT short-circuit.
    assert r_hi.tier_used == "TIER_1_PROFILE_MAPPING", (
        f"quality_score=1.0 must short-circuit; got tier_used={r_hi.tier_used}"
    )
    assert r_lo.tier_used != "TIER_1_PROFILE_MAPPING", (
        f"quality_score=0.4 must not short-circuit; got tier_used={r_lo.tier_used}"
    )
    # Hi-quality replay confidence is set by the short-circuit formula; lo is 0 or cascade value.
    assert r_lo.confidence < r_hi.confidence, (
        f"quality_score=0.4 must produce lower confidence than 1.0; "
        f"got hi={r_hi.confidence}, lo={r_lo.confidence}"
    )


def test_b1_quality_score_disqualifies_field_rich_short_circuit() -> None:
    """When quality_score < 0.7, the field-rich short-circuit must NOT fire,
    even when the replayed units look field-rich."""
    p = ScrapeProfile(canonical_id="b1-no-shortcircuit")
    p.api_hints.llm_field_mappings = [
        LlmFieldMapping(
            api_url_pattern="/api/units",
            json_paths=_FIELD_RICH_JSON_PATHS,
            quality_score=0.4,  # below 0.7 threshold
        )
    ]
    result = asyncio.run(GenericAdapter().extract(page=None, ctx=_make_ctx(p, _FIELD_RICH_API)))

    # tier_used must NOT be TIER_1_PROFILE_MAPPING (the short-circuit path).
    assert result.tier_used != "TIER_1_PROFILE_MAPPING", (
        f"low-quality mapping must not short-circuit at TIER_1_PROFILE_MAPPING; "
        f"got tier_used={result.tier_used}"
    )


def test_b1_default_quality_score_one_preserves_legacy_behavior() -> None:
    """Mappings without explicit quality_score (default 1.0) replay at full confidence.
    Ensures B1 doesn't regress existing behavior."""
    p = ScrapeProfile(canonical_id="b1-default")
    p.api_hints.llm_field_mappings = [
        LlmFieldMapping(
            api_url_pattern="/api/units",
            json_paths=_FIELD_RICH_JSON_PATHS,
            # quality_score defaults to 1.0
        )
    ]
    result = asyncio.run(GenericAdapter().extract(page=None, ctx=_make_ctx(p, _FIELD_RICH_API)))
    assert result.units, "default quality_score=1.0 must produce units"
    assert result.tier_used == "TIER_1_PROFILE_MAPPING", (
        f"default quality must short-circuit; got tier_used={result.tier_used}"
    )
    # Confidence formula: min(0.90, 0.7 + 0.03 * n_units) * 1.0 ≥ 0.70 for ≥1 unit
    assert result.confidence >= 0.70


def test_b1_aggregate_min_quality_when_multiple_mappings() -> None:
    """When two mappings replay against different responses, the aggregate
    quality is the MIN of theirs (worst-case), blocking short-circuit when < 0.7."""
    api = [
        {
            "url": "https://example.com/api/units",
            "body": [{"id": "U1", "price": 1500, "br": 1, "ba": 1, "area": 750}],
        },
        {
            "url": "https://example.com/api/availability",
            "body": [{"unit": "U1", "date": "2026-06-01"}],
        },
    ]
    p = ScrapeProfile(canonical_id="b1-multi")
    p.api_hints.llm_field_mappings = [
        LlmFieldMapping(
            api_url_pattern="/api/units",
            json_paths=_FIELD_RICH_JSON_PATHS,
            quality_score=1.0,
        ),
        LlmFieldMapping(
            api_url_pattern="/api/availability",
            json_paths={"unit_id": "unit", "available_date": "date"},
            quality_score=0.5,
        ),
    ]
    # Both replay; aggregate should reflect the lower of the two (0.5 < 0.7)
    # → field-rich short-circuit must not fire.
    result = asyncio.run(GenericAdapter().extract(page=None, ctx=_make_ctx(p, api)))
    assert result.tier_used != "TIER_1_PROFILE_MAPPING", (
        "aggregate min quality (0.5) below threshold must prevent short-circuit; "
        f"got tier_used={result.tier_used}"
    )
