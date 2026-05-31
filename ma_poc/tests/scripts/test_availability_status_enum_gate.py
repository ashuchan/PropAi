"""availability_status enum-gate normalization (2026-05-31).

The 2026-05-31 may13 canary QC scan surfaced 181 rows with free-text
availability_status leaking through ("Notice - Application Pending",
"Sign Waitlist", "Call for Details!", "Only 1 Vacant Apartment Left!",
"Check Availability", and ~28 other distinct phrases). Prior behaviour
was capture-first — unknown strings passed through unchanged. New
behaviour: anything not in the enum gets mapped via phrase patterns
or coerced to UNKNOWN. Downstream raw-text preserved in
``availability_status_raw``.

Both copies kept in sync:
  - ma_poc/scripts/runners/jugnu.py:_norm_avail_status
  - ma_poc/core/schema_v2.py:_norm_status
"""
from __future__ import annotations

import pytest

from ma_poc.scripts.runners.jugnu import _norm_avail_status as N_jugnu
from ma_poc.core.schema_v2 import _norm_status as N_schema


_VALID_ENUM = {"AVAILABLE", "UNAVAILABLE", "WAITLIST", "WAITLISTED",
               "LEASED", "PENDING", "UNKNOWN"}


@pytest.mark.parametrize("fn", [N_jugnu, N_schema], ids=["jugnu", "schema_v2"])
class TestEnumGate:
    """Run the matrix against BOTH implementations to keep them in sync."""

    def test_none_and_empty_pass_through_as_none(self, fn) -> None:
        assert fn(None) is None
        assert fn("") is None
        assert fn("   ") is None

    def test_canonical_enum_values_uppercased_unchanged(self, fn) -> None:
        for v in _VALID_ENUM:
            assert fn(v) == v
            assert fn(v.lower()) == v
            assert fn(v.title()) == v

    # ── Real free-text values observed in the may13 canary ──

    @pytest.mark.parametrize("raw,expected", [
        ("Notice - Application Pending", "PENDING"),
        ("Notice - Leased", "LEASED"),
        ("Sign Waitlist", "WAITLIST"),
        ("Join the Wait List", "WAITLIST"),
        ("Application Pending Review", "PENDING"),
        ("Currently Leased", "LEASED"),
        ("Fully Occupied", "LEASED"),
        ("Not Available", "UNAVAILABLE"),
        ("Leased Out", "UNAVAILABLE"),
        ("SOLD OUT", "UNAVAILABLE"),
        ("Available Now", "AVAILABLE"),
        ("Only 1 Vacant Apartment Left!", "AVAILABLE"),
        ("1 Vacant", "AVAILABLE"),
        ("Now Leasing", "AVAILABLE"),
    ])
    def test_observed_phrases_map_to_enum(self, fn, raw, expected) -> None:
        assert fn(raw) == expected

    # ── Junk / non-enum text falls back to UNKNOWN, never leaks raw ──

    @pytest.mark.parametrize("raw", [
        "Call for Details!",
        "Check Availability",
        "Contact Leasing Office",
        "Inquire Within",
        "???",
        "TBD",
        "See Note",
    ])
    def test_non_enum_phrases_become_UNKNOWN(self, fn, raw) -> None:
        result = fn(raw)
        assert result == "UNKNOWN", (
            f"junk phrase {raw!r} leaked through as {result!r} — "
            f"must coerce to UNKNOWN, not pass raw"
        )

    def test_output_is_always_in_enum_or_none(self, fn) -> None:
        """The contract: caller can rely on the output being either None
        or one of the enum values. Nothing else."""
        bag = [
            None, "", "   ",
            "Available", "AVAILABLE", "unavailable", "UNKNOWN",
            "Notice - Pending", "Sign Waitlist", "Leased Out",
            "Random junk string with no enum hint",
            "12345", "$1,500/mo",
        ]
        for v in bag:
            r = fn(v)
            assert r is None or r in _VALID_ENUM, (
                f"output for {v!r} = {r!r} is NOT in enum {_VALID_ENUM}"
            )
