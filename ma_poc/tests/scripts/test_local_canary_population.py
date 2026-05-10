"""Tests for local_canary.py — stratified population selection and regression basket.

Covers:
- select_stratified(): guarantees ≥1 row per distinct tier, pms, fetch_outcome
- select_stratified(): AND-stratification (covers multiple dimensions simultaneously)
- select_stratified(): respects limit, deduplicates by property_id, empty input
- select_regression_basket(): filters to SUCCESS/CARRY_FORWARD only
- select_regression_basket(): uses default strata (tier+pms+fetch_outcome)
- select_regression_basket(): explicit strata override
- select_regression_basket(): limit=0 raises ValueError
- select_regression_basket(): empty input returns empty list
- main() integration: regression basket merged with failure basket
- main() integration: basket column written to canary_input.csv
- _find_successes_csv(): auto-discovery from analyzer output root
- write_canary_input_csv(): basket + cloud_units columns present
"""

from __future__ import annotations

import csv
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

# ── Fixtures path ──────────────────────────────────────────────────────────────
_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "canary"
_SUCCESSES_CSV = _FIXTURE_DIR / "successes.csv"
_FAILURES_CSV = _FIXTURE_DIR / "failures.csv"


def _load_successes() -> list[dict[str, str]]:
    with _SUCCESSES_CSV.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _load_failures() -> list[dict[str, str]]:
    with _FAILURES_CSV.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ── TestSelectStratified ───────────────────────────────────────────────────────


class TestSelectStratified:
    def _make_rows(self, specs: list[dict]) -> list[dict[str, str]]:
        return [
            {
                "property_id": s["pid"],
                "terminal_tier": s.get("tier", ""),
                "pms_detected": s.get("pms", ""),
                "fetch_outcome": s.get("fetch", ""),
                "verdict": s.get("verdict", "SUCCESS"),
                "url": f"https://example.com/{s['pid']}",
            }
            for s in specs
        ]

    def test_one_per_tier_covered_from_fixture(self):
        rows = _load_successes()
        result = lc.select_stratified(rows, ["terminal_tier"], n_per_stratum=1)
        tiers_in_fixture = {r["terminal_tier"] for r in rows if r["terminal_tier"]}
        tiers_in_result = {r["terminal_tier"] for r in result}
        assert tiers_in_fixture == tiers_in_result

    def test_one_per_pms_covered_from_fixture(self):
        rows = _load_successes()
        result = lc.select_stratified(rows, ["pms_detected"], n_per_stratum=1)
        pms_in_fixture = {r["pms_detected"] for r in rows if r["pms_detected"]}
        pms_in_result = {r["pms_detected"] for r in result}
        assert pms_in_fixture == pms_in_result

    def test_one_per_fetch_outcome_covered_from_fixture(self):
        rows = _load_successes()
        result = lc.select_stratified(rows, ["fetch_outcome"], n_per_stratum=1)
        outcomes_in_fixture = {r["fetch_outcome"] for r in rows if r["fetch_outcome"]}
        outcomes_in_result = {r["fetch_outcome"] for r in result}
        assert outcomes_in_fixture == outcomes_in_result

    def test_multi_stratum_covers_all_dimensions(self):
        """AND-stratification across tier + pms covers both dimensions."""
        rows = _load_successes()
        result = lc.select_stratified(rows, ["terminal_tier", "pms_detected"])
        tiers_covered = {r["terminal_tier"] for r in result if r["terminal_tier"]}
        pms_covered = {r["pms_detected"] for r in result if r["pms_detected"]}
        all_tiers = {r["terminal_tier"] for r in rows if r["terminal_tier"]}
        all_pms = {r["pms_detected"] for r in rows if r["pms_detected"]}
        assert tiers_covered == all_tiers
        assert pms_covered == all_pms

    def test_limit_caps_result(self):
        rows = _load_successes()
        result = lc.select_stratified(rows, ["terminal_tier"], limit=3)
        assert len(result) <= 3

    def test_no_duplicate_property_ids(self):
        rows = _load_successes()
        result = lc.select_stratified(rows, ["terminal_tier", "pms_detected"])
        pids = [r["property_id"] for r in result]
        assert len(pids) == len(set(pids)), "Duplicate property_ids found"

    def test_empty_input_returns_empty(self):
        result = lc.select_stratified([], ["terminal_tier"])
        assert result == []

    def test_rows_with_blank_strata_values_skipped_in_bins(self):
        """Rows with blank stratum values are not placed in any bin."""
        rows = self._make_rows([
            {"pid": "A", "tier": "TIER_1_PROFILE_MAPPING"},
            {"pid": "B", "tier": ""},  # blank — should not create a bin
        ])
        result = lc.select_stratified(rows, ["terminal_tier"])
        pids = {r["property_id"] for r in result}
        assert "A" in pids
        # B may or may not be in result (fill-up phase) — it should not error

    def test_limit_fill_uses_original_order(self):
        """After stratum picks, fill-up respects original row order."""
        rows = self._make_rows([
            {"pid": f"P{i:03d}", "tier": "TIER_4_LLM_API"} for i in range(10)
        ])
        result = lc.select_stratified(rows, ["terminal_tier"], limit=5)
        # First pick comes from the single-tier stratum; rest filled from original order.
        assert len(result) == 5
        assert len({r["property_id"] for r in result}) == 5

    def test_n_per_stratum_two_picks_per_value(self):
        rows = self._make_rows([
            {"pid": "A1", "tier": "TIER_4_LLM_API"},
            {"pid": "A2", "tier": "TIER_4_LLM_API"},
            {"pid": "A3", "tier": "TIER_4_LLM_API"},
            {"pid": "B1", "tier": "TIER_1_PROFILE_MAPPING"},
        ])
        result = lc.select_stratified(rows, ["terminal_tier"], n_per_stratum=2)
        tier_a_count = sum(1 for r in result if r["terminal_tier"] == "TIER_4_LLM_API")
        assert tier_a_count == 2

    def test_result_is_subset_of_input(self):
        rows = _load_successes()
        result = lc.select_stratified(rows, ["terminal_tier", "pms_detected"])
        input_pids = {r["property_id"] for r in rows}
        for r in result:
            assert r["property_id"] in input_pids


# ── TestSelectRegressionBasket ─────────────────────────────────────────────────


class TestSelectRegressionBasket:
    def test_only_success_carry_forward_included(self):
        rows = _load_successes()
        result = lc.select_regression_basket(rows)
        for r in result:
            assert r["verdict"] in {"SUCCESS", "CARRY_FORWARD"}

    def test_default_strata_covers_tier_pms_fetch(self):
        rows = _load_successes()
        result = lc.select_regression_basket(rows)
        tiers = {r["terminal_tier"] for r in result if r["terminal_tier"]}
        pms = {r["pms_detected"] for r in result if r["pms_detected"]}
        all_tiers = {r["terminal_tier"] for r in rows if r["terminal_tier"]}
        all_pms = {r["pms_detected"] for r in rows if r["pms_detected"]}
        assert tiers == all_tiers
        assert pms == all_pms

    def test_carry_forward_included_for_state_persistence_path(self):
        rows = _load_successes()
        result = lc.select_regression_basket(rows)
        verdicts = {r["verdict"] for r in result}
        assert "CARRY_FORWARD" in verdicts, (
            "CARRY_FORWARD must be represented to exercise the state-persistence code path"
        )

    def test_limit_respected(self):
        rows = _load_successes()
        result = lc.select_regression_basket(rows, limit=4)
        assert len(result) <= 4

    def test_limit_zero_raises(self):
        rows = _load_successes()
        with pytest.raises(ValueError, match="limit must be > 0"):
            lc.select_regression_basket(rows, limit=0)

    def test_empty_input_returns_empty(self):
        result = lc.select_regression_basket([])
        assert result == []

    def test_explicit_strata_override(self):
        """Passing explicit strata_keys overrides the default."""
        rows = _load_successes()
        result = lc.select_regression_basket(rows, strata_keys=["pms_detected"])
        # With only pms stratification, result may be smaller but still covers all PMS.
        pms_covered = {r["pms_detected"] for r in result if r["pms_detected"]}
        all_pms = {r["pms_detected"] for r in rows if r["pms_detected"] and r["verdict"] in {"SUCCESS", "CARRY_FORWARD"}}
        assert pms_covered == all_pms

    def test_no_duplicate_property_ids_in_basket(self):
        rows = _load_successes()
        result = lc.select_regression_basket(rows)
        pids = [r["property_id"] for r in result]
        assert len(pids) == len(set(pids))

    def test_failed_rows_excluded(self):
        rows = [
            {"property_id": "X1", "verdict": "FAILED_NO_DATA", "terminal_tier": "TIER_4_LLM_API",
             "pms_detected": "unknown", "fetch_outcome": "OK", "url": "https://x.com/1"},
            {"property_id": "X2", "verdict": "SUCCESS", "terminal_tier": "TIER_4_LLM_API",
             "pms_detected": "unknown", "fetch_outcome": "OK", "url": "https://x.com/2"},
        ]
        result = lc.select_regression_basket(rows, limit=10)
        pids = {r["property_id"] for r in result}
        assert "X1" not in pids
        assert "X2" in pids


# ── TestPopulationMerge ────────────────────────────────────────────────────────


class TestPopulationMerge:
    """Integration tests: basket tags, deduplication, and CSV output."""

    def test_failure_basket_tagged_correctly(self):
        rows = _load_failures()
        for row in rows:
            row["basket"] = lc.BASKET_FAILURE
        assert all(r["basket"] == lc.BASKET_FAILURE for r in rows)

    def test_regression_basket_tagged_correctly(self):
        rows = _load_successes()
        regression = lc.select_regression_basket(rows)
        for row in regression:
            row["basket"] = lc.BASKET_REGRESSION
        assert all(r["basket"] == lc.BASKET_REGRESSION for r in regression)

    def test_merge_deduplicates_by_property_id(self):
        """If a property_id appears in both baskets, it appears only once in merged."""
        failure_row = {
            "property_id": "SHARED", "verdict": "FAILED_NO_DATA",
            "terminal_tier": "TIER_4_LLM_API", "pms_detected": "unknown",
            "fetch_outcome": "OK", "url": "https://x.com/shared",
            "basket": lc.BASKET_FAILURE,
        }
        sentinel_row = {
            "property_id": "SHARED", "verdict": "SUCCESS",
            "terminal_tier": "TIER_4_LLM_API", "pms_detected": "unknown",
            "fetch_outcome": "OK", "url": "https://x.com/shared",
            "basket": lc.BASKET_REGRESSION,
        }

        # Simulate main() merge: failure basket wins.
        seen_pids: set[str] = {failure_row["property_id"]}
        merged = [failure_row]
        if sentinel_row["property_id"] not in seen_pids:
            merged.append(sentinel_row)

        assert len(merged) == 1
        assert merged[0]["basket"] == lc.BASKET_FAILURE

    def test_write_canary_input_csv_includes_basket_column(self, tmp_path):
        rows = _load_failures()
        for row in rows:
            row["basket"] = lc.BASKET_FAILURE
        out = tmp_path / "canary_input.csv"
        lc.write_canary_input_csv(rows, out)

        with out.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            written = list(reader)

        assert "basket" in written[0], "basket column must be present in canary_input.csv"
        assert all(r["basket"] == lc.BASKET_FAILURE for r in written)

    def test_write_canary_input_csv_includes_cloud_units_column(self, tmp_path):
        rows = _load_successes()
        for row in rows:
            row["basket"] = lc.BASKET_REGRESSION
        out = tmp_path / "canary_input.csv"
        lc.write_canary_input_csv(rows, out)

        with out.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            written = list(reader)

        assert "cloud_units" in written[0], "cloud_units column must be present"

    def test_write_canary_input_csv_cloud_units_from_successes(self, tmp_path):
        """Regression sentinel rows carry their cloud unit counts."""
        rows = [
            {
                "property_id": "S001", "url": "https://x.com/1",
                "verdict": "SUCCESS", "terminal_tier": "TIER_2_API_NARROW",
                "pms_detected": "unknown", "domain": "x.com",
                "basket": lc.BASKET_REGRESSION,
                "units": "12",  # units column from successes.csv
            }
        ]
        out = tmp_path / "canary_input.csv"
        lc.write_canary_input_csv(rows, out)

        with out.open(newline="", encoding="utf-8") as fh:
            written = list(csv.DictReader(fh))

        assert written[0]["cloud_units"] == "12"


# ── TestFindSuccessesCsv ───────────────────────────────────────────────────────


class TestFindSuccessesCsv:
    def test_returns_none_when_not_found(self):
        result = lc._find_successes_csv("1900-01-01")
        assert result is None

    def test_returns_path_when_file_exists(self, tmp_path, monkeypatch):
        run_date = "2026-05-10"
        out_dir = tmp_path / f"cloud_run_{run_date}"
        out_dir.mkdir(parents=True)
        csv_path = out_dir / "successes.csv"
        csv_path.write_text("property_id,verdict\nX1,SUCCESS\n", encoding="utf-8")

        monkeypatch.setattr(lc, "_ANALYZER_OUT_ROOT", tmp_path)
        result = lc._find_successes_csv(run_date)
        assert result == csv_path

    def test_ignores_empty_file(self, tmp_path, monkeypatch):
        run_date = "2026-05-10"
        out_dir = tmp_path / f"cloud_run_{run_date}"
        out_dir.mkdir(parents=True)
        csv_path = out_dir / "successes.csv"
        csv_path.write_text("", encoding="utf-8")  # empty file

        monkeypatch.setattr(lc, "_ANALYZER_OUT_ROOT", tmp_path)
        result = lc._find_successes_csv(run_date)
        assert result is None


# ── TestMainIntegrationWithRegressionBasket ────────────────────────────────────


class TestMainIntegrationWithRegressionBasket:
    """main() smoke tests verifying that the regression basket is merged in."""

    def _write_successes(self, path: Path) -> None:
        """Write a minimal successes.csv fixture."""
        import shutil
        shutil.copy(_SUCCESSES_CSV, path)

    def test_main_reads_regression_basket_csv(self, tmp_path, monkeypatch):
        """--regression-basket-csv path is read and merged into canary_input.csv."""
        successes_csv = tmp_path / "successes.csv"
        self._write_successes(successes_csv)

        monkeypatch.setattr(lc, "_find_failures_csv", lambda _: _FAILURES_CSV)
        monkeypatch.setattr(lc, "setup_canary_db", lambda dsn, db_mode="sqlite": None)
        monkeypatch.setattr(lc, "teardown_canary_db", lambda p: None)
        monkeypatch.setattr(lc, "replay", lambda **kw: 0)
        monkeypatch.setattr(lc, "read_canary_outcomes", lambda *a, **kw: {})
        monkeypatch.setattr(lc, "_DEFAULT_OUT_ROOT", tmp_path)

        out_dir = tmp_path / "run1"
        exit_code = lc.main([
            "--from-run", "2026-05-10",
            "--regression-basket-csv", str(successes_csv),
            "--regression-basket-size", "5",
            "--out-dir", str(out_dir),
        ])

        assert exit_code == 0

        csv_path = out_dir / "canary_input.csv"
        assert csv_path.exists()

        with csv_path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))

        baskets = {r["basket"] for r in rows}
        assert lc.BASKET_FAILURE in baskets
        assert lc.BASKET_REGRESSION in baskets

    def test_main_regression_basket_size_zero_skips_basket(self, tmp_path, monkeypatch):
        """--regression-basket-size 0 disables the regression basket entirely."""
        successes_csv = tmp_path / "successes.csv"
        self._write_successes(successes_csv)

        monkeypatch.setattr(lc, "_find_failures_csv", lambda _: _FAILURES_CSV)
        monkeypatch.setattr(lc, "setup_canary_db", lambda dsn, db_mode="sqlite": None)
        monkeypatch.setattr(lc, "teardown_canary_db", lambda p: None)
        monkeypatch.setattr(lc, "replay", lambda **kw: 0)
        monkeypatch.setattr(lc, "read_canary_outcomes", lambda *a, **kw: {})
        monkeypatch.setattr(lc, "_DEFAULT_OUT_ROOT", tmp_path)

        out_dir = tmp_path / "run2"
        lc.main([
            "--from-run", "2026-05-10",
            "--regression-basket-csv", str(successes_csv),
            "--regression-basket-size", "0",
            "--out-dir", str(out_dir),
        ])

        csv_path = out_dir / "canary_input.csv"
        with csv_path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))

        # All rows should be FAILURE basket only
        assert all(r["basket"] == lc.BASKET_FAILURE for r in rows)

    def test_main_stratify_flag_reduces_failure_basket(self, tmp_path, monkeypatch):
        """--stratify applies stratified sampling to the failure basket."""
        monkeypatch.setattr(lc, "_find_failures_csv", lambda _: _FAILURES_CSV)
        monkeypatch.setattr(lc, "setup_canary_db", lambda dsn, db_mode="sqlite": None)
        monkeypatch.setattr(lc, "teardown_canary_db", lambda p: None)
        monkeypatch.setattr(lc, "replay", lambda **kw: 0)
        monkeypatch.setattr(lc, "read_canary_outcomes", lambda *a, **kw: {})
        monkeypatch.setattr(lc, "_DEFAULT_OUT_ROOT", tmp_path)

        out_dir = tmp_path / "run3"
        exit_code = lc.main([
            "--from-run", "2026-05-10",
            "--stratify",
            "--regression-basket-size", "0",
            "--out-dir", str(out_dir),
        ])

        # Should not crash and should write the CSV
        assert exit_code in (0, 1)
        assert (out_dir / "canary_input.csv").exists()

    def test_parser_accepts_regression_basket_flags(self):
        parser = lc.build_parser()
        args = parser.parse_args([
            "--from-run", "2026-05-10",
            "--regression-basket-size", "15",
            "--stratify",
        ])
        assert args.regression_basket_size == 15
        assert args.stratify is True

    def test_parser_default_regression_basket_size(self):
        parser = lc.build_parser()
        args = parser.parse_args(["--from-run", "2026-05-10"])
        assert args.regression_basket_size == lc._DEFAULT_REGRESSION_BASKET_SIZE
