"""Phase C tests — _resolve_source_url + _field_patches emission."""

from __future__ import annotations

import pytest

from models.scrape_profile import ScrapeProfile
from services.profile_store import ProfileStore
from services.profile_updater import update_profile_after_extraction


def test_c1_resolve_source_url_picks_right_response() -> None:
    """_resolve_source_url must return the response whose body contains
    the target unit's identifiers, not blindly pick index 0."""
    raw_apis = [
        {"url": "https://x.com/api/community-info", "body": {"name": "Foo"}},
        {"url": "https://x.com/api/units", "body": [{"id": "U1", "rent": 1500}]},
    ]
    target = {"unit_id": "U1"}
    from ma_poc.scripts.runners.jugnu import _resolve_source_url

    url, body = _resolve_source_url(raw_apis, target)
    assert url == "https://x.com/api/units"
    assert body == [{"id": "U1", "rent": 1500}]


def test_c1_resolve_source_url_fallback_to_first_nonempty() -> None:
    """When no response contains the needle, fall back to the first non-empty."""
    from ma_poc.scripts.runners.jugnu import _resolve_source_url

    raw_apis = [
        {"url": "https://x.com/api/info", "body": {"name": "X"}},
        {"url": "https://x.com/api/units", "body": [{"id": "U99"}]},
    ]
    url, body = _resolve_source_url(raw_apis, {"unit_id": "UNKNOWN"})
    # Should fall back to first non-empty body
    assert url == "https://x.com/api/info"


def test_c1_resolve_source_url_empty_raw_apis() -> None:
    from ma_poc.scripts.runners.jugnu import _resolve_source_url

    url, body = _resolve_source_url([], {"unit_id": "U1"})
    assert url == ""
    assert body == {}


def test_c2_field_patches_persisted_via_profile_updater(tmp_path) -> None:
    """When scrape_result contains _field_patches, they must be persisted
    into profile.api_hints.field_patches by update_profile_after_extraction."""
    store = ProfileStore(tmp_path / "profiles")
    p = ScrapeProfile(canonical_id="phase_c_001")
    store.save(p)

    scrape_result = {
        "extraction_tier_used": "TIER_4_LLM",
        "_field_patches": [
            {
                "api_url_pattern": "/api/units",
                "field_name": "rent_low",
                "json_path": "rent_range.min",
                "confidence": 0.92,
                "_envelope_hash": "abc1234567890def",
            }
        ],
    }
    p = update_profile_after_extraction(p, scrape_result, 5, store)
    assert len(p.api_hints.field_patches) == 1
    assert p.api_hints.field_patches[0].field_name == "rent_low"
    assert p.api_hints.field_patches[0].json_path == "rent_range.min"


def test_url_pattern_from_strips_scheme_and_host() -> None:
    from ma_poc.scripts.runners.jugnu import _url_pattern_from

    assert _url_pattern_from("https://example.com/api/v1/units?page=1") == "/api/v1/units"
    assert _url_pattern_from("/api/units") == "/api/units"
    assert _url_pattern_from("") == ""
