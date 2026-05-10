"""Consume layer: observation reports (amenities and concessions).

Verifies that `reporting.observation_reports.build_amenities_report()` and
`build_concessions_report()` produce the expected schema and counts from
synthetic property data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reporting.observation_reports import (
    aggregate_property_amenities,
    build_amenities_report,
    build_concessions_report,
)


RUN_DATE = "2026-05-10"


def _make_run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs" / RUN_DATE
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# aggregate_property_amenities helper
# ---------------------------------------------------------------------------


def test_aggregate_amenities_deduplicates() -> None:
    """Duplicate amenity strings across units are deduplicated."""
    units = [
        {"amenities": ["Pool", "Gym", "Pool"]},
        {"amenities": ["pool", "Parking"]},
    ]
    result = aggregate_property_amenities(units)

    assert "pool" in result
    assert "gym" in result
    assert "parking" in result
    # Deduplicated: "pool" appears only once
    assert result.count("pool") == 1


def test_aggregate_amenities_merges_explicit_list() -> None:
    """Explicit property-level amenity list is merged with unit-level amenities."""
    units = [{"amenities": ["Pool"]}]
    explicit = ["Rooftop Terrace", "Concierge"]

    result = aggregate_property_amenities(units, explicit=explicit)

    assert "rooftop terrace" in result
    assert "concierge" in result
    assert "pool" in result


def test_aggregate_amenities_handles_empty() -> None:
    """aggregate_property_amenities on empty input returns empty list."""
    assert aggregate_property_amenities([]) == []


# ---------------------------------------------------------------------------
# build_amenities_report
# ---------------------------------------------------------------------------


def test_amenities_report_written_to_disk(tmp_path: Path) -> None:
    """build_amenities_report writes amenities_report.json to run_dir."""
    run_dir = _make_run_dir(tmp_path)
    props = [
        {
            "property_id": "P1",
            "units": [{"amenities": ["Pool", "Gym"]}],
            "_meta": {"scrape_tier_used": "TIER_1_API"},
        }
    ]

    build_amenities_report(props, run_dir, RUN_DATE)

    report_file = run_dir / "amenities_report.json"
    assert report_file.exists()


def test_amenities_report_properties_with_count(tmp_path: Path) -> None:
    """build_amenities_report tracks how many properties have amenities."""
    run_dir = _make_run_dir(tmp_path)
    props = [
        {"property_id": "P1", "units": [{"amenities": ["Pool"]}]},
        {"property_id": "P2", "units": []},  # no amenities
    ]

    report = build_amenities_report(props, run_dir, RUN_DATE)

    assert report["summary"]["properties_with_amenities"] >= 1
    assert report["summary"]["properties_total"] == 2


def test_amenities_report_top_amenities_populated(tmp_path: Path) -> None:
    """top_amenities list contains the most-common amenity strings."""
    run_dir = _make_run_dir(tmp_path)
    props = [
        {"property_id": f"P{i}", "units": [{"amenities": ["Pool", "Gym"]}]}
        for i in range(5)
    ]

    report = build_amenities_report(props, run_dir, RUN_DATE)

    top = report["summary"].get("top_50_amenities", [])
    assert isinstance(top, list)
    assert len(top) >= 1


# ---------------------------------------------------------------------------
# build_concessions_report
# ---------------------------------------------------------------------------


def test_concessions_report_written_to_disk(tmp_path: Path) -> None:
    """build_concessions_report writes concessions_report.json."""
    run_dir = _make_run_dir(tmp_path)
    props = [
        {
            "property_id": "P1",
            "units": [{"concessions": "1 month free"}],
        }
    ]

    build_concessions_report(props, run_dir, RUN_DATE)

    report_file = run_dir / "concessions_report.json"
    assert report_file.exists()


def test_concessions_report_no_crash_on_empty(tmp_path: Path) -> None:
    """build_concessions_report does not raise when properties have no concessions."""
    run_dir = _make_run_dir(tmp_path)
    props = [{"property_id": "P1", "units": []}]

    report = build_concessions_report(props, run_dir, RUN_DATE)

    assert isinstance(report, dict)
