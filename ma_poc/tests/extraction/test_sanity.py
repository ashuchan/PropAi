"""Tests for ``ma_poc.extraction.sanity``.

Covers each bound, the "null all aliases" guarantee, idempotency,
non-mutation, and the ABSENT sentinel preservation.

#69 adds :class:`TestClampIsRecorded` — the clamp must leave EVIDENCE.
A clamped value that is silently dropped is indistinguishable from
genuine operator absence, which makes every null rent / area sentinel in
a run undecomposable.
"""

from __future__ import annotations

import pytest

from ma_poc.extraction.canonical import (
    ABSENT,
    BEDS_KEYS,
    SQFT_KEYS,
    get_numeric,
)
from ma_poc.extraction.sanity import (
    REASON_ABOVE_MAX,
    REASON_BELOW_MIN,
    REASON_IMPLAUSIBLE_FOR_BEDS,
    UNKNOWN_TIER,
    clamp_ledger,
    clamp_tier_context,
    reset_clamp_ledger,
    sanity_bound,
)


@pytest.fixture(autouse=True)
def _isolate_clamp_ledger():
    """The ledger is a process-wide singleton — isolate every test."""
    reset_clamp_ledger()
    yield
    reset_clamp_ledger()

# ── Beds bounds ───────────────────────────────────────────────────────────────


class TestBedsBound:
    @pytest.mark.parametrize("value", [0, 1, 2, 3, 7])
    def test_in_range_preserved(self, value):
        result = sanity_bound({"beds": value})
        assert result.get("beds") == value
        assert "_sanity_dropped" not in result

    @pytest.mark.parametrize("value", [-1, 8, 99, 100])
    def test_out_of_range_nulled(self, value):
        # -1 is the ABSENT sentinel; it's already "not present" and skipped.
        # The other out-of-range values get nulled.
        result = sanity_bound({"beds": value})
        if value == -1:
            # Sentinel is treated as absent — no drop emitted, no null overwrite.
            assert "_sanity_dropped" not in result
            assert result.get("beds") == -1
        else:
            assert result.get("beds") is None
            assert "beds" in result.get("_sanity_dropped", [])

    def test_studio_zero_beds_preserved(self):
        """Studios have beds=0 — must NOT be nulled (zero is a real value)."""
        result = sanity_bound({"beds": 0})
        assert result.get("beds") == 0

    def test_out_of_range_alias_nulled_too(self):
        """If beds=99 is stored under the non-canonical ``bedrooms`` alias,
        sanity must null it. Otherwise a subsequent ``get_numeric`` falls
        through to find 99 again."""
        result = sanity_bound({"bedrooms": 99})
        # Both V2 canonical and the alias should be None
        assert result.get("bedrooms") is None
        assert get_numeric(result, BEDS_KEYS) is None

    def test_pascalcase_alias_nulled_case_insensitively(self):
        """If a raw producer (Windsor RentCafe) emits BedRooms=99 (PascalCase),
        case-insensitive matching catches it."""
        result = sanity_bound({"BedRooms": 99})
        assert result.get("BedRooms") is None


# ── Baths bounds ──────────────────────────────────────────────────────────────


class TestBathsBound:
    @pytest.mark.parametrize("value", [0, 1, 1.5, 2, 2.5, 10])
    def test_in_range_preserved(self, value):
        result = sanity_bound({"baths": value})
        assert result.get("baths") == value

    @pytest.mark.parametrize("value", [-0.5, 10.5, 100])
    def test_out_of_range_nulled(self, value):
        result = sanity_bound({"baths": value})
        assert result.get("baths") is None
        assert "baths" in result.get("_sanity_dropped", [])


# ── Sqft bounds ───────────────────────────────────────────────────────────────


class TestSqftBound:
    @pytest.mark.parametrize("value", [150, 500, 1019, 5000, 10000])
    def test_in_range_preserved(self, value):
        result = sanity_bound({"area": value})
        assert result.get("area") == value

    @pytest.mark.parametrize("value", [10, 100, 149, 10001, 50000])
    def test_out_of_range_nulled(self, value):
        result = sanity_bound({"area": value})
        assert result.get("area") is None
        assert "area" in result.get("_sanity_dropped", [])

    def test_absent_sentinel_preserved(self):
        """``area=-1`` is the explicit ABSENT sentinel — must survive sanity.
        ``get_numeric`` treats it as absent, so it never reaches the bounds
        check, and the dict value stays unchanged."""
        result = sanity_bound({"area": ABSENT})
        assert result.get("area") == ABSENT
        assert "_sanity_dropped" not in result

    def test_sqft_alias_under_pascalcase(self):
        """MinimumSqft=12000 from a Windsor payload → out-of-range → nulled."""
        result = sanity_bound({"MinimumSqft": 12000})
        assert get_numeric(result, SQFT_KEYS) is None


# ── Rent bounds ───────────────────────────────────────────────────────────────


class TestRentBound:
    @pytest.mark.parametrize("value", [200, 1500, 5000, 50000])
    def test_rent_low_in_range_preserved(self, value):
        result = sanity_bound({"rent_low": value})
        assert result.get("rent_low") == value

    @pytest.mark.parametrize("value", [-100, 0, 100, 199, 50001, 100000])
    def test_rent_low_out_of_range_nulled(self, value):
        result = sanity_bound({"rent_low": value})
        assert result.get("rent_low") is None
        assert "rent_low" in result.get("_sanity_dropped", [])

    @pytest.mark.parametrize("value", [200, 5000, 50000])
    def test_rent_high_in_range_preserved(self, value):
        result = sanity_bound({"rent_high": value})
        assert result.get("rent_high") == value

    def test_rent_high_out_of_range_nulled_independently_of_low(self):
        """rent_high can fail sanity while rent_low passes."""
        result = sanity_bound({"rent_low": 1500, "rent_high": 999999})
        assert result.get("rent_low") == 1500
        assert result.get("rent_high") is None


# ── Compound (multi-field) cases ──────────────────────────────────────────────


class TestCompoundCases:
    def test_unit_with_one_bad_field_keeps_others(self):
        """A row with beds=99 (bad) but otherwise good should keep its
        baths/area/rent. is_valid_unit will then accept via baths/area."""
        unit = {
            "beds": 99,
            "baths": 2,
            "area": 1019,
            "rent_low": 1500,
            "rent_high": 2000,
        }
        result = sanity_bound(unit)
        assert result.get("beds") is None
        assert result.get("baths") == 2
        assert result.get("area") == 1019
        assert result.get("rent_low") == 1500
        assert result.get("rent_high") == 2000
        assert result.get("_sanity_dropped") == ["beds"]

    def test_multiple_dropped_fields_recorded_in_order(self):
        unit = {"beds": 99, "baths": 100, "area": 5}
        result = sanity_bound(unit)
        # Drop order matches the function's check order
        assert result.get("_sanity_dropped") == ["beds", "baths", "area"]

    def test_all_fields_in_range_no_marker(self):
        unit = {"beds": 2, "baths": 2, "area": 1019, "rent_low": 1500}
        result = sanity_bound(unit)
        assert "_sanity_dropped" not in result

    def test_does_not_drop_a_row(self):
        """Sanity NEVER deletes the row — even if every field is bad,
        the returned dict still has the same keys (with None values)."""
        unit = {"beds": 99, "baths": 100, "area": 5, "rent_low": -1, "rent_high": 999999}
        result = sanity_bound(unit)
        assert isinstance(result, dict)
        # The dict is non-empty (carries the nulled keys + _sanity_dropped)
        assert len(result) > 0


# ── Idempotency + non-mutation ────────────────────────────────────────────────


class TestIdempotencyAndNonMutation:
    def test_does_not_mutate_input(self):
        unit = {"beds": 99, "area": 1019}
        before = dict(unit)
        sanity_bound(unit)
        assert unit == before

    def test_is_idempotent(self):
        """A second call on the result yields the same content."""
        unit = {"beds": 99, "baths": 2, "area": 1019}
        first = sanity_bound(unit)
        second = sanity_bound(first)
        assert first == second

    def test_empty_dict(self):
        result = sanity_bound({})
        assert result == {}

    def test_non_dict_input_returned_unchanged(self):
        assert sanity_bound(None) is None  # type: ignore[arg-type]
        assert sanity_bound("x") == "x"  # type: ignore[arg-type]


# ── Real-data regression ──────────────────────────────────────────────────────


class TestRealDataSanity:
    def test_skyline_kessler_neighborhood_passes_through_unchanged(self):
        """Skyline at Kessler nestiolistings item — no dimensions, no rent.
        Sanity has nothing to do; the dict comes out the same shape.
        is_valid_unit will reject it later."""
        unit = {"name": "Hoboken", "area": "Hudson County", "city": "Bayonne"}
        result = sanity_bound(unit)
        # ``area`` is a geographic string — get_numeric returns None — sanity
        # has nothing to bound — no drop emitted.
        assert "_sanity_dropped" not in result
        assert result["area"] == "Hudson County"  # untouched

    def test_maa_worthington_unit_passes_sanity(self):
        """Real MAA Worthington API item — all values in range."""
        unit = {
            "floor_plan_name": "Traditional 2x2 1019 SF",
            "beds": 2,
            "baths": 2,
            "area": 1019,
            "rent_low": 2680,
            "rent_high": 5165,
        }
        result = sanity_bound(unit)
        assert "_sanity_dropped" not in result
        assert result == {**unit}

    def test_dom_scan_balcony_sqft_100_dropped(self):
        """The 22658 countryridge case: DOM scan picked up "100 sq ft" from
        balcony text. After inference (if any), sanity catches sqft=100 as
        below the 150 floor and nulls it."""
        unit = {"floor_plan_name": "2BR / 1.5BA", "beds": 2, "baths": 1.5, "area": 100, "rent_low": 1125}
        result = sanity_bound(unit)
        assert result.get("area") is None
        assert "area" in result.get("_sanity_dropped", [])
        # Other fields survive — beds=2, baths=1.5, rent=1125 enough for is_valid_unit
        assert result.get("beds") == 2
        assert result.get("baths") == 1.5
        assert result.get("rent_low") == 1125


class TestCrossFieldSqftVsBeds:
    """2026-05-13 addition: catch sqft values that pass the absolute [150,
    10000] range but are impossibly small for the bed count. Tier-4 LLM
    hallucinations (LLM confused deposit/fee with sqft) accounted for 1,156
    such rows in the daily output.
    """

    def test_2br_at_310_sqft_nulls_sqft(self):
        """The Mansions at Sunset Ridge case: 2BR/2BA reported at 310 sqft.
        That's physically impossible — sqft is the LLM hallucination.
        Beds trusted; sqft nulled."""
        unit = {"floor_plan_name": "London", "beds": 2, "baths": 2, "area": 310, "rent_low": 2460}
        result = sanity_bound(unit)
        assert result.get("area") is None
        assert "area_implausible_for_beds" in result.get("_sanity_dropped", [])
        assert result.get("beds") == 2  # beds untouched
        assert result.get("baths") == 2

    def test_3br_at_173_sqft_nulls_sqft(self):
        """Cypress Grove case: '3 Bed 2 Bath Garden' reported at 173 sqft."""
        unit = {"floor_plan_name": "Three Bed, Two Bath Garden", "beds": 3, "baths": 2, "area": 173}
        result = sanity_bound(unit)
        assert result.get("area") is None
        assert "area_implausible_for_beds" in result.get("_sanity_dropped", [])

    def test_studio_at_200_sqft_passes(self):
        """A studio at 200 sqft is small but real (micro-units exist).
        Floor for 0BR is exactly 200 — must pass."""
        unit = {"beds": 0, "area": 200}
        result = sanity_bound(unit)
        assert result.get("area") == 200
        assert "_sanity_dropped" not in result

    def test_1br_at_400_sqft_passes(self):
        """Compact 1BR at 400 sqft is real (micro-units in dense cities).
        Floor for 1BR is 350 — 400 must pass."""
        unit = {"beds": 1, "area": 400}
        result = sanity_bound(unit)
        assert result.get("area") == 400

    def test_2br_at_700_sqft_passes(self):
        """Tight but real 2BR at 700 sqft passes (floor for 2BR is 500)."""
        unit = {"beds": 2, "area": 700}
        result = sanity_bound(unit)
        assert result.get("area") == 700

    def test_real_2br_at_950_sqft_passes(self):
        """Production Morgan Properties King's Manor case — 2BR/2BA @ 950 sqft.
        Must pass cleanly."""
        unit = {"beds": 2, "baths": 2, "area": 950, "rent_low": 1365}
        result = sanity_bound(unit)
        assert result == unit

    def test_no_beds_means_no_cross_field_check(self):
        """When beds is absent (None or missing), the cross-field check
        cannot fire — we don't know the floor to apply."""
        unit = {"area": 200}  # 200 is in the absolute [150, 10000] range
        result = sanity_bound(unit)
        assert result.get("area") == 200

    def test_no_sqft_means_no_cross_field_check(self):
        """When sqft is absent, nothing to null."""
        unit = {"beds": 2}
        result = sanity_bound(unit)
        assert "_sanity_dropped" not in result
        assert result.get("beds") == 2

    def test_sqft_already_nulled_by_absolute_range_is_idempotent(self):
        """If sqft was already nulled by the per-field range check
        (e.g. sqft=50 < 150 floor), the cross-field pass is a no-op."""
        unit = {"beds": 2, "area": 50}  # Below absolute 150 floor
        result = sanity_bound(unit)
        assert result.get("area") is None
        assert "area" in result.get("_sanity_dropped", [])
        # area_implausible_for_beds should NOT be added — area was already
        # nulled by the absolute-range pass, so the cross-field check sees None.
        assert "area_implausible_for_beds" not in result.get("_sanity_dropped", [])

    def test_5br_floor(self):
        """5BR @ 800 sqft is impossible (floor for 5BR is 1100)."""
        unit = {"beds": 5, "area": 800}
        result = sanity_bound(unit)
        assert result.get("area") is None
        assert "area_implausible_for_beds" in result.get("_sanity_dropped", [])

    def test_sanity_bound_remains_idempotent(self):
        """After cross-field nulling, a second call is a no-op."""
        unit = {"beds": 2, "area": 310}
        once = sanity_bound(unit)
        twice = sanity_bound(once)
        assert once == twice


# ── #69: a clamped value is RECORDED, not silently dropped ───────────────────

#: Table-test for the clamp predicate AND its reason code. Every row is
#: ``(field, value, expect_clamped, expect_reason)``. The must-NOT-match
#: rows are the point: an in-range or absent value must produce ZERO
#: ledger entries, or the ledger becomes noise nobody can aggregate.
_CLAMP_TABLE = [
    # ── must clamp (rent) ───────────────────────────────────────────────
    ("rent_low", 1, True, REASON_BELOW_MIN),           # $1 rent
    ("rent_low", 0, True, REASON_BELOW_MIN),           # $0 "call for pricing"
    ("rent_low", 199.99, True, REASON_BELOW_MIN),      # just under the floor
    ("rent_low", 8005551212, True, REASON_ABOVE_MAX),  # phone number as rent
    ("rent_high", 150000, True, REASON_ABOVE_MAX),     # comma-as-decimal
    # ── must clamp (area) ───────────────────────────────────────────────
    ("area", 45, True, REASON_BELOW_MIN),              # gross parse failure
    ("area", 1, True, REASON_BELOW_MIN),               # unit count as sqft
    ("area", 149, True, REASON_BELOW_MIN),             # just under the floor
    ("area", 1450, False, None),                       # a price-shaped sqft
    #   ^ 1450 is INSIDE [150, 10000]: the clamp cannot see a rent read as
    #     sqft when it lands in range. Documents the blind spot.
    ("area", 10001, True, REASON_ABOVE_MAX),           # just over the ceiling
    # ── must clamp (beds / baths) ───────────────────────────────────────
    ("beds", 99, True, REASON_ABOVE_MAX),
    ("baths", 11, True, REASON_ABOVE_MAX),
    ("beds", -2, True, REASON_BELOW_MIN),
    # ── must NOT clamp ──────────────────────────────────────────────────
    ("rent_low", 200, False, None),                    # exactly the floor
    ("rent_low", 50000, False, None),                  # exactly the ceiling
    ("rent_low", 1495, False, None),                   # ordinary rent
    ("area", 150, False, None),                        # exactly the floor
    ("area", 10000, False, None),                      # exactly the ceiling
    ("area", 750, False, None),                        # ordinary sqft
    ("beds", 0, False, None),                          # studio
    ("beds", 7, False, None),                          # max plan
    ("baths", 0, False, None),                         # no separate bath
    ("rent_low", None, False, None),                   # genuine absence
    ("area", ABSENT, False, None),                     # ABSENT sentinel
    ("beds", ABSENT, False, None),                     # sentinel wins over bound
    ("rent_low", "not a number", False, None),         # uncoercible
]


class TestClampIsRecorded:
    """The regression guard: a clamp must leave aggregable evidence."""

    @pytest.mark.parametrize(
        "field,value,expect_clamped,expect_reason",
        _CLAMP_TABLE,
        ids=[f"{f}={v!r}" for f, v, _c, _r in _CLAMP_TABLE],
    )
    def test_clamp_table(self, field, value, expect_clamped, expect_reason):
        result = sanity_bound({field: value}, tier="TIER_TEST", property_id="P9")
        ledger = clamp_ledger()
        evidence = result.get("_sanity_clamped", [])

        if not expect_clamped:
            # Must-NOT-match: nothing nulled, nothing recorded anywhere.
            assert evidence == [], f"{field}={value!r} recorded a phantom clamp"
            assert ledger.total() == 0
            return

        # The value is still nulled — emitted output is unchanged.
        assert result.get(field) is None
        # ...but it is no longer SILENT.
        assert len(evidence) == 1, f"{field}={value!r} left no evidence"
        rec = evidence[0]
        assert rec["field"] == field
        assert rec["reason"] == expect_reason
        assert rec["value"] == pytest.approx(float(value))
        assert rec["tier"] == "TIER_TEST"
        # ...and it is aggregable.
        assert ledger.total() == 1
        assert ledger.by_field(field)["TIER_TEST"] == 1
        sample = ledger.rows()[0]["examples"][0]
        assert sample["value"] == pytest.approx(float(value))
        assert sample["property_id"] == "P9"
        assert sample["reason"] == expect_reason

    def test_cross_field_area_clamp_is_recorded(self):
        """The sqft-too-small-for-beds pass records too, with its own reason."""
        result = sanity_bound({"beds": 2, "area": 310}, tier="TIER_4_LLM_DOM")
        assert result.get("area") is None
        evidence = result["_sanity_clamped"]
        assert [e["reason"] for e in evidence] == [REASON_IMPLAUSIBLE_FOR_BEDS]
        assert evidence[0]["value"] == pytest.approx(310.0)
        # Bound recorded is the per-beds floor, not the absolute range.
        assert evidence[0]["bounds"][0] == pytest.approx(500.0)
        assert clamp_ledger().by_field("area")["TIER_4_LLM_DOM"] == 1

    def test_pre_clamp_value_survives_the_alias_wipe(self):
        """The recorded value must outlive ``_null_all_aliases``.

        This is the whole point: after the clamp, NO alias and no
        ``<field>_raw`` twin holds the original — the ledger is the only
        surviving copy.
        """
        unit = {"sqft": 45, "area": 45, "minimum_sqft": 45}
        result = sanity_bound(unit, tier="T", property_id="P1")
        assert get_numeric(result, SQFT_KEYS) is None
        assert all(result[k] is None for k in ("sqft", "area", "minimum_sqft"))
        assert clamp_ledger().rows()[0]["examples"][0]["value"] == pytest.approx(45.0)

    def test_multiple_clamps_on_one_row_all_recorded(self):
        """A row with three junk fields records three entries, not one."""
        result = sanity_bound(
            {"unit_id": "A1", "beds": 99, "area": 45, "rent_low": 1},
            tier="TIER_3_DOM",
            property_id="P2",
        )
        assert [e["field"] for e in result["_sanity_clamped"]] == [
            "beds",
            "area",
            "rent_low",
        ]
        assert clamp_ledger().total() == 3
        assert clamp_ledger().by_tier("TIER_3_DOM") == {
            "beds": 1,
            "area": 1,
            "rent_low": 1,
        }
        # Joinable back to the row that lost the values.
        assert clamp_ledger().to_dict()["per_property"]["P2"] == {
            "beds:ABOVE_MAX": 1,
            "area:BELOW_MIN": 1,
            "rent_low:BELOW_MIN": 1,
        }

    def test_which_tier_produces_the_most_clamped_rents(self):
        """The named aggregation question must be answerable in one call."""
        for _ in range(3):
            sanity_bound({"rent_low": 1}, tier="TIER_3_DOM_GENERIC")
        sanity_bound({"rent_low": 1}, tier="TIER_1_API_SIGHTMAP")
        sanity_bound({"area": 45}, tier="TIER_3_DOM_GENERIC")
        assert clamp_ledger().by_field("rent_low").most_common(1) == [
            ("TIER_3_DOM_GENERIC", 3)
        ]

    def test_ambient_tier_context_attributes_the_clamp(self):
        with clamp_tier_context("TIER_1_API_RENTCAFE_SECURECAFE"):
            sanity_bound({"rent_low": 1})
        assert clamp_ledger().by_field("rent_low") == {
            "TIER_1_API_RENTCAFE_SECURECAFE": 1
        }

    def test_explicit_tier_beats_ambient_context(self):
        with clamp_tier_context("AMBIENT"):
            sanity_bound({"rent_low": 1}, tier="EXPLICIT")
        assert clamp_ledger().by_field("rent_low") == {"EXPLICIT": 1}

    def test_tier_falls_back_to_the_unit_dict_then_unknown(self):
        sanity_bound({"rent_low": 1, "extraction_tier": "TIER_2_JSONLD"})
        sanity_bound({"rent_low": 1})
        assert clamp_ledger().by_field("rent_low") == {
            "TIER_2_JSONLD": 1,
            UNKNOWN_TIER: 1,
        }

    def test_context_restored_when_body_raises(self):
        with pytest.raises(ValueError):
            with clamp_tier_context("X"):
                raise ValueError("boom")
        sanity_bound({"rent_low": 1})
        assert clamp_ledger().by_field("rent_low") == {UNKNOWN_TIER: 1}

    def test_recording_does_not_change_the_emitted_values(self):
        """Instrumentation is observability — emitted values are identical."""
        unit = {"beds": 2, "baths": 2, "area": 45, "rent_low": 1, "rent_high": 1495}
        result = sanity_bound(unit)
        emitted = {k: v for k, v in result.items() if not k.startswith("_sanity")}
        assert emitted == {
            "beds": 2,
            "baths": 2,
            "area": None,
            "rent_low": None,
            "rent_high": 1495,
        }

    def test_input_dict_is_still_never_mutated(self):
        unit = {"rent_low": 1}
        sanity_bound(unit)
        assert unit == {"rent_low": 1}

    def test_ledger_reset_clears_every_view(self):
        sanity_bound({"rent_low": 1}, tier="T", property_id="P")
        assert clamp_ledger().total() == 1
        reset_clamp_ledger()
        led = clamp_ledger()
        assert led.total() == 0
        assert led.rows() == []
        assert led.to_dict()["per_property"] == {}
