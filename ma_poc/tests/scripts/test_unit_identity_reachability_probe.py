"""Coverage for conservative unit-identity reachability classification."""

from __future__ import annotations

from ma_poc.scripts.diagnostics.browser_endpoint_discovery import BrowserEndpointProbeResult
from ma_poc.scripts.diagnostics.unit_identity_reachability_probe import (
    ReachabilityOutcome,
    _completed_ids_from_payloads,
    _safe_public_url,
    band_batches,
    ordered_work_items,
    parse_args,
    reachability_outcome,
    reachability_record,
    work_item_from_row,
)
from ma_poc.services.endpoint_discovery_profiles import DiscoveryClassification


def _row(*, apartment_id: str, band: str, website: str = "https://example.test/floorplans/") -> dict[str, str]:
    """Build one valid immutable-cohort CSV row."""
    return {
        "apartmentid": apartment_id,
        "band": band,
        "website": website,
        "verdict": "SUCCESS_PLAN_LEVEL",
        "tier": "TIER_3_DOM_GENERIC",
        "n_real": "0",
        "n_syn": "3",
        "ever_gold": "0",
    }


def _result(**overrides: object) -> BrowserEndpointProbeResult:
    """Build a reachable default browser observation for pure-logic tests."""
    values: dict[str, object] = {
        "warm_status": 200,
        "classification": DiscoveryClassification.PUBLIC_PLAN_ONLY,
        "warm_page_url": "https://example.test/floorplans/",
        "warm_urls_tried": ("https://example.test/floorplans/",),
        "networkidle_reached": True,
        "navigation_levels_reached": 1,
    }
    values.update(overrides)
    return BrowserEndpointProbeResult(**values)  # type: ignore[arg-type]


def test_worklist_is_band_ordered_stably_even_when_gcs_csv_is_not() -> None:
    """The 41 false successes always run before synthetic and resistant bands."""
    items = ordered_work_items(
        [
            _row(apartment_id="d", band="D_never_any_unit"),
            _row(apartment_id="b", band="B_all_synthetic"),
            _row(apartment_id="a", band="A_success_no_anchor"),
            _row(apartment_id="c", band="C_resistant_plan_level"),
        ]
    )
    assert [item.apartment_id for item in items] == ["a", "b", "c", "d"]
    assert [[item.apartment_id for item in batch] for batch in band_batches(items)] == [
        ["a"],
        ["b"],
        ["c"],
        ["d"],
    ]


def test_bare_marketing_host_is_normalized_without_losing_path() -> None:
    """A source row without a scheme remains a valid public starting point."""
    item = work_item_from_row(
        _row(apartment_id="42", band="B_all_synthetic", website="www.example.test/floorplans/#/")
    )
    assert item.website == "https://www.example.test/floorplans/#/"


def test_strict_api_rows_publish_identity_and_keep_three_distinct_samples() -> None:
    """A real anchor/rent row is positive evidence, not merely an API success label."""
    item = work_item_from_row(_row(apartment_id="42", band="A_success_no_anchor"))
    result = _result(
        classification=DiscoveryClassification.API_VERIFIED,
        endpoint_url="https://api.example.test/units?move_in_date=2026-07-28",
        strict_api_rows=(
            {"unit_number": "03A", "market_rent_low": 1200, "source_ids": {"unit_number": "03A"}},
            {"unit_number": "E", "market_rent_low": 1210, "source_ids": {"unit_code": "E"}},
            {"unit_number": "08-B", "market_rent_low": 1220, "source_ids": {"unit_number": "08-B"}},
        ),
    )
    record = reachability_record(item, result)
    assert record["outcome"] == ReachabilityOutcome.PUBLISHES_UNIT_IDENTITY.value
    assert record["shape"] == "XHR_JSON"
    assert record["sample_anchors"] == ["03A", "E", "08-B"]
    assert record["sample_rent_for_anchor"] == 1200
    assert record["anchor_varies_across_units"] is True
    assert record["proof_url"] == "https://api.example.test/units"


def test_plan_rows_without_strict_identity_are_not_positive_evidence() -> None:
    """Plan price/count data can support a negative only after a complete route walk."""
    result = _result(
        classification=DiscoveryClassification.PUBLIC_PLAN_ONLY,
        controls_matched=0,
        plan_rows_observed=4,
    )
    assert reachability_outcome(result) == ReachabilityOutcome.NO_PUBLIC_UNIT_IDENTITY


def test_unopened_control_or_route_cap_is_inconclusive_not_absence() -> None:
    """The third bucket prevents a bounded crawler limit becoming a false ceiling."""
    assert reachability_outcome(_result(controls_matched=2, controls_clicked=1)) == (
        ReachabilityOutcome.COULD_NOT_ESTABLISH
    )
    assert reachability_outcome(_result(detail_urls_tried=("https://example.test/a",) * 3)) == (
        ReachabilityOutcome.COULD_NOT_ESTABLISH
    )


def test_blocked_route_is_inconclusive_even_when_plan_cards_render() -> None:
    """A public route block can never be emitted as no-public-unit identity."""
    result = _result(
        blocked_public_paths=("https://portal.example.test/availability",),
        plan_rows_observed=3,
    )
    record = reachability_record(work_item_from_row(_row(apartment_id="42", band="B_all_synthetic")), result)
    assert record["outcome"] == ReachabilityOutcome.COULD_NOT_ESTABLISH.value
    assert record["blocked_reason"] == "public-route-blocked"


def test_runtime_dates_and_tracking_are_not_checkpointed() -> None:
    """Route evidence remains reusable rather than pinning an observed day."""
    assert _safe_public_url(
        "https://example.test/availability?p=abc&moveInDate=7%2F28%2F2026&refreshPricing=true&_gl=tracking"
    ) == "https://example.test/availability?p=abc"


def test_checkpoint_resume_treats_inconclusive_as_completed_until_explicit_retry() -> None:
    """A 10-hour process survives restarts without silently repeating browser work."""
    payload = '{"workflow_version":"unit-identity-reachability-probe-v1","apartment_id":"42","outcome":"COULD_NOT_ESTABLISH"}\n'
    assert _completed_ids_from_payloads([payload]) == {"42"}


def test_hyperbrowser_is_an_explicit_backend_and_cannot_mix_with_direct_mode() -> None:
    """A canary never silently claims an HB residential browser while using local IP."""
    assert parse_args(["--browser-backend", "hyperbrowser"]).browser_backend == "hyperbrowser"
    direct = parse_args(["--browser-backend", "direct"])
    assert direct.direct_device_ip is True
