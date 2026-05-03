"""Phase E tests — DOM hints consecutive_misses tracking + eviction."""

from __future__ import annotations

import pytest

from models.scrape_profile import FieldSelectorMap, ScrapeProfile
from services.profile_store import ProfileStore
from services.profile_updater import update_profile_after_extraction


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / "profiles")


def _miss_result() -> dict:
    return {
        "extraction_tier_used": "TIER_3_DOM",
        "_dom_hints_attempted": True,
        "_dom_hints_hit": False,
    }


def _hit_result() -> dict:
    return {
        "extraction_tier_used": "TIER_3_DOM",
        "_dom_hints_attempted": True,
        "_dom_hints_hit": True,
        "units": [{"unit_id": "U1"}],
    }


def test_e2_consecutive_misses_increments_on_miss(store: ProfileStore) -> None:
    """Each hints-attempted + no-hit call must increment consecutive_misses."""
    p = ScrapeProfile(canonical_id="phase_e_001")
    p.dom_hints.field_selectors.container = ".unit"
    store.save(p)

    p = update_profile_after_extraction(p, _miss_result(), 5, store)
    assert p.dom_hints.consecutive_misses == 1

    p = update_profile_after_extraction(p, _miss_result(), 5, store)
    assert p.dom_hints.consecutive_misses == 2


def test_e2_three_consecutive_misses_evicts_selectors(store: ProfileStore) -> None:
    """After 3 consecutive misses, field_selectors must be cleared."""
    p = ScrapeProfile(canonical_id="phase_e_002")
    p.dom_hints.field_selectors.container = ".unit"
    store.save(p)

    for _ in range(3):
        p = update_profile_after_extraction(p, _miss_result(), 5, store)

    assert p.dom_hints.field_selectors.container in (None, ""), (
        "Selector must be cleared after 3 consecutive misses"
    )
    assert p.dom_hints.consecutive_misses == 0, (
        "consecutive_misses must reset to 0 after eviction"
    )


def test_e2_hit_resets_consecutive_misses(store: ProfileStore) -> None:
    """A successful hints hit must reset consecutive_misses to 0."""
    p = ScrapeProfile(canonical_id="phase_e_003")
    p.dom_hints.field_selectors.container = ".unit"
    p.dom_hints.consecutive_misses = 2
    store.save(p)

    p = update_profile_after_extraction(p, _hit_result(), 1, store)
    assert p.dom_hints.consecutive_misses == 0


def test_e2_no_attempt_no_change(store: ProfileStore) -> None:
    """If hints weren't attempted, consecutive_misses must not change."""
    p = ScrapeProfile(canonical_id="phase_e_004")
    p.dom_hints.consecutive_misses = 1
    store.save(p)

    result = {"extraction_tier_used": "TIER_1_API", "units": [{"unit_id": "U1"}]}
    p = update_profile_after_extraction(p, result, 1, store)
    assert p.dom_hints.consecutive_misses == 1
