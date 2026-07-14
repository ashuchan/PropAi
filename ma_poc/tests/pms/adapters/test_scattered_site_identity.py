"""Tests for the scattered-site (AppFolio) marketing-address unit identity.

Background — 2026-07-14 identity-layer fix. "Scattered site" / small-PMC
AppFolio properties list each home by its FULL STREET ADDRESS. The raw feed
only gives us AppFolio's internal listing_id or a bare apartment suffix
("C", "#3", "APT 219"), neither of which is what a prospect sees on the
marketing page. Verified live against ``fairlawn.appfolio.com`` and
``terracemgmt.appfolio.com`` /listings: each card's identifier IS the full
address, with the apartment suffix inline ("400 Blake St #4110",
"6014 W. 25th St., #1032"); no numeric / uuid id is ever shown.

These tests lock in:
  * ``is_street_address`` — address-shape gate (rejects plan descriptors
    that also start with a digit).
  * ``address_unit_id`` — the marketing slug.
  * ``resolve_scattered_site_ids`` — listing-id disambiguation ONLY for a
    no-suffix address AppFolio lists more than once.
  * End-to-end through ``parse_appfolio_listings_ssr``.
"""
from __future__ import annotations

import pytest

from ma_poc.pms.adapters._parsing import (
    address_unit_id,
    is_street_address,
    resolve_scattered_site_ids,
)
from ma_poc.pms.adapters.appfolio import parse_appfolio_listings_ssr


# ── is_street_address — the safety gate ──────────────────────────────────────
@pytest.mark.parametrize(
    "addr",
    [
        "2323 East Main Street - APT 219, Richmond, VA 23223",
        "5480 Woodvale Court West, Westerville 43081",
        "203 Hull Street, 4B, Richmond, VA 23224",
        "3811 NE Royal View Avenue #A09, Vancouver, WA 98662",
        "200 Reserve Blvd., Charlottesville, VA 22901",
        "400 Blake St #4110, New Haven, CT 06515",
        "6014 W. 25th St., #1032, Speedway, IN 46224",
        "234 Sherman Ave, Meriden, CT",  # no ZIP but has street token
        # letter-suffixed house numbers (verified live on terracemgmt)
        "703A Sunflower St., Savoy, IL 61874",
        "90C Edbert Street, Chicopee, MA 01020",
    ],
)
def test_is_street_address_true(addr: str) -> None:
    assert is_street_address(addr) is True


@pytest.mark.parametrize(
    "not_addr",
    [
        "",
        "The Oakwood",
        "1 Bedroom, 1 Bath",  # digit-led plan descriptor, no street token/zip
        "2 Bed / 2 Bath",
        "550 Sqft Studio",  # digit-led, but no street token / ZIP / suffix
        "Studio",
        "WILLOW SPRINGS APTS - A-203",  # name-prefixed (no leading house #)
        "B06",
    ],
)
def test_is_street_address_false(not_addr: str) -> None:
    assert is_street_address(not_addr) is False


# ── address_unit_id — the marketing slug ─────────────────────────────────────
@pytest.mark.parametrize(
    ("addr", "expected"),
    [
        (
            "2323 East Main Street - APT 219, Richmond, VA 23223",
            "2323-east-main-street-apt-219-richmond-va-23223",
        ),
        # mid-string suffix is preserved (never cut at the first comma)
        (
            "203 Hull Street, 4B, Richmond, VA 23224",
            "203-hull-street-4b-richmond-va-23224",
        ),
        (
            "6014 W. 25th St., #1032, Speedway, IN 46224",
            "6014-w-25th-st-1032-speedway-in-46224",
        ),
        # double-space normalised to a single hyphen
        (
            "2525 E. Main Street  - APT 724, Richmond, VA 23223",
            "2525-e-main-street-apt-724-richmond-va-23223",
        ),
    ],
)
def test_address_unit_id_slug(addr: str, expected: str) -> None:
    assert address_unit_id(addr) == expected


def test_address_unit_id_empty_for_non_address() -> None:
    assert address_unit_id("1 Bedroom, 1 Bath") == ""
    assert address_unit_id("The Oakwood") == ""
    assert address_unit_id("") == ""


def test_two_suffixes_same_building_get_distinct_ids() -> None:
    a = address_unit_id("400 Blake St #4308, New Haven, CT 06515")
    b = address_unit_id("400 Blake St #4309, New Haven, CT 06515")
    assert a and b and a != b


# ── resolve_scattered_site_ids — no-suffix collision disambiguation ───────────
def _u(uid: str, lid: str | None) -> dict:
    sids = {"appfolio_listing_id": lid} if lid else {}
    return {"unit_id": uid, "source_ids": sids}


def test_resolver_leaves_unique_slugs_clean() -> None:
    units = [
        _u("400-blake-st-4308-new-haven-ct-06515", "1"),
        _u("400-blake-st-4309-new-haven-ct-06515", "2"),
    ]
    n = resolve_scattered_site_ids(units)
    assert n == 0
    assert units[0]["unit_id"] == "400-blake-st-4308-new-haven-ct-06515"
    assert units[1]["unit_id"] == "400-blake-st-4309-new-haven-ct-06515"


def test_resolver_disambiguates_no_suffix_collision_with_listing_id() -> None:
    # "234 Sherman Ave" listed twice (a 1bd and a 2bd) → append listing id.
    units = [
        _u("234-sherman-ave-meriden-ct", "5501"),
        _u("234-sherman-ave-meriden-ct", "5502"),
    ]
    n = resolve_scattered_site_ids(units)
    assert n == 2
    assert {u["unit_id"] for u in units} == {
        "234-sherman-ave-meriden-ct-5501",
        "234-sherman-ave-meriden-ct-5502",
    }


def test_resolver_falls_back_to_appfolio_id_then_listable_uid() -> None:
    units = [
        {"unit_id": "7524-southside-blvd", "source_ids": {"appfolio_id": "7839"}},
        {
            "unit_id": "7524-southside-blvd",
            "source_ids": {"appfolio_listable_uid": "abcdef123456"},
        },
    ]
    resolve_scattered_site_ids(units)
    assert units[0]["unit_id"] == "7524-southside-blvd-7839"
    assert units[1]["unit_id"] == "7524-southside-blvd-abcdef123456"


# ── end-to-end through the SSR parser ────────────────────────────────────────
def _ssr_card(listing_id: str, address: str, rent: str = "$1,500") -> str:
    return (
        f'<div data-listing-id="{listing_id}">'
        f'<span class="js-listing-blurb-rent">{rent}</span>'
        '<span class="js-listing-blurb-bed-bath">2 bd / 1 ba</span>'
        '<span class="js-listing-square-feet">Square Feet: 800</span>'
        f'<span class="js-listing-address">{address}</span>'
        "</div>"
    )


def test_ssr_scattered_site_uses_address_slug() -> None:
    html = _ssr_card("1377", "400 Blake St #4308, New Haven, CT 06515")
    units = parse_appfolio_listings_ssr(html, "https://x.appfolio.com/listings")
    assert len(units) == 1
    # unit_id is the marketing address slug — NOT the internal listing_id.
    assert units[0]["unit_id"] == "400-blake-st-4308-new-haven-ct-06515"
    # display suffix + provenance preserved
    assert units[0]["unit_number"] == "4308"
    assert units[0]["source_ids"]["appfolio_listing_id"] == "1377"


def test_ssr_no_suffix_duplicate_address_disambiguated() -> None:
    html = (
        _ssr_card("5501", "234 Sherman Ave, Meriden, CT", "$2,225")
        + _ssr_card("5502", "234 Sherman Ave, Meriden, CT", "$1,675")
    )
    units = parse_appfolio_listings_ssr(html, "https://x.appfolio.com/listings")
    assert len(units) == 2
    ids = {u["unit_id"] for u in units}
    # both anchored to the address, kept distinct by listing id
    assert ids == {
        "234-sherman-ave-meriden-ct-5501",
        "234-sherman-ave-meriden-ct-5502",
    }


def test_ssr_real_multifamily_plan_name_untouched() -> None:
    # floor_plan_name that is a plan/building name (not an address) must keep
    # the listing_id-or-suffix behaviour — the fix is a strict no-op here.
    html = _ssr_card("9001", "The Oakwood")
    units = parse_appfolio_listings_ssr(html, "https://x.appfolio.com/listings")
    assert len(units) == 1
    # strict no-op: the fix never sets unit_id for a non-address plan name;
    # unit_id is assigned downstream from unit_number (the listing_id here).
    assert "unit_id" not in units[0]
    assert units[0]["unit_number"] == "9001"
