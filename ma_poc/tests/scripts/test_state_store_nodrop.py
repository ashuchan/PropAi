"""Tests for scripts/state_store.py no-drop contract and UNITS_KEYLESS_HIGH gate.

Regression scope (B1 fix):
  - Truly anchorless units must be rescued via synthesize_unkeyable_id,
    NOT silently skipped.  synthetic_key_used must increment; skipped_no_identity
    must stay 0.
  - The merge_yield gate must fire UNITS_KEYLESS_HIGH when > 50 % of units
    are unkeyable — tests the full FS state-store → merge_yield path that was
    broken before the fix.

Carry-forward tests (B2 fix):
  - carry_forward_units must restore prior units when today's scrape fails.
  - data from yesterday's index is accessible even when the run_dir arg
    does not resolve to a "latest" symlink.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.scripts.state_store import StateStore
from ma_poc.services.merge_yield import evaluate as merge_yield_evaluate


# ── helpers ────────────────────────────────────────────────────────────────────

def _anchored(unit_id: str, rent: float = 1500.0) -> dict:
    return {"unit_id": unit_id, "market_rent_low": rent, "bedrooms": 1, "sqft": 750}


def _anchorless(rent: float = 1400.0) -> dict:
    """Unit with no unit_id and no physical fields — truly unkeyable."""
    return {"market_rent_low": rent}


def _fp_only(fp_name: str) -> dict:
    """Unit with only a floor plan name — eligible for _last_resort_key."""
    return {"floor_plan_name": fp_name, "market_rent_low": 1300.0}


# ── no-drop contract ───────────────────────────────────────────────────────────

class TestNoDrop:
    def test_anchored_unit_not_skipped(self, tmp_path: Path):
        ss = StateStore(tmp_path)
        diff = ss.upsert_units("p1", [_anchored("101")], "2026-05-07")
        assert diff["skipped_no_identity"] == 0
        assert diff["synthetic_key_used"] == 0
        assert diff["input_count"] == 1
        assert "101" in diff["new"]

    def test_anchorless_rescued_not_skipped(self, tmp_path: Path):
        """Anchorless unit must be rescued with unkeyable_ id, not dropped."""
        ss = StateStore(tmp_path)
        diff = ss.upsert_units("p1", [_anchorless()], "2026-05-07")
        assert diff["skipped_no_identity"] == 0
        assert diff["synthetic_key_used"] == 1
        assert diff["input_count"] == 1
        assert len(diff["new"]) == 1
        assert diff["new"][0].startswith("unkeyable_")

    def test_anchorless_uid_stored_in_index(self, tmp_path: Path):
        ss = StateStore(tmp_path)
        ss.upsert_units("p1", [_anchorless(1500.0)], "2026-05-07")
        idx = ss.unit_index.get("p1", {})
        assert len(idx) == 1
        stored_uid = next(iter(idx))
        assert stored_uid.startswith("unkeyable_")

    def test_multiple_anchorless_each_get_unique_id(self, tmp_path: Path):
        """Two anchorless units with different rents → different unkeyable_ ids."""
        ss = StateStore(tmp_path)
        diff = ss.upsert_units(
            "p1",
            [_anchorless(1400.0), _anchorless(1600.0)],
            "2026-05-07",
        )
        assert diff["synthetic_key_used"] == 2
        assert len(diff["new"]) == 2
        uid_a, uid_b = diff["new"]
        assert uid_a != uid_b
        assert uid_a.startswith("unkeyable_")
        assert uid_b.startswith("unkeyable_")

    def test_identical_anchorless_collide(self, tmp_path: Path):
        """Two byte-identical anchorless units → same id (within-batch dedup)."""
        ss = StateStore(tmp_path)
        # Use separate dicts (same content) to avoid mutation aliasing
        diff = ss.upsert_units("p1", [_anchorless(1500.0), _anchorless(1500.0)], "2026-05-07")
        # Both resolve to the same unkeyable_ id; second is "unchanged"
        assert diff["synthetic_key_used"] == 2
        # new + unchanged = 2 total entries, but same key so index has 1 entry
        assert len(ss.unit_index.get("p1", {})) == 1

    def test_fp_only_uses_inferred_not_unkeyable(self, tmp_path: Path):
        """Floor-plan-only unit → inferred_ id, not unkeyable_."""
        ss = StateStore(tmp_path)
        diff = ss.upsert_units("p1", [_fp_only("1BR")], "2026-05-07")
        assert diff["synthetic_key_used"] == 0
        assert diff["skipped_no_identity"] == 0
        assert diff["new"][0].startswith("inferred_")

    def test_mixed_batch_counts_correctly(self, tmp_path: Path):
        ss = StateStore(tmp_path)
        units = [
            _anchored("101"),       # natural id
            _fp_only("2BR"),        # inferred_
            _anchorless(1200.0),    # unkeyable_
            _anchorless(1300.0),    # unkeyable_
        ]
        diff = ss.upsert_units("p1", units, "2026-05-07")
        assert diff["input_count"] == 4
        assert diff["synthetic_key_used"] == 2
        assert diff["skipped_no_identity"] == 0
        assert len(diff["new"]) == 4


# ── UNITS_KEYLESS_HIGH gate ────────────────────────────────────────────────────

class TestUnitsKeylessHighGate:
    """Verify merge_yield.evaluate fires correctly from real state_store diffs."""

    def _diff_from_units(self, units: list[dict], tmp_path: Path) -> dict:
        ss = StateStore(tmp_path)
        return ss.upsert_units("prop", units, "2026-05-07")

    def test_gate_fires_when_majority_unkeyable(self, tmp_path: Path):
        """3 unkeyable / 4 total = 75 % → next_tier_requested."""
        units = [
            _anchored("101"),
            _anchorless(1200.0),
            _anchorless(1300.0),
            _anchorless(1400.0),
        ]
        diff = self._diff_from_units(units, tmp_path)
        # Wrap the plain dict as a SimpleNamespace for the gate
        from types import SimpleNamespace
        diff_ns = SimpleNamespace(**diff)
        verdict = merge_yield_evaluate(diff_ns)
        assert verdict.next_tier_requested is True
        assert verdict.keyless_ratio == pytest.approx(0.75)

    def test_gate_silent_when_minority_unkeyable(self, tmp_path: Path):
        """1 unkeyable / 4 total = 25 % → no escalation."""
        units = [
            _anchored("101"),
            _anchored("102"),
            _anchored("103"),
            _anchorless(1200.0),
        ]
        diff = self._diff_from_units(units, tmp_path)
        from types import SimpleNamespace
        diff_ns = SimpleNamespace(**diff)
        verdict = merge_yield_evaluate(diff_ns)
        assert verdict.next_tier_requested is False
        assert verdict.keyless_ratio == pytest.approx(0.25)

    def test_gate_silent_on_empty_input(self, tmp_path: Path):
        diff = self._diff_from_units([], tmp_path)
        from types import SimpleNamespace
        verdict = merge_yield_evaluate(SimpleNamespace(**diff))
        assert verdict.next_tier_requested is False
        assert verdict.keyless_ratio == 0.0

    def test_gate_fires_at_boundary(self, tmp_path: Path):
        """Exactly 50 % unkeyable → NOT fired (strictly-greater-than threshold)."""
        units = [_anchored("101"), _anchorless(1300.0)]
        diff = self._diff_from_units(units, tmp_path)
        from types import SimpleNamespace
        verdict = merge_yield_evaluate(SimpleNamespace(**diff))
        assert verdict.next_tier_requested is False
        assert verdict.keyless_ratio == pytest.approx(0.5)

    def test_gate_fires_just_above_boundary(self, tmp_path: Path):
        """2/3 unkeyable = 66 % → fires."""
        units = [_anchored("101"), _anchorless(1300.0), _anchorless(1400.0)]
        diff = self._diff_from_units(units, tmp_path)
        from types import SimpleNamespace
        verdict = merge_yield_evaluate(SimpleNamespace(**diff))
        assert verdict.next_tier_requested is True


# ── carry-forward ──────────────────────────────────────────────────────────────

class TestCarryForward:
    def test_carry_forward_restores_prior_units(self, tmp_path: Path):
        ss = StateStore(tmp_path)
        ss.upsert_units("p1", [_anchored("101", 1500.0), _anchored("102", 1600.0)], "2026-05-06")
        carried = ss.carry_forward_units("p1", "2026-05-07")
        assert len(carried) == 2
        ids = {u["unit_id"] for u in carried}
        assert ids == {"101", "102"}

    def test_carry_forward_increments_carryforward_days(self, tmp_path: Path):
        ss = StateStore(tmp_path)
        ss.upsert_units("p1", [_anchored("101")], "2026-05-06")
        carried = ss.carry_forward_units("p1", "2026-05-07")
        assert carried[0]["carryforward_days"] == 1
        # Second carry-forward day
        ss.upsert_units("p1", carried, "2026-05-07")
        carried2 = ss.carry_forward_units("p1", "2026-05-08")
        assert carried2[0]["carryforward_days"] == 2

    def test_carry_forward_skips_disappeared_units(self, tmp_path: Path):
        ss = StateStore(tmp_path)
        ss.upsert_units("p1", [_anchored("101"), _anchored("102")], "2026-05-05")
        # Run without "102" for 2 consecutive days so it enters disappeared
        for day in ("2026-05-06", "2026-05-07"):
            ss.upsert_units("p1", [_anchored("101")], day)
        carried = ss.carry_forward_units("p1", "2026-05-08")
        ids = {u["unit_id"] for u in carried}
        assert "101" in ids
        assert "102" not in ids

    def test_carry_forward_empty_when_no_prior_state(self, tmp_path: Path):
        ss = StateStore(tmp_path)
        carried = ss.carry_forward_units("unknown_prop", "2026-05-07")
        assert carried == []

    def test_unkeyable_units_carry_forward(self, tmp_path: Path):
        """Rescued unkeyable_ units must also participate in carry-forward."""
        ss = StateStore(tmp_path)
        ss.upsert_units("p1", [_anchorless(1400.0)], "2026-05-06")
        carried = ss.carry_forward_units("p1", "2026-05-07")
        assert len(carried) == 1
        assert carried[0]["unit_id"].startswith("unkeyable_")
        assert carried[0]["carryforward_days"] == 1


# ── cross-run merge parity ─────────────────────────────────────────────────────

class TestCrossRunMerge:
    def test_anchored_unit_merges_across_runs(self, tmp_path: Path):
        """Same unit_id in two runs → 'updated', not duplicated."""
        ss = StateStore(tmp_path)
        ss.upsert_units("p1", [_anchored("101", 1500.0)], "2026-05-06")
        diff2 = ss.upsert_units("p1", [_anchored("101", 1600.0)], "2026-05-07")
        assert "101" in diff2["updated"]
        assert len(ss.unit_index.get("p1", {})) == 1

    def test_unkeyable_unit_deduplicates_within_same_run(self, tmp_path: Path):
        """Identical anchorless units in same run → one entry in index."""
        ss = StateStore(tmp_path)
        unit = _anchorless(1500.0)
        ss.upsert_units("p1", [unit, unit], "2026-05-07")
        assert len(ss.unit_index.get("p1", {})) == 1

    def test_run_date_updates_last_seen(self, tmp_path: Path):
        ss = StateStore(tmp_path)
        ss.upsert_units("p1", [_anchored("101")], "2026-05-06")
        ss.upsert_units("p1", [_anchored("101")], "2026-05-07")
        entry = ss.unit_index["p1"]["101"]
        assert entry["last_seen_date"] == "2026-05-07"
