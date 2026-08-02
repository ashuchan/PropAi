"""RentVision current detail-table physical identity regression."""

from __future__ import annotations

from datetime import datetime

from ma_poc.pms.adapters.rentvision import parse_rentvision_unit_table
from ma_poc.scripts.runners.jugnu import (
    _emit_v2_units_for_property,
    _format_v2_unit,
)

BIRCH_POND = [
    ("Atlantic", "4", "31", "84", "10699934", 1305, "Available Now", "08/03/2026"),
    (
        "Atlantic",
        "5",
        "16",
        "37",
        "10699941",
        1280,
        "Available on <span>August 18, 2026</span>",
        "08/18/2026",
    ),
    (
        "Atlantic",
        "1",
        "19",
        "57",
        "10699892",
        1308,
        "Available on <span>September 16, 2026</span>",
        "09/16/2026",
    ),
    (
        "Brunswick",
        "2",
        "32",
        "90",
        "10699909",
        1469,
        "Available on <span>August 4, 2026</span>",
        "08/04/2026",
    ),
    (
        "Brunswick",
        "3",
        "30",
        "75",
        "10699920",
        1429,
        "Available on <span>August 8, 2026</span>",
        "08/08/2026",
    ),
    (
        "Brunswick",
        "2",
        "12",
        "18",
        "10699900",
        1420,
        "Available on <span>September 12, 2026</span>",
        "09/12/2026",
    ),
]


def _row(
    unit: str,
    building: str,
    apply_id: str,
    sightmap_id: str,
    rent: int,
    availability: str,
    move_in: str,
) -> str:
    return f"""
    <tr>
      <th class="left wrap">{unit}</th>
      <td class="standard wrap">{building}</td>
      <td class="standard identifiable-links right">
        Prices Starting at <span>${rent:,}</span>
      </td>
      <td class="standard unit-availability">{availability}</td>
      <td class="map-icon"><button onclick="openEngrainSightMapPopup(
        ['{sightmap_id}'], '{sightmap_id}')"></button></td>
      <td class="unit-actions"><button onclick="window.location =
        '?UnitId&#61;{apply_id}&amp;MoveInDate&#61;{move_in}'"></button></td>
    </tr>
    """


def _plan_html(plan: str) -> str:
    rows = [entry for entry in BIRCH_POND if entry[0] == plan]
    return f"<h1>{plan}</h1>" + "".join(_row(*entry[1:]) for entry in rows)


def test_complete_birch_pond_roster_survives_source_to_final() -> None:
    parsed = []
    for plan, slug in (
        ("Atlantic", "one-bedroom/atlantic"),
        ("Brunswick", "two-bedroom/brunswick"),
    ):
        parsed.extend(
            parse_rentvision_unit_table(
                _plan_html(plan),
                f"https://www.birchpondapts.com/floorplans/{slug}",
                plan,
            )
        )

    assert len(parsed) == 6
    assert {row["unit_id"] for row in parsed} == {apply_id for _, _, _, apply_id, *_ in BIRCH_POND}
    assert {row["source_ids"]["sightmap_unit_id"] for row in parsed} == {
        sightmap_id for _, _, _, _, sightmap_id, *_ in BIRCH_POND
    }
    assert [row["unit_number"] for row in parsed].count("2") == 2
    assert {row["building"] for row in parsed if row["unit_number"] == "2"} == {
        "32",
        "12",
    }
    formatted = [_format_v2_unit(row, datetime(2026, 8, 2, 12, 0), "75722") for row in parsed]
    final = _emit_v2_units_for_property(formatted)

    assert len(final) == 6
    assert len({row["unit_id"] for row in final}) == 6
    assert {row["unit_id"] for row in final} == {apply_id for _, _, _, apply_id, *_ in BIRCH_POND}


def test_building_qualified_fallback_is_bounded_to_missing_apply_id() -> None:
    html = _row("2", "7", "", "", 1400, "Available Now", "08/02/2026")
    rows = parse_rentvision_unit_table(
        html,
        "https://example.com/floorplans/two-bedroom/control",
        "Control",
    )

    assert rows[0]["unit_id"] == "7-2"
    assert rows[0]["unit_number"] == "2"
    assert rows[0]["building"] == "7"
