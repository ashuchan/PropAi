"""Tests for the v2-side unit-field normalisers in ``scripts/runners/jugnu.py``.

These pin the contract for ``_format_date_str`` / ``_format_rent`` /
``_format_area`` / ``_normalize_availability_status`` after the
2026-05-19 hardening triggered by PID 67736 (live210main.com, AppFolio,
308 units): every unit emitted ``"Available 7/4/26"`` for the date and
``"AVAILABLE"`` for the status, but the v2 output shipped
``available_date=null`` and dropped ``availability_status`` entirely —
because the strict ISO/numeric-only date regex didn't match and the v2
unit dict had no status slot.

Test fixture style mirrors the other ``test_jugnu_*`` modules: import
the helper, call directly, assert the canonical output.
"""

from __future__ import annotations

from datetime import date, datetime

from ma_poc.scripts.runners.jugnu import (
    _format_area,
    _format_date_str,
    _format_rent,
    _format_v2_unit,
    _normalize_availability_status,
)


_TODAY = date(2026, 5, 19)
_TODAY_STR = _TODAY.strftime("%Y-%m-%d")
_SCRAPE_TS = datetime(2026, 5, 19, 12, 0, 0)


# ── _format_date_str ─────────────────────────────────────────────────────────


def test_date_passes_through_iso() -> None:
    assert _format_date_str("2026-07-04") == "2026-07-04"


def test_date_iso_with_time_suffix_is_truncated() -> None:
    assert _format_date_str("2026-07-04T13:45:00Z") == "2026-07-04"


def test_date_appfolio_available_prefix_with_2digit_year() -> None:
    """The original PID 67736 incident — pre-fix returned None."""
    assert _format_date_str("Available 7/4/26") == "2026-07-04"


def test_date_appfolio_available_prefix_with_4digit_year() -> None:
    assert _format_date_str("Available 7/4/2026") == "2026-07-04"


def test_date_entrata_movein_prefix() -> None:
    assert _format_date_str("Move-in 8/21/26") == "2026-08-21"
    assert _format_date_str("Move In 8/21/2026") == "2026-08-21"


def test_date_available_now_resolves_to_today(monkeypatch) -> None:
    """"Available Now" / "Ready" / "Immediate" should resolve to today's
    date so the AVAILABLE-now signal isn't silently dropped."""
    assert _format_date_str("Available Now", today=_TODAY) == _TODAY_STR
    assert _format_date_str("Ready", today=_TODAY) == _TODAY_STR
    assert _format_date_str("Immediate", today=_TODAY) == _TODAY_STR
    assert _format_date_str("Now", today=_TODAY) == _TODAY_STR


def test_date_absent_tokens_return_none() -> None:
    """Explicit "no date" placeholders must map to None — distinct from
    "Now" which maps to today's date."""
    for token in ("N/A", "TBD", "TBA", "Call", "Inquire",
                  "Unavailable", "Coming soon", "Waitlist", "-", "--"):
        assert _format_date_str(token) is None, f"token {token!r} should be None"


def test_date_long_form_month_name() -> None:
    assert _format_date_str("July 4, 2026") == "2026-07-04"
    assert _format_date_str("Jul 4 2026") == "2026-07-04"
    assert _format_date_str("4 July 2026") == "2026-07-04"


def test_date_invalid_format_returns_none() -> None:
    assert _format_date_str("not a date") is None
    assert _format_date_str("") is None
    assert _format_date_str(None) is None
    assert _format_date_str("13/45/2026") is None  # invalid month/day


def test_date_with_dash_separator() -> None:
    assert _format_date_str("7-4-2026") == "2026-07-04"
    assert _format_date_str("7-4-26") == "2026-07-04"


def test_date_two_digit_year_uses_strptime_2000_century() -> None:
    """``%y`` interprets 00-68 as 2000-2068; 69-99 as 1969-1999.
    Property dates are always near-future so the 2000-century mapping is
    what we want. This pins it so a future refactor that switches to a
    custom mapping has to update the test deliberately."""
    assert _format_date_str("7/4/26") == "2026-07-04"
    assert _format_date_str("7/4/30") == "2030-07-04"


# ── _format_rent ─────────────────────────────────────────────────────────────


def test_rent_passes_through_scalar() -> None:
    assert _format_rent(1450) == 1450.0
    assert _format_rent(1450.5) == 1450.5


def test_rent_strips_dollar_sign_and_commas() -> None:
    assert _format_rent("$1,450") == 1450.0
    assert _format_rent("$1,450.00") == 1450.0


def test_rent_strips_from_prefix() -> None:
    assert _format_rent("From $1,450") == 1450.0
    assert _format_rent("Starting at 1450") == 1450.0
    assert _format_rent("Starting from $1,200") == 1200.0
    assert _format_rent("As low as $1,200") == 1200.0
    assert _format_rent("Only $1,450") == 1450.0


def test_rent_strips_per_month_suffix() -> None:
    assert _format_rent("$1,450/month") == 1450.0
    assert _format_rent("$1,450/mo") == 1450.0
    assert _format_rent("1450 per month") == 1450.0
    assert _format_rent("$1,450 USD") == 1450.0


def test_rent_range_takes_low_end() -> None:
    """Range strings ship as rent_low; rent_high comes from a separate
    path (market_rent_high). Pre-fix the range string returned None
    because the hyphen broke float() parsing."""
    assert _format_rent("$1,200 - $1,500") == 1200.0
    assert _format_rent("1200 - 1500") == 1200.0
    assert _format_rent("$1,200–$1,500") == 1200.0  # em-dash


def test_rent_absent_tokens_return_none() -> None:
    for token in ("Call", "Contact", "Inquire", "TBD", "N/A",
                  "Call for pricing", "Market", "$0", "0"):
        assert _format_rent(token) is None, f"token {token!r} should be None"


def test_rent_zero_and_below_returns_none() -> None:
    """Rents must be > 1; 0 / negative are noise that previously slipped
    through the int-coercion path."""
    assert _format_rent(0) is None
    assert _format_rent(1) is None
    assert _format_rent(-100) is None
    assert _format_rent("0") is None


def test_rent_bool_returns_none() -> None:
    """bool is an int subclass — guard against True/False slipping
    through as 1.0/0.0 rents."""
    assert _format_rent(True) is None
    assert _format_rent(False) is None


def test_rent_invalid_returns_none() -> None:
    assert _format_rent("not a rent") is None
    assert _format_rent("") is None
    assert _format_rent(None) is None


# ── _format_area ─────────────────────────────────────────────────────────────


def test_area_passes_through_int_in_bounds() -> None:
    assert _format_area(850) == 850
    assert _format_area(150) == 150  # lower bound inclusive
    assert _format_area(10_000) == 10_000  # upper bound inclusive


def test_area_rejects_out_of_bounds_int() -> None:
    """Bedroom counts / floor numbers / truncated values must hit -1."""
    assert _format_area(9) == -1
    assert _format_area(70) == -1   # the old "070" truncation case
    assert _format_area(149) == -1
    assert _format_area(10_001) == -1
    assert _format_area(0) == -1
    assert _format_area(-5) == -1


def test_area_strips_sqft_suffix_variants() -> None:
    assert _format_area("850 sqft") == 850
    assert _format_area("850 sq ft") == 850
    assert _format_area("850 sq. ft.") == 850
    assert _format_area("850 sq. ft") == 850
    assert _format_area("850 square feet") == 850
    assert _format_area("850 square foot") == 850
    assert _format_area("850 SF") == 850
    assert _format_area("850 ft²") == 850


def test_area_range_takes_low_end() -> None:
    assert _format_area("850 - 950 sqft") == 850
    assert _format_area("850-950 sqft") == 850
    assert _format_area("850–950") == 850  # en-dash, no suffix


def test_area_invalid_returns_minus_one() -> None:
    assert _format_area("not an area") == -1
    assert _format_area("") == -1
    assert _format_area(None) == -1
    assert _format_area(-1) == -1
    assert _format_area("-1") == -1


def test_area_string_in_bounds_no_suffix() -> None:
    """Strings without units should still parse — covers raw int-as-string
    from JSON-LD properties."""
    assert _format_area("850") == 850
    assert _format_area("850.0") == 850


# ── _normalize_availability_status ───────────────────────────────────────────


def test_status_explicit_available() -> None:
    assert _normalize_availability_status("AVAILABLE") == "AVAILABLE"
    assert _normalize_availability_status("available") == "AVAILABLE"
    assert _normalize_availability_status("Avail") == "AVAILABLE"
    assert _normalize_availability_status("Vacant") == "AVAILABLE"
    assert _normalize_availability_status("Open") == "AVAILABLE"


def test_status_explicit_unavailable() -> None:
    assert _normalize_availability_status("UNAVAILABLE") == "UNAVAILABLE"
    assert _normalize_availability_status("Leased") == "UNAVAILABLE"
    assert _normalize_availability_status("Rented") == "UNAVAILABLE"
    assert _normalize_availability_status("Occupied") == "UNAVAILABLE"
    assert _normalize_availability_status("Reserved") == "UNAVAILABLE"


def test_status_explicit_waitlist() -> None:
    assert _normalize_availability_status("Waitlist") == "WAITLIST"
    assert _normalize_availability_status("wait list") == "WAITLIST"


def test_status_boolean_strings() -> None:
    assert _normalize_availability_status("true") == "AVAILABLE"
    assert _normalize_availability_status("yes") == "AVAILABLE"
    assert _normalize_availability_status("Y") == "AVAILABLE"
    assert _normalize_availability_status("1") == "AVAILABLE"
    assert _normalize_availability_status("false") == "UNAVAILABLE"
    assert _normalize_availability_status("no") == "UNAVAILABLE"
    assert _normalize_availability_status("0") == "UNAVAILABLE"


def test_status_unknown_value_passes_through_uppercased() -> None:
    """Unknown producer strings must survive (uppercased) so we don't
    drop signals we haven't classified yet — operator can grep them."""
    assert _normalize_availability_status("Pending Inspection") == "PENDING INSPECTION"


def test_status_inferred_from_available_now_date() -> None:
    """No explicit status field but date field says "Available Now" —
    infer AVAILABLE. This is the PID 67736 path where AppFolio puts
    "Available Now" in the date column and emits no status column at
    all."""
    assert _normalize_availability_status(
        "",
        raw_available_date="Available Now",
        normalized_available_date=_TODAY_STR,
        scrape_ts=_SCRAPE_TS,
    ) == "AVAILABLE"


def test_status_inferred_from_future_date() -> None:
    """No status but a future move-in date — infer AVAILABLE."""
    assert _normalize_availability_status(
        "",
        raw_available_date="7/4/2026",
        normalized_available_date="2026-07-04",
        scrape_ts=_SCRAPE_TS,
    ) == "AVAILABLE"


def test_status_not_inferred_from_past_date() -> None:
    """A move-in date in the past doesn't imply currently-available;
    leave None so downstream gates can decide explicitly."""
    assert _normalize_availability_status(
        "",
        raw_available_date="1/1/2020",
        normalized_available_date="2020-01-01",
        scrape_ts=_SCRAPE_TS,
    ) is None


def test_status_no_signal_returns_none() -> None:
    assert _normalize_availability_status("") is None
    assert _normalize_availability_status("", raw_available_date=None) is None


# ── _format_v2_unit integration ──────────────────────────────────────────────


def test_v2_unit_appfolio_available_date_with_status_inferred() -> None:
    """End-to-end: an AppFolio-shaped unit dict with the PID 67736
    pattern — verify the v2 row carries both the parsed date and the
    inferred AVAILABLE status. Pre-fix both fields were lost."""
    unit = {
        "unit_id": "4425",
        "floor_plan_name": "AppFolio listing 4425",
        "bedrooms": 2,
        "bathrooms": 1.0,
        "sqft": 987,
        "market_rent_low": 2460,
        "market_rent_high": 2460,
        "available_date": "Available 7/4/26",
        # NOTE: no explicit status field — AppFolio doesn't emit one.
    }
    out = _format_v2_unit(unit, _SCRAPE_TS, property_id="67736")
    assert out["available_date"] == "2026-07-04"
    assert out["availability_status"] == "AVAILABLE"
    assert out["rent_low"] == 2460.0
    assert out["area"] == 987
    assert out["unit_id"] == "4425"


def test_v2_unit_available_now_resolves_to_today() -> None:
    """"Available Now" -> today's date + AVAILABLE status, matching the
    pattern on the PID 67736 listing units 1137 and 2109."""
    unit = {
        "unit_id": "1137",
        "bedrooms": 1, "bathrooms": 1.0, "sqft": 726,
        "market_rent_low": 2025,
        "available_date": "Available Now",
    }
    out = _format_v2_unit(unit, _SCRAPE_TS, property_id="67736")
    assert out["available_date"] == _SCRAPE_TS.date().strftime("%Y-%m-%d")
    assert out["availability_status"] == "AVAILABLE"


def test_v2_unit_explicit_status_overrides_inference() -> None:
    """An explicit producer status takes precedence over the
    date-inference fallback."""
    unit = {
        "unit_id": "5612",
        "bedrooms": 1, "bathrooms": 1.0, "sqft": 586,
        "market_rent_low": 2250,
        "available_date": "Available 8/1/26",
        "availability_status": "Reserved",  # explicit producer signal
    }
    out = _format_v2_unit(unit, _SCRAPE_TS, property_id="67736")
    assert out["available_date"] == "2026-08-01"
    assert out["availability_status"] == "UNAVAILABLE"  # mapped from "Reserved"
