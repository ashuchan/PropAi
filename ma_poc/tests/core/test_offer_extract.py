"""Offer-taxonomy extraction (2026-05-24).

Regression oracle: the 8-column reference xlsx ``scraped_units_2026-05-23.xlsx``
with 4,562 populated offer rows. Each test case below is anchored on a
real input → expected output observed in that xlsx (sample sizes
inline).

Output keys mirror the xlsx column headers:
  offer_banner       — short offer-only phrase
  offer_type         — categorical
  offer_target       — what offer applies to
  offer_value        — formatted string with unit
  offer_conditions   — semicolon-delimited key:value pairs
"""
from __future__ import annotations

import pytest

from ma_poc.core.offer_extract import (
    classify_offer_target,
    classify_offer_type,
    extract_offer,
    extract_offer_banner,
    extract_offer_conditions,
    extract_offer_value,
)


# ─────────────────────────────────────────────────────────────────────
# OFFER TYPE — 7 categories in priority order
# ─────────────────────────────────────────────────────────────────────


def test_type_free_rent_n_weeks() -> None:
    # xlsx: 567 rows with "6 weeks" value; 505 with "8 weeks"; 302 "10 weeks"
    assert classify_offer_type("6 weeks FREE rent") == "free_rent"
    assert classify_offer_type("8 weeks of complimentary rent on us") == "free_rent"


def test_type_free_rent_n_months() -> None:
    # xlsx: 1101 rows with "1 month"; 401 with "2 months"
    assert classify_offer_type("1 month free on a 12-month lease") == "free_rent"
    assert classify_offer_type("Enjoy 2 months free rent") == "free_rent"


def test_type_free_rent_first_month() -> None:
    # xlsx: 29 rows "first month"
    assert classify_offer_type("First month free with new lease") == "free_rent"
    assert classify_offer_type("Get your first full month free") == "free_rent"


def test_type_dollar_off() -> None:
    # xlsx: 783 rows ($500 → 352, $750 → 319, $400 in samples)
    assert classify_offer_type("$400 off rent · apply within 48h") == "dollar_off"
    assert classify_offer_type("$500 off your first month") in ("dollar_off", "free_rent")
    assert classify_offer_type("$750 off move-in costs") == "dollar_off"


def test_type_waived_fee() -> None:
    # xlsx: 91 rows
    assert classify_offer_type("Waived admin fee") == "waived_fee"
    assert classify_offer_type("No application fee") == "waived_fee"
    assert classify_offer_type("Complimentary amenity fee") == "waived_fee"


def test_type_reduced_rate() -> None:
    # xlsx: 46 rows ("Reduced rent · military")
    assert classify_offer_type("Reduced rent for military") == "reduced_rate"
    assert classify_offer_type("Special pricing on rent") == "reduced_rate"


def test_type_look_and_lease() -> None:
    # xlsx: 35 rows
    assert classify_offer_type("Look & lease special · select units") == "look_and_lease"
    assert classify_offer_type("Look and lease bonus this week") == "look_and_lease"


def test_type_reduced_deposit() -> None:
    # xlsx: 23 rows ("Reduced deposit")
    assert classify_offer_type("Reduced deposit on all floor plans") == "reduced_deposit"
    assert classify_offer_type("$99 deposit special") == "reduced_deposit"


def test_type_percent_off() -> None:
    # xlsx: 1 row ("50% off rent")
    assert classify_offer_type("50% off rent for new tenants") == "percent_off"


def test_type_priority_waived_fee_beats_free_rent() -> None:
    """When both 'no app fee' AND 'free rent' appear, waived_fee wins —
    matches xlsx ordering of specific-before-generic."""
    text = "No app fee + 1 month free on select homes"
    assert classify_offer_type(text) == "waived_fee"


def test_type_none_when_no_offer() -> None:
    assert classify_offer_type("Open Saturday 10am-5pm") is None
    assert classify_offer_type("") is None
    assert classify_offer_type(None) is None


# ─────────────────────────────────────────────────────────────────────
# OFFER TARGET — what the offer applies to
# ─────────────────────────────────────────────────────────────────────


def test_target_rent_default_for_free_rent() -> None:
    # xlsx: 3938 of all targets are "rent"
    assert classify_offer_target("6 weeks free rent", "free_rent") == "rent"
    assert classify_offer_target("$500 off rent", "dollar_off") == "rent"


def test_target_deposit_for_reduced_deposit() -> None:
    # xlsx: 23 rows
    assert classify_offer_target("Reduced deposit", "reduced_deposit") == "deposit"


def test_target_app_fee_for_waived_app_fee() -> None:
    assert classify_offer_target("No application fee", "waived_fee") == "app_fee"


def test_target_admin_fee_for_waived_admin_fee() -> None:
    # xlsx: 3 rows admin_fee
    assert classify_offer_target("Waived admin fee", "waived_fee") == "admin_fee"


def test_target_amenity_fee_for_waived_amenity_fee() -> None:
    # xlsx: 62 rows
    assert classify_offer_target("Waived amenity fee", "waived_fee") == "amenity_fee"


def test_target_move_in_cost_explicit() -> None:
    # xlsx: 119 rows
    assert classify_offer_target("$500 toward move-in cost", "dollar_off") == "move_in_cost"


def test_target_fallback_rent_for_free_rent_no_explicit_word() -> None:
    """When offer_type=free_rent and no specific target word, fallback to rent."""
    # "1 month free" has no "rent" word but it's still rent-targeted
    assert classify_offer_target("1 month free", "free_rent") == "rent"


def test_target_none_for_empty_text() -> None:
    assert classify_offer_target("", "free_rent") is None
    assert classify_offer_target(None, "free_rent") is None


# ─────────────────────────────────────────────────────────────────────
# OFFER VALUE — formatted string with unit
# ─────────────────────────────────────────────────────────────────────


def test_value_n_weeks() -> None:
    assert extract_offer_value("6 weeks free rent", "free_rent") == "6 weeks"
    assert extract_offer_value("8 weeks on us", "free_rent") == "8 weeks"
    assert extract_offer_value("10 weeks complimentary", "free_rent") == "10 weeks"


def test_value_n_months() -> None:
    assert extract_offer_value("1 month free", "free_rent") == "1 month"
    assert extract_offer_value("2 months free rent", "free_rent") == "2 months"
    assert extract_offer_value("Get 5 months free", "free_rent") == "5 months"


def test_value_word_number_normalized() -> None:
    # "two months free" → "2 months" (numeric)
    assert extract_offer_value("Two months free on us", "free_rent") == "2 months"


def test_value_first_month_literal() -> None:
    # xlsx: 29 rows "first month"
    assert extract_offer_value("First month free", "free_rent") == "first month"


def test_value_first_n_months_returns_count() -> None:
    """'first 2 full months free' → '2 months' (count wins over 'first')."""
    assert extract_offer_value("first 2 full months are free", "free_rent") == "2 months"


def test_value_dollar_off() -> None:
    assert extract_offer_value("$400 off rent", "dollar_off") == "$400"
    assert extract_offer_value("$500 off", "dollar_off") == "$500"


def test_value_dollar_with_comma_formatted() -> None:
    """$1,000+ amounts re-formatted with comma."""
    assert extract_offer_value("$1500 off move-in", "dollar_off") == "$1,500"
    assert extract_offer_value("$1,000 credit", "dollar_off") == "$1,000"


def test_value_percent() -> None:
    # xlsx: "50% off rent" → "50%"
    assert extract_offer_value("50% off rent", "percent_off") == "50%"
    assert extract_offer_value("25% reduction", "percent_off") == "25%"


def test_value_none_for_vague_reduced_rate() -> None:
    """'Reduced rent · military' has no numeric anchor → None.
    Matches xlsx: 46 reduced_rate rows mostly have empty Value."""
    assert extract_offer_value("Reduced rent for military", "reduced_rate") is None


def test_value_none_for_vague_reduced_deposit() -> None:
    """'Reduced deposit' has no numeric → None.
    Matches xlsx: 23 reduced_deposit rows mostly empty Value."""
    assert extract_offer_value("Reduced deposit", "reduced_deposit") is None


def test_value_none_for_waived_fee() -> None:
    """'Waived admin fee' is binary (waived/not) → no numeric value."""
    assert extract_offer_value("Waived admin fee", "waived_fee") is None


def test_value_none_for_empty_input() -> None:
    assert extract_offer_value("", "free_rent") is None
    assert extract_offer_value(None, "free_rent") is None


# ─────────────────────────────────────────────────────────────────────
# OFFER CONDITIONS — semicolon-delimited key:value
# ─────────────────────────────────────────────────────────────────────


def test_conditions_deadline_month_day() -> None:
    # xlsx: 111 rows "deadline:May 31st"
    assert extract_offer_conditions("Must move-in by May 25th") == "deadline:May 25th"
    assert extract_offer_conditions("Offer ends June 30") == "deadline:June 30"


def test_conditions_deadline_slash_format() -> None:
    # xlsx: 60 rows "deadline:6/30/2026"
    cond = extract_offer_conditions("Apply by 6/30/2026 for the bonus")
    assert "deadline:6/30/2026" in cond


def test_conditions_unit_scope_select() -> None:
    # xlsx: 765 rows "unit_scope:select"
    assert "unit_scope:select" in extract_offer_conditions("On select homes only")


def test_conditions_unit_scope_bedroom_specific() -> None:
    # xlsx: 182 rows "unit_scope:2-bedroom"; 81 rows 1-bedroom
    assert "unit_scope:1-bedroom" in extract_offer_conditions("Valid on 1-bedroom units")
    assert "unit_scope:2-bedroom" in extract_offer_conditions("2 bedroom apartments only")


def test_conditions_lease_length_min() -> None:
    # xlsx: 324 rows "lease_length:12+ months"
    cond = extract_offer_conditions("Requires 12+ month lease")
    assert cond and "lease_length:12+ months" in cond


def test_conditions_apply_within_hours() -> None:
    # xlsx: 77 rows "apply_within:24h"; 37 rows "apply_within:48h"
    cond = extract_offer_conditions("Apply within 48 hours for the special")
    assert "apply_within:48h" in cond
    cond = extract_offer_conditions("Apply within 24 hrs")
    assert "apply_within:24h" in cond


def test_conditions_audience_military() -> None:
    cond = extract_offer_conditions("Reduced rent for military families")
    assert cond and "audience:military" in cond


def test_conditions_restrictions_generic() -> None:
    # xlsx: 322 rows just "restrictions"
    assert extract_offer_conditions("*Conditions apply") == "restrictions"
    assert extract_offer_conditions("Subject to availability") == "restrictions"


def test_conditions_multi_value_semicolon_joined() -> None:
    """Multiple conditions in one banner → semicolon-joined.
    xlsx: 58 rows "deadline:05/31/26; lease_length:12+ months; apply_within:48h" """
    text = (
        "Sign by 5/31/26 with a 12-month lease and apply within 48 hours "
        "on select 2-bedroom units"
    )
    cond = extract_offer_conditions(text)
    assert cond
    parts = set(cond.split("; "))
    assert any(p.startswith("deadline:") for p in parts)
    assert any(p.startswith("lease_length:") for p in parts)
    assert any(p.startswith("apply_within:") for p in parts)
    assert any(p.startswith("unit_scope:") for p in parts)


def test_conditions_none_when_no_qualifiers() -> None:
    # xlsx: 153 rows with Type=free_rent but Conditions=None
    assert extract_offer_conditions("6 weeks FREE rent") is None


# ─────────────────────────────────────────────────────────────────────
# OFFER BANNER — short offer-only phrase pulled from chrome
# ─────────────────────────────────────────────────────────────────────


def test_banner_pulls_short_phrase_from_long_text() -> None:
    """xlsx Row 1: full raw text 'Alexan Gateway... LEASE TODAY AND GET ...'
    → banner '6 weeks FREE rent'."""
    raw = (
        "Alexan Gateway Skip to main content Enable accessibility for low "
        "vision Open the accessibility menu LEASE TODAY AND GET 6 weeks "
        "FREE rent on select 1- and 2-bedroom homes"
    )
    banner = extract_offer_banner(raw)
    assert banner is not None
    assert "6 weeks" in banner.lower()
    assert "free" in banner.lower()
    # Should be much shorter than raw
    assert len(banner) < 100


def test_banner_dollar_off() -> None:
    raw = "Welcome to Burnham Pointe Apartments. $400 off rent · apply within 48h"
    banner = extract_offer_banner(raw)
    assert banner is not None
    assert "$400" in banner


def test_banner_reduced_deposit_short_input() -> None:
    """Short input is already its own banner — return as-is (slightly trimmed)."""
    assert extract_offer_banner("Reduced deposit") is not None
    assert "Reduced deposit" in extract_offer_banner("Reduced deposit")


def test_banner_trims_disclaimer_tail() -> None:
    """'6 weeks free *Conditions apply' → '6 weeks free' (tail dropped)."""
    raw = "Enjoy 6 weeks free rent *Conditions apply, see office for details"
    banner = extract_offer_banner(raw)
    assert banner is not None
    assert "*" not in banner
    assert "Conditions apply" not in banner


def test_banner_none_when_no_offer_signal() -> None:
    assert extract_offer_banner("Welcome to our community. Tours available.") is None
    assert extract_offer_banner("") is None
    assert extract_offer_banner(None) is None


# ─────────────────────────────────────────────────────────────────────
# extract_offer() — top-level all-5-fields orchestrator
# ─────────────────────────────────────────────────────────────────────


def test_extract_offer_returns_all_five_keys_always() -> None:
    """Schema stability: all 5 keys always in output, None when no signal."""
    out = extract_offer("nothing here")
    assert set(out.keys()) == {
        "offer_banner", "offer_type", "offer_target",
        "offer_value", "offer_conditions",
    }
    assert all(v is None for v in out.values())


def test_extract_offer_alexan_gateway_full_row() -> None:
    """End-to-end xlsx Row 1 reproduction.

    Reference xlsx values:
      Banner:     "6 weeks FREE rent"
      Type:       free_rent
      Target:     rent
      Value:      "6 weeks"
      Conditions: None
    """
    raw = (
        "Alexan Gateway Skip to main content Enable accessibility for low "
        "vision Open the accessibility menu LEASE TODAY AND GET 6 weeks "
        "FREE rent"
    )
    out = extract_offer(raw)
    assert out["offer_type"] == "free_rent"
    assert out["offer_target"] == "rent"
    assert out["offer_value"] == "6 weeks"
    assert out["offer_conditions"] is None
    assert out["offer_banner"] is not None
    assert "6 weeks" in out["offer_banner"].lower()


def test_extract_offer_burnham_pointe_full_row() -> None:
    """xlsx: '$400 off rent · apply within 48h' →
        dollar_off / rent / $400 / apply_within:48h"""
    raw = "Special offer: $400 off rent. Apply within 48 hours."
    out = extract_offer(raw)
    assert out["offer_type"] == "dollar_off"
    assert out["offer_target"] == "rent"
    assert out["offer_value"] == "$400"
    assert "apply_within:48h" in (out["offer_conditions"] or "")


def test_extract_offer_vaya_full_row() -> None:
    """xlsx: 'Look & lease special · select units' →
        look_and_lease / rent / None / unit_scope:select; restrictions"""
    raw = "Look & lease special on select units. Restrictions apply."
    out = extract_offer(raw)
    assert out["offer_type"] == "look_and_lease"
    assert out["offer_target"] == "rent"
    assert out["offer_value"] is None
    cond = out["offer_conditions"] or ""
    assert "unit_scope:select" in cond
    assert "restrictions" in cond


def test_extract_offer_ridge_carlton_audience() -> None:
    """xlsx: 'Reduced rent · military' → reduced_rate / rent / None / audience:military"""
    raw = "Reduced rent special for military families"
    out = extract_offer(raw)
    assert out["offer_type"] == "reduced_rate"
    assert out["offer_target"] == "rent"
    assert out["offer_value"] is None
    assert out["offer_conditions"] == "audience:military"


def test_extract_offer_3333_elm_percent() -> None:
    """xlsx: '50% off rent' → percent_off / rent / 50% / None"""
    raw = "Limited time: 50% off rent for the first 6 months"
    out = extract_offer(raw)
    assert out["offer_type"] == "percent_off"
    assert out["offer_target"] == "rent"
    assert out["offer_value"] == "50%"


def test_extract_offer_empty_input_all_none() -> None:
    for inp in ("", "   ", None):
        out = extract_offer(inp)
        assert all(v is None for v in out.values())


# ─────────────────────────────────────────────────────────────────────
# 2026-05-24 user-found gap: "N weeks BASE rent free" phrasing
# (theblakeoptimistpark.com — "Exclusive Offer: 10 Weeks Base Rent Free")
# ─────────────────────────────────────────────────────────────────────


def test_blake_optimist_banner_extracted() -> None:
    """Real banner from theblakeoptimistpark.com (verified live
    2026-05-24). Pre-fix the regex missed because 'Base' between
    'Weeks' and 'Rent' broke the chain."""
    text = "Exclusive Offer: 10 Weeks Base Rent Free"
    out = extract_offer(text)
    assert out["offer_type"] == "free_rent"
    assert out["offer_value"] == "10 weeks"


def test_long_form_blake_banner_extracted() -> None:
    """The full body copy with descriptive lead-in."""
    text = (
        "Upgrade your lifestyle with our limited-time special—get "
        "10 weeks of base rent free on select apartments. Hurry—ends soon!"
    )
    out = extract_offer(text)
    assert out["offer_type"] == "free_rent"
    assert out["offer_value"] == "10 weeks"


def test_qualifier_words_supported_in_free_rent() -> None:
    """Various qualifier words between duration and 'rent free' should
    all match (base / effective / monthly / total / select / premium / market)."""
    cases = [
        ("6 weeks base rent free", "6 weeks"),
        ("8 months effective rent free", "8 months"),
        ("12 weeks monthly rent free", "12 weeks"),
        ("4 weeks total rent free", "4 weeks"),
        ("3 months select rent free", "3 months"),
        ("2 months market rent free", "2 months"),
    ]
    for text, expected_value in cases:
        out = extract_offer(text)
        assert out["offer_type"] == "free_rent", (
            f"failed for {text!r}: got {out}"
        )
        assert out["offer_value"] == expected_value, (
            f"failed for {text!r}: got value={out['offer_value']}"
        )


def test_rent_waived_synonym() -> None:
    """'waived' is a synonym for 'free' in this context (operator's
    terminology varies — 'waived' is RealPage/Yardi standard)."""
    text = "8 months base rent waived"
    out = extract_offer(text)
    assert out["offer_type"] == "free_rent"
    assert out["offer_value"] == "8 months"
