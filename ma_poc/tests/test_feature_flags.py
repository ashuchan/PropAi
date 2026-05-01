"""Tests for the feature-flag registry.

All flags are module-level Finals evaluated at import time.
Tests use monkeypatch.setenv() + importlib.reload() to re-evaluate them.
"""

from __future__ import annotations

import importlib

import pytest


def _reload_flags() -> object:
    """Reload the feature_flags module and return it."""
    import ma_poc.config.feature_flags as mod
    importlib.reload(mod)
    return mod


def test_master_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env vars set, ENABLE_TIER_ESCALATION must be False."""
    monkeypatch.delenv("ENABLE_TIER_ESCALATION", raising=False)
    mod = _reload_flags()
    assert mod.ENABLE_TIER_ESCALATION is False  # type: ignore[attr-defined]


def test_master_flag_on_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting ENABLE_TIER_ESCALATION=true makes the flag True."""
    monkeypatch.setenv("ENABLE_TIER_ESCALATION", "true")
    mod = _reload_flags()
    assert mod.ENABLE_TIER_ESCALATION is True  # type: ignore[attr-defined]


def test_subflag_blocked_when_master_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """ENABLE_RESIDENTIAL_TIER=true but master off → residential stays False."""
    monkeypatch.delenv("ENABLE_TIER_ESCALATION", raising=False)
    monkeypatch.setenv("ENABLE_RESIDENTIAL_TIER", "true")
    mod = _reload_flags()
    assert mod.ENABLE_TIER_ESCALATION is False  # type: ignore[attr-defined]
    assert mod.ENABLE_RESIDENTIAL_TIER is False  # type: ignore[attr-defined]


def test_subflag_default_when_master_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Master on + no sub-flag env vars → DC_PROXY True, RESIDENTIAL False, UNLOCKER False."""
    monkeypatch.setenv("ENABLE_TIER_ESCALATION", "true")
    monkeypatch.delenv("ENABLE_DC_PROXY_TIER", raising=False)
    monkeypatch.delenv("ENABLE_RESIDENTIAL_TIER", raising=False)
    monkeypatch.delenv("ENABLE_UNLOCKER_TIER", raising=False)
    mod = _reload_flags()
    assert mod.ENABLE_TIER_ESCALATION is True  # type: ignore[attr-defined]
    assert mod.ENABLE_DC_PROXY_TIER is True  # type: ignore[attr-defined]
    assert mod.ENABLE_RESIDENTIAL_TIER is False  # type: ignore[attr-defined]
    assert mod.ENABLE_UNLOCKER_TIER is False  # type: ignore[attr-defined]
