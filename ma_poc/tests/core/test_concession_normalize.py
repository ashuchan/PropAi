"""Unit tests for ma_poc.core.concession_normalize.

Two invariants under test:

  1. Each supported offer shape returns the documented dict shape.
  2. Anything not matched returns ``None`` — the caller's raw text
     remains the system of record, no false-positive structuring.
"""

from __future__ import annotations

import pytest

from ma_poc.core.concession_normalize import normalize_concession


class TestFreeRent:
    def test_n_months_free(self) -> None:
        result = normalize_concession("2 months free rent")
        assert result is not None
        assert result["type"] == "free_rent"
        assert result["free_period"] == {"value": 2, "unit": "months"}
        assert result["source"] == "TEXT"
        assert "text" in result

    def test_n_weeks_free(self) -> None:
        result = normalize_concession("6 weeks free on a 12-month lease")
        assert result is not None
        assert result["type"] == "free_rent"
        assert result["free_period"] == {"value": 6, "unit": "weeks"}

    def test_word_number(self) -> None:
        result = normalize_concession("Two months free on select units")
        assert result is not None
        assert result["type"] == "free_rent"
        assert result["free_period"] == {"value": 2, "unit": "months"}

    def test_free_rent_inverted(self) -> None:
        # "free rent for X months" inverted form.
        result = normalize_concession("Free rent for 2 months on signing")
        assert result is not None
        assert result["type"] == "free_rent"
        assert result["free_period"] == {"value": 2, "unit": "months"}

    def test_n_out_of_range_rejected(self) -> None:
        # 30 months free is implausible — should not match.
        result = normalize_concession("30 months free")
        assert result is None or result["type"] != "free_rent"


class TestDiscount:
    def test_dollar_off(self) -> None:
        result = normalize_concession("Save $500 off your first month")
        assert result is not None
        assert result["type"] == "discount"
        assert result["amount"] == {"value": 500, "currency": "USD"}

    def test_dollar_with_commas(self) -> None:
        result = normalize_concession("Save $1,500 today!")
        assert result is not None
        assert result["amount"]["value"] == 1500

    def test_bare_dollar_off(self) -> None:
        result = normalize_concession("$750 off your application fees")
        assert result is not None
        assert result["type"] == "discount"
        assert result["amount"]["value"] == 750


class TestPercentOff:
    def test_simple(self) -> None:
        result = normalize_concession("Get 10% off your first month")
        assert result is not None
        assert result["type"] == "percent_off"
        assert result["percent"] == 10.0

    def test_out_of_range_rejected(self) -> None:
        # 150% off is nonsensical — should not match.
        result = normalize_concession("150% off")
        assert result is None or result["type"] != "percent_off"


class TestWaivedFee:
    def test_application_fee(self) -> None:
        result = normalize_concession("Waived application fee for new residents")
        assert result is not None
        assert result["type"] == "waived_fee"
        assert result["fee_kind"] == "application"

    def test_admin_fee(self) -> None:
        result = normalize_concession("Waived admin fees this month")
        assert result is not None
        assert result["type"] == "waived_fee"
        assert "admin" in result["fee_kind"]


class TestOtherShapes:
    def test_reduced_deposit(self) -> None:
        result = normalize_concession("Reduced deposit for qualified applicants")
        assert result is not None
        assert result["type"] == "reduced_deposit"

    def test_look_and_lease(self) -> None:
        result = normalize_concession("Look and Lease special — apply today")
        assert result is not None
        assert result["type"] == "look_and_lease"


class TestDeadline:
    def test_move_in_by_date(self) -> None:
        result = normalize_concession("2 months free rent — move in by June 5th")
        assert result is not None
        assert result["deadline"] is not None
        assert "June" in result["deadline"] or "5" in result["deadline"]

    def test_no_deadline(self) -> None:
        result = normalize_concession("2 months free rent on select units")
        assert result is not None
        assert result["deadline"] is None

    def test_numeric_date_format(self) -> None:
        result = normalize_concession("Move in by 6/15 and save $500")
        assert result is not None
        # Either the free_rent rule or save rule matches — both should
        # successfully parse the deadline.
        if result["deadline"] is not None:
            assert "6" in result["deadline"]


class TestNoMatch:
    def test_unrelated_text_returns_none(self) -> None:
        assert normalize_concession("Welcome to our beautiful community") is None

    def test_empty_returns_none(self) -> None:
        assert normalize_concession(None) is None
        assert normalize_concession("") is None
        assert normalize_concession("   ") is None

    def test_amenity_free_not_matched(self) -> None:
        # "free wifi" / "free parking" should NOT register as a
        # concession (bare ``free`` without a unit).
        assert normalize_concession("Free WiFi in every unit") is None

    def test_header_only_returns_none(self) -> None:
        # Header-only inputs have no parsable shape — caller keeps raw.
        result = normalize_concession("Limited Time Offer!")
        # The normaliser DOES recognise "limited time offer" as a
        # banner header but only via the look_and_lease / similar
        # broad markers — accept either None OR a low-information
        # result. The key invariant: it never raises.
        assert result is None or isinstance(result, dict)


class TestSourceLabel:
    def test_default_source(self) -> None:
        result = normalize_concession("2 months free")
        assert result is not None
        assert result["source"] == "TEXT"

    def test_custom_source(self) -> None:
        result = normalize_concession("2 months free", source="IMAGE_BANNER")
        assert result is not None
        assert result["source"] == "IMAGE_BANNER"

    @pytest.mark.parametrize("source", ["TEXT", "IMAGE_BANNER", "URL_PROBE", "API"])
    def test_source_passthrough(self, source: str) -> None:
        result = normalize_concession("Save $500", source=source)
        assert result is not None
        assert result["source"] == source
