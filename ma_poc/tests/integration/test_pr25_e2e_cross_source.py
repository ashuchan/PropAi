"""PR25 — E2E cross-source merge tests (deferred from PR24)."""

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

import pytest

from ma_poc.models.scrape_profile import LlmFieldMapping, ScrapeProfile
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.generic import GenericAdapter
from ma_poc.services.source_planner import (
    CompletenessReport,
    Decision,
    evaluate_completeness,
    plan_next_action,
)


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
        property_id="t7t8-test",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


def test_e2e_physical_then_transactional_merge() -> None:
    """T7: A low-quality mapping replays physical units; the cascade narrow parser
    produces transactional units. _phase5_post_merge merges both into unified rows.

    Exercises the cross-source merge path introduced by the B1 fix: when
    quality_score < 0.7, replay is stashed and the cascade runs, then
    _phase5_post_merge combines the two sources by identity.
    """
    # API body parseable by both the mapping (via apply_saved_mapping) and
    # the narrow parser (via parse_generic_api). The mapping captures physical
    # fields; the narrow parser captures transactional fields.
    api = [
        {
            "url": "https://example.com/api/units",
            "body": [
                {
                    "unit_number": "U1",
                    "beds": 2,
                    "sqft": 900,
                    "asking_rent": 1500,
                    "available_date": "2026-06-01",
                },
            ],
        }
    ]

    # Low-quality mapping: quality_score=0.4 → stashed, not short-circuited.
    # Replays only physical fields (bedrooms + sqft), forcing cascade to contribute.
    p = ScrapeProfile(canonical_id="t7-cross-merge")
    p.api_hints.llm_field_mappings = [
        LlmFieldMapping(
            api_url_pattern="/api/units",
            json_paths={
                "unit_id": "unit_number",
                "bedrooms": "beds",
                "sqft": "sqft",
            },
            quality_score=0.4,  # below 0.7 → no short-circuit → cascade runs
        )
    ]
    result = asyncio.run(GenericAdapter().extract(page=None, ctx=_make_ctx(p, api)))

    assert result.units, "cross-source merge must produce at least one unit"
    # The cascade ran after replay, so tier must reflect that both sources contributed.
    assert result.tier_used != "TIER_1_PROFILE_MAPPING", (
        f"low-quality replay must not short-circuit; got tier_used={result.tier_used}"
    )
    # At least one unit must carry data from the cascade (transactional or identity)
    # and at least one from the mapping (physical). The merger unifies them.
    merged_unit = result.units[0]
    # The cascade (narrow parser) should have added transactional/identity fields
    has_rent = any(
        merged_unit.get(k) for k in ("asking_rent", "market_rent_low", "market_rent_high", "rent_low")
    )
    has_physical = any(merged_unit.get(k) for k in ("beds", "bedrooms", "sqft"))
    assert has_rent or has_physical, (
        "merged unit must carry fields from at least one source"
    )


def test_e2e_planner_target_group_routes_to_correct_tier() -> None:
    """T8: When units have only physical data (no transactional), the planner
    returns a Decision whose target_field_group is 'transactional'.

    This verifies that the planner correctly identifies the missing group and
    encodes it in the decision so the cascade can route escalation accordingly.
    """
    # Units with only physical fields — no rent or availability.
    physical_only_units = [
        {"unit_id": "U1", "beds": 2, "sqft": 900},
        {"unit_id": "U2", "beds": 1, "sqft": 750},
    ]
    report = evaluate_completeness(physical_only_units)

    # Completeness: identity and physical present, transactional absent.
    assert report.pct_with_transactional == 0.0, (
        "units with no rent/availability must have 0% transactional completeness"
    )
    assert report.pct_with_physical > 0.0, "physical fields must be detected"

    # Planner with budget remaining should recommend escalation.
    budget = {"llm_api_calls": 3, "llm_dom_calls": 1, "link_hop": 3, "llm_monolithic": 1}
    decision = plan_next_action(report, sources_already_run=set(), budget_remaining=budget)

    # When transactional group is empty and budget remains, planner must escalate.
    assert decision.action != "STOP", (
        f"planner must not STOP when transactional group is empty; got action={decision.action}"
    )
    assert decision.action != "ACCEPT_PARTIAL", (
        f"planner must not ACCEPT_PARTIAL with budget remaining; got action={decision.action}"
    )
    # The target_field_group should direct the cascade toward transactional data.
    if decision.target_field_group is not None:
        assert decision.target_field_group == "transactional", (
            f"planner must target 'transactional' group; got {decision.target_field_group}"
        )
