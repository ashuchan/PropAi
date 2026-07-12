"""Web-Unlocker cost-minimization levers (2026-07-12).

Grounded in the production run 2026-05-27 analysis:
  * Unlocker fired ~14,650×/run — 4.7 calls PER unlocked-property, because
    every subpage hop of a walled property's crawl escalates to Unlocker.
    A 325-property long tail (5-42 calls each) consumed 79% of all spend.
    Unlocker is billed PER REQUEST → this multiplier is the cost.
  * RESIDENTIAL was 100% wasted upstream of Unlocker: 964/964 residential
    escalations fell through to Unlocker (residential recovers 0, Unlocker 91%).

Lever A: per-property Unlocker call cap (WEB_UNLOCKER_MAX_CALLS_PER_PROPERTY).
Lever C: drop RESIDENTIAL from the ladder when Unlocker is on
         (SKIP_RESIDENTIAL_WHEN_UNLOCKER).
"""
from __future__ import annotations

import importlib

import pytest

# ── Lever A: per-property Unlocker cap ──────────────────────────────────────


@pytest.fixture
def unlocker_mod(monkeypatch):
    monkeypatch.setenv("WEB_UNLOCKER_MAX_CALLS_PER_PROPERTY", "3")
    from ma_poc.fetch.providers import unlocker as U
    importlib.reload(U)
    U.reset_unlocker_property_counts()
    yield U
    U.reset_unlocker_property_counts()


def test_cap_reserves_up_to_limit_then_denies(unlocker_mod):
    U = unlocker_mod
    got = [U._wu_try_reserve_property("P1") for _ in range(5)]
    assert got == [True, True, True, False, False]
    assert U.unlocker_property_call_count("P1") == 3


def test_cap_is_per_property_independent(unlocker_mod):
    U = unlocker_mod
    for _ in range(3):
        assert U._wu_try_reserve_property("A")
    # A exhausted, B is fresh
    assert U._wu_try_reserve_property("A") is False
    assert U._wu_try_reserve_property("B") is True


def test_empty_property_id_never_capped(unlocker_mod):
    U = unlocker_mod
    assert all(U._wu_try_reserve_property("") for _ in range(10))


def test_no_cap_when_env_unset(monkeypatch):
    monkeypatch.delenv("WEB_UNLOCKER_MAX_CALLS_PER_PROPERTY", raising=False)
    from ma_poc.fetch.providers import unlocker as U
    importlib.reload(U)
    U.reset_unlocker_property_counts()
    assert all(U._wu_try_reserve_property("C") for _ in range(20))


def test_invalid_and_zero_cap_disable_the_gate(monkeypatch):
    from ma_poc.fetch.providers import unlocker as U
    for val in ("0", "-1", "abc"):
        monkeypatch.setenv("WEB_UNLOCKER_MAX_CALLS_PER_PROPERTY", val)
        importlib.reload(U)
        U.reset_unlocker_property_counts()
        assert all(U._wu_try_reserve_property("Z") for _ in range(6)), val


@pytest.mark.asyncio
async def test_fetch_returns_bot_blocked_when_capped(monkeypatch):
    """Over-cap fetch() returns a synthetic BOT_BLOCKED without calling BD."""
    monkeypatch.setenv("WEB_UNLOCKER_MAX_CALLS_PER_PROPERTY", "1")
    from types import SimpleNamespace

    from ma_poc.fetch.contracts import FetchOutcome, RenderMode
    from ma_poc.fetch.providers import unlocker as U
    importlib.reload(U)
    U.reset_unlocker_property_counts()

    # sentinel: if BD is called, the test fails loudly
    async def _boom(*a, **k):
        raise AssertionError("BrightData must NOT be called when capped")
    monkeypatch.setattr(U, "_api_attempt", _boom)

    prov = U.UnlockerProvider.__new__(U.UnlockerProvider)
    prov._mode = "api"
    prov._proxy_url = None
    task = SimpleNamespace(
        property_id="Q", url="https://x.test/", render_mode=RenderMode.GET,
        budget_ms=30000, etag=None, last_modified=None,
    )
    # 1st call reserves + would hit BD (stubbed OK); 2nd is capped.
    async def _ok(*a, **k):
        return _mk_ok(U)
    monkeypatch.setattr(U, "_api_attempt", _ok)
    r1 = await prov.fetch(task, None)
    assert r1.outcome == FetchOutcome.OK
    monkeypatch.setattr(U, "_api_attempt", _boom)
    r2 = await prov.fetch(task, None)
    assert r2.outcome == FetchOutcome.BOT_BLOCKED
    assert r2.error_signature == "unlocker_property_cap"


def _mk_ok(U):
    from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
    return FetchResult(
        url="https://x.test/", outcome=FetchOutcome.OK, status=200,
        body=b"<html>ok</html>", headers={}, render_mode=RenderMode.GET,
        final_url="https://x.test/", attempts=1, elapsed_ms=1,
        error_signature=None, proxy_used="***unlocker***",
    )


# ── Lever C: drop residential when unlocker is on ───────────────────────────


def _ladder(monkeypatch, **env):
    for k in (
        "ENABLE_TIER_ESCALATION", "ENABLE_RESIDENTIAL_TIER",
        "ENABLE_UNLOCKER_TIER", "ENABLE_DC_PROXY_TIER",
        "SKIP_RESIDENTIAL_WHEN_UNLOCKER", "ENABLE_FLARESOLVERR_TIER",
    ):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import ma_poc.config.feature_flags as ff
    importlib.reload(ff)
    import ma_poc.fetch.tier_escalator as te
    importlib.reload(te)
    from ma_poc.models.fetch_tier import FetchTier
    return [t.name for t in te._build_ladder(FetchTier.DIRECT)]


def test_residential_dropped_when_unlocker_on_by_default(monkeypatch):
    ladder = _ladder(
        monkeypatch, ENABLE_TIER_ESCALATION="true",
        ENABLE_RESIDENTIAL_TIER="true", ENABLE_UNLOCKER_TIER="true",
        ENABLE_DC_PROXY_TIER="false",
    )
    assert ladder == ["DIRECT", "UNLOCKER"]
    assert "RESIDENTIAL" not in ladder


def test_residential_kept_when_skip_disabled(monkeypatch):
    ladder = _ladder(
        monkeypatch, ENABLE_TIER_ESCALATION="true",
        ENABLE_RESIDENTIAL_TIER="true", ENABLE_UNLOCKER_TIER="true",
        ENABLE_DC_PROXY_TIER="false", SKIP_RESIDENTIAL_WHEN_UNLOCKER="false",
    )
    assert ladder == ["DIRECT", "RESIDENTIAL", "UNLOCKER"]


def test_residential_kept_when_unlocker_off(monkeypatch):
    # No unlocker to fall through to → residential stays available.
    ladder = _ladder(
        monkeypatch, ENABLE_TIER_ESCALATION="true",
        ENABLE_RESIDENTIAL_TIER="true", ENABLE_UNLOCKER_TIER="false",
        ENABLE_DC_PROXY_TIER="false",
    )
    assert ladder == ["DIRECT", "RESIDENTIAL"]


def test_dc_proxy_still_precedes_unlocker(monkeypatch):
    # DC (cheap) stays; only the wasted residential is dropped.
    ladder = _ladder(
        monkeypatch, ENABLE_TIER_ESCALATION="true",
        ENABLE_RESIDENTIAL_TIER="true", ENABLE_UNLOCKER_TIER="true",
        ENABLE_DC_PROXY_TIER="true",
    )
    assert ladder == ["DIRECT", "DC_PROXY", "UNLOCKER"]
