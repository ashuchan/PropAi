"""Quality-tiered DOM-hint eviction.

Validated selectors (``field_selectors_quality >= 0.8``) keep 3-strike
resilience — transient misses don't destroy a hint that proved itself.

Soft / degraded selectors (``quality < 0.8`` — including selectors
saved via the loosened-self-validation path at quality=0.4) evict on
the first miss. They already failed self-validation OR were labelled
flaky at save time; cutting losses faster avoids 2 more days of wasted
DOM cascade attempts.

Boundary at 0.8 mirrors the comment in ``pms.adapters.generic.py``
("ratio >= 0.8 → high quality, persist as-is").
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


def _miss_result() -> dict[str, Any]:
    return {
        "extraction_tier_used": "TIER_3_DOM",
        "_dom_hints_attempted": True,
        "_dom_hints_hit": False,
        "units": [{"unit_id": "U1"}],
    }


def _hit_result() -> dict[str, Any]:
    return {
        "extraction_tier_used": "TIER_3_DOM",
        "_dom_hints_attempted": True,
        "_dom_hints_hit": True,
        "units": [{"unit_id": "U1"}],
    }


# ── High-quality (>= 0.8): 3-strike preserved ──────────────────────────────


class TestHighQualityResilience:
    def test_quality_one_zero_one_miss_does_not_evict(self, store: ProfileStore) -> None:
        """quality=1.0 + 1 miss: NOT evicted. Resilience preserved
        (transient page issue shouldn't destroy a validated hint)."""
        p = ScrapeProfile(canonical_id="pr8_hi_001")
        p.dom_hints.field_selectors.container = ".unit"
        p.dom_hints.field_selectors_quality = 1.0
        store.save(p)

        p = update_profile_after_extraction(p, _miss_result(), 5, store)
        assert p.dom_hints.field_selectors.container == ".unit"
        assert p.dom_hints.consecutive_misses == 1

    def test_quality_one_zero_three_misses_evicts(self, store: ProfileStore) -> None:
        """quality=1.0 + 3 misses: evicted. Existing behaviour preserved."""
        p = ScrapeProfile(canonical_id="pr8_hi_002")
        p.dom_hints.field_selectors.container = ".unit"
        p.dom_hints.field_selectors_quality = 1.0
        store.save(p)

        for _ in range(3):
            p = update_profile_after_extraction(p, _miss_result(), 5, store)

        assert p.dom_hints.field_selectors.container in (None, "")
        assert p.dom_hints.consecutive_misses == 0


# ── Low-quality (< 0.8): single-miss eviction ──────────────────────────────


class TestLowQualitySingleMiss:
    def test_quality_zero_four_one_miss_evicts(self, store: ProfileStore) -> None:
        """quality=0.4 (PR-6 degraded save) + 1 miss: evicted.
        Already-suspect hint that fails again is gone — saves the
        2 wasted cascade attempts on day 2 + 3."""
        p = ScrapeProfile(canonical_id="pr8_lo_001")
        p.dom_hints.field_selectors.container = ".unit"
        p.dom_hints.field_selectors_quality = 0.4
        store.save(p)

        p = update_profile_after_extraction(p, _miss_result(), 5, store)
        assert p.dom_hints.field_selectors.container in (None, ""), (
            "Single miss on quality<0.8 must evict. PR 8 design intent."
        )
        assert p.dom_hints.consecutive_misses == 0

    def test_quality_zero_seven_nine_just_below_boundary_evicts_on_one(
        self, store: ProfileStore
    ) -> None:
        """quality=0.79 (just below the 0.8 boundary) + 1 miss: evicted.
        Pin the boundary — any drift in the >= 0.8 condition surfaces
        as a failing test rather than silent behaviour change."""
        p = ScrapeProfile(canonical_id="pr8_lo_002")
        p.dom_hints.field_selectors.container = ".unit"
        p.dom_hints.field_selectors_quality = 0.79
        store.save(p)

        p = update_profile_after_extraction(p, _miss_result(), 5, store)
        assert p.dom_hints.field_selectors.container in (None, "")

    def test_quality_zero_eight_zero_at_boundary_uses_three_strikes(
        self, store: ProfileStore
    ) -> None:
        """quality=0.80 (at boundary, >=) + 1 miss: NOT evicted yet.
        The boundary is inclusive on the high-quality side."""
        p = ScrapeProfile(canonical_id="pr8_lo_003")
        p.dom_hints.field_selectors.container = ".unit"
        p.dom_hints.field_selectors_quality = 0.80
        store.save(p)

        p = update_profile_after_extraction(p, _miss_result(), 5, store)
        assert p.dom_hints.field_selectors.container == ".unit"
        assert p.dom_hints.consecutive_misses == 1


# ── Hit-resets-streak: preserved across both tiers ────────────────────────


class TestHitResetsStreak:
    def test_low_quality_hit_resets_consecutive_misses(self, store: ProfileStore) -> None:
        """A hit on a low-quality entry still resets the miss streak —
        single-miss only triggers when the hint is consistently failing."""
        p = ScrapeProfile(canonical_id="pr8_reset_001")
        p.dom_hints.field_selectors.container = ".unit"
        p.dom_hints.field_selectors_quality = 0.4
        # Pre-populate one miss in the prior run
        p.dom_hints.consecutive_misses = 0
        store.save(p)

        p = update_profile_after_extraction(p, _hit_result(), 1, store)
        assert p.dom_hints.consecutive_misses == 0
        assert p.dom_hints.field_selectors.container == ".unit", (
            "A hit on a degraded hint must NOT cause eviction — only a "
            "miss does."
        )
