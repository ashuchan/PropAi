"""#69 — sanity-clamp evidence report tests.

The clamp in ``ma_poc/extraction/sanity.py`` nulls implausible values into
something byte-identical to genuine operator absence. These tests pin the
run-level artifact that makes those clamps countable and attributable —
without it the ledger is an in-process object nobody reads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.extraction.post_process import post_process
from ma_poc.extraction.sanity import (
    clamp_tier_context,
    reset_clamp_ledger,
    sanity_bound,
)
from ma_poc.reporting.observation_reports import build_clamp_report


@pytest.fixture(autouse=True)
def _isolate_clamp_ledger():
    reset_clamp_ledger()
    yield
    reset_clamp_ledger()


def test_empty_ledger_still_writes_an_honest_zero(tmp_path: Path) -> None:
    """No clamps must produce a report saying zero — not a missing file.

    A missing artifact is "couldn't look"; an explicit zero is "nothing
    there". The two must not be confusable.
    """
    report = build_clamp_report(tmp_path, "2026-07-27")
    on_disk = json.loads((tmp_path / "clamp_report.json").read_text())
    assert on_disk == report
    assert report["total_clamps"] == 0
    assert report["properties_affected"] == 0
    assert report["rows"] == []


def test_report_is_aggregable_by_tier(tmp_path: Path) -> None:
    """"Which tier produces the most clamped rents" must be a filter, not a grep."""
    for _ in range(4):
        sanity_bound({"rent_low": 1}, tier="TIER_3_DOM_GENERIC", property_id="P1")
    sanity_bound({"rent_low": 8005551212}, tier="TIER_1_API_SIGHTMAP", property_id="P2")
    sanity_bound({"area": 45}, tier="TIER_3_DOM_GENERIC", property_id="P1")

    report = build_clamp_report(tmp_path, "2026-07-27")

    assert report["total_clamps"] == 6
    assert report["properties_affected"] == 2
    assert report["by_field"] == {"rent_low": 5, "area": 1}
    assert report["by_tier"] == {"TIER_3_DOM_GENERIC": 5, "TIER_1_API_SIGHTMAP": 1}

    rent_rows = [r for r in report["rows"] if r["field"] == "rent_low"]
    assert max(rent_rows, key=lambda r: r["n"])["tier"] == "TIER_3_DOM_GENERIC"
    # Rows are sorted most-clamped first so a reader sees the worst offender.
    assert [r["n"] for r in report["rows"]] == sorted(
        (r["n"] for r in report["rows"]), reverse=True
    )


def test_report_carries_the_pre_clamp_value(tmp_path: Path) -> None:
    """The destroyed value is the evidence — it must reach disk."""
    sanity_bound({"rent_low": 8005551212}, tier="T", property_id="P1")
    report = build_clamp_report(tmp_path, "2026-07-27")
    example = report["rows"][0]["examples"][0]
    assert example["value"] == pytest.approx(8005551212.0)
    assert example["reason"] == "ABOVE_MAX"
    assert example["bounds"] == [200.0, 50000.0]


def test_per_property_index_decomposes_absence(tmp_path: Path) -> None:
    """The #56 prerequisite: clamped area vs genuinely-not-published area.

    ``P_clamped`` had an area and lost it to the clamp. ``P_absent`` never
    had one. Both emit ``area=None``; only the report tells them apart.
    """
    with clamp_tier_context("TIER_1_API_SIGHTMAP"):
        post_process([{"unit_id": "A", "rent_low": 1500, "area": 45}], property_id="P_clamped")
        post_process([{"unit_id": "B", "rent_low": 1500}], property_id="P_absent")

    report = build_clamp_report(tmp_path, "2026-07-27")
    assert report["per_property"] == {"P_clamped": {"area:BELOW_MIN": 1}}
    assert "P_absent" not in report["per_property"]
    assert report["rows"][0]["tier"] == "TIER_1_API_SIGHTMAP"
    assert report["rows"][0]["examples"][0]["unit_id"] == "A"


def test_report_is_strict_json(tmp_path: Path) -> None:
    """The open upper bound must not land on disk as the ``Infinity`` literal.

    ``json.dumps`` writes bare ``Infinity`` and Python reads it back, so a
    lenient round-trip hides the bug; every non-Python consumer rejects it.
    """
    sanity_bound({"beds": 2, "area": 310}, tier="TIER_4_LLM_DOM", property_id="P1")
    build_clamp_report(tmp_path, "2026-07-27")
    text = (tmp_path / "clamp_report.json").read_text()
    assert "Infinity" not in text and "NaN" not in text
    on_disk = json.loads(text, parse_constant=_reject_constant)
    assert on_disk["rows"][0]["reason"] == "IMPLAUSIBLE_FOR_BEDS"
    assert on_disk["rows"][0]["examples"][0]["value"] == pytest.approx(310.0)
    assert on_disk["rows"][0]["examples"][0]["bounds"] == [500.0, None]


def _reject_constant(name: str) -> None:
    raise AssertionError(f"non-standard JSON constant on disk: {name}")
