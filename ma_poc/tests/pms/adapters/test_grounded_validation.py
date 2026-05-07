"""Source-grounded validation tests (2026-05-07).

LLM tiers can hallucinate rents that aren't anywhere in the source HTML or
captured API bodies. The grounded validator drops these before emission.
"""

from __future__ import annotations

from ma_poc.pms.adapters._parsing import filter_llm_units_grounded, is_rent_grounded

# ── is_rent_grounded ────────────────────────────────────────────────────


def test_grounded_canonical_comma_form() -> None:
    assert is_rent_grounded(1275, "Rent: $1,275 monthly")


def test_grounded_decimal_form() -> None:
    assert is_rent_grounded(1275, "Rent: $1,275.00 / month")


def test_grounded_plain_integer() -> None:
    assert is_rent_grounded(1275, "Starting at 1275")


def test_grounded_no_dollar_sign_with_comma() -> None:
    assert is_rent_grounded(1275, "1,275 per month")


def test_grounded_rejects_word_boundary_high() -> None:
    """1275 must NOT match inside 12750."""
    assert not is_rent_grounded(1275, "Just $12,750 here")


def test_grounded_rejects_leading_zero() -> None:
    """1275 must NOT match inside 01275."""
    assert not is_rent_grounded(1275, "Code 01275")


def test_grounded_rejects_substring_in_larger_int() -> None:
    """1275 must NOT match inside 12752."""
    assert not is_rent_grounded(1275, "Suite 12752 office")


def test_grounded_null_rent_is_always_grounded() -> None:
    """Units that legitimately have no rent (rent_low=None) pass through."""
    assert is_rent_grounded(None, "")
    assert is_rent_grounded(None, "no rent here either")


def test_grounded_rent_not_in_empty_source() -> None:
    assert not is_rent_grounded(1275, "")
    assert not is_rent_grounded(1275, "no rents at all here")


def test_grounded_rent_not_in_unrelated_text() -> None:
    """Rent NOT present → must not be grounded."""
    assert not is_rent_grounded(1275, "$1,500 - $2,000 monthly")


def test_grounded_handles_4digit_no_comma() -> None:
    """5+ digit numbers (e.g. 5500) should also work without comma."""
    # 5,500 only has comma at the digit-3 position, so the "plain" form is "5500"
    assert is_rent_grounded(5500, "5500/mo")
    assert is_rent_grounded(5500, "$5,500.00")


# ── filter_llm_units_grounded ────────────────────────────────────────────


def test_filter_drops_hallucinated_rent() -> None:
    """A unit whose rent is nowhere in source gets dropped; others kept."""
    units = [
        {"unit_number": "101", "market_rent_low": 1275, "market_rent_high": 1275},
        # Hallucinated — 9999 not in source
        {"unit_number": "102", "market_rent_low": 9999, "market_rent_high": 9999},
        {"unit_number": "103", "market_rent_low": 1500, "market_rent_high": 1500},
    ]
    src = "Available: $1,275 and $1,500"
    filtered, dropped = filter_llm_units_grounded(units, src)
    assert dropped == 1
    assert [u["unit_number"] for u in filtered] == ["101", "103"]


def test_filter_passes_units_with_no_rent() -> None:
    """rent_low=None units pass — there's nothing to ground."""
    units = [
        {"unit_number": "101", "market_rent_low": None, "market_rent_high": None},
    ]
    filtered, dropped = filter_llm_units_grounded(units, "")
    assert dropped == 0
    assert len(filtered) == 1


def test_filter_uses_rent_high_when_low_missing() -> None:
    """If rent_low absent, validate rent_high instead."""
    units = [
        {"unit_number": "101", "market_rent_low": None, "market_rent_high": 1500},
    ]
    filtered, dropped = filter_llm_units_grounded(units, "$1,500 monthly")
    assert dropped == 0
    assert len(filtered) == 1


def test_filter_handles_string_rent_values() -> None:
    """Some upstream code emits rent as a string — handle gracefully."""
    units = [
        {"unit_number": "101", "rent_low": "1275", "rent_high": "1275"},
    ]
    filtered, dropped = filter_llm_units_grounded(units, "$1,275")
    assert dropped == 0
    assert len(filtered) == 1


def test_filter_returns_empty_for_empty_input() -> None:
    filtered, dropped = filter_llm_units_grounded([], "")
    assert filtered == []
    assert dropped == 0


def test_filter_drops_all_hallucinated() -> None:
    """When every unit's rent is hallucinated, all get dropped."""
    units = [
        {"unit_number": "1", "market_rent_low": 1000},
        {"unit_number": "2", "market_rent_low": 2000},
        {"unit_number": "3", "market_rent_low": 3000},
    ]
    filtered, dropped = filter_llm_units_grounded(units, "Schedule a tour today!")
    assert filtered == []
    assert dropped == 3
