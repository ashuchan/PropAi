"""A bare ``AVAILABLE`` with nothing behind it must ship ``UNKNOWN``.

2026-07-31 (#91 "BUG2", #75 residual). ``resolve_plan_row_availability`` cleans
up plan-LEVEL rows; its inverse blind spot is a row the plan predicate did NOT
flag (``is_floor_plan_level`` False) that still asserts ``AVAILABLE`` while
carrying zero inventory evidence — no rent, no unit anchor, no date. Measured on
the fresh-250 cohort: 37 such rows across 10 properties, 9 of them SecureCafe
plan-catalogue placeholders whose online-leasing portal held zero units (the
site reads "get notified" / "contact for availability"). ``AVAILABLE`` there
asserts a bookable apartment we have no evidence for; the honest label is
``UNKNOWN``.

``withdraw_unsupported_available`` is the pure rule; the two v2 unit formatters
(``core.schema_v2`` canonical + the ``scripts.runners.jugnu`` production fork)
must apply it identically. Both are exercised here — a change to one fork only
must fail a test.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ma_poc.core.schema_v2 import (
    _format_v2_unit as _format_canonical,
)
from ma_poc.core.schema_v2 import (
    _row_has_availability_date,
    withdraw_unsupported_available,
)
from ma_poc.scripts.runners.jugnu import _format_v2_unit as _format_jugnu

_TS = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)

# Both v2 unit formatters share a name in different modules; run every
# integration assertion through BOTH so a fork drifting out of lock-step fails.
_FORMATTERS = pytest.mark.parametrize(
    "fmt", [_format_canonical, _format_jugnu], ids=["canonical", "jugnu"]
)


class TestPureRule:
    """The truth table of ``withdraw_unsupported_available``."""

    def test_zero_evidence_available_is_withdrawn(self) -> None:
        assert (
            withdraw_unsupported_available(
                "AVAILABLE", has_rent=False, has_anchor=False, has_date=False
            )
            == "UNKNOWN"
        )

    @pytest.mark.parametrize(
        ("has_rent", "has_anchor", "has_date"),
        [(True, False, False), (False, True, False), (False, False, True)],
        ids=["rent", "anchor", "date"],
    )
    def test_any_single_signal_preserves_available(
        self, has_rent: bool, has_anchor: bool, has_date: bool
    ) -> None:
        assert (
            withdraw_unsupported_available(
                "AVAILABLE",
                has_rent=has_rent,
                has_anchor=has_anchor,
                has_date=has_date,
            )
            == "AVAILABLE"
        )

    @pytest.mark.parametrize("status", ["UNAVAILABLE", "UNKNOWN", "WAITLIST", None])
    def test_non_available_passes_through_untouched(self, status: str | None) -> None:
        # Never manufactures AVAILABLE, never rewrites a non-AVAILABLE — even
        # with zero evidence, which for those statuses is not a contradiction.
        assert (
            withdraw_unsupported_available(
                status, has_rent=False, has_anchor=False, has_date=False
            )
            == status
        )


class TestDateDetection:
    """``_row_has_availability_date`` reads every spelling the formatter reads."""

    @pytest.mark.parametrize(
        "field",
        [
            "available_date",
            "availability_date",
            "internalAvailableDate",
            "availableDate",
            "date_available",
            "dateAvailable",
        ],
    )
    def test_each_spelling_counts_as_a_date(self, field: str) -> None:
        assert _row_has_availability_date({field: "2026-08-01"}) is True

    def test_no_date_field_is_false(self) -> None:
        assert _row_has_availability_date({"floor_plan_name": "B1"}) is False

    def test_agrees_with_the_formatter_date_parse(self) -> None:
        # Uses the same ``_format_date`` the formatter uses, so ``has_date`` and
        # the emitted ``available_date`` can never disagree. A phrase that parser
        # rejects (``None``) is therefore not counted as a date here either.
        assert _row_has_availability_date(
            {"available_date": "Contact for availability"}
        ) is False


@_FORMATTERS
class TestFormatterIntegration:
    """Both forks withdraw the placeholder AVAILABLE and keep supported ones."""

    def test_placeholder_available_becomes_unknown(self, fmt: object) -> None:
        # The SecureCafe shape: a plan name + a default "Available", nothing else.
        out = fmt({"floor_plan_name": "B1", "availability_status": "Available"}, _TS)  # type: ignore[operator]
        assert out["availability_status"] == "UNKNOWN"

    def test_withdrawn_row_gets_no_fabricated_today_date(self, fmt: object) -> None:
        # The AVAILABLE branch of _resolve_available_date keys on the resolved
        # status; withdrawing it must also drop the "available today" stamp.
        out = fmt({"floor_plan_name": "B1", "availability_status": "Available"}, _TS)  # type: ignore[operator]
        assert out["available_date"] is None

    def test_available_with_rent_is_kept(self, fmt: object) -> None:
        out = fmt(  # type: ignore[operator]
            {
                "floor_plan_name": "B1",
                "availability_status": "Available",
                "market_rent_low": 1500,
            },
            _TS,
        )
        assert out["availability_status"] == "AVAILABLE"

    def test_available_with_unit_anchor_is_kept(self, fmt: object) -> None:
        out = fmt(  # type: ignore[operator]
            {"unit_number": "101", "availability_status": "Available"}, _TS
        )
        assert out["availability_status"] == "AVAILABLE"

    def test_available_with_date_is_kept(self, fmt: object) -> None:
        out = fmt(  # type: ignore[operator]
            {
                "floor_plan_name": "B1",
                "availability_status": "Available",
                "available_date": "2026-08-15",
            },
            _TS,
        )
        assert out["availability_status"] == "AVAILABLE"
        assert out["available_date"] == "2026-08-15"

    def test_source_unavailable_is_untouched(self, fmt: object) -> None:
        out = fmt(  # type: ignore[operator]
            {"floor_plan_name": "B1", "availability_status": "Unavailable"}, _TS
        )
        assert out["availability_status"] == "UNAVAILABLE"
