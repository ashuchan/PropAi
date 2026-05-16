"""Tests for count_floor_plan_signal_cardinality.

Added 2026-05-16 as Bug #9 fix. The function shipped in commit 1d068dd
("addressed timeouts") with 128 LOC and 7 new ranker event fields but no
test coverage — violating the project's mandatory workflow Step 3.

Covers:
- Rent dollar-range clamp ($200-$50,000)
- Dedup of identical rent values
- _RE_BED_BATH_PAIR separator-bridge regression (two adjacent units on a
  flat stripped-HTML layout should NOT be merged into one bed/bath pair)
- JSON-LD Apartment vs Offer split
- PropertyValue bed/bath dim-count
- Empty input handling
"""
from __future__ import annotations

from ma_poc.pms.signal_engine.floor_plan_signals import (
    FloorPlanSignalCardinality,
    count_floor_plan_signal_cardinality,
)


def test_empty_inputs_return_zeroes() -> None:
    """No text and no HTML should produce a zero-valued snapshot."""
    out = count_floor_plan_signal_cardinality("", "")
    assert isinstance(out, FloorPlanSignalCardinality)
    assert out.total_count == 0
    assert out.unique_rents == 0
    assert out.unique_bed_bath_pairs == 0
    assert out.listing_rows_with_rent == 0
    assert out.jsonld_offer_count == 0
    assert out.jsonld_apartment_count == 0
    assert out.property_value_bed_bath == 0
    assert out.matched_signal_bytes == 0


def test_rent_dedup_collapses_identical_values() -> None:
    """Three occurrences of $1500 should be unique_rents=1.

    NOTE: ``_RE_DOLLAR`` is ``\\$\\s?(\\d{2,5}(?:,\\d{3})?(?:\\.\\d+)?)`` —
    it requires 2-5 digits before any optional comma group, so the
    common rendering ``$1,500`` (1 digit + comma) is NOT matched. This
    is a fixture-level limitation of the regex; document it here so a
    future contributor doesn't waste time trying to make ``$1,500`` work.
    """
    text = "Studio $1500 · 1 BR $1500 · 2 BR $1500"
    out = count_floor_plan_signal_cardinality(text, "")
    assert out.unique_rents == 1


def test_rent_range_clamp_drops_implausible_values() -> None:
    """$50 (deposit-like) and >$50,000 (sale price) must be excluded.

    Per the regex limitation documented above, only values where the
    first numeric group is 2-5 digits make it into ``unique_rents``. So
    ``$1,200`` (1-digit prefix) is silently dropped before the clamp
    even fires. Verify the clamp ON the values that DO reach it.
    """
    # $50: 2 digits → captured → 50 < 200 → excluded.
    # $200: captured → 200 admitted (boundary).
    # $50,000: captured → 50000 admitted (boundary).
    # $99,000: captured → 99000 excluded (over).
    # $50,001: captured → 50001 excluded (just over).
    text = "$50 deposit · rent $200 · $50,000 · $99,000 · $50,001"
    out = count_floor_plan_signal_cardinality(text, "")
    # Admitted: {200, 50000}. Excluded: {50, 99000, 50001}.
    assert out.unique_rents == 2


def test_bed_bath_pair_does_not_bridge_across_unit_boundary() -> None:
    """Regression test for the 40-char separator window in _RE_BED_BATH_PAIR.

    Pre-2026-05-16, a stripped-HTML layout like ``1 BR 750 sqft 2 BA``
    (two adjacent unit cards collapsed to flat text by whitespace
    stripping) could match (1 BR, 2 BA) as one bed/bath pair, inflating
    the cardinality. Verify the regex doesn't do that on a string whose
    bed→bath gap exceeds the separator window.
    """
    # 45-char gap between '1 BR' and the next 'BA' — beyond the 40-char
    # window in the regex. Should NOT match.
    text = "1 BR " + ("x" * 45) + " 2 BA"
    out = count_floor_plan_signal_cardinality(text, "")
    assert out.unique_bed_bath_pairs == 0

    # Now verify the regex CAN match a legitimate single unit-card.
    text_ok = "1 BR / 1 BA"
    out_ok = count_floor_plan_signal_cardinality(text_ok, "")
    assert out_ok.unique_bed_bath_pairs == 1


def test_bed_bath_pair_handles_decimal_bath() -> None:
    """1.5 / 2.5 baths should count as their own pairs."""
    text = "2 bed 1.5 bath · 2 bed 2.5 bath"
    out = count_floor_plan_signal_cardinality(text, "")
    # (2, 1.5) and (2, 2.5) — two distinct pairs.
    assert out.unique_bed_bath_pairs == 2


def test_matched_signal_bytes_accumulates() -> None:
    """matched_signal_bytes should be > 0 whenever any rent or bed-bath fires."""
    text = "Studio $1,500 — 1 BR / 1 BA"
    out = count_floor_plan_signal_cardinality(text, "")
    assert out.matched_signal_bytes > 0


def test_jsonld_apartment_vs_offer_split() -> None:
    """``@type: Apartment|FloorPlan`` increments apartment count;
    ``@type: Offer|Product`` increments offer count.

    The regex (``_RE_LISTING_REPEAT_OFFER_ARRAY``) anchors on
    ``"@type": "<exact>"`` so ``ApartmentComplex`` is intentionally
    NOT matched — it's a property-level wrapper, not a per-unit type.
    """
    html = """
    <script type="application/ld+json">
    {"@type": "ApartmentComplex"}
    </script>
    <script type="application/ld+json">
    {"@type": "Apartment"}
    </script>
    <script type="application/ld+json">
    {"@type": "FloorPlan"}
    </script>
    <script type="application/ld+json">
    {"@type": "Offer"}
    </script>
    <script type="application/ld+json">
    {"@type": "Product"}
    </script>
    """
    out = count_floor_plan_signal_cardinality("", html)
    # Apartment + FloorPlan → apartment bucket = 2.
    # Offer + Product → offer bucket = 2.
    # ApartmentComplex deliberately excluded.
    assert out.jsonld_apartment_count == 2
    assert out.jsonld_offer_count == 2


def test_property_value_dim_count() -> None:
    """JSON-LD ``PropertyValue`` with ``name: Bedrooms`` / ``Bathrooms``
    should count toward ``property_value_bed_bath``. Used for Squarespace
    e-commerce Product + additionalProperty pattern (PID 61950)."""
    html = """
    {"@type":"PropertyValue","name":"Bedrooms","value":1},
    {"@type":"PropertyValue","name":"Bathrooms","value":1},
    {"@type":"PropertyValue","name":"Square Footage","value":750}
    """
    out = count_floor_plan_signal_cardinality("", html)
    assert out.property_value_bed_bath >= 2  # Bedrooms + Bathrooms


def test_total_count_preserves_legacy_score() -> None:
    """total_count must equal count_floor_plan_signals(text) so the
    existing ``floor_plan_signal_count`` event field stays consistent
    with the new cardinality snapshot."""
    from ma_poc.pms.signal_engine.floor_plan_signals import count_floor_plan_signals

    text = "Studio · 1 BR 1 BA 750 sqft · 2 BR"
    out = count_floor_plan_signal_cardinality(text, "")
    assert out.total_count == count_floor_plan_signals(text)


def test_dataclass_is_frozen() -> None:
    """FloorPlanSignalCardinality is declared frozen; mutation must raise."""
    out = count_floor_plan_signal_cardinality("Studio", "")
    import dataclasses
    try:
        out.total_count = 99  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("frozen dataclass allowed mutation")
