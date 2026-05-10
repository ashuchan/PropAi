"""Persist layer: FileSystemDataProvider run artifact roundtrip.

Verifies that properties.json, issues.jsonl, and report.json written through
FileSystemDataProvider are re-read identically by the same provider.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from data_provider import FileSystemDataProvider, IssueEntry, RunReport


RUN_DATE = "2026-05-10"


@pytest.fixture
def fs(tmp_path: Path) -> FileSystemDataProvider:
    return FileSystemDataProvider(
        base_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
    )


# ---------------------------------------------------------------------------
# properties.json roundtrip
# ---------------------------------------------------------------------------


def test_write_then_read_properties_identical(fs: FileSystemDataProvider) -> None:
    """properties.json written and read back produces identical list."""
    props = [
        {"Unique ID": "P1", "Property Name": "Sunrise Lofts", "units": []},
        {"Unique ID": "P2", "Property Name": "Harbor View", "units": []},
    ]
    fs.runs.write_properties(RUN_DATE, props)
    result = fs.runs.read_properties(RUN_DATE)

    assert len(result) == 2
    assert result[0]["Unique ID"] == "P1"
    assert result[1]["Property Name"] == "Harbor View"


def test_read_properties_missing_run_returns_empty(fs: FileSystemDataProvider) -> None:
    """Reading properties for a run that was never written returns []."""
    result = fs.runs.read_properties("1970-01-01")
    assert result == []


def test_properties_overwrite_replaces_previous(fs: FileSystemDataProvider) -> None:
    """A second write_properties call overwrites the first entirely."""
    fs.runs.write_properties(RUN_DATE, [{"Unique ID": "P1"}])
    fs.runs.write_properties(RUN_DATE, [{"Unique ID": "P2"}, {"Unique ID": "P3"}])

    result = fs.runs.read_properties(RUN_DATE)
    assert len(result) == 2
    assert result[0]["Unique ID"] == "P2"


# ---------------------------------------------------------------------------
# issues.jsonl roundtrip
# ---------------------------------------------------------------------------


def test_append_then_read_issues(fs: FileSystemDataProvider) -> None:
    """Issues appended to a run are re-read as IssueEntry objects."""
    fs.runs.append_issue(RUN_DATE, IssueEntry(
        severity="ERROR",
        code="TIMEOUT",
        message="Request timed out after 30s",
        canonical_id="P1",
    ))
    fs.runs.append_issue(RUN_DATE, IssueEntry(
        severity="WARNING",
        code="LOW_UNIT_COUNT",
        message="Only 1 unit extracted; expected >= 5",
        canonical_id="P2",
    ))

    issues = list(fs.runs.read_issues(RUN_DATE))

    assert len(issues) == 2
    assert issues[0].severity == "ERROR"
    assert issues[0].canonical_id == "P1"
    assert issues[1].code == "LOW_UNIT_COUNT"


def test_read_issues_no_file_returns_empty(fs: FileSystemDataProvider) -> None:
    """Reading issues when none have been written returns an empty iterator."""
    issues = list(fs.runs.read_issues("1970-01-01"))
    assert issues == []


# ---------------------------------------------------------------------------
# report.json roundtrip
# ---------------------------------------------------------------------------


def test_write_then_read_report(fs: FileSystemDataProvider) -> None:
    """RunReport written and read back preserves key fields."""
    report = RunReport(
        run_date=RUN_DATE,
        generated_at="2026-05-10T12:00:00",
        tier_distribution={"TIER_1_API": 5, "TIER_3_DOM": 2},
    )
    fs.runs.write_report(RUN_DATE, report)
    loaded = fs.runs.read_report(RUN_DATE)

    assert loaded is not None
    assert loaded.run_date == RUN_DATE
    assert loaded.tier_distribution.get("TIER_1_API") == 5


def test_list_runs_after_write(fs: FileSystemDataProvider) -> None:
    """list_runs returns dates for all runs that have been written."""
    for date in ("2026-05-08", "2026-05-09", "2026-05-10"):
        fs.runs.write_properties(date, [{"Unique ID": "P1"}])

    runs = fs.runs.list_runs()

    assert "2026-05-08" in runs
    assert "2026-05-10" in runs
    assert len(runs) >= 3
