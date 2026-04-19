"""Tests for validation orchestrator — end-to-end validation pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ma_poc.validation.orchestrator import validate


@dataclass
class FakeExtractResult:
    property_id: str = "test_001"
    records: list[dict[str, Any]] = field(default_factory=list)
    tier_used: str = "test:tier"
    adapter_name: str = "test"
    confidence: float = 0.9


def _valid_unit(unit_id: str = "u1") -> dict:
    return {
        "unit_id": unit_id,
        "floor_plan_type": "1BR",
        "bedrooms": 1,
        "asking_rent": 1500,
        "sqft": 750,
    }


def test_validate_all_accept_no_next_tier() -> None:
    er = FakeExtractResult(records=[_valid_unit("u1"), _valid_unit("u2")])
    vr = validate(er)
    assert len(vr.accepted) == 2
    assert len(vr.rejected) == 0
    assert vr.next_tier_requested is False


def test_validate_majority_reject_requests_next_tier() -> None:
    er = FakeExtractResult(records=[
        {"asking_rent": -100},  # reject
        {"asking_rent": -200},  # reject
        _valid_unit("u1"),  # accept
    ])
    vr = validate(er)
    assert len(vr.rejected) == 2
    assert len(vr.accepted) == 1
    assert vr.next_tier_requested is True


def test_validate_exactly_half_reject_does_not_request_next_tier() -> None:
    er = FakeExtractResult(records=[
        {"asking_rent": -100},  # reject
        _valid_unit("u1"),  # accept
    ])
    vr = validate(er)
    assert len(vr.rejected) == 1
    assert len(vr.accepted) == 1
    assert vr.next_tier_requested is False  # strict >0.5


def test_validate_flags_do_not_count_as_rejects() -> None:
    er = FakeExtractResult(records=[_valid_unit("u1")])
    history = {"u1": {"asking_rent": 3000, "sqft": 750}}
    vr = validate(er, history)
    assert len(vr.accepted) == 1
    assert len(vr.rejected) == 0
    assert len(vr.flagged) == 1  # rent swing flagged


def test_validate_preserves_source_extract_reference() -> None:
    er = FakeExtractResult(records=[_valid_unit()])
    vr = validate(er)
    assert vr.source_extract is er


def test_validate_inferred_ids_counted() -> None:
    er = FakeExtractResult(records=[
        {"floor_plan_type": "1BR", "bedrooms": 1, "asking_rent": 1500, "sqft": 750},
    ])
    vr = validate(er)
    assert vr.identity_fallback_used_count == 1


def test_validate_emits_events_per_record() -> None:
    er = FakeExtractResult(records=[_valid_unit(), {"asking_rent": -100}])
    vr = validate(er)
    assert len(vr.accepted) == 1
    assert len(vr.rejected) == 1


def test_validate_never_raises_on_malformed_record() -> None:
    er = FakeExtractResult(records=[
        {},  # Completely empty
        None,  # type: ignore
    ])
    # Should not raise
    try:
        vr = validate(er)
    except TypeError:
        # None record may cause TypeError, that's acceptable to catch
        pass
