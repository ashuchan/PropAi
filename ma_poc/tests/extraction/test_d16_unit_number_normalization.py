"""D16 test 5 — ``normalize_unit_number`` collapses producer variants to one key.

The normaliser is the keying primitive for the cross-page unit-number
dedup pass (D16 item 6). False-positive collapses cause real apartments
to disappear; false-negative collapses leak duplicates into the run
report. These tests pin the contract.
"""

from __future__ import annotations

import pytest

from ma_poc.extraction.unit_number import normalize_unit_number


class TestPrefixStripping:
    @pytest.mark.parametrize(
        "raw",
        [
            "Apt 101",
            "apt 101",
            "APT 101",
            "Apartment 101",
            "Unit 101",
            "unit 101",
            "Suite 101",
            "Ste 101",
            "No 101",
            "Number 101",
            "#101",
            "# 101",
            " 101 ",
            "101",
        ],
    )
    def test_prefix_variants_collapse_to_101(self, raw):
        assert normalize_unit_number(raw) == "101"


class TestTrailingDotZero:
    def test_float_coerced_int_string_collapses(self):
        # ``str(float(618))`` → ``"618.0"`` — observed on producers that
        # pass int through float.
        assert normalize_unit_number("618.0") == "618"

    def test_multiple_trailing_zeros_collapse(self):
        assert normalize_unit_number("618.00") == "618"

    def test_non_numeric_with_dot_zero_preserved(self):
        # Only the purely-numeric ``\d+\.0+$`` form is stripped. An
        # alphanumeric suffix means ``.0`` is part of an oddly-shaped
        # apartment identifier — leave it alone.
        assert normalize_unit_number("618A.0") == "618a.0"


class TestAlphanumericSuffix:
    def test_uppercase_lowercase_collapse(self):
        """``101A`` and ``101a`` are the same apartment."""
        assert normalize_unit_number("101A") == normalize_unit_number("101a")

    def test_different_suffixes_stay_distinct(self):
        """``101A`` and ``101B`` are physically distinct apartments."""
        assert normalize_unit_number("101A") != normalize_unit_number("101B")

    def test_numeric_suffix_preserved(self):
        assert normalize_unit_number("101-2") == "101-2"

    def test_townhome_letter_unit_distinct(self):
        # Two units in different buildings using "A" / "B" suffix.
        assert normalize_unit_number("1A") != normalize_unit_number("1B")
        assert normalize_unit_number("1A") == "1a"


class TestAbsenceAndNone:
    @pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
    def test_empty_returns_none(self, raw):
        assert normalize_unit_number(raw) is None

    @pytest.mark.parametrize("raw", ["-1", -1])
    def test_absence_sentinel_returns_none(self, raw):
        assert normalize_unit_number(raw) is None

    def test_only_prefix_returns_none(self):
        # ``"Apt "`` alone strips to empty.
        assert normalize_unit_number("Apt ") is None

    def test_only_hash_returns_none(self):
        assert normalize_unit_number("#") is None


class TestCoercibleTypes:
    def test_int_coerced(self):
        assert normalize_unit_number(618) == "618"

    def test_float_coerced_and_dot_zero_stripped(self):
        assert normalize_unit_number(618.0) == "618"

    def test_string_int_coerced(self):
        assert normalize_unit_number("618") == "618"


class TestDeterminism:
    def test_idempotent(self):
        """Normalising an already-normalised key returns the same key."""
        first = normalize_unit_number("Apt 618")
        assert first == "618"
        assert normalize_unit_number(first) == "618"
