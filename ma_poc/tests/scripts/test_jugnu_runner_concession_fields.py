"""2026-05-25 — runner ``_format_v2_unit`` concession/offer field parity
with the canonical ``ma_poc/core/schema_v2.py:_format_v2_unit``.

Pre-fix: ma_poc/scripts/runners/jugnu.py:_format_v2_unit (the function
the canary runner actually calls) was divergent from schema_v2.py — it
emitted 21 unit-level keys but NONE of the 10 concession/offer columns
(concession_text, concession_text_clean, _concession_quality,
concession_value, concession_source, offer_banner, offer_type,
offer_target, offer_value, offer_conditions). Canary 1ef1060 captured
property-level concession banners on 2,312 of 4,982 properties (46%)
but produced ZERO per-unit concession data — the xlsx export had no
column for it because the data never made it to the unit dicts.

Post-fix: the runner's _format_v2_unit now emits all 10 fields. Adapters
already populate concession_text + offer_* on unit dicts via
make_unit_dict in _parsing.py; the runner now surfaces them.

This file pins the parity so a future refactor cannot silently re-drop
the columns.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ma_poc.scripts.runners.jugnu import _format_v2_unit

_TS = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)

# All 10 concession + offer keys the canonical schema emits per unit.
_CONCESSION_OFFER_KEYS: tuple[str, ...] = (
    "concession_text",
    "concession_text_clean",
    "_concession_quality",
    "concession_value",
    "concession_source",
    "offer_banner",
    "offer_type",
    "offer_target",
    "offer_value",
    "offer_conditions",
)


def test_all_10_concession_offer_keys_present_even_when_unset() -> None:
    """Schema stability — all 10 keys must be present (None when unset)
    so downstream readers (xlsx export, offer_report, concession_report)
    see a consistent shape regardless of whether the unit had a banner."""
    out = _format_v2_unit(
        {"floor_plan_name": "A1", "market_rent_low": 1500},
        _TS,
    )
    for k in _CONCESSION_OFFER_KEYS:
        assert k in out, (
            f"key {k!r} missing from runner _format_v2_unit output; "
            f"all 10 concession/offer keys must always be emitted"
        )
        # Unset → None (not "", not 0)
        assert out[k] is None, (
            f"key {k!r} expected None for an unset concession, got {out[k]!r}"
        )


def test_concession_text_passthrough_from_adapter() -> None:
    """When an adapter populates ``concession_text`` directly (the
    canonical key the make_unit_dict helper uses), it surfaces on the
    runner output verbatim."""
    out = _format_v2_unit(
        {
            "floor_plan_name": "A1",
            "market_rent_low": 1500,
            "concession_text": "$500 off first month with 12-month lease",
        },
        _TS,
    )
    assert out["concession_text"] == "$500 off first month with 12-month lease"


def test_concession_text_legacy_alias_chain() -> None:
    """The runner accepts all 17 legacy aliases for concession text the
    canonical schema accepts (concession, concessions,
    specials_description, specials, promotion, promo, offer, offers,
    incentive, incentives, deal, savings, discount, free_rent,
    look_and_lease, move_in_special). Each variant must promote to the
    canonical ``concession_text`` key."""
    for alias in (
        "concession", "concessions", "specials_description",
        "specialsDescription", "special", "specials",
        "promotion", "promo", "offer", "offers",
        "incentive", "incentives", "deal", "savings",
        "discount", "free_rent", "look_and_lease", "move_in_special",
    ):
        unit = {
            "floor_plan_name": "A1",
            "market_rent_low": 1500,
            alias: "1 month free!",
        }
        out = _format_v2_unit(unit, _TS)
        assert out["concession_text"] == "1 month free!", (
            f"alias {alias!r} did not promote to concession_text; "
            f"got {out['concession_text']!r}"
        )


def test_canonical_text_wins_over_legacy_alias_when_both_present() -> None:
    """If both ``concession_text`` (canonical) and a legacy alias are
    set, the canonical wins. Same precedence as schema_v2.py."""
    unit = {
        "floor_plan_name": "A1",
        "market_rent_low": 1500,
        "concession_text": "Canonical text",
        "promotion": "Legacy text (should be ignored)",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["concession_text"] == "Canonical text"


def test_concession_clean_fires_on_non_empty_text() -> None:
    """``concession_text_clean`` should be a non-empty string when raw
    text is present (cleaning may normalise but won't fully empty it
    on real-world banner content)."""
    out = _format_v2_unit(
        {
            "floor_plan_name": "A1",
            "market_rent_low": 1500,
            "concession_text": "Move-in Special! $500 off first month.",
        },
        _TS,
    )
    assert out["concession_text_clean"] is not None
    assert isinstance(out["concession_text_clean"], str)


def test_concession_quality_classifier_fires_on_non_empty_text() -> None:
    """``_concession_quality`` is the classifier label from
    classify_concession_quality — should be a non-empty string label
    when raw text is present."""
    out = _format_v2_unit(
        {
            "floor_plan_name": "A1",
            "market_rent_low": 1500,
            "concession_text": "$500 off first month",
        },
        _TS,
    )
    assert out["_concession_quality"] is not None
    assert isinstance(out["_concession_quality"], str)


def test_concession_value_numeric_coercion() -> None:
    """``concession_value`` is a float; runner must coerce strings
    safely and pass-through real floats."""
    # As float
    out = _format_v2_unit(
        {"floor_plan_name": "A1", "concession_value": 500.0},
        _TS,
    )
    assert out["concession_value"] == 500.0

    # As stringified number (adapters have historically done this)
    out = _format_v2_unit(
        {"floor_plan_name": "A1", "concession_value": "1500"},
        _TS,
    )
    assert out["concession_value"] == 1500.0

    # Non-numeric string → None (defensive)
    out = _format_v2_unit(
        {"floor_plan_name": "A1", "concession_value": "see manager"},
        _TS,
    )
    assert out["concession_value"] is None


def test_offer_taxonomy_fields_passthrough() -> None:
    """Five offer_* keys flow through verbatim — set by the
    offer_extract module via make_unit_dict. The runner doesn't
    transform them; it just surfaces them."""
    unit = {
        "floor_plan_name": "A1",
        "market_rent_low": 1500,
        "offer_banner": "Move-in special",
        "offer_type": "RENT_DISCOUNT",
        "offer_target": "FIRST_MONTH",
        "offer_value": "500",
        "offer_conditions": "12-month lease required",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["offer_banner"] == "Move-in special"
    assert out["offer_type"] == "RENT_DISCOUNT"
    assert out["offer_target"] == "FIRST_MONTH"
    assert out["offer_value"] == "500"
    assert out["offer_conditions"] == "12-month lease required"


def test_concession_text_empty_string_normalises_to_none() -> None:
    """An empty / whitespace-only ``concession_text`` is treated as
    None — matches schema_v2 behaviour."""
    out = _format_v2_unit(
        {"floor_plan_name": "A1", "concession_text": ""},
        _TS,
    )
    assert out["concession_text"] is None
    assert out["concession_text_clean"] is None
    assert out["_concession_quality"] is None


def test_concession_text_whitespace_only_normalises_to_none() -> None:
    out = _format_v2_unit(
        {"floor_plan_name": "A1", "concession_text": "   \n\t  "},
        _TS,
    )
    assert out["concession_text"] is None


@pytest.mark.parametrize("non_string_value", [
    123,
    12.5,
    True,
    {"nested": "dict"},
    ["list", "of", "things"],
])
def test_concession_text_non_string_falls_through(non_string_value: object) -> None:
    """When ``concession_text`` is a non-string (some older adapter paths
    have emitted dicts/ints), it falls through to None — matches
    schema_v2 behaviour where non-string concession_text is invalid."""
    out = _format_v2_unit(
        {"floor_plan_name": "A1", "concession_text": non_string_value},
        _TS,
    )
    assert out["concession_text"] is None


def test_concession_source_passthrough() -> None:
    """``concession_source`` is just passed through — captures which
    selector/extractor found the banner (specials_section,
    rendered_dom, etc.)."""
    out = _format_v2_unit(
        {
            "floor_plan_name": "A1",
            "concession_text": "1 month free",
            "concession_source": "specials_section",
        },
        _TS,
    )
    assert out["concession_source"] == "specials_section"


def test_existing_unit_fields_unchanged_by_concession_addition() -> None:
    """The 10 new keys are ADDITIVE — pre-existing field semantics
    (beds, baths, sqft, unit_id, rent_low, etc.) must be preserved
    exactly as before so no regression on any other consumer."""
    unit = {
        "unit_id": "101",
        "floor_plan_name": "A1",
        "bedrooms": 1,
        "bathrooms": 1,
        "sqft": 750,
        "market_rent_low": 1500,
        "market_rent_high": 1500,
        "concession_text": "Move-in special",
    }
    out = _format_v2_unit(unit, _TS)
    # Pre-existing fields preserved.
    assert out["unit_id"] == "101"
    assert out["floor_plan_name"] == "A1"
    assert out["beds"] == 1
    assert out["baths"] == 1.0
    assert out["area"] == 750
    assert out["rent_low"] == 1500.0
    assert out["rent_high"] == 1500.0
    # New fields present.
    assert out["concession_text"] == "Move-in special"
