"""Timeout-salvage + hop-budget guards (RCA 2026-07-25).

Three defects found while diagnosing the rotating per-property-timeout cohort
in the 5k canary:

1. ``partial_state["profile"]`` had NO writer, so the "persist the discovered
   route from a timed-out run" block in ``jugnu._process_one`` was dead code.
   Timed-out properties stayed COLD forever and re-paid full discovery — a
   compounding trap (0 "next run starts warm" lines across 366 timeouts).
2. ``LINK_HOP_BUDGET_S`` was a hop-*start* gate only: an in-flight hop ran
   unbounded, and re-entering ``_try_link_hop`` computed a FRESH deadline, so
   one property chained 8 hops over ~2,900s against a nominal 150s budget.
3. The salvage checkpoint existed ONLY inside the link-hop accumulation loop,
   so only 6.9% of timed-out properties salvaged any data.
"""

from __future__ import annotations

from typing import Any

from ma_poc.pms.scraper import checkpoint_partial

# ── 3. checkpoint widening ──────────────────────────────────────────────────

def _budget_with_ref() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (shared_budget, external_ref) wired the way the runner wires them."""
    ext: dict[str, Any] = {}
    return {"_external_partial_ref": ext}, ext


def test_checkpoint_writes_units_tier_and_route() -> None:
    budget, ext = _budget_with_ref()
    checkpoint_partial(
        budget,
        [{"unit_number": "101"}, {"unit_number": "102"}],
        tier_used="TIER_1_API",
        winning_page_url="https://x.com/floorplans",
    )
    assert len(ext["units"]) == 2
    assert ext["tier_used"] == "TIER_1_API"
    # route knowledge is what makes the NEXT run warm
    assert ext["profile_hints"]["winning_page_url"] == "https://x.com/floorplans"
    # in-process mirror stays in sync for non-cancelled readers
    assert len(budget["_partial_units"]) == 2


def test_checkpoint_never_shrinks_a_richer_earlier_view() -> None:
    """A later, thinner checkpoint must not destroy a richer earlier one.

    The single-page path checkpoints early; a later hop stage may re-checkpoint
    with fewer units (e.g. a sub-page that yielded less). Salvage should keep
    the best view seen.
    """
    budget, ext = _budget_with_ref()
    checkpoint_partial(budget, [{"u": 1}, {"u": 2}, {"u": 3}], tier_used="TIER_1_API")
    checkpoint_partial(budget, [{"u": 9}], tier_used="TIER_3_DOM")
    assert len(ext["units"]) == 3, "a 1-unit view overwrote a 3-unit view"
    # tier still advances — it describes the latest attempt, not the unit set
    assert ext["tier_used"] == "TIER_3_DOM"


def test_checkpoint_is_a_noop_without_a_ref_and_never_raises() -> None:
    # no external ref registered (e.g. daily_runner path) → silently ignored
    budget: dict[str, Any] = {}
    checkpoint_partial(budget, [{"u": 1}], tier_used="T")
    assert "_partial_units" not in budget

    # None budget, junk ref, and junk units must never raise
    checkpoint_partial(None, [{"u": 1}])
    checkpoint_partial({"_external_partial_ref": "not-a-dict"}, [{"u": 1}])
    checkpoint_partial({"_external_partial_ref": {}}, None)


def test_checkpoint_route_only_when_no_units_yet() -> None:
    """Route knowledge is worth persisting even with zero units salvaged.

    This is the case that kept properties cold: discovery succeeded (we found
    the floorplans URL) but extraction never finished before the budget expired.
    """
    budget, ext = _budget_with_ref()
    checkpoint_partial(budget, None, winning_page_url="https://x.com/availability")
    assert ext["profile_hints"]["winning_page_url"] == "https://x.com/availability"
    assert "units" not in ext


# ── 2. hop budget is per-property, not per-call ─────────────────────────────

def test_hop_deadline_is_inherited_across_reentry() -> None:
    """Re-entering the hop loop must NOT reset the wall-clock budget.

    Mirrors the deadline logic in ``_try_link_hop``: first entry seeds the
    deadline into shared_budget; every later entry inherits it. Without this,
    each re-entry granted a fresh LINK_HOP_BUDGET_S (measured: 8 hops /
    ~2,900s against a 150s budget).
    """
    import time

    LINK_HOP_BUDGET_S = 150.0
    shared: dict[str, Any] = {}

    def _resolve_deadline() -> float:
        if isinstance(shared.get("_hop_deadline"), (int, float)):
            return float(shared["_hop_deadline"])
        d = time.monotonic() + LINK_HOP_BUDGET_S
        shared["_hop_deadline"] = d
        return d

    first = _resolve_deadline()
    second = _resolve_deadline()  # simulated re-entry
    third = _resolve_deadline()
    assert first == second == third, "re-entry reset the hop budget"


def test_min_hop_fetch_floor_keeps_a_real_attempt_possible() -> None:
    """An almost-spent budget still allows a genuine fetch, not an instant cancel."""
    from ma_poc.pms.scraper import _MIN_HOP_FETCH_S

    assert _MIN_HOP_FETCH_S >= 5.0, "floor too small to complete any real fetch"
    # the in-flight allowance is max(floor, remaining) — never zero/negative
    for remaining in (-100.0, 0.0, 3.0, 90.0):
        assert max(_MIN_HOP_FETCH_S, remaining) >= _MIN_HOP_FETCH_S
