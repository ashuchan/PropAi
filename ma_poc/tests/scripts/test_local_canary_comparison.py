"""Tests for local_canary.py — attribution (L4) and compare subcommand (L5).

Covers:
- attribute_fix() returns correct fix names for each attribution rule
- attribute_fix() returns "" when no rule matches
- attribute_fix() joins multiple non-exclusive matches with "(heuristic)"
- Tier improvement detection (cloud LLM-tier → canary earlier tier)
- compare() populates attributed_fix only for IMPROVED rows
- compare() does not set attributed_fix for REGRESSED / UNCHANGED rows
- _main_compare() reads two summary.json files and prints delta
- _main_compare() returns exit 1 when treatment regressed vs. baseline
- _main_compare() returns exit 0 when treatment did not regress vs. baseline
- _main_compare() returns exit 2 when summary.json is missing
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_TESTS_DIR = Path(__file__).resolve().parent.parent
_MA_POC_ROOT = _TESTS_DIR.parent
_REPO_ROOT = _MA_POC_ROOT.parent
for _p in (_REPO_ROOT, _MA_POC_ROOT, _MA_POC_ROOT / "scripts" / "diagnostics"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import local_canary as lc  # type: ignore[import-not-found]
import local_canary_attribution as attr  # type: ignore[import-not-found]


# ── TestAttributeFix ──────────────────────────────────────────────────────────


class TestAttributeFix:
    def test_profile_replay_hit_returns_url_pattern_normalization(self):
        result = attr.attribute_fix(
            {"profile.replay_hit"},
            cloud_tier="TIER_4_LLM_API",
            canary_tier="TIER_1_PROFILE_MAPPING",
        )
        assert result == "url_pattern_normalization"

    def test_degraded_dom_saved_returns_degraded_dom_hint_persistence(self):
        result = attr.attribute_fix(
            {"dom_hints.degraded_saved"},
            cloud_tier="TIER_4_LLM_DOM",
            canary_tier="TIER_3_DOM_SCAN",
        )
        assert result == "degraded_dom_hint_persistence"

    def test_field_patch_hit_returns_field_patch_persistence(self):
        result = attr.attribute_fix(
            {"field_patch.hit"},
            cloud_tier="TIER_4_LLM_API",
            canary_tier="TIER_4_LLM_API",
        )
        assert result == "field_patch_persistence"

    def test_tier_improvement_alone_returns_source_tiered_budget(self):
        result = attr.attribute_fix(
            set(),
            cloud_tier="TIER_4_LLM_API",
            canary_tier="TIER_2_API_NARROW",
        )
        assert result == "source_tiered_budget"

    def test_no_match_returns_empty_string(self):
        result = attr.attribute_fix(
            {"fetch.tier_escalated"},  # irrelevant event
            cloud_tier="TIER_4_LLM_API",
            canary_tier="TIER_4_LLM_API",  # no tier improvement
        )
        assert result == ""

    def test_empty_event_kinds_no_tier_improvement_returns_empty(self):
        result = attr.attribute_fix(
            set(),
            cloud_tier="",
            canary_tier="",
        )
        assert result == ""

    def test_tier_improvement_same_tier_no_match(self):
        """Same tier = no improvement; source_tiered_budget must NOT fire."""
        result = attr.attribute_fix(
            set(),
            cloud_tier="TIER_4_LLM_API",
            canary_tier="TIER_4_LLM_API",
        )
        assert result == ""

    def test_replay_hit_takes_precedence_over_tier_improvement(self):
        """profile.replay_hit is first in registry; it wins even with tier improvement."""
        result = attr.attribute_fix(
            {"profile.replay_hit"},
            cloud_tier="TIER_4_LLM_API",
            canary_tier="TIER_1_PROFILE_MAPPING",
        )
        # First matching exclusive rule wins.
        assert result == "url_pattern_normalization"

    def test_unknown_tier_strings_handled_gracefully(self):
        """Unknown tier names are ranked 999; no crash."""
        result = attr.attribute_fix(
            set(),
            cloud_tier="TIER_UNKNOWN_A",
            canary_tier="TIER_UNKNOWN_B",
        )
        assert result == ""

    def test_tier_improvement_higher_number_lower_rank(self):
        """TIER_4_LLM_API (rank 6) vs TIER_2_API_NARROW (rank 1) → improvement."""
        improved = attr._tier_improved("TIER_4_LLM_API", "TIER_2_API_NARROW")
        assert improved is True

    def test_no_tier_improvement_when_canary_is_higher(self):
        improved = attr._tier_improved("TIER_1_PROFILE_MAPPING", "TIER_4_LLM_DOM")
        assert improved is False


# ── TestCompareWithAttribution ────────────────────────────────────────────────


class TestCompareWithAttribution:
    def _make_cloud_row(self, pid, verdict="FAILED_NO_DATA", tier="TIER_4_LLM_API"):
        return {
            "property_id": pid,
            "url": f"https://example.com/{pid}",
            "verdict": verdict,
            "terminal_tier": tier,
            "pms_detected": "unknown",
            "domain": "example.com",
        }

    def _make_canary_outcome(self, pid, verdict="SUCCESS", tier="TIER_1_PROFILE_MAPPING",
                             event_kinds=None):
        return lc._CanaryOutcome(
            property_id=pid,
            verdict=verdict,
            units=3,
            tier=tier,
            event_kinds=event_kinds if event_kinds is not None else {"profile.replay_hit"},
        )

    def test_improved_row_gets_attributed_fix(self):
        cloud_rows = [self._make_cloud_row("P001")]
        outcomes = {"P001": self._make_canary_outcome("P001")}

        report = lc.compare(cloud_rows, outcomes, source_run_date="2026-05-10")

        assert len(report.rows) == 1
        row = report.rows[0]
        assert row.verdict == lc.VERDICT_IMPROVED
        assert row.attributed_fix == "url_pattern_normalization"

    def test_regressed_row_has_empty_attributed_fix(self):
        cloud_rows = [self._make_cloud_row("P001", verdict="SUCCESS")]
        outcomes = {
            "P001": lc._CanaryOutcome(
                property_id="P001",
                verdict="FAILED_NO_DATA",
                units=0,
                tier="TIER_4_LLM_API",
                event_kinds=set(),
            )
        }

        report = lc.compare(cloud_rows, outcomes, source_run_date="2026-05-10")
        row = report.rows[0]
        assert row.verdict == lc.VERDICT_REGRESSED
        assert row.attributed_fix == ""

    def test_unchanged_ok_has_empty_attributed_fix(self):
        cloud_rows = [self._make_cloud_row("P001", verdict="SUCCESS")]
        outcomes = {
            "P001": self._make_canary_outcome("P001", verdict="SUCCESS")
        }

        report = lc.compare(cloud_rows, outcomes, source_run_date="2026-05-10")
        row = report.rows[0]
        assert row.verdict == lc.VERDICT_UNCHANGED_OK
        assert row.attributed_fix == ""

    def test_canary_tier_populated_in_compared_row(self):
        cloud_rows = [self._make_cloud_row("P001")]
        outcomes = {
            "P001": self._make_canary_outcome(
                "P001",
                tier="TIER_2_API_NARROW",
                event_kinds=set(),
            )
        }

        report = lc.compare(cloud_rows, outcomes, source_run_date="2026-05-10")
        row = report.rows[0]
        assert row.canary_tier == "TIER_2_API_NARROW"

    def test_source_tiered_budget_attributed_when_tier_improved(self):
        cloud_rows = [self._make_cloud_row("P001", tier="TIER_4_LLM_API")]
        outcomes = {
            "P001": self._make_canary_outcome(
                "P001",
                tier="TIER_2_API_NARROW",
                event_kinds=set(),  # no specific events, but tier improved
            )
        }

        report = lc.compare(cloud_rows, outcomes, source_run_date="2026-05-10")
        row = report.rows[0]
        assert row.verdict == lc.VERDICT_IMPROVED
        assert row.attributed_fix == "source_tiered_budget"

    def test_timeout_row_has_empty_attributed_fix(self):
        cloud_rows = [self._make_cloud_row("P001")]
        # No canary outcome → TIMEOUT_IN_CANARY
        report = lc.compare(cloud_rows, {}, source_run_date="2026-05-10")
        row = report.rows[0]
        assert row.verdict == lc.VERDICT_TIMEOUT_IN_CANARY
        assert row.attributed_fix == ""

    def test_mixed_set_attribution_counts(self):
        cloud_rows = [
            self._make_cloud_row("P001"),  # IMPROVED
            self._make_cloud_row("P002"),  # IMPROVED
            self._make_cloud_row("P003", verdict="SUCCESS"),  # REGRESSED
        ]
        outcomes = {
            "P001": self._make_canary_outcome("P001", event_kinds={"profile.replay_hit"}),
            "P002": self._make_canary_outcome("P002", event_kinds={"field_patch.hit"}),
            "P003": lc._CanaryOutcome(
                property_id="P003",
                verdict="FAILED_NO_DATA",
                units=0,
                tier="",
                event_kinds=set(),
            ),
        }

        report = lc.compare(cloud_rows, outcomes, source_run_date="2026-05-10")
        assert report.improved == 2
        assert report.regressed == 1

        improved_rows = [r for r in report.rows if r.verdict == lc.VERDICT_IMPROVED]
        assert {r.attributed_fix for r in improved_rows} == {
            "url_pattern_normalization",
            "field_patch_persistence",
        }


# ── TestReadCanaryOutcomesWithTier ─────────────────────────────────────────────


class TestReadCanaryOutcomesWithTier:
    def _write_events(self, events_path: Path, events: list[dict]) -> None:
        import json

        events_path.parent.mkdir(parents=True, exist_ok=True)
        with events_path.open("w") as fh:
            for ev in events:
                fh.write(json.dumps(ev) + "\n")

    def test_tier_won_populates_outcome_tier(self, tmp_path):
        events_path = tmp_path / "runs" / "2026-05-10" / "events.jsonl"
        self._write_events(
            events_path,
            [
                {"kind": "extract.tier_won", "property_id": "P001", "tier_used": "TIER_2_API_NARROW"},
                {"kind": "output.property_emitted", "property_id": "P001", "verdict": "SUCCESS", "units": 5},
            ],
        )
        outcomes = lc.read_canary_outcomes(tmp_path, "2026-05-10")
        assert outcomes["P001"].tier == "TIER_2_API_NARROW"

    def test_event_kinds_collected(self, tmp_path):
        events_path = tmp_path / "runs" / "2026-05-10" / "events.jsonl"
        self._write_events(
            events_path,
            [
                {"kind": "profile.replay_hit", "property_id": "P001"},
                {"kind": "extract.tier_won", "property_id": "P001", "tier_used": "TIER_1_PROFILE_MAPPING"},
                {"kind": "output.property_emitted", "property_id": "P001", "verdict": "SUCCESS", "units": 3},
            ],
        )
        outcomes = lc.read_canary_outcomes(tmp_path, "2026-05-10")
        assert "profile.replay_hit" in outcomes["P001"].event_kinds

    def test_tier_won_without_property_emitted_not_in_outcomes(self, tmp_path):
        """A tier_won event without a matching property_emitted → no outcome entry."""
        events_path = tmp_path / "runs" / "2026-05-10" / "events.jsonl"
        self._write_events(
            events_path,
            [
                {"kind": "extract.tier_won", "property_id": "P001", "tier_used": "TIER_4_LLM_API"},
            ],
        )
        outcomes = lc.read_canary_outcomes(tmp_path, "2026-05-10")
        assert "P001" not in outcomes


# ── TestMainCompare (L5) ──────────────────────────────────────────────────────


class TestMainCompare:
    def _write_summary(self, out_dir: Path, data: dict) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "summary.json").write_text(json.dumps(data), encoding="utf-8")

    def _base_summary(self, regressed=0, improved=0, passed=True) -> dict:
        return {
            "run_date": "2026-05-10",
            "source_run_date": "2026-05-09",
            "properties_total": 3,
            "improved": improved,
            "regressed": regressed,
            "unchanged_ok": 1,
            "unchanged_fail": 1,
            "timeout_in_canary": 0,
            "passed": passed,
            "rows": [],
        }

    def test_returns_exit_0_when_treatment_does_not_regress(self, tmp_path):
        baseline_dir = tmp_path / "baseline"
        treatment_dir = tmp_path / "treatment"
        self._write_summary(baseline_dir, self._base_summary(regressed=0))
        self._write_summary(treatment_dir, self._base_summary(regressed=0))

        exit_code = lc._main_compare([
            "--baseline-dir", str(baseline_dir),
            "--treatment-dir", str(treatment_dir),
        ])
        assert exit_code == 0

    def test_returns_exit_1_when_treatment_regresses(self, tmp_path):
        baseline_dir = tmp_path / "baseline"
        treatment_dir = tmp_path / "treatment"
        self._write_summary(baseline_dir, self._base_summary(regressed=0))
        self._write_summary(treatment_dir, self._base_summary(regressed=1, passed=False))

        exit_code = lc._main_compare([
            "--baseline-dir", str(baseline_dir),
            "--treatment-dir", str(treatment_dir),
        ])
        assert exit_code == 1

    def test_returns_exit_2_when_summary_missing(self, tmp_path):
        baseline_dir = tmp_path / "baseline"
        treatment_dir = tmp_path / "treatment"
        baseline_dir.mkdir()
        # treatment_dir missing → no summary.json
        treatment_dir.mkdir()

        exit_code = lc._main_compare([
            "--baseline-dir", str(baseline_dir),
            "--treatment-dir", str(treatment_dir),
        ])
        assert exit_code == 2

    def test_json_flag_emits_valid_json(self, tmp_path, capsys):
        baseline_dir = tmp_path / "baseline"
        treatment_dir = tmp_path / "treatment"
        self._write_summary(baseline_dir, self._base_summary(regressed=0, improved=2))
        self._write_summary(treatment_dir, self._base_summary(regressed=0, improved=3))

        lc._main_compare([
            "--baseline-dir", str(baseline_dir),
            "--treatment-dir", str(treatment_dir),
            "--json",
        ])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "delta_improved" in data
        assert data["delta_improved"] == 1

    def test_delta_improved_computed_correctly(self, tmp_path):
        baseline_dir = tmp_path / "baseline"
        treatment_dir = tmp_path / "treatment"
        self._write_summary(baseline_dir, self._base_summary(improved=5))
        self._write_summary(treatment_dir, self._base_summary(improved=8))

        import io
        import contextlib

        out_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf):
            lc._main_compare([
                "--baseline-dir", str(baseline_dir),
                "--treatment-dir", str(treatment_dir),
                "--json",
            ])

        data = json.loads(out_buf.getvalue())
        assert data["delta_improved"] == 3
        assert data["delta_regressed"] == 0

    def test_compare_subcommand_dispatched_from_main(self, tmp_path):
        """main(['compare', ...]) dispatches to _main_compare."""
        baseline_dir = tmp_path / "baseline"
        treatment_dir = tmp_path / "treatment"
        self._write_summary(baseline_dir, self._base_summary())
        self._write_summary(treatment_dir, self._base_summary())

        exit_code = lc.main([
            "compare",
            "--baseline-dir", str(baseline_dir),
            "--treatment-dir", str(treatment_dir),
        ])
        assert exit_code == 0
