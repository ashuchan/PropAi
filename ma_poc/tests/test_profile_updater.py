"""Tests for profile updater — claude-scrapper-arch.md Step 6.4."""

from __future__ import annotations

import pytest

from models.scrape_profile import ProfileMaturity, ScrapeProfile
from pms.source_provenance import sanitise_source_url
from services.profile_store import ProfileStore
from services.profile_updater import (
    _base_tier_num,
    _compute_quality_signals,
    _identity_admitted_unit_source_urls,
    _row_zip,
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


def _unit_source(
    url: str,
    *,
    identity_status: str = "MATCH",
    status: int = 200,
    unit_count: int = 4,
) -> dict:
    return {
        "provider": "test",
        "response_kind": "unit_roster",
        "source_url": url,
        "response_status": status,
        "response_sha256": "a" * 64,
        "unit_count": unit_count,
        "identity": {"status": identity_status},
    }


def test_exact_matched_unit_source_turns_bootstrap_success_into_warm_route(
    store: ProfileStore,
) -> None:
    p = ScrapeProfile(canonical_id="source-route-001")
    p.navigation.entry_url = "https://property.example/"
    store.save(p)
    source_url = "https://portal.example/api/units?property=7"
    result = {
        "extraction_tier_used": "TIER_1_API_VENDOR",
        "units": [{"unit_number": "101", "market_rent_low": 1200}],
        "_unit_source_provenance": [_unit_source(source_url, unit_count=1)],
    }

    updated = update_profile_after_extraction(p, result, 1, store)

    assert updated.navigation.winning_page_url == source_url
    assert updated.navigation.availability_page_path == "/api/units"
    assert updated.navigation.availability_links == [source_url]


def test_unknown_or_unhashed_unit_source_never_becomes_warm_state(
    store: ProfileStore,
) -> None:
    p = ScrapeProfile(canonical_id="source-route-002")
    store.save(p)
    unknown = _unit_source("https://wrong.example/api/units", identity_status="UNKNOWN")
    unhashed = _unit_source("https://property.example/api/units")
    unhashed["response_sha256"] = ""
    result = {
        "extraction_tier_used": "TIER_1_API_VENDOR",
        "units": [{"unit_number": "101", "market_rent_low": 1200}],
        "_unit_source_provenance": [unknown, unhashed],
    }

    updated = update_profile_after_extraction(p, result, 1, store)

    assert _identity_admitted_unit_source_urls(result) == []
    assert updated.navigation.winning_page_url is None
    assert updated.navigation.availability_links == []


def test_redacted_provenance_recovers_only_exact_in_memory_replay_url(
    store: ProfileStore,
) -> None:
    p = ScrapeProfile(canonical_id="source-route-redacted")
    store.save(p)
    raw_url = "https://portal.example/api/units?api_key=public-123&property=7"
    safe_url = sanitise_source_url(raw_url)
    result = {
        "extraction_tier_used": "TIER_1_API_VENDOR",
        "units": [{"unit_number": "101", "market_rent_low": 1200}],
        "_winning_page_url": raw_url,
        "_raw_api_responses": [{"url": raw_url, "body": {"units": [{"rent": 1200}]}}],
        "_unit_source_provenance": [_unit_source(safe_url, unit_count=1)],
    }

    updated = update_profile_after_extraction(p, result, 1, store)

    assert "%3Credacted%3E" in safe_url
    assert _identity_admitted_unit_source_urls(result) == [raw_url]
    assert updated.navigation.winning_page_url == raw_url
    assert updated.navigation.availability_links == [raw_url]


def test_redacted_provenance_without_exact_raw_route_stays_diagnostic_only(
    store: ProfileStore,
) -> None:
    p = ScrapeProfile(canonical_id="source-route-redacted-missing")
    store.save(p)
    safe_url = sanitise_source_url("https://portal.example/api/units?token=secret&property=7")
    result = {
        "extraction_tier_used": "TIER_1_API_VENDOR",
        "units": [{"unit_number": "101", "market_rent_low": 1200}],
        "_unit_source_provenance": [_unit_source(safe_url, unit_count=1)],
    }

    updated = update_profile_after_extraction(p, result, 1, store)

    assert _identity_admitted_unit_source_urls(result) == []
    assert updated.navigation.winning_page_url is None
    assert updated.navigation.availability_links == []


def test_unknown_provenance_blocks_winning_and_raw_api_profile_routes(
    store: ProfileStore,
) -> None:
    p = ScrapeProfile(canonical_id="source-route-unknown")
    store.save(p)
    url = "https://wrong.example/api/units"
    result = {
        "extraction_tier_used": "TIER_1_API_VENDOR",
        "units": [{"unit_number": "101", "market_rent_low": 1200}],
        "_winning_page_url": url,
        "_raw_api_responses": [{"url": url, "body": {"units": [{"rent": 1200}]}}],
        "_unit_source_provenance": [_unit_source(url, identity_status="UNKNOWN", unit_count=1)],
    }

    updated = update_profile_after_extraction(p, result, 1, store)

    assert updated.navigation.winning_page_url is None
    assert updated.navigation.availability_links == []
    assert updated.api_hints.known_endpoints == []


def test_multiple_matched_unit_sources_are_retained_without_inventing_one_winner(
    store: ProfileStore,
) -> None:
    p = ScrapeProfile(canonical_id="source-route-003")
    store.save(p)
    urls = [
        "https://property.example/floorplans/a1/units",
        "https://property.example/floorplans/b1/units",
    ]
    result = {
        "extraction_tier_used": "TIER_MERGED_CROSS_PAGE",
        "units": [
            {"unit_number": "101", "market_rent_low": 1200},
            {"unit_number": "201", "market_rent_low": 1400},
        ],
        "_unit_source_provenance": [_unit_source(url, unit_count=1) for url in urls],
    }

    updated = update_profile_after_extraction(p, result, 2, store)

    assert updated.navigation.winning_page_url is None
    assert updated.navigation.availability_links == urls


def test_matched_provenance_does_not_override_roster_contamination(
    store: ProfileStore,
) -> None:
    p = ScrapeProfile(canonical_id="source-route-004")
    store.save(p)
    source_url = "https://portfolio.example/api/units"
    result = {
        "extraction_tier_used": "TIER_1_API_VENDOR",
        "units": _roster_rows("85283", 8),
        "_property_zip": "85013",
        "_unit_source_provenance": [_unit_source(source_url, unit_count=8)],
    }

    updated = update_profile_after_extraction(p, result, 8, store)

    assert updated.navigation.winning_page_url is None
    assert updated.navigation.availability_links == []


# ── 2026-07-19: suffixed-tier persistence fix (writer tier-string mismatch) ──


@pytest.mark.parametrize(
    "tier,expected",
    [
        ("TIER_1_API", 1),  # bare — exact map
        ("TIER_1_API_ENTRATA", 1),  # suffixed API
        ("TIER_1_KNOCK_API", 1),
        ("TIER_1_API_RENTCAFE_SECURECAFE", 1),
        ("TIER_1_DOM_CAMDEN", 1),
        ("TIER_3_DOM", 3),  # bare
        ("TIER_1_DOM_ENTRATA_PP_SSR", 1),
        ("TIER_MERGED_CROSS_PAGE", None),  # no TIER_<n> leading token
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
    assert [e.url_pattern for e in up.api_hints.known_endpoints] == ["https://x.securecafe.com/api/units"]


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


# ── 2026-07-28: roster-contamination guard on winning_page_url ────────────
#
# Reproduced on the 2026-07-27 full run: property 260505 (Onyx Uptown PHX,
# Phoenix AZ 85013) shipped 211 rows named for Tempe/Memphis addresses because
# a prior run persisted ``chamberlin.appfolio.com/listings`` as its
# winning_page_url; that run's own event log shows it replayed at score 10001
# (``extract.link_hop_started`` … anchor="profile:winning_page_url"). Skipping
# the write is not sufficient on its own — the poison was already on disk.


def _roster_rows(zip_code: str, n: int) -> list[dict]:
    """n rows named for addresses in a single ZIP (the roster row shape)."""
    return [
        {
            "unit_name": f"999 E Baseline Rd, {2400 + i}, Tempe, AZ {zip_code}",
            "unit_number": str(2400 + i),
            "market_rent_low": 1200 + i,
        }
        for i in range(n)
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # MUST resolve — real address tails
        ("999 E Baseline Rd, 2406, Tempe, AZ 85283", "85283"),
        ("2069 N. Argyle Ave., 304, Hollywood Hills, CA 90068", "90068"),
        ("3140 N Clybourn Ave Unit 205, Chicago, IL 60618", "60618"),
        ("100 Main St, Springfield, MA 01020-1234", "01020"),
        ("100 Main St, Springfield, MA,01020", "01020"),
        ("Chicago IL 60618", "60618"),
        ("wa 98503 lacey", "98503"),
        # MUST NOT resolve — anything that is not an address tail. A loose
        # 5-digit matcher here would misread ordinary unit text as an address.
        ("101", None),
        ("Unit B2", None),
        ("1 BA 12345 sqft", None),
        ("2 BR 90210", None),
        ("APT 12345", None),
        ("BLDG 60618", None),
        ("The Sycamore - 2 Bed / 2 Bath", None),
        ("AppFolio listing 8545", None),
        ("Studio 12345", None),
        ("MLS 12345", None),
        ("A2 60618", None),
        ("Rent $1,450 / 850 sq ft", None),
    ],
)
def test_row_zip_matcher_table(text: str, expected: str | None) -> None:
    assert _row_zip({"unit_name": text}) == expected


def test_contaminated_result_is_not_persisted_as_winning_url(store: ProfileStore) -> None:
    """The write gate: a roster result must not become the replay anchor."""
    p = ScrapeProfile(canonical_id="contam-001")
    store.save(p)
    result = {
        "extraction_tier_used": "TIER_1_DOM_APPFOLIO_SSR",
        "units": _roster_rows("85283", 8),
        "_winning_page_url": "https://chamberlin.appfolio.com/listings",
        "_property_zip": "85013",
    }
    up = update_profile_after_extraction(p, result, 8, store)
    assert up.navigation.winning_page_url is None
    assert up.navigation.availability_page_path is None


def test_persisted_poisoned_winning_url_is_invalidated_on_replay(
    store: ProfileStore,
) -> None:
    """The poison already on disk: replaying it must clear it everywhere.

    Clearing ``winning_page_url`` alone only demotes the URL from hop score
    10_001 to 10_000, because ``availability_links`` is the next-highest
    injected candidate — so the URL must leave that list too.
    """
    poisoned = "https://chamberlin.appfolio.com/listings"
    p = ScrapeProfile(canonical_id="contam-002")
    p.navigation.winning_page_url = poisoned
    p.navigation.availability_page_path = "/listings"
    p.navigation.availability_links = [poisoned]
    p.confidence.maturity = ProfileMaturity.HOT
    store.save(p)

    result = {
        "extraction_tier_used": "TIER_1_DOM_APPFOLIO_SSR",
        "units": _roster_rows("85283", 211),
        "_winning_page_url": poisoned,
        "_property_zip": "85013",
        "_explored_links": {poisoned: True},
    }
    up = update_profile_after_extraction(p, result, 211, store)

    assert up.navigation.winning_page_url is None
    assert up.navigation.availability_page_path is None
    assert poisoned not in up.navigation.availability_links
    assert poisoned in up.navigation.explored_links
    assert up.confidence.maturity == ProfileMaturity.COLD


def test_quality_flag_contaminated_also_blocks_the_write(store: ProfileStore) -> None:
    """The pre-existing volume flag, now actually consulted at the write.

    These rows carry no address text at all, so only the CONTAMINATED flag
    can condemn this result.
    """
    p = ScrapeProfile(canonical_id="contam-003")
    store.save(p)
    result = {
        "extraction_tier_used": "TIER_1_DOM_APPFOLIO_SSR",
        "units": [{"unit_number": str(i), "market_rent_low": 1200} for i in range(200)],
        "_expected_total_units": 20,
        "_winning_page_url": "https://pmc.example.com/listings",
    }
    up = update_profile_after_extraction(p, result, 200, store)
    assert up.quality.last_quality_flag == "CONTAMINATED"
    assert up.navigation.winning_page_url is None


def test_clean_cross_host_portal_url_still_persists(store: ProfileStore) -> None:
    """Guard against the opposite failure: 541 of the 1,884 persisted profiles
    that carry a winning_page_url are legitimately cross-host (rentcafe /
    knock / resman / securecafe portals). Host mismatch is NOT contamination.
    """
    p = ScrapeProfile(canonical_id="clean-001")
    p.navigation.entry_url = "https://www.parkatidlewild.com/"
    store.save(p)
    portal = "https://parkatidlewild.securecafe.com/onlineleasing/availability"
    result = {
        "extraction_tier_used": "TIER_1_API_RENTCAFE_SECURECAFE",
        "units": [{"unit_number": "101", "market_rent_low": 1200}],
        "_winning_page_url": portal,
        "_property_zip": "29650",
        "_explored_links": {portal: True},
    }
    up = update_profile_after_extraction(p, result, 1, store)
    assert up.navigation.winning_page_url == portal
    assert portal in up.navigation.availability_links


def test_matching_zip_roster_is_not_contamination(store: ProfileStore) -> None:
    """Address-named rows in the property's OWN ZIP are its own buildings, not
    a roster dump — the URL must still be learned. This is the scoped-filter
    shape (``?filters[property_list]=COLLEGE PARK`` → 12 cards, all 98503).
    """
    p = ScrapeProfile(canonical_id="clean-002")
    store.save(p)
    url = "https://olympicmanagement.appfolio.com/listings?filters%5Bproperty_list%5D=COLLEGE%20PARK"
    result = {
        "extraction_tier_used": "TIER_1_DOM_APPFOLIO_SSR",
        "units": _roster_rows("98503", 12),
        "_winning_page_url": url,
        "_property_zip": "98503",
    }
    up = update_profile_after_extraction(p, result, 12, store)
    assert up.navigation.winning_page_url == url


def test_unknown_property_zip_declines_rather_than_guesses(store: ProfileStore) -> None:
    """No CSV ZIP → nothing to compare against → the URL is still learned.
    The check must not turn "couldn't look" into "contaminated"."""
    p = ScrapeProfile(canonical_id="clean-003")
    store.save(p)
    url = "https://pmc.example.com/listings"
    result = {
        "extraction_tier_used": "TIER_1_DOM_APPFOLIO_SSR",
        "units": _roster_rows("85283", 40),
        "_winning_page_url": url,
    }
    up = update_profile_after_extraction(p, result, 40, store)
    assert up.navigation.winning_page_url == url


def test_zero_unit_invalidation_still_fires(store: ProfileStore) -> None:
    """The pre-existing invalidation branch must keep working unchanged."""
    p = ScrapeProfile(canonical_id="stale-001")
    p.navigation.winning_page_url = "https://example.com/floorplans"
    p.navigation.availability_page_path = "/floorplans"
    p.confidence.maturity = ProfileMaturity.HOT
    store.save(p)
    result = {
        "extraction_tier_used": "FAILED",
        "units": [],
        "_winning_page_url_hop_outcome": "profile:winning_page_url:failed",
    }
    up = update_profile_after_extraction(p, result, 0, store)
    assert up.navigation.winning_page_url is None
    assert up.confidence.maturity == ProfileMaturity.COLD
