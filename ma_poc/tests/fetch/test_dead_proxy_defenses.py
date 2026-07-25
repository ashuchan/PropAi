"""Dead-proxy defence-in-depth + rescue-path guards (RCA 2026-07-25).

A single black-holing IP in ``PROXY_POOL_URLS`` silently took down an entire 5k
run: the local render path produced **0 usable bodies out of 10,677 fetch
attempts** (vs 54.7% on the 2026-07-19 reference), every "success" came from the
static-HTML fallback, and 336 previously-successful properties were stamped
FAILED_UNREACHABLE. Four independent defects made that possible, each pinned
here:

1. a DISABLED proxy tier still attached a pool proxy to every fetch;
2. the pool could never evict a dead proxy — ``mark_failure`` was reachable only
   inside the ``rotate_identity`` branch, which TRANSIENT never sets, so health
   stayed pinned at 1.0 forever;
3. a proxied RENDER returning TRANSIENT with an empty body never retried direct;
4. the residential rescue rung kept dialling a broken tunnel (134x CONNECT 502),
   burning ~20s of budget per property, with no circuit breaker.

Plus the verdict layer discarding units that extraction had already produced.
"""

from __future__ import annotations

from typing import Any

import pytest

from ma_poc.fetch.proxy_pool import ProxyPool

# ── 1 + 2: the pool must be able to quarantine a dead proxy ─────────────────

def test_pool_quarantines_a_proxy_after_repeated_failures() -> None:
    """health decays below the 0.25 quarantine floor, so pick() stops choosing it."""
    dead = "http://10.0.0.1:10080"
    alive = "http://10.0.0.2:10080"
    pool = ProxyPool([dead, alive])

    # Decay is 0.25/failure from 1.0 and pick() keeps `health >= 0.25`, so the
    # 3rd failure lands exactly ON the threshold and the 4th clears it. Pinned
    # deliberately: the contract is "quarantine below 0.25", and the number of
    # failures needed is what decides how long a dead proxy keeps taking traffic.
    for _ in range(4):
        pool.mark_failure(dead, "TRANSIENT")

    snap = {str(p["url"]): p for p in pool.health_snapshot()}
    dead_health = next(
        float(v["health"]) for k, v in snap.items() if "10.0.0.1" in k  # type: ignore[arg-type]
    )
    assert dead_health < 0.25, f"dead proxy still at health {dead_health}"

    # Every pick now avoids it (sticky_key varied so we exercise the weighting).
    picks = {pool.pick(sticky_key=f"p{i}") for i in range(25)}
    assert not any(p and "10.0.0.1" in p for p in picks), (
        "quarantined proxy is still being selected"
    )


def test_single_dead_proxy_pool_degrades_to_none_not_to_a_dead_proxy() -> None:
    """With only a dead proxy configured, pick() must return None, not the corpse.

    Returning the dead proxy is what turned one bad IP into a run-wide outage:
    every render kept being routed through a black hole.
    """
    dead = "http://10.0.0.1:10080"
    pool = ProxyPool([dead])
    for _ in range(4):
        pool.mark_failure(dead, "TRANSIENT")
    assert pool.pick(sticky_key="prop1") is None


# ── 3: PROBE_PROXY_URL circuit breaker ──────────────────────────────────────

def test_probe_proxy_circuit_opens_on_repeated_tunnel_failures(monkeypatch) -> None:
    from ma_poc.fetch import fetcher as F

    F._probe_proxy_reset_circuit()
    monkeypatch.setenv("PROBE_PROXY_CIRCUIT_FAILS", "3")
    monkeypatch.setenv("PROBE_PROXY_CIRCUIT_COOLDOWN_S", "60")

    assert F._probe_proxy_circuit_open() is False
    for _ in range(2):
        F._probe_proxy_note_failure("CONNECT tunnel failed, response 502")
    assert F._probe_proxy_circuit_open() is False, "opened too early"
    F._probe_proxy_note_failure("CONNECT tunnel failed, response 502")
    assert F._probe_proxy_circuit_open() is True, "breaker never opened"

    F._probe_proxy_reset_circuit()
    assert F._probe_proxy_circuit_open() is False


def test_probe_proxy_circuit_ignores_target_site_errors(monkeypatch) -> None:
    """A 403/404 from the TARGET is the proxy working — it must not trip."""
    from ma_poc.fetch import fetcher as F

    F._probe_proxy_reset_circuit()
    monkeypatch.setenv("PROBE_PROXY_CIRCUIT_FAILS", "2")
    for _ in range(6):
        F._probe_proxy_note_failure("HTTP 403 Forbidden from origin")
        F._probe_proxy_note_failure("read timed out waiting for body")
    assert F._probe_proxy_circuit_open() is False
    F._probe_proxy_reset_circuit()


def test_probe_proxy_circuit_env_is_fault_tolerant(monkeypatch) -> None:
    from ma_poc.fetch import fetcher as F

    monkeypatch.setenv("PROBE_PROXY_CIRCUIT_FAILS", "not-a-number")
    monkeypatch.setenv("PROBE_PROXY_CIRCUIT_COOLDOWN_S", "junk")
    assert F._probe_proxy_threshold() == 8
    assert F._probe_proxy_cooldown_s() == 600.0


# ── 4: a failed entry fetch must not veto extracted units ───────────────────

class _Extract:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records


@pytest.mark.parametrize("outcome", ["TRANSIENT", "BOT_BLOCKED", "RATE_LIMITED"])
def test_failed_fetch_does_not_discard_extracted_units(outcome: str) -> None:
    """The salvage/link-hop/plan-text paths can produce units after a failed
    entry fetch. Extraction succeeding is stronger evidence than the fetch
    failing — measured cost of the old behaviour: 10 properties / 68 units /
    63 rents silently dropped in one run."""
    from ma_poc.reporting import verdict as V

    units = [{"unit_number": "101", "market_rent_low": 1500}]
    res = V.compute(fetch_outcome=outcome, extract_result=_Extract(units))
    assert res.verdict != V.Verdict.FAILED_UNREACHABLE, (
        f"{outcome} with {len(units)} extracted units was still stamped "
        f"FAILED_UNREACHABLE (reason={res.reason!r})"
    )


def test_failed_fetch_with_no_units_still_unreachable() -> None:
    """The guard must stay dispositive when nothing was extracted."""
    from ma_poc.reporting import verdict as V

    for er in (None, _Extract([]), {"records": []}, {"units": []}):
        res = V.compute(fetch_outcome="TRANSIENT", extract_result=er)
        assert res.verdict == V.Verdict.FAILED_UNREACHABLE
        assert "TRANSIENT" in res.reason


def test_dead_url_still_terminal_even_with_units() -> None:
    """DEAD_URL is checked BEFORE the new guard and stays terminal."""
    from ma_poc.reporting import verdict as V

    res = V.compute(
        fetch_outcome="DEAD_URL",
        extract_result=_Extract([{"unit_number": "1", "market_rent_low": 900}]),
    )
    assert res.verdict == V.Verdict.DEAD_URL


def test_units_helper_tolerates_shapes() -> None:
    from ma_poc.reporting.verdict import _has_any_extracted_units as h

    assert h(None) is False
    assert h(_Extract([])) is False
    assert h(_Extract([{"a": 1}])) is True
    assert h({"records": [{"a": 1}]}) is True
    assert h({"units": [{"a": 1}]}) is True
    assert h({"records": None, "units": None}) is False
    assert h("garbage") is False  # never raises


# ── 5: the DC proxy rung must be opt-IN, not opt-out ────────────────────────

def _reload_flags(monkeypatch, **env: str):
    """Reload the flag module under a given environment (flags read at import)."""
    import importlib

    from ma_poc.config import feature_flags as ff

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(ff)


def test_dc_proxy_tier_is_off_by_default(monkeypatch) -> None:
    """Unset ENABLE_DC_PROXY_TIER must mean OFF.

    It defaulted ON until 2026-07-25, which made PROXY_POOL_URLS opt-OUT: a job
    that never mentioned the flag still had a DC proxy attached to every fetch,
    so one silently-dead pool entry produced 0 usable rendered bodies across a
    whole 5k run. An unused tier pointing at unverified infrastructure must not
    be something you have to remember to switch off.
    """
    monkeypatch.delenv("ENABLE_DC_PROXY_TIER", raising=False)
    ff = _reload_flags(monkeypatch, ENABLE_TIER_ESCALATION="true")
    try:
        assert ff.ENABLE_DC_PROXY_TIER is False
    finally:
        _reload_flags(monkeypatch)


def test_dc_proxy_tier_can_still_be_opted_in(monkeypatch) -> None:
    """The rung is kept, not deleted — an explicit opt-in must still work."""
    ff = _reload_flags(
        monkeypatch, ENABLE_TIER_ESCALATION="true", ENABLE_DC_PROXY_TIER="true"
    )
    try:
        assert ff.ENABLE_DC_PROXY_TIER is True
    finally:
        _reload_flags(monkeypatch)


def test_master_flag_still_gates_the_dc_rung(monkeypatch) -> None:
    """Master off ⇒ rung off, even with an explicit opt-in."""
    ff = _reload_flags(
        monkeypatch, ENABLE_TIER_ESCALATION="false", ENABLE_DC_PROXY_TIER="true"
    )
    try:
        assert ff.ENABLE_DC_PROXY_TIER is False
    finally:
        _reload_flags(monkeypatch)
