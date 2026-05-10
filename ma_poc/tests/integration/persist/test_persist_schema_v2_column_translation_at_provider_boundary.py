"""Persist layer: V1 property dict keys translated at FS provider boundary.

The FS provider receives property snapshots with V1 key names ("Unique ID",
"Property Name") from the runner and translates them to the V2 canonical
fields (canonical_id, proj_name) in PropertyIndexEntry on read-back.
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
# V1 → V2 translation
# ---------------------------------------------------------------------------


def test_v1_snapshot_key_unique_id_accepted(fs: FileSystemDataProvider) -> None:
    """property_state.upsert with a V1-style snapshot using 'Unique ID' key succeeds."""
    v1_snap = {
        "Unique ID": "TRANS-001",
        "Property Name": "Grand Plaza",
        "City": "Miami",
        "Website": "https://grandplaza.example.com",
    }
    with fs.transaction():
        is_new = fs.property_state.upsert("TRANS-001", v1_snap, RUN_DATE)

    assert is_new is True
    assert fs.property_state.exists("TRANS-001")


def test_v2_canonical_id_on_readback(fs: FileSystemDataProvider) -> None:
    """The PropertyIndexEntry returned by get() always has canonical_id set."""
    v1_snap = {"Unique ID": "TRANS-002", "Property Name": "Pacific Heights"}
    with fs.transaction():
        fs.property_state.upsert("TRANS-002", v1_snap, RUN_DATE)

    entry = fs.property_state.get("TRANS-002")

    assert entry is not None
    assert entry.canonical_id == "TRANS-002"


def test_v2_proj_name_on_readback(fs: FileSystemDataProvider) -> None:
    """proj_name in PropertyIndexEntry is populated from the snapshot."""
    snap = {"proj_name": "Midtown Towers"}
    with fs.transaction():
        fs.property_state.upsert("TRANS-003", snap, RUN_DATE)

    entry = fs.property_state.get("TRANS-003")

    assert entry is not None
    assert entry.proj_name == "Midtown Towers"


def test_properties_json_preserves_v1_keys_verbatim(fs: FileSystemDataProvider) -> None:
    """properties.json written via runs.write_properties preserves V1 keys as-is.

    The runner emits both V1 and V2 keys in the run output; the FS run store
    writes the raw dict without any transformation so downstream consumers
    (TS dashboard, sync_run_to_pg) receive the exact original payload.
    """
    props = [
        {
            "Unique ID": "TRANS-004",
            "Property Name": "Bayview Apts",
            "canonical_id": "TRANS-004",
            "proj_name": "Bayview Apts",
        }
    ]
    fs.runs.write_properties(RUN_DATE, props)
    result = fs.runs.read_properties(RUN_DATE)

    assert len(result) == 1
    assert result[0].get("Unique ID") == "TRANS-004"
    assert result[0].get("canonical_id") == "TRANS-004"


def test_all_canonical_ids_includes_upserting_properties(fs: FileSystemDataProvider) -> None:
    """all_canonical_ids() lists every property that has been upserted."""
    with fs.transaction():
        for cid in ("TRANS-005", "TRANS-006", "TRANS-007"):
            fs.property_state.upsert(cid, {"proj_name": f"Prop {cid}"}, RUN_DATE)

    ids = fs.property_state.all_canonical_ids()
    assert {"TRANS-005", "TRANS-006", "TRANS-007"}.issubset(ids)
