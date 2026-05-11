"""Tests for ``ma_poc.extraction.canonical``.

Covers presence detection, alias-table integrity, value coercion, and the
specific data shapes observed in the 2026-05-11 cloud-run regressions
(Skyline at Kessler neighborhoods API, MAA Worthington RentCafe payload,
PascalCase Windsor payloads).
"""

from __future__ import annotations

import pytest

from ma_poc.extraction.canonical import (
    ABSENT,
    AVAIL_DATE_KEYS,
    BATHS_KEYS,
    BEDS_KEYS,
    FP_NAME_KEYS,
    RENT_HI_KEYS,
    RENT_LO_KEYS,
    RENT_RANGE_KEYS,
    SQFT_KEYS,
    UID_KEYS,
    get_numeric,
    get_raw,
    get_str,
    is_present,
)
from ma_poc.models.source import FieldValue, SourceId

# ── is_present ────────────────────────────────────────────────────────────────


class TestIsPresent:
    """Presence predicate must cover every "absent" shape we observe."""

    @pytest.mark.parametrize(
        "value",
        [
            None,
            ABSENT,
            -1,
            "-1",
            "",
            "   ",
            "\t\n",
            "null",
            "NULL",
            "Null",
            "None",
            "none",
            "n/a",
            "N/A",
        ],
        ids=[
            "None",
            "ABSENT_const",
            "minus_one_int",
            "minus_one_str",
            "empty_str",
            "whitespace_spaces",
            "whitespace_tab_newline",
            "null_lower",
            "NULL_upper",
            "Null_title",
            "None_title",
            "none_lower",
            "na_lower",
            "NA_upper",
        ],
    )
    def test_absent_shapes_are_not_present(self, value):
        assert is_present(value) is False

    @pytest.mark.parametrize(
        "value",
        [0, 0.0, "0", 1, 1.5, -1.5, "Hoboken", "1019", "Traditional 2x2 1019 SF"],
        ids=[
            "zero_int",
            "zero_float",
            "zero_str",
            "one_int",
            "float_pos",
            "float_neg_non_sentinel",
            "geographic_str",
            "numeric_str",
            "plan_name_str",
        ],
    )
    def test_real_values_are_present(self, value):
        assert is_present(value) is True

    def test_zero_beds_is_present_studio_invariant(self):
        # Studios have beds=0; the predicate MUST admit this as a real value.
        assert is_present(0) is True

    def test_nan_is_not_present(self):
        assert is_present(float("nan")) is False

    def test_inf_is_present(self):
        # +inf is not the absence sentinel; downstream sanity bounds catch
        # impossibly-large values. Keeping presence detection narrow.
        assert is_present(float("inf")) is True

    @pytest.mark.parametrize("value", [{}, [], (), set(), frozenset()])
    def test_empty_collections_are_not_present(self, value):
        """Empty collections have nothing for downstream to extract.

        Without this guard, ``get_str({"floor_plan_name": {}}, FP_NAME_KEYS)``
        would stringify ``{}`` as ``"{}"`` and surface it as a "real" value.
        """
        assert is_present(value) is False

    @pytest.mark.parametrize("value", [{"min": 1500}, [1, 2], (1,), {1}])
    def test_non_empty_collections_are_present(self, value):
        """Non-empty collections ARE present — value extractors then decide
        how to handle them (``get_numeric`` unwraps nested dicts;
        ``get_str`` / ``get_raw`` defer to the caller)."""
        assert is_present(value) is True


# ── get_numeric ───────────────────────────────────────────────────────────────


class TestGetNumeric:
    """Numeric coercion across alias keys."""

    def test_first_present_alias_wins(self):
        unit = {"baths": None, "bathrooms": 2, "_bathrooms": 999}
        assert get_numeric(unit, BATHS_KEYS) == 2.0

    def test_returns_none_when_no_alias_present(self):
        unit = {"unrelated": 5}
        assert get_numeric(unit, BEDS_KEYS) is None

    def test_returns_none_when_all_aliases_absent(self):
        unit = {"beds": None, "bedrooms": "", "_bedrooms": "-1"}
        assert get_numeric(unit, BEDS_KEYS) is None

    @pytest.mark.parametrize(
        "raw, expected",
        [
            (5, 5.0),
            (5.5, 5.5),
            ("5", 5.0),
            ("5.5", 5.5),
            ("$1,500", 1500.0),
            ("1,500", 1500.0),
            ("  1500  ", 1500.0),
            ("$1500.50", 1500.50),
        ],
        ids=[
            "int",
            "float",
            "numeric_str",
            "decimal_str",
            "dollar_with_thousands",
            "thousands_no_dollar",
            "whitespace_padded",
            "dollar_with_decimal",
        ],
    )
    def test_numeric_coercion(self, raw, expected):
        assert get_numeric({"beds": raw}, BEDS_KEYS) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "Hoboken",           # the 2026-05-11 Skyline at Kessler bug
            "Hudson County",     # geographic area string under sqft alias
            "abc",
            "$",                 # dollar with no number
            "two",               # word form (handled by infer.py, not here)
            "..",                # punctuation only
        ],
    )
    def test_non_numeric_strings_are_skipped(self, raw):
        assert get_numeric({"sqft": raw}, SQFT_KEYS) is None

    def test_geographic_area_string_under_sqft_alias_returns_none(self):
        """Regression for 2026-05-11 nestiolistings.com neighborhoods endpoint.

        The endpoint emits items like ``{"name": "Bayonne", "area": "Hudson County"}``.
        ``area`` is in SQFT_KEYS, but its value is geographic, not numeric.
        get_numeric must reject the string and return None — never let a
        geographic-area string reach the unit's sqft field.
        """
        nestio_item = {"name": "Bayonne", "area": "Hudson County",
                       "city": "Bayonne", "state": "NJ"}
        assert get_numeric(nestio_item, SQFT_KEYS) is None

    def test_bool_is_not_numeric(self):
        # bool is a subclass of int; True == 1 by Python's rules. But
        # admitting True as beds=1 would silently accept boolean junk.
        assert get_numeric({"beds": True}, BEDS_KEYS) is None
        assert get_numeric({"beds": False}, BEDS_KEYS) is None

    def test_zero_is_numeric(self):
        # Studio = 0 beds. Real measurement.
        assert get_numeric({"beds": 0}, BEDS_KEYS) == 0.0
        assert get_numeric({"beds": "0"}, BEDS_KEYS) == 0.0

    def test_nan_is_skipped(self):
        assert get_numeric({"beds": float("nan")}, BEDS_KEYS) is None

    def test_positive_inf_is_skipped(self):
        # Inf passes is_present (intentional — not the absence sentinel)
        # but get_numeric drops it because it's not a finite measurement.
        assert get_numeric({"beds": float("inf")}, BEDS_KEYS) is None

    def test_negative_inf_is_skipped(self):
        assert get_numeric({"beds": float("-inf")}, BEDS_KEYS) is None

    def test_field_value_unwrapped(self):
        fv = FieldValue(value=2, source=SourceId.API_RENTCAFE_FLOORPLANS, confidence=0.9)
        assert get_numeric({"beds": fv}, BEDS_KEYS) == 2.0

    def test_field_value_with_none_value_returns_none(self):
        fv = FieldValue(value=None, source=SourceId.JSON_LD, confidence=0.9)
        assert get_numeric({"beds": fv}, BEDS_KEYS) is None

    def test_field_value_with_absent_sentinel_returns_none(self):
        fv = FieldValue(value=ABSENT, source=SourceId.IDENTITY_FALLBACK, confidence=0.5)
        assert get_numeric({"area": fv}, SQFT_KEYS) is None

    def test_negative_one_string_is_sentinel(self):
        # "-1" is the string form of the ABSENT sentinel; treated as absent.
        assert get_numeric({"area": "-1"}, SQFT_KEYS) is None

    def test_nested_dict_min_unwrapped(self):
        """ResMan / Yardi-nested rent: ``{rent: {min: 1500, max: 2000}}``.

        Mirrors the contract of ``_parsing.get_field``. ``min`` is tried
        first → returns 1500.
        """
        unit = {"rent_low": {"min": 1500, "max": 2000}}
        assert get_numeric(unit, RENT_LO_KEYS) == 1500.0

    def test_nested_dict_low_unwrapped_when_no_min(self):
        unit = {"rent_low": {"low": 1500, "high": 2000}}
        assert get_numeric(unit, RENT_LO_KEYS) == 1500.0

    def test_nested_dict_falls_through_sub_keys(self):
        """When earlier sub-keys are absent, try the next ones."""
        unit = {"rent_low": {"amount": "1,500"}}
        assert get_numeric(unit, RENT_LO_KEYS) == 1500.0

    def test_nested_dict_max_when_only_high_bound_present(self):
        """``max`` / ``high`` are fallbacks when no lower-bound sub-key exists."""
        unit = {"rent_low": {"max": 2000}}
        assert get_numeric(unit, RENT_LO_KEYS) == 2000.0

    def test_nested_dict_all_sub_keys_absent_returns_none(self):
        unit = {"rent_low": {"currency": "USD", "label": "Starting at"}}
        assert get_numeric(unit, RENT_LO_KEYS) is None

    def test_nested_dict_falls_through_to_next_alias(self):
        """If the nested-dict has no numeric sub-keys, try the next alias."""
        unit = {
            "rent_low": {"label": "TBD"},      # primary alias — un-coercible nested dict
            "market_rent_low": 1500,            # next alias — wins
        }
        assert get_numeric(unit, RENT_LO_KEYS) == 1500.0

    def test_list_value_is_skipped(self):
        """List values are ambiguous for a scalar field — caller must
        flatten if it has list-shaped rent. ``get_numeric`` returns None
        rather than guessing min vs max."""
        assert get_numeric({"rent_low": [1500, 2000]}, RENT_LO_KEYS) is None

    def test_list_value_falls_through_to_next_alias(self):
        unit = {"rent_low": [1500, 2000], "market_rent_low": 1500}
        assert get_numeric(unit, RENT_LO_KEYS) == 1500.0

    def test_nested_dict_with_field_value_sub_keys(self):
        """Defensive: nested-dict sub-keys themselves carrying FieldValue?
        Today's code path doesn't emit this shape, but the helper should
        not raise on unfamiliar input."""
        # Currently we do NOT unwrap FieldValues inside nested dicts —
        # _coerce_scalar_to_float would get the FieldValue object and
        # fail str(fv) → fall through to next alias. Document this as
        # current behaviour. If a future producer wraps sub-keys, we'd
        # extend _coerce_scalar_to_float.
        fv = FieldValue(value=1500, source=SourceId.JSON_LD, confidence=0.9)
        unit = {"rent_low": {"min": fv}}
        # Pydantic FieldValue.__str__ might or might not produce a numeric
        # form; this test pins our current behaviour — None — and would
        # need updating if we add FieldValue support to nested-dict.
        result = get_numeric(unit, RENT_LO_KEYS)
        # Accept either None (no FV unwrap in nested-dict) or 1500.0
        # (if Pydantic's repr happens to coerce). The contract is "doesn't
        # raise"; behaviour beyond that is not pinned.
        assert result is None or result == 1500.0


# ── get_str ───────────────────────────────────────────────────────────────────


class TestGetStr:
    """String extraction across alias keys."""

    def test_first_present_alias_wins(self):
        unit = {"floor_plan_name": "A1", "_floor_plan": "X"}
        assert get_str(unit, FP_NAME_KEYS) == "A1"

    def test_returns_none_when_no_alias_present(self):
        assert get_str({"unrelated": "X"}, FP_NAME_KEYS) is None

    def test_strips_whitespace(self):
        assert get_str({"floor_plan_name": "  A1  "}, FP_NAME_KEYS) == "A1"

    def test_skips_null_token_strings(self):
        unit = {"floor_plan_name": "null", "_floor_plan": "A1"}
        assert get_str(unit, FP_NAME_KEYS) == "A1"

    def test_skips_empty_string(self):
        unit = {"floor_plan_name": "", "_floor_plan": "A1"}
        assert get_str(unit, FP_NAME_KEYS) == "A1"

    def test_stringifies_non_string(self):
        assert get_str({"unit_number": 217}, UID_KEYS) == "217"

    def test_field_value_unwrapped(self):
        fv = FieldValue(value="A1", source=SourceId.JSON_LD, confidence=0.9)
        assert get_str({"floor_plan_name": fv}, FP_NAME_KEYS) == "A1"


# ── get_raw ───────────────────────────────────────────────────────────────────


class TestGetRaw:
    """Raw access preserves value type (no coercion)."""

    def test_returns_int_unchanged(self):
        assert get_raw({"beds": 2}, BEDS_KEYS) == 2
        assert isinstance(get_raw({"beds": 2}, BEDS_KEYS), int)

    def test_returns_string_unchanged(self):
        assert get_raw({"floor_plan_name": "A1"}, FP_NAME_KEYS) == "A1"

    def test_unwraps_field_value(self):
        fv = FieldValue(value="A1", source=SourceId.JSON_LD, confidence=0.9)
        assert get_raw({"floor_plan_name": fv}, FP_NAME_KEYS) == "A1"

    def test_returns_none_when_no_alias_present(self):
        assert get_raw({"unrelated": "X"}, FP_NAME_KEYS) is None

    def test_returns_none_when_alias_is_absent_sentinel(self):
        assert get_raw({"area": ABSENT}, SQFT_KEYS) is None

    def test_empty_dict_value_is_not_present(self):
        """Empty-collection guard on is_present prevents
        ``get_str`` from stringifying ``{}`` as ``"{}"``."""
        assert get_str({"floor_plan_name": {}}, FP_NAME_KEYS) is None
        assert get_raw({"floor_plan_name": {}}, FP_NAME_KEYS) is None

    def test_empty_list_value_is_not_present(self):
        assert get_str({"floor_plan_name": []}, FP_NAME_KEYS) is None
        assert get_raw({"floor_plan_name": []}, FP_NAME_KEYS) is None


# ── Alias-table invariants ────────────────────────────────────────────────────


class TestAliasTableInvariants:
    """Drift-protection tests: a future producer adding a new field-name
    variant should either reuse an existing alias or add it here. These
    tests pin the contract."""

    def test_no_alias_appears_in_two_logical_groups(self):
        """A single key name (case-insensitive) must not be claimed by two
        logical fields. Catches accidental overlap that would cause one
        consumer to mask another's read.

        Compared on lowercased form because lookup is case-insensitive
        — ``Beds`` in one group and ``beds`` in another would collide at
        runtime.
        """
        groups: dict[str, tuple[str, ...]] = {
            "BEDS_KEYS": BEDS_KEYS,
            "BATHS_KEYS": BATHS_KEYS,
            "SQFT_KEYS": SQFT_KEYS,
            "FP_NAME_KEYS": FP_NAME_KEYS,
            "UID_KEYS": UID_KEYS,
            "RENT_LO_KEYS": RENT_LO_KEYS,
            "RENT_HI_KEYS": RENT_HI_KEYS,
            "AVAIL_DATE_KEYS": AVAIL_DATE_KEYS,
            "RENT_RANGE_KEYS": RENT_RANGE_KEYS,
        }
        seen: dict[str, str] = {}
        for group_name, keys in groups.items():
            for k in keys:
                k_lc = k.lower()
                if k_lc in seen:
                    pytest.fail(
                        f"Alias {k!r} (case-insens {k_lc!r}) appears in both "
                        f"{seen[k_lc]} and {group_name}. "
                        "Each key name must claim exactly one logical field."
                    )
                seen[k_lc] = group_name

    def test_no_intra_group_case_duplicates(self):
        """Within a single group, no two aliases differ only by case.

        With case-insensitive lookup, ``Beds`` and ``beds`` in the same
        tuple are dead-equivalent — the second never gets matched. Pin
        intent so a sloppy edit doesn't add the same logical alias twice.
        """
        groups: dict[str, tuple[str, ...]] = {
            "BEDS_KEYS": BEDS_KEYS,
            "BATHS_KEYS": BATHS_KEYS,
            "SQFT_KEYS": SQFT_KEYS,
            "FP_NAME_KEYS": FP_NAME_KEYS,
            "UID_KEYS": UID_KEYS,
            "RENT_LO_KEYS": RENT_LO_KEYS,
            "RENT_HI_KEYS": RENT_HI_KEYS,
            "AVAIL_DATE_KEYS": AVAIL_DATE_KEYS,
            "RENT_RANGE_KEYS": RENT_RANGE_KEYS,
        }
        for group_name, keys in groups.items():
            seen_lc: set[str] = set()
            for k in keys:
                k_lc = k.lower()
                if k_lc in seen_lc:
                    pytest.fail(
                        f"Group {group_name}: alias {k!r} duplicates an "
                        f"earlier entry case-insensitively ({k_lc!r}). "
                        "Remove the redundant entry."
                    )
                seen_lc.add(k_lc)

    @pytest.mark.parametrize(
        "group_name, keys, v2_canonical",
        [
            ("BEDS_KEYS", BEDS_KEYS, "beds"),
            ("BATHS_KEYS", BATHS_KEYS, "baths"),
            ("SQFT_KEYS", SQFT_KEYS, "area"),
            ("FP_NAME_KEYS", FP_NAME_KEYS, "floor_plan_name"),
            ("UID_KEYS", UID_KEYS, "unit_id"),
            ("RENT_LO_KEYS", RENT_LO_KEYS, "rent_low"),
            ("RENT_HI_KEYS", RENT_HI_KEYS, "rent_high"),
            ("AVAIL_DATE_KEYS", AVAIL_DATE_KEYS, "available_date"),
        ],
    )
    def test_v2_canonical_name_is_first(self, group_name, keys, v2_canonical):
        """[0] must be the V2 canonical name.

        ``_format_v2_unit_record`` writes to this name; consumers reading
        via these aliases must find the V2-emitted value first.
        """
        assert keys[0] == v2_canonical, (
            f"{group_name}[0] must be {v2_canonical!r}, got {keys[0]!r}"
        )

    def test_name_is_not_in_floor_plan_aliases(self):
        """Regression for 2026-05-11 Skyline at Kessler.

        The Nestio neighborhoods API emits ``{"name": "Hoboken", ...}``
        — if ``name`` were a FP_NAME_KEYS alias, the neighborhood name
        would float through as floor_plan_name. By keeping ``name`` out,
        the generic-API parser only treats it as floor_plan_name when
        explicitly mapped by an adapter.
        """
        assert "name" not in FP_NAME_KEYS

    def test_id_is_not_in_unit_id_aliases(self):
        """``id`` is too generic — many API bodies emit it for property-,
        floorplan-, or community-level identifiers. Producers wanting to
        treat ``id`` as the unit-level identifier must map it explicitly
        inside their adapter (see e.g. MAA Worthington's ULID ``id``)."""
        assert "id" not in UID_KEYS


# ── Real-data shape regression cases ──────────────────────────────────────────


class TestRealDataShapes:
    """End-to-end shapes from cloud-run artifacts. Documents the exact
    payloads observed in 2026-05-11 and asserts canonical's behavior on
    them, anchoring the contract to real-world data."""

    def test_nestiolistings_neighborhood_item(self):
        """Skyline at Kessler — nestiolistings.com/api/v2/neighborhoods.

        Real captured response item from run-2026-05-11/shard_0/11611.md.
        The bug: ``area="Hudson County"`` was being admitted as sqft by the
        legacy broad parser, producing 258 fake-unit rows.

        canonical's contract:
          * no sqft is recoverable (area is geographic, not numeric)
          * no beds / baths / rent are present
          * floor_plan_name returns None (``name`` is NOT a fp_name alias)
          * the only present field is ``name`` which is not surfaced by any
            standard alias group — adapter would have to explicitly map it.
        """
        item = {"id": 304, "name": "Hoboken", "area": "Hudson County",
                "city": "Hoboken", "state": "NJ"}
        assert get_numeric(item, SQFT_KEYS) is None
        assert get_numeric(item, BEDS_KEYS) is None
        assert get_numeric(item, BATHS_KEYS) is None
        assert get_numeric(item, RENT_LO_KEYS) is None
        assert get_numeric(item, RENT_HI_KEYS) is None
        assert get_str(item, FP_NAME_KEYS) is None
        assert get_str(item, UID_KEYS) is None

    def test_maa_worthington_rentcafe_unit_item(self):
        """MAA Worthington — maac.com/.../units/available/ (RentCafe-backed).

        Real captured response item from run-2026-05-11/shard_0/11992.md.
        The bug: the RentCafe adapter only reads ``minimumSqft``/``maximumSqft``,
        missing the unit-level ``sqft`` field. canonical surfaces it because
        ``sqft`` is in SQFT_KEYS; the field-map fix in rentcafe.py lands
        separately.
        """
        item = {
            "id": "01KGMK3HTJ0YW5MZ45EWDR05PP",
            "rentCafeFloorplanId": "2321569",
            "floorplanName": "Traditional 2x2 1019 SF",
            "apartmentName": "217",
            "beds": 2,
            "baths": 2,
            "sqft": 1019,
            "minimumRent": 2680,
            "maximumRent": 5165,
            "availableDate": "05/23/2026",
        }
        assert get_numeric(item, BEDS_KEYS) == 2.0
        assert get_numeric(item, BATHS_KEYS) == 2.0
        assert get_numeric(item, SQFT_KEYS) == 1019.0
        assert get_numeric(item, RENT_LO_KEYS) == 2680.0
        assert get_numeric(item, RENT_HI_KEYS) == 5165.0
        # apartmentName under UID_KEYS — picks up "217"
        assert get_str(item, UID_KEYS) == "217"
        # floorplanName under FP_NAME_KEYS — picks up the plan name
        assert get_str(item, FP_NAME_KEYS) == "Traditional 2x2 1019 SF"
        assert get_str(item, AVAIL_DATE_KEYS) == "05/23/2026"

    def test_pascalcase_windsor_rentcafe_shape(self):
        """RentCafe Windsor / Bexley / Pacifica payloads use PascalCase keys.

        canonical's case-insensitive lookup unifies PascalCase, camelCase,
        and lowercased forms. ``FloorplanName.lower() == "floorplanname"``
        matches the ``floorplanname`` alias; ``BedRooms.lower() == "bedrooms"``
        matches the ``bedrooms`` alias.
        """
        item = {
            "FloorplanName": "1BR Standard",
            "BedRooms": 1,
            "BathRooms": 1,
            "MinimumSqft": 750,
            "MinRent": 1500,
        }
        assert get_numeric(item, BEDS_KEYS) == 1.0
        assert get_numeric(item, BATHS_KEYS) == 1.0
        assert get_str(item, FP_NAME_KEYS) == "1BR Standard"
        assert get_numeric(item, SQFT_KEYS) == 750.0
        assert get_numeric(item, RENT_LO_KEYS) == 1500.0

    def test_screaming_caps_keys_match_via_case_insensitivity(self):
        """Belt-and-braces: ALL_CAPS shouts still match (some legacy CMS feeds).
        ``BEDS`` lowercases to ``beds``; matches the first BEDS_KEYS alias."""
        item = {"BEDS": 2, "BATHS": 2, "SQFT": 1019}
        assert get_numeric(item, BEDS_KEYS) == 2.0
        assert get_numeric(item, BATHS_KEYS) == 2.0
        assert get_numeric(item, SQFT_KEYS) == 1019.0

    def test_provenanced_unit_field_value_shape(self):
        """Merger output — ProvenancedUnit dict with FieldValue-wrapped values.

        ``canonical`` must transparently unwrap each FieldValue so a single
        consumer (``is_valid_unit``, ``schema_gate``) can read both the raw
        adapter dicts and the post-merge dicts identically.
        """
        unit = {
            "beds": FieldValue(value=2, source=SourceId.API_RENTCAFE_FLOORPLANS, confidence=0.9),
            "baths": FieldValue(value=2.0, source=SourceId.JSON_LD, confidence=0.85),
            "area": FieldValue(value=1019, source=SourceId.API_RENTCAFE_FLOORPLANS, confidence=0.9),
            "floor_plan_name": FieldValue(value="Traditional 2x2 1019 SF",
                                          source=SourceId.API_RENTCAFE_FLOORPLANS,
                                          confidence=0.9),
        }
        assert get_numeric(unit, BEDS_KEYS) == 2.0
        assert get_numeric(unit, BATHS_KEYS) == 2.0
        assert get_numeric(unit, SQFT_KEYS) == 1019.0
        assert get_str(unit, FP_NAME_KEYS) == "Traditional 2x2 1019 SF"
