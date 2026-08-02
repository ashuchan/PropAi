"""Fail-closed coverage for server-rendered residence availability tables."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from ma_poc.pms.adapters._static_residence_table import (
    recover_static_residence_table,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.generic_plan_text import GenericPlanTextAdapter
from ma_poc.pms.detector import DetectedPMS
from ma_poc.pms.source_provenance import context_unit_source_provenance
from ma_poc.scripts.runners.jugnu import _format_v2_floor_plan

URL = "https://www.1515parkplace.example/availability.html"


def _table(*rows: str) -> str:
    return f"""
    <html><head><title>1515 Park Place in Crown Heights, Brooklyn</title></head>
    <body>
      <div>1515 Park Pl, Brooklyn, NY 11213</div>
      <div class="table">
        <div class="table-header">
          <div class="table-cell">Residence</div>
          <div class="table-cell">Bed/Bath</div>
          <div class="table-cell">Price</div>
          <div class="table-cell">Floorplan</div>
        </div>
        {''.join(rows)}
      </div>
    </body></html>
    """


def _row(
    code: str,
    bed_bath: str,
    rent: str,
    href: str = "/floorplan.jpg",
) -> str:
    return f"""
    <div class="table-row">
      <div class="table-cell residence">{code}</div>
      <div class="table-cell bed">{bed_bath}</div>
      <div class="table-cell price">{rent}</div>
      <div class="table-cell table-links"><a href="{href}">View</a></div>
    </div>
    """


def _ctx(html: str, *, name: str = "1515 Park Place") -> AdapterContext:
    return AdapterContext(
        base_url=URL,
        detected=DetectedPMS(pms="generic_plan_text", confidence=0.55),
        profile=None,
        expected_total_units=None,
        property_id="261580",
        fetch_result=SimpleNamespace(body=html.encode(), final_url=URL),
        property_name=name,
        address="1515 Park Pl",
        city="Brooklyn",
        state="NY",
        zip_code="11213",
    )


def test_mixed_table_emits_only_specific_physical_residences() -> None:
    context = _ctx(
        _table(
            _row("205-805", "1 Bed/1Bath", "$2,300 - $2,450"),
            _row("206-806", "1 Bed/1Bath", "$2,300 - $2,450", "/images/floorplans/1bed/206.jpg"),
            _row("303-803", "1 Bed/1Bath", "$2,200 - $2,300", "/images/floorplans/1bed/303.jpg"),
            _row("102", "2 Bed/2Bath", "$3,000"),
            _row("307-807", "2 Bed/1Bath", "$2,750 - $3,000", "/images/floorplans/2bed/307.jpg"),
            _row("201-801", "3 Bed/2Bath", "$3,500 - $3,700", "/images/floorplans/3bed/201.jpg"),
            _row("101", "4 Bed/2Bath", "$4,500"),
            _row("103", "4 Bed/2Bath", "$4,300"),
        )
    )

    rows = recover_static_residence_table(context)

    assert [row["unit_number"] for row in rows] == ["102", "101", "103"]
    assert [row["market_rent_low"] for row in rows] == [3000, 4500, 4300]
    assert [row["bedrooms"] for row in rows] == ["2", "4", "4"]
    assert [row["bathrooms"] for row in rows] == ["2", "2", "2"]
    assert all(row["floor_plan_name"] == "" for row in rows)
    assert all(row["availability_date"] == "" for row in rows)
    assert all(
        row["data_gaps"] == ["floor_plan_name", "sqft", "availability_date"]
        for row in rows
    )
    assert context._static_residence_table_telemetry == {
        "raw_rows": 8,
        "accepted_physical_residences": 3,
        "accepted_plan_stacks": 5,
        "skipped_numeric_stack_ranges": [
            "205-805",
            "206-806",
            "303-803",
            "307-807",
            "201-801",
        ],
        "source_url": URL,
    }
    plans = context._static_residence_table_plan_summaries
    assert [plan["floor_plan_name"] for plan in plans] == [
        "205-805",
        "206-806",
        "303-803",
        "307-807",
        "201-801",
    ]
    assert [(plan["market_rent_low"], plan["market_rent_high"]) for plan in plans] == [
        (2300, 2450),
        (2300, 2450),
        (2200, 2300),
        (2750, 3000),
        (3500, 3700),
    ]
    assert all(plan["is_floor_plan_level"] for plan in plans)
    assert all(plan["unit_number"] == "" for plan in plans)
    formatted_plans = [
        _format_v2_floor_plan(
            plan,
            datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
            "261580",
        )
        for plan in plans
    ]
    assert all(plan["unit_id"] is None for plan in formatted_plans)
    assert all(plan["availability_status"] == "UNKNOWN" for plan in formatted_plans)
    assert all(plan["available_date"] is None for plan in formatted_plans)
    provenance = context_unit_source_provenance(context)
    assert len(provenance) == 1
    assert provenance[0]["provider"] == "static_residence_table"
    assert provenance[0]["unit_count"] == 3
    assert provenance[0]["identity"]["source_count"] == 8
    assert provenance[0]["identity"]["admitted_plan_count"] == 5


def test_alphanumeric_hyphenated_physical_code_is_not_a_numeric_range() -> None:
    rows = recover_static_residence_table(
        _ctx(_table(_row("C-06", "1 Bed/1Bath", "$1,230")))
    )
    assert [row["unit_number"] for row in rows] == ["C-06"]


def test_wrong_property_identity_rejects_before_parsing() -> None:
    assert (
        recover_static_residence_table(
            _ctx(
                _table(_row("101", "4 Bed/2Bath", "$4,500")),
                name="Sibling Tower",
            )
        )
        == []
    )


def test_duplicate_physical_residence_rejects_entire_table() -> None:
    html = _table(
        _row("101", "4 Bed/2Bath", "$4,500"),
        _row("101", "4 Bed/2Bath", "$4,300"),
    )
    assert recover_static_residence_table(_ctx(html)) == []


def test_non_range_malformed_row_rejects_entire_table() -> None:
    html = _table(
        _row("101", "4 Bed/2Bath", "$4,500"),
        _row("103", "Call for details", "$4,300"),
    )
    assert recover_static_residence_table(_ctx(html)) == []


def test_sales_price_and_ambiguous_duplicate_tables_fail_closed() -> None:
    sale = _table(_row("PH1", "4 Bed/3Bath", "$1,250,000"))
    assert recover_static_residence_table(_ctx(sale)) == []

    one = _table(_row("101", "4 Bed/2Bath", "$4,500"))
    duplicate_tables = one.replace("</body>", one.split("<body>", 1)[1])
    assert recover_static_residence_table(_ctx(duplicate_tables)) == []


def test_non_availability_path_cannot_activate_table_parser() -> None:
    context = _ctx(_table(_row("101", "4 Bed/2Bath", "$4,500")))
    context.fetch_result.final_url = "https://www.1515parkplace.example/residences.html"
    assert recover_static_residence_table(context) == []


async def test_generic_adapter_preserves_static_native_rows_before_flattening() -> None:
    context = _ctx(
        _table(
            _row("205-805", "1 Bed/1Bath", "$2,300 - $2,450"),
            _row("102", "2 Bed/2Bath", "$3,000"),
            _row("101", "4 Bed/2Bath", "$4,500"),
        )
    )

    result = await GenericPlanTextAdapter().extract(None, context)

    assert [plan["floor_plan_name"] for plan in result.plan_summaries] == [
        "205-805"
    ]
    assert [row["unit_number"] for row in result.units] == ["102", "101"]
    assert result.tier_used == "TIER_1_DOM_STATIC_RESIDENCE_TABLE"
    assert result.winning_url == URL
    assert result.confidence == 0.95
    assert len(result.unit_source_provenance) == 1
