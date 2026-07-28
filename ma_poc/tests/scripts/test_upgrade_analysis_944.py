"""Tests for deterministic, conservative upgrade-analysis Phase-1 segmentation."""

from __future__ import annotations

from ma_poc.scripts.diagnostics.upgrade_analysis_944 import (
    CandidateProperty,
    assign_lever,
    deterministic_sample,
    phase_one_payload,
    segment_candidates,
)


def _candidate(
    apartment_id: str,
    *,
    verdict: str = "FAILED_NO_DATA",
    pms: str = "unknown",
    tier: str = "GENERIC_VALIDITY_REJECTED",
    ceiling: str | None = "UNCERTAIN",
) -> CandidateProperty:
    """Build one minimal Phase-1 candidate for pure unit tests."""
    return CandidateProperty(
        apartment_id=apartment_id,
        website=f"https://{apartment_id}.example.test/floorplans",
        property_name="Example",
        verdict=verdict,
        detected_pms=pms,
        winning_tier=tier,
        publish_ceiling=ceiling,
        render_mode="RENDER",
        fetch_error=None,
    )


def test_direct_api_failure_modes_take_priority_over_broad_rendering() -> None:
    """Observed adapter failures never disappear into an optimistic generic bucket."""
    assert assign_lever(_candidate("1", pms="entrata", tier="TIER_1_API_ENTRATA_NO_RESPONSE", ceiling="NEEDS_RENDER")) == "ENTRATA_API_NO_RESPONSE_DOM_FALLBACK"
    assert assign_lever(_candidate("2", pms="onesite", tier="TIER_1_API_ONESITE_NO_RESPONSE")) == "ONESITE_API_NO_RESPONSE_RENDER_FALLBACK"
    assert assign_lever(_candidate("3", pms="rentcafe", tier="TIER_1_API_RENTCAFE_SHAPE_REJECTED")) == "RENTCAFE_API_SHAPE_REPAIR"
    assert assign_lever(_candidate("4", pms="entrata", tier="TIER_1_API_ENTRATA_SHAPE_REJECTED")) == "ENTRATA_API_SHAPE_REPAIR"


def test_plan_level_pms_rows_are_routed_to_public_availability_work() -> None:
    """Plan-level rows are not falsely presented as a parser-only issue."""
    candidate = _candidate(
        "5",
        verdict="SUCCESS_PLAN_LEVEL",
        pms="rentcafe",
        tier="TIER_1_API_RENTCAFE_PLAN_LEVEL",
        ceiling=None,
    )
    assert assign_lever(candidate) == "PMS_PUBLIC_AVAILABILITY_ROUTE"


def test_sampling_is_stable_and_never_exceeds_requested_size() -> None:
    """Re-running Phase 1 samples the same randomly ordered property IDs."""
    candidates = [_candidate(str(index)) for index in range(40)]
    first = deterministic_sample(candidates, lever="EXTRACTION_MISS_PUBLIC_ROUTE")
    second = deterministic_sample(reversed(candidates), lever="EXTRACTION_MISS_PUBLIC_ROUTE")
    assert [row.apartment_id for row in first] == [row.apartment_id for row in second]
    assert len(first) == 25


def test_phase_one_reports_mismatch_without_dropping_candidates() -> None:
    """A mutable artifact mismatch remains visible rather than being silently filtered."""
    candidates = [_candidate(str(index), verdict="SUCCESS_PLAN_LEVEL", pms="generic") for index in range(657)]
    candidates.extend(_candidate(f"n{index}") for index in range(288))
    payload = phase_one_payload(candidates)
    assert payload["observed_total"] == 945
    assert payload["cohort_matches_brief"] is False
    assert "no properties were silently removed" in str(payload["cohort_mismatch_note"])
    assert sum(lever["properties_addressed"] for lever in payload["levers"]) == 945


def test_segments_form_a_full_disjoint_partition() -> None:
    """Each property is assigned one measured mechanism worklist exactly once."""
    candidates = [
        _candidate("1", tier="TIER_1_API_ENTRATA_NO_RESPONSE", pms="entrata"),
        _candidate("2", tier="TIER_1_API_RENTCAFE_SHAPE_REJECTED", pms="rentcafe"),
        _candidate("3", ceiling="NEEDS_RENDER"),
        _candidate("4", verdict="SUCCESS_PLAN_LEVEL", pms="appfolio", ceiling=None),
        _candidate("5", verdict="SUCCESS_PLAN_LEVEL", pms="unknown", ceiling=None),
    ]
    segments = segment_candidates(candidates)
    assert sum(len(rows) for rows in segments.values()) == len(candidates)
    assert {row.apartment_id for rows in segments.values() for row in rows} == {"1", "2", "3", "4", "5"}
