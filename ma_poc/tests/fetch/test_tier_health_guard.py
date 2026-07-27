"""Dead-proxy blind spot #2: the escalator had no health tracking at all.

RCA 2026-07-27. ``ProxyPool`` quarantine works and is covered by
``test_dead_proxy_defenses.py``. But the *tier escalator* never used
``ProxyPool``: its providers build proxies from ``BrightDataProvider.get_config()``
and ``_make_provider()`` is called fresh per hop, so nothing accumulated health
and nothing could ever be quarantined on that path. One dead proxy therefore
produced 267 consecutive TRANSIENT fetches with 0 OK across two shards of the
2026-07-22-hb250 run, silently degrading all 4,982 properties.

This matters beyond the (now default-off) DC rung: ``ENABLE_RESIDENTIAL_TIER``
is true on all four production jobs, so BrightData residential rides the same
escalation machinery and inherited the same blind spot.

Covers both halves of the contract:
  * the pool stops *offering* a dead proxy (health decay → quarantine);
  * the escalator stops *using* a dead tier (run-level circuit breaker).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ma_poc.fetch import tier_health
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.fetch.proxy_pool import ProxyPool
from ma_poc.models.fetch_tier import FetchTier


@pytest.fixture(autouse=True)
def _clean_breaker(monkeypatch):
    """Isolate breaker state, and switch the guard ON.

    The module ships in SHADOW mode (counts + alarms, never reroutes), so tests
    that assert on *skipping* must opt in explicitly — exactly as a production
    job would. Shadow-mode behaviour is pinned separately below.
    """
    tier_health.reset()
    monkeypatch.setenv("ENABLE_TIER_HEALTH_GUARD", "true")
    yield
    tier_health.reset()


# ── Half 1: the pool stops offering a dead proxy ────────────────────────────


def test_pool_stops_offering_a_proxy_that_failed_n_times() -> None:
    """N consecutive failures drive health under 0.25 and pick() drops it."""
    dead = "http://user:pass@10.0.0.1:10080"
    alive = "http://user:pass@10.0.0.2:10080"
    pool = ProxyPool([dead, alive])

    for _ in range(4):  # 1.0 → .75 → .50 → .25 → quarantined
        pool.mark_failure(dead, "TRANSIENT")

    picks = {pool.pick(sticky_key=f"prop{i}") for i in range(40)}
    assert picks == {alive}, f"dead proxy still offered: {picks}"


def test_pool_with_only_a_dead_proxy_returns_none() -> None:
    """Better no proxy than a black hole — None lets callers fall back to direct."""
    dead = "http://10.0.0.1:10080"
    pool = ProxyPool([dead])
    for _ in range(4):
        pool.mark_failure(dead, "TRANSIENT")
    assert pool.pick(sticky_key="prop1") is None


# ── Half 2: the escalator stops using a dead tier ───────────────────────────


def _result(outcome: FetchOutcome, tier: FetchTier) -> FetchResult:
    return FetchResult(
        url="https://example.com",
        outcome=outcome,
        status=None,
        body=None,
        headers={},
        render_mode=RenderMode.GET,
        final_url="https://example.com",
        attempts=1,
        elapsed_ms=5,
        error_signature="Error:net::ERR_TUNNEL_CONNECTION_FAILED",
        fetch_tier_used=int(tier),
        fetch_tier_attempts=[int(tier)],
    )


def test_breaker_latches_a_tier_off_after_n_transport_failures(monkeypatch) -> None:
    monkeypatch.setenv("FETCH_TIER_DEAD_AFTER", "5")

    for _ in range(4):
        tier_health.note_result("RESIDENTIAL", FetchOutcome.TRANSIENT)
    assert not tier_health.is_tier_disabled("RESIDENTIAL"), "tripped too early"

    tier_health.note_result("RESIDENTIAL", FetchOutcome.TRANSIENT)
    assert tier_health.is_tier_disabled("RESIDENTIAL"), "breaker never tripped"


def test_a_success_resets_the_failure_streak(monkeypatch) -> None:
    """A dead tier is one that fails *consecutively* — intermittent is not dead."""
    monkeypatch.setenv("FETCH_TIER_DEAD_AFTER", "5")
    for _ in range(10):
        for _ in range(4):
            tier_health.note_result("DC_PROXY", FetchOutcome.TRANSIENT)
        tier_health.note_result("DC_PROXY", FetchOutcome.OK)
    assert not tier_health.is_tier_disabled("DC_PROXY")


def test_bot_blocked_never_trips_the_breaker(monkeypatch) -> None:
    """A block is the tier WORKING — the origin answered. Only transport counts.

    Without this, a run against a heavily-walled cohort would disable every
    paid tier and quietly cripple itself — the opposite failure to the one
    this guard exists to prevent.
    """
    monkeypatch.setenv("FETCH_TIER_DEAD_AFTER", "3")
    for _ in range(50):
        tier_health.note_result("RESIDENTIAL", FetchOutcome.BOT_BLOCKED)
        tier_health.note_result("RESIDENTIAL", FetchOutcome.DEAD_URL)
        tier_health.note_result("RESIDENTIAL", FetchOutcome.HARD_FAIL)
    assert not tier_health.is_tier_disabled("RESIDENTIAL")


def test_direct_is_never_disabled(monkeypatch) -> None:
    """DIRECT is unproxied and is the escalator's fallback floor."""
    monkeypatch.setenv("FETCH_TIER_DEAD_AFTER", "2")
    for _ in range(50):
        tier_health.note_result("DIRECT", FetchOutcome.TRANSIENT)
    assert not tier_health.is_tier_disabled("DIRECT")


def test_guard_is_off_by_default(monkeypatch) -> None:
    """Shipped default is SHADOW mode — routing is never changed unless opted in.

    Pinned because the incident this guard addresses was itself caused by a flag
    that defaulted ON and was therefore opt-OUT (ENABLE_DC_PROXY_TIER).
    """
    monkeypatch.delenv("ENABLE_TIER_HEALTH_GUARD", raising=False)
    assert tier_health.guard_enabled() is False

    monkeypatch.setenv("FETCH_TIER_DEAD_AFTER", "2")
    for _ in range(10):
        tier_health.note_result("RESIDENTIAL", FetchOutcome.TRANSIENT)
    assert not tier_health.is_tier_disabled("RESIDENTIAL")


def test_shadow_mode_still_raises_the_alarm(monkeypatch) -> None:
    """The whole point of shadow mode: detection without behaviour change.

    Jul-25 degraded silently for eight days. The alarm alone would have caught
    it on day one, so it must fire even with the skipping switched off.
    """
    monkeypatch.delenv("ENABLE_TIER_HEALTH_GUARD", raising=False)
    monkeypatch.setenv("FETCH_TIER_DEAD_AFTER", "3")
    seen: list[tuple] = []
    monkeypatch.setattr(
        tier_health, "emit", lambda kind, pid, **kw: seen.append((kind, pid, kw))
    )

    for _ in range(6):
        tier_health.note_result("RESIDENTIAL", FetchOutcome.TRANSIENT, property_id="p1")

    assert len(seen) == 1, "shadow mode swallowed the alarm"
    assert seen[0][0].value == "fetch.tier_disabled"
    assert not tier_health.is_tier_disabled("RESIDENTIAL"), "shadow mode rerouted"


def test_guard_can_be_switched_on(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_TIER_HEALTH_GUARD", "true")
    monkeypatch.setenv("FETCH_TIER_DEAD_AFTER", "2")
    for _ in range(10):
        tier_health.note_result("RESIDENTIAL", FetchOutcome.TRANSIENT)
    assert tier_health.is_tier_disabled("RESIDENTIAL")


def test_threshold_env_is_fault_tolerant(monkeypatch) -> None:
    monkeypatch.setenv("FETCH_TIER_DEAD_AFTER", "not-a-number")
    assert tier_health.dead_after() == 20


def test_trip_emits_one_loud_run_level_event(monkeypatch) -> None:
    """The alarm whose absence cost eight days of silent degradation."""
    monkeypatch.setenv("FETCH_TIER_DEAD_AFTER", "3")
    seen: list[tuple] = []
    monkeypatch.setattr(
        tier_health, "emit", lambda kind, pid, **kw: seen.append((kind, pid, kw))
    )

    for _ in range(9):
        tier_health.note_result(
            "RESIDENTIAL", FetchOutcome.TRANSIENT, property_id="p1",
            error_signature="Error:net::ERR_TUNNEL_CONNECTION_FAILED",
        )

    assert len(seen) == 1, f"expected exactly one alarm, got {len(seen)}"
    kind, _pid, payload = seen[0]
    assert kind.value == "fetch.tier_disabled"
    assert payload["tier"] == "RESIDENTIAL"
    assert payload["successes"] == 0
    assert payload["error_signature"] == "Error:net::ERR_TUNNEL_CONNECTION_FAILED"


@pytest.mark.asyncio
async def test_escalator_stops_using_a_dead_tier(monkeypatch) -> None:
    """End-to-end: the escalator must stop *calling* a tier the breaker killed.

    This is the assertion that would have failed before the fix — the escalator
    consulted no health state whatsoever, so a black-holing proxy was re-dialled
    for every property in the run.
    """
    monkeypatch.setenv("FETCH_TIER_DEAD_AFTER", "4")

    import ma_poc.fetch.tier_escalator as esc

    monkeypatch.setattr(esc, "ENABLE_TIER_ESCALATION", True)
    calls: list[str] = []

    class _DeadProxyProvider:
        """Simulates the 2026-07-25 proxy: every connect black-holes."""

        def __init__(self, tier: FetchTier) -> None:
            self._tier = tier

        async def fetch(self, task, profile):
            calls.append(self._tier.name)
            return _result(FetchOutcome.TRANSIENT, self._tier)

    monkeypatch.setattr(esc, "_build_ladder", lambda floor: [FetchTier.RESIDENTIAL])
    monkeypatch.setattr(esc, "_make_provider", _DeadProxyProvider)
    # Stub the DIRECT fallback the escalator reaches once the tier is dead.
    # Without this the test makes real network calls to example.com — the exact
    # live-network leak that makes "passing" suites lie.
    direct_calls = _stub_direct(monkeypatch)

    task = _task()
    profile = _profile()

    # Drive the breaker to its threshold through the real escalator.
    for _ in range(4):
        await esc.fetch_with_escalation(task, profile)
    assert tier_health.is_tier_disabled("RESIDENTIAL"), "breaker did not trip"

    calls_at_trip = len(calls)
    for _ in range(25):
        await esc.fetch_with_escalation(task, profile)

    assert len(calls) == calls_at_trip, (
        f"escalator kept dialling the dead tier: {len(calls) - calls_at_trip} "
        "further attempts after the breaker tripped"
    )
    # ...and the properties were still served, via the unguarded DIRECT floor.
    assert len(direct_calls) == 25


@pytest.mark.asyncio
async def test_escalator_falls_back_to_direct_when_all_tiers_dead(monkeypatch) -> None:
    """A tripped breaker must not strand the run with a synthetic LADDER_EMPTY."""
    monkeypatch.setenv("FETCH_TIER_DEAD_AFTER", "1")

    import ma_poc.fetch.tier_escalator as esc

    monkeypatch.setattr(esc, "ENABLE_TIER_ESCALATION", True)
    monkeypatch.setattr(esc, "_build_ladder", lambda floor: [FetchTier.RESIDENTIAL])

    class _Dead:
        def __init__(self, tier): ...
        async def fetch(self, task, profile):
            return _result(FetchOutcome.TRANSIENT, FetchTier.RESIDENTIAL)

    monkeypatch.setattr(esc, "_make_provider", _Dead)
    direct_calls = _stub_direct(monkeypatch)

    await esc.fetch_with_escalation(_task(), _profile())  # trips the breaker
    assert tier_health.is_tier_disabled("RESIDENTIAL")

    res = await esc.fetch_with_escalation(_task(), _profile())
    assert direct_calls, "did not fall back to DIRECT"
    assert res.outcome == FetchOutcome.OK


# ── helpers ─────────────────────────────────────────────────────────────────


def _stub_direct(monkeypatch) -> list[int]:
    """Replace DirectProvider with an in-memory stub; return its call log.

    The escalator does ``from ...providers.direct import DirectProvider`` at
    call time, so patching the module attribute is enough.
    """
    calls: list[int] = []

    class _Direct:
        async def fetch(self, task, profile):
            calls.append(1)
            return _result(FetchOutcome.OK, FetchTier.DIRECT)

    import ma_poc.fetch.providers.direct as direct_mod

    monkeypatch.setattr(direct_mod, "DirectProvider", _Direct)
    return calls


def _task():
    """MagicMock task — matches the fixture style in test_escalator_e5.py."""
    task = MagicMock()
    task.url = "https://example.com"
    task.property_id = "p1"
    task.render_mode = RenderMode.GET
    task.budget_ms = 10000
    return task


def _profile():
    profile = MagicMock()
    fp = MagicMock()
    fp.tier_floor = FetchTier.RESIDENTIAL
    fp.consecutive_successes_at_floor = 0
    fp.last_demotion_probe_at = None
    profile.fetch = fp
    return profile
