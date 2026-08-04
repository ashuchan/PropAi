from __future__ import annotations

from types import SimpleNamespace

import pytest

from ma_poc.pms.property_scope import apply_collection_scope, apply_collection_scope_to_result


@pytest.mark.parametrize(
    ("property_id", "keep", "drop"),
    [
        ("264077", "Flats - A1", "Rise - A1"),
        ("78783", "Link A2", "Canvas A2"),
        ("98191", "Timber | B1", "Meredith House | B1"),
    ],
    ids=("novi-flats", "link-480", "timber"),
)
def test_verified_collection_keeps_only_configured_subcommunity(
    property_id: str, keep: str, drop: str
) -> None:
    rows, dropped = apply_collection_scope(
        [{"floor_plan_name": keep}, {"floor_plan_name": drop}],
        property_id=property_id,
        tier="TIER_1_API_RENTCAFE_SECURECAFE",
    )
    assert [row["floor_plan_name"] for row in rows] == [keep]
    assert dropped == 1


@pytest.mark.parametrize("property_id", ["52", "1000", "999999"])
def test_unconfigured_collection_is_unchanged(property_id: str) -> None:
    source = [{"floor_plan_name": "North A1"}, {"floor_plan_name": "South B1"}]
    rows, dropped = apply_collection_scope(
        source,
        property_id=property_id,
        tier="TIER_1_API_RENTCAFE_SECURECAFE",
    )
    assert rows == source
    assert dropped == 0


def test_configured_collection_fails_closed_when_no_row_matches() -> None:
    rows, dropped = apply_collection_scope(
        [{"floor_plan_name": "Rise - A1"}],
        property_id="264077",
        tier="TIER_1_API_RENTCAFE_SECURECAFE",
    )
    assert rows == []
    assert dropped == 1


def test_rule_does_not_touch_unrelated_provider() -> None:
    source = [{"floor_plan_name": "Rise - A1"}]
    rows, dropped = apply_collection_scope(
        source,
        property_id="264077",
        tier="TIER_1_API_SIGHTMAP",
    )
    assert rows == source
    assert dropped == 0


def test_final_boundary_removes_link_siblings_and_merges_cross_feed_aliases() -> None:
    rentcafe = [
        {
            "unit_id": unit_number,
            "unit_number": unit_number,
            "floor_plan_name": plan,
            "available_date": available_date,
            "availability_status": "AVAILABLE",
            "extraction_tier": "TIER_1_API_RENTCAFE_SECURECAFE",
            "source_ids": {"securecafe_floorplan_id": floorplan_id},
            "_inferred": True,
        }
        for unit_number, plan, available_date, floorplan_id in (
            ("312", "Link A1C | 1 Bed | 1 Bath", "8/5/2026", "5611041"),
            ("109", "Link A1C | 1 Bed | 1 Bath", "8/5/2026", "5611041"),
            ("408", "Link B1A | 2 Bed | 1 Bath", "8/27/2026", "5611049"),
        )
    ]
    marketing = [
        {
            "unit_id": provider_id,
            "unit_number": unit_number,
            "floor_plan_name": plan,
            "market_rent_low": rent,
            "available_date": "2026-07-10",
            "availability_status": "AVAILABLE",
            "extraction_tier": "TIER_1_DOM_JONAH_SSR_UNITS",
            "source_ids": {"jonah_id_value": provider_id},
            "source_property_id": "31718",
        }
        for provider_id, unit_number, plan, rent in (
            ("10644823", "312", "Link A1C", 2597),
            ("10644787", "109", "Link A1C", 2597),
            ("10644838", "408", "Link B1A", 3276),
            ("10637954", "199-303", "Canvas A1B", 2599),
            ("10646912", "502", "Flats A1A", 2500),
            ("8730898", "314", "Block A1A", 2400),
        )
    ]
    extract_result = SimpleNamespace(records=[*rentcafe, *marketing])
    result = {
        "units": [*rentcafe, *marketing],
        "plan_summaries": [],
        "extraction_tier_used": "TIER_1_API_RENTCAFE_SECURECAFE",
        "errors": [],
        "_extract_result": extract_result,
    }

    stats = apply_collection_scope_to_result(result, property_id="78783")

    assert stats == {"units_dropped": 3, "plans_dropped": 0, "aliases_deduped": 3}
    assert [row["unit_number"] for row in result["units"]] == ["312", "109", "408"]
    assert [row["unit_id"] for row in result["units"]] == ["10644823", "10644787", "10644838"]
    assert [row["available_date"] for row in result["units"]] == [
        "8/5/2026",
        "8/5/2026",
        "8/27/2026",
    ]
    assert [row["market_rent_low"] for row in result["units"]] == [2597, 2597, 3276]
    assert result["units"][0]["source_ids"] == {
        "jonah_id_value": "10644823",
        "securecafe_floorplan_id": "5611041",
    }
    assert result["units"][0]["unit_id_aliases"] == ["312"]
    assert extract_result.records is result["units"]
    assert "units_dropped=3" in result["errors"][0]


def test_final_boundary_scopes_novi_plan_channel_without_minting_unit_ids() -> None:
    result = {
        "units": [],
        "plan_summaries": [
            {"floor_plan_name": "Flats A1", "is_floor_plan_level": True, "rent_low": 1800},
            {"floor_plan_name": "Rise A1", "is_floor_plan_level": True, "rent_low": 1700},
        ],
        "extraction_tier_used": "TIER_1_API_RENTCAFE_SECURECAFE_PLAN_LEVEL",
        "errors": [],
    }

    stats = apply_collection_scope_to_result(result, property_id="264077")

    assert stats["plans_dropped"] == 1
    assert result["plan_summaries"] == [
        {"floor_plan_name": "Flats A1", "is_floor_plan_level": True, "rent_low": 1800}
    ]
    assert "unit_id" not in result["plan_summaries"][0]


def test_final_boundary_leaves_unconfigured_and_non_rentcafe_results_unchanged() -> None:
    unconfigured = {
        "units": [{"unit_number": "101", "floor_plan_name": "North A1"}],
        "extraction_tier_used": "TIER_1_API_RENTCAFE_SECURECAFE",
    }
    sightmap = {
        "units": [{"unit_number": "101", "floor_plan_name": "Rise A1"}],
        "extraction_tier_used": "TIER_1_API_SIGHTMAP",
    }

    assert apply_collection_scope_to_result(unconfigured, property_id="225785") == {
        "units_dropped": 0,
        "plans_dropped": 0,
        "aliases_deduped": 0,
    }
    assert unconfigured["units"][0]["floor_plan_name"] == "North A1"
    assert apply_collection_scope_to_result(sightmap, property_id="264077") == {
        "units_dropped": 0,
        "plans_dropped": 0,
        "aliases_deduped": 0,
    }
    assert sightmap["units"][0]["floor_plan_name"] == "Rise A1"
