"""AppFolio Wisconsin grid-address identity regression.

The 20-row ledger is the complete property-scoped Jade at North Hills roster
fetched on 2026-08-02.  Mutable values are intentionally omitted from the
fixture except where the source-to-final formatter requires a normal unit
shape; the public address, apartment suffix, and native listing ID are pinned.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ma_poc.pms.adapters._parsing import (
    address_unit_id,
    contains_street_address,
    is_street_address,
)
from ma_poc.pms.adapters.appfolio import parse_appfolio_listings_ssr
from ma_poc.scripts.runners.jugnu import (
    _emit_v2_units_for_property,
    _format_v2_unit,
)

JADE_ROSTER = [
    ("1155", "N72W12759 Good Hope Rd, Unit 107, Menomonee Falls, WI 53051"),
    ("3478", "N72W12759 Good Hope Rd, Unit 306, Menomonee Falls, WI 53051"),
    ("1343", "N72W12759 Good Hope Rd, Unit 315, Menomonee Falls, WI 53051"),
    ("1420", "N72W12823 Good Hope Rd, Unit 205, Menomonee Falls, WI 53051"),
    ("1347", "N72W12759 Good Hope Rd, Unit 110, Menomonee Falls, WI 53051"),
    ("1533", "N72W12801 Good Hope Rd, Unit 303, Menomonee Falls, WI 53051"),
    ("1430", "N72W12801 Good Hope Rd, Unit 305, Menomonee Falls, WI 53051"),
    ("3364", "N72W12801 Good Hope Rd, Unit 202, Menomonee Falls, WI 53051"),
    ("3335", "N72W12727 Good Hope Rd, Unit 208, Menomonee Falls, WI 53051"),
    ("3319", "N72W12759 Good Hope Rd, Unit 305, Menomonee Falls, WI 53051"),
    ("754", "N72W12759 Good Hope Rd, Unit 207, Menomonee Falls, WI 53051"),
    ("3261", "N72W12801 Good Hope Rd, Unit 201, Menomonee Falls, WI 53051"),
    ("3243", "N72W12801 Good Hope Rd, Unit 208, Menomonee Falls, WI 53051"),
    ("3224", "N72W12823 Good Hope Rd, Unit 212, Menomonee Falls, WI 53051"),
    ("718", "N72W12801 Good Hope Rd, Unit 313, Menomonee Falls, WI 53051"),
    ("764", "N72W12759 Good Hope Rd, Unit 302, Menomonee Falls, WI 53051"),
    ("3191", "N72W12823 Good Hope Rd, Unit 107, Menomonee Falls, WI 53051"),
    ("3006", "N72W12823 Good Hope Rd, Unit 106, Menomonee Falls, WI 53051"),
    ("1158", "N72W12801 Good Hope Rd, Unit 206, Menomonee Falls, WI 53051"),
    ("303", "N72W12801 Good Hope Rd, Unit 212, Menomonee Falls, WI 53051"),
]


def _card(listing_id: str, address: str) -> str:
    return f"""
    <article class="listing-item js-listing-item" data-listing-id="{listing_id}">
      <div class="js-listing-blurb-rent">$1,565</div>
      <div class="js-listing-blurb-bed-bath">1 bd / 1 ba</div>
      <div class="js-listing-square-feet">Square Feet: 747</div>
      <div class="js-listing-available">NOW</div>
      <span class="js-listing-address">{address}</span>
    </article>
    """


@pytest.mark.parametrize(
    "address",
    [
        "N72W12759 Good Hope Rd, Unit 107, Menomonee Falls, WI 53051",
        "W359N5890 Brown Street, Oconomowoc, WI 53066",
    ],
)
def test_wisconsin_grid_address_is_bounded_address_shape(address: str) -> None:
    assert is_street_address(address)
    assert contains_street_address(address)
    assert address_unit_id(address)


@pytest.mark.parametrize(
    "not_address",
    ["N72W12759", "N72W12759 Plan A1", "N72 W12759 Good Hope Rd"],
)
def test_grid_like_plan_tokens_do_not_pass_without_exact_grammar_and_context(
    not_address: str,
) -> None:
    assert not is_street_address(not_address)


def test_complete_jade_roster_survives_source_to_final_with_unique_ids() -> None:
    html = "".join(_card(listing_id, address) for listing_id, address in JADE_ROSTER)
    parsed = parse_appfolio_listings_ssr(
        html,
        "https://harmoniq.appfolio.com/listings"
        "?filters%5Bproperty_list%5D=Prop%20Group%20Jade%20at%20North%20Hills",
    )

    assert len(parsed) == 20
    assert all(row.get("unit_id") for row in parsed)
    assert {row["unit_id"] for row in parsed} == {
        address_unit_id(address) for _, address in JADE_ROSTER
    }
    formatted = [
        _format_v2_unit(row, datetime(2026, 8, 2, 12, 0), "302663")
        for row in parsed
    ]
    final = _emit_v2_units_for_property(formatted)

    assert len(final) == 20
    assert len({row["unit_id"] for row in final}) == 20
    assert {row["unit_id_raw"] for row in final} == {
        address_unit_id(address) for _, address in JADE_ROSTER
    }
    assert {row["source_ids"]["appfolio_listing_id"] for row in final} == {
        listing_id for listing_id, _ in JADE_ROSTER
    }
