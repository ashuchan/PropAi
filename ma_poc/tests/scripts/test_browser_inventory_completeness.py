"""Unit coverage for local browser inventory reconciliation helpers."""

from __future__ import annotations

from ma_poc.scripts.diagnostics.browser_endpoint_discovery import (
    BrowserEndpointProbeResult,
    DiscoveryClassification,
)
from ma_poc.scripts.diagnostics.browser_inventory_completeness import (
    InventoryTarget,
    comparable_unit,
    compare_inventory,
    completed_ids_from_payloads,
    current_roster_rows,
    roster_date_from_run_uri,
    select_targets,
)


def test_compare_inventory_finds_missing_unit_and_visible_field_gap() -> None:
    """A current browser unit missing from output remains an explicit gap."""
    comparison = compare_inventory(
        [{"unit_id": "08-B", "rent_low": 1199, "area": 700}],
        [
            {"unit_number": "08 B", "market_rent_low": 1199, "sqft": 700},
            {"unit_number": "20-D", "market_rent_low": 1250, "sqft": 715},
        ],
    )
    assert comparison["missing_observed_unit_keys"] == ["20D"]
    assert comparison["field_mismatches"] == []


def test_compare_inventory_rejects_plan_or_synthetic_identity() -> None:
    """Neither a plan code nor an inferred ID can satisfy unit coverage."""
    assert comparable_unit({"unit_id": "inferred_a1", "rent_low": 1200}) is None
    assert comparable_unit({"unit_id": "A1", "rent_low": 1200, "is_floor_plan_level": True}) is None


def test_compare_inventory_flags_browser_visible_area_and_rent_mismatch() -> None:
    """Values shown on a current browser card are compared per real unit ID."""
    comparison = compare_inventory(
        [{"unit_id": "101", "rent_low": 1200, "area": 700}],
        [{"unit_number": "101", "market_rent_low": 1250, "sqft": 725}],
    )
    assert comparison["missing_observed_unit_keys"] == []
    assert {item["field"] for item in comparison["field_mismatches"]} == {"area", "rent"}


def test_checkpoint_filter_uses_current_workflow_only() -> None:
    """An unrelated diagnostic checkpoint cannot suppress a validation target."""
    payloads = [
        '{"workflow_version":"browser-inventory-completeness-v2","canonical_id":"11"}\n',
        '{"workflow_version":"other","canonical_id":"22"}\n',
    ]
    assert completed_ids_from_payloads(payloads) == {"11"}


def test_roster_date_comes_from_the_immutable_run_not_todays_clock() -> None:
    """Historical validation never silently applies the machine's current date."""
    assert roster_date_from_run_uri("gs://jugnu-canary/runs/2026-07-27-full-0d54ca7/") is not None
    assert str(roster_date_from_run_uri("gs://jugnu-canary/runs/2026-07-27-full-0d54ca7/")) == "2026-07-27"
    assert roster_date_from_run_uri("gs://jugnu-canary/other/2026-07-27/") is None


def test_selection_is_deterministic_and_excludes_completed_records() -> None:
    """A resumable random sample cannot repeat an already checked property."""
    targets = [
        InventoryTarget(str(index), str(index), f"https://{index}.example.test", "SUCCESS", "unknown", ())
        for index in range(4)
    ]
    first = select_targets(targets, {"1"}, limit=2, seed="fixed", verdicts={"SUCCESS"})
    second = select_targets(targets, {"1"}, limit=2, seed="fixed", verdicts={"SUCCESS"})
    assert [target.canonical_id for target in first] == [target.canonical_id for target in second]
    assert "1" not in [target.canonical_id for target in first]


def test_selection_excludes_zero_output_properties_from_row_coverage_sample() -> None:
    """The 500-property denominator must have run rows to validate."""
    empty = InventoryTarget("empty", "empty", "https://empty.example.test", "SUCCESS", "unknown", ())
    emitted = InventoryTarget(
        "emitted",
        "emitted",
        "https://emitted.example.test",
        "SUCCESS",
        "unknown",
        ({"unit_id": "101", "rent_low": 1200},),
    )
    assert select_targets([empty, emitted], set(), limit=2, seed="fixed", verdicts={"SUCCESS"}) == [emitted]
    selected = select_targets(
        [empty, emitted],
        set(),
        limit=2,
        seed="fixed",
        verdicts={"SUCCESS"},
        include_empty_run_output=True,
    )
    assert {target.canonical_id for target in selected} == {"empty", "emitted"}


def test_stratified_selection_represents_platform_and_verdict_cohorts() -> None:
    """A second sample cannot be consumed entirely by one dominant cohort."""
    targets = [
        InventoryTarget("a1", "a1", "https://a1.test", "SUCCESS", "rentcafe", ({"unit_id": "1"},)),
        InventoryTarget("a2", "a2", "https://a2.test", "SUCCESS", "rentcafe", ({"unit_id": "2"},)),
        InventoryTarget("b1", "b1", "https://b1.test", "FAILED", "rentcafe", ({"unit_id": "3"},)),
        InventoryTarget("c1", "c1", "https://c1.test", "SUCCESS", "entrata", ({"unit_id": "4"},)),
    ]
    selected = select_targets(
        targets,
        set(),
        limit=3,
        seed="fixed",
        verdicts=set(),
        stratified=True,
    )
    assert {(target.platform, target.verdict) for target in selected} == {
        ("rentcafe", "SUCCESS"),
        ("rentcafe", "FAILED"),
        ("entrata", "SUCCESS"),
    }


def test_current_roster_ignores_unscoped_api_payload_when_dom_is_absent() -> None:
    """An endpoint payload cannot invent a date-scoped browser discrepancy."""
    probe = BrowserEndpointProbeResult(
        warm_status=200,
        classification=DiscoveryClassification.API_VERIFIED,
        strict_api_rows=({"unit_number": "101", "asking_rent": 1200},),
    )
    rows, api_scope_unverified = current_roster_rows(probe)
    assert rows == ()
    assert api_scope_unverified is True


def test_current_roster_uses_rendered_dom_even_when_api_rows_exist() -> None:
    """Rendered availability wins when discovery also captured a broader API."""
    dom_row = {"unit_number": "101", "asking_rent": 1200, "sqft": 700}
    probe = BrowserEndpointProbeResult(
        warm_status=200,
        classification=DiscoveryClassification.API_VERIFIED,
        strict_api_rows=({"unit_number": "101", "asking_rent": 1200}, {"unit_number": "102", "asking_rent": 1250}),
        strict_dom_rows=(dom_row,),
    )
    rows, api_scope_unverified = current_roster_rows(probe)
    assert rows == (dom_row,)
    assert api_scope_unverified is False
