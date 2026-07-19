"""Tests for profile updater — claude-scrapper-arch.md Step 6.4."""

from __future__ import annotations

import pytest

from models.scrape_profile import ProfileMaturity, ScrapeProfile
from services.profile_store import ProfileStore
from services.profile_updater import (
    _base_tier_num,
    _compute_quality_signals,
    update_profile_after_extraction,
)


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / "profiles")


def test_update_after_tier1_success_records_api_urls(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="t1-001")
    store.save(p)
    result = {
        "extraction_tier_used": "TIER_1_API",
        "_raw_api_responses": [
            {"url": "/api/units", "body": {"units": [{"rent": 1200}]}},
        ],
    }
    updated = update_profile_after_extraction(p, result, 5, store)
    assert len(updated.api_hints.known_endpoints) == 1
    assert updated.api_hints.known_endpoints[0].url_pattern == "/api/units"


def test_update_after_llm_success_writes_css_selectors(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="llm-001")
    store.save(p)
    result = {
        "extraction_tier_used": "TIER_4_LLM",
        "_llm_hints": {
            "css_selectors": {"container": ".unit-row", "rent": ".price"},
            "platform_guess": "entrata",
            "field_mapping_notes": "Data in table rows",
        },
    }
    updated = update_profile_after_extraction(p, result, 10, store)
    assert updated.dom_hints.field_selectors.container == ".unit-row"
    assert updated.dom_hints.field_selectors.rent == ".price"
    assert updated.dom_hints.platform_detected == "entrata"


def test_update_after_llm_success_writes_json_paths(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="llm-002")
    store.save(p)
    result = {
        "extraction_tier_used": "TIER_4_LLM",
        "_llm_hints": {
            "api_urls_with_data": ["/api/v1/units"],
            "json_paths": {"rent": "$.data.rent", "unit_id": "$.data.id"},
        },
    }
    updated = update_profile_after_extraction(p, result, 5, store)
    assert len(updated.api_hints.known_endpoints) == 1
    assert updated.api_hints.known_endpoints[0].json_paths["rent"] == "$.data.rent"


def test_maturity_promotion_cold_to_warm_after_1_success(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="promo-001")
    store.save(p)
    result = {"extraction_tier_used": "TIER_3_DOM"}
    updated = update_profile_after_extraction(p, result, 5, store)
    assert updated.confidence.maturity == ProfileMaturity.WARM


def test_maturity_promotion_warm_to_hot_after_3_successes(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="promo-002")
    store.save(p)
    result = {"extraction_tier_used": "TIER_1_API", "_raw_api_responses": []}
    for _ in range(3):
        p = update_profile_after_extraction(p, result, 10, store)
    assert p.confidence.maturity == ProfileMaturity.HOT
    assert p.confidence.consecutive_successes == 3


def test_consecutive_failures_resets_on_success(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="reset-001")
    p.confidence.consecutive_failures = 5
    store.save(p)
    result = {"extraction_tier_used": "TIER_3_DOM"}
    updated = update_profile_after_extraction(p, result, 3, store)
    assert updated.confidence.consecutive_failures == 0
    assert updated.confidence.consecutive_successes == 1


def test_navigation_hints_recorded_from_crawled_urls(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="nav-001")
    store.save(p)
    result = {
        "extraction_tier_used": "TIER_3_DOM",
        "property_links_crawled": [
            "https://example.com/gallery",
            "https://example.com/floor-plans",
            "https://example.com/contact",
        ],
    }
    updated = update_profile_after_extraction(p, result, 5, store)
    assert updated.navigation.availability_page_path == "/floor-plans"


# ── 2026-07-19: suffixed-tier persistence fix (writer tier-string mismatch) ──


@pytest.mark.parametrize(
    "tier,expected",
    [
        ("TIER_1_API", 1),                       # bare — exact map
        ("TIER_1_API_ENTRATA", 1),               # suffixed API
        ("TIER_1_KNOCK_API", 1),
        ("TIER_1_API_RENTCAFE_SECURECAFE", 1),
        ("TIER_1_DOM_CAMDEN", 1),
        ("TIER_3_DOM", 3),                       # bare
        ("TIER_1_DOM_ENTRATA_PP_SSR", 1),
        ("TIER_MERGED_CROSS_PAGE", None),        # no TIER_<n> leading token
        ("generic:no_body_short_circuit", None),
        ("", None),
        (None, None),
    ],
)
def test_base_tier_num_tolerates_suffixes(tier, expected) -> None:
    assert _base_tier_num(tier) == expected


def test_suffixed_api_success_persists_preferred_and_endpoints(store: ProfileStore) -> None:
    """A suffixed TIER_1_API_* win must now persist preferred_tier +
    last_success_tier AND capture known_endpoints (pre-fix: all skipped)."""
    p = ScrapeProfile(canonical_id="sfx-001")
    store.save(p)
    result = {
        "extraction_tier_used": "TIER_1_API_RENTCAFE_SECURECAFE",
        "_raw_api_responses": [
            {"url": "https://x.securecafe.com/api/units", "body": {"units": [{"rent": 1200}]}},
        ],
    }
    up = update_profile_after_extraction(p, result, 44, store)
    assert up.confidence.preferred_tier == 1
    assert up.confidence.last_success_tier == 1
    assert [e.url_pattern for e in up.api_hints.known_endpoints] == [
        "https://x.securecafe.com/api/units"
    ]


def test_suffixed_dom_success_persists_preferred_tier(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="sfx-002")
    store.save(p)
    result = {"extraction_tier_used": "TIER_1_DOM_CAMDEN"}
    up = update_profile_after_extraction(p, result, 6, store)
    # TIER_1_DOM_* resolves to family 1; preferred_tier now set (was null pre-fix)
    assert up.confidence.preferred_tier == 1


def test_seeded_preferred_tier_never_raised_by_lower_win(store: ProfileStore) -> None:
    """Regression: preferred_tier only lowers — a later higher-tier win must not
    overwrite a seeded/earned tier-1 (protects seeds)."""
    p = ScrapeProfile(canonical_id="sfx-003")
    p.confidence.preferred_tier = 1
    store.save(p)
    up = update_profile_after_extraction(p, {"extraction_tier_used": "TIER_4_LLM_DOM"}, 5, store)
    assert up.confidence.preferred_tier == 1


# ── 2026-07-19: quality-aware learning (#2) ──


def test_compute_quality_unit_level() -> None:
    units = [
        {"unit_number": "101", "market_rent_low": 1200},
        {"unit_number": "102", "market_rent_low": 1300},
    ]
    ul, pl, cov, rent, flag = _compute_quality_signals(units, expected=2)
    assert (ul, pl, flag) == (2, 0, "UNIT_LEVEL")
    assert cov == 1.0 and rent == 1.0


def test_compute_quality_plan_level() -> None:
    units = [{"unit_number": "", "floor_plan_name": "A", "market_rent_low": 1200}]
    ul, pl, _cov, _rent, flag = _compute_quality_signals(units, expected=None)
    assert (ul, pl, flag) == (0, 1, "PLAN_LEVEL")


def test_compute_quality_contaminated_pmc_dump() -> None:
    # 200 units where only ~20 expected → PMC-wide contamination (AppFolio class)
    units = [{"unit_number": str(i)} for i in range(200)]
    _ul, _pl, _cov, _rent, flag = _compute_quality_signals(units, expected=20)
    assert flag == "CONTAMINATED"


def test_compute_quality_thin_under_extraction() -> None:
    units = [{"unit_number": "1", "market_rent_low": 1000}]
    _ul, _pl, cov, _rent, flag = _compute_quality_signals(units, expected=100)
    assert flag == "THIN" and cov is not None and cov < 0.3


def test_compute_quality_rent_present_ratio() -> None:
    units = [{"unit_number": "1", "market_rent_low": 1000}, {"unit_number": "2"}]
    _ul, _pl, _cov, rent, _flag = _compute_quality_signals(units, expected=None)
    assert rent == 0.5


def test_writer_records_quality_and_plan_streak(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="q-001")
    store.save(p)
    plan = {
        "extraction_tier_used": "TIER_1_DOM_GENERIC_PLAN_TEXT",
        "units": [{"unit_number": "", "market_rent_low": 1200}],
    }
    up = update_profile_after_extraction(p, plan, 1, store)
    assert up.quality.last_quality_flag == "PLAN_LEVEL"
    assert up.quality.consecutive_plan_level == 1
    # a second plan-level success advances the upgrade-opportunity streak
    up = update_profile_after_extraction(up, plan, 1, store)
    assert up.quality.consecutive_plan_level == 2
    # a unit-level success resets the streak and records clean gold
    unit = {
        "extraction_tier_used": "TIER_1_API_ENTRATA",
        "units": [{"unit_number": "101", "market_rent_low": 1200}],
    }
    up = update_profile_after_extraction(up, unit, 1, store)
    assert up.quality.last_quality_flag == "UNIT_LEVEL"
    assert up.quality.consecutive_plan_level == 0
    assert up.quality.last_unit_level_count == 1


def test_quality_survives_round_trip(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="q-002")
    store.save(p)
    r = {
        "extraction_tier_used": "TIER_1_API_ENTRATA",
        "units": [{"unit_number": "5", "market_rent_low": 1400}],
        "_expected_total_units": 1,
    }
    update_profile_after_extraction(p, r, 1, store)
    loaded = ProfileStore(store._base).load("q-002")  # type: ignore[attr-defined]
    assert loaded is not None
    assert loaded.quality.last_quality_flag == "UNIT_LEVEL"
    assert loaded.quality.last_coverage_ratio == 1.0
