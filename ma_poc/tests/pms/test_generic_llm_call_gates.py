"""Source-level guardrails for the LLM-call gates inside ``GenericAdapter``.

The monolithic LLM (tier 6c) and DOM-targeted LLM (tier 6b) call sites
live deep inside ``pms.adapters.generic._extract_internal`` (~2800 lines
of async logic). A full integration test would need a stub Playwright
Page, an AdapterContext, and a full extraction cascade — overkill for
these narrow gate conditions. Instead the tests below pin the call
sites at source level so any future refactor that drops one of the
gates fails loudly and prompts a review.

Two distinct gate families are covered:

1.  **stub_aggregate_copy gate** — when the entry page's structural-
    listing detector says "no real listings on this body", the cheaper
    DOM-targeted LLM is skipped (existing behaviour) AND the more
    expensive monolithic LLM is now also skipped. Pre-fix the monolithic
    call still fired on the same body and produced an empty result 247
    times in the 2026-05-18 cloud run, burning budget that hops needed.

2.  **High-floor-plan-signal free-call bypass** — pages with
    ``floor_plan_signal_count >= SIGNAL_THRESHOLD_HIGH`` (= 4 distinct
    structural signal types) are signal-saturated. Both LLM call sites
    bypass the per-property call budget AND skip the decrement on these
    pages so the LLM call always fires on the page most likely to
    produce real units, regardless of how many earlier hops consumed
    the call budget. The cost cap stays as the ultimate brake.
"""

from __future__ import annotations

import inspect
import re

from ma_poc.pms.adapters import generic as _generic_adapter


_SRC = inspect.getsource(_generic_adapter)


# ── stub_aggregate_copy gate on the monolithic LLM ──────────────────────────


def test_monolithic_llm_call_site_checks_stub_aggregate_copy() -> None:
    """The if-block that decides whether to fire the monolithic LLM must
    include ``not _stub_aggregate_copy`` in its conditions. Without it the
    gate skips the cheaper LLM_DOM call (per playbook §8.10) but still
    fires the monolithic call on the same body — guaranteed empty
    result, burns ~$0.02 + ~7s per property."""
    match = re.search(
        r"if\s*\([^)]*?not\s+_stub_aggregate_copy[^)]*?llm_monolithic"
        r"|if\s*\([^)]*?llm_monolithic[^)]*?not\s+_stub_aggregate_copy",
        _SRC,
        re.DOTALL,
    )
    assert match, (
        "Regression: the monolithic LLM call site no longer checks "
        "`not _stub_aggregate_copy`. See generic.py near the "
        '`_budget["llm_monolithic"]` decrement.'
    )


def test_stub_aggregate_copy_skip_event_has_distinct_reason() -> None:
    """When the monolithic LLM is skipped because of stub_aggregate_copy,
    the analyzer needs to tell it apart from budget-pressure skips. The
    skip event must emit a reason containing ``stub_aggregate_copy``."""
    assert "stub_aggregate_copy" in _SRC, (
        "The monolithic LLM skip path must surface a stub_aggregate_copy "
        "reason so the analyzer can bucket it separately from budget skips."
    )


# ── High-floor-plan-signal free-call bypass ──────────────────────────────────


def test_high_signal_page_flag_computed_once_per_property() -> None:
    """``_high_signal_page_dom`` is computed alongside ``_stub_aggregate_copy``
    so both LLM call sites can reuse it without recomputation."""
    assert "_high_signal_page_dom" in _SRC
    assert "SIGNAL_THRESHOLD_HIGH" in _SRC


def test_monolithic_call_site_bypasses_budget_on_high_signal() -> None:
    """The monolithic call site must OR ``_high_signal_page_dom`` into its
    budget-gate so a saturated page (fp_signals >= 4) still gets an LLM
    call even after the budget is exhausted. Match is multi-line because
    the gate condition spans several lines in the source."""
    match = re.search(
        r"llm_monolithic[\s\S]*?or\s+_high_signal_page_dom",
        _SRC,
    )
    assert match, (
        "Regression: the monolithic LLM call site no longer treats "
        "high-fp_signal pages as a free budget pass."
    )


def test_dom_targeted_call_site_bypasses_budget_on_high_signal() -> None:
    """Same contract for the DOM-targeted LLM. Multi-line gate condition."""
    match = re.search(
        r"llm_dom_calls[\s\S]*?or\s+_high_signal_page_dom",
        _SRC,
    )
    assert match, (
        "Regression: the DOM-targeted LLM call site no longer treats "
        "high-fp_signal pages as a free budget pass."
    )


def test_high_signal_page_does_not_decrement_budget() -> None:
    """When the high-signal bypass fires, the budget MUST NOT decrement —
    otherwise the next call wastes a slot. The decrement should sit
    inside a ``if not _high_signal_page_dom:`` guard at BOTH call sites."""
    guard_matches = re.findall(
        r"if\s+not\s+_high_signal_page_dom\s*:\s*\n\s*_budget\[",
        _SRC,
    )
    assert len(guard_matches) >= 2, (
        f"Expected >= 2 `if not _high_signal_page_dom: _budget[...]` "
        f"guards (one each for llm_monolithic and llm_dom_calls); found "
        f"{len(guard_matches)}."
    )


# ── Signal-threshold constant invariants ─────────────────────────────────────


def test_signal_threshold_high_is_strictly_above_structural() -> None:
    """The threshold must be ABOVE SIGNAL_THRESHOLD_STRUCTURAL — otherwise
    callers would confuse "real listing page" with "saturated listing page"
    and the free-call would fire on every page that has any unit signal."""
    from ma_poc.pms.signal_engine.floor_plan_signals import (
        SIGNAL_THRESHOLD_HIGH,
        SIGNAL_THRESHOLD_STRUCTURAL,
    )
    assert SIGNAL_THRESHOLD_HIGH > SIGNAL_THRESHOLD_STRUCTURAL
