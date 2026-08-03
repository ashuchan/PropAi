"""Entrata ProspectPortal building/address identity regressions.

The snippets below retain the exact semantic shape fetched from Phoenix
Orlando, Abberly Grove, and Seasons at Mount Pleasant on 2026-08-02: the
fourth unit spec has no text label but carries Entrata's ``lucide-building``
icon.  The values and native IDs are copied from those public responses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from ma_poc.pms.adapters.entrata import parse_entrata_pp_unit_cards
from ma_poc.scripts.runners.jugnu import (
    _emit_v2_units_for_property,
    _format_v2_unit,
)


def _card(*, unit: str, uid: str, building: str, rent: int = 1700) -> str:
    return f"""
    <article class="unit-card container-shape-dynamic unit-item-details-{uid}">
      <h3 class="unit-number">{unit}</h3>
      <div class="unit-specs">
        <div class="spec-item"><span>2 Bed</span></div>
        <div class="spec-item"><span>2 Bath</span></div>
        <div class="spec-item"><span>1,075 SqFt</span></div>
        <div class="spec-item">
          <svg class="lucide lucide-building h-4 w-4"></svg>
          <span>{building}</span>
        </div>
      </div>
      <div class="unit-pricing"><span class="price-value">${rent:,}</span></div>
      <span>Available Now</span>
      <button data-unit-id="{uid}"></button>
    </article>
    """


@pytest.mark.parametrize(
    ("url", "unit", "uid", "building"),
    [
        (
            "https://www.livephoenixorlando.com/floorplans/orlando-FL/"
            "the-phoenix-orlando/the-dahlia-545556-1/",
            "207",
            "4270134",
            "48",
        ),
        (
            "https://www.abberlygrove.com/floorplans/raleigh-NC/"
            "abberly-grove/hatteras-with-sunroom-23044-1/",
            "202",
            "100436",
            "09",
        ),
        (
            "https://www.seasonsmtpleasant.com/floorplans/mount-pleasant-WI/"
            "seasons-at-mount-pleasant/1b-1167440-1/",
            "106",
            "5092046",
            "Q 4441",
        ),
    ],
)
def test_current_icon_labelled_building_shape_is_retained(
    url: str,
    unit: str,
    uid: str,
    building: str,
) -> None:
    rows = parse_entrata_pp_unit_cards(_card(unit=unit, uid=uid, building=building), url)

    assert len(rows) == 1
    assert rows[0]["unit_number"] == unit
    assert rows[0]["building"] == building
    assert rows[0]["source_ids"]["entrata_uid"] == uid


@pytest.mark.parametrize(
    ("url", "cards", "expected_ids"),
    [
        (
            "https://www.livephoenixorlando.com/floorplans/orlando-FL/"
            "the-phoenix-orlando/the-dahlia-545556-1/",
            _card(unit="207", uid="4270134", building="48")
            + _card(unit="207", uid="4270158", building="60"),
            {"48-207", "60-207"},
        ),
        (
            "https://www.abberlygrove.com/floorplans/raleigh-NC/"
            "abberly-grove/hatteras-with-sunroom-23044-1/",
            _card(unit="201", uid="100432", building="07", rent=1975)
            + _card(unit="201", uid="100439", building="06", rent=2040)
            + _card(unit="202", uid="100436", building="09", rent=1995)
            + _card(unit="202", uid="100441", building="10", rent=2065),
            {"07-201", "06-201", "09-202", "10-202"},
        ),
        (
            "https://www.seasonsmtpleasant.com/floorplans/mount-pleasant-WI/"
            "seasons-at-mount-pleasant/1b-1167440-1/",
            _card(unit="106", uid="5092046", building="Q 4441")
            + _card(unit="106", uid="5072910", building="D 4320", rent=1750),
            {"Q 4441-106", "D 4320-106"},
        ),
    ],
)
def test_current_collision_shapes_survive_source_to_final_output(
    url: str,
    cards: str,
    expected_ids: set[str],
) -> None:
    parsed = parse_entrata_pp_unit_cards(cards, url)
    formatted = [
        _format_v2_unit(row, datetime(2026, 8, 2, 12, 0), "ENTRATA-TEST")
        for row in parsed
    ]

    final = _emit_v2_units_for_property(formatted)

    assert len(final) == len(expected_ids)
    assert {row["unit_id"] for row in final} == expected_ids
    assert len({row["source_ids"]["entrata_uid"] for row in final}) == len(final)


def test_unlabelled_fourth_spec_is_not_promoted_to_building() -> None:
    html = _card(unit="101", uid="12345", building="12").replace(
        'class="lucide lucide-building h-4 w-4"',
        'class="lucide lucide-calendar h-4 w-4"',
    )

    rows: list[dict[str, Any]] = parse_entrata_pp_unit_cards(
        html,
        "https://example.com/floorplans/example/example/a1-12345-1/",
    )

    assert rows[0]["building"] == ""
