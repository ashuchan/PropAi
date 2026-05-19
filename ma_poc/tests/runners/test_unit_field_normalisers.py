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


# ── 2026-05-19 (R1) — available_date_raw surfacing ────────────────────────────


def test_v2_unit_emits_available_date_raw_for_parseable_string() -> None:
    """Parseable producer string: ``available_date`` is ISO,
    ``available_date_raw`` preserves the literal (whitespace-collapsed)."""
    unit = {
        "unit_id": "4425",
        "bedrooms": 2, "bathrooms": 1.0, "sqft": 987,
        "market_rent_low": 2460,
        "available_date": "Available 7/4/26",
    }
    out = _format_v2_unit(unit, _SCRAPE_TS, property_id="67736")
    assert out["available_date"] == "2026-07-04"
    assert out["available_date_raw"] == "Available 7/4/26"


def test_v2_unit_falls_back_to_date_placeholder_for_raw() -> None:
    """When the schema gate has already nulled ``available_date`` and
    stashed the literal in ``_date_placeholder``, the v2 formatter
    surfaces the placeholder as ``available_date_raw``.
    """
    unit = {
        "unit_id": "9821",
        "bedrooms": 1, "bathrooms": 1.0, "sqft": 650,
        "market_rent_low": 1800,
        "available_date": None,
        "_date_placeholder": "Available 7/24",  # year ambiguous — gate nulled it
    }
    out = _format_v2_unit(unit, _SCRAPE_TS, property_id="67736")
    assert out["available_date"] is None  # parser still can't infer a year
    assert out["available_date_raw"] == "Available 7/24"


def test_v2_unit_collapses_internal_whitespace_in_raw() -> None:
    """DOM-injected ``\\n\\t`` runs are collapsed to single spaces so
    Postgres' VARCHAR(64) limit isn't burned on whitespace."""
    unit = {
        "unit_id": "1137",
        "bedrooms": 1, "bathrooms": 1.0, "sqft": 726,
        "market_rent_low": 2025,
        "available_date": "Avail.\n\t\t\tNow",
    }
    out = _format_v2_unit(unit, _SCRAPE_TS, property_id="67736")
    assert out["available_date_raw"] == "Avail. Now"


def test_v2_unit_raw_none_when_no_signal() -> None:
    """No producer date in any slot → ``available_date_raw`` is None,
    not empty string. Distinguishes "not extracted" from "explicitly
    empty"."""
    unit = {
        "unit_id": "0001",
        "bedrooms": 1, "bathrooms": 1.0, "sqft": 700,
        "market_rent_low": 1500,
    }
    out = _format_v2_unit(unit, _SCRAPE_TS, property_id="67736")
    assert out["available_date"] is None
    assert out["available_date_raw"] is None


def test_v2_unit_prefers_available_date_over_placeholder() -> None:
    """When the gate already normalised, the typed column is the
    canonical source — ``available_date_raw`` reflects the original
    pre-normalisation string.
    """
    unit = {
        "unit_id": "5400",
        "bedrooms": 2, "bathrooms": 2.0, "sqft": 1100,
        "market_rent_low": 2800,
        "available_date": "2026-06-15",  # already normalised by upstream
        "available_date_raw": "Available 6/15/26",  # the original
    }
    out = _format_v2_unit(unit, _SCRAPE_TS, property_id="67736")
    assert out["available_date"] == "2026-06-15"
    assert out["available_date_raw"] == "Available 6/15/26"


# ── 2026-05-20 — alias key regression (PID 67736 / AppFolio SSR) ─────────────


def test_v2_unit_reads_legacy_availability_date_key() -> None:
    """The AppFolio SSR adapter emits the producer's date string under the
    legacy v1 key ``availability_date`` (with the "y"). Pre-2026-05-20
    ``_format_v2_unit`` only read ``available_date`` (no "y") — 94 % of units
    in the 2026-05-19 cloud run shipped ``available_date=null`` and
    ``available_date_raw=null`` because the formatter never found the data.
    """
    unit = {
        "unit_id": "2216",
        "bedrooms": 1, "bathrooms": 1.0, "sqft": 665,
        "market_rent_low": 2295,
        # Date lands on the legacy alias — this is what the AppFolio
        # adapter actually emits per the cloud-run artifacts.
        "availability_date": "Available 7/10/26",
        "availability_status": "AVAILABLE",
    }
    out = _format_v2_unit(unit, _SCRAPE_TS, property_id="67736")
    assert out["available_date"] == "2026-07-10"
    assert out["available_date_raw"] == "Available 7/10/26"
    assert out["availability_status"] == "AVAILABLE"


def test_v2_unit_reads_camelcase_availabledate_alias() -> None:
    """MAA emits ``availableDate`` (camelCase); after the adapter lowers
    field keys it lands as ``availabledate``. AVAIL_DATE_KEYS resolves it."""
    unit = {
        "unit_id": "303",
        "beds": 1, "baths": 1.0, "area": 720,
        "rent_low": 1850,
        "availabledate": "2026-08-01",
    }
    out = _format_v2_unit(unit, _SCRAPE_TS, property_id="MAA")
    assert out["available_date"] == "2026-08-01"
    assert out["available_date_raw"] == "2026-08-01"


def test_v2_unit_v2_canonical_key_still_wins_over_alias() -> None:
    """Priority order: ``available_date`` (v2 canonical) beats
    ``availability_date`` (v1 alias) when both are set. Otherwise a stale
    schema-gate-normalised value would be overridden by a raw legacy alias."""
    unit = {
        "unit_id": "X",
        "beds": 1, "baths": 1.0, "area": 700,
        "rent_low": 1500,
        "available_date": "2026-06-15",            # already normalised
        "availability_date": "Available 6/15/26",  # the raw producer string
    }
    out = _format_v2_unit(unit, _SCRAPE_TS, property_id="X")
    # Both keys point at the same logical date — should ship the ISO form
    # regardless of which slot the producer used.
    assert out["available_date"] == "2026-06-15"


def test_v2_unit_no_date_anywhere_yields_null() -> None:
    """When no producer alias is populated, both typed and raw columns are
    null — distinguishes "no data" from "data we couldn't parse"."""
    unit = {
        "unit_id": "no_date",
        "beds": 1, "baths": 1.0, "area": 700,
        "rent_low": 1500,
    }
    out = _format_v2_unit(unit, _SCRAPE_TS, property_id="X")
    assert out["available_date"] is None
    assert out["available_date_raw"] is None


# ── 2026-05-20 — status normalisation long-tail (cloud run telemetry) ────────


def test_status_available_ready_normalised() -> None:
    """RealPage and several other adapters emit ``AVAILABLE_READY`` to
    indicate "available now, no preparation needed". Pre-2026-05-20 it
    fell through to the unknown-pass-through branch and shipped as-is."""
    assert _normalize_availability_status("AVAILABLE_READY") == "AVAILABLE"
    assert _normalize_availability_status("available_ready") == "AVAILABLE"
    assert _normalize_availability_status("AvailableReady") == "AVAILABLE"


def test_status_schema_org_instock_normalised() -> None:
    """JSON-LD extractors sometimes copy ``offers[].availability:
    "https://schema.org/InStock"`` verbatim into the status slot.
    Strip the URL prefix down to the local name before lookup."""
    assert _normalize_availability_status("https://schema.org/InStock") == "AVAILABLE"
    assert _normalize_availability_status("http://schema.org/InStock") == "AVAILABLE"
    assert _normalize_availability_status(
        "https://schema.org/OutOfStock"
    ) == "UNAVAILABLE"
    assert _normalize_availability_status(
        "https://schema.org/LimitedAvailability"
    ) == "WAITLIST"
    assert _normalize_availability_status(
        "https://schema.org/PreOrder"
    ) == "COMING_SOON"
    assert _normalize_availability_status(
        "https://schema.org/Discontinued"
    ) == "UNAVAILABLE"


def test_status_schema_org_trailing_slash_tolerated() -> None:
    """Some emitters include a trailing slash on the Schema.org URI."""
    assert _normalize_availability_status(
        "https://schema.org/InStock/"
    ) == "AVAILABLE"


def test_status_schema_org_unknown_term_falls_through() -> None:
    """Unrecognised Schema.org names pass through uppercased (after URL
    strip) so we don't drop signals we haven't classified yet."""
    out = _normalize_availability_status("https://schema.org/SoldOut")
    assert out == "UNAVAILABLE"  # mapped via 'soldout' alias


def test_status_in_stock_underscore_variant() -> None:
    """``in_stock`` / ``instock`` (Schema.org local name) → AVAILABLE."""
    assert _normalize_availability_status("in_stock") == "AVAILABLE"
    assert _normalize_availability_status("instock") == "AVAILABLE"
