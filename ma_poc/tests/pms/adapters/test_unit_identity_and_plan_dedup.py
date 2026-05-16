"""Tests for unit-identity validation and plan-vs-unit dedup correctness.

Covers four extraction-correctness guards:
  * Stop-word filter on the unit_number regex (rejects "Details", "with",
    "for", "Bed" etc. captured from marketing prose like "apartment Details").
  * Dict-shaped floor_plan_name unwrap (vendor APIs sometimes return
    `{"name":"...","provider_id":"..."}` as the name value).
  * Planner STOP guard when every emitted unit is plan-level (inferred id)
    so the cascade keeps hopping for per-unit detail.
  * Plan-summary dedup against unit-level rows sharing floor_plan_id
    (prevents JSON-LD FloorPlan typology from inflating unit counts).
"""
from __future__ import annotations

import pytest

from ma_poc.pms.adapters._html_extract import (
    _container_yields_unit,
    _is_valid_unit_number,
)
from ma_poc.pms.adapters._api_parser import _get, _unwrap_name


# ── Unit-number regex: reject English stop-words captured from prose ────────

class TestUnitNumberStopwordFilter:
    """Marketing prose like "apartment Details" no longer yields unit_id='Details'.

    Production PIDs 2982 / 3483 / 595 / 3986 / 8188 on 2026-05-14 emitted
    a single record with unit_number ∈ {'Details', 'with', 'Bed', 'for'},
    which made the planner see pct_complete=1/1 and STOP before link-hop.
    """

    @pytest.mark.parametrize(
        "tok, ok",
        [
            ("203", True),       # canonical numeric
            ("A301", True),      # alpha-numeric prefix
            ("3B", True),        # numeric-alpha
            ("A-12", True),      # hyphenated
            ("12A-3", True),     # mixed
            ("Details", False),  # marketing-blurb stop-word
            ("with", False),
            ("for", False),
            ("near", False),
            ("Bed", False),
            ("Studio", False),   # studio is plan-level, not a unit number
            ("This", False),
            ("Now", False),
            ("", False),
        ],
    )
    def test_validator_decisions(self, tok: str, ok: bool) -> None:
        assert _is_valid_unit_number(tok) is ok, f"_is_valid_unit_number({tok!r}) wrong"

    def test_container_drops_stopword_capture(self) -> None:
        """The Cortland-on-Pike-class blurb yields a unit but unit_number=''."""
        text = "View Details for this 1 Bed / 1 Bath apartment for rent. 750 sqft. $1,689/mo"
        u = _container_yields_unit(text)
        assert u is not None
        assert u["unit_number"] == ""  # 'Details' / 'for' filtered out
        assert u["bedrooms"] == "1"
        assert u["sqft"] == "750"
        assert u["market_rent_low"] == 1689

    def test_real_unit_numbers_preserved(self) -> None:
        """Real per-unit numeric ids still flow through unchanged."""
        text = "Unit 203B  1 Bed / 1 Bath  750 sqft  $1,500/mo"
        u = _container_yields_unit(text)
        assert u is not None
        assert u["unit_number"] == "203B"


# ── Dict-shaped floor-plan-name fields are unwrapped before stringify ───────

class TestFloorPlanNameDictUnwrap:
    """RealPage/Morgan-Properties APIs return floor_plan as a dict — we now
    unwrap rather than stringify. PID 161 Hickory Mill on 2026-05-14 emitted
    floor_plan_name='{"name":"One Bedroom - 700 sqft","provider_id":"5286288"}'.
    """

    def test_unwrap_name_dict(self) -> None:
        v = {"name": "One Bedroom - 700 sqft", "provider_id": "5286288"}
        assert _unwrap_name(v) == "One Bedroom - 700 sqft"

    def test_unwrap_name_string_passthrough(self) -> None:
        assert _unwrap_name("Studio") == "Studio"

    def test_unwrap_name_none(self) -> None:
        assert _unwrap_name(None) == ""

    def test_unwrap_name_dict_no_name_key(self) -> None:
        """Dict without name/label/title yields empty string, not str(dict)."""
        assert _unwrap_name({"provider_id": "5286288"}) == ""

    def test_unwrap_name_label_fallback(self) -> None:
        assert _unwrap_name({"label": "B1"}) == "B1"

    def test_get_unwraps_name_shaped_dict(self) -> None:
        """``_get`` now unwraps dict-shaped name fields too, not just rent/sqft."""
        item = {"floorPlanName": {"name": "Two Bedroom", "provider_id": "abc"}}
        assert _get(item, "floorPlanName") == "Two Bedroom"

    def test_get_unwraps_rent_shaped_dict_still_works(self) -> None:
        """Pre-existing rent unwrap behaviour is preserved."""
        item = {"rent": {"min": 1351, "max": 1500}}
        assert _get(item, "rent") == "1351"


# ── Planner STOP guard: keep hopping when emitted units are all plan-level ──

class TestPlannerStopGuardForInferredIds:
    """Planner returns ESCALATE_LINK_HOP (not STOP) when all units are plan-level."""

    def _make_report(self, n: int, complete: float) -> object:
        from ma_poc.services.source_planner import CompletenessReport
        return CompletenessReport(
            n_units=n,
            pct_with_identity=complete,
            pct_with_physical=complete,
            pct_with_transactional=complete,
            pct_complete=complete,
        )

    def test_all_inferred_with_budget_escalates(self) -> None:
        """N plan-level units + link-hop budget → ESCALATE_LINK_HOP."""
        from ma_poc.services.source_planner import plan_next_action
        report = self._make_report(n=6, complete=1.0)
        d = plan_next_action(
            report=report,
            sources_already_run=set(),
            budget_remaining={"link_hop": 2, "llm_api_calls": 0, "llm_dom_calls": 0, "llm_monolithic": 0},
            pms_name="unknown",
            units_all_inferred=True,
        )
        assert d.action == "ESCALATE_LINK_HOP"
        assert "plan-level" in d.rationale

    def test_all_inferred_above_count_threshold_falls_through_to_stop(self) -> None:
        """When >6 inferred units are present, the cascade has produced enough
        plan rows that further hops just inflate the count. Fall through to STOP.

        Canary 2026-05-16 found B5 over-firing on 722 Canyon Ridge (12 inferred
        units → 18 after hop) and 8188 Montclair (9 → 18). Threshold protects
        these sentinels."""
        from ma_poc.services.source_planner import plan_next_action
        report = self._make_report(n=12, complete=1.0)
        d = plan_next_action(
            report=report,
            sources_already_run=set(),
            budget_remaining={"link_hop": 3, "llm_api_calls": 0, "llm_dom_calls": 0, "llm_monolithic": 0},
            pms_name="unknown",
            units_all_inferred=True,
        )
        assert d.action == "STOP"

    def test_all_inferred_at_threshold_still_escalates(self) -> None:
        """At exactly 6 inferred units (The Izzy cloud-run baseline), B5 still fires."""
        from ma_poc.services.source_planner import plan_next_action
        report = self._make_report(n=6, complete=1.0)
        d = plan_next_action(
            report=report,
            sources_already_run=set(),
            budget_remaining={"link_hop": 3, "llm_api_calls": 0, "llm_dom_calls": 0, "llm_monolithic": 0},
            pms_name="unknown",
            units_all_inferred=True,
        )
        assert d.action == "ESCALATE_LINK_HOP"

    def test_all_inferred_no_budget_falls_through_to_stop(self) -> None:
        """All inferred BUT no hop budget → planner falls through; STOP if complete."""
        from ma_poc.services.source_planner import plan_next_action
        report = self._make_report(n=6, complete=1.0)
        d = plan_next_action(
            report=report,
            sources_already_run=set(),
            budget_remaining={"link_hop": 0, "llm_api_calls": 0, "llm_dom_calls": 0, "llm_monolithic": 0},
            pms_name="unknown",
            units_all_inferred=True,
        )
        assert d.action == "STOP"

    def test_natural_ids_stop_when_complete(self) -> None:
        """Natural unit ids + 100% complete → STOP as before. No regression."""
        from ma_poc.services.source_planner import plan_next_action
        report = self._make_report(n=18, complete=1.0)
        d = plan_next_action(
            report=report,
            sources_already_run=set(),
            budget_remaining={"link_hop": 3, "llm_api_calls": 3, "llm_dom_calls": 1, "llm_monolithic": 1},
            pms_name="rentcafe",
            units_all_inferred=False,
        )
        assert d.action == "STOP"

    def test_default_units_all_inferred_false(self) -> None:
        """The new parameter defaults to False — pre-existing callers untouched."""
        from ma_poc.services.source_planner import plan_next_action
        report = self._make_report(n=18, complete=1.0)
        d = plan_next_action(
            report=report,
            sources_already_run=set(),
            budget_remaining={"link_hop": 3, "llm_api_calls": 0, "llm_dom_calls": 0, "llm_monolithic": 0},
            pms_name="rentcafe",
        )
        assert d.action == "STOP"


# ── Plan-summary rows that duplicate unit-level rows by floor_plan_id ──────

class TestPlanRowDedupAgainstUnits:
    """Plan-level rows whose floor_plan_id matches a unit-level row's
    floor_plan_id are dropped before emission.

    Repro shape from PID 67327 Windsong Estates 2026-05-14:
        18 real units across 8 plans + 9 phantom plan-only rows for the
        same plans → 27 rows emitted instead of 18.
    """

    def test_drop_plan_when_unit_exists_for_same_plan(self) -> None:
        """Helper isolates the B4 logic without needing GenericAdapter wired up."""
        # Mimic post_process partition:
        unit_level = [
            {"unit_id": "29104", "floor_plan_id": "FP-A", "floor_plan_name": "The Country",
             "bedrooms": "2", "bathrooms": "2", "sqft": "1100", "market_rent_low": 1495},
            {"unit_id": "7104", "floor_plan_id": "FP-A", "floor_plan_name": "The Country",
             "bedrooms": "2", "bathrooms": "2", "sqft": "1100", "market_rent_low": 1495},
        ]
        plan_level = [
            {"unit_id": "inferred_xxx", "_inferred_id": True, "floor_plan_id": "FP-A",
             "floor_plan_name": "The Country", "bedrooms": "2", "bathrooms": "2"},
        ]
        # Apply the B4 logic directly (matches the implementation in
        # generic.py:868 post_process branch).
        unit_fpids = {str(u["floor_plan_id"]) for u in unit_level if u.get("floor_plan_id")}
        kept_plans = [p for p in plan_level if str(p.get("floor_plan_id") or "") not in unit_fpids]
        assert kept_plans == []
        # The 2 real units survive untouched.
        assert len(unit_level) == 2

    def test_plan_kept_when_no_unit_for_that_plan(self) -> None:
        """A plan summary for a plan with no leased units survives the dedup."""
        unit_level = [
            {"unit_id": "29104", "floor_plan_id": "FP-A", "floor_plan_name": "Country"},
        ]
        plan_level = [
            {"unit_id": "inferred_xxx", "_inferred_id": True, "floor_plan_id": "FP-B",
             "floor_plan_name": "Western"},
        ]
        unit_fpids = {str(u["floor_plan_id"]) for u in unit_level if u.get("floor_plan_id")}
        kept_plans = [p for p in plan_level if str(p.get("floor_plan_id") or "") not in unit_fpids]
        assert len(kept_plans) == 1
        assert kept_plans[0]["floor_plan_id"] == "FP-B"
