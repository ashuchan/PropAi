"""Tests for `scripts/ingest_properties_csv.py`.

The ingest is a one-shot bootstrap that seeds the `properties` table
from a CSV. Re-runs must be idempotent — same CSV in, same row state
out — and existing scrape-state columns must not be clobbered.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from data_provider.sqlite import SqliteDataProvider
from scripts.ingest_properties_csv import ingest

SAMPLE_CSV = """property_id,Property Name,Website,City,State,ZIP Code,Property Type,Management Company
P001,Maple Court,https://maple.example.com,Austin,TX,78701,Stabilized,Acme PMC
P002,Oak Tower,https://oak.example.com,Austin,TX,78704,LEASE_UP,Acme PMC
P003,Pine Heights,https://pine.example.com,Dallas,TX,75201,Stabilized,Beta PMC
"""


@pytest.fixture
def provider(tmp_path: Path) -> SqliteDataProvider:
    p = SqliteDataProvider(url=f"sqlite:///{tmp_path / 'ingest.db'}")
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def csv_path(tmp_path: Path) -> Path:
    path = tmp_path / "properties.csv"
    path.write_text(SAMPLE_CSV, encoding="utf-8")
    return path


def test_first_run_inserts_all_rows(
    provider: SqliteDataProvider, csv_path: Path
) -> None:
    counts = ingest(csv_path=csv_path, provider=provider)
    assert counts == {"inserted": 3, "updated": 0, "total": 3}
    assert provider.property_state.all_canonical_ids() == {"P001", "P002", "P003"}


def test_re_run_is_idempotent(
    provider: SqliteDataProvider, csv_path: Path
) -> None:
    ingest(csv_path=csv_path, provider=provider)
    counts = ingest(csv_path=csv_path, provider=provider)
    assert counts == {"inserted": 0, "updated": 3, "total": 3}
    # Row state unchanged.
    assert provider.property_state.all_canonical_ids() == {"P001", "P002", "P003"}


def test_dry_run_does_not_write(
    provider: SqliteDataProvider, csv_path: Path
) -> None:
    counts = ingest(csv_path=csv_path, provider=provider, dry_run=True)
    assert counts == {"inserted": 3, "updated": 0, "total": 3}
    # Nothing actually written.
    assert provider.property_state.all_canonical_ids() == set()


def test_csv_changes_propagate_on_reingest(
    provider: SqliteDataProvider, tmp_path: Path
) -> None:
    """Updating a CSV cell and re-ingesting must update the row in place."""
    v1_csv = tmp_path / "v1.csv"
    v1_csv.write_text(
        "property_id,Property Name,Website\nP001,Old Name,https://old.example.com\n",
        encoding="utf-8",
    )
    ingest(csv_path=v1_csv, provider=provider)

    v2_csv = tmp_path / "v2.csv"
    v2_csv.write_text(
        "property_id,Property Name,Website\nP001,New Name,https://new.example.com\n",
        encoding="utf-8",
    )
    ingest(csv_path=v2_csv, provider=provider)

    row = provider.property_state.get("P001")
    assert row is not None
    assert row.proj_name == "New Name"
    assert row.website == "https://new.example.com"


def test_property_type_lands_in_extra_for_catalog_replay(
    provider: SqliteDataProvider, csv_path: Path
) -> None:
    """`Property Type` has no column on `properties`; it must round-trip
    via `extra` so SqlPropertyCatalogSource surfaces it as
    `property_type` on the DTO."""
    ingest(csv_path=csv_path, provider=provider)
    rows = provider.property_catalog.list_active()
    by_id = {r.canonical_id: r for r in rows}
    assert by_id["P001"].property_type == "Stabilized"
    assert by_id["P002"].property_type == "LEASE_UP"


def test_ingest_rejects_non_sql_provider(tmp_path: Path) -> None:
    """Trying to ingest into a filesystem provider must error early —
    there's no `properties` table to upsert into."""
    from data_provider.filesystem import FileSystemDataProvider

    fs = FileSystemDataProvider(
        base_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
    )
    csv = tmp_path / "p.csv"
    csv.write_text(SAMPLE_CSV, encoding="utf-8")
    try:
        with pytest.raises(RuntimeError, match="SQL DataProvider"):
            ingest(csv_path=csv, provider=fs)  # type: ignore[arg-type]
    finally:
        fs.close()
