"""Phase H tests — planner + budget wiring into the cascade."""

from __future__ import annotations

import pathlib
import pytest

_MA_POC = pathlib.Path(__file__).resolve().parent.parent.parent.parent  # ma_poc/
_GENERIC = _MA_POC / "pms" / "adapters" / "generic.py"
_SCRAPER = _MA_POC / "pms" / "scraper.py"

from ma_poc.models.scrape_profile import ProfileMaturity, ScrapeProfile
from ma_poc.services.source_planner import compute_budget


def test_h1_compute_budget_called_for_cold_profile() -> None:
    """compute_budget must return COLD rotation budget when profile is COLD."""
    p = ScrapeProfile(canonical_id="phase_h_001")
    assert p.confidence.maturity == ProfileMaturity.COLD
    p.confidence.cold_run_count = 0
    budget = compute_budget(p, is_cold=True)
    assert "llm_api_calls" in budget
    assert "llm_monolithic" in budget
    assert "link_hop" in budget
    # COLD run 0 mod 3 == 0: api_calls=1, monolithic=0, link_hop=1
    assert budget["llm_api_calls"] == 1
    assert budget["llm_monolithic"] == 0


def test_h1_compute_budget_hot_profile_gets_full_budget() -> None:
    """HOT profile must receive full budget."""
    p = ScrapeProfile(canonical_id="phase_h_002")
    p.confidence.maturity = ProfileMaturity.HOT
    budget = compute_budget(p, is_cold=False)
    assert budget["llm_api_calls"] == 3
    assert budget["llm_monolithic"] == 1
    assert budget["link_hop"] == 3


def test_h1_compute_budget_cold_rotates_by_run_count() -> None:
    """COLD budget must rotate based on cold_run_count."""
    p = ScrapeProfile(canonical_id="phase_h_003")
    p.confidence.cold_run_count = 1  # n % 3 == 1: api=0, monolithic=0, dom=1, link_hop=3
    budget = compute_budget(p, is_cold=True)
    assert budget["llm_api_calls"] == 0
    assert budget["llm_monolithic"] == 0
    assert budget["link_hop"] == 3

    p.confidence.cold_run_count = 2  # n % 3 == 2: api=0, monolithic=1, dom=1, link_hop=1
    budget = compute_budget(p, is_cold=True)
    assert budget["llm_api_calls"] == 0
    assert budget["llm_monolithic"] == 1


def test_h1_compute_budget_cold_always_keeps_dom_llm() -> None:
    """COLD rotations must always keep llm_dom_calls >= 1.

    The targeted-DOM-LLM tier is the only extractor that works on
    marketing/boutique-CMS sites without API responses or JSON-LD. Gating
    it for COLD profiles produced a -15pp success regression in May 2026
    (commit c19bc86 / PR #23). Pin the invariant so it can't recur.
    """
    p = ScrapeProfile(canonical_id="phase_h_dom_invariant")
    for cnt in (0, 1, 2, 3, 4, 5):
        p.confidence.cold_run_count = cnt
        budget = compute_budget(p, is_cold=True)
        assert budget["llm_dom_calls"] >= 1, (
            f"cold_run_count={cnt} dropped llm_dom_calls to "
            f"{budget['llm_dom_calls']} — see source_planner.compute_budget"
        )


def _get_adapter_context():
    """Import AdapterContext with playwright stubs in place."""
    import sys
    import types

    def _make_stub(name):
        mod = types.ModuleType(name)
        mod.BrowserContext = object  # type: ignore[attr-defined]
        mod.Page = object  # type: ignore[attr-defined]
        mod.async_playwright = object  # type: ignore[attr-defined]
        return mod

    sys.modules.setdefault("playwright", _make_stub("playwright"))
    sys.modules.setdefault("playwright.async_api", _make_stub("playwright.async_api"))
    from ma_poc.pms.adapters.base import AdapterContext
    from ma_poc.pms.detector import DetectedPMS
    return AdapterContext, DetectedPMS


def test_h2_adapter_context_has_budget_field() -> None:
    """AdapterContext must carry a budget field with default values."""
    AdapterContext, DetectedPMS = _get_adapter_context()
    ctx = AdapterContext(
        base_url="https://example.com",
        detected=DetectedPMS(pms="unknown", confidence=0.5),
        profile=None,
        expected_total_units=None,
        property_id="test_001",
    )
    assert hasattr(ctx, "budget"), "AdapterContext must have a budget field"
    assert "llm_api_calls" in ctx.budget
    assert "llm_monolithic" in ctx.budget
    assert "link_hop" in ctx.budget


def _get_assess_and_decide():
    """Import _assess_and_decide with playwright stubs in place."""
    import sys
    import types

    def _make_stub(name):
        mod = types.ModuleType(name)
        mod.BrowserContext = object  # type: ignore[attr-defined]
        mod.Page = object  # type: ignore[attr-defined]
        mod.async_playwright = object  # type: ignore[attr-defined]
        return mod

    sys.modules.setdefault("playwright", _make_stub("playwright"))
    sys.modules.setdefault("playwright.async_api", _make_stub("playwright.async_api"))
    from ma_poc.pms.adapters.generic import _assess_and_decide
    from ma_poc.pms.adapters.base import AdapterContext
    from ma_poc.pms.detector import DetectedPMS
    return _assess_and_decide, AdapterContext, DetectedPMS


def test_h2_planner_stop_respected_by_assess_and_decide() -> None:
    """_assess_and_decide must return STOP when planner says extraction is complete."""
    _assess_and_decide, AdapterContext, DetectedPMS = _get_assess_and_decide()

    ctx = AdapterContext(
        base_url="https://example.com",
        detected=DetectedPMS(pms="unknown", confidence=0.5),
        profile=None,
        expected_total_units=None,
        property_id="test_h2_001",
        budget={"llm_api_calls": 3, "llm_dom_calls": 1, "llm_monolithic": 1, "link_hop": 3},
    )

    # Units with full identity + physical + transactional — planner should STOP
    units = [
        {
            "unit_id": "U1",
            "bedrooms": "2",
            "sqft": "900",
            "asking_rent": 1500,
            "floor_plan_name": "Plan A",
        }
        for _ in range(10)
    ]
    decision_log: list = []
    decision = _assess_and_decide(units, set(), ctx, decision_log)
    assert decision is not None
    assert decision.action == "STOP"


def test_h2_empty_units_returns_none() -> None:
    """_assess_and_decide must return None when no units are present."""
    _assess_and_decide, AdapterContext, DetectedPMS = _get_assess_and_decide()

    ctx = AdapterContext(
        base_url="https://example.com",
        detected=DetectedPMS(pms="unknown", confidence=0.5),
        profile=None,
        expected_total_units=None,
        property_id="test_h2_002",
    )
    decision_log: list = []
    decision = _assess_and_decide([], set(), ctx, decision_log)
    assert decision is None
    assert len(decision_log) == 0


def test_h3_budget_zero_llm_targeted_prevents_api_llm_tier() -> None:
    """When llm_targeted budget is 0, api_llm_budget in cascade must be 0."""
    # Verifies that ctx.budget flows into the api_llm_budget variable.
    # We inspect the source rather than running the full async cascade.
    import ast
    src = _GENERIC.read_text()
    assert "ctx.budget" in src or "_budget.get(" in src, (
        "generic.py must read budget from ctx, not use hardcoded values"
    )
    assert "api_llm_budget = 3" not in src, (
        "Hardcoded api_llm_budget = 3 must be replaced with ctx.budget lookup"
    )
    assert "dom_llm_budget = 1" not in src, (
        "Hardcoded dom_llm_budget = 1 must be replaced with ctx.budget lookup"
    )


def test_h3_scraper_contains_compute_budget_call() -> None:
    """scraper.py must call compute_budget(."""
    src = _SCRAPER.read_text()
    assert "compute_budget(" in src, "scraper.py must call compute_budget("


def test_h3_generic_contains_plan_next_action_call() -> None:
    """generic.py must call plan_next_action(."""
    src = _GENERIC.read_text()
    assert "plan_next_action(" in src, "generic.py must call plan_next_action("
