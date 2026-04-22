"""Tests for carry_forward — safety net re-emission."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from ma_poc.discovery.carry_forward import (
    carry_forward_property,
    should_carry_forward,
)


def _make_state_store(prior: dict | None = None) -> MagicMock:
    store = MagicMock()
    store.get_last_known_property.return_value = prior
    return store


def test_carry_forward_returns_none_when_no_prior_record(tmp_path: Path) -> None:
    store = _make_state_store(None)
    result = carry_forward_property("p1", tmp_path, store, "fetch_failed")
    assert result is None


def test_carry_forward_copies_prior_record_with_tag(tmp_path: Path) -> None:
    prior = {"_meta": {"canonical_id": "p1"}, "units": [{"unit_id": "u1"}]}
    store = _make_state_store(prior)
    result = carry_forward_property("p1", tmp_path, store, "fetch_failed")
    assert result is not None
    assert result["_meta"]["scrape_outcome"] == "CARRY_FORWARD"


def test_carry_forward_marks_scrape_outcome_code(tmp_path: Path) -> None:
    prior = {"_meta": {"canonical_id": "p1"}, "units": []}
    store = _make_state_store(prior)
    result = carry_forward_property("p1", tmp_path, store, "extraction_failed")
    assert result is not None
    assert result["_meta"]["carry_forward_reason"] == "extraction_failed"


def test_carry_forward_preserves_unit_identities(tmp_path: Path) -> None:
    units = [{"unit_id": "u1", "asking_rent": 1500}, {"unit_id": "u2", "asking_rent": 2000}]
    prior = {"_meta": {"canonical_id": "p1"}, "units": units}
    store = _make_state_store(prior)
    result = carry_forward_property("p1", tmp_path, store, "fetch_failed")
    assert result is not None
    assert len(result["units"]) == 2
    assert result["units"][0]["unit_id"] == "u1"


def test_carry_forward_does_not_fire_when_current_scrape_ok() -> None:
    result = {"_meta": {"scrape_tier_used": "TIER_1_API"}, "units": [{"u": 1}]}
    should, reason = should_carry_forward(result)
    assert should is False


def test_carry_forward_fires_on_fetch_hard_fail() -> None:
    # When fetch_outcome is HARD_FAIL, carry forward fires regardless of result
    should, reason = should_carry_forward(None, fetch_outcome="HARD_FAIL")
    assert should is True
    assert "HARD_FAIL" in reason
