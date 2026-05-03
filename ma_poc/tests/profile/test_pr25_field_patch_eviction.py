"""PR25 B2 — FieldPatch hit/miss tracking and eviction.

Verifies that _apply_field_patches bumps consecutive_replay_failures on miss
and success_count on hit, so profile_updater's eviction logic actually fires.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

for _name in ("playwright", "playwright.async_api"):
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        _mod.BrowserContext = object  # type: ignore[attr-defined]
        _mod.Page = object  # type: ignore[attr-defined]
        _mod.async_playwright = object  # type: ignore[attr-defined]
        sys.modules[_name] = _mod

from ma_poc.models.scrape_profile import FieldPatch, ScrapeProfile
from ma_poc.pms.adapters.generic import _apply_field_patches
from ma_poc.services.profile_store import ProfileStore
from ma_poc.services.profile_updater import update_profile_after_extraction


def test_b2_field_patch_hit_increments_success_count() -> None:
    """A patch that successfully fills at least one unit must bump success_count."""
    patch = FieldPatch(
        api_url_pattern="api/units",
        field_name="rent_low",
        json_path="rents",
        confidence=0.9,
    )
    assert patch.success_count == 0
    assert patch.consecutive_replay_failures == 0

    units = [{"unit_id": "U1"}, {"unit_id": "U2"}]
    api = [{"url": "https://x.com/api/units", "body": {"rents": [1500, 1700]}}]
    _apply_field_patches(units, api, [patch])

    assert patch.success_count == 1, (
        f"patch hit must bump success_count to 1; got {patch.success_count}"
    )
    assert patch.consecutive_replay_failures == 0
    assert units[0]["rent_low"] == 1500


def test_b2_field_patch_miss_no_url_match_increments_failures() -> None:
    """When no response matches the URL pattern, consecutive_replay_failures bumps."""
    patch = FieldPatch(
        api_url_pattern="api/availability",
        field_name="rent_low",
        json_path="rents",
    )
    units = [{"unit_id": "U1"}]
    api = [{"url": "https://x.com/api/units", "body": {"rents": [1500]}}]  # different URL
    _apply_field_patches(units, api, [patch])

    assert patch.consecutive_replay_failures == 1
    assert patch.success_count == 0


def test_b2_field_patch_miss_path_returns_none_increments_failures() -> None:
    """When json_path navigates to None, consecutive_replay_failures bumps."""
    patch = FieldPatch(
        api_url_pattern="api/units",
        field_name="rent_low",
        json_path="nonexistent.path",
    )
    units = [{"unit_id": "U1"}]
    api = [{"url": "https://x.com/api/units", "body": {"rents": [1500]}}]
    _apply_field_patches(units, api, [patch])

    assert patch.consecutive_replay_failures == 1


def test_b2_field_patch_miss_when_all_units_already_filled_increments_failures() -> None:
    """When every unit already has the field filled, the patch contributes nothing
    → counted as a miss."""
    patch = FieldPatch(
        api_url_pattern="api/units",
        field_name="rent_low",
        json_path="rents",
    )
    units = [{"unit_id": "U1", "rent_low": 9999}]  # already filled
    api = [{"url": "https://x.com/api/units", "body": {"rents": [1500]}}]
    _apply_field_patches(units, api, [patch])

    assert patch.consecutive_replay_failures == 1
    assert units[0]["rent_low"] == 9999  # unchanged


def test_b2_field_patch_hit_resets_failure_streak() -> None:
    """A successful patch hit resets consecutive_replay_failures to 0."""
    patch = FieldPatch(
        api_url_pattern="api/units",
        field_name="rent_low",
        json_path="rents",
        consecutive_replay_failures=2,  # already on a streak
    )
    units = [{"unit_id": "U1"}]
    api = [{"url": "https://x.com/api/units", "body": {"rents": [1500]}}]
    _apply_field_patches(units, api, [patch])

    assert patch.consecutive_replay_failures == 0, (
        "successful hit must reset the failure streak"
    )
    assert patch.success_count == 1


def test_b2_field_patch_evicted_after_3_misses(tmp_path: Path) -> None:
    """End-to-end: after 3 cascade runs where the patch produces no values,
    update_profile_after_extraction evicts the patch."""
    store = ProfileStore(tmp_path / "profiles")
    p = ScrapeProfile(canonical_id="b2-evict")
    p.api_hints.field_patches = [
        FieldPatch(
            api_url_pattern="api/dead",
            field_name="rent_low",
            json_path="rents",
        )
    ]
    store.save(p)

    api_no_match = [{"url": "https://x.com/api/other", "body": {"v": 1}}]
    units = [{"unit_id": "U1"}]

    # Run 1: miss
    _apply_field_patches(units, api_no_match, p.api_hints.field_patches)
    p = update_profile_after_extraction(p, {"extraction_tier_used": "TIER_3_DOM"}, 1, store)
    assert len(p.api_hints.field_patches) == 1
    assert p.api_hints.field_patches[0].consecutive_replay_failures == 1

    # Run 2: miss
    _apply_field_patches(units, api_no_match, p.api_hints.field_patches)
    p = update_profile_after_extraction(p, {"extraction_tier_used": "TIER_3_DOM"}, 1, store)
    assert len(p.api_hints.field_patches) == 1
    assert p.api_hints.field_patches[0].consecutive_replay_failures == 2

    # Run 3: miss → eviction
    _apply_field_patches(units, api_no_match, p.api_hints.field_patches)
    p = update_profile_after_extraction(p, {"extraction_tier_used": "TIER_3_DOM"}, 1, store)
    assert len(p.api_hints.field_patches) == 0, (
        "patch must be evicted after 3 consecutive misses"
    )


def test_b2_field_patch_partial_fill_counts_as_hit() -> None:
    """If at least one unit gets filled (even if others don't), it's a hit."""
    patch = FieldPatch(
        api_url_pattern="api/units",
        field_name="rent_low",
        json_path="rents",
    )
    # 3 units, but only 1 patch value → 1 unit filled, 2 unchanged
    units = [{"unit_id": "U1"}, {"unit_id": "U2"}, {"unit_id": "U3"}]
    api = [{"url": "https://x.com/api/units", "body": {"rents": [1500]}}]
    _apply_field_patches(units, api, [patch])

    assert patch.success_count == 1
    assert patch.consecutive_replay_failures == 0
    assert units[0]["rent_low"] == 1500
    assert "rent_low" not in units[1]
    assert "rent_low" not in units[2]
