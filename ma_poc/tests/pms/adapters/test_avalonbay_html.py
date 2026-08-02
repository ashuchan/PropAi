"""AvalonBay HTML embedded-JSON extraction tests (2026-05-23).

Background — the area-but-no-rent fix:
  43 properties in the canary's full-2c2a0af stamp TIER_1_5_EMBEDDED
  with SUCCESS_PLAN_LEVEL (no_rent_signal). They ARE Avalon sites —
  the generic embedded-JSON tier extracted unit_id + squareFeet from
  the Fusion CMS blob but missed the nested rent path
  startingAtPricesUnfurnished.prices.price entirely.

  Fix: AvalonBay adapter now (a) reads the nested rent path in
  parse_avalonbay_units, (b) carries an HTML-direct entry point
  parse_avalonbay_html that finds the Fusion units array and
  extracts every field, (c) falls through to HTML when API responses
  are empty.

Live-verified 2026-05-23 on 5 Avalon properties: 127 units with
rent+sqft+beds+plan_name across montville/meydenbauer/frisco/
alderwood/union (matches the canary's plan-only counts within
expected delta — those reflected only the available subset of total
inventory).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ma_poc.pms.adapters.avalonbay import (
    _extract_balanced_array,
    parse_avalonbay_html,
    parse_avalonbay_units,
)

# ─── parse_avalonbay_units — nested rent path ────────────────────────


def _avalon_unit_shape(
    unit_id: str = "AVB-NJ043-001-422",
    unit_name: str = "422",
    beds: int = 0,
    baths: int = 1,
    sqft: int = 507,
    price: int | None = 2090,
    net_eff_price: int | None = None,
    fp_name: str = "SM04.1",
) -> dict:
    """Build a unit dict matching the Avalon Fusion shape captured
    2026-05-23 from avaloncommunities.com/.../avalon-montville/."""
    u: dict = {
        "unitId": unit_id,
        "unitName": unit_name,
        "bedroomNumber": beds,
        "bathroomNumber": baths,
        "squareFeet": sqft,
        "floorPlan": {"name": fp_name},
        "floorNumber": "4",
        "availableDateUnfurnished": "2026-07-10T04:00:00+00:00",
    }
    if price is not None or net_eff_price is not None:
        prices: dict = {}
        if price is not None:
            prices["price"] = price
            prices["totalPrice"] = price + 100
        if net_eff_price is not None:
            prices["netEffectivePrice"] = net_eff_price
        u["startingAtPricesUnfurnished"] = {
            "moveInDate": "2026-07-10",
            "leaseTerm": 12,
            "appliedDiscount": 0,
            "prices": prices,
        }
    return u


def test_parse_units_extracts_nested_price_int() -> None:
    """The Fusion JSON ships price as an int literal (not a string).
    The old code path called money_to_int(int) which raised TypeError —
    the new _coerce_rent handles both."""
    items = [_avalon_unit_shape(price=2090)]
    units = parse_avalonbay_units(items, "https://x.com/avalon")
    assert len(units) == 1
    u = units[0]
    assert u["market_rent_low"] == 2090
    # squareFeet, beds, baths all flow through unchanged.
    assert u["sqft"] == "507"
    assert u["bedrooms"] == "0"
    assert u["bathrooms"] == "1"
    assert u["floor_plan_name"] == "SM04.1"
    assert u["unit_id"] == "AVB-NJ043-001-422"
    assert u["unit_number"] == "422"
    assert u["unit_name"] == "422"
    assert u["source_ids"] == {"avalonbay_unit_id": "AVB-NJ043-001-422"}


def test_native_ids_preserve_three_complete_avalon_roster_shapes() -> None:
    from ma_poc.scripts.runners.jugnu import _format_v2_unit

    cohorts = (
        ("ARLINGTON", 81, 47),
        ("MEYDENBAUER", 27, 27),
        ("MONTVILLE", 25, 25),
    )
    for property_code, source_count, visible_count in cohorts:
        items = [
            _avalon_unit_shape(
                unit_id=f"AVB-{property_code}-{index:03d}",
                unit_name=f"{index % visible_count:03d}",
                beds=index % 3,
                sqft=600 + index,
                price=2_000 + index,
                fp_name=f"PLAN-{index % 5}",
            )
            for index in range(source_count)
        ]

        parsed = parse_avalonbay_units(items, "https://avaloncommunities.com/test")
        formatted = [
            _format_v2_unit(
                row,
                datetime(2026, 8, 2, tzinfo=UTC),
                property_code,
            )
            for row in parsed
        ]

        assert len(parsed) == source_count
        assert len({row["unit_id"] for row in formatted}) == source_count
        assert len({row["unit_name"] for row in formatted}) == visible_count
        assert all(row["source_ids"]["avalonbay_unit_id"] == row["unit_id"] for row in formatted)


def test_parse_units_falls_back_to_net_effective_price() -> None:
    """When 'price' is missing but 'netEffectivePrice' is present
    (e.g. concession-only listings), use the net-effective value."""
    items = [_avalon_unit_shape(price=None, net_eff_price=1875)]
    units = parse_avalonbay_units(items, "https://x.com")
    assert units[0]["market_rent_low"] == 1875


def test_parse_units_prefers_flat_price_over_nested() -> None:
    """If a flat ``minRent`` is present (the legacy XHR shape), it must
    still win — don't reorder the parser's priority."""
    items = [
        {
            "unitId": "AVB-X-1",
            "unitName": "101",
            "bedroomNumber": 1,
            "bathroomNumber": 1,
            "squareFeet": 700,
            "floorPlan": {"name": "A1"},
            "minRent": 1500,  # legacy flat path
            "startingAtPricesUnfurnished": {"prices": {"price": 9999}},  # would-be nested
        }
    ]
    units = parse_avalonbay_units(items, "https://x.com")
    assert units[0]["market_rent_low"] == 1500  # flat wins


def test_parse_units_no_rent_when_all_paths_missing() -> None:
    """If neither flat nor nested is present and no bedroom-summary
    starting_rents was supplied, the parser produces a unit with no
    rent — that's still a valid plan-level row."""
    items = [_avalon_unit_shape(price=None, net_eff_price=None)]
    units = parse_avalonbay_units(items, "https://x.com")
    assert len(units) == 1
    assert units[0]["market_rent_low"] is None


def test_parse_units_summary_fallback_still_works() -> None:
    """Belt-and-braces: the original summary-by-bedroom-count fallback
    still fires when both per-unit paths are empty."""
    items = [_avalon_unit_shape(price=None, net_eff_price=None, beds=1)]
    summary = {
        "totalPricesStartingAt": {"1": {"unfurnished": 2400}},
    }
    units = parse_avalonbay_units(items, "https://x.com", summary=summary)
    assert units[0]["market_rent_low"] == 2400


# ─── _extract_balanced_array — bracket / brace / quote handling ──────


def test_extract_balanced_array_simple() -> None:
    html = "prefix [1, 2, 3] suffix"
    assert _extract_balanced_array(html, 7) == "[1, 2, 3]"


def test_extract_balanced_array_with_nested_objects() -> None:
    html = '[{"a": 1, "b": [2, 3]}, {"c": [4]}]'
    assert _extract_balanced_array(html, 0) == html


def test_extract_balanced_array_string_with_brackets_inside() -> None:
    """Bracket-like chars INSIDE a JSON string literal must not bump
    the depth counter."""
    html = '[{"name": "foo[bar]baz"}]'
    assert _extract_balanced_array(html, 0) == html


def test_extract_balanced_array_unbalanced_returns_empty() -> None:
    """Defensive: a truncated array (no matching close) returns ''
    rather than reading past EOF."""
    html = "[1, 2, 3"
    assert _extract_balanced_array(html, 0) == ""


def test_extract_balanced_array_wrong_start_char() -> None:
    """If the caller hands a non-'[' index, fail safely."""
    html = "abc"
    assert _extract_balanced_array(html, 0) == ""


# ─── parse_avalonbay_html — full end-to-end with embedded fixture ────


def _wrap_fusion_blob(units_array_json: str) -> str:
    """Wrap a units-array JSON string in minimal HTML mimicking the
    Avalon Fusion fusion-metadata layout."""
    return (
        "<html><head>"
        '<script id="fusion-metadata" type="application/javascript">'
        "window.Fusion=window.Fusion||{};"
        "Fusion.globalContent = {"
        '  "community": {"id":"AVB-NJ043", "name":"Avalon Test"},'
        '  "units": ' + units_array_json + "};"
        "</script></head><body></body></html>"
    )


def test_parse_html_extracts_full_unit_inventory() -> None:
    units_json = json.dumps(
        [
            _avalon_unit_shape(unit_id="AVB-X-001", unit_name="101", price=1500, sqft=600, beds=1, baths=1),
            _avalon_unit_shape(unit_id="AVB-X-002", unit_name="102", price=1750, sqft=750, beds=1, baths=1),
            _avalon_unit_shape(unit_id="AVB-X-003", unit_name="201", price=2400, sqft=1100, beds=2, baths=2),
        ]
    )
    html = _wrap_fusion_blob(units_json)
    units = parse_avalonbay_html(html, "https://avaloncommunities.com/test")
    assert len(units) == 3
    assert units[0]["market_rent_low"] == 1500
    assert units[0]["sqft"] == "600"
    assert units[0]["unit_number"] == "101"
    assert units[2]["market_rent_low"] == 2400
    assert units[2]["sqft"] == "1100"


def test_parse_html_empty_when_no_avalon_marker() -> None:
    """A page without the AVB- unit_id prefix MUST NOT extract — the
    parser would otherwise misfire on any site with a "units":[...]
    array (RentCafe, Entrata, etc. all use the same key name)."""
    html = '<html>{"units":[{"unitId":"foo","unitName":"1"}]}</html>'
    assert parse_avalonbay_html(html, "https://x.com") == []


def test_parse_html_empty_when_blob_truncated() -> None:
    """A page where the Fusion blob is truncated (network failure mid-
    SSR) must return [] rather than raising — the adapter's caller
    catches no exception, but a partial array would feed bogus units
    downstream."""
    units_json = '[{"unitId":"AVB-X-001","unitName":"101"'  # no close
    html = '<script id="fusion-metadata">Fusion.globalContent = {"units": ' + units_json
    assert parse_avalonbay_html(html, "https://x.com") == []


def test_parse_html_empty_string_input() -> None:
    assert parse_avalonbay_html("", "https://x.com") == []
    assert parse_avalonbay_html("<html>no Fusion here</html>", "https://x.com") == []
