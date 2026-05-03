"""Phase D tests — record_source_observations wiring + _merged_units plumbing."""

from __future__ import annotations

import pytest

from models.scrape_profile import ScrapeProfile
from models.source import FieldValue, ProvenancedUnit, SourceId
from services.profile_store import ProfileStore
from services.profile_updater import update_profile_after_extraction


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / "profiles")


def test_d1_record_source_observations_called_after_merge(store: ProfileStore) -> None:
    """When _merged_units is provided, source observations must be written."""
    p = ScrapeProfile(canonical_id="phase_d_001")
    store.save(p)

    pu = ProvenancedUnit()
    pu["unit_id"] = FieldValue(value="U1", source=SourceId.API_SIGHTMAP, confidence=0.95)
    pu["asking_rent"] = FieldValue(value=1500, source=SourceId.FIELD_PATCH, confidence=0.85)

    scrape_result = {
        "extraction_tier_used": "TIER_MERGED_DETERMINISTIC",
        "units": [{"unit_id": "U1", "asking_rent": 1500}],
        "_merged_units": [pu],
        "_sources": [],
    }
    p = update_profile_after_extraction(p, scrape_result, 1, store)

    by_key = {
        (o.source_id, o.field_group): o
        for o in p.api_hints.source_observations
    }
    assert (SourceId.API_SIGHTMAP.value, "identity") in by_key, (
        "API_SIGHTMAP should be recorded for identity group"
    )
    assert (SourceId.FIELD_PATCH.value, "transactional") in by_key, (
        "FIELD_PATCH should be recorded for transactional group"
    )


def test_d1_no_merged_units_no_observations(store: ProfileStore) -> None:
    """When _merged_units is absent, no observations should be written."""
    p = ScrapeProfile(canonical_id="phase_d_002")
    store.save(p)

    scrape_result = {
        "extraction_tier_used": "TIER_1_API",
        "units": [{"unit_id": "U1"}],
    }
    p = update_profile_after_extraction(p, scrape_result, 1, store)
    assert len(p.api_hints.source_observations) == 0


def test_d2_last_sources_run_persisted(store: ProfileStore) -> None:
    """When _sources is populated, last_sources_run must be written."""
    from models.source import ExtractedSource

    p = ScrapeProfile(canonical_id="phase_d_003")
    store.save(p)

    src = ExtractedSource(
        source_id=SourceId.MAPPING_REPLAY,
        source_url="https://x.com",
        envelope_hash="h1",
        units=[],
    )
    scrape_result = {
        "extraction_tier_used": "TIER_MERGED_DETERMINISTIC",
        "units": [],
        "_merged_units": [],
        "_sources": [src],
    }
    p = update_profile_after_extraction(p, scrape_result, 0, store)
    assert SourceId.MAPPING_REPLAY.value in p.confidence.last_sources_run
