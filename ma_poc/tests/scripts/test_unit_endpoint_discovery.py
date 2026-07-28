"""Pure-logic tests for the bounded endpoint-discovery canary runner."""

from __future__ import annotations

from ma_poc.models.scrape_profile import ApiEndpoint, ScrapeProfile
from ma_poc.pms.adapters._prospectportal_warm_replay import (
    ProspectPortalWarmReplayResult,
    PublicPlanPricingSummary,
)
from ma_poc.scripts.diagnostics.unit_endpoint_discovery import (
    _checkpoint_row,
    _classify_replay,
    _has_verified_dynamic_vus,
    _public_warm_candidate,
)
from ma_poc.services.endpoint_discovery_profiles import DiscoveryClassification


def test_candidate_accepts_vanity_or_saved_portal_conventional_warm_url() -> None:
    """The canary accepts custom domains and saved portal availability links."""
    profile = ScrapeProfile(canonical_id="43")
    profile.navigation.winning_page_url = "https://x.example/austin/x/conventional/"
    assert _public_warm_candidate(profile) == profile.navigation.winning_page_url
    profile.navigation.winning_page_url = "https://x.prospectportal.com/Apartments/module/application_authentication/"
    profile.navigation.availability_links = [
        "https://x.prospectportal.com/austin/x/conventional/"
    ]
    assert _public_warm_candidate(profile) == profile.navigation.availability_links[0]


def test_verified_template_is_not_spent_twice_in_later_canary_batches() -> None:
    """Resuming a checkpoint moves to untested candidates instead of repeats."""
    profile = ScrapeProfile(canonical_id="43")
    profile.api_hints.known_endpoints.append(
        ApiEndpoint(
            url_pattern="https://x/?action=view_unit_spaces"
            "&property_floorplan[id]={floorplan_id}&move_in_date={move_in_date}"
        )
    )
    assert _has_verified_dynamic_vus(profile) is True


def test_strict_replay_is_api_verified() -> None:
    """A single real unit-plus-rent row meets the endpoint proof contract."""
    result = ProspectPortalWarmReplayResult(
        200, 1, (200,), ({"unit_number": "D205", "market_rent_low": 800},)
    )
    assert _classify_replay(result) == DiscoveryClassification.API_VERIFIED


def test_blocked_response_is_not_mislabeled_api_missing() -> None:
    """Blocked public access remains a distinct operational outcome."""
    result = ProspectPortalWarmReplayResult(403, 0, (), ())
    assert _classify_replay(result) == DiscoveryClassification.ACCESS_BLOCKED


def test_checkpoint_separates_visible_plan_price_from_unit_endpoint_proof() -> None:
    """A clear floor-plan price must remain an auditable non-unit field."""
    replay = ProspectPortalWarmReplayResult(
        200,
        0,
        (),
        (),
        error="no-floorplan-ids",
        public_plan_pricing=PublicPlanPricingSummary(3, 2, 1200, 1450),
    )
    row = _checkpoint_row(
        "43",
        "https://example.com/floorplans",
        "https://example.com/austin/example/conventional/",
        replay,
        DiscoveryClassification.API_NOT_FOUND_YET,
        "checkpoint_only",
    )
    assert row["strict_unit_rent_rows"] == 0
    assert row["public_plan_pricing"] == {
        "status": "PLAN_RECORDS_WITH_LISTED_PRICE",
        "plans_observed": 3,
        "plans_with_numeric_price": 2,
        "price_low": 1200,
        "price_high": 1450,
    }
