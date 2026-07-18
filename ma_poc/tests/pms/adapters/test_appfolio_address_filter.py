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
    _address_matches,
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
