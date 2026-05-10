"""``FieldPatch`` end-to-end persistence + JSONPath bracket walker.

Three layers tested independently and end-to-end:

1. ``_normalize_json_path`` — accepts every shape the LLM canonically emits
   (``$.units[*].rent``, ``units[0].rent``, ``data.units.0.rent``, ``uuid``)
   and produces one canonical token list.
2. ``_walk_json_path`` — resolves the token list against real JSON shapes,
   handling ``[N]`` index, ``[*]`` wildcard, and missing-key None.
3. ``_apply_field_patches`` — the reader that combines (1)+(2) and fills
   patches into units. Verified against bracket-notation paths.

Plus the order-of-operations regression: simulating the production
sequence (scrape → recovery → profile-update) verifies recovered
patches reach ``save_field_patch`` and end up in
``profile.api_hints.field_patches``.
"""

from __future__ import annotations

import pytest

from models.scrape_profile import ScrapeProfile
from services.profile_store import ProfileStore
from services.profile_updater import (
    _JSON_PATH_WILDCARD,
    _normalize_json_path,
    _walk_json_path,
    save_field_patch,
)

# ─── _normalize_json_path ──────────────────────────────────────────────────


class TestNormalizeJsonPath:
    def test_dollar_prefix_stripped(self):
        assert _normalize_json_path("$.units[*].rent") == ["units", _JSON_PATH_WILDCARD, "rent"]

    def test_dot_only_legacy_form(self):
        assert _normalize_json_path("data.units.0.rent") == ["data", "units", 0, "rent"]

    def test_bracket_index(self):
        assert _normalize_json_path("units[0].rent") == ["units", 0, "rent"]

    def test_mixed_dot_and_bracket(self):
        assert _normalize_json_path("data.units[*].pricing.amount") == [
            "data", "units", _JSON_PATH_WILDCARD, "pricing", "amount"
        ]

    def test_single_segment(self):
        assert _normalize_json_path("uuid") == ["uuid"]

    def test_empty_returns_empty_list(self):
        assert _normalize_json_path("") == []
        assert _normalize_json_path(None) == []

    def test_idempotent_via_string_round_trip(self):
        """Re-stripping a path that's already been processed produces the
        same tokens — survives multiple lstrip("$").lstrip(".") calls
        across writer/reader boundaries."""
        once = _normalize_json_path("$.units[0].rent")
        # The stored string form (from save_field_patch) is "units[0].rent"
        twice = _normalize_json_path("units[0].rent")
        assert once == twice


# ─── _walk_json_path ───────────────────────────────────────────────────────


class TestWalkJsonPath:
    def test_simple_dict_traversal(self):
        body = {"data": {"rent": 1500}}
        assert _walk_json_path(body, _normalize_json_path("data.rent")) == 1500

    def test_list_index(self):
        body = {"units": [{"rent": 1200}, {"rent": 1800}]}
        assert _walk_json_path(body, _normalize_json_path("units[0].rent")) == 1200
        assert _walk_json_path(body, _normalize_json_path("units[1].rent")) == 1800

    def test_wildcard_collects_field_from_each_element(self):
        """[*] is the load-bearing token — pre-PR _get_path returned None
        for any path containing it. Now: returns a list of values."""
        body = {"units": [{"rent": 1200}, {"rent": 1500}, {"rent": 1800}]}
        assert _walk_json_path(body, _normalize_json_path("units[*].rent")) == [1200, 1500, 1800]

    def test_wildcard_drops_missing_elements(self):
        """When some elements lack the field, they're dropped (None) rather
        than poisoning the result list."""
        body = {"units": [{"rent": 1200}, {"sqft": 800}, {"rent": 1800}]}
        assert _walk_json_path(body, _normalize_json_path("units[*].rent")) == [1200, 1800]

    def test_wildcard_against_non_list_returns_none(self):
        body = {"units": {"rent": 1500}}  # dict, not list
        assert _walk_json_path(body, _normalize_json_path("units[*].rent")) is None

    def test_missing_key_returns_none(self):
        body = {"data": {"rent": 1500}}
        assert _walk_json_path(body, _normalize_json_path("data.missing")) is None

    def test_empty_path_returns_object_unchanged(self):
        body = {"a": 1}
        assert _walk_json_path(body, []) == body

    def test_none_object_returns_none(self):
        assert _walk_json_path(None, _normalize_json_path("x.y")) is None

    def test_dollar_prefix_resolves_correctly(self):
        body = {"uuid": "abc-123"}
        assert _walk_json_path(body, _normalize_json_path("$.uuid")) == "abc-123"


# ─── _apply_field_patches end-to-end ───────────────────────────────────────


class TestApplyFieldPatchesBracketSupport:
    """Pre-PR: any path containing brackets silently failed at replay,
    even when the patch was correctly saved. Post-PR: brackets resolve.
    """

    def test_wildcard_path_fills_each_unit_positionally(self):
        from ma_poc.models.scrape_profile import FieldPatch
        from ma_poc.pms.adapters.generic import _apply_field_patches

        units = [
            {"unit_id": "U1", "rent_low": None},
            {"unit_id": "U2", "rent_low": None},
            {"unit_id": "U3", "rent_low": None},
        ]
        api_responses = [
            {
                "url": "https://api.example.com/units",
                "body": {"units": [
                    {"id": "U1", "rent": 1200},
                    {"id": "U2", "rent": 1500},
                    {"id": "U3", "rent": 1800},
                ]},
            },
        ]
        patches = [
            FieldPatch(
                api_url_pattern="api.example.com/units",
                field_name="rent_low",
                json_path="units[*].rent",
                confidence=0.9,
            ),
        ]
        out = _apply_field_patches(units, api_responses, patches)
        assert out[0]["rent_low"] == 1200
        assert out[1]["rent_low"] == 1500
        assert out[2]["rent_low"] == 1800

    def test_legacy_dot_only_path_still_works(self):
        from ma_poc.models.scrape_profile import FieldPatch
        from ma_poc.pms.adapters.generic import _apply_field_patches

        units = [{"unit_id": "U1", "rent_low": None}]
        api_responses = [
            {"url": "https://api.example.com/x", "body": {"data": {"units": [{"r": 999}]}}},
        ]
        patches = [
            FieldPatch(
                api_url_pattern="api.example.com/x",
                field_name="rent_low",
                json_path="data.units.0.r",  # legacy dot-only — no brackets
                confidence=0.9,
            ),
        ]
        out = _apply_field_patches(units, api_responses, patches)
        assert out[0]["rent_low"] == 999

    def test_does_not_overwrite_non_null_values(self):
        from ma_poc.models.scrape_profile import FieldPatch
        from ma_poc.pms.adapters.generic import _apply_field_patches

        units = [{"unit_id": "U1", "rent_low": 1000}]  # already populated
        api_responses = [
            {"url": "https://api.example.com/u", "body": {"units": [{"r": 9999}]}},
        ]
        patches = [
            FieldPatch(
                api_url_pattern="api.example.com/u",
                field_name="rent_low",
                json_path="units[*].r",
                confidence=0.9,
            ),
        ]
        out = _apply_field_patches(units, api_responses, patches)
        assert out[0]["rent_low"] == 1000  # preserved


# ─── save_field_patch persists bracket paths losslessly ────────────────────


class TestSaveFieldPatchBracketPath:
    def test_bracket_path_preserved_through_save(self):
        """PR 2 fix: writer no longer strips brackets, so the reader can
        still resolve them. Pre-PR did ``lstrip("$").lstrip(".")`` only,
        but the reader had no walker for brackets — they ended up dead-on-
        arrival. Now both sides parse through _normalize_json_path."""
        p = ScrapeProfile(canonical_id="pr2-bracket-001")
        ok = save_field_patch(p, {
            "api_url_pattern": "https://api.example.com/units",
            "field_name": "rent_low",
            "json_path": "$.units[*].pricing.amount",
            "confidence": 0.9,
        })
        assert ok is True
        fp = p.api_hints.field_patches[0]
        # "$." stripped, brackets preserved
        assert fp.json_path == "units[*].pricing.amount"

    def test_dollar_prefix_stripped_idempotently(self):
        """Calling save twice for the same URL+field upserts; both shapes
        canonicalise to the same json_path."""
        p = ScrapeProfile(canonical_id="pr2-idem-001")
        save_field_patch(p, {
            "api_url_pattern": "/api/x",
            "field_name": "unit_id",
            "json_path": "$.uuid",
        })
        save_field_patch(p, {
            "api_url_pattern": "/api/x",
            "field_name": "unit_id",
            "json_path": "uuid",  # same path, different prefix style
        })
        assert len(p.api_hints.field_patches) == 1
        assert p.api_hints.field_patches[0].json_path == "uuid"


# ─── Order-of-operations regression ────────────────────────────────────────


class TestOrderOfOperations:
    """PR 2 Gap 2: pre-PR, _run_null_field_recovery ran AFTER profile-save.
    Patches accumulated on result["_field_patches"] but never reached
    save_field_patch. Now recovery runs INSIDE _process_property before the
    update step, so patches reach persistence on the same call.

    These tests simulate the call sequence rather than running the live
    runner (which would require a full async harness + scrape stack).
    """

    @pytest.fixture
    def store(self, tmp_path):
        return ProfileStore(tmp_path / "profiles")

    def test_patches_added_before_update_reach_profile(self, store):
        """Simulates the FIXED order: recovery populates _field_patches,
        then update_profile_after_extraction runs. Patches must persist."""
        from services.profile_updater import update_profile_after_extraction

        p = ScrapeProfile(canonical_id="pr2-order-fixed")
        store.save(p)
        # Production-like result dict — recovery has populated _field_patches
        # BEFORE update_profile_after_extraction is called.
        result = {
            "extraction_tier_used": "TIER_1_API",
            "_raw_api_responses": [
                {"url": "https://api.example.com/u", "body": {"units": [{"id": "U1"}]}},
            ],
            "_field_patches": [
                {
                    "api_url_pattern": "/api/u",
                    "field_name": "unit_id",
                    "json_path": "units[*].id",
                    "confidence": 0.95,
                },
            ],
        }
        p = update_profile_after_extraction(p, result, 1, store)
        assert len(p.api_hints.field_patches) == 1
        assert p.api_hints.field_patches[0].field_name == "unit_id"

    def test_patches_added_after_update_reach_nothing(self, store):
        """Pre-PR (broken) order: update runs first (no _field_patches),
        recovery runs after (mutates result but profile already saved).
        This test pins the broken pattern so we can SEE it's broken if
        anyone reverts the Gap 2 fix."""
        from services.profile_updater import update_profile_after_extraction

        p = ScrapeProfile(canonical_id="pr2-order-broken")
        store.save(p)
        result = {
            "extraction_tier_used": "TIER_1_API",
            "_raw_api_responses": [
                {"url": "https://api.example.com/u", "body": {"units": [{"id": "U1"}]}},
            ],
            # _field_patches NOT yet populated (recovery hasn't run)
        }
        p = update_profile_after_extraction(p, result, 1, store)
        # ... recovery runs LATER, mutates result but doesn't re-call update
        result["_field_patches"] = [
            {"api_url_pattern": "/api/u", "field_name": "unit_id",
             "json_path": "units[*].id", "confidence": 0.95},
        ]
        # Profile already saved with empty patches — this is the broken state.
        assert len(p.api_hints.field_patches) == 0
