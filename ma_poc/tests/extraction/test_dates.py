"""Tests for the shared lenient date parser.

Covers the 12 producer-shape failure buckets enumerated in the
2026-05-18 cloud-run telemetry (1,168 of 22,839 placeholder events
that the pre-2026-05-19 ``_format_date_str`` returned None for). Each
test pins one bucket so a future regex tweak that drops support for a
bucket fails loudly here.
"""

from __future__ import annotations

from datetime import date

from ma_poc.extraction.dates import (
    DATE_ABSENT_TOKENS,
    DATE_NOW_TOKENS,
    DATE_PREFIX_RE,
    format_loose_date,
)

# Pin the "today" anchor so the "Available Now" / month-day-without-year
# branches are deterministic.
_TODAY = date(2026, 5, 19)


# ── Pre-2026-05-19 surface (regression guard) ────────────────────────────────


def test_passes_iso() -> None:
    assert format_loose_date("2026-07-04") == "2026-07-04"


def test_iso_with_time_suffix_is_truncated() -> None:
    assert format_loose_date("2026-07-04T13:45:00Z") == "2026-07-04"


def test_appfolio_available_prefix_2digit_year() -> None:
    assert format_loose_date("Available 7/4/26") == "2026-07-04"


def test_appfolio_available_prefix_4digit_year() -> None:
    assert format_loose_date("Available 7/4/2026") == "2026-07-04"


def test_entrata_movein_prefix() -> None:
    assert format_loose_date("Move-in 8/21/26") == "2026-08-21"
    assert format_loose_date("Move In 8/21/2026") == "2026-08-21"


def test_available_now_resolves_to_today() -> None:
    assert format_loose_date("Available Now", today=_TODAY) == "2026-05-19"
    assert format_loose_date("NOW", today=_TODAY) == "2026-05-19"
    assert format_loose_date("Now", today=_TODAY) == "2026-05-19"


def test_absent_tokens_return_none() -> None:
    for tok in ("N/A", "TBD", "TBA", "Call", "Inquire",
                "Unavailable", "Coming soon", "Waitlist", "-", "--"):
        assert format_loose_date(tok) is None, f"token {tok!r} should be None"


def test_long_form_month_name() -> None:
    assert format_loose_date("July 4, 2026") == "2026-07-04"
    assert format_loose_date("Jul 4 2026") == "2026-07-04"
    assert format_loose_date("4 July 2026") == "2026-07-04"


def test_invalid_returns_none() -> None:
    assert format_loose_date("not a date") is None
    assert format_loose_date("") is None
    assert format_loose_date(None) is None
    assert format_loose_date("13/45/2026") is None


def test_two_digit_year_uses_2000_century() -> None:
    assert format_loose_date("7/4/26") == "2026-07-04"
    assert format_loose_date("7/4/30") == "2030-07-04"


# ── 2026-05-19 (R3) — 12 new failure buckets ─────────────────────────────────


# Bucket 1: ``"Date:"`` prefix (Funnel / RealPage)
def test_date_colon_prefix_4digit_year() -> None:
    assert format_loose_date("Date: 5/22/2026") == "2026-05-22"
    assert format_loose_date("Date: 8/12/2026") == "2026-08-12"


def test_date_colon_prefix_2digit_year() -> None:
    assert format_loose_date("Date: 5/22/26") == "2026-05-22"


# Bucket 2: ``"On:"`` prefix (RealPage variants)
def test_available_on_colon_prefix() -> None:
    assert format_loose_date("Available On: 5/18/2026") == "2026-05-18"


# Bucket 3: trailing exclamation
def test_trailing_exclamation_stripped() -> None:
    assert format_loose_date("Available now!", today=_TODAY) == "2026-05-19"
    assert format_loose_date("Available 7/4/26!") == "2026-07-04"


# Bucket 4: internal whitespace (DOM-injected \n \t runs)
def test_internal_whitespace_collapsed() -> None:
    assert format_loose_date("Avail.\n\t\t\t\tNow", today=_TODAY) == "2026-05-19"
    assert format_loose_date("Available   7/4/26") == "2026-07-04"


# Bucket 5: weekday prefix
def test_weekday_prefix_stripped() -> None:
    assert format_loose_date("Tuesday June 23 2026") == "2026-06-23"
    assert format_loose_date("Wednesday July 1 2026") == "2026-07-01"
    assert format_loose_date("Saturday May 23 2026") == "2026-05-23"


def test_weekday_short_form_stripped() -> None:
    """RealPage occasionally emits short-form weekday names."""
    assert format_loose_date("Mon June 23 2026") == "2026-06-23"
    assert format_loose_date("Thu May 21 2026") == "2026-05-21"


# Bucket 6: ordinal-suffix on the day
def test_ordinal_suffix_st_nd_rd_th() -> None:
    assert format_loose_date("Available May 30th") == "2026-05-30"
    assert format_loose_date("Available Jul 21st") == "2026-07-21"
    assert format_loose_date("Available Jul 4th") == "2026-07-04"
    assert format_loose_date("Available Jun 3rd") == "2026-06-03"
    assert format_loose_date("Available Aug 2nd") == "2026-08-02"


# Bucket 7: month + day, no year — assume current year if future
def test_month_day_no_year_assumed_current() -> None:
    assert format_loose_date("Available May 21", today=_TODAY) == "2026-05-21"
    assert format_loose_date("Avail. Jun 03", today=_TODAY) == "2026-06-03"
    assert format_loose_date("Available Jul 17", today=_TODAY) == "2026-07-17"
    assert format_loose_date("Available Aug 24", today=_TODAY) == "2026-08-24"


# Bucket 7b: month + day, no year — if past, roll forward to next year
def test_month_day_no_year_past_rolls_forward() -> None:
    """A producer emitting ``"Feb 14"`` in May means *next* February —
    rentals don't show past dates as availability."""
    # _TODAY = 2026-05-19; "Feb 14" without year would be 2026-02-14 (past).
    # Should roll forward to 2027-02-14.
    assert format_loose_date("Available Feb 14", today=_TODAY) == "2027-02-14"
    assert format_loose_date("Avail. Jan 5", today=_TODAY) == "2027-01-05"


# Bucket 8: junk extractor output — must NOT parse
def test_junk_strings_return_none() -> None:
    """Extractor bugs occasionally route floor-plan summaries / lease
    terms into the date field. Parser must not invent a date."""
    assert format_loose_date("15 Months") is None
    assert format_loose_date("Studio") is None
    assert format_loose_date("1 Bed / 1 Bath / 822 sq. ft.") is None
    assert format_loose_date("/ month") is None
    assert format_loose_date("to") is None


# Bucket 9: long unit-shape fragments (extractor bug, but must not raise)
def test_freeform_unit_fragment_returns_none() -> None:
    assert format_loose_date(
        "Unit 0304 Available on June 3, 2026 1 bedroom"
    ) is None


# Bucket 10: starts with explicit "Available starting"
def test_starting_connector_stripped() -> None:
    assert format_loose_date("Available starting 7/1/2026") == "2026-07-01"


# ── Idempotency / round-trip ──────────────────────────────────────────────────


def test_idempotent_on_iso_output() -> None:
    """ISO output must be a fixed point — calling the parser twice yields
    the same answer. Guards against a future "normalize" pass that
    accidentally adds prefix-stripping to already-clean ISO strings."""
    for raw in ("Available 7/4/26", "Date: 5/22/2026",
                "Tuesday June 23 2026", "Available May 21"):
        first = format_loose_date(raw, today=_TODAY)
        assert first is not None
        second = format_loose_date(first, today=_TODAY)
        assert second == first, f"non-idempotent: {raw!r} -> {first!r} -> {second!r}"


# ── Token table shapes (back-compat for callers that import them) ────────────


def test_date_now_tokens_includes_available_now() -> None:
    # The jugnu availability-status inference relies on these.
    assert "available now" in DATE_NOW_TOKENS
    assert "now" in DATE_NOW_TOKENS
    assert "ready" in DATE_NOW_TOKENS


def test_date_absent_tokens_includes_not_available() -> None:
    assert "not available" in DATE_ABSENT_TOKENS
    assert "tbd" in DATE_ABSENT_TOKENS


def test_date_prefix_re_matches_available_prefix() -> None:
    """``_normalize_availability_status`` strips this regex too — confirm
    behaviour matches the parser."""
    assert DATE_PREFIX_RE.match("Available ")
    assert DATE_PREFIX_RE.match("Move-in ")
    assert DATE_PREFIX_RE.match("Date: ")


# ── jugnu wrapper round-trip ──────────────────────────────────────────────────


def test_jugnu_wrapper_delegates() -> None:
    """``ma_poc.scripts.runners.jugnu._format_date_str`` is a thin
    back-compat alias — must produce identical output to
    ``format_loose_date`` for every supported input. Prevents the two
    from drifting if a future refactor inlines either."""
    from ma_poc.scripts.runners.jugnu import _format_date_str

    cases = [
        "2026-07-04", "Available 7/4/26", "Available Now",
        "Date: 5/22/2026", "Tuesday June 23 2026", "Available Jul 21st",
        "Available May 21", "Not Available", "Studio", "", None,
    ]
    for c in cases:
        assert _format_date_str(c, today=_TODAY) == format_loose_date(c, today=_TODAY)
