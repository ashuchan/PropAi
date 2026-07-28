"""Tests for deterministic, conservative upgrade-analysis Phase-1 segmentation."""

from __future__ import annotations

import pytest

from ma_poc.scripts.diagnostics._public_url import normalize_public_url
from ma_poc.scripts.diagnostics.upgrade_analysis_944 import (
    UNSETTLED_LEVER,
    CandidateProperty,
    assign_lever,
    candidate_from_property,
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


def _row(website: object, *, canonical_id: str | None = "38532", verdict: str = "SUCCESS_PLAN_LEVEL") -> dict[str, object]:
    """Build one v2 Canary run row shaped exactly like the 2026-07-27 shards."""
    meta: dict[str, object] = {
        "verdict": verdict,
        "provenance": {
            "detected_pms": "rentmanager",
            "winning_tier": "TIER_3_PLAN_TEXT",
            "fetch": {"render_mode": "RENDER", "error_signature": None},
        },
    }
    if canonical_id is not None:
        meta["canonical_id"] = canonical_id
    return {"_meta": meta, "apartment_id": canonical_id, "website": website, "proj_name": "Glenbrook"}


def test_scheme_less_marketing_host_is_classified_not_crashed() -> None:
    """74 of the 950 2026-07-27 cohort rows carry a bare host; the fetch path scraped them fine."""
    candidate = candidate_from_property(_row("www.glenbrook-apts.com"))
    assert candidate.website == "https://www.glenbrook-apts.com"
    assert candidate.unsettled_reason is None
    assert assign_lever(candidate) == "PMS_PUBLIC_AVAILABILITY_ROUTE"


def test_row_without_a_usable_public_route_is_reported_unsettled_not_dropped() -> None:
    """An unusable website never aborts the run and never disappears from the count."""
    candidate = candidate_from_property(_row("~", verdict="FAILED_NO_DATA"))
    assert candidate.apartment_id == "38532"
    assert candidate.website == ""
    assert candidate.unsettled_reason == "UNUSABLE_PUBLIC_WEBSITE:~"
    assert assign_lever(candidate) == UNSETTLED_LEVER


def test_row_without_an_identity_keeps_a_stable_surrogate_key() -> None:
    """A row with no canonical id stays addressable and stays visible."""
    first = candidate_from_property(_row("https://a.example.test/", canonical_id=None))
    second = candidate_from_property(_row("https://a.example.test/", canonical_id=None))
    assert first.apartment_id == second.apartment_id
    assert first.apartment_id.startswith("unidentified-")
    assert first.unsettled_reason == "MISSING_CANONICAL_ID"
    assert assign_lever(first) == UNSETTLED_LEVER


def test_unsettled_rows_are_listed_in_full_and_never_ranked_as_work() -> None:
    """The unsettled bucket is enumerated whole, sorts last, and carries no rank."""
    rows = [_row("https://ok.example.test/", canonical_id=str(index)) for index in range(30)]
    rows.extend(_row("~", canonical_id=f"u{index}") for index in range(30))
    payload = phase_one_payload([candidate_from_property(row) for row in rows])
    assert payload["observed_total"] == 60
    assert sum(lever["properties_addressed"] for lever in payload["levers"]) == 60
    assert payload["unsettled_total"] == 30
    assert len(payload["unsettled"]) == 30
    unsettled_lever = payload["levers"][-1]
    assert unsettled_lever["lever"] == UNSETTLED_LEVER
    assert unsettled_lever["rank"] is None
    assert unsettled_lever["rate_status"] == "UNSETTLED"
    assert unsettled_lever["sample_n_planned"] == 30  # full enumeration, not a 25-row sample
    assert payload["levers"][0]["rank"] == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # must normalize
        ("www.glenbrook-apts.com", "https://www.glenbrook-apts.com"),
        ("www.reserveatlenoxpark.net/floorplans/#/", "https://www.reserveatlenoxpark.net/floorplans/#/"),
        ("  www.tgmdanada.com  ", "https://www.tgmdanada.com"),
        ("live-foxglen.co.uk/availability?bed=2", "https://live-foxglen.co.uk/availability?bed=2"),
        # must pass through untouched
        ("https://www.wyncrofthill.com/floorplans", "https://www.wyncrofthill.com/floorplans"),
        ("http://www.pebblebrookapts.com", "http://www.pebblebrookapts.com"),
        # must NOT be turned into a URL
        ("~", None),
        ("", None),
        ("   ", None),
        ("/floorplans/", None),
        ("#pricing", None),
        ("localhost", None),
        ("Call for pricing", None),
        ("mailto:leasing@example.com", None),
        ("tel:+15551234567", None),
        ("javascript:void(0)", None),
        ("ftp://files.example.com", None),
        ("N/A", None),
    ],
)
def test_public_url_normalizer_table(raw: str, expected: str | None) -> None:
    """Table-test: a scheme is added only for host-like values, never for arbitrary text."""
    assert normalize_public_url(raw) == expected
