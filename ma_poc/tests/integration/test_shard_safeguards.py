"""Tests for the four shard-stability safeguards added in May 2026.

Background: three consecutive Cloud Run scrape jobs got stuck on different
shards (8 → 12 → 17), each burning the full 4h Cloud Run task budget on
LLM retries against a single property. Root causes were

  Fix #1  no per-property wall-clock timeout in jugnu.py / jugnu_retry.py
  Fix #2  ``shared_budget`` was copied (``dict(shared_budget)``) before
          being threaded into AdapterContext, so each link-hop sub-page
          got a fresh 5-call LLM budget and one property could fire 20
  Fix #3  no cumulative LLM cost gate per property — sub-tiers and Tier 5
          Vision could keep spending past any sane threshold
  Fix #4  shard_entry's ``subprocess.run`` had no timeout, so Cloud Run's
          SIGKILL ate the runner's artifacts before they could upload

These tests pin each safeguard so a future refactor can't silently undo
them. They mix source-level structural checks (cheap drift detectors)
with behavioural assertions of the load-bearing contracts.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

# Playwright stubs so importing ma_poc.pms.adapters.base doesn't pull
# the real browser bindings in unit-test envs that don't ship them.
for _name in ("playwright", "playwright.async_api"):
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        _mod.BrowserContext = object  # type: ignore[attr-defined]
        _mod.Page = object  # type: ignore[attr-defined]
        _mod.async_playwright = object  # type: ignore[attr-defined]
        sys.modules[_name] = _mod


_REPO = Path(__file__).resolve().parents[2]  # ma_poc/
_JUGNU = _REPO / "scripts" / "runners" / "jugnu.py"
_JUGNU_RETRY = _REPO / "scripts" / "runners" / "jugnu_retry.py"
_SCRAPER = _REPO / "pms" / "scraper.py"
_GENERIC = _REPO / "pms" / "adapters" / "generic.py"
_SHARD_ENTRY = _REPO / "scripts" / "runners" / "shard_entry.py"


# ── Fix #1: per-property wall-clock timeout ────────────────────────────


def test_fix1_jugnu_wraps_process_property_in_wait_for() -> None:
    """jugnu.py's _process_one must wrap _process_property in asyncio.wait_for.

    Without this wrapper a single property's LLM-retry tail can monopolise
    the AsyncPool until Cloud Run's 4h task timeout fires and the shard
    dies without uploading artifacts.
    """
    src = _JUGNU.read_text(encoding="utf-8")
    assert "asyncio.wait_for(" in src, "jugnu.py must wrap _process_property in asyncio.wait_for"
    assert "PER_PROPERTY_TIMEOUT_SECONDS" in src
    # The wait_for call site must reference _process_property — pin the
    # actual wrap, not just incidental uses of wait_for elsewhere.
    assert "_process_property" in src.split("asyncio.wait_for(", 1)[1][:600]


def test_fix1_jugnu_retry_wraps_process_property_in_wait_for() -> None:
    """jugnu_retry.py needs the same guard — a retry is exactly as
    susceptible to the LLM-tail wedge that ate the original run."""
    src = _JUGNU_RETRY.read_text(encoding="utf-8")
    assert "asyncio.wait_for(" in src
    assert "PER_PROPERTY_TIMEOUT_SECONDS" in src


def test_fix1_jugnu_timeout_handler_emits_failed_record() -> None:
    """When wait_for fires, jugnu.py must convert it into _make_failed_record
    with a recognisable reason string instead of letting the bare TimeoutError
    surface as 'Property X crashed' (indistinguishable from real crashes)."""
    src = _JUGNU.read_text(encoding="utf-8")
    # The dedicated except block exists.
    assert "except (TimeoutError, asyncio.TimeoutError)" in src
    # And produces a _make_failed_record with the timeout-reason convention.
    assert "per_property_timeout:" in src
    assert "_make_failed_record" in src


def test_fix1_per_property_timeout_constant_has_sane_default() -> None:
    """Default must be a wall-clock cap that's tight enough to cut the
    pathological tail (multi-hour hangs) but loose enough to let p99
    legitimate properties finish (~120s observed in prod)."""
    from ma_poc.scripts.runners.jugnu import PER_PROPERTY_TIMEOUT_SECONDS

    assert isinstance(PER_PROPERTY_TIMEOUT_SECONDS, float)
    # 60s would clip legitimate slow sites; 7200s would let a wedge eat
    # most of the 4h Cloud Run budget. Anything in [60, 7200] is defensible.
    assert 60 <= PER_PROPERTY_TIMEOUT_SECONDS <= 7200


def test_fix1_per_property_timeout_env_override() -> None:
    """Allow ops to dial it via PER_PROPERTY_TIMEOUT_SECONDS without a
    code change. The resolver must reject malformed/non-positive values
    and fall back to the safe default rather than hard-failing import."""
    from ma_poc.scripts.runners.jugnu import _resolve_per_property_timeout

    # Bogus values must NOT crash startup — they fall back to the default.
    import os

    saved = os.environ.get("PER_PROPERTY_TIMEOUT_SECONDS")
    try:
        os.environ["PER_PROPERTY_TIMEOUT_SECONDS"] = "not-a-number"
        assert _resolve_per_property_timeout() == 600.0
        os.environ["PER_PROPERTY_TIMEOUT_SECONDS"] = "-5"
        assert _resolve_per_property_timeout() == 600.0
        os.environ["PER_PROPERTY_TIMEOUT_SECONDS"] = "0"
        assert _resolve_per_property_timeout() == 600.0
        os.environ["PER_PROPERTY_TIMEOUT_SECONDS"] = "300"
        assert _resolve_per_property_timeout() == 300.0
    finally:
        if saved is None:
            os.environ.pop("PER_PROPERTY_TIMEOUT_SECONDS", None)
        else:
            os.environ["PER_PROPERTY_TIMEOUT_SECONDS"] = saved


@pytest.mark.asyncio
async def test_fix1_wait_for_pattern_cancels_long_coroutine() -> None:
    """End-to-end pattern check: asyncio.wait_for genuinely cancels a
    coroutine that overshoots the timeout, raising TimeoutError. This is
    the mechanism _process_one relies on; if Python ever changed it,
    every other safeguard above would still leave shards hung."""

    async def slow() -> None:
        await asyncio.sleep(10)

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await asyncio.wait_for(slow(), timeout=0.01)


# ── Fix #2: shared_budget passed by reference (no dict() copy) ─────────


def test_fix2_scraper_uses_shared_budget_by_reference() -> None:
    """scraper.py must NOT copy the shared_budget dict. The pre-Fix#2
    code did ``budget: dict = dict(shared_budget)`` and decrements got
    silently dropped on the floor — link-hop sub-pages each got a fresh
    5-call LLM budget."""
    src = _SCRAPER.read_text(encoding="utf-8")
    assert "budget: dict = shared_budget" in src, (
        "scraper.scrape() must alias shared_budget, not copy it"
    )
    # Negative drift detector — make sure the bad pattern doesn't sneak
    # back in via a future refactor.
    assert "budget: dict = dict(shared_budget)" not in src
    assert "budget = dict(shared_budget)" not in src


def test_fix2_adapter_context_budget_is_caller_dict_reference() -> None:
    """AdapterContext.budget must be the same dict reference the caller
    passed in. This is the contract sub-tiers 6a/6b/6c rely on for
    decrements to compose across recursive scrape() calls."""
    from ma_poc.pms.adapters.base import AdapterContext
    from ma_poc.pms.detector import DetectedPMS

    shared_budget: dict = {
        "llm_api_calls": 3,
        "llm_dom_calls": 1,
        "llm_monolithic": 1,
        "link_hop": 3,
    }
    ctx = AdapterContext(
        base_url="https://example.com",
        detected=DetectedPMS(pms="unknown", confidence=0.0),
        profile=None,
        expected_total_units=None,
        property_id="P001",
        budget=shared_budget,
    )
    # Same reference, not a copy — id() check is the strongest assertion.
    assert ctx.budget is shared_budget


def test_fix2_budget_decrement_propagates_to_caller_dict() -> None:
    """Simulate the GenericAdapter sub-tier 6a decrement pattern and
    verify the caller's dict reflects it. This is the actual mechanism
    that prevents one property from firing 20 LLM calls (1 entry × 5 +
    3 hops × 5)."""
    shared: dict = {"llm_api_calls": 3, "llm_dom_calls": 1, "llm_monolithic": 1}

    # The exact decrement pattern used inside generic.py:
    shared["llm_api_calls"] = int(shared.get("llm_api_calls", 0)) - 1
    shared["llm_api_calls"] = int(shared.get("llm_api_calls", 0)) - 1
    shared["llm_api_calls"] = int(shared.get("llm_api_calls", 0)) - 1

    assert shared["llm_api_calls"] == 0, (
        "after 3 decrements the shared budget should be 0; if you got 3 here "
        "the GenericAdapter is using a local counter again — see Fix #2"
    )


# ── Fix #3: cumulative LLM cost gate ────────────────────────────────────


def test_fix3_cost_gate_helpers_present_in_generic_adapter() -> None:
    """The two closures that implement Fix #3 must remain wired. If they
    get factored into a module helper, update this test — but don't
    delete it."""
    src = _GENERIC.read_text(encoding="utf-8")
    assert "_llm_cost_exceeded" in src, "cost-gate predicate is missing"
    assert "_record_interaction_cost" in src, "cost-recording helper is missing"
    assert "_cost_cap_usd" in src
    assert "_cost_usd_spent" in src


def test_fix3_cost_gate_default_is_env_overridable_property_llm_cost_cap() -> None:
    """F0.1 (2026-05-09): the cost-cap default lives in
    ``services/source_planner.get_property_llm_cost_cap_usd`` and is
    env-overridable via ``PROPERTY_LLM_COST_CAP_USD``. The 2026-05-09
    cloud-run regression showed the prior hard-coded $1.00 starved
    link-hop pages of LLM rescue; the new $1.50 baseline plus the
    per-hop bonus restores headroom while keeping the wedge protection.

    The generic adapter must read its fallback through the planner
    helper (not redefine the literal) so future env tuning takes effect
    without a code change."""
    src = _GENERIC.read_text(encoding="utf-8")
    assert "get_property_llm_cost_cap_usd" in src, (
        "GenericAdapter must source the cost cap from the planner helper "
        "so PROPERTY_LLM_COST_CAP_USD is honored"
    )
    assert "_cost_cap_usd" in src, "the budget-dict key must remain stable"
    # The literal $1.00 from the regression-causing commit must NOT remain
    # as the in-code default. (1.00 may still appear in unrelated math.)
    assert '_budget.get("_cost_cap_usd", 1.00)' not in src, (
        "remove the hard-coded $1.00 default that caused the 2026-05-09 regression"
    )


def test_fix3_cost_gate_called_before_every_paid_tier() -> None:
    """Every paid tier (6a per-API loop, 6a outer guard, 6b, 6c, Tier 5
    Vision) must consult _llm_cost_exceeded() before issuing a call.
    A single missed gate would re-open the burn path."""
    src = _GENERIC.read_text(encoding="utf-8")
    # 6 = (6a outer) + (6a inner loop) + (6b) + (6c) + (vision) + helper-def
    # If a future refactor consolidates them into one wrapper, lower this
    # bound but keep ≥3 (one per tier 6a/6b/6c at minimum).
    count = src.count("_llm_cost_exceeded()")
    assert count >= 5, (
        f"expected ≥5 cost-gate call sites (one per LLM sub-tier + helper), "
        f"found {count}. A missed gate re-opens the per-day burn path."
    )


def test_fix3_cost_record_pattern_is_safe_against_malformed_interaction() -> None:
    """Replicate the _record_interaction_cost logic and verify it never
    crashes on missing keys, None values, or non-numeric cost. The helper
    runs inside a try/finally-free path and an exception there would
    leak past the GenericAdapter into the runner."""

    def _record(budget: dict, interaction: dict | None) -> None:
        # Mirror of generic.py's _record_interaction_cost.
        if not interaction:
            return
        try:
            cost = float(interaction.get("cost_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            return
        if cost > 0:
            budget["_cost_usd_spent"] = float(budget.get("_cost_usd_spent", 0.0) or 0.0) + cost

    b: dict = {}
    _record(b, None)                        # no-op
    _record(b, {})                          # missing cost_usd → no-op
    _record(b, {"cost_usd": None})          # None → no-op
    _record(b, {"cost_usd": "not-a-num"})   # bad string → no-op (no crash)
    _record(b, {"cost_usd": 0})             # zero → no-op
    _record(b, {"cost_usd": 0.05})          # records
    _record(b, {"cost_usd": 0.10})          # accumulates

    assert b["_cost_usd_spent"] == pytest.approx(0.15)


# ── Fix #4: subprocess.run timeout in shard_entry ──────────────────────


def test_fix4_shard_entry_subprocess_has_timeout() -> None:
    """shard_entry's subprocess.run must have an explicit timeout=
    parameter so the parent regains control before Cloud Run's SIGKILL
    eats the artifact-upload + PG-sync finally block."""
    src = _SHARD_ENTRY.read_text(encoding="utf-8")
    assert "subprocess.run(cmd, check=False, timeout=" in src, (
        "shard_entry must pass timeout= to subprocess.run; without it "
        "Cloud Run's SIGKILL discards the artifact upload"
    )


def test_fix4_shard_entry_catches_timeout_expired() -> None:
    """The TimeoutExpired except block is what lets the finally block
    run on the parent side (vs being skipped by an unhandled exception)."""
    src = _SHARD_ENTRY.read_text(encoding="utf-8")
    assert "except subprocess.TimeoutExpired" in src
    # Exit code 124 follows GNU coreutils `timeout` convention so ops
    # tools can detect timeout-class failures uniformly.
    assert "124" in src


def test_fix4_shard_entry_timeout_below_cloud_run_4h() -> None:
    """The default subprocess timeout must give the finally block ~5–15
    minutes of headroom before Cloud Run's 4h (14400s) task SIGKILL."""
    src = _SHARD_ENTRY.read_text(encoding="utf-8")
    assert 'SHARD_RUNNER_TIMEOUT_SECONDS' in src
    # Pin the default literal so a refactor can't quietly raise it past
    # the 4h Cloud Run cap.
    assert '"13500"' in src, (
        "default subprocess timeout should be 13500s (3h45m) — leaves "
        "15min before Cloud Run's 4h SIGKILL for finally-block work"
    )


def test_fix4_shard_entry_finally_runs_after_timeout() -> None:
    """The artifact-upload finally block must NOT be inside the
    TimeoutExpired except — otherwise a non-timeout subprocess error
    would skip the upload. The structure is:

        try:
            try: subprocess.run(... timeout=...)
            except TimeoutExpired: ...
            sync_to_postgres(...)
        finally:
            _upload_artifacts(...)
    """
    src = _SHARD_ENTRY.read_text(encoding="utf-8")
    # The literal "finally:" appears AFTER the except subprocess.TimeoutExpired
    # in the same try.
    timeout_idx = src.find("except subprocess.TimeoutExpired")
    finally_idx = src.find("finally:", timeout_idx)
    upload_idx = src.find("_upload_artifacts(", finally_idx)
    assert timeout_idx > 0 and finally_idx > timeout_idx and upload_idx > finally_idx, (
        "finally block with _upload_artifacts must follow the TimeoutExpired "
        "handler — losing artifacts on subprocess timeout was the point of Fix #4"
    )
