"""Unit tests for ``ma_poc.services.floorplan_catalog``.

Validates the H2/H6/H8/H11 invariants from CLAUDE_PROMPTS_MERGE_RESILIENCE.md.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from ma_poc.services.floorplan_catalog import (
    KNOWN_PLANS_PROMPT_LIMIT,
    SNAP_REASON_EXACT_THREE,
    SNAP_REASON_LOW_COVERAGE,
    SNAP_REASON_MULTI_FAIL_CLOSED,
    SNAP_REASON_NAME_DISAMBIG,
    SNAP_REASON_NO_MATCH,
    SNAP_REASON_UNIQUE_TWO,
    FloorplanCatalog,
)


def _write_csv(path: Path, rows: list[tuple]) -> None:
    header = "apartmentid,floorplannumber,description,bed,bath,area\n"
    body = "\n".join(",".join(str(c) for c in r) for r in rows)
    path.write_text(header + body + "\n", encoding="utf-8")


def _write_mapping(path: Path, chosen: str | None = "apartmentid") -> None:
    path.write_text(
        json.dumps(
            {
                "chosen_field": chosen,
                "coverage": (
                    {"field": chosen, "matched": 100, "total": 100, "ratio": 1.0}
                    if chosen
                    else None
                ),
                "candidates": [],
                "min_coverage": 0.9,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def catalog_dir(tmp_path: Path) -> Path:
    csv_path = tmp_path / "Floorplan- comparisons.csv"
    map_path = tmp_path / "csv_floorplan_mapping.json"
    _write_csv(
        csv_path,
        [
            ("100", 1, "Aspen", 1, 1, 750),
            ("100", 2, "Birch", 1, 1, 850),
            ("100", 3, "Cedar", 2, 2, 1100),
            # Two plans share the same beds/baths/sqft → only the description
            # disambiguates them. Exercises H8 fail-closed.
            ("200", 1, "Maple Junior", 2, 2, 950),
            ("200", 2, "Maple Senior", 2, 2, 950),
            # sqft sentinel — should be treated as null.
            ("300", 1, "Quail", 0, 1, -1),
        ],
    )
    _write_mapping(map_path, "apartmentid")
    return tmp_path


# ── Catalog loading ──────────────────────────────────────────────────────────


def test_catalog_loads_from_csv(catalog_dir: Path) -> None:
    cat = FloorplanCatalog(
        csv_path=catalog_dir / "Floorplan- comparisons.csv",
        mapping_path=catalog_dir / "csv_floorplan_mapping.json",
    )
    assert not cat.in_bypass_mode
    assert cat.chosen_field == "apartmentid"
    plans_100 = cat.get_plans("100")
    assert len(plans_100) == 3
    assert {p.description for p in plans_100} == {"Aspen", "Birch", "Cedar"}


# ── H2: strict sqft ────────────────────────────────────────────────────────


def test_strict_sqft_no_tolerance(catalog_dir: Path) -> None:
    cat = FloorplanCatalog(
        csv_path=catalog_dir / "Floorplan- comparisons.csv",
        mapping_path=catalog_dir / "csv_floorplan_mapping.json",
    )
    # Aspen is 750 sqft. 748 is close but not equal → no match (H2).
    res = cat.snap("100", beds=1, baths=1.0, sqft=748, floor_plan_name_hint="Aspen")
    assert res.plan is None
    assert res.reason == SNAP_REASON_NO_MATCH


def test_sqft_sentinel_treated_as_null(catalog_dir: Path) -> None:
    cat = FloorplanCatalog(
        csv_path=catalog_dir / "Floorplan- comparisons.csv",
        mapping_path=catalog_dir / "csv_floorplan_mapping.json",
    )
    # apartment 300 has sqft=-1 (sentinel). Extracted -1 should be treated
    # as absent — uniqueness comes from beds + baths matching one row.
    res = cat.snap("300", beds=0, baths=1.0, sqft=-1, floor_plan_name_hint=None)
    assert res.plan is not None
    assert res.plan.description == "Quail"
    assert res.reason == SNAP_REASON_UNIQUE_TWO


def test_snap_unique_two_field_when_sqft_null(catalog_dir: Path) -> None:
    cat = FloorplanCatalog(
        csv_path=catalog_dir / "Floorplan- comparisons.csv",
        mapping_path=catalog_dir / "csv_floorplan_mapping.json",
    )
    # apartment 100 has only one 2BR/2BA plan (Cedar) — sqft None still
    # produces a unique survivor.
    res = cat.snap("100", beds=2, baths=2.0, sqft=None, floor_plan_name_hint=None)
    assert res.plan is not None
    assert res.plan.description == "Cedar"
    assert res.reason == SNAP_REASON_UNIQUE_TWO


# ── Disambiguation by floor_plan_name ──────────────────────────────────────


def test_snap_name_disambiguation_when_multiple_candidates(catalog_dir: Path) -> None:
    cat = FloorplanCatalog(
        csv_path=catalog_dir / "Floorplan- comparisons.csv",
        mapping_path=catalog_dir / "csv_floorplan_mapping.json",
    )
    # apartment 200 has two 2BR/2BA/950sqft plans — Junior and Senior. Hint
    # "Maple Junior" should pick the right one.
    res = cat.snap(
        "200",
        beds=2,
        baths=2.0,
        sqft=950,
        floor_plan_name_hint="Maple Junior",
    )
    assert res.plan is not None
    assert res.plan.description == "Maple Junior"
    assert res.reason == SNAP_REASON_NAME_DISAMBIG


# ── H8: ambiguous fail-closed ──────────────────────────────────────────────


def test_snap_ambiguous_returns_fail_closed(catalog_dir: Path) -> None:
    cat = FloorplanCatalog(
        csv_path=catalog_dir / "Floorplan- comparisons.csv",
        mapping_path=catalog_dir / "csv_floorplan_mapping.json",
    )
    # No hint → cannot disambiguate Junior vs Senior → fail closed (H8).
    res = cat.snap("200", beds=2, baths=2.0, sqft=950, floor_plan_name_hint=None)
    assert res.plan is None
    assert res.reason == SNAP_REASON_MULTI_FAIL_CLOSED


def test_snap_returns_exact_three_for_unique_three_field_match(catalog_dir: Path) -> None:
    cat = FloorplanCatalog(
        csv_path=catalog_dir / "Floorplan- comparisons.csv",
        mapping_path=catalog_dir / "csv_floorplan_mapping.json",
    )
    res = cat.snap("100", beds=1, baths=1.0, sqft=750, floor_plan_name_hint="Aspen")
    assert res.plan is not None
    assert res.plan.description == "Aspen"
    assert res.reason == SNAP_REASON_EXACT_THREE


# ── H6: low-coverage bypass ────────────────────────────────────────────────


def test_low_coverage_bypass_short_circuits(tmp_path: Path) -> None:
    csv_path = tmp_path / "Floorplan- comparisons.csv"
    map_path = tmp_path / "csv_floorplan_mapping.json"
    _write_csv(csv_path, [("100", 1, "Aspen", 1, 1, 750)])
    _write_mapping(map_path, chosen=None)
    cat = FloorplanCatalog(csv_path=csv_path, mapping_path=map_path)
    assert cat.in_bypass_mode is True
    assert cat.get_plans("100") == []
    res = cat.snap("100", beds=1, baths=1.0, sqft=750, floor_plan_name_hint="Aspen")
    assert res.plan is None
    assert res.reason == SNAP_REASON_LOW_COVERAGE


def test_missing_mapping_file_falls_back_to_bypass(tmp_path: Path) -> None:
    csv_path = tmp_path / "Floorplan- comparisons.csv"
    _write_csv(csv_path, [("100", 1, "Aspen", 1, 1, 750)])
    cat = FloorplanCatalog(
        csv_path=csv_path, mapping_path=tmp_path / "missing.json"
    )
    assert cat.in_bypass_mode is True


# ── H11: known-plans block size + truncation logging ───────────────────────


def test_known_plans_block_caps_at_limit(tmp_path: Path) -> None:
    csv_path = tmp_path / "Floorplan- comparisons.csv"
    map_path = tmp_path / "csv_floorplan_mapping.json"
    rows = [
        ("999", i, f"Plan{i}", 1, 1, 600 + i)
        for i in range(KNOWN_PLANS_PROMPT_LIMIT + 5)
    ]
    _write_csv(csv_path, rows)
    _write_mapping(map_path, "apartmentid")
    cat = FloorplanCatalog(csv_path=csv_path, mapping_path=map_path)
    text, meta = cat.known_plans_block("999")
    assert meta["truncated"] is True
    assert meta["plan_count_emitted"] == KNOWN_PLANS_PROMPT_LIMIT
    assert "and 5 more plans" in text


def test_known_plans_truncation_logged(tmp_path: Path, caplog) -> None:
    import logging

    csv_path = tmp_path / "Floorplan- comparisons.csv"
    map_path = tmp_path / "csv_floorplan_mapping.json"
    rows = [
        ("999", i, f"Plan{i}", 1, 1, 600 + i)
        for i in range(KNOWN_PLANS_PROMPT_LIMIT + 2)
    ]
    _write_csv(csv_path, rows)
    _write_mapping(map_path, "apartmentid")
    cat = FloorplanCatalog(csv_path=csv_path, mapping_path=map_path)
    with caplog.at_level(logging.INFO, logger="ma_poc.services.floorplan_catalog"):
        cat.known_plans_block("999")
    assert any("truncated known-plans" in rec.message for rec in caplog.records)


def test_known_plans_block_empty_when_unknown_property(catalog_dir: Path) -> None:
    cat = FloorplanCatalog(
        csv_path=catalog_dir / "Floorplan- comparisons.csv",
        mapping_path=catalog_dir / "csv_floorplan_mapping.json",
    )
    text, meta = cat.known_plans_block("does-not-exist")
    assert text == ""
    assert meta["plan_count_total"] == 0
    assert meta["bypass"] is False
