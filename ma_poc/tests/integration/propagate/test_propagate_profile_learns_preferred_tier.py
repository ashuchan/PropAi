"""Propagate layer: profile learns and retains the preferred extraction tier.

After a successful extraction the profile records the winning tier number in
`confidence.preferred_tier`. If a cheaper tier succeeds on a later run, the
preferred_tier is updated downward. It is never moved upward by a worse tier.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from models.scrape_profile import ProfileMaturity, ScrapeProfile
from services.profile_store import ProfileStore
from services.profile_updater import update_profile_after_extraction


def _make_store(tmp_path: Path) -> ProfileStore:
    return ProfileStore(base_dir=tmp_path / "profiles")


def _success_result(tier: str, units: int = 2) -> dict:
    return {
        "extraction_tier_used": tier,
        "units": [{"unit_id": f"u{i}"} for i in range(units)],
    }


def _failure_result() -> dict:
    return {"extraction_tier_used": "FAILED", "units": []}


# ---------------------------------------------------------------------------
# Tier recording
# ---------------------------------------------------------------------------


def test_preferred_tier_set_after_first_success(tmp_path: Path) -> None:
    """First successful extraction records the tier in preferred_tier."""
    store = _make_store(tmp_path)
    profile = ScrapeProfile(canonical_id="prop-pt-001")

    update_profile_after_extraction(profile, _success_result("TIER_3_DOM"), 2, store)

    assert profile.confidence.preferred_tier == 3


def test_preferred_tier_moves_down_when_better_tier_wins(tmp_path: Path) -> None:
    """If a lower-numbered tier wins later, preferred_tier decreases."""
    store = _make_store(tmp_path)
    profile = ScrapeProfile(canonical_id="prop-pt-002")

    update_profile_after_extraction(profile, _success_result("TIER_3_DOM"), 2, store)
    assert profile.confidence.preferred_tier == 3

    update_profile_after_extraction(profile, _success_result("TIER_1_API"), 3, store)
    assert profile.confidence.preferred_tier == 1


def test_preferred_tier_does_not_move_up_on_worse_tier(tmp_path: Path) -> None:
    """Once preferred_tier is set to 1, a later tier-3 win must not increase it."""
    store = _make_store(tmp_path)
    profile = ScrapeProfile(canonical_id="prop-pt-003")

    update_profile_after_extraction(profile, _success_result("TIER_1_API"), 3, store)
    assert profile.confidence.preferred_tier == 1

    update_profile_after_extraction(profile, _success_result("TIER_3_DOM"), 2, store)
    assert profile.confidence.preferred_tier == 1


def test_preferred_tier_none_before_any_success(tmp_path: Path) -> None:
    """preferred_tier starts as None and stays None on failure."""
    store = _make_store(tmp_path)
    profile = ScrapeProfile(canonical_id="prop-pt-004")

    assert profile.confidence.preferred_tier is None

    update_profile_after_extraction(profile, _failure_result(), 0, store)
    assert profile.confidence.preferred_tier is None


def test_last_success_tier_updated_independently(tmp_path: Path) -> None:
    """last_success_tier always reflects the most recent winning tier,
    whereas preferred_tier is the best (lowest-numbered) tier seen so far."""
    store = _make_store(tmp_path)
    profile = ScrapeProfile(canonical_id="prop-pt-005")

    update_profile_after_extraction(profile, _success_result("TIER_1_API"), 2, store)
    assert profile.confidence.last_success_tier == 1

    update_profile_after_extraction(profile, _success_result("TIER_3_DOM"), 2, store)
    assert profile.confidence.last_success_tier == 3
    assert profile.confidence.preferred_tier == 1  # unchanged
