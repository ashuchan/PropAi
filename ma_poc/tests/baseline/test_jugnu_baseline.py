"""Tests for Jugnu J0 — baseline metrics script."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.jugnu_baseline import (
    BaselineMetrics,
    compute_failure_signatures,
    compute_llm_cost,
    compute_profile_maturity,
    compute_tier_distribution,
    compute_timing,
    compute_totals,
    find_latest_run_dir,
    load_properties_json,
    main,
    write_json,
    write_markdown,
)


def _make_prop(
    tier: str = "TIER_1_API",
    errors: list[str] | None = None,
    units: list[dict] | None = None,
    carry_forward: bool = False,
    canonical_id: str = "test_001",
) -> dict:
    """Create a minimal property dict for testing."""
    return {
        "units": units or [{"unit_id": "u1"}],
        "_meta": {
            "canonical_id": canonical_id,
            "scrape_tier_used": tier,
            "scrape_errors": errors or [],
            "carry_forward_used": carry_forward,
        },
    }


class TestFindLatestRunDir:
    def test_baseline_finds_latest_run(self, tmp_path: Path) -> None:
        """Given two date dirs, returns the latest by name sort."""
        (tmp_path / "2026-04-13").mkdir()
        (tmp_path / "2026-04-15").mkdir()
        result = find_latest_run_dir(tmp_path)
        assert result.name == "2026-04-15"

    def test_baseline_handles_empty_runs_dir(self, tmp_path: Path) -> None:
        """Empty runs directory raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="No run directories"):
            find_latest_run_dir(tmp_path)

    def test_baseline_handles_nonexistent_dir(self, tmp_path: Path) -> None:
        """Non-existent runs directory raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="does not exist"):
            find_latest_run_dir(tmp_path / "nonexistent")


class TestTierDistribution:
    def test_baseline_tier_distribution_aggregates_correctly(self) -> None:
        """Fixture with 3 TIER_1_API, 2 TIER_3_DOM, 1 FAILED."""
        props = [
            _make_prop("TIER_1_API"),
            _make_prop("TIER_1_API"),
            _make_prop("TIER_1_API"),
            _make_prop("TIER_3_DOM"),
            _make_prop("TIER_3_DOM"),
            _make_prop("FAILED"),
        ]
        dist = compute_tier_distribution(props)
        assert dist["TIER_1_API"]["count"] == 3
        assert dist["TIER_3_DOM"]["count"] == 2
        assert dist["FAILED"]["count"] == 1


class TestLlmCost:
    def test_baseline_llm_wasted_calls_counted(self, tmp_path: Path) -> None:
        """Property with empty units and LLM interactions counts as wasted."""
        props = [
            {
                "units": [],
                "_llm_interactions": [{"cost_usd": 0.001}],
                "_meta": {"canonical_id": "p001"},
            }
        ]
        result = compute_llm_cost(props, tmp_path)
        assert result["wasted_count"] == 1
        assert result["wasted_llm_calls"][0]["canonical_id"] == "p001"


class TestFailureSignatures:
    def test_baseline_failure_signature_grouping(self) -> None:
        """5 SSL errors + 3 Timeouts produce two signature groups."""
        props = []
        for i in range(5):
            props.append(
                _make_prop("FAILED", errors=["ERR_SSL_PROTOCOL_ERROR"],
                           canonical_id=f"ssl_{i}")
            )
        for i in range(3):
            props.append(
                _make_prop("FAILED", errors=["Timeout waiting for page"],
                           canonical_id=f"to_{i}")
            )
        sigs = compute_failure_signatures(props)
        assert len(sigs) == 2
        assert sigs[0]["count"] == 5  # SSL is more common
        assert sigs[1]["count"] == 3


class TestProfileMaturity:
    def test_baseline_profile_maturity_counts_null_providers(
        self, tmp_path: Path
    ) -> None:
        """3 profiles where 2 have api_provider==null."""
        for i, provider in enumerate([None, None, "entrata"]):
            profile = {
                "confidence": {"maturity": "WARM"},
                "api_hints": {"api_provider": provider},
            }
            (tmp_path / f"p{i}.json").write_text(json.dumps(profile))
        result = compute_profile_maturity(tmp_path)
        assert result["api_provider_null"] == 2
        assert result["WARM"] == 3

    def test_baseline_handles_missing_profiles_dir(
        self, tmp_path: Path
    ) -> None:
        """Non-existent profiles dir returns zeros, doesn't raise."""
        result = compute_profile_maturity(tmp_path / "missing")
        assert result["total"] == 0
        assert result["api_provider_null"] == 0


class TestOutputFiles:
    def test_baseline_writes_both_json_and_md(self, tmp_path: Path) -> None:
        """After main() with fixtures, both JSON and MD files exist."""
        runs = tmp_path / "data" / "runs" / "2026-04-17"
        runs.mkdir(parents=True)
        props = [_make_prop("TIER_1_API") for _ in range(5)]
        (runs / "properties.json").write_text(json.dumps(props))
        profiles = tmp_path / "config" / "profiles"
        profiles.mkdir(parents=True)

        baseline_dir = tmp_path / "data" / "baseline"
        docs_dir = tmp_path / "docs"

        metrics = BaselineMetrics(
            run_dir=str(runs),
            totals=compute_totals(props),
            tier_distribution=compute_tier_distribution(props),
            llm_cost=compute_llm_cost(props, runs),
            failure_signatures=compute_failure_signatures(props),
            profile_maturity=compute_profile_maturity(profiles),
            timing=compute_timing(props, runs),
            change_detection={"skip_rate_pct": 0.0},
        )
        write_json(metrics, baseline_dir / "2026-04-17.json")
        write_markdown(metrics, docs_dir / "JUGNU_BASELINE.md")

        assert (baseline_dir / "2026-04-17.json").exists()
        assert (docs_dir / "JUGNU_BASELINE.md").exists()
        md_text = (docs_dir / "JUGNU_BASELINE.md").read_text()
        assert "## 1. Totals" in md_text

    def test_baseline_is_idempotent(self, tmp_path: Path) -> None:
        """Running twice on same data produces same numbers."""
        props = [
            _make_prop("TIER_1_API", canonical_id=f"p{i}")
            for i in range(3)
        ]
        runs = tmp_path / "runs" / "2026-04-17"
        runs.mkdir(parents=True)

        totals1 = compute_totals(props)
        totals2 = compute_totals(props)
        assert totals1 == totals2

        dist1 = compute_tier_distribution(props)
        dist2 = compute_tier_distribution(props)
        assert dist1 == dist2
