"""Tests for the step 5 profile self-learning loop wired into jugnu_runner.

Covers:
  - _SimpleProfileStore now backs onto services.profile_store.ProfileStore
    (load returns ScrapeProfile, not a dict)
  - bootstrap() creates a COLD profile from URL-based detection
  - save() round-trips via the backing ProfileStore
  - update_profile_after_extraction is compatible with the Jugnu scrape
    result shape (no crash, promotes maturity on success)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make ma_poc/ importable so services.profile_store works when running
# these tests under the project venv.
_MA_POC = Path(__file__).resolve().parents[3]
if str(_MA_POC) not in sys.path:
    sys.path.insert(0, str(_MA_POC))


def test_simple_profile_store_backs_onto_real_profile_store(tmp_path: Path) -> None:
    from ma_poc.scripts.jugnu_runner import _SimpleProfileStore
    from models.scrape_profile import ProfileMaturity, ScrapeProfile

    store = _SimpleProfileStore(tmp_path)
    assert store.get_profile("missing") is None

    bootstrapped = store.bootstrap("cid-123", {}, "https://rentcafe.com/abc")
    assert bootstrapped is not None
    assert isinstance(bootstrapped, ScrapeProfile)
    assert bootstrapped.confidence.maturity == ProfileMaturity.COLD

    store.save(bootstrapped)
    # Round-trip.
    reloaded = store.get_profile("cid-123")
    assert reloaded is not None
    assert reloaded.canonical_id == "cid-123"


def test_update_profile_after_extraction_accepts_jugnu_result_shape(tmp_path: Path) -> None:
    """Profile updater should digest Jugnu's scrape result without crashing.

    This is the contract between scraper.py and services.profile_updater.
    If scraper.py ever stops populating _raw_api_responses / _winning_page_url
    / extraction_tier_used, this test will catch the drift.
    """
    from ma_poc.scripts.jugnu_runner import _SimpleProfileStore
    from models.scrape_profile import ProfileMaturity
    from services.profile_updater import update_profile_after_extraction

    store = _SimpleProfileStore(tmp_path)
    profile = store.bootstrap("cid-xyz", {}, "https://www.rentcafe.com/foo")
    assert profile is not None

    # Shape Jugnu's scraper.py actually produces after step 4.
    scrape_result = {
        "extraction_tier_used": "TIER_1_API",
        "units": [{"unit_number": "101"}, {"unit_number": "102"}],
        "_raw_api_responses": [
            {"url": "https://yardi.example.com/api/v1/floorplans",
             "body": [{"unit_number": "101", "rent": 1500},
                      {"unit_number": "102", "rent": 1600}]},
        ],
        "_winning_page_url": "https://yardi.example.com/api/v1/floorplans",
        "_llm_hints": None,
        "_llm_analysis_results": {},
        "_explored_links": {},
        "property_links_crawled": [],
    }

    updated = update_profile_after_extraction(
        profile, scrape_result, len(scrape_result["units"]), store.backing,
    )
    # After 1 success we should be WARM.
    assert updated.confidence.consecutive_successes == 1
    assert updated.confidence.maturity == ProfileMaturity.WARM
    # Winning URL recorded for next run.
    assert updated.navigation.winning_page_url == scrape_result["_winning_page_url"]
