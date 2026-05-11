"""Phase 1 qualifier integration tests — spec §9, cases Q1–Q7.

Tests both the SourceQualifier directly and the create_default_qualifier factory.
All 7 spec cases are implemented exactly as described.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ma_poc.pms.signal_engine.defaults import create_default_qualifier
from ma_poc.pms.signal_engine.models import SourceKind, SourceSignal


@pytest.fixture(scope="module")
def qualifier():
    return create_default_qualifier()


# ── Q1: JS file blocked by media type ─────────────────────────────────────────

def test_q1_js_content_type_blocked(qualifier) -> None:
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://cdn.rentcafe.com/app.js",
        content_type="text/javascript",
        field_keys=frozenset(),
    )
    result = qualifier.qualify(sig)
    assert result.qualifies is False
    assert result.reason.startswith("media_blocked:")


# ── Q2: Generic unit API ───────────────────────────────────────────────────────

def test_q2_generic_unit_api_qualifies(qualifier) -> None:
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://api.example.com/units",
        field_keys=frozenset({"rent", "sqft", "unitNumber", "beds"}),
    )
    result = qualifier.qualify(sig)
    assert result.qualifies is True
    assert result.matched_combination is not None
    assert result.matched_combination.label == "unit_generic"


# ── Q3: RentCafe unit-level (RC2) ─────────────────────────────────────────────

def test_q3_rentcafe_unit_keys_qualify(qualifier) -> None:
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://api.rentcafe.com/apiclient/apartmentsdetail",
        # PascalCase — __post_init__ normalises to lowercase
        field_keys=frozenset({"RentCafeApartmentId", "RentCafeFloorplanId"}),
    )
    result = qualifier.qualify(sig)
    assert result.qualifies is True
    assert result.matched_combination is not None
    assert result.matched_combination.label == "rentcafe_unit"


# ── Q4: RentCafe floor-plan level ─────────────────────────────────────────────

def test_q4_rentcafe_floor_plan_qualifies(qualifier) -> None:
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://api.rentcafe.com/apiclient/floorplans",
        field_keys=frozenset({
            "floorplanname", "minimumrent", "maximumrent", "floorplanid",
        }),
    )
    result = qualifier.qualify(sig)
    assert result.qualifies is True
    assert result.matched_combination is not None
    # floorplanname, minimumrent, maximumrent are in _UNIT_SIGNAL_KEYS (lowercased)
    # so unit_generic fires first; either label confirms unit-data detected.
    assert result.matched_combination.label in ("unit_generic", "rentcafe_floor_plan")


def test_q4b_rentcafe_floor_plan_label_with_pure_fp_keys(qualifier) -> None:
    """Keys that are NOT in _UNIT_SIGNAL_KEYS force the rentcafe_floor_plan combination."""
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://api.rentcafe.com/apiclient/floorplans",
        # availableunitscount and availabilityurl are not in _UNIT_SIGNAL_KEYS
        field_keys=frozenset({
            "floorplanid", "availableunitscount", "availabilityurl",
        }),
    )
    result = qualifier.qualify(sig)
    assert result.qualifies is True
    assert result.matched_combination is not None
    assert result.matched_combination.label == "rentcafe_floor_plan"


# ── Q5: Blocked endpoint within TTL ───────────────────────────────────────────

def test_q5_blocked_within_ttl_rejected(qualifier) -> None:
    blocked_at = datetime.now(timezone.utc) - timedelta(days=5)
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://example.com/api/chat",
        field_keys=frozenset({"message", "response"}),
        blocked_at=blocked_at,
        noise_verdicts=2,
    )
    result = qualifier.qualify(sig)
    assert result.qualifies is False
    assert result.reason.startswith("blocked:")


# ── Q6: Blocked endpoint TTL expired ──────────────────────────────────────────

def test_q6_blocked_ttl_expired_readmitted(qualifier) -> None:
    blocked_at = datetime.now(timezone.utc) - timedelta(days=15)
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://example.com/api/chat",
        field_keys=frozenset({"rent", "sqft", "unitNumber", "beds"}),
        blocked_at=blocked_at,
        noise_verdicts=3,
    )
    result = qualifier.qualify(sig)
    # TTL expired — re-admitted; field keys now match unit_generic
    assert result.qualifies is True


# ── Q7: Insufficient noise verdicts ───────────────────────────────────────────

def test_q7_insufficient_noise_verdicts_readmitted(qualifier) -> None:
    blocked_at = datetime.now(timezone.utc) - timedelta(days=2)
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://example.com/api/chat",
        field_keys=frozenset({"rent", "unitNumber", "beds", "sqft"}),
        blocked_at=blocked_at,
        noise_verdicts=1,  # < min_noise_verdicts=2
    )
    result = qualifier.qualify(sig)
    assert result.qualifies is True


# ── Additional: non-API kinds bypass field check ───────────────────────────────

def test_internal_link_qualifies_without_field_check(qualifier) -> None:
    sig = SourceSignal(
        kind=SourceKind.INTERNAL_LINK,
        url="https://example.com/floorplans",
        field_keys=frozenset(),  # no fields — link has no body
    )
    result = qualifier.qualify(sig)
    assert result.qualifies is True
    assert result.reason.startswith("non_api:")


def test_llm_hint_qualifies_without_field_check(qualifier) -> None:
    sig = SourceSignal(
        kind=SourceKind.LLM_HINT,
        url="https://example.com/floorplans",
    )
    result = qualifier.qualify(sig)
    assert result.qualifies is True


def test_api_with_no_field_keys_rejected(qualifier) -> None:
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://example.com/api/empty",
        field_keys=frozenset(),
    )
    result = qualifier.qualify(sig)
    assert result.qualifies is False
    assert result.reason == "no_field_keys"


# ── SourceSignal invariants ────────────────────────────────────────────────────

def test_source_signal_field_keys_always_lowercase() -> None:
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        field_keys=frozenset({"RentCafeApartmentId", "UnitRent", "MarketRent"}),
    )
    assert all(k == k.lower() for k in sig.field_keys)


def test_source_signal_url_suffix_derived_from_url() -> None:
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://cdn.example.com/bundle.min.js?v=42",
    )
    assert sig.url_suffix == ".js"


def test_source_signal_url_suffix_explicit_wins() -> None:
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://example.com/bundle",
        url_suffix=".js",
    )
    assert sig.url_suffix == ".js"


# ── Media filter: URL suffix gate ─────────────────────────────────────────────

def test_js_url_suffix_blocked_even_without_content_type(qualifier) -> None:
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://cdngeneralmvc.rentcafe.com/bundle.min.js",
        content_type=None,
        field_keys=frozenset(),
    )
    result = qualifier.qualify(sig)
    assert result.qualifies is False
    assert result.reason.startswith("media_blocked:")


def test_woff2_font_blocked(qualifier) -> None:
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://fonts.example.com/font.woff2",
        content_type="font/woff2",
    )
    result = qualifier.qualify(sig)
    assert result.qualifies is False


# ── qualify_many convenience ───────────────────────────────────────────────────

def test_qualify_many_returns_pairs(qualifier) -> None:
    signals = [
        SourceSignal(kind=SourceKind.LLM_HINT, url="https://example.com/fp"),
        SourceSignal(
            kind=SourceKind.API_RESPONSE,
            content_type="text/javascript",
            field_keys=frozenset(),
        ),
    ]
    results = qualifier.qualify_many(signals)
    assert len(results) == 2
    assert results[0][1].qualifies is True
    assert results[1][1].qualifies is False
