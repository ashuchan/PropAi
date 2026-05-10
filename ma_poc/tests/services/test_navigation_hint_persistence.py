"""navigation_hint persistence from empty-units LLM_DOM tier.

Pinning the contract:
- ``_llm_hints["navigation_hint"]`` (singular, latest tier wins) is read.
- ``scrape_result["_llm_navigation_hints"]`` (list across all tiers) is
  ALSO read so when LLM_DOM emits ``/floorplans`` and the monolithic LLM
  emits ``/availability``, BOTH end up in the profile — not just the last.
- Persistence works even when ``units`` is empty (the all-tiers-failed
  path that surfaces ``aggregated_hints`` to ``_llm_hints``).
- 10-cap on ``last_navigation_hints``; duplicates dedup with most-recent
  position.
"""

from __future__ import annotations

from typing import Any

import pytest

from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.services.profile_store import ProfileStore
from ma_poc.services.profile_updater import update_profile_after_extraction


@pytest.fixture
def store(tmp_path: Any) -> ProfileStore:
    return ProfileStore(tmp_path / "profiles")


def test_nav_hint_persists_when_units_empty(store: ProfileStore) -> None:
    """The LLM_DOM tier returned 0 units but a useful navigation_hint.
    Adapter's all-tiers-empty path surfaces ``_llm_hints`` with the
    nav_hint inside. Writer must persist it so next run can follow."""
    p = ScrapeProfile(canonical_id="pr7_001")
    store.save(p)

    scrape_result: dict[str, Any] = {
        "extraction_tier_used": "TIER_4_LLM_DOM",  # tier ran but no units
        "units": [],  # empty
        "_llm_hints": {
            "navigation_hint": "/floor-plans",
        },
    }
    p = update_profile_after_extraction(p, scrape_result, 1, store)

    assert "/floor-plans" in p.navigation.last_navigation_hints, (
        "Empty-units LLM_DOM with navigation_hint must still persist."
    )


def test_all_nav_hints_from_list_persist(store: ProfileStore) -> None:
    """LLM_DOM emitted ``/floor-plans``, monolithic emitted ``/availability``.
    The aggregator collapses ``_llm_hints["navigation_hint"]`` to the
    last one, but the ``_llm_navigation_hints`` list preserves both. PR 7
    reads the list so both end up persisted, not just the most-recent."""
    p = ScrapeProfile(canonical_id="pr7_002")
    store.save(p)

    scrape_result: dict[str, Any] = {
        "extraction_tier_used": "TIER_4_LLM",
        "units": [],
        "_llm_hints": {
            # Latest-wins single hint
            "navigation_hint": "/availability",
        },
        "_llm_navigation_hints": [
            "/floor-plans",
            "/availability",
        ],
    }
    p = update_profile_after_extraction(p, scrape_result, 1, store)

    persisted = p.navigation.last_navigation_hints
    assert "/floor-plans" in persisted
    assert "/availability" in persisted


def test_nav_hint_dedup_across_singular_and_list(store: ProfileStore) -> None:
    """If the singular ``navigation_hint`` is also in the list, only one
    entry should land in the profile, not two."""
    p = ScrapeProfile(canonical_id="pr7_003")
    store.save(p)

    scrape_result: dict[str, Any] = {
        "extraction_tier_used": "TIER_4_LLM",
        "units": [],
        "_llm_hints": {"navigation_hint": "/floor-plans"},
        "_llm_navigation_hints": ["/floor-plans"],
    }
    p = update_profile_after_extraction(p, scrape_result, 1, store)

    persisted = p.navigation.last_navigation_hints
    assert persisted.count("/floor-plans") == 1


def test_nav_hint_cap_at_10(store: ProfileStore) -> None:
    """``last_navigation_hints`` keeps the most-recent 10. Older entries fall off."""
    p = ScrapeProfile(canonical_id="pr7_004")
    # Pre-populate with 8 prior hints
    p.navigation.last_navigation_hints = [f"/old-{i}" for i in range(8)]
    store.save(p)

    scrape_result: dict[str, Any] = {
        "extraction_tier_used": "TIER_4_LLM",
        "units": [],
        "_llm_navigation_hints": [f"/new-{i}" for i in range(5)],
    }
    p = update_profile_after_extraction(p, scrape_result, 1, store)

    persisted = p.navigation.last_navigation_hints
    assert len(persisted) == 10, "Cap at 10 — most-recent should win."
    # The most-recent 5 (/new-0..4) MUST be present.
    for i in range(5):
        assert f"/new-{i}" in persisted
    # And only the LATEST 5 of the old 8 survive (oldest 3 fall off).
    assert "/old-0" not in persisted
    assert "/old-1" not in persisted
    assert "/old-2" not in persisted


def test_no_nav_hints_no_changes(store: ProfileStore) -> None:
    """When neither ``_llm_hints.navigation_hint`` nor
    ``_llm_navigation_hints`` is present, the profile's nav-hint list is
    untouched (not cleared)."""
    p = ScrapeProfile(canonical_id="pr7_005")
    p.navigation.last_navigation_hints = ["/prior"]
    store.save(p)

    scrape_result: dict[str, Any] = {
        "extraction_tier_used": "TIER_3_DOM",
        "units": [{"unit_id": "U1", "asking_rent": 1500}],
        # No _llm_hints, no _llm_navigation_hints
    }
    p = update_profile_after_extraction(p, scrape_result, 1, store)

    assert p.navigation.last_navigation_hints == ["/prior"], (
        "Absence of nav-hint signal must NOT clear prior hints."
    )


def test_list_only_no_singular(store: ProfileStore) -> None:
    """The all-tiers-empty path may set ``_llm_navigation_hints`` without
    setting ``_llm_hints``. Writer must still pick up the list."""
    p = ScrapeProfile(canonical_id="pr7_006")
    store.save(p)

    scrape_result: dict[str, Any] = {
        "extraction_tier_used": "TIER_3_DOM",
        "units": [],
        # No _llm_hints at all
        "_llm_navigation_hints": ["/contact-us", "/floor-plans"],
    }
    p = update_profile_after_extraction(p, scrape_result, 1, store)

    persisted = p.navigation.last_navigation_hints
    assert "/contact-us" in persisted
    assert "/floor-plans" in persisted
