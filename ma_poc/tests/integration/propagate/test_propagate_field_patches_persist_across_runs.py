"""Propagate layer: LLM-discovered field mappings are saved and not re-learned.

`save_llm_field_mapping()` persists a json_paths mapping to the profile.
On a subsequent run the mapping is already present so no LLM call is needed
to re-discover it (the cascade replays the stored mapping instead).
"""

from __future__ import annotations

from models.scrape_profile import ScrapeProfile
from services.profile_updater import save_llm_field_mapping


# ---------------------------------------------------------------------------
# Mapping persistence
# ---------------------------------------------------------------------------


def test_field_mapping_saved_to_profile() -> None:
    """save_llm_field_mapping() appends to profile.api_hints.llm_field_mappings."""
    profile = ScrapeProfile(canonical_id="prop-fp-001")

    mapping = {
        "api_url_pattern": "https://example.com/api/floorplans",
        "json_paths": {"bedrooms": "beds", "market_rent_low": "minRent", "floor_plan_name": "name"},
        "response_envelope": "data.units",
    }
    saved = save_llm_field_mapping(profile, mapping)

    assert saved is True
    assert len(profile.api_hints.llm_field_mappings) == 1
    m = profile.api_hints.llm_field_mappings[0]
    # normalize_url_pattern (added in main PR5) strips the scheme so patterns
    # survive api_key rotation and scheme changes between runs.
    assert "example.com/api/floorplans" in m.api_url_pattern
    assert m.json_paths.get("bedrooms") == "beds"
    assert m.response_envelope == "data.units"


def test_field_mapping_upserted_on_resave() -> None:
    """Re-saving a mapping for the same URL updates it in place (no duplicate)."""
    profile = ScrapeProfile(canonical_id="prop-fp-002")
    base = {
        "api_url_pattern": "https://example.com/api/units",
        "json_paths": {"bedrooms": "beds"},
    }
    save_llm_field_mapping(profile, base)
    assert len(profile.api_hints.llm_field_mappings) == 1

    updated = {
        "api_url_pattern": "https://example.com/api/units",
        "json_paths": {"bedrooms": "num_bedrooms", "market_rent_low": "price"},
    }
    save_llm_field_mapping(profile, updated)

    assert len(profile.api_hints.llm_field_mappings) == 1
    assert profile.api_hints.llm_field_mappings[0].json_paths.get("bedrooms") == "num_bedrooms"


def test_multiple_different_urls_each_get_own_entry() -> None:
    """Mappings for distinct URLs are stored as separate entries."""
    profile = ScrapeProfile(canonical_id="prop-fp-003")

    for i in range(3):
        save_llm_field_mapping(profile, {
            "api_url_pattern": f"https://example.com/api/endpoint{i}",
            "json_paths": {"bedrooms": f"beds_{i}"},
        })

    assert len(profile.api_hints.llm_field_mappings) == 3


def test_empty_json_paths_rejected() -> None:
    """Mappings with empty json_paths are rejected (return False)."""
    profile = ScrapeProfile(canonical_id="prop-fp-004")

    saved = save_llm_field_mapping(profile, {
        "api_url_pattern": "https://example.com/api/units",
        "json_paths": {},
    })

    assert saved is False
    assert len(profile.api_hints.llm_field_mappings) == 0


def test_empty_url_pattern_rejected() -> None:
    """Mappings with empty api_url_pattern are rejected (return False)."""
    profile = ScrapeProfile(canonical_id="prop-fp-005")

    saved = save_llm_field_mapping(profile, {
        "api_url_pattern": "",
        "json_paths": {"bedrooms": "beds"},
    })

    assert saved is False
    assert len(profile.api_hints.llm_field_mappings) == 0


def test_mappings_cap_at_20_entries() -> None:
    """Profile caps llm_field_mappings at 20; oldest entries are evicted."""
    profile = ScrapeProfile(canonical_id="prop-fp-006")

    for i in range(25):
        save_llm_field_mapping(profile, {
            "api_url_pattern": f"https://example.com/api/ep{i:03d}",
            "json_paths": {"bedrooms": f"beds_{i}"},
        })

    assert len(profile.api_hints.llm_field_mappings) <= 20
