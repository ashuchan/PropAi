"""Phase 4 — anchor-first merge cascade tests.

Validates the H2/H8/H9/H10 invariants from CLAUDE_PROMPTS_MERGE_RESILIENCE.md.
"""

from __future__ import annotations

import sys
import types

import pytest

# Stub playwright like the rest of the integration suite.
for _name in ("playwright", "playwright.async_api"):
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        _mod.BrowserContext = object  # type: ignore[attr-defined]
        _mod.Page = object  # type: ignore[attr-defined]
        _mod.async_playwright = object  # type: ignore[attr-defined]
        sys.modules[_name] = _mod

from ma_poc.observability import events as ev_module
from ma_poc.pms.adapters._merge_fns import (
    availability_count_aware_merge as _availability_count_aware_merge,
)
from ma_poc.pms.adapters.generic import (
    _merge_into_result_units,
)


# ── R0 / R0a ───────────────────────────────────────────────────────────────


def test_R0_floor_plan_id_wins_over_R0a_unit_id() -> None:
    """When floor_plan_id matches on both sides, R0 fires first regardless of unit_id."""
    existing = [
        {"unit_id": "OLD", "floor_plan_id": "P1_1", "beds": 1, "rent_low": 1000},
    ]
    incoming = [
        {"unit_id": "NEW", "floor_plan_id": "P1_1", "rent_low": 1100},
    ]
    result = _merge_into_result_units(existing, incoming, property_id="P1")
    assert len(result) == 1
    # rent_low is mutable → latest wins (1100); floor_plan_id was the anchor.
    assert result[0]["rent_low"] == 1100
    # unit_id is physical → existing wins on conflict.
    assert result[0]["unit_id"] == "OLD"


# ── H2: strict sqft ────────────────────────────────────────────────────────


def test_strict_sqft_no_tolerance_in_R1b() -> None:
    """H2: even a 1-sqft difference blocks R1b match."""
    existing = [{"beds": 1, "baths": 1.0, "sqft": 750, "rent_low": 1400}]
    incoming = [{"beds": 1, "baths": 1.0, "sqft": 751, "rent_low": 1500}]
    result = _merge_into_result_units(existing, incoming, property_id="P1")
    assert len(result) == 2


# ── R1c (floor_plan_name + beds + baths) ───────────────────────────────────


def test_R1c_floor_plan_name_match_with_baths() -> None:
    existing = [{"floor_plan_name": "Aspen", "beds": 1, "baths": 1.0, "sqft": 750}]
    incoming = [{"floor_plan_name": "Aspen", "beds": 1, "baths": 1.0, "rent_low": 1400}]
    result = _merge_into_result_units(existing, incoming, property_id="P1")
    assert len(result) == 1
    assert result[0]["sqft"] == 750  # physical preserved
    assert result[0]["rent_low"] == 1400


# ── R1d / R1e / R1f loose rungs ────────────────────────────────────────────


def test_R1d_two_field_match_when_sqft_null() -> None:
    """R1d: beds + baths match, sqft null on both, no name on either side."""
    existing = [{"beds": 2, "baths": 2.0, "rent_low": 1700}]
    incoming = [{"beds": 2, "baths": 2.0, "available_date": "2026-07-01"}]
    result = _merge_into_result_units(existing, incoming, property_id="P1")
    assert len(result) == 1
    assert result[0]["available_date"] == "2026-07-01"


def test_R1e_beds_only_when_others_null() -> None:
    """R1e fires when only beds match (baths, sqft, name all null on at least one side)."""
    existing = [{"beds": 1, "rent_low": 1400}]
    incoming = [{"beds": 1, "available_date": "2026-08-01"}]
    result = _merge_into_result_units(existing, incoming, property_id="P1")
    assert len(result) == 1
    assert result[0]["rent_low"] == 1400
    assert result[0]["available_date"] == "2026-08-01"


def test_R1f_beds_plus_floor_plan_when_baths_sqft_null() -> None:
    existing = [{"beds": 1, "floor_plan_name": "Aspen", "rent_low": 1400}]
    incoming = [{"beds": 1, "floor_plan_name": "Aspen", "concession_text": "1 month free"}]
    result = _merge_into_result_units(existing, incoming, property_id="P1")
    assert len(result) == 1
    assert result[0]["concession_text"] == "1 month free"


# ── H8: ambiguous fail-closed ──────────────────────────────────────────────


def test_ambiguous_match_fails_closed_R1c() -> None:
    """Two existing records match the same R1c signature → new record appended."""
    existing = [
        {"floor_plan_name": "Maple", "beds": 2, "baths": 2.0, "sqft": 950, "unit_id": "A"},
        {"floor_plan_name": "Maple", "beds": 2, "baths": 2.0, "sqft": 1000, "unit_id": "B"},
    ]
    incoming = [
        {"floor_plan_name": "Maple", "beds": 2, "baths": 2.0, "rent_low": 1500},
    ]
    result = _merge_into_result_units(existing, incoming, property_id="P1")
    assert len(result) == 3, "ambiguous R1c match must fail closed (H8)"


def test_ambiguous_match_fails_closed_R1e() -> None:
    """Two existing 1BR-only records → new 1BR-only record gets appended."""
    existing = [
        {"beds": 1, "unit_id": "A", "rent_low": 1200},
        {"beds": 1, "unit_id": "B", "rent_low": 1300},
    ]
    incoming = [{"beds": 1, "rent_low": 1400}]
    result = _merge_into_result_units(existing, incoming, property_id="P1")
    assert len(result) == 3


# ── H9 mutable / physical field merge ──────────────────────────────────────


def test_mutable_latest_wins() -> None:
    existing = [{"unit_id": "U1", "rent_low": 1000, "available_date": "2026-06-01"}]
    incoming = [{"unit_id": "U1", "rent_low": 1100, "available_date": "2026-07-01"}]
    result = _merge_into_result_units(existing, incoming, property_id="P1")
    assert result[0]["rent_low"] == 1100
    assert result[0]["available_date"] == "2026-07-01"


def test_physical_existing_wins() -> None:
    existing = [{"unit_id": "U1", "sqft": 750, "beds": 1, "baths": 1.0}]
    incoming = [{"unit_id": "U1", "sqft": 800, "beds": 2, "baths": 2.0}]
    result = _merge_into_result_units(existing, incoming, property_id="P1")
    assert result[0]["sqft"] == 750
    assert result[0]["beds"] == 1
    assert result[0]["baths"] == 1.0


def test_physical_conflict_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """When physical fields differ, an EXTRACT_PHYSICAL_ATTRIBUTE_CONFLICT event fires."""
    captured: list[tuple] = []

    def fake_emit(kind, pid, **data):
        captured.append((kind.value, pid, data))

        # Reuse a real Event object to satisfy callers that read attrs.
        return ev_module.Event(kind=kind, property_id=pid, data=data)

    monkeypatch.setattr(ev_module, "emit", fake_emit)

    existing = [{"unit_id": "U1", "sqft": 750}]
    incoming = [{"unit_id": "U1", "sqft": 800}]
    _merge_into_result_units(existing, incoming, property_id="P1")
    assert any(
        e[0] == "extract.physical_attribute_conflict" and e[2]["field"] == "sqft"
        for e in captured
    ), f"expected a physical_attribute_conflict event, got {captured}"


# ── H10 ────────────────────────────────────────────────────────────────────


def test_merge_within_property_only() -> None:
    """The merge function operates within a single property — callers group
    by property_id, and property_id is forwarded only as telemetry. The
    function never crosses property boundaries because every call is scoped
    to one property's units list."""
    p1 = [{"unit_id": "U1", "beds": 1, "rent_low": 1000}]
    p2 = [{"unit_id": "U1", "beds": 1, "rent_low": 2000}]
    # The two property lists are merged independently — both inputs survive.
    out_p1 = _merge_into_result_units(p1, [], property_id="P1")
    out_p2 = _merge_into_result_units(p2, [], property_id="P2")
    assert out_p1[0]["rent_low"] == 1000
    assert out_p2[0]["rent_low"] == 2000


# ── availability_count semantics ───────────────────────────────────────────


def test_availability_count_summed_on_R0_R1a_only() -> None:
    """At full-attribute matches (R0 / R1a), counts are additive."""
    target = {"availability_count": 3}
    source = {"availability_count": 2}
    _availability_count_aware_merge(target, source, "R0")
    assert target["availability_count"] == 5

    target = {"availability_count": 3}
    _availability_count_aware_merge(target, source, "R1a")
    assert target["availability_count"] == 5


def test_availability_count_latest_wins_below_R1a() -> None:
    target = {"availability_count": 3}
    source = {"availability_count": 2}
    _availability_count_aware_merge(target, source, "R1c")
    assert target["availability_count"] == 2  # latest wins below R1a


def test_amenities_union_dedups() -> None:
    existing = [{"unit_id": "U1", "amenities": ["pool", "gym"]}]
    incoming = [{"unit_id": "U1", "amenities": ["gym", "dog park"]}]
    result = _merge_into_result_units(existing, incoming, property_id="P1")
    assert sorted(result[0]["amenities"]) == ["dog park", "gym", "pool"]


def test_amenities_union_preserves_existing_scalar() -> None:
    """Bug fix regression test: when existing amenities is a non-list scalar
    (e.g. a previous tier set it as a single string) and incoming provides a
    list, the existing scalar must survive the union."""
    existing = [{"unit_id": "U1", "amenities": "pool"}]
    incoming = [{"unit_id": "U1", "amenities": ["gym"]}]
    result = _merge_into_result_units(existing, incoming, property_id="P1")
    out = result[0]["amenities"]
    assert isinstance(out, list)
    assert "pool" in out
    assert "gym" in out


def test_ambiguous_fail_closed_event_carries_real_candidate_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug fix regression test: candidate_count was previously hardcoded to 2."""
    captured: list[tuple] = []

    def fake_emit(kind, pid, **data):
        captured.append((kind.value, pid, data))
        return ev_module.Event(kind=kind, property_id=pid, data=data)

    monkeypatch.setattr(ev_module, "emit", fake_emit)

    # Three existing 1BR records ambiguous at R1e.
    existing = [
        {"beds": 1, "unit_id": "A"},
        {"beds": 1, "unit_id": "B"},
        {"beds": 1, "unit_id": "C"},
    ]
    incoming = [{"beds": 1, "rent_low": 1500}]
    _merge_into_result_units(existing, incoming, property_id="P1")
    matches = [
        e for e in captured if e[0] == "extract.ambiguous_merge_fail_closed"
    ]
    assert matches
    assert matches[0][2]["candidate_count"] == 3
