"""Propagate layer: maturity transitions across 3 successive successful runs.

COLD → WARM after 1 success
COLD → WARM → HOT after 3 consecutive successes

Also verifies that 3 consecutive failures demote the profile back to COLD.
"""

from __future__ import annotations

from pathlib import Path

from models.scrape_profile import ProfileMaturity, ScrapeProfile
from services.profile_store import ProfileStore
from services.profile_updater import update_profile_after_extraction


def _make_store(tmp_path: Path) -> ProfileStore:
    return ProfileStore(base_dir=tmp_path / "profiles")


def _success(tier: str = "TIER_2_JSONLD", units: int = 2) -> dict:
    return {
        "extraction_tier_used": tier,
        "units": [{"unit_id": f"u{i}"} for i in range(units)],
    }


def _failure() -> dict:
    return {"extraction_tier_used": "FAILED", "units": []}


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


def test_new_profile_starts_cold(tmp_path: Path) -> None:
    """A freshly created ScrapeProfile has maturity COLD."""
    profile = ScrapeProfile(canonical_id="prop-mty-001")
    assert profile.confidence.maturity == ProfileMaturity.COLD


def test_one_success_promotes_to_warm(tmp_path: Path) -> None:
    """After a single success, maturity becomes WARM."""
    store = _make_store(tmp_path)
    profile = ScrapeProfile(canonical_id="prop-mty-002")

    update_profile_after_extraction(profile, _success(), 2, store)

    assert profile.confidence.maturity == ProfileMaturity.WARM
    assert profile.confidence.consecutive_successes == 1


def test_three_consecutive_successes_promote_to_hot(tmp_path: Path) -> None:
    """Three consecutive successes promote maturity all the way to HOT."""
    store = _make_store(tmp_path)
    profile = ScrapeProfile(canonical_id="prop-mty-003")

    for _ in range(3):
        update_profile_after_extraction(profile, _success(), 2, store)

    assert profile.confidence.maturity == ProfileMaturity.HOT
    assert profile.confidence.consecutive_successes == 3


def test_two_successes_stay_warm(tmp_path: Path) -> None:
    """Two successes are not enough for HOT; profile stays WARM."""
    store = _make_store(tmp_path)
    profile = ScrapeProfile(canonical_id="prop-mty-004")

    for _ in range(2):
        update_profile_after_extraction(profile, _success(), 2, store)

    assert profile.confidence.maturity == ProfileMaturity.WARM


# ---------------------------------------------------------------------------
# Demotion
# ---------------------------------------------------------------------------


def test_failure_resets_consecutive_successes(tmp_path: Path) -> None:
    """A failure after two successes resets consecutive_successes to 0."""
    store = _make_store(tmp_path)
    profile = ScrapeProfile(canonical_id="prop-mty-005")

    for _ in range(2):
        update_profile_after_extraction(profile, _success(), 2, store)
    assert profile.confidence.consecutive_successes == 2

    update_profile_after_extraction(profile, _failure(), 0, store)

    assert profile.confidence.consecutive_successes == 0
    assert profile.confidence.consecutive_failures == 1


def test_three_failures_demote_to_cold(tmp_path: Path) -> None:
    """Three consecutive failures demote profile back to COLD."""
    store = _make_store(tmp_path)
    profile = ScrapeProfile(canonical_id="prop-mty-006")

    # Promote to HOT first
    for _ in range(3):
        update_profile_after_extraction(profile, _success(), 2, store)
    assert profile.confidence.maturity == ProfileMaturity.HOT

    # Then fail 3 times
    for _ in range(3):
        update_profile_after_extraction(profile, _failure(), 0, store)

    assert profile.confidence.maturity == ProfileMaturity.COLD


def test_interrupted_run_resets_and_recovers(tmp_path: Path) -> None:
    """Two successes → failure → two more successes → back to WARM."""
    store = _make_store(tmp_path)
    profile = ScrapeProfile(canonical_id="prop-mty-007")

    for _ in range(2):
        update_profile_after_extraction(profile, _success(), 2, store)
    update_profile_after_extraction(profile, _failure(), 0, store)
    for _ in range(2):
        update_profile_after_extraction(profile, _success(), 2, store)

    assert profile.confidence.consecutive_successes == 2
    assert profile.confidence.maturity == ProfileMaturity.WARM
