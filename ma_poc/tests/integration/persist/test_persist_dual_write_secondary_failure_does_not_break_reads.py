"""Persist layer: secondary failure in DualWriteDataProvider is swallowed.

When the secondary provider raises an exception during a write, the primary
write still succeeds and reads from the primary continue to work.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from data_provider import DualWriteDataProvider, FileSystemDataProvider, IssueEntry, RunReport


RUN_DATE = "2026-05-10"


@pytest.fixture
def fs(tmp_path: Path) -> FileSystemDataProvider:
    return FileSystemDataProvider(
        base_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
    )


def _make_failing_secondary() -> MagicMock:
    """Build a mock secondary whose every write method raises IOError."""
    secondary = MagicMock()
    secondary.property_state.upsert.side_effect = IOError("secondary disk full")
    secondary.property_state.get.side_effect = IOError("secondary disk full")
    secondary.unit_state.upsert_units.side_effect = IOError("secondary disk full")
    secondary.runs.write_properties.side_effect = IOError("secondary disk full")
    secondary.runs.append_issue.side_effect = IOError("secondary disk full")
    secondary.profiles.put.side_effect = IOError("secondary disk full")
    return secondary


# ---------------------------------------------------------------------------
# Property state
# ---------------------------------------------------------------------------


def test_property_upsert_succeeds_when_secondary_fails(fs: FileSystemDataProvider) -> None:
    """Primary write succeeds even when secondary raises IOError."""
    failing_secondary = _make_failing_secondary()
    dual = DualWriteDataProvider(primary=fs, secondary=failing_secondary)

    # Must not raise
    with dual.transaction():
        dual.property_state.upsert("FAIL-001", {"proj_name": "Resilient"}, RUN_DATE)

    assert fs.property_state.exists("FAIL-001") is True


def test_property_read_succeeds_when_secondary_fails(fs: FileSystemDataProvider) -> None:
    """Reading from primary still works after a secondary write failure."""
    failing_secondary = _make_failing_secondary()
    dual = DualWriteDataProvider(primary=fs, secondary=failing_secondary)

    with dual.transaction():
        dual.property_state.upsert("FAIL-002", {"proj_name": "Primary OK"}, RUN_DATE)

    entry = dual.property_state.get("FAIL-002")
    assert entry is not None
    assert entry.proj_name == "Primary OK"


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def test_unit_upsert_succeeds_when_secondary_fails(fs: FileSystemDataProvider) -> None:
    """Unit write goes through to primary even when secondary raises."""
    failing_secondary = _make_failing_secondary()
    dual = DualWriteDataProvider(primary=fs, secondary=failing_secondary)

    units = [{"unit_id": "U1", "bedrooms": 1, "market_rent_low": 1500}]
    with dual.transaction():
        dual.property_state.upsert("FAIL-003", {}, RUN_DATE)
        dual.unit_state.upsert_units("FAIL-003", units, RUN_DATE)

    fs_units = fs.unit_state.get_units("FAIL-003")
    assert len(fs_units) == 1


# ---------------------------------------------------------------------------
# Run artifacts
# ---------------------------------------------------------------------------


def test_properties_json_write_succeeds_when_secondary_fails(fs: FileSystemDataProvider) -> None:
    """properties.json write succeeds on primary even with secondary failure."""
    failing_secondary = _make_failing_secondary()
    dual = DualWriteDataProvider(primary=fs, secondary=failing_secondary)

    props = [{"Unique ID": "P1"}, {"Unique ID": "P2"}]
    dual.runs.write_properties(RUN_DATE, props)

    result = fs.runs.read_properties(RUN_DATE)
    assert len(result) == 2
