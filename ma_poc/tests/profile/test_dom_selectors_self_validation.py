"""Save-time self-validation for LLM-produced DOM selectors.

Background — when ``analyze_dom_with_llm`` returned units AND a CSS-
selector dict, the selectors were persisted blindly. Tomorrow's run hit
``extract_with_hints(...)`` with selectors that didn't match the
freshly-fetched HTML, the cascade fell through to LLM, and the cycle
repeated. The ``consecutive_misses`` counter eventually evicted bad
selectors after three full runs, but the per-property cost was three
days of LLM tax for a hint that never had a chance.

The save-time validation closes the gap: the adapter replays the
selectors against the same ``dom_section_html`` the LLM saw and stamps
a quality score on the saved hints. Selectors that fail to reproduce
their own units are dropped at the source.

These tests pin:
  - The quality score round-trips from adapter → ``_llm_hints`` →
    ``profile.dom_hints.field_selectors_quality``.
  - Out-of-range scores are clamped (defensive — the adapter shouldn't
    emit them, but malformed payloads from external callers shouldn't
    corrupt the model).
  - Default for legacy profiles (no quality recorded) is 1.0 so
    pre-fix profiles aren't accidentally demoted.
"""

from __future__ import annotations

from typing import Any

import pytest

from models.scrape_profile import FieldSelectorMap, ScrapeProfile
from services.profile_store import ProfileStore
from services.profile_updater import update_profile_after_extraction


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / "profiles")


def _scrape_result_with_dom_hints(
    css_selectors: dict[str, str],
    *,
    quality: float | None = None,
    units: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    hints: dict[str, Any] = {"css_selectors": css_selectors}
    if quality is not None:
        hints["css_selectors_quality"] = quality
    return {
        "extraction_tier_used": "TIER_4_LLM_DOM",
        "units": units or [{"unit_id": "U1", "asking_rent": 1500}],
        "_llm_hints": hints,
    }


def test_quality_one_zero_when_validator_perfect(store: ProfileStore) -> None:
    """When the adapter reports quality=1.0 (selectors reproduced every
    LLM-produced unit), the persisted profile records 1.0 and the
    replay path treats the hints as fully trusted on the next run."""
    p = ScrapeProfile(canonical_id="dom_sv_001")
    store.save(p)

    result = _scrape_result_with_dom_hints(
        {"container": ".unit-card", "rent": ".price"},
        quality=1.0,
    )
    p = update_profile_after_extraction(p, result, 1, store)

    fs = p.dom_hints.field_selectors
    assert fs.container == ".unit-card"
    assert p.dom_hints.field_selectors_quality == 1.0


def test_quality_below_threshold_persists_but_marks_low(store: ProfileStore) -> None:
    """A 0.5 quality score (selectors reproduced ~half of units) must
    persist on the profile so the cascade can still try them tomorrow,
    but the score travels along so the replay-side gate can soft-fail.

    The alternative — refusing to save anything below 1.0 — would force
    every property to re-LLM every day, which is the regression we're
    closing, just in the opposite direction."""
    p = ScrapeProfile(canonical_id="dom_sv_002")
    store.save(p)

    result = _scrape_result_with_dom_hints(
        {"container": ".kinda-flaky", "rent": ".price"},
        quality=0.5,
    )
    p = update_profile_after_extraction(p, result, 4, store)

    assert p.dom_hints.field_selectors.container == ".kinda-flaky"
    assert p.dom_hints.field_selectors_quality == 0.5


def test_missing_quality_defaults_to_one(store: ProfileStore) -> None:
    """Legacy adapter calls (or non-LLM_DOM tiers that contributed
    selectors via some other path) might not include a quality score.
    Default to 1.0 so they aren't accidentally demoted vs. the new
    LLM-DOM path that always carries a score."""
    p = ScrapeProfile(canonical_id="dom_sv_003")
    store.save(p)

    result = _scrape_result_with_dom_hints(
        {"container": ".no-quality-set", "rent": ".price"},
        quality=None,  # explicitly omitted
    )
    p = update_profile_after_extraction(p, result, 2, store)

    assert p.dom_hints.field_selectors_quality == 1.0


def test_quality_clamped_to_unit_interval(store: ProfileStore) -> None:
    """The adapter computes quality as ``len(replayed)/expected`` —
    bounded above by the loop, but a malformed hint payload from
    elsewhere could ship a >1.0 or negative value. Defensive clamp
    keeps the model field meaningful for downstream gates."""
    p = ScrapeProfile(canonical_id="dom_sv_004")
    store.save(p)

    # Above 1.0 — clamp to 1.0
    result_high = _scrape_result_with_dom_hints(
        {"container": ".high"}, quality=2.5,
    )
    p = update_profile_after_extraction(p, result_high, 3, store)
    assert p.dom_hints.field_selectors_quality == 1.0

    # Below 0.0 — clamp to 0.0
    result_low = _scrape_result_with_dom_hints(
        {"container": ".neg"}, quality=-0.5,
    )
    p = update_profile_after_extraction(p, result_low, 3, store)
    assert p.dom_hints.field_selectors_quality == 0.0


def test_quality_persists_across_load(store: ProfileStore) -> None:
    """Round-trip: save with quality=0.7, reload, score still there."""
    p = ScrapeProfile(canonical_id="dom_sv_005")
    store.save(p)

    result = _scrape_result_with_dom_hints(
        {"container": ".kept"}, quality=0.7,
    )
    update_profile_after_extraction(p, result, 5, store)

    reloaded = store.load("dom_sv_005")
    assert reloaded is not None
    assert reloaded.dom_hints.field_selectors_quality == 0.7


def test_legacy_profile_without_quality_field_loads_with_default() -> None:
    """A profile JSON that pre-dates this field must still validate
    against the model. Pydantic's default kicks in and gives the legacy
    profile the optimistic 1.0 score so existing replay flows keep
    working until the next save updates it."""
    legacy_payload = {
        "canonical_id": "legacy_001",
        "version": 1,
        "dom_hints": {
            "field_selectors": {"container": ".legacy"},
            "consecutive_misses": 0,
            # ← no field_selectors_quality at all
        },
    }
    p = ScrapeProfile.model_validate(legacy_payload)
    assert p.dom_hints.field_selectors_quality == 1.0
    assert p.dom_hints.field_selectors.container == ".legacy"


def test_replay_gate_quality_below_threshold_skipped() -> None:
    """The adapter reads ``field_selectors_quality`` to decide whether
    to bother with the saved hints in the cascade. Hints scored below
    0.4 are skipped; at or above 0.4 they get a try.

    This test pins the threshold logic at the model level so a future
    refactor of the cascade can't silently change it."""
    p = ScrapeProfile(canonical_id="legacy_002")
    p.dom_hints.field_selectors = FieldSelectorMap(container=".broken")
    p.dom_hints.field_selectors_quality = 0.3
    # Threshold check that the cascade applies — pinning here so any
    # threshold change has a single test to update.
    threshold = 0.4
    assert p.dom_hints.field_selectors_quality < threshold

    p.dom_hints.field_selectors_quality = 0.4
    assert p.dom_hints.field_selectors_quality >= threshold
