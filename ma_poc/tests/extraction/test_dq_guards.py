"""Unit tests for ma_poc.extraction.dq_guards.

Covers every guard exposed by the shared DQ module:
  - canonicalize_status
  - normalize_unit_id
  - is_valid_sqft
  - is_rent_in_fee_context
  - detect_same_rent_leak
  - apply_unit_guards (the unified entry point used by adapter try_dom)

Smoke-tests + edge cases. Mirror the inline checks the AvalonBay /
Cortland adapters route through.
"""

from __future__ import annotations

import pytest

from ma_poc.extraction import dq_guards as dq


# ── canonicalize_status ───────────────────────────────────────────────────────


class TestCanonicalizeStatus:
    @pytest.mark.parametrize("raw,expected", [
        ("AVAILABLE", ("AVAILABLE", None)),
        ("available", ("AVAILABLE", None)),
        ("Avail", ("AVAILABLE", None)),
        ("OPEN", ("AVAILABLE", None)),
        ("Vacant", ("AVAILABLE", None)),
        ("true", ("AVAILABLE", None)),
        ("YES", ("AVAILABLE", None)),
        ("1", ("AVAILABLE", None)),
    ])
    def test_available_variants(self, raw, expected):
        assert dq.canonicalize_status(raw) == expected

    @pytest.mark.parametrize("raw,expected_subtype", [
        ("WAITLIST", "WAITLIST"),
        ("Wait List", "WAITLIST"),
        ("Sign Waitlist", "WAITLIST"),
        ("Coming Soon", "COMING_SOON"),
        ("PRELEASING", "COMING_SOON"),
        ("RESERVED", "RESERVED"),
        ("LEASED", "LEASED"),
        ("Rented", "LEASED"),
        ("Occupied", "LEASED"),
        ("Model Unit", "MODEL"),
        ("Maintenance", "OFF_MARKET"),
        ("Pending", "PENDING"),
    ])
    def test_unavailable_subtypes(self, raw, expected_subtype):
        status, subtype = dq.canonicalize_status(raw)
        assert status == "UNAVAILABLE"
        assert subtype == expected_subtype

    @pytest.mark.parametrize("raw", [None, "", "  ", "  \n\t"])
    def test_empty_or_none_is_unknown(self, raw):
        assert dq.canonicalize_status(raw) == ("UNKNOWN", None)

    def test_unrecognized_string_is_unknown(self):
        assert dq.canonicalize_status("foobar quux") == ("UNKNOWN", None)


# ── normalize_unit_id ─────────────────────────────────────────────────────────


class TestNormalizeUnitId:
    @pytest.mark.parametrize("raw,fpn", [
        ("A1", "A1"),
        ("a1", "A1"),
        ("studio a", "Studio A"),
    ])
    def test_equal_to_fpn_returns_none(self, raw, fpn):
        assert dq.normalize_unit_id(raw, fpn) is None

    @pytest.mark.parametrize("junk", [
        "Number", "Type", "Unit", "Apartment", "to", "from",
        "Sign Waitlist", "Click Here", "Apply Now", "#", "-",
    ])
    def test_junk_tokens_return_none(self, junk):
        assert dq.normalize_unit_id(junk, None) is None

    @pytest.mark.parametrize("good", [
        "101", "A-203", "PH-1", "Studio-04", "12B", "4518-C",
    ])
    def test_real_ids_pass(self, good):
        assert dq.normalize_unit_id(good, "Plan A") == good

    @pytest.mark.parametrize("raw", [None, "", "  ", "?", "/"])
    def test_empty_or_punct_returns_none(self, raw):
        assert dq.normalize_unit_id(raw, None) is None

    def test_strips_whitespace(self):
        assert dq.normalize_unit_id("  101  ", None) == "101"

    def test_no_fpn_argument_still_works(self):
        assert dq.normalize_unit_id("101", None) == "101"


# ── is_valid_sqft ─────────────────────────────────────────────────────────────


class TestIsValidSqft:
    @pytest.mark.parametrize("value", [200, 400, 700, 1500, 5000, 10000])
    def test_in_bounds(self, value):
        assert dq.is_valid_sqft(value) is True

    @pytest.mark.parametrize("value", [None, 0, 50, 100, 150, 199, 10001, 50000])
    def test_out_of_bounds(self, value):
        assert dq.is_valid_sqft(value) is False

    def test_string_input_coerces(self):
        assert dq.is_valid_sqft("750") is True
        assert dq.is_valid_sqft("not-a-number") is False

    @pytest.mark.parametrize("source_text", [
        "Apartment: #E-5314",
        "Apt 1004",
        "Suite 2100",
        "Unit 850",
        "#5103",
        "B-2105",
    ])
    def test_rejects_unit_id_shaped_sources(self, source_text):
        # Even when the value is in-bounds, a unit-id-shaped source rejects.
        # 5103/5314/etc. are themselves in-bounds; the SOURCE text reveals
        # the leak.
        assert dq.is_valid_sqft(5103, source_text) is False
        assert dq.is_valid_sqft(850, source_text) is False

    def test_plain_digit_source_passes(self):
        assert dq.is_valid_sqft(850, "850 sq ft") is True
        assert dq.is_valid_sqft(850, "850") is True


# ── is_rent_in_fee_context ────────────────────────────────────────────────────


class TestIsRentInFeeContext:
    @pytest.mark.parametrize("rent,html", [
        (50, '<dt>Application Fee</dt><dd>$50</dd>'),
        (250, 'Pet Deposit: $250'),
        (75, 'Admin Fee — $75'),
        (300, 'Security Deposit $300'),
        (200, 'Parking Fee: $200/mo'),
        (45, 'Move-in Fee $45'),
    ])
    def test_fee_context_rejects(self, rent, html):
        assert dq.is_rent_in_fee_context(rent, html) is True

    @pytest.mark.parametrize("rent,html", [
        (1450, 'Rent: $1,450 / month'),
        (1450, 'Monthly rent starting at $1,450'),
        (1450, 'Base rent $1,450'),
        (2200, 'Asking $2,200'),
    ])
    def test_real_rent_passes(self, rent, html):
        assert dq.is_rent_in_fee_context(rent, html) is False

    def test_none_inputs(self):
        assert dq.is_rent_in_fee_context(None, "anything") is False
        assert dq.is_rent_in_fee_context(1450, None) is False

    def test_no_match_no_block(self):
        # Rent value doesn't appear anywhere in HTML → don't block.
        assert dq.is_rent_in_fee_context(1450, "completely unrelated text") is False


# ── detect_same_rent_leak ─────────────────────────────────────────────────────


class TestDetectSameRentLeak:
    def test_nulls_inferred_id_same_rent_under_cap(self):
        units = [
            {"unit_id": "inferred_a", "market_rent_low": 675, "beds": 1},
            {"unit_id": "inferred_b", "market_rent_low": 675, "beds": 2},
            {"unit_id": "inferred_c", "market_rent_low": 675, "beds": 3},
        ]
        out = dq.detect_same_rent_leak(units)
        for u in out:
            assert u["market_rent_low"] is None
            assert u.get("_same_rent_leak_nulled") is True

    def test_above_cap_does_not_fire(self):
        units = [
            {"unit_id": "inferred_a", "market_rent_low": 2400, "beds": 1},
            {"unit_id": "inferred_b", "market_rent_low": 2400, "beds": 2},
            {"unit_id": "inferred_c", "market_rent_low": 2400, "beds": 3},
        ]
        out = dq.detect_same_rent_leak(units)
        for u in out:
            assert u["market_rent_low"] == 2400

    def test_real_unit_ids_does_not_fire(self):
        units = [
            {"unit_id": "4518-C", "market_rent_low": 720},
            {"unit_id": "4518-G", "market_rent_low": 720},
            {"unit_id": "4518-L", "market_rent_low": 720},
        ]
        out = dq.detect_same_rent_leak(units)
        for u in out:
            assert u["market_rent_low"] == 720

    def test_only_two_does_not_fire(self):
        units = [
            {"unit_id": "inferred_a", "market_rent_low": 675},
            {"unit_id": "inferred_b", "market_rent_low": 675},
        ]
        out = dq.detect_same_rent_leak(units)
        for u in out:
            assert u["market_rent_low"] == 675

    def test_empty_input_returns_empty(self):
        assert dq.detect_same_rent_leak([]) == []


# ── apply_unit_guards (the unified entry) ─────────────────────────────────────


class TestApplyUnitGuards:
    def test_status_canonicalized(self):
        units = [{"unit_id": "101", "availability_status": "WAITLIST"}]
        out = dq.apply_unit_guards(units, property_id="TEST")
        assert out[0]["availability_status"] == "UNAVAILABLE"
        assert out[0]["_avail_subtype"] == "WAITLIST"

    def test_unit_id_normalised(self):
        units = [{"unit_id": "A1", "floor_plan_name": "A1"}]
        out = dq.apply_unit_guards(units, property_id="TEST")
        assert out[0]["unit_id"] is None
        assert out[0]["_inferred_id"] is True

    def test_real_unit_ids_pass_through(self):
        units = [{"unit_id": "101", "floor_plan_name": "A1", "availability_status": "AVAILABLE"}]
        out = dq.apply_unit_guards(units, property_id="TEST")
        assert out[0]["unit_id"] == "101"
        assert out[0]["availability_status"] == "AVAILABLE"
        assert "_avail_subtype" not in out[0]

    def test_source_html_triggers_fee_rejection(self):
        units = [{"unit_id": "101", "market_rent_low": 50, "availability_status": "AVAILABLE"}]
        html = '<dt>Application Fee</dt><dd>$50</dd>'
        out = dq.apply_unit_guards(units, property_id="TEST", source_html=html)
        assert out[0]["market_rent_low"] is None
        assert out[0]["_fee_context_rent_rejected"] is True

    def test_same_rent_leak_pass(self):
        units = [
            {"unit_id": "inferred_a", "market_rent_low": 675},
            {"unit_id": "inferred_b", "market_rent_low": 675},
            {"unit_id": "inferred_c", "market_rent_low": 675},
        ]
        out = dq.apply_unit_guards(units, property_id="TEST", detect_same_rent=True)
        for u in out:
            assert u["market_rent_low"] is None

    def test_detect_same_rent_false_skips_pass(self):
        units = [
            {"unit_id": "inferred_a", "market_rent_low": 675},
            {"unit_id": "inferred_b", "market_rent_low": 675},
            {"unit_id": "inferred_c", "market_rent_low": 675},
        ]
        out = dq.apply_unit_guards(units, property_id="TEST", detect_same_rent=False)
        for u in out:
            assert u["market_rent_low"] == 675

    def test_empty_input_returns_empty(self):
        assert dq.apply_unit_guards([], property_id="TEST") == []

    def test_passthrough_when_no_modifications(self):
        units = [{"unit_id": "101", "floor_plan_name": "Plan A", "availability_status": "AVAILABLE"}]
        out = dq.apply_unit_guards(units, property_id="TEST", detect_same_rent=False)
        # Untouched dict is reference-shared (not copied).
        assert out[0] is units[0]
