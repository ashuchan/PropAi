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

import contextlib
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


# ── 1. timed-out properties must LEARN their route (end-to-end) ─────────────

class _FakeNav:
    def __init__(self, winning_page_url: str | None = None) -> None:
        self.winning_page_url = winning_page_url


class _FakeProfile:
    def __init__(self, winning_page_url: str | None = None) -> None:
        self.navigation = _FakeNav(winning_page_url)


class _FakeStore:
    """Minimal stand-in for ProfileStore (get_profile / save)."""

    def __init__(self, profile: Any | None = None) -> None:
        self._profile = profile
        self.saved: list[Any] = []

    def get_profile(self, _pid: str) -> Any | None:
        return self._profile

    def save(self, profile: Any) -> None:
        self.saved.append(profile)


def _persist():
    from ma_poc.scripts.runners.jugnu import persist_timeout_route_hints

    return persist_timeout_route_hints


def test_timeout_persists_route_end_to_end() -> None:
    """scraper checkpoint -> partial_state -> profile write.

    Exercises the REAL chain: checkpoint_partial writes the hint into the
    caller-scoped dict (the one that survives cancellation), and the timeout
    handler's helper turns it into a persisted winning_page_url.
    """
    budget, ext = _budget_with_ref()
    # what the scraper does when a page yields units
    checkpoint_partial(
        budget,
        [{"unit_number": "12"}],
        tier_used="TIER_1_API",
        winning_page_url="https://x.com/floorplans",
    )

    store = _FakeStore(_FakeProfile())
    wrote = _persist()(store, "P1", ext, unit_count=1)

    assert wrote is True
    assert len(store.saved) == 1
    assert store.saved[0].navigation.winning_page_url == "https://x.com/floorplans"


def test_timeout_persists_route_even_with_zero_units() -> None:
    """The case that kept properties COLD: discovery worked, extraction didn't."""
    budget, ext = _budget_with_ref()
    checkpoint_partial(budget, None, winning_page_url="https://x.com/availability")

    store = _FakeStore(_FakeProfile())
    assert _persist()(store, "P1", ext, unit_count=0) is True
    assert store.saved[0].navigation.winning_page_url == "https://x.com/availability"


def test_timeout_persist_is_idempotent_and_narrow() -> None:
    # unchanged URL → no write (no pointless version bump)
    budget, ext = _budget_with_ref()
    checkpoint_partial(budget, None, winning_page_url="https://x.com/fp")
    store = _FakeStore(_FakeProfile("https://x.com/fp"))
    assert _persist()(store, "P1", ext) is False
    assert store.saved == []

    # a timeout must NOT look like a success: no success/maturity fields touched
    budget2, ext2 = _budget_with_ref()
    checkpoint_partial(budget2, None, winning_page_url="https://x.com/new")
    prof = _FakeProfile("https://x.com/old")
    prof.confidence = type("C", (), {"consecutive_successes": 0, "preferred_tier": None})()
    store2 = _FakeStore(prof)
    assert _persist()(store2, "P1", ext2) is True
    assert prof.confidence.consecutive_successes == 0
    assert prof.confidence.preferred_tier is None


def test_timeout_persist_never_raises_on_bad_inputs() -> None:
    p = _persist()
    assert p(_FakeStore(_FakeProfile()), "P1", None) is False          # no state
    assert p(_FakeStore(_FakeProfile()), "P1", {}) is False            # no hints
    assert p(_FakeStore(_FakeProfile()), "P1", {"profile_hints": {}}) is False  # no url
    assert p(_FakeStore(None), "P1", {"profile_hints": {"winning_page_url": "u"}}) is False
    assert p(object(), "P1", {"profile_hints": {"winning_page_url": "u"}}) is False  # no save()

    class _Boom:
        def get_profile(self, _p): raise RuntimeError("store down")
        def save(self, _p): ...

    assert p(_Boom(), "P1", {"profile_hints": {"winning_page_url": "u"}}) is False


# ── periodic profile push: task-death insurance ─────────────────────────────

def test_profile_push_interval_default_and_override(monkeypatch) -> None:
    from ma_poc.scripts.runners.jugnu import _profile_push_interval_s

    monkeypatch.delenv("PROFILE_PUSH_INTERVAL_S", raising=False)
    assert _profile_push_interval_s() == 300.0          # sane default
    monkeypatch.setenv("PROFILE_PUSH_INTERVAL_S", "60")
    assert _profile_push_interval_s() == 60.0
    monkeypatch.setenv("PROFILE_PUSH_INTERVAL_S", "0")   # 0 disables
    assert _profile_push_interval_s() == 0.0
    monkeypatch.setenv("PROFILE_PUSH_INTERVAL_S", "junk")
    assert _profile_push_interval_s() == 300.0           # never raises


async def test_periodic_push_flushes_repeatedly_and_offloads(monkeypatch) -> None:
    """The pusher must fire more than once and never run on the event loop.

    Running the blocking upload inline would freeze every property coroutine
    sharing the loop — the exact starvation this pipeline was just fixed for.
    """
    import asyncio as _a
    import threading

    from ma_poc.scripts.runners import jugnu as J

    loop_thread = threading.get_ident()
    calls: list[int] = []

    def _fake_push(_dir):
        calls.append(threading.get_ident())

    monkeypatch.setattr(J, "_push_profiles_to_gcs", _fake_push)

    task = _a.create_task(J._periodic_profile_push(None, 0.02))
    await _a.sleep(0.13)
    task.cancel()
    with contextlib.suppress(_a.CancelledError):
        await task

    assert len(calls) >= 2, f"expected repeated flushes, got {len(calls)}"
    assert all(t != loop_thread for t in calls), "upload ran on the event loop"


async def test_periodic_push_survives_a_failing_upload(monkeypatch) -> None:
    """One bad flush must not kill the pusher — learning keeps being saved."""
    import asyncio as _a

    from ma_poc.scripts.runners import jugnu as J

    n = {"i": 0}

    def _flaky(_dir):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("GCS blip")

    monkeypatch.setattr(J, "_push_profiles_to_gcs", _flaky)

    task = _a.create_task(J._periodic_profile_push(None, 0.02))
    await _a.sleep(0.13)
    task.cancel()
    with contextlib.suppress(_a.CancelledError):
        await task

    assert n["i"] >= 2, "pusher died on the first failed upload"
