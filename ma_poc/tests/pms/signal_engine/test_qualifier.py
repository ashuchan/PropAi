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


# ── Q2: Generic unit API ─────────────────────────────────────────────────────
# unit_generic has been removed from the default qualifier — only cross-group
# bed+bath+area and bed+bath+floor_plan_name combinations are active.
# {rent, sqft, unitNumber, beds} has no bath key → fails both combinations.

def test_q2_generic_unit_api_without_bath_rejected(qualifier) -> None:
    """rent+sqft+unitNumber+beds fails: no bath key → neither cross-group rule fires."""
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://api.example.com/units",
        field_keys=frozenset({"rent", "sqft", "unitNumber", "beds"}),
    )
    result = qualifier.qualify(sig)
    assert result.qualifies is False


def test_q2_generic_unit_api_with_bath_qualifies(qualifier) -> None:
    """beds+bathrooms+sqft satisfies floor_plan_bed_bath_area (cross-group)."""
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://api.example.com/units",
        field_keys=frozenset({"beds", "bathrooms", "sqft"}),
    )
    result = qualifier.qualify(sig)
    assert result.qualifies is True
    assert result.matched_combination is not None
    assert result.matched_combination.label == "floor_plan_bed_bath_area"


# ── Q3: RentCafe unit-level ID keys (RC2) ─────────────────────────────────────
# Pure-ID combinations (only apartment/floorplan/property IDs, no data) are
# intentionally excluded from the default qualifier — they belong in
# create_rentcafe_qualifier() only. The default path requires actual data.

def test_q3_rentcafe_pure_id_keys_rejected_by_default_qualifier(qualifier) -> None:
    """RentCafeApartmentId + RentCafeFloorplanId alone must NOT qualify on
    the generic path. ID-only matches carry no unit data value.
    """
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://api.rentcafe.com/apiclient/apartmentsdetail",
        field_keys=frozenset({"RentCafeApartmentId", "RentCafeFloorplanId"}),
    )
    result = qualifier.qualify(sig)
    assert result.qualifies is False, (
        "Pure RentCafe ID keys must not qualify on the default (generic) qualifier"
    )


def test_q3_rentcafe_unit_ids_qualify_via_rentcafe_qualifier() -> None:
    """The same ID keys DO qualify on the RentCafe-specific qualifier."""
    from ma_poc.pms.signal_engine.defaults import create_rentcafe_qualifier
    rc_q = create_rentcafe_qualifier()
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://api.rentcafe.com/apiclient/apartmentsdetail",
        field_keys=frozenset({"RentCafeApartmentId", "RentCafeFloorplanId"}),
    )
    result = rc_q.qualify(sig)
    assert result.qualifies is True
    assert result.matched_combination is not None
    assert result.matched_combination.label == "rentcafe_unit"


# ── Q4: RentCafe floor-plan level ─────────────────────────────────────────────

def test_q4_rentcafe_floor_plan_rejected_by_default_qualifier(qualifier) -> None:
    """RentCafe floor-plan keys do NOT qualify on the generic path.
    They route via create_rentcafe_qualifier() inside _is_rentcafe_response().
    """
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://api.rentcafe.com/apiclient/floorplans",
        field_keys=frozenset({
            "floorplanname", "minimumrent", "maximumrent", "floorplanid",
        }),
    )
    result = qualifier.qualify(sig)
    assert result.qualifies is False


def test_q4b_rentcafe_floor_plan_qualifies_via_rentcafe_qualifier() -> None:
    """The same keys DO qualify on the RentCafe-specific qualifier."""
    from ma_poc.pms.signal_engine.defaults import create_rentcafe_qualifier
    rc_q = create_rentcafe_qualifier()
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://api.rentcafe.com/apiclient/floorplans",
        field_keys=frozenset({
            "floorplanid", "availableunitscount", "availabilityurl",
            "floorplanname", "minimumrent",
        }),
    )
    result = rc_q.qualify(sig)
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
    # Use field_keys that satisfy the cross-group bed+bath+area combination.
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://example.com/api/floorplans",
        field_keys=frozenset({"beds", "bathrooms", "sqft"}),
        blocked_at=blocked_at,
        noise_verdicts=3,
    )
    result = qualifier.qualify(sig)
    # TTL expired — re-admitted; field keys match floor_plan_bed_bath_area
    assert result.qualifies is True


# ── Q7: Insufficient noise verdicts ───────────────────────────────────────────

def test_q7_insufficient_noise_verdicts_readmitted(qualifier) -> None:
    blocked_at = datetime.now(timezone.utc) - timedelta(days=2)
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://example.com/api/floorplans",
        field_keys=frozenset({"beds", "bathrooms", "sqft"}),
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


# ── Default qualifier composition guards ──────────────────────────────────────

def test_default_qualifier_excludes_pure_id_combinations(qualifier) -> None:
    """rentcafe_unit (pure ID matching) must not appear in the default qualifier.
    ID-only matches carry no data value — they live in create_rentcafe_qualifier().
    """
    labels = {c.label for c in qualifier.combinations}
    assert "rentcafe_unit" not in labels, (
        "rentcafe_unit (pure IDs, no data) must not qualify on the generic path"
    )


# ── Cross-group FieldCombination enforcement ───────────────────────────────────

def _make_floor_plan_qualifier():
    """Isolated qualifier with only the two cross-group floor-plan combinations.

    The full default qualifier places unit_generic first, which fires for
    {beds, sqft} since both appear in _UNIT_SIGNAL_KEYS. An isolated qualifier
    lets tests exercise the cross-group combinations directly.
    """
    from ma_poc.pms.signal_engine.defaults import DEFAULT_MEDIA_FILTER
    from ma_poc.pms.signal_engine.qualifier import (
        FieldCombination,
        SourceQualifier,
    )
    return SourceQualifier(
        combinations=[
            FieldCombination(
                keys=frozenset({"beds", "bedrooms", "baths", "bathrooms", "sqft", "area"}),
                min_count=3,
                label="floor_plan_bed_bath_area",
                required_groups=(
                    frozenset({"beds", "bedrooms"}),
                    frozenset({"baths", "bathrooms"}),
                    frozenset({"sqft", "area"}),
                ),
            ),
            FieldCombination(
                keys=frozenset({
                    "beds", "bedrooms", "baths", "bathrooms",
                    "floor_plan_name", "floorplanname",
                }),
                min_count=3,
                label="floor_plan_bed_bath_name",
                required_groups=(
                    frozenset({"beds", "bedrooms"}),
                    frozenset({"baths", "bathrooms"}),
                    frozenset({"floor_plan_name", "floorplanname"}),
                ),
            ),
        ],
        media_filter=DEFAULT_MEDIA_FILTER,
    )


def test_bed_bath_area_combination_qualifies() -> None:
    """beds + bathrooms + sqft → floor_plan_bed_bath_area."""
    q = _make_floor_plan_qualifier()
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        field_keys=frozenset({"beds", "bathrooms", "sqft"}),
    )
    result = q.qualify(sig)
    assert result.qualifies is True
    assert result.matched_combination is not None
    assert result.matched_combination.label == "floor_plan_bed_bath_area"


def test_bed_bath_name_combination_qualifies() -> None:
    """bedrooms + baths + floorplanname → floor_plan_bed_bath_name."""
    q = _make_floor_plan_qualifier()
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        field_keys=frozenset({"bedrooms", "baths", "floorplanname"}),
    )
    result = q.qualify(sig)
    assert result.qualifies is True
    assert result.matched_combination is not None
    assert result.matched_combination.label == "floor_plan_bed_bath_name"


def test_three_bed_bath_synonyms_do_not_qualify(qualifier) -> None:
    """beds + bedrooms + bathrooms satisfies min_count=3 but fails cross-group:
    no area/sqft key present, so floor_plan_bed_bath_area must be rejected.
    And no floor_plan_name key, so floor_plan_bed_bath_name is also rejected.
    """
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        field_keys=frozenset({"beds", "bedrooms", "bathrooms"}),
    )
    result = qualifier.qualify(sig)
    # Must not match either floor_plan combination
    if result.qualifies and result.matched_combination:
        assert result.matched_combination.label not in (
            "floor_plan_bed_bath_area", "floor_plan_bed_bath_name"
        ), "Three bed/bath synonyms must not satisfy a bed+bath+area/name rule"


def test_beds_area_without_bath_does_not_qualify_as_floor_plan(qualifier) -> None:
    """beds + sqft without any bath key must NOT match floor_plan_bed_bath_area."""
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        field_keys=frozenset({"beds", "sqft"}),  # min_count=3 not met anyway
    )
    result = qualifier.qualify(sig)
    if result.qualifies and result.matched_combination:
        assert result.matched_combination.label != "floor_plan_bed_bath_area"


def test_required_groups_enforced_on_isolated_combination() -> None:
    """Unit test for FieldCombination.required_groups enforcement in SourceQualifier."""
    from ma_poc.pms.signal_engine.qualifier import (
        FieldCombination,
        MediaTypeFilter,
        SourceQualifier,
    )
    combo = FieldCombination(
        keys=frozenset({"a", "b", "c", "d"}),
        min_count=3,
        label="test_cross",
        required_groups=(frozenset({"a", "b"}), frozenset({"c", "d"})),
    )
    q = SourceQualifier(
        combinations=[combo],
        media_filter=MediaTypeFilter(
            blocked_content_types=frozenset(),
            blocked_url_suffixes=frozenset(),
        ),
    )
    # a + b + c: min_count met (3), group1={a,b}✓ group2={c}✓ → qualifies
    sig_ok = SourceSignal(kind=SourceKind.API_RESPONSE, field_keys=frozenset({"a", "b", "c"}))
    assert q.qualify(sig_ok).qualifies is True

    # a + b + (no c/d): min_count not met → rejected before cross-group check
    sig_no_count = SourceSignal(kind=SourceKind.API_RESPONSE, field_keys=frozenset({"a", "b"}))
    assert q.qualify(sig_no_count).qualifies is False

    # a + b + b2 (only group1 covered, 3 keys but no group2): min_count=3 but
    # we need at least one key from each required_group.
    # Use keys outside the combo to pad count without hitting group2.
    # The combo only checks combo.keys ∩ field_keys, so extra keys don't help.
    # Simulate: signal has a, b, and a third key NOT in combo → count < 3 anyway.
    # So test: a, b, c where c IS in combo (group2) — this passes, covered above.
    # Test the real edge: a, b, d(group2) — group1 covered by a, group2 by d → passes
    sig_group2_via_d = SourceSignal(kind=SourceKind.API_RESPONSE, field_keys=frozenset({"a", "b", "d"}))
    assert q.qualify(sig_group2_via_d).qualifies is True

    # All keys from group1 only (a, b + pretend a third): only 2 combo keys → count<3
    # The real cross-group failure: a,b,b — but frozenset deduplicates.
    # Correct test: a combo where group1 has 3 keys to reach min_count without group2.
    combo2 = FieldCombination(
        keys=frozenset({"a", "b", "x", "c", "d"}),
        min_count=3,
        label="test_cross2",
        required_groups=(frozenset({"a", "b", "x"}), frozenset({"c", "d"})),
    )
    q2 = SourceQualifier(combinations=[combo2], media_filter=q.media_filter)
    # a + b + x: min_count=3 ✓, group1={a,b,x}✓ but group2={c,d} NOT covered → rejected
    sig_only_group1 = SourceSignal(kind=SourceKind.API_RESPONSE, field_keys=frozenset({"a", "b", "x"}))
    assert q2.qualify(sig_only_group1).qualifies is False, (
        "min_count satisfied but group2 uncovered — must reject"
    )


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
