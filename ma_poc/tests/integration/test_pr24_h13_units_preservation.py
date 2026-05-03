"""PR24 H13/H14 — units preserved through escalation, budget shared across hops."""

from __future__ import annotations

import sys
import types

for _name in ("playwright", "playwright.async_api"):
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        _mod.BrowserContext = object  # type: ignore[attr-defined]
        _mod.Page = object  # type: ignore[attr-defined]
        _mod.async_playwright = object  # type: ignore[attr-defined]
        sys.modules[_name] = _mod

from ma_poc.services.source_planner import (
    CompletenessReport,
    plan_next_action,
    evaluate_completeness,
)
from ma_poc.models.source import FieldValue, SourceId, ProvenancedUnit


def _make_partial_unit(with_identity: bool = True, with_physical: bool = True) -> ProvenancedUnit:
    u: ProvenancedUnit = ProvenancedUnit()
    if with_identity:
        u["unit_id"] = FieldValue(value="U1", source=SourceId.API_SIGHTMAP, confidence=0.95)
    if with_physical:
        u["beds"] = FieldValue(value=2, source=SourceId.API_SIGHTMAP, confidence=0.90)
        u["sqft"] = FieldValue(value=900, source=SourceId.API_SIGHTMAP, confidence=0.90)
    # Note: no transactional field → pct_with_transactional will be 0
    return u


def test_h13_incomplete_units_trigger_escalation() -> None:
    """H13: when units have no transactional data, planner escalates (not STOP)."""
    units = [_make_partial_unit() for _ in range(5)]
    report = evaluate_completeness(units)
    assert report.pct_with_transactional == 0.0
    d = plan_next_action(
        report,
        sources_already_run=set(),
        budget_remaining={"llm_api_calls": 3, "llm_dom_calls": 1, "llm_monolithic": 1, "link_hop": 3},
    )
    assert d.action != "STOP", "planner should not STOP when transactional group is empty"


def test_h13_stop_only_when_all_groups_present() -> None:
    """H13: STOP fires only when all three groups pass their floors."""
    report = CompletenessReport(10, 1.0, 1.0, 0.80, 0.95)
    d = plan_next_action(
        report,
        sources_already_run=set(),
        budget_remaining={"llm_api_calls": 3, "llm_dom_calls": 1, "llm_monolithic": 1, "link_hop": 3},
    )
    assert d.action == "STOP"


def test_h14_budget_shared_prevents_double_allocation() -> None:
    """H14: shared_budget parameter prevents recomputing budget on each hop."""
    # We verify the scraper.scrape() signature accepts shared_budget
    import inspect
    from ma_poc.pms.scraper import scrape
    sig = inspect.signature(scrape)
    assert "shared_budget" in sig.parameters, \
        "scrape() must accept shared_budget to prevent double budget allocation"


def test_h14_try_link_hop_accepts_shared_budget() -> None:
    """H14: _try_link_hop also accepts shared_budget."""
    import inspect
    from ma_poc.pms.scraper import _try_link_hop
    sig = inspect.signature(_try_link_hop)
    assert "shared_budget" in sig.parameters, \
        "_try_link_hop() must accept shared_budget"
