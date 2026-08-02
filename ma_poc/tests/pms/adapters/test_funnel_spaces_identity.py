"""Funnel Spaces native identity and property-bound provenance regression.

The ledgers are the complete first-party rosters fetched on 2026-08-02:
Windsor Burnet (29), Cirrus (16), and The Estates at Cougar Mountain (9).
The August 1 audit observed one additional Windsor apartment; the 55-to-54
change is live inventory movement, not an expected parser drop.
"""

from __future__ import annotations

from datetime import datetime

from ma_poc.pms.adapters.funnel import parse_funnel_spaces_ssr
from ma_poc.scripts.runners.jugnu import (
    _emit_v2_units_for_property,
    _format_v2_unit,
)

# native id, display unit, plan id/name, beds, baths, area, rent, date,
# property asset id, source community name
ROSTERS = {
    "119144": [
        (
            "5376446",
            "2217",
            "271703",
            "A1",
            "1",
            "1",
            "580",
            "1313",
            "2026-09-20",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376422",
            "2129",
            "271704",
            "A2",
            "1",
            "1",
            "640",
            "1371",
            "2026-09-30",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376405",
            "2112",
            "271705",
            "A2A",
            "1",
            "1",
            "640",
            "1388",
            "2026-08-17",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376213",
            "1130",
            "271705",
            "A2A",
            "1",
            "1",
            "640",
            "1373",
            "2026-09-14",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376328",
            "1345",
            "271705",
            "A2A",
            "1",
            "1",
            "640",
            "1423",
            "2026-09-15",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376237",
            "1206",
            "271705",
            "A2A",
            "1",
            "1",
            "640",
            "1378",
            "2026-09-21",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376459",
            "2230",
            "271706",
            "A3",
            "1",
            "1",
            "683",
            "1448",
            "2026-07-02",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376396",
            "2101",
            "271706",
            "A3",
            "1",
            "1",
            "683",
            "1593",
            "2026-07-07",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376529",
            "2428",
            "271706",
            "A3",
            "1",
            "1",
            "683",
            "1473",
            "2026-07-13",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376370",
            "1431",
            "271706",
            "A3",
            "1",
            "1",
            "683",
            "1503",
            "2026-08-13",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376392",
            "1453",
            "271706",
            "A3",
            "1",
            "1",
            "683",
            "1503",
            "2026-08-28",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376497",
            "2332",
            "271706",
            "A3",
            "1",
            "1",
            "683",
            "1403",
            "2026-08-28",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376352",
            "1413",
            "271706",
            "A3",
            "1",
            "1",
            "683",
            "1558",
            "2026-09-04",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376391",
            "1452",
            "271706",
            "A3",
            "1",
            "1",
            "683",
            "1458",
            "2026-09-04",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376510",
            "2409",
            "271706",
            "A3",
            "1",
            "1",
            "683",
            "1473",
            "2026-09-11",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376262",
            "1231",
            "271706",
            "A3",
            "1",
            "1",
            "683",
            "1478",
            "2026-09-25",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376307",
            "1324",
            "271706",
            "A3",
            "1",
            "1",
            "683",
            "1481",
            "2026-10-06",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376251",
            "1220",
            "271706",
            "A3",
            "1",
            "1",
            "683",
            "1436",
            "2026-10-08",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376273",
            "1242",
            "271707",
            "A4",
            "1",
            "1",
            "722",
            "1438",
            "2026-08-28",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376322",
            "1339",
            "271707",
            "A4",
            "1",
            "1",
            "722",
            "1441",
            "2026-10-12",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376365",
            "1426",
            "271708",
            "A5",
            "1",
            "1",
            "785",
            "1593",
            "2026-08-07",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376537",
            "2436",
            "271708",
            "A5",
            "1",
            "1",
            "785",
            "1663",
            "2026-08-31",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376469",
            "2304",
            "271708",
            "A5",
            "1",
            "1",
            "785",
            "1538",
            "2026-09-01",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376209",
            "1126",
            "271708",
            "A5",
            "1",
            "1",
            "785",
            "1533",
            "2026-09-04",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376399",
            "2104",
            "271708",
            "A5",
            "1",
            "1",
            "785",
            "1558",
            "2026-09-14",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376235",
            "1204",
            "271708",
            "A5",
            "1",
            "1",
            "785",
            "1616",
            "2026-10-02",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376360",
            "1421",
            "271710",
            "B1",
            "2",
            "2",
            "1090",
            "1963",
            "2026-07-06",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376416",
            "2123",
            "271710",
            "B1",
            "2",
            "2",
            "1090",
            "1928",
            "2026-07-28",
            "267407",
            "Windsor Burnet",
        ),
        (
            "5376245",
            "1214",
            "271711",
            "B2",
            "2",
            "2",
            "1197",
            "2083",
            "2026-08-07",
            "267407",
            "Windsor Burnet",
        ),
    ],
    "58969": [
        ("5501401", "2808", "280345", "A10", "1", "1", "785", "3205", "2026-08-10", "301977", "Cirrus"),
        ("5501265", "1510", "280346", "A11", "1", "1", "839", "3260", "2026-06-29", "301977", "Cirrus"),
        ("5501243", "1210", "280346", "A11", "1", "1", "839", "3225", "2026-09-04", "301977", "Cirrus"),
        ("5501510", "3908", "280347", "A17", "1", "1", "760", "3480", "2026-07-22", "301977", "Cirrus"),
        ("5501418", "3005", "280352", "A8", "1", "1", "847", "3355", "2026-08-18", "301977", "Cirrus"),
        ("5501349", "2306", "280353", "A9", "1", "1", "855", "3185", "2026-03-31", "301977", "Cirrus"),
        ("5501439", "3206", "280353", "A9", "1", "1", "855", "3275", "2026-07-05", "301977", "Cirrus"),
        ("5501379", "2606", "280353", "A9", "1", "1", "855", "3215", "2026-08-07", "301977", "Cirrus"),
        ("5501399", "2806", "280353", "A9", "1", "1", "855", "3220", "2026-10-05", "301977", "Cirrus"),
        ("5501337", "2205", "280354", "B", "2", "2", "1202", "5021", "2026-08-24", "301977", "Cirrus"),
        ("5501517", "4007", "280356", "B3", "2", "2", "1289", "6538", "2026-07-08", "301977", "Cirrus"),
        ("5501169", "0603", "281949", "A22 1Bed1", "1", "1", "846", "3065", "2026-08-07", "301977", "Cirrus"),
        ("5501465", "3502", "283094", "A14", "1", "1", "927", "3510", "2026-08-18", "301977", "Cirrus"),
        ("5501256", "1501", "286129", "S8", "0", "1", "563", "2671", "2026-08-14", "301977", "Cirrus"),
        ("5501343", "2211", "346850", "A12", "1", "1", "737", "2860", "2026-10-08", "301977", "Cirrus"),
        ("5501312", "2002", "395991", "A13", "1", "1", "620", "2795", "2026-08-13", "301977", "Cirrus"),
    ],
    "26967": [
        (
            "5125433",
            "110412",
            "265967",
            "Cascade",
            "2",
            "2",
            "1273",
            "3195",
            "2026-08-31",
            "269991",
            "The Estates at Cougar Mountain",
        ),
        (
            "5125300",
            "050310",
            "265968",
            "Cedar",
            "1",
            "1",
            "701",
            "2355",
            "2026-08-09",
            "269991",
            "The Estates at Cougar Mountain",
        ),
        (
            "5125464",
            "130404",
            "265968",
            "Cedar",
            "1",
            "1",
            "701",
            "2246",
            "2026-08-11",
            "269991",
            "The Estates at Cougar Mountain",
        ),
        (
            "5125426",
            "110404",
            "265968",
            "Cedar",
            "1",
            "1",
            "701",
            "2323",
            "2026-09-20",
            "269991",
            "The Estates at Cougar Mountain",
        ),
        (
            "5125333",
            "060411",
            "265970",
            "Grand Ridge",
            "2",
            "2",
            "1112",
            "2993",
            "2026-07-10",
            "269991",
            "The Estates at Cougar Mountain",
        ),
        (
            "5125470",
            "130411",
            "265970",
            "Grand Ridge",
            "2",
            "2",
            "1112",
            "3082",
            "2026-08-10",
            "269991",
            "The Estates at Cougar Mountain",
        ),
        (
            "5125481",
            "140210",
            "265971",
            "Rainier",
            "2",
            "2",
            "1133",
            "3103",
            "2026-05-27",
            "269991",
            "The Estates at Cougar Mountain",
        ),
        (
            "5125279",
            "050109",
            "265972",
            "Sammamish",
            "2",
            "1",
            "1002",
            "2917",
            "2026-08-10",
            "269991",
            "The Estates at Cougar Mountain",
        ),
        (
            "5125328",
            "060405",
            "265975",
            "Taylor Ridge",
            "2",
            "2",
            "1058",
            "3023",
            "2026-07-13",
            "269991",
            "The Estates at Cougar Mountain",
        ),
    ],
}


def _card(row: tuple[str, ...]) -> str:
    unit_id, unit, plan_id, plan, beds, baths, area, rent, date, asset, community = row
    return f"""
    <article class="spaces-unit"
      data-spaces-id="{unit_id}" data-spaces-unit-id="{unit_id}"
      data-spaces-unit="{unit}" data-spaces-plan-id="{plan_id}"
      data-spaces-sort-plan-name="{plan}" data-spaces-sort-bed="{beds}"
      data-spaces-sort-bath="{baths}" data-spaces-sort-area="{area}"
      data-spaces-sort-price="{rent}" data-spaces-soonest="{date}"
      data-spaces-asset="{asset}" data-spaces-community="{community}"
      data-spaces-obj="unit" data-spaces-available="true">
    </article>
    """


def test_complete_current_spaces_rosters_survive_source_to_final() -> None:
    total = 0
    for property_id, expected_rows in ROSTERS.items():
        source_url = f"https://source.test/{property_id}/floorplans/"
        parsed = parse_funnel_spaces_ssr("".join(_card(row) for row in expected_rows), source_url)
        total += len(parsed)

        assert len(parsed) == len(expected_rows)
        assert len({row["unit_id"] for row in parsed}) == len(expected_rows)
        expected_by_id = {row[0]: row for row in expected_rows}
        for row in parsed:
            expected = expected_by_id[row["unit_id"]]
            unit_id, unit, plan_id, plan, beds, baths, area, rent, date, asset, community = expected
            assert row["unit_number"] == unit
            assert row["unit_name"] == unit
            assert row["floor_plan_name"] == plan
            assert row["bedrooms"] == beds
            assert row["bathrooms"] == baths
            assert row["sqft"] == area
            assert row["market_rent_low"] == int(rent)
            assert row["availability_date"] == date
            assert row["source_ids"] == {
                "funnel_spaces_unit_id": unit_id,
                "funnel_spaces_plan_id": plan_id,
                "funnel_spaces_asset_id": asset,
            }
            assert row["source_property_id"] == asset
            assert row["source_property_name"] == community
            assert row["source_api_url"] == source_url

        final = _emit_v2_units_for_property(
            [_format_v2_unit(row, datetime(2026, 8, 2, 12, 0), property_id) for row in parsed]
        )
        assert len(final) == len(expected_rows)
        assert {row["unit_id"] for row in final} == set(expected_by_id)
        for row in final:
            expected = expected_by_id[row["unit_id"]]
            _, unit, plan_id, plan, beds, baths, area, rent, date, asset, _ = expected
            assert row["unit_name"] == unit
            assert row["floor_plan_name"] == plan
            assert row["beds"] == int(beds)
            assert row["baths"] == float(baths)
            assert row["area"] == int(area)
            assert row["rent_low"] == int(rent)
            assert row["available_date"] == date
            assert row["source_ids"] == {
                "funnel_spaces_unit_id": row["unit_id"],
                "funnel_spaces_plan_id": plan_id,
                "funnel_spaces_asset_id": asset,
            }

    assert total == 54


def test_card_id_is_a_bounded_fallback_when_unit_id_attribute_is_absent() -> None:
    html = _card(ROSTERS["119144"][0]).replace(' data-spaces-unit-id="5376446"', "")
    rows = parse_funnel_spaces_ssr(html, "https://source.test/floorplans/")

    assert rows[0]["unit_id"] == "5376446"
    assert rows[0]["source_ids"]["funnel_spaces_unit_id"] == "5376446"
