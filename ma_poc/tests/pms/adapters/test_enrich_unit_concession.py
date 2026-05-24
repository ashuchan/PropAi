"""Per-unit concession backfill tests (2026-05-24).

Pins ``enrich_unit_concession_fields`` — the post-process pass that
backfills canonical concession + offer fields on units produced by
raw-dict adapters (_api_parser.py, _html_extract.py, knock.py,
_air_communities.py, _amli.py, _funnel.py, _nestio_widget.py,
_realpage_leasing.py).

These adapters bypass ``make_unit_dict`` and write the legacy
``concession`` field directly. Without this backfill, downstream
consumers reading ``concession_text`` / ``concession_text_clean`` /
``offer_type`` / etc. see None even when an offer exists.
"""
from __future__ import annotations

from ma_poc.pms.adapters._parsing import enrich_unit_concession_fields


# ── Legacy concession field → canonical backfill ──────────────────────


def test_legacy_concession_populates_all_canonical_fields() -> None:
    """A raw-dict adapter that only set ``concession`` gets all canonical
    fields backfilled."""
    unit = {
        "floor_plan_name": "A1",
        "concession": "6 weeks free rent on select 1-bedrooms",
    }
    enrich_unit_concession_fields(unit)
    assert unit["concession_text"] == "6 weeks free rent on select 1-bedrooms"
    assert unit["concession"] == "6 weeks free rent on select 1-bedrooms"  # legacy mirror
    assert unit["concession_text_clean"]
    assert unit["_concession_quality"] in ("clean", "unclean_orphan_prefix")
    assert unit["offer_type"] == "free_rent"
    assert unit["offer_target"] == "rent"
    assert unit["offer_value"] == "6 weeks"


def test_canonical_field_already_set_takes_priority() -> None:
    """If ``concession_text`` was already populated (e.g. by
    ``make_unit_dict`` or ``_html_extract.py`` strikethrough_dom path),
    don't overwrite — capture-first principle."""
    unit = {
        "floor_plan_name": "B1",
        "concession": "ignore me",
        "concession_text": "Use the canonical text",
    }
    enrich_unit_concession_fields(unit)
    assert unit["concession_text"] == "Use the canonical text"
    assert unit["offer_value"] is None  # text doesn't match any offer pattern


# ── Property-level fallback ───────────────────────────────────────────


def test_property_level_text_fills_when_unit_has_none() -> None:
    """When the unit has no concession but the property-level banner
    captured one (via scraper.py Step 3), the per-unit fields populate
    from that fallback."""
    unit = {"floor_plan_name": "C1"}
    enrich_unit_concession_fields(
        unit,
        property_concession_text="$500 off rent · apply within 48 hours",
    )
    assert unit["concession_text"] == "$500 off rent · apply within 48 hours"
    assert unit["offer_type"] == "dollar_off"
    assert unit["offer_value"] == "$500"
    assert "apply_within:48h" in (unit["offer_conditions"] or "")


def test_property_level_text_does_not_override_unit_level() -> None:
    """A unit with its own concession beats the property-level fallback."""
    unit = {
        "floor_plan_name": "D1",
        "concession": "1 month free for this unit only",
    }
    enrich_unit_concession_fields(
        unit,
        property_concession_text="$500 off (whole property)",
    )
    assert unit["concession_text"] == "1 month free for this unit only"
    assert unit["offer_type"] == "free_rent"


# ── Idempotency ───────────────────────────────────────────────────────


def test_idempotent_when_called_twice() -> None:
    """Running twice produces the same output — safe to call from
    multiple post-process passes."""
    unit = {"floor_plan_name": "E1", "concession": "6 weeks free rent"}
    enrich_unit_concession_fields(unit)
    snapshot = dict(unit)
    enrich_unit_concession_fields(unit)
    assert unit == snapshot


def test_make_unit_dict_output_passes_through_unchanged() -> None:
    """A unit produced by make_unit_dict (which already sets canonical
    fields) goes through enrich without losing or changing anything."""
    from ma_poc.pms.adapters._parsing import make_unit_dict

    u = make_unit_dict(
        floor_plan_name="F1",
        rent_low=1500,
        rent_high=1500,
        concession="$400 off rent",
    )
    pre_offer_type = u["offer_type"]
    pre_offer_value = u["offer_value"]
    enrich_unit_concession_fields(u)
    assert u["offer_type"] == pre_offer_type
    assert u["offer_value"] == pre_offer_value


# ── Caller-supplied concession_value preserved ───────────────────────


def test_caller_supplied_concession_value_preserved() -> None:
    """Adapter that knows the numeric value (parsed from structured API)
    has that value preserved by enrich — not overwritten by derived."""
    unit = {
        "floor_plan_name": "G1",
        "concession": "Special $X off",
        "concession_value": 1234.0,  # adapter parsed this
    }
    enrich_unit_concession_fields(unit)
    assert unit["concession_value"] == 1234.0


# ── Empty / missing concession → clean None state ────────────────────


def test_no_concession_data_anywhere_leaves_all_none() -> None:
    unit = {"floor_plan_name": "H1", "rent_low": 1500}
    enrich_unit_concession_fields(unit)
    assert unit["concession_text"] is None
    assert unit["concession_text_clean"] is None
    assert unit["_concession_quality"] is None
    assert unit["concession_value"] is None
    assert unit["concession_source"] is None
    assert unit["offer_type"] is None
    assert unit["offer_banner"] is None


def test_whitespace_only_legacy_field_treated_as_none() -> None:
    unit = {"floor_plan_name": "I1", "concession": "   "}
    enrich_unit_concession_fields(unit)
    assert unit["concession_text"] is None


# ── Raw-dict adapter shape reproductions ─────────────────────────────


def test_api_parser_raw_dict_shape() -> None:
    """Real shape from _api_parser.py:489 — generic API parser emits
    a raw dict with concession from ``specials_description``."""
    unit = {
        "unit_number": "101",
        "floor_plan_name": "1BR",
        "bedrooms": "1",
        "bathrooms": "1",
        "sqft": "750",
        "market_rent_low": 1500,
        "market_rent_high": 1500,
        "deposit": "",
        "concession": "First month free with 12-month lease",
        "availability_status": "AVAILABLE",
        "available_units": "1",
    }
    enrich_unit_concession_fields(unit)
    assert unit["concession_text"] == "First month free with 12-month lease"
    assert unit["offer_type"] == "free_rent"
    assert unit["offer_value"] == "first month"
    assert unit["offer_conditions"] == "lease_length:12+ months"


def test_html_extract_empty_concession_default() -> None:
    """Real shape from _html_extract.py (6 sites) — emits
    ``"concession": ""``. After enrich, all canonical fields are None
    (the empty string is correctly treated as no-data)."""
    unit = {
        "floor_plan_name": "1BR",
        "concession": "",
    }
    enrich_unit_concession_fields(unit)
    assert unit["concession_text"] is None
    assert unit["concession_text_clean"] is None
    assert unit["offer_type"] is None


def test_html_extract_with_property_level_fallback() -> None:
    """Same _html_extract.py shape, but the property-level banner DID
    capture an offer — the per-unit fields populate from that."""
    unit = {
        "floor_plan_name": "1BR",
        "concession": "",
    }
    enrich_unit_concession_fields(
        unit,
        property_concession_text=(
            "Reduced rent special for military families this month only"
        ),
    )
    assert unit["concession_text"] is not None
    assert unit["offer_type"] == "reduced_rate"
    assert unit["offer_conditions"] == "audience:military"


def test_knock_raw_dict_with_concession() -> None:
    """Real shape from knock.py:175 — raw unit dict with concession
    pulled from doorway-api ``leasingSpecial``."""
    unit = {
        "unit_number": "5A",
        "floor_plan_name": "Studio",
        "bedrooms": "0",
        "bathrooms": "1",
        "sqft": "450",
        "market_rent_low": 1200,
        "market_rent_high": 1200,
        "rent_range": "1200",
        "availability_status": "AVAILABLE",
        "availability_date": "2026-06-01",
        "building": "Building A",
        "concession": (
            "APRIL SHOWERS BRING FREE RENT! Move In by April 30, 2025 "
            "and Receive One Month Free"
        ),
        "extraction_tier": "TIER_1_KNOCK_API",
    }
    enrich_unit_concession_fields(unit)
    assert unit["concession_text"] is not None
    assert "APRIL SHOWERS" in unit["concession_text"]
    assert unit["offer_type"] == "free_rent"
    assert unit["offer_value"] == "1 month"
    cond = unit["offer_conditions"] or ""
    assert "deadline:April 30" in cond or "30" in cond
