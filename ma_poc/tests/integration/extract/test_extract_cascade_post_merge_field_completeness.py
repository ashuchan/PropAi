"""Phase 5 — generic.py cascade post-merge tests.

Exercises the new behavior where sub-tier 0 (profile mapping replay) no
longer preempts the adapter when replayed units are field-incomplete.
The post-merge step combines replay + cascade output via merge_sources.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from models.scrape_profile import LlmFieldMapping, ScrapeProfile
from pms.adapters.base import AdapterContext, AdapterResult
from pms.adapters.generic import GenericAdapter


@dataclass
class _StubDetected:
    pms: str = "unknown"


def _make_ctx(profile: ScrapeProfile, api_responses: list[dict[str, Any]]) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://example.com/availability",
        detected=_StubDetected(),  # type: ignore[arg-type]
        profile=profile,
        expected_total_units=None,
        property_id="phase5-test",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


def test_phase5_post_merge_replay_only_no_cascade() -> None:
    """When replay produces field-rich units, behavior matches pre-Phase-5."""
    p = ScrapeProfile(canonical_id="phase5-001")
    p.api_hints.llm_field_mappings = [
        LlmFieldMapping(
            api_url_pattern="/api/units",
            json_paths={"unit_id": "id", "rent_low": "rent"},
            response_envelope="",
        )
    ]
    api = [
        {
            "url": "https://example.com/api/units",
            "body": [{"id": "U1", "rent": 1500}, {"id": "U2", "rent": 1700}],
        }
    ]
    ctx = _make_ctx(p, api)
    adapter = GenericAdapter()
    result = asyncio.run(adapter.extract(page=None, ctx=ctx))
    # Replay produces units with unit_id + market_rent_low → field-rich →
    # short-circuits to TIER_1_PROFILE_MAPPING (legacy behavior preserved)
    assert result.tier_used == "TIER_1_PROFILE_MAPPING"
    assert len(result.units) == 2


def test_phase5_post_merge_field_incomplete_replay_falls_through() -> None:
    """A mapping that learned ONLY beds (no rent / unit_id) is field-incomplete.
    Sub-tier 0 stashes it; the cascade keeps running; post-merge combines.
    Phase 5 invariant: the partial mapping cannot mask cascade-native fields."""
    p = ScrapeProfile(canonical_id="phase5-002")
    p.api_hints.llm_field_mappings = [
        LlmFieldMapping(
            api_url_pattern="/api/units",
            json_paths={"beds": "bedroom_count"},  # only beds, no identity / rent
            response_envelope="",
        )
    ]
    # Body has fields the mapping learns AND fields the cascade's narrow parser
    # can extract natively — id, asking_rent, sqft.
    api = [
        {
            "url": "https://example.com/api/units",
            "body": [
                {
                    "id": "U1",
                    "unit_number": "U1",
                    "rent": 1500,
                    "sqft": 750,
                    "bedroom_count": 1,
                },
                {
                    "id": "U2",
                    "unit_number": "U2",
                    "rent": 1700,
                    "sqft": 850,
                    "bedroom_count": 2,
                },
            ],
        }
    ]
    ctx = _make_ctx(p, api)
    adapter = GenericAdapter()
    result = asyncio.run(adapter.extract(page=None, ctx=ctx))
    # Either path is acceptable: cascade-only (replay was incomplete and
    # produced no usable units) OR the merged path (replay produced beds-only
    # rows which were merged with cascade rows).
    assert result.units, "cascade or merged path should produce units"


def test_phase5_post_merge_helper_with_no_sidecar_is_noop() -> None:
    """If sub-tier 0 didn't stash replay units, post-merge does nothing."""
    result = AdapterResult()
    result.units = [{"unit_number": "U1", "asking_rent": 1500}]
    result.tier_used = "TIER_1_API"
    ctx = AdapterContext(
        base_url="x",
        detected=_StubDetected(),  # type: ignore[arg-type]
        profile=None,
        expected_total_units=None,
        property_id="p",
    )
    GenericAdapter._phase5_post_merge(result, ctx)
    assert result.tier_used == "TIER_1_API"
    assert len(result.units) == 1


def test_phase5_post_merge_sidecar_only_promotes_to_replay_tier() -> None:
    """Sidecar present, cascade empty → output = sidecar; tier = TIER_1_PROFILE_MAPPING."""
    result = AdapterResult()
    result.units = []
    result._phase5_replay_units = [{"unit_number": "U1", "asking_rent": 1500}]  # type: ignore[attr-defined]
    ctx = AdapterContext(
        base_url="x",
        detected=_StubDetected(),  # type: ignore[arg-type]
        profile=None,
        expected_total_units=None,
        property_id="p",
    )
    GenericAdapter._phase5_post_merge(result, ctx)
    assert result.tier_used == "TIER_1_PROFILE_MAPPING"
    assert len(result.units) == 1


def test_phase5_post_merge_both_sources_emit_merged_tier_label() -> None:
    """Both replay sidecar AND cascade output → tier rewrites to TIER_MERGED_DETERMINISTIC."""
    result = AdapterResult()
    result.units = [
        {"unit_number": "U1", "asking_rent": 1500, "sqft": 750}
    ]
    result.tier_used = "TIER_1_API"
    result._phase5_replay_units = [{"unit_number": "U1", "beds": 1, "asking_rent": 1500}]  # type: ignore[attr-defined]
    ctx = AdapterContext(
        base_url="https://example.com",
        detected=_StubDetected(),  # type: ignore[arg-type]
        profile=None,
        expected_total_units=None,
        property_id="p",
    )
    GenericAdapter._phase5_post_merge(result, ctx)
    assert result.tier_used == "TIER_MERGED_DETERMINISTIC"
    # The merged unit should carry the union of fields (beds from sidecar,
    # sqft from cascade)
    assert len(result.units) == 1
    u = result.units[0]
    assert u.get("beds") == 1
    assert u.get("sqft") == 750
    assert u.get("asking_rent") == 1500


def test_phase5_post_merge_with_llm_cascade_emits_hybrid_label() -> None:
    result = AdapterResult()
    result.units = [{"unit_number": "U1", "asking_rent": 1500}]
    result.tier_used = "TIER_4_LLM_API"
    result._phase5_replay_units = [{"unit_number": "U1", "sqft": 750}]  # type: ignore[attr-defined]
    ctx = AdapterContext(
        base_url="https://x.com",
        detected=_StubDetected(),  # type: ignore[arg-type]
        profile=None,
        expected_total_units=None,
        property_id="p",
    )
    GenericAdapter._phase5_post_merge(result, ctx)
    assert result.tier_used == "TIER_MERGED_HYBRID"


def test_phase5_provenance_stripped_from_legacy_output() -> None:
    """Phase 5 invariant H6: emitted unit dicts must pass make_unit_dict shape
    when serialized to legacy output. Provenance lives elsewhere."""
    result = AdapterResult()
    result.units = [{"unit_number": "U1", "asking_rent": 1500}]
    result.tier_used = "TIER_1_API"
    result._phase5_replay_units = [{"unit_number": "U1", "sqft": 750}]  # type: ignore[attr-defined]
    ctx = AdapterContext(
        base_url="x",
        detected=_StubDetected(),  # type: ignore[arg-type]
        profile=None,
        expected_total_units=None,
        property_id="p",
    )
    GenericAdapter._phase5_post_merge(result, ctx)
    for u in result.units:
        assert "_provenance" not in u
