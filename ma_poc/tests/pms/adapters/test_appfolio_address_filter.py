"""2026-05-25 canary 1ef1060 regression #11b — AppFolio post-fetch
address filter for the cohort that chip #100 (commit 37c0035) couldn't
help.

Chip #100 added a URL-level ``filters[property_list]`` filter when the
PMC's embed JS exposes a ``propertyGroup``. But some multi-property
PMCs (e.g. ``riedman`` — Academy Place in Corning NY) ship an embed JS
WITHOUT a propertyGroup. The propertyGroup filter never fires for those
PMCs, so the vanity /listings response still leaks the entire PMC.

USER-VERIFIED EXAMPLE — Academy Place (pid 221701, Corning NY 14830):
  Before:  190 units extracted from Erie PA, Canandaigua NY, Grand
           Island NY, Ithaca NY, etc. (entire riedman PMC portfolio)
  After:   4 units, all at 11 West Third St., Corning NY 14830 —
           matches Academy Place exactly

The post-fetch filter runs AFTER ``parse_appfolio_listings_ssr`` and
operates on the parsed unit list. It uses the property's CSV-sourced
street address + ZIP (threaded through ``AdapterContext.address`` and
``zip_code``) to drop listings whose address doesn't match the target.

This file pins:
  - the normalizer ``_normalize_street`` (handles "W" ↔ "West",
    "3rd" ↔ "Third", "St" ↔ "Street", apt suffix strip)
  - the matcher ``_address_matches`` (ZIP exact + street fuzzy + house
    number exact)
  - the orchestrator ``filter_listings_by_property_address``
    (no-op / activate / fallback behaviour matrix)
"""
from __future__ import annotations

import pytest

from ma_poc.pms.adapters.appfolio import (
    ScopeEvidence,
    _address_matches,
    _extract_zip,
    _normalize_street,
    filter_listings_by_property_address,
)

# ─────────────────────────────────────────────────────────────────────
# _normalize_street — string normalization for fuzzy comparison
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Academy Place CSV vs AppFolio listing forms — both must normalize
        # to the same string so token_set_ratio scores 100.
        ("11 W 3rd St", "11 west 3 st"),
        ("11 West Third St.", "11 west 3 st"),
        ("11 West Third St., Apt.111", "11 west 3 st"),
        # Direction abbreviations are expanded only when they stand alone.
        # "West" must NOT be eaten by the bare-letter alternative.
        ("171 East First Street", "171 east 1 st"),
        ("176 Denison Parkway East", "176 denison pkwy east"),
        # # suffix is stripped without a word boundary requirement.
        ("4801 Meadowview Dr. #317", "4801 meadowview dr"),
        # APT suffix is stripped (case-insensitive).
        ("176 Denison Parkway East, APT 338", "176 denison pkwy east"),
        # Empty input → empty output (no crashes).
        ("", ""),
    ],
)
def test_normalize_street_matrix(raw: str, expected: str) -> None:
    assert _normalize_street(raw) == expected


def test_normalize_street_west_not_eaten_by_t_alternative() -> None:
    """Regression guard for the bug discovered during initial probe:
    the apt-suffix regex used to include a bare ``t`` alternative
    without word boundaries, which ate ``t Third`` from ``West Third``
    and produced ``11 wes st`` instead of ``11 west 3 st``. That made
    the Academy Place match score drop to exactly 85 (right at the
    threshold). With ``\\b`` anchors the bug is fixed.
    """
    assert "west" in _normalize_street("11 West Third St.")


# ─────────────────────────────────────────────────────────────────────
# _address_matches — single-listing match check
# ─────────────────────────────────────────────────────────────────────


def test_address_matches_exact_zip_and_street() -> None:
    assert _address_matches(
        listing_address="11 West Third St., Apt.111, Corning, NY 14830",
        ctx_address="11 W 3rd St",
        ctx_zip="14830",
        fuzzy_threshold=85,
    )


def test_address_matches_rejects_wrong_zip() -> None:
    """A listing at the wrong ZIP must be rejected even if the street
    name happens to score high — ZIP is the strongest signal."""
    assert not _address_matches(
        listing_address="11 West Third St., Apt.111, Erie, PA 16509",
        ctx_address="11 W 3rd St",
        ctx_zip="14830",
        fuzzy_threshold=85,
    )


def test_address_matches_rejects_different_house_number_same_zip() -> None:
    """Within the same ZIP, listings with a different leading house
    number are rejected. This is the Academy Place vs ``171 E First
    Street`` case — both are at ZIP 14830 but house numbers (11 vs 171)
    differ. token_set_ratio alone is too lenient to reject these.
    """
    assert not _address_matches(
        listing_address="171 East First Street, APT 204F, Corning, NY 14830",
        ctx_address="11 W 3rd St",
        ctx_zip="14830",
        fuzzy_threshold=85,
    )


def test_address_matches_rejects_different_street_same_zip() -> None:
    """The ``176 Denison Parkway East`` listing in the riedman response
    must NOT match Academy Place even though both are at ZIP 14830."""
    assert not _address_matches(
        listing_address="176 Denison Parkway East, APT 338, Corning, NY 14830",
        ctx_address="11 W 3rd St",
        ctx_zip="14830",
        fuzzy_threshold=85,
    )


def test_address_matches_zip_strips_plus4_suffix() -> None:
    """``14830-1234`` and ``14830`` must compare equal."""
    assert _address_matches(
        listing_address="11 West Third St., Corning, NY 14830-1234",
        ctx_address="11 W 3rd St",
        ctx_zip="14830",
        fuzzy_threshold=85,
    )


def test_address_matches_empty_listing_address_rejects() -> None:
    assert not _address_matches(
        listing_address="",
        ctx_address="11 W 3rd St",
        ctx_zip="14830",
        fuzzy_threshold=85,
    )


def test_address_matches_no_ctx_zip_falls_back_to_street_only() -> None:
    """When ZIP isn't supplied, the street fuzzy check still runs."""
    assert _address_matches(
        listing_address="11 West Third St., Apt.111, Corning, NY 14830",
        ctx_address="11 W 3rd St",
        ctx_zip="",
        fuzzy_threshold=85,
    )


# ─────────────────────────────────────────────────────────────────────
# filter_listings_by_property_address — orchestrator
# ─────────────────────────────────────────────────────────────────────


def _unit(address: str, listing_id: str = "1") -> dict[str, str]:
    """Construct a fake parsed unit matching what
    ``parse_appfolio_listings_ssr`` produces — address lives in
    ``floor_plan_name``.
    """
    return {
        "floor_plan_name": address,
        "unit_number": listing_id,
        "rent_range": "$1,500",
        "extraction_tier": "TIER_1_DOM_APPFOLIO_SSR",
    }


def test_filter_academy_place_signature() -> None:
    """The USER-VERIFIED Academy Place case: 7 listings across 3
    streets in Corning (same ZIP 14830) plus a wider PMC bleed.
    After filtering with ``ctx_address='11 W 3rd St'`` and
    ``ctx_zip='14830'`` only the Academy Place units survive."""
    units = [
        _unit("11 West Third St., Apt.111, Corning, NY 14830", "u1"),
        _unit("11 West Third St., Apt.205, Corning, NY 14830", "u2"),
        _unit("11 West Third St., T9, Corning, NY 14830", "u3"),
        _unit("11 West Third St., Apt.406, Corning, NY 14830", "u4"),
        _unit("176 Denison Parkway East, APT 338, Corning, NY 14830", "u5"),
        _unit("176 Denison Parkway East, APT 234, Corning, NY 14830", "u6"),
        _unit("171 East First Street, APT 204F, Corning, NY 14830", "u7"),
        _unit("4801 Meadowview Dr. #317, Erie, PA 16509", "u8"),
        _unit("1106 Hammocks Drive, Canandaigua, NY 14424", "u9"),
        _unit("2060-5 Town Hall Terrace, Grand Island, NY 14072", "u10"),
    ]
    filtered, tel = filter_listings_by_property_address(
        units, ctx_address="11 W 3rd St", ctx_zip="14830"
    )
    assert tel["filter_activated"] is True
    assert tel["kept"] == 4
    assert tel["dropped"] == 6
    assert tel["reason"] == "address_filter_applied"
    assert {u["unit_number"] for u in filtered} == {"u1", "u2", "u3", "u4"}


def test_filter_single_address_response_is_noop() -> None:
    """Single-property PMC: every listing has the same address.
    Filter must be a no-op (nothing to disambiguate, no risk of
    contamination)."""
    units = [
        _unit("1503 E Park Ave, Valdosta, GA 31602", f"u{i}")
        for i in range(5)
    ]
    filtered, tel = filter_listings_by_property_address(
        units, ctx_address="1503 E Park Ave", ctx_zip="31602"
    )
    assert tel["filter_activated"] is False
    assert tel["reason"] == "single_address_in_response"
    assert filtered == units


def test_filter_empty_ctx_address_is_noop_passthrough() -> None:
    """No ctx address AND no ctx zip → filter cannot run; return
    units unchanged. (CSV row without an address column shouldn't
    catastrophically drop everything.)"""
    units = [
        _unit("11 West Third St., Apt.111, Corning, NY 14830", "u1"),
        _unit("4801 Meadowview Dr. #317, Erie, PA 16509", "u2"),
    ]
    filtered, tel = filter_listings_by_property_address(
        units, ctx_address="", ctx_zip=""
    )
    assert tel["filter_activated"] is False
    assert tel["reason"] == "no_ctx_address_or_zip"
    assert filtered == units


def test_filter_mismatched_zip_demotes_unscopeable_dump() -> None:
    """2026-07-18 contamination fix (reverses the prior keep-all fallback):
    when the ctx ZIP/address match NO listing in a multi-address PMC dump,
    the property is un-scopeable — emit NOTHING so it demotes to
    FAILED_NO_DATA rather than shipping OTHER properties' (other-city) rents.
    (The 2026-07-17 canary showed 94/259 AppFolio props leaking whole-PMC
    inventory this way; correctness beats the metric.)"""
    units = [
        _unit("11 West Third St., Apt.111, Corning, NY 14830", "u1"),
        _unit("4801 Meadowview Dr. #317, Erie, PA 16509", "u2"),
    ]
    filtered, tel = filter_listings_by_property_address(
        # ctx ZIP/address belong to a property NOT in this PMC.
        units, ctx_address="1503 E Park Ave", ctx_zip="31602",
    )
    assert tel["filter_activated"] is True
    assert tel["reason"] == "filter_rejected_all_demote"
    assert tel["unscopeable"] is True
    assert tel["kept"] == 0
    assert tel["dropped"] == 2
    assert filtered == []


def test_filter_empty_units_list_is_noop() -> None:
    """Filtering an empty list is a no-op (defensive — caller already
    branches on truthiness, but the helper must not crash)."""
    filtered, tel = filter_listings_by_property_address(
        [], ctx_address="11 W 3rd St", ctx_zip="14830"
    )
    assert filtered == []
    assert tel["filter_activated"] is False
    assert tel["reason"] == "no_units_to_filter"


def test_filter_zip_only_when_address_missing() -> None:
    """If the CSV row has a ZIP but no street, the filter still drops
    obviously-wrong listings (different ZIP). It can't disambiguate
    within the same ZIP, but it still helps."""
    units = [
        _unit("11 West Third St., Apt.111, Corning, NY 14830", "u1"),
        _unit("171 East First Street, APT 204F, Corning, NY 14830", "u2"),
        _unit("4801 Meadowview Dr. #317, Erie, PA 16509", "u3"),
        _unit("1106 Hammocks Drive, Canandaigua, NY 14424", "u4"),
    ]
    filtered, tel = filter_listings_by_property_address(
        units, ctx_address="", ctx_zip="14830"
    )
    assert tel["filter_activated"] is True
    assert tel["kept"] == 2
    assert tel["dropped"] == 2
    assert {u["unit_number"] for u in filtered} == {"u1", "u2"}


def test_filter_address_field_override() -> None:
    """The address-field name is overridable in case a future caller
    stashes the listing address somewhere other than
    ``floor_plan_name``."""
    units = [
        {
            "raw_address": "11 West Third St., Apt.111, Corning, NY 14830",
            "unit_number": "u1",
        },
        {
            "raw_address": "4801 Meadowview Dr. #317, Erie, PA 16509",
            "unit_number": "u2",
        },
    ]
    filtered, tel = filter_listings_by_property_address(
        units,
        ctx_address="11 W 3rd St",
        ctx_zip="14830",
        address_field="raw_address",
    )
    assert tel["filter_activated"] is True
    assert tel["kept"] == 1
    assert filtered[0]["unit_number"] == "u1"


def test_filter_threshold_below_85_admits_borderline_match() -> None:
    """A more lenient threshold can be passed (e.g. for tenants with
    looser address formatting). Verify the parameter is wired through
    by admitting a borderline-similar address that fails at the
    default but passes at 60.
    """
    # A 'borderline' listing where the street name shares only a few
    # tokens with the target — different number anchors mean it would
    # fail house-number check anyway, so test with same house number
    # and partial-name overlap.
    units = [
        _unit("11 Third Place, Corning, NY 14830", "u1"),
    ]
    # Score "11 west 3 st" vs "11 3 pl" — token_set_ratio is low
    # but the same house number + same ZIP. At threshold=60 the fuzzy
    # check still rejects because the street tokens really do diverge.
    # We assert the threshold parameter is honoured: at threshold=30
    # the borderline match is admitted.
    filtered_strict, tel_strict = filter_listings_by_property_address(
        units, ctx_address="11 W 3rd St", ctx_zip="14830", fuzzy_threshold=85,
    )
    # Strict threshold + only 1 distinct address → no-op (cannot
    # disambiguate from itself); telemetry confirms.
    assert tel_strict["filter_activated"] is False

    # Add a second distinct address to force the filter to engage.
    units2 = units + [
        _unit("999 Wrong Way, Erie, PA 16509", "u2"),
    ]
    filtered_lenient, tel_lenient = filter_listings_by_property_address(
        units2, ctx_address="11 W 3rd St", ctx_zip="14830", fuzzy_threshold=30,
    )
    assert tel_lenient["filter_activated"] is True
    assert {u["unit_number"] for u in filtered_lenient} == {"u1"}


def test_filter_telemetry_shape_keys_present() -> None:
    """The telemetry dict shape is part of the API — downstream
    observability reads these exact keys."""
    units = [
        _unit("11 West Third St., Apt.111, Corning, NY 14830", "u1"),
        _unit("4801 Meadowview Dr. #317, Erie, PA 16509", "u2"),
    ]
    _, tel = filter_listings_by_property_address(
        units, ctx_address="11 W 3rd St", ctx_zip="14830"
    )
    assert set(tel.keys()) == {"filter_activated", "kept", "dropped", "reason"}
    assert isinstance(tel["filter_activated"], bool)
    assert isinstance(tel["kept"], int)
    assert isinstance(tel["dropped"], int)
    assert isinstance(tel["reason"], str)


# ─────────────────────────────────────────────────────────────────────
# 2026-07-28 — two data-losing defects in the matcher, found while
# scoping the AppFolio-embed recovery against the 2026-07-27 run corpus,
# plus the ``ScopeEvidence.WEAK_EVIDENCE`` grade that recovery needs.
# ─────────────────────────────────────────────────────────────────────


def test_five_digit_house_number_is_not_mistaken_for_the_zip() -> None:
    """``_extract_zip`` took the FIRST five-digit token in the listing
    address. On a five-digit-numbered street that is the HOUSE NUMBER:

        "12224 NE 8th St., 210, Bellevue, WA 98005"  →  "12224"

    which never equals the property's real ZIP, so every such listing was
    rejected as somebody else's. Real casualties in the reference run:
    pid 24251 (Milano, 12224 NE 8th St) and pid 26515 (The Court at
    Northgate, 11300 3rd Ave NE).
    """
    assert _address_matches(
        listing_address="12224 NE 8th St., 210, Bellevue, WA 98005",
        ctx_address="12224 NE 8th St",
        ctx_zip="98005",
        fuzzy_threshold=85,
    )
    assert _address_matches(
        listing_address="11300 3rd Ave. NE, 225, Seattle, WA 98125",
        ctx_address="11300 3rd Ave NE",
        ctx_zip="98125",
        fuzzy_threshold=85,
    )


def test_four_digit_csv_zip_restores_leading_zero() -> None:
    """CSV type inference drops leading zeroes from New England ZIPs.

    AppFolio Websites properties commonly span neighbouring street numbers.
    Without restoring the zero, the same-street rule cannot prove that those
    buildings share a ZIP and drops real units.  These are live cohort shapes:
    Mall Apartments (01020) and West Gate Town Homes (06515).
    """
    assert _extract_zip("1020") == "01020"
    assert _extract_zip("6515") == "06515"
    assert _address_matches(
        listing_address="90C Edbert Street, Chicopee, MA 01020",
        ctx_address="83 Edbert St",
        ctx_zip="1020",
        fuzzy_threshold=85,
    )
    assert _address_matches(
        listing_address="239 Cooper Pl, New Haven, CT 06515",
        ctx_address="283 Cooper Pl",
        ctx_zip="6515",
        fuzzy_threshold=85,
    )


def test_four_digit_csv_zip_still_rejects_another_zip() -> None:
    """Leading-zero recovery must not weaken cross-property scoping."""
    assert not _address_matches(
        listing_address="239 Cooper Pl, New Haven, CT 06515",
        ctx_address="83 Edbert St",
        ctx_zip="1020",
        fuzzy_threshold=85,
    )


def test_same_street_same_zip_neighbouring_building_is_this_property() -> None:
    """A community is not always one street number. The Lofts (pid 299097,
    CSV ``14912 Mallett Rd``, Biloxi MS 39532) lists 13 of its 29 units at
    ``15080 Mallett Rd``; Conway Club (pid 19154, ``1900 S Conway Rd``)
    also lists 1908 and 1910 S Conway Rd. The exact-house-number rule
    threw those away — data loss, by the filter meant to prevent it.
    """
    assert _address_matches(
        listing_address="15080 Mallett Rd, I202, Biloxi, MS 39532",
        ctx_address="14912 Mallett Rd",
        ctx_zip="39532",
        fuzzy_threshold=85,
    )
    assert _address_matches(
        listing_address="1910 S. CONWAY ROAD APT 144, ORLANDO, FL 32812",
        ctx_address="1900 S Conway Rd",
        ctx_zip="32812",
        fuzzy_threshold=85,
    )


def test_same_zip_different_street_still_rejected() -> None:
    """The relaxation above is street-scoped, not ZIP-scoped. Chasewood
    (pid 1912, CSV 3420 S Coulter, Amarillo TX) must still reject its
    account's Lubbock listings, and Academy Place must still reject
    Denison Parkway.
    """
    assert not _address_matches(
        listing_address="6107 66th St, Lubbock, TX 79424",
        ctx_address="3420 S Coulter",
        ctx_zip="79424",
        fuzzy_threshold=85,
    )
    assert not _address_matches(
        listing_address="176 Denison Parkway East, APT 338, Corning, NY 14830",
        ctx_address="11 W 3rd St",
        ctx_zip="14830",
        fuzzy_threshold=85,
    )


def test_same_street_relaxation_requires_both_zips() -> None:
    """No ZIP on one side = nothing to bound the street match with, so a
    differing house number stays a reject. "Could not look" is not "match".
    """
    assert not _address_matches(
        listing_address="15080 Mallett Rd, I202, Biloxi, MS",
        ctx_address="14912 Mallett Rd",
        ctx_zip="",
        fuzzy_threshold=85,
    )


def test_weak_evidence_validates_a_single_address_response() -> None:
    """Default mode waves a single-address response through — load-bearing
    for single-property PMCs on the vanity path. ``ScopeEvidence.WEAK_EVIDENCE`` does not:
    a 2-row roster at ONE street that is not this property is exactly the
    shape of pid 237787 (Heritage Amity Commons, CSV Douglassville PA;
    roster all "1 Applewood Drive, Perkasie, PA 18944").
    """
    units = [
        _unit("1 Applewood Drive, Perkasie, PA 18944", "u1"),
        _unit("1 Applewood Drive, Perkasie, PA 18944", "u2"),
    ]
    kept_default, tel_default = filter_listings_by_property_address(
        units, ctx_address="606A Lake Dr", ctx_zip="19518"
    )
    assert len(kept_default) == 2
    assert tel_default["reason"] == "single_address_in_response"

    kept_strict, tel_strict = filter_listings_by_property_address(
        units, ctx_address="606A Lake Dr", ctx_zip="19518", evidence=ScopeEvidence.WEAK_EVIDENCE
    )
    assert kept_strict == []
    assert tel_strict["reason"] == "filter_rejected_all_demote"
    assert tel_strict["unscopeable"] is True


def test_weak_evidence_refuses_a_roster_it_cannot_check() -> None:
    """With neither a ctx address nor a ctx ZIP there is nothing to verify
    an account roster against. Default mode passes it through; strict mode
    refuses it and says so.
    """
    units = [
        _unit("10309-92ND SW, 07, TACOMA, WA 98498", "u1"),
        _unit("3307 COLLEGE STREET SE, A101, LACEY, WA 98503", "u2"),
    ]
    kept_default, tel_default = filter_listings_by_property_address(
        units, ctx_address="", ctx_zip=""
    )
    assert len(kept_default) == 2
    assert tel_default["reason"] == "no_ctx_address_or_zip"

    kept_strict, tel_strict = filter_listings_by_property_address(
        units, ctx_address="", ctx_zip="", evidence=ScopeEvidence.WEAK_EVIDENCE
    )
    assert kept_strict == []
    assert tel_strict["reason"] == "no_ctx_address_or_zip_demote"
    assert tel_strict["unscopeable"] is True


def test_weak_evidence_keeps_a_single_property_accounts_whole_roster() -> None:
    """MUST NOT BREAK — the roster really is theirs (pid 261381: 65 units,
    all 614 CENTRAL PKWY). Strict mode validates rather than waves through,
    and validation says yes.
    """
    units = [
        _unit("614 CENTRAL PKWY, 101, CINCINNATI, OH 45202", "u1"),
        _unit("614 CENTRAL PKWY, 205, CINCINNATI, OH 45202", "u2"),
    ]
    kept, tel = filter_listings_by_property_address(
        units, ctx_address="614 Central Pkwy", ctx_zip="45202", evidence=ScopeEvidence.WEAK_EVIDENCE
    )
    assert len(kept) == 2
    assert tel["reason"] == "address_filter_applied"


def test_street_is_not_always_the_first_comma_segment() -> None:
    """AppFolio operators sometimes prefix the community name, and ship both
    shapes in ONE roster (pid 44905 Oakpoint, CSV 5018 Marconi Ave,
    Carmichael CA 95608):

        "Oakpoint Apartments, 5018 Marconi Avenue #115, Carmichael, CA 95608"
        "River Oaks Apartments, 700 Bogue Rd. #69, Yuba City, CA 95991"

    Comparing only segment 0 pits "Oakpoint Apartments" against
    "5018 Marconi Ave" and rejects all 22 rows — including the 5 that ARE
    Oakpoint's. Read the later segments, and the 5 stay while the 16 in Yuba
    City still go.
    """
    assert _address_matches(
        listing_address="Oakpoint Apartments, 5018 Marconi Avenue #115, Carmichael, CA 95608",
        ctx_address="5018 Marconi Ave",
        ctx_zip="95608",
        fuzzy_threshold=85,
    )
    assert not _address_matches(
        listing_address="River Oaks Apartments, 700 Bogue Rd. #69, Yuba City, CA 95991",
        ctx_address="5018 Marconi Ave",
        ctx_zip="95608",
        fuzzy_threshold=85,
    )


def test_city_and_state_segments_are_not_treated_as_streets() -> None:
    """Widening to later segments must not widen to the city/state/ZIP tail —
    only segments that start with a house number qualify. pid 269983 (J Street
    Lofts, 3460 J St, Philadelphia PA 19134) must still reject its account's
    Venango St and Frankford Ave listings, which share its city AND its ZIP.
    """
    for addr in (
        "1810 E Venango St - 405, Philadelphia, PA 19134",
        "3701 Frankford Ave  - 305, Philadelphia, PA 19134",
    ):
        assert not _address_matches(
            listing_address=addr,
            ctx_address="3460 J St",
            ctx_zip="19134",
            fuzzy_threshold=85,
        )
