from __future__ import annotations

import json

import pytest

from ma_poc.models.scrape_profile import ApiEndpoint, ProfileMaturity, ScrapeProfile
from ma_poc.scripts.backfills.promote_strict_canary_profiles import (
    FrozenProfile,
    merge_reusable_routes,
    parse_gcs_uri,
    profile_is_actionable,
    reusable_route_signals,
    strict_success,
    verify_expected_target_plan,
    write_snapshot,
)


def _profile(canonical_id: str = "101") -> ScrapeProfile:
    return ScrapeProfile(canonical_id=canonical_id)


def test_strict_success_requires_verdict_units_and_real_identity() -> None:
    row = {
        "units": [{"unit_number": "A-1"}],
        "_meta": {
            "verdict": "SUCCESS",
            "provenance": {"data_quality": {"real_id_units": 1}},
        },
    }
    assert strict_success(row)
    assert not strict_success({**row, "units": []})
    assert not strict_success(
        {
            **row,
            "_meta": {
                "verdict": "SUCCESS_PLAN_LEVEL",
                "provenance": {"data_quality": {"real_id_units": 1}},
            },
        }
    )
    assert not strict_success(
        {
            **row,
            "_meta": {
                "verdict": "SUCCESS",
                "provenance": {"data_quality": {"real_id_units": 0}},
            },
        }
    )


def test_bootstrap_platform_is_not_an_actionable_route() -> None:
    profile = _profile()
    profile.dom_hints.platform_detected = "entrata"
    assert reusable_route_signals(profile) == ()
    assert profile_is_actionable(profile) is False


def test_route_fields_are_actionable() -> None:
    profile = _profile()
    profile.navigation.availability_links = ["https://example.test/floorplans"]
    profile.api_hints.known_endpoints = [ApiEndpoint(url_pattern="https://api.example.test/units")]
    assert reusable_route_signals(profile) == (
        "availability_links",
        "known_endpoints",
    )


def test_merge_preserves_existing_winner_and_appends_canary_winner() -> None:
    target = _profile()
    target.navigation.winning_page_url = "https://example.test/organic"
    target.confidence.maturity = ProfileMaturity.HOT
    source = _profile()
    source.navigation.winning_page_url = "https://example.test/canary"
    source.navigation.availability_page_path = "/canary"
    source.confidence.preferred_tier = 1

    assert merge_reusable_routes(target, source) is True
    assert target.navigation.winning_page_url == "https://example.test/organic"
    assert target.navigation.availability_links == ["https://example.test/canary"]
    assert target.navigation.availability_page_path == "/canary"
    assert target.confidence.maturity == ProfileMaturity.HOT
    assert target.confidence.preferred_tier == 1


def test_merge_deduplicates_endpoints_and_does_not_replace_provider() -> None:
    target = _profile()
    target.api_hints.api_provider = "organic"
    target.api_hints.known_endpoints = [ApiEndpoint(url_pattern="https://api.example.test/units")]
    source = _profile()
    source.api_hints.api_provider = "canary"
    source.api_hints.known_endpoints = [
        ApiEndpoint(url_pattern="https://api.example.test/units"),
        ApiEndpoint(url_pattern="https://api.example.test/units-v2"),
    ]

    assert merge_reusable_routes(target, source) is True
    assert target.api_hints.api_provider == "organic"
    assert [endpoint.url_pattern for endpoint in target.api_hints.known_endpoints] == [
        "https://api.example.test/units",
        "https://api.example.test/units-v2",
    ]


def test_merge_is_idempotent_after_first_application() -> None:
    target = _profile()
    source = _profile()
    source.navigation.winning_page_url = "https://example.test/floorplans"
    source.api_hints.widget_endpoints = ["https://example.test/widget"]

    assert merge_reusable_routes(target, source) is True
    assert merge_reusable_routes(target, source) is False


def test_merge_rejects_cross_property_profile() -> None:
    with pytest.raises(ValueError, match="canonical_id_mismatch"):
        merge_reusable_routes(_profile("1"), _profile("2"))


def test_parse_gcs_uri_normalizes_prefix() -> None:
    assert parse_gcs_uri("gs://bucket/profiles/run") == (
        "bucket",
        "profiles/run/",
    )
    with pytest.raises(ValueError, match="not_a_gcs_uri"):
        parse_gcs_uri("https://bucket/profiles")


def test_execute_snapshot_does_not_overwrite_reviewed_dry_run(tmp_path) -> None:
    frozen = {
        101: FrozenProfile(
            property_id=101,
            raw=b"{}",
            generation=7,
            sha256="sha",
            route_signals=("winning_page_url",),
        )
    }
    dry_run = write_snapshot(tmp_path, frozen, {"mode": "dry_run"})
    execute = write_snapshot(tmp_path, frozen, {"mode": "execute"})

    assert dry_run.name == "promotion_manifest.json"
    assert execute.name == "promotion_manifest_execute.json"
    assert dry_run.read_text(encoding="utf-8") != execute.read_text(encoding="utf-8")


def test_reviewed_target_create_merge_split_is_generation_guarded(tmp_path) -> None:
    manifest = tmp_path / "promotion_manifest.json"
    manifest.write_text(
        json.dumps({"create_ids": [101, 102], "overlap_ids": [103]}),
        encoding="utf-8",
    )

    verify_expected_target_plan(manifest, {101, 102, 103}, {103, 999})
    with pytest.raises(RuntimeError, match="target_profile_plan_changed_since_review"):
        verify_expected_target_plan(manifest, {101, 102, 103}, {102, 103})
