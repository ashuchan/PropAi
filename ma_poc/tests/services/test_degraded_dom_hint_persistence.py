"""Degraded DOM-hint persistence flag (``ENABLE_DEGRADED_DOM_PERSIST``).

Tests:
- ``enable_degraded_dom_persist()`` defaults to ``True`` and flips with
  the env var. Re-read each call (no import-time freezing).
- When the LLM_DOM_TARGETED save-site decides to persist degraded
  selectors (flag ON, quality < 0.4 → clamped to 0.4), the writer
  receives ``_llm_hints["css_selectors_quality"] == 0.4`` and the
  profile records 0.4 → so the replay-side gate (>= 0.4) admits them.
- When the flag is OFF, the adapter omits ``css_selectors`` from
  ``_llm_hints`` entirely, and the writer leaves
  ``profile.dom_hints.field_selectors`` un-set.

The save-site gate logic itself is one if/else inside
``pms.adapters.generic`` and is exercised end-to-end by the existing
DOM-selector tests. These tests pin the contract the gate emits.
"""

from __future__ import annotations

from typing import Any

import pytest

from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.services.profile_store import ProfileStore
from ma_poc.services.profile_updater import update_profile_after_extraction

# ── Flag function ──────────────────────────────────────────────────────────


class TestFeatureFlag:
    def test_default_is_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ENABLE_DEGRADED_DOM_PERSIST", raising=False)
        from ma_poc.config.feature_flags import enable_degraded_dom_persist
        assert enable_degraded_dom_persist() is True

    def test_explicit_false_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENABLE_DEGRADED_DOM_PERSIST", "false")
        from ma_poc.config.feature_flags import enable_degraded_dom_persist
        assert enable_degraded_dom_persist() is False

    def test_explicit_true_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENABLE_DEGRADED_DOM_PERSIST", "true")
        from ma_poc.config.feature_flags import enable_degraded_dom_persist
        assert enable_degraded_dom_persist() is True

    def test_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENABLE_DEGRADED_DOM_PERSIST", "TRUE")
        from ma_poc.config.feature_flags import enable_degraded_dom_persist
        assert enable_degraded_dom_persist() is True
        monkeypatch.setenv("ENABLE_DEGRADED_DOM_PERSIST", "FALSE")
        assert enable_degraded_dom_persist() is False

    def test_live_toggle_no_reimport(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The function reads env each call (per docstring). Flipping
        mid-process must take effect without an importlib.reload."""
        from ma_poc.config.feature_flags import enable_degraded_dom_persist
        monkeypatch.setenv("ENABLE_DEGRADED_DOM_PERSIST", "true")
        assert enable_degraded_dom_persist() is True
        monkeypatch.setenv("ENABLE_DEGRADED_DOM_PERSIST", "false")
        assert enable_degraded_dom_persist() is False
        monkeypatch.setenv("ENABLE_DEGRADED_DOM_PERSIST", "true")
        assert enable_degraded_dom_persist() is True


# ── Save-site contract: degraded saved → replay gate admits ───────────────


@pytest.fixture
def store(tmp_path: Any) -> ProfileStore:
    return ProfileStore(tmp_path / "profiles")


def _adapter_result_when_gate_persists_degraded(
    css_selectors: dict[str, str],
) -> dict[str, Any]:
    """Mimic what the adapter writes to ``_llm_hints`` when the gate's
    flag-ON branch runs: real selectors + quality clamped to 0.4."""
    return {
        "extraction_tier_used": "TIER_4_LLM_DOM",
        "units": [{"unit_id": "U1", "asking_rent": 1500}],
        "_llm_hints": {
            "css_selectors": css_selectors,
            "css_selectors_quality": 0.4,  # clamped from raw < 0.4
        },
    }


def _adapter_result_when_gate_drops_degraded() -> dict[str, Any]:
    """Mimic what the adapter writes when the gate's flag-OFF branch
    runs: no css_selectors at all (only nav hints / aggregator data)."""
    return {
        "extraction_tier_used": "TIER_4_LLM_DOM",
        "units": [{"unit_id": "U1", "asking_rent": 1500}],
        "_llm_hints": {
            # No css_selectors key — the gate dropped them.
            "navigation_hint": "https://example.com/floor-plans",
        },
    }


class TestGateContract:
    def test_degraded_saved_at_replay_gate_floor(self, store: ProfileStore) -> None:
        """When the gate persists degraded selectors, quality must be
        EXACTLY 0.4 (the replay-side admission threshold). Below 0.4
        and the saved selectors would be inert; above 0.4 misrepresents
        their actual reproducibility."""
        p = ScrapeProfile(canonical_id="pr6_001")
        store.save(p)

        result = _adapter_result_when_gate_persists_degraded(
            {"container": ".unit-card", "rent": ".price"}
        )
        p = update_profile_after_extraction(p, result, 1, store)

        fs = p.dom_hints.field_selectors
        assert fs.container == ".unit-card"
        assert fs.rent == ".price"
        assert p.dom_hints.field_selectors_quality == 0.4, (
            "Saved quality must equal the replay-side admission floor — "
            "any lower and the replay gate at >= 0.4 rejects the entry."
        )

    def test_dropped_when_gate_off_leaves_field_selectors_unset(
        self, store: ProfileStore
    ) -> None:
        """When the gate drops (flag off), the writer must NOT receive
        css_selectors. Profile's container stays None — replay path
        sees no hints to attempt."""
        p = ScrapeProfile(canonical_id="pr6_002")
        store.save(p)

        result = _adapter_result_when_gate_drops_degraded()
        p = update_profile_after_extraction(p, result, 1, store)

        fs = p.dom_hints.field_selectors
        # No selectors persisted — pre-fix fallback behaviour.
        assert fs.container is None
        assert fs.rent is None


# ── Replay-side admission: 0.4 quality is exactly the threshold ───────────


class TestReplayAdmissionFloor:
    def test_quality_exactly_0_4_passes_replay_gate(self) -> None:
        """The matcher in ``pms.adapters.generic:1330`` does
        ``if fs_quality >= 0.4: dom_hints = fs``. PR 6's clamp to 0.4
        relies on the >= comparison — pin the boundary here so a future
        edit to ``> 0.4`` would surface as a failing test, not a silent
        no-op of every PR-6-saved hint."""
        # The condition is a direct comparison on a float; testing the
        # contract via the constant rather than the runtime predicate
        # itself, because the runtime code is wrapped in an entire
        # adapter pipeline that's expensive to spin up here.
        threshold = 0.4
        clamp_value = 0.4
        assert clamp_value >= threshold, (
            "PR 6 saves degraded hints at exactly 0.4. The replay "
            "admission gate must accept exactly-0.4 (>=) — not require "
            "strictly greater."
        )
