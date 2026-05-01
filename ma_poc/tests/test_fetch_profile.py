"""Tests for FetchProfile model and its embedding in ScrapeProfile — Phase E1."""

from __future__ import annotations

import json

from ma_poc.models.fetch_tier import FetchTier
from ma_poc.models.scrape_profile import FetchProfile, ScrapeProfile


def test_default_fetch_profile_floor_is_direct() -> None:
    """A freshly constructed FetchProfile must default to DIRECT tier."""
    fp = FetchProfile()
    assert fp.tier_floor == FetchTier.DIRECT
    assert fp.tier_floor == 0


def test_fetch_profile_serialization_roundtrip() -> None:
    """model_dump(mode='json') → model_validate() must produce an equal model."""
    fp = FetchProfile(
        tier_floor=FetchTier.DC_PROXY,
        last_success_tier=FetchTier.DIRECT,
        consecutive_successes_at_floor=3,
        total_escalations=1,
    )
    dumped = fp.model_dump(mode="json")
    json_str = json.dumps(dumped)
    restored = FetchProfile.model_validate(json.loads(json_str))

    assert restored.tier_floor == fp.tier_floor
    assert restored.last_success_tier == fp.last_success_tier
    assert restored.consecutive_successes_at_floor == fp.consecutive_successes_at_floor
    assert restored.total_escalations == fp.total_escalations


def test_fetch_profile_embedded_in_scrape_profile() -> None:
    """ScrapeProfile must expose a .fetch attribute defaulting to FetchProfile()."""
    sp = ScrapeProfile(canonical_id="test-embed-001")
    assert hasattr(sp, "fetch")
    assert isinstance(sp.fetch, FetchProfile)
    assert sp.fetch.tier_floor == FetchTier.DIRECT


def test_legacy_profile_loads_with_default_fetch() -> None:
    """A JSON blob without a 'fetch' key must load and use fetch=FetchProfile()."""
    legacy_json = json.dumps(
        {
            "canonical_id": "legacy-001",
            "version": 2,
            "schema_version": "v2",
        }
    )
    sp = ScrapeProfile.model_validate(json.loads(legacy_json))
    assert sp.fetch.tier_floor == FetchTier.DIRECT
    assert sp.fetch.consecutive_failures_at_floor == 0
