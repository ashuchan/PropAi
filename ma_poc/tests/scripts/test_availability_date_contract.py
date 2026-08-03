"""Production availability-date contract and provenance regressions.

The July 31 fleet audit found that direct Knock, G5, and generic adapter rows
carried future dates under ``availability_date`` while the production Jugnu
formatter read only ``available_date`` and replaced the missing value with the
capture date.  These tests exercise the production formatter as well as the
canonical formatter so their duplicate implementations cannot drift again.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ma_poc.core.schema_v2 import _format_v2_unit as core_format_unit
from ma_poc.scripts.runners.jugnu import _format_v2_unit as jugnu_format_unit

CAPTURE_TS = datetime(2026, 7, 31, 18, 30, tzinfo=UTC)
Formatter = Callable[..., dict[str, Any]]


@pytest.fixture(params=[core_format_unit, jugnu_format_unit], ids=["core", "jugnu"])
def formatter(request: pytest.FixtureRequest) -> Formatter:
    return request.param


def _raw_date(output: dict[str, Any]) -> str | None:
    return output.get("_available_date_raw") or output.get("available_date_raw")


@pytest.mark.parametrize(
    "date_key",
    [
        "available_date",
        "availability_date",
        "internalAvailableDate",
        "availableDate",
        "date_available",
        "dateAvailable",
    ],
)
def test_every_supported_alias_preserves_explicit_future_date(
    formatter: Formatter,
    date_key: str,
) -> None:
    output = formatter(
        {
            "unit_id": "1816",
            "floor_plan_name": "A1",
            date_key: "2026-09-08",
        },
        CAPTURE_TS,
        "property-1",
    )

    assert output["available_date"] == "2026-09-08"
    assert _raw_date(output) == "2026-09-08"
    assert output["availability_date_provenance"] == "explicit_future"


@pytest.mark.parametrize(
    ("adapter", "unit", "expected"),
    [
        (
            "knock",
            {
                "unit_number": "10201",
                "_floor_plan": "A3",
                "availability_date": "2026-10-06",
                "market_rent_low": 1895,
                "availability_status": "AVAILABLE",
            },
            "2026-10-06",
        ),
        (
            "g5",
            {
                "unit_number": "1816",
                "floor_plan_name": "B2",
                "availability_date": "2026-09-08T23:30:00-05:00",
                "market_rent_low": 2100,
            },
            "2026-09-08",
        ),
        (
            "generic_api",
            {
                "unit_id": "B104",
                "floor_plan_name": "B1",
                "availability_date": "2026-08-23",
                "rent_low": 1750,
            },
            "2026-08-23",
        ),
    ],
)
def test_live_validated_direct_adapter_shapes_survive_production_formatter(
    adapter: str,
    unit: dict[str, Any],
    expected: str,
) -> None:
    output = jugnu_format_unit(unit, CAPTURE_TS, f"property-{adapter}")

    assert output["available_date"] == expected
    assert output["available_date_raw"] == unit["availability_date"]
    assert output["availability_date_provenance"] == "explicit_future"


def test_knock_parser_to_production_formatter_preserves_future_date() -> None:
    from ma_poc.pms.adapters.knock import parse_knock_units

    [parsed] = parse_knock_units(
        {
            "units_data": {
                "layouts": [],
                "units": [
                    {
                        "name": "10201",
                        "layoutName": "A3",
                        "price": 1895,
                        "availableOn": "2026-10-06T00:00:00.000Z",
                    }
                ],
            }
        }
    )
    output = jugnu_format_unit(parsed, CAPTURE_TS, "knock-property")

    assert parsed["availability_date"] == "2026-10-06T00:00:00.000Z"
    assert output["available_date"] == "2026-10-06"
    assert output["availability_date_provenance"] == "explicit_future"


def test_g5_parser_to_production_formatter_preserves_future_date() -> None:
    from ma_poc.pms.adapters.g5 import parse_g5_apartments

    [parsed] = parse_g5_apartments(
        {
            "data": {
                "apartmentComplex": {
                    "apartments": [
                        {
                            "name": "1816",
                            "availabilityDate": "2026-09-08",
                            "prices": [{"value": "2100", "priceType": "min_rent"}],
                            "floorplan": {
                                "name": "B2",
                                "beds": 2,
                                "baths": 2,
                                "sqft": 1050,
                            },
                        }
                    ]
                }
            }
        }
    )
    output = jugnu_format_unit(parsed, CAPTURE_TS, "g5-property")

    assert parsed["availability_date"] == "2026-09-08"
    assert output["available_date"] == "2026-09-08"
    assert output["availability_date_provenance"] == "explicit_future"


def test_generic_api_parser_to_production_formatter_preserves_future_date() -> None:
    from ma_poc.pms.adapters._api_parser import parse_api_responses

    [parsed] = parse_api_responses(
        [
            {
                "url": "https://example.test/api/units",
                "body": {
                    "units": [
                        {
                            "id": "B104",
                            "model_id": "B1",
                            "beds": 1,
                            "baths": 1,
                            "rent": {"min": 1750, "max": 1750},
                            "sqft": {"min": 771, "max": 771},
                            "availableDate": "2026-08-23",
                        }
                    ]
                },
            }
        ]
    )
    output = jugnu_format_unit(parsed, CAPTURE_TS, "generic-property")

    assert parsed["availability_date"] == "2026-08-23"
    assert output["available_date"] == "2026-08-23"
    assert output["availability_date_provenance"] == "explicit_future"


def test_realpage_response_units_internal_date_survives_production_formatter() -> None:
    """Current api.ws.realpage.com wraps units under ``response.units`` and
    names the operator-facing date ``internalAvailableDate``.  This is the
    exact OneSite/OLL shape live-probed across six properties on August 1."""
    from ma_poc.pms.adapters._api_parser import realpage_units_to_adapter_shape

    [parsed] = realpage_units_to_adapter_shape(
        {
            "response": {
                "units": [
                    {
                        "id": 14185870,
                        "unitNumber": "128",
                        "rent": 1325,
                        "squareFeet": 730,
                        "floorplanName": "A1",
                        "internalAvailableDate": "2026-09-22 00:00 -0500",
                    }
                ]
            }
        },
        "https://api.ws.realpage.com/v2/property/8648527/units",
    )
    output = jugnu_format_unit(parsed, CAPTURE_TS, "plum-tree")

    assert parsed["availability_date"] == "2026-09-22"
    assert output["available_date"] == "2026-09-22"
    assert output["availability_date_provenance"] == "explicit_future"


@pytest.mark.parametrize(
    ("visible", "expected_date", "expected_provenance"),
    [
        ("Available Now", "2026-07-31", "available_now"),
        ("Available September 22, 2026", "2026-09-22", "explicit_future"),
    ],
)
def test_entrata_visible_date_token_survives_to_production_formatter(
    visible: str,
    expected_date: str,
    expected_provenance: str,
) -> None:
    from ma_poc.pms.adapters.entrata import parse_entrata_prospectportal_html

    [parsed] = parse_entrata_prospectportal_html(
        f"""
        <div class="fp-card">
          <div class="fp-title">A1</div>
          <div class="dynamic-text-before">1 Bed / 1 Bath</div>
          <div class="dynamic-text-after">700 sq. ft</div>
          <div class="fee-transparency-text">From $1,800 per month</div>
          <div class="availability">{visible}</div>
        </div>
        """,
        "https://example.test/city/property/conventional/",
    )

    output = jugnu_format_unit(parsed, CAPTURE_TS, "entrata-property")

    assert output["available_date"] == expected_date
    assert output["availability_date_provenance"] == expected_provenance


def test_yearless_numeric_date_uses_capture_year(formatter: Formatter) -> None:
    output = formatter(
        {
            "unit_number": "303",
            "market_rent_low": 3250,
            "availability_status": "AVAILABLE",
            "availability_date": "Available 9/1",
        },
        datetime(2026, 8, 1, 12, tzinfo=UTC),
        "cricket-flats",
    )

    assert output["available_date"] == "2026-09-01"
    assert output["availability_date_provenance"] == "explicit_future"


def test_visible_available_now_keeps_provenance(formatter: Formatter) -> None:
    output = formatter(
        {
            "unit_number": "406",
            "market_rent_low": 2850,
            "availability_status": "AVAILABLE",
            "availability_date": "Available Now",
        },
        datetime(2026, 8, 1, 12, tzinfo=UTC),
        "cricket-flats",
    )

    assert output["available_date"] == "2026-08-01"
    assert output["availability_date_provenance"] == "available_now"


def test_securecafe_apply_date_to_production_formatter_preserves_future_date() -> None:
    """SecureCafe's visible cell may say only ``Available`` while its public
    unit Apply URL carries the exact future date. Exercise parser -> the
    formatter production actually invokes, not either layer in isolation."""
    from ma_poc.pms.adapters.rentcafe import parse_securecafe_availableunits

    html = """
    <h1>Floor Plan: Autumn with W/D Connection - 1 Bedroom, 1 Bathroom</h1>
    <tr class='AvailUnitRow'>
      <th data-label='Apartment'>#2824</th>
      <td data-label='Rent'>$1,120</td>
      <td data-label='Date Available'>Available</td>
      <td data-label='Action'>
        <a href='rentaloptions.aspx?UnitID=46573837&amp;FloorPlanID=6091916&amp;MoveInDate=8%2F31%2F2026'>Apply</a>
      </td>
    </tr>
    """
    [parsed] = parse_securecafe_availableunits(html, "https://example.securecafe.com/availableunits.aspx")
    output = jugnu_format_unit(parsed, CAPTURE_TS, "black-hawk")

    assert parsed["availability_date"] == "8/31/2026"
    assert output["available_date"] == "2026-08-31"
    assert output["availability_date_provenance"] == "explicit_future"


def test_rentcafe_lt_row_date_to_production_formatter() -> None:
    from ma_poc.pms.adapters.rentcafe_layout_tab import parse_rentcafe_lt_applyga

    html = """
    <tr class="unit-container">
      <td class="td-card-available">Date: 9/25/2026</td>
      <td><a id="1325" onclick="applyGAClick(
        'A1R','1 Bed(s)','723','1419','1609','1325')">Apply</a></td>
    </tr>
    """
    [parsed] = parse_rentcafe_lt_applyga(html, "https://example.test/availableunits")
    output = jugnu_format_unit(parsed, CAPTURE_TS, "lexington-farms")

    assert parsed["availability_date"] == "9/25/2026"
    assert output["available_date"] == "2026-09-25"
    assert output["availability_date_provenance"] == "explicit_future"


def test_rentcafe_nestin_card_date_to_production_formatter() -> None:
    from ma_poc.pms.adapters._rentcafe_nestin import _parse_applyga_button_layout

    html = """
    <div class="card-body">
      <p>Date Available: 9/5/2026</p>
      <a id="4741-109" onclick="applyGAClick(
        'A1A','1 Bed(s)','638','1535','1711','4741-109')">Apply</a>
    </div>
    """
    [parsed] = _parse_applyga_button_layout(html, "https://example.test/a1a")
    output = jugnu_format_unit(parsed, CAPTURE_TS, "integra-sunrise")

    assert parsed["availability_date"] == "9/5/2026"
    assert output["available_date"] == "2026-09-05"
    assert output["availability_date_provenance"] == "explicit_future"


def test_udr_view_model_to_production_formatter_preserves_future_date() -> None:
    """UDR's Schema.org unit and first-party date model are joined before
    the production formatter sees the row."""
    from ma_poc.pms.adapters._udr import parse_udr_jsonld

    html = """
    <script type="application/ld+json">
    {"@type":"ItemList","itemListElement":[{"@type":"ListItem","item":{
      "@type":["Apartment","Product"],"name":"Apartment 720",
      "url":"?unitid=13665408","offers":{"price":3704,
      "availability":"https://schema.org/InStock"},
      "numberOfBedrooms":0,"numberOfBathroomsTotal":1}}]}
    </script>
    <script>
    window.udr.jsonObjPropertyViewModel = {"floorPlans":[{"units":[{
      "marketingName":"720","AvailableDateLabel":"9/25/2026"
    }]}]};
    </script>
    """
    [parsed] = parse_udr_jsonld(html, source_url="https://www.udr.com/example")
    output = jugnu_format_unit(parsed, CAPTURE_TS, "345-harrison")

    assert parsed["availability_date"] == "9/25/2026"
    assert output["available_date"] == "2026-09-25"
    assert output["availability_date_provenance"] == "explicit_future"


def test_entrata_visible_unit_date_to_production_formatter() -> None:
    """ProspectPortal JSON-LD omits the date, but its SSR card beside the
    same unit publishes it visibly."""
    from ma_poc.pms.adapters.entrata import parse_entrata_floorplan_html_jsonld

    html = """
    <script type="application/ld+json">{"@graph":[
      {"@type":"FloorPlan","name":"The Washington","numberOfBedrooms":1,
       "numberOfBathroomsTotal":1,"floorSize":{"value":460}},
      {"@type":"ItemList","itemListElement":[{"item":{
        "@type":"Apartment","name":"2-535T104","floorSize":{
          "value":460,"description":"Unit #2-535T104, Starting at $1477.0"
        }}}]}
    ]}</script>
    <div class="unit-details">
      <div class="available-unit__name">Unit #2-535T104</div>
      <div class="available-date">Available Sep 5</div>
    </div>
    """
    [parsed] = parse_entrata_floorplan_html_jsonld(
        html, "https://example.test/floor-plan/1-bedroom/washington.html"
    )
    output = jugnu_format_unit(parsed, CAPTURE_TS, "foxchase")

    assert parsed["availability_date"] == "Available Sep 5"
    assert output["available_date"] == "2026-09-05"
    assert output["availability_date_provenance"] == "explicit_future"


def test_short_form_precedence_and_empty_fallthrough(formatter: Formatter) -> None:
    preferred = formatter(
        {
            "unit_id": "1",
            "available_date": "2026-08-10",
            "availability_date": "2026-09-10",
        },
        CAPTURE_TS,
        "p",
    )
    fallback = formatter(
        {
            "unit_id": "2",
            "available_date": "",
            "availability_date": "2026-09-10",
        },
        CAPTURE_TS,
        "p",
    )

    assert preferred["available_date"] == "2026-08-10"
    assert fallback["available_date"] == "2026-09-10"


def test_visible_available_now_uses_capture_date_not_wall_clock(
    formatter: Formatter,
) -> None:
    output = formatter(
        {
            "unit_id": "now-1",
            "availability_date": "Available Now",
            "availability_status": "AVAILABLE",
        },
        CAPTURE_TS,
        "p",
    )

    assert output["available_date"] == "2026-07-31"
    assert output["availability_date_provenance"] == "available_now"


@pytest.mark.parametrize(
    ("raw_date", "expected_date", "expected_provenance"),
    [
        ("2026-07-31", "2026-07-31", "explicit_capture_date"),
        ("2026-07-01", "2026-07-01", "historical_embedded"),
        ("2100-01-01", "2026-07-31", "sentinel_clamped"),
    ],
)
def test_explicit_date_provenance_classes(
    formatter: Formatter,
    raw_date: str,
    expected_date: str,
    expected_provenance: str,
) -> None:
    output = formatter(
        {
            "unit_id": "dated-1",
            "availability_date": raw_date,
            "availability_status": "AVAILABLE",
        },
        CAPTURE_TS,
        "p",
    )

    assert output["available_date"] == expected_date
    assert output["availability_date_provenance"] == expected_provenance


def test_status_default_and_missing_are_distinguishable(
    formatter: Formatter,
) -> None:
    defaulted = formatter(
        {"unit_id": "default-1", "availability_status": "AVAILABLE"},
        CAPTURE_TS,
        "p",
    )
    missing = formatter({"unit_id": "missing-1"}, CAPTURE_TS, "p")

    assert defaulted["available_date"] == "2026-07-31"
    assert defaulted["availability_date_provenance"] == "capture_date_default"
    assert missing["available_date"] is None
    assert missing["availability_date_provenance"] == "missing"


@pytest.mark.parametrize(
    "status",
    ["UNAVAILABLE", "LEASED", "PENDING", "WAITLIST", "WAITLISTED"],
)
def test_negative_status_with_rent_never_receives_capture_date_default(
    formatter: Formatter,
    status: str,
) -> None:
    output = formatter(
        {
            "unit_id": f"negative-{status.lower()}",
            "market_rent_low": 1750,
            "availability_status": status,
        },
        CAPTURE_TS,
        "negative-status-property",
    )

    assert output["available_date"] is None
    assert output["availability_date_provenance"] == "missing"


@pytest.mark.parametrize("status", ["PENDING", "LEASED", "UNAVAILABLE"])
def test_negative_status_overrides_relative_available_now_only(
    formatter: Formatter,
    status: str,
) -> None:
    """Beechwood canary shape: stale relative text cannot beat current status."""
    output = formatter(
        {
            "unit_id": f"stale-now-{status.lower()}",
            "market_rent_low": 1750,
            "availability_status": status,
            "availability_date": "Available Now",
        },
        CAPTURE_TS,
        "negative-relative-property",
    )

    assert output["available_date"] is None
    assert output["availability_date_provenance"] == "negative_status_override"


def test_negative_raw_token_blocks_available_status_and_rent_defaults(
    formatter: Formatter,
) -> None:
    output = formatter(
        {
            "unit_id": "contradictory-negative-token",
            "market_rent_low": 1750,
            "availability_status": "AVAILABLE",
            "availability_date": "Not Available",
        },
        CAPTURE_TS,
        "negative-token-property",
    )

    assert output["available_date"] is None
    assert output["availability_date_provenance"] == "negative_or_unpublished"


def test_explicit_future_date_survives_even_with_negative_current_status(
    formatter: Formatter,
) -> None:
    output = formatter(
        {
            "unit_id": "future-offer",
            "market_rent_low": 1750,
            "availability_status": "UNAVAILABLE",
            "availability_date": "2026-09-12",
        },
        CAPTURE_TS,
        "future-offer-property",
    )

    assert output["available_date"] == "2026-09-12"
    assert output["availability_date_provenance"] == "explicit_future"


def test_plan_waitlist_status_survives_without_rent_or_unit_anchor(
    formatter: Formatter,
) -> None:
    output = formatter(
        {
            "floor_plan_name": "Waitlist Plan",
            "is_floor_plan_level": True,
            "availability_status": "WAITLIST",
        },
        CAPTURE_TS,
        "waitlist-property",
    )

    assert output["availability_status"] == "WAITLIST"
    assert output["available_date"] is None


def test_timezone_timestamp_preserves_operator_calendar_date(
    formatter: Formatter,
) -> None:
    output = formatter(
        {
            "unit_id": "tz-1",
            "availability_date": "2026-09-08T23:30:00-05:00",
        },
        CAPTURE_TS,
        "p",
    )

    assert output["available_date"] == "2026-09-08"
    assert output["availability_date_provenance"] == "explicit_future"


def test_daily_unit_export_keeps_raw_alias_and_provenance(tmp_path: Path) -> None:
    from ma_poc.scripts.email.daily_failures import (
        _SCRAPED_COLUMNS,
        _flatten_properties_json,
    )

    path = tmp_path / "properties.json"
    path.write_text(
        json.dumps(
            [
                {
                    "canonical_id": "p1",
                    "proj_name": "Alias Apartments",
                    "units": [
                        {
                            "unit_id": "1816",
                            "available_date": "2026-09-08",
                            "available_date_raw": "2026-09-08T23:30:00-05:00",
                            "availability_date_provenance": "explicit_future",
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )

    [row] = _flatten_properties_json(path)
    columns = dict(_SCRAPED_COLUMNS)

    assert row["available_date_raw"] == "2026-09-08T23:30:00-05:00"
    assert row["availability_date_provenance"] == "explicit_future"
    assert columns["availability_date_provenance"] == "Availability Date Provenance"
