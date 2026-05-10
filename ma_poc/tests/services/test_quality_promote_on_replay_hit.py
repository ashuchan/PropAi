"""Promote-on-replay-hit for DOM ``field_selectors_quality``.

A successful DOM replay hit bumps ``field_selectors_quality`` by +0.05,
clamped at 1.0, so degraded saves at 0.4 (from the loosened
self-validation gate) can rise toward full quality after consistent
success.

Combined with the quality-tiered eviction policy (1-strike for <0.8 vs
3-strike for >=0.8), a degraded selector that proves itself across
exactly 8 hits crosses the 0.8 boundary and enters the high-resilience
bucket. A hint that misses early stays in the aggressive-eviction
bucket and gets pruned.

LlmFieldMapping promotion lives in ``pms.adapters.generic.py`` and is
exercised via the cascade — covered indirectly by the writer-contract
test with the ``ENABLE_PROMOTE_ON_HINT`` flag toggled.
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


def _hit_result() -> dict[str, Any]:
    return {
        "extraction_tier_used": "TIER_3_DOM",
        "_dom_hints_attempted": True,
        "_dom_hints_hit": True,
        "units": [{"unit_id": "U1", "asking_rent": 1500}],
    }


# ── Promote on hit (default ON) ────────────────────────────────────────────


class TestPromoteOnDomHit:
    def test_quality_zero_four_promotes_to_zero_four_five(self, store: ProfileStore) -> None:
        """Single hit on a quality=0.4 entry → quality=0.45."""
        p = ScrapeProfile(canonical_id="pr9_promote_001")
        p.dom_hints.field_selectors.container = ".unit"
        p.dom_hints.field_selectors_quality = 0.4
        store.save(p)

        p = update_profile_after_extraction(p, _hit_result(), 1, store)
        assert abs(p.dom_hints.field_selectors_quality - 0.45) < 1e-9

    def test_quality_zero_nine_eight_clamps_at_one(self, store: ProfileStore) -> None:
        """Quality near the cap → clamped to 1.0, no overflow."""
        p = ScrapeProfile(canonical_id="pr9_promote_002")
        p.dom_hints.field_selectors.container = ".unit"
        p.dom_hints.field_selectors_quality = 0.98
        store.save(p)

        p = update_profile_after_extraction(p, _hit_result(), 1, store)
        assert p.dom_hints.field_selectors_quality == 1.0

    def test_repeated_hits_cross_pr8_boundary(self, store: ProfileStore) -> None:
        """A degraded save (0.4) that hits 8+ times crosses the 0.8 boundary
        and enters PR 8's high-resilience eviction bucket. This is the key
        graduation pathway PR 9 sub-2 enables.

        0.4 + 8 × 0.05 = 0.8 exactly.
        """
        p = ScrapeProfile(canonical_id="pr9_promote_003")
        p.dom_hints.field_selectors.container = ".unit"
        p.dom_hints.field_selectors_quality = 0.4
        store.save(p)

        for _ in range(8):
            p = update_profile_after_extraction(p, _hit_result(), 1, store)

        # Strict equality (no float drift) thanks to round(.., 2) in
        # the promotion code. A degraded save graduates EXACTLY at 8 hits.
        assert p.dom_hints.field_selectors_quality == 0.8, (
            "After 8 hits, a degraded save should reach the 0.8 boundary "
            "→ PR 8's high-resilience tier."
        )


# ── Flag off: no promotion ─────────────────────────────────────────────────


class TestFlagOffPath:
    def test_flag_off_no_promotion(
        self, store: ProfileStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ENABLE_PROMOTE_ON_HINT is off, quality stays put on hit.
        Only consecutive_misses still resets to 0."""
        monkeypatch.setenv("ENABLE_PROMOTE_ON_HINT", "false")
        p = ScrapeProfile(canonical_id="pr9_promote_off_001")
        p.dom_hints.field_selectors.container = ".unit"
        p.dom_hints.field_selectors_quality = 0.4
        p.dom_hints.consecutive_misses = 2
        store.save(p)

        p = update_profile_after_extraction(p, _hit_result(), 1, store)
        assert p.dom_hints.field_selectors_quality == 0.4
        assert p.dom_hints.consecutive_misses == 0


# ── Hit-without-promotion-flag-still-resets-misses ─────────────────────────


def test_hit_resets_misses_independent_of_flag(
    store: ProfileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The miss-streak reset on hit is foundational PR-1 behavior; the
    promote flag must not affect it whether on or off."""
    monkeypatch.setenv("ENABLE_PROMOTE_ON_HINT", "false")
    p = ScrapeProfile(canonical_id="pr9_reset_misses_001")
    p.dom_hints.field_selectors.container = ".unit"
    p.dom_hints.consecutive_misses = 2
    store.save(p)

    p = update_profile_after_extraction(p, _hit_result(), 1, store)
    assert p.dom_hints.consecutive_misses == 0
