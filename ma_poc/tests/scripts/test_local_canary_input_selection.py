"""Tests for local_canary.py — input selection phase.

Covers:
- failures.csv parsing (real analyzer output schema)
- Filter AND-composition (tier, pms, outcome)
- --include-property-id OR'd in after filtering
- --limit 0 rejected with a clear error
- Empty-result path (all rows filtered out)
- _find_failures_csv locates the right file
- write_canary_input_csv produces a jugnu-compatible CSV
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

# ── Path bootstrap (same pattern as the module under test) ────────────────────
_TESTS_DIR = Path(__file__).resolve().parent.parent
_MA_POC_ROOT = _TESTS_DIR.parent
_REPO_ROOT = _MA_POC_ROOT.parent
for _p in (_REPO_ROOT, _MA_POC_ROOT, _MA_POC_ROOT / "scripts" / "diagnostics"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import local_canary as lc  # type: ignore[import-not-found]

# ── Fixtures ──────────────────────────────────────────────────────────────────

_FIXTURE_CSV = Path(__file__).resolve().parents[1] / "fixtures" / "canary" / "failures.csv"

_THREE_ROWS = [
    {
        "property_id": "10001",
        "url": "https://example-apartments.com/floorplans",
        "verdict": "FAILED_NO_DATA",
        "terminal_tier": "TIER_4_LLM_API",
        "pms_detected": "unknown",
        "domain": "example-apartments.com",
        "fetch_outcome": "OK",
        "pattern_id": "P3",
    },
    {
        "property_id": "10002",
        "url": "https://realty-corp.com/availability",
        "verdict": "FAILED_UNREACHABLE",
        "terminal_tier": "",
        "pms_detected": "unknown",
        "domain": "realty-corp.com",
        "fetch_outcome": "HARD_FAIL",
        "pattern_id": "P7",
    },
    {
        "property_id": "10003",
        "url": "https://luxeliving.net/apartments",
        "verdict": "FAILED_NO_DATA",
        "terminal_tier": "TIER_4_LLM_DOM",
        "pms_detected": "entrata",
        "domain": "luxeliving.net",
        "fetch_outcome": "OK",
        "pattern_id": "P4",
    },
]


# ── Parser tests ──────────────────────────────────────────────────────────────


class TestReadFailuresCsv:
    def test_reads_fixture_file(self):
        """Actual fixture CSV must parse without error and yield 3 rows."""
        rows = lc._read_failures_csv(_FIXTURE_CSV)
        assert len(rows) == 3

    def test_fixture_has_required_columns(self):
        rows = lc._read_failures_csv(_FIXTURE_CSV)
        required = {"property_id", "url", "verdict", "terminal_tier", "pms_detected"}
        for row in rows:
            assert required <= set(row.keys()), f"Missing columns in row: {row}"

    def test_fixture_property_ids_are_strings(self):
        rows = lc._read_failures_csv(_FIXTURE_CSV)
        for row in rows:
            assert isinstance(row["property_id"], str)
            assert row["property_id"].strip() != ""

    def test_fixture_verdicts_are_terminal_failures(self):
        rows = lc._read_failures_csv(_FIXTURE_CSV)
        valid_verdicts = {"FAILED_NO_DATA", "FAILED_UNREACHABLE", "CARRY_FORWARD"}
        for row in rows:
            assert row["verdict"] in valid_verdicts, f"Unexpected verdict: {row['verdict']}"


# ── Filter AND-composition ────────────────────────────────────────────────────


class TestSelectProperties:
    def test_no_filters_returns_all_up_to_limit(self):
        result = lc.select_properties(_THREE_ROWS, limit=50)
        assert len(result) == 3

    def test_filter_tier_keeps_only_matching(self):
        result = lc.select_properties(_THREE_ROWS, filter_tier="TIER_4_LLM_API", limit=50)
        assert len(result) == 1
        assert result[0]["property_id"] == "10001"

    def test_filter_pms_keeps_only_matching(self):
        result = lc.select_properties(_THREE_ROWS, filter_pms="entrata", limit=50)
        assert len(result) == 1
        assert result[0]["property_id"] == "10003"

    def test_filter_outcome_keeps_only_matching(self):
        result = lc.select_properties(
            _THREE_ROWS, filter_outcome="FAILED_UNREACHABLE", limit=50
        )
        assert len(result) == 1
        assert result[0]["property_id"] == "10002"

    def test_filters_and_compose(self):
        """tier=TIER_4_LLM_DOM AND pms=entrata → only row 10003."""
        result = lc.select_properties(
            _THREE_ROWS,
            filter_tier="TIER_4_LLM_DOM",
            filter_pms="entrata",
            limit=50,
        )
        assert len(result) == 1
        assert result[0]["property_id"] == "10003"

    def test_filters_and_compose_no_match_returns_empty(self):
        result = lc.select_properties(
            _THREE_ROWS,
            filter_tier="TIER_4_LLM_API",
            filter_pms="entrata",  # entrata row has TIER_4_LLM_DOM, not API
            limit=50,
        )
        assert result == []

    def test_limit_caps_output(self):
        result = lc.select_properties(_THREE_ROWS, limit=1)
        assert len(result) == 1

    def test_limit_zero_raises(self):
        with pytest.raises(ValueError, match="limit"):
            lc.select_properties(_THREE_ROWS, limit=0)

    def test_limit_negative_raises(self):
        with pytest.raises(ValueError, match="limit"):
            lc.select_properties(_THREE_ROWS, limit=-5)

    def test_limit_larger_than_rows_returns_all(self):
        result = lc.select_properties(_THREE_ROWS, limit=1000)
        assert len(result) == 3


# ── --include-property-id OR-after-filter ────────────────────────────────────


class TestIncludePropertyId:
    def test_force_include_bypasses_tier_filter(self):
        """10002 has no tier; tier filter normally excludes it. Force-include wins."""
        result = lc.select_properties(
            _THREE_ROWS,
            filter_tier="TIER_4_LLM_API",
            include_ids=["10002"],
            limit=50,
        )
        ids = [r["property_id"] for r in result]
        assert "10002" in ids
        assert "10001" in ids  # tier filter still lets this through

    def test_force_include_deduplicates_if_already_in_filtered(self):
        """10001 passes the tier filter; including it again should not double it."""
        result = lc.select_properties(
            _THREE_ROWS,
            filter_tier="TIER_4_LLM_API",
            include_ids=["10001"],
            limit=50,
        )
        ids = [r["property_id"] for r in result]
        assert ids.count("10001") == 1

    def test_force_include_unknown_id_is_silently_ignored(self):
        """A property_id that does not exist in the source rows is skipped."""
        result = lc.select_properties(
            _THREE_ROWS,
            include_ids=["DOES_NOT_EXIST"],
            limit=50,
        )
        # Original 3 rows remain; the unknown ID adds nothing
        assert len(result) == 3
        assert all(r["property_id"] != "DOES_NOT_EXIST" for r in result)

    def test_force_include_multiple_ids(self):
        result = lc.select_properties(
            _THREE_ROWS,
            filter_outcome="FAILED_UNREACHABLE",  # only 10002 passes
            include_ids=["10001", "10003"],
            limit=50,
        )
        ids = {r["property_id"] for r in result}
        assert ids == {"10001", "10002", "10003"}


# ── _find_failures_csv ────────────────────────────────────────────────────────


class TestFindFailuresCsv:
    def test_finds_in_analyzer_out_root(self, tmp_path):
        """Simulates the analyze_cloud_run.py default output location."""
        run_date = "2026-05-10"
        out_dir = tmp_path / f"cloud_run_{run_date}"
        out_dir.mkdir()
        expected = out_dir / "failures.csv"
        expected.write_text("property_id,url\n", encoding="utf-8")

        # Patch the module-level constant so the lookup goes to tmp_path
        original = lc._ANALYZER_OUT_ROOT
        lc._ANALYZER_OUT_ROOT = tmp_path
        try:
            found = lc._find_failures_csv(run_date)
        finally:
            lc._ANALYZER_OUT_ROOT = original

        assert found == expected

    def test_returns_none_if_not_found(self):
        """A run date with no artifacts anywhere → None."""
        found = lc._find_failures_csv("1900-01-01")
        assert found is None


# ── write_canary_input_csv ────────────────────────────────────────────────────


class TestWriteCanaryInputCsv:
    def test_output_has_required_jugnu_columns(self, tmp_path):
        out_path = tmp_path / "canary_input.csv"
        lc.write_canary_input_csv(_THREE_ROWS, out_path)

        with out_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)

        assert len(rows) == 3
        for row in rows:
            assert "property_id" in row
            assert "url" in row

    def test_output_preserves_verdict_for_compare_phase(self, tmp_path):
        out_path = tmp_path / "canary_input.csv"
        lc.write_canary_input_csv(_THREE_ROWS, out_path)

        with out_path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))

        # verdict column must survive the round-trip so compare() can read it
        verdicts = {r["property_id"]: r["verdict"] for r in rows}
        assert verdicts["10001"] == "FAILED_NO_DATA"
        assert verdicts["10002"] == "FAILED_UNREACHABLE"

    def test_output_property_ids_match_input(self, tmp_path):
        out_path = tmp_path / "canary_input.csv"
        lc.write_canary_input_csv(_THREE_ROWS, out_path)

        with out_path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))

        assert {r["property_id"] for r in rows} == {"10001", "10002", "10003"}

    def test_empty_input_writes_header_only(self, tmp_path):
        out_path = tmp_path / "canary_input.csv"
        lc.write_canary_input_csv([], out_path)

        with out_path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))

        assert rows == []
