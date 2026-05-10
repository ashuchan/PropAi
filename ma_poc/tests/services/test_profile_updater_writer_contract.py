"""Hard CI gate: every writer in update_profile_after_extraction must persist.

This test exists to catch the c3a8b8b-class regression where a writer ships
without its end-to-end wiring and silently drops every record (the
2026-05-10 regression: 5,054 profiles with 3 mappings and 0 patches in the DB
because writers ran but produced nothing).

When you add a new writer to ``services.profile_updater.update_profile_after_extraction``
or to a Pydantic field on ``ScrapeProfile`` that's intended to persist, you
MUST extend the fixture below to exercise it AND the assertions below to
verify it round-trips. Adding a slot without an assertion is by design a
build break — the test will still pass but the fixture's growth makes the
omission visible in PR review.

Synthesised fixtures use shapes drawn from real 2026-05-10 cloud-run data:
- pid 11327 (planoparktownhomes.com): TIER_4_LLM_API winner shape — non-empty
  json_paths + url + 9 units extracted.
- pid 53592 (1701arch.com): rescue + noise classification shape.
- pid 1783 (gscapts.com): css_selectors + navigation_hint shape.
"""

from __future__ import annotations

import pytest

from models.scrape_profile import ScrapeProfile
from services.profile_store import ProfileStore
from services.profile_updater import update_profile_after_extraction


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / "profiles")


@pytest.fixture
def real_world_scrape_result() -> dict:
    """A scrape_result that exercises every writer in update_profile_after_extraction.

    Field shapes are lifted from real 2026-05-10 cloud-run artifacts so any
    future Pydantic / serializer drift is caught against the real data the
    LLM and adapters actually produce in production.
    """
    return {
        # Top-level outcome (drives maturity + tier rotation)
        "extraction_tier_used": "TIER_4_LLM_API",
        "units": [
            {"unit_id": "U1", "rent_low": 1500, "rent_high": 1500, "bedrooms": 1},
            {"unit_id": "U2", "rent_low": 1800, "rent_high": 1850, "bedrooms": 2},
        ],
        # Channel: known_endpoints (writes via _llm_hints.api_urls_with_data)
        "_llm_hints": {
            "api_urls_with_data": [
                "https://www.planoparktownhomes.com/api/v3/floorplans/all/",
            ],
            # Channel: dom_hints.field_selectors (writes when css_selectors present)
            "css_selectors": {
                "container": ".unit-card",
                "rent": ".rent-price",
                "sqft": ".unit-sqft",
            },
            "css_selectors_quality": 1.0,
            "platform_guess": "custom",
            # Channel: navigation.last_navigation_hints
            "navigation_hint": "/floorplans",
            # Channel: api_hints.json_paths (also indexed via known_endpoints)
            "json_paths": {
                "rent_low": "minRent",
                "unit_id": "unitNumber",
            },
        },
        # Channel: llm_field_mappings (writes via _llm_analysis_results)
        "_llm_analysis_results": {
            # Mapping shape — lifted from pid 11327's real LLM-API response
            "https://www.planoparktownhomes.com/api/v3/floorplans/all/?api_key=c19c32fb315ddbf814b8620b497b1ec740a2fec1": {
                "api_url_pattern": "https://www.planoparktownhomes.com/api/v3/floorplans/all/?api_key=c19c32fb315ddbf814b8620b497b1ec740a2fec1",
                "json_paths": {
                    "unit_id": "unitNumber",
                    "rent_low": "minRent",
                    "rent_high": "maxRent",
                    "bedrooms": "bedCount",
                    "sqft": "sqFt",
                },
                "response_envelope": "",
            },
            # Channel: blocked_endpoints (writes via noise: prefix)
            "https://bam.nr-data.net/1/NRBR-noise": "noise:analytics",
        },
        # Channel: navigation.explored_links (writes from _explored_links)
        "_explored_links": {
            "https://www.planoparktownhomes.com/floorplans": True,
            "https://www.planoparktownhomes.com/contact": False,
        },
        # Channel: known_endpoints (writes from _raw_api_responses)
        "_raw_api_responses": [
            {
                "url": "https://www.planoparktownhomes.com/api/v3/floorplans/all/",
                "body": {"floorplans": [{"id": 1, "rent": 1500}]},
            },
        ],
    }


def test_full_writer_contract(
    store: ProfileStore, real_world_scrape_result: dict
) -> None:
    """ALL writers in update_profile_after_extraction must persist their channel.

    This is a hard CI gate. Failing means a writer is broken or unwired —
    exactly the c3a8b8b regression shape. Do not xfail or skip; fix the
    writer.

    PR 1 scope: mappings + dom_selectors + blocked + known + explored.
    PR 2 will extend with field_patches.
    PR 7 will extend with navigation_hint persistence assertion.
    """
    p = ScrapeProfile(canonical_id="contract-001")
    store.save(p)

    p = update_profile_after_extraction(p, real_world_scrape_result, 2, store)

    # Channel 1 — llm_field_mappings (the channel this PR repairs)
    assert len(p.api_hints.llm_field_mappings) > 0, (
        "REGRESSION: llm_field_mappings writer broke. The classifier "
        "didn't recognise the mapping shape OR save_llm_field_mapping "
        "early-returned. Re-run pre-impl trace from PR 1 docs."
    )
    mapping = p.api_hints.llm_field_mappings[0]
    assert mapping.api_url_pattern.startswith("https://www.planoparktownhomes.com/")
    assert "rent_low" in mapping.json_paths

    # Channel 2 — blocked_endpoints
    assert any(
        "bam.nr-data.net" in b.url_pattern for b in p.api_hints.blocked_endpoints
    ), "REGRESSION: blocked_endpoints writer broke."

    # Channel 3 — known_endpoints
    assert any(
        "planoparktownhomes.com" in k.url_pattern for k in p.api_hints.known_endpoints
    ), "REGRESSION: known_endpoints writer broke."

    # Channel 4 — dom_hints.field_selectors
    assert p.dom_hints.field_selectors is not None
    assert p.dom_hints.field_selectors.container == ".unit-card", (
        "REGRESSION: dom_hints.field_selectors writer broke."
    )

    # Channel 5 — navigation.explored_links
    assert any(
        "/floorplans" in url for url in p.navigation.explored_links
    ) or any(
        "/contact" in url for url in p.navigation.explored_links
    ), "REGRESSION: navigation.explored_links writer broke."


def test_full_writer_contract_survives_round_trip(
    store: ProfileStore, real_world_scrape_result: dict, tmp_path
) -> None:
    """The same contract — but verified after a save/load round-trip.

    This catches Pydantic/JSON-serializer drift that wouldn't show up
    in the in-memory test above. If a Pydantic field stops round-tripping
    through .model_dump() / .model_validate(), this test fails before the
    sentinel probe ever fires in production.
    """
    p = ScrapeProfile(canonical_id="contract-002")
    store.save(p)
    p = update_profile_after_extraction(p, real_world_scrape_result, 2, store)
    store.save(p)

    # Read back from disk via a fresh store — proves the wire-format
    # survives a serializer cycle.
    fresh_store = ProfileStore(store._base)  # type: ignore[attr-defined]
    loaded = fresh_store.load("contract-002")
    assert loaded is not None

    # Re-run the same channel assertions against the loaded profile.
    assert len(loaded.api_hints.llm_field_mappings) > 0
    assert any(
        "bam.nr-data.net" in b.url_pattern for b in loaded.api_hints.blocked_endpoints
    )
    assert any(
        "planoparktownhomes.com" in k.url_pattern for k in loaded.api_hints.known_endpoints
    )
    assert loaded.dom_hints.field_selectors is not None
    assert loaded.dom_hints.field_selectors.container == ".unit-card"
