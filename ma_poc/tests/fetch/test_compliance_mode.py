"""COMPLIANCE_MODE kill-switch (RealPage legal 2026-07-22).

Pins that COMPLIANCE_MODE=1 forces every challenge-solver OFF while KEEPING the
code: (1) the flag helpers; (2) the web_unlocker_get chokepoint no-ops even with
a key set (covers all adapter-internal unlocker paths); (3) the escalator ladder
drops UNLOCKER + FLARESOLVERR even when their tier flags are on.
"""

from __future__ import annotations

import importlib

import pytest


def test_flag_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    import ma_poc.config.feature_flags as ff

    monkeypatch.delenv("COMPLIANCE_MODE", raising=False)
    assert ff.compliance_mode() is False
    assert ff.web_unlocker_allowed() is True and ff.flaresolverr_allowed() is True
    monkeypatch.setenv("COMPLIANCE_MODE", "1")
    assert ff.compliance_mode() is True
    assert ff.web_unlocker_allowed() is False and ff.flaresolverr_allowed() is False


def test_web_unlocker_get_blocked_even_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from ma_poc.pms.adapters._probe import web_unlocker_get

    # A key IS set — without the compliance gate this would attempt a network
    # call; the gate must short-circuit to the empty shape first (no network).
    monkeypatch.setenv("WEB_UNLOCKER_KEY", "fake-key-should-never-be-used")
    monkeypatch.setenv("COMPLIANCE_MODE", "1")
    resp = web_unlocker_get("https://walled.example/floorplans")
    assert resp.status_code == 0 and resp.text == ""  # no-op, cascade falls through


def test_ladder_drops_solvers_under_compliance(monkeypatch: pytest.MonkeyPatch) -> None:
    from ma_poc.models.fetch_tier import FetchTier

    # Turn the solver tiers ON at import, then prove compliance overrides them.
    monkeypatch.setenv("ENABLE_TIER_ESCALATION", "true")
    monkeypatch.setenv("ENABLE_UNLOCKER_TIER", "true")
    monkeypatch.setenv("ENABLE_FLARESOLVERR_TIER", "true")
    import ma_poc.config.feature_flags as ff
    import ma_poc.fetch.tier_escalator as te
    importlib.reload(ff)
    importlib.reload(te)
    try:
        monkeypatch.delenv("COMPLIANCE_MODE", raising=False)
        ladder_off = te._build_ladder(0)
        assert FetchTier.UNLOCKER in ladder_off and FetchTier.FLARESOLVERR in ladder_off

        monkeypatch.setenv("COMPLIANCE_MODE", "1")
        ladder_on = te._build_ladder(0)
        assert FetchTier.UNLOCKER not in ladder_on
        assert FetchTier.FLARESOLVERR not in ladder_on
    finally:
        # restore clean import state for other tests
        monkeypatch.delenv("ENABLE_UNLOCKER_TIER", raising=False)
        monkeypatch.delenv("ENABLE_FLARESOLVERR_TIER", raising=False)
        importlib.reload(ff)
        importlib.reload(te)
