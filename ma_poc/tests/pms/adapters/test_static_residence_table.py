"""Fail-closed coverage for server-rendered residence availability tables."""

from __future__ import annotations

from types import SimpleNamespace

from ma_poc.pms.adapters._static_residence_table import (
    recover_static_residence_table,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.generic_plan_text import GenericPlanTextAdapter
from ma_poc.pms.detector import DetectedPMS

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


def _row(code: str, bed_bath: str, rent: str) -> str:
    return f"""
    <div class="table-row">
      <div class="table-cell residence">{code}</div>
      <div class="table-cell bed">{bed_bath}</div>
      <div class="table-cell price">{rent}</div>
      <div class="table-cell table-links"><a href="/floorplan.jpg">View</a></div>
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
            _row("102", "2 Bed/2Bath", "$3,000"),
            _row("307-807", "2 Bed/1Bath", "$2,750 - $3,000"),
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
        "raw_rows": 5,
        "accepted_physical_residences": 3,
        "skipped_numeric_stack_ranges": ["205-805", "307-807"],
        "source_url": URL,
    }


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

    assert result.plan_summaries == []
    assert [row["unit_number"] for row in result.units] == ["102", "101"]
    assert result.tier_used == "TIER_1_DOM_STATIC_RESIDENCE_TABLE"
    assert result.winning_url == URL
    assert result.confidence == 0.95
