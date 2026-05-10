"""Persist layer: FileSystemDataProvider transaction atomicity.

Writes inside a transaction that completes normally are persisted.
Writes inside a transaction that raises an exception are rolled back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from data_provider import FileSystemDataProvider


RUN_DATE = "2026-05-10"


@pytest.fixture
def fs(tmp_path: Path) -> FileSystemDataProvider:
    return FileSystemDataProvider(
        base_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
    )


# ---------------------------------------------------------------------------
# Successful transaction
# ---------------------------------------------------------------------------


def test_committed_transaction_persists_data(fs: FileSystemDataProvider) -> None:
    """Data written inside a completed transaction is visible after exit."""
    snap = {"proj_name": "Committed"}
    with fs.transaction():
        fs.property_state.upsert("TXN-001", snap, RUN_DATE)

    assert fs.property_state.exists("TXN-001")


def test_nested_transaction_flushes_on_outermost_exit(fs: FileSystemDataProvider) -> None:
    """Nested transactions are no-ops on the inner level; outer exit commits."""
    with fs.transaction():
        fs.property_state.upsert("TXN-002a", {}, RUN_DATE)
        with fs.transaction():  # inner — no-op
            fs.property_state.upsert("TXN-002b", {}, RUN_DATE)

    assert fs.property_state.exists("TXN-002a")
    assert fs.property_state.exists("TXN-002b")


# ---------------------------------------------------------------------------
# Failed transaction
# ---------------------------------------------------------------------------


def test_exception_in_transaction_rolls_back(fs: FileSystemDataProvider) -> None:
    """An exception inside the transaction block prevents the write from being committed."""
    try:
        with fs.transaction():
            fs.property_state.upsert("TXN-003", {"proj_name": "Should Roll Back"}, RUN_DATE)
            raise RuntimeError("Simulated mid-transaction failure")
    except RuntimeError:
        pass

    # The state index should not have been flushed — re-loading should find nothing
    fresh_fs = FileSystemDataProvider(
        base_dir=fs._base,
        config_dir=fs._config,
    )
    assert not fresh_fs.property_state.exists("TXN-003")


# ---------------------------------------------------------------------------
# Multiple writes in one transaction
# ---------------------------------------------------------------------------


def test_multiple_properties_in_one_transaction(fs: FileSystemDataProvider) -> None:
    """Several property upserts inside one transaction all land atomically."""
    with fs.transaction():
        for i in range(5):
            fs.property_state.upsert(f"TXN-BULK-{i:03d}", {"proj_name": f"Bulk {i}"}, RUN_DATE)

    for i in range(5):
        assert fs.property_state.exists(f"TXN-BULK-{i:03d}")
