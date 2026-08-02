"""Contract tests for IPropertyCatalogSource.

Both implementations (CSV-backed via FileSystemDataProvider, SQL-backed
via SqliteDataProvider) must satisfy the same contract: stable order,
filter semantics, shard math, count_active matching list_active length.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from data_provider import DataProvider
from data_provider.dtos import CatalogFilters, PropertyToScrape
from data_provider.filesystem import (
    CsvPropertyCatalogSource,
    FileSystemDataProvider,
    hydrate_property_catalog_rows,
)
from data_provider.postgres import PostgresDataProvider
from data_provider.sqlite import SqliteDataProvider

SAMPLE_CSV = """property_id,Property Name,Website,City,State,ZIP Code,Property Type,Management Company
P001,Maple Court,https://maple.example.com,Austin,TX,78701,Stabilized,Acme PMC
P002,Oak Tower,https://oak.example.com,Austin,TX,78704,LEASE_UP,Acme PMC
P003,Pine Heights,https://pine.example.com,Dallas,TX,75201,Stabilized,Beta PMC
P004,Cedar Park,https://cedar.example.com,Houston,TX,77002,Stabilized,Beta PMC
P005,Birch Place,https://birch.example.com,Dallas,TX,75205,LEASE_UP,Acme PMC
"""


def _write_csv(tmp_path: Path, content: str = SAMPLE_CSV) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    csv_path = config_dir / "properties.csv"
    csv_path.write_text(content, encoding="utf-8")
    return csv_path


def _seed_sql_catalog(provider: DataProvider, rows: list[dict[str, str]]) -> None:
    """Write rows directly to the `properties` table via the state store
    upsert. Avoids importing the ingest script (which is exercised by its
    own tests)."""
    for r in rows:
        snapshot = {
            "proj_name": r["Property Name"],
            "website": r["Website"],
            "city": r["City"],
            "state": r["State"],
            "zip_code": r["ZIP Code"],
            "pmc": r["Management Company"],
            # `property_type` isn't a column — goes via extra. The state
            # store doesn't accept extra explicitly, so put it in the
            # snapshot dict and the store's _split_known_extra() will
            # route it correctly.
            "property_type": r["Property Type"],
        }
        provider.property_state.upsert(r["property_id"], snapshot, "2026-05-08")


# ── Provider fixtures ────────────────────────────────────────────────────────


def _fs_provider_with_csv(tmp_path: Path) -> FileSystemDataProvider:
    csv_path = _write_csv(tmp_path)
    return FileSystemDataProvider(
        base_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        catalog_csv_path=csv_path,
    )


def _sqlite_provider_with_seed(tmp_path: Path) -> SqliteDataProvider:
    url = f"sqlite:///{tmp_path / 'catalog.db'}"
    p = SqliteDataProvider(url=url)
    rows = [
        {
            "property_id": "P001",
            "Property Name": "Maple Court",
            "Website": "https://maple.example.com",
            "City": "Austin",
            "State": "TX",
            "ZIP Code": "78701",
            "Property Type": "Stabilized",
            "Management Company": "Acme PMC",
        },
        {
            "property_id": "P002",
            "Property Name": "Oak Tower",
            "Website": "https://oak.example.com",
            "City": "Austin",
            "State": "TX",
            "ZIP Code": "78704",
            "Property Type": "LEASE_UP",
            "Management Company": "Acme PMC",
        },
        {
            "property_id": "P003",
            "Property Name": "Pine Heights",
            "Website": "https://pine.example.com",
            "City": "Dallas",
            "State": "TX",
            "ZIP Code": "75201",
            "Property Type": "Stabilized",
            "Management Company": "Beta PMC",
        },
        {
            "property_id": "P004",
            "Property Name": "Cedar Park",
            "Website": "https://cedar.example.com",
            "City": "Houston",
            "State": "TX",
            "ZIP Code": "77002",
            "Property Type": "Stabilized",
            "Management Company": "Beta PMC",
        },
        {
            "property_id": "P005",
            "Property Name": "Birch Place",
            "Website": "https://birch.example.com",
            "City": "Dallas",
            "State": "TX",
            "ZIP Code": "75205",
            "Property Type": "LEASE_UP",
            "Management Company": "Acme PMC",
        },
    ]
    _seed_sql_catalog(p, rows)
    return p


@pytest.fixture
def provider(request, tmp_path) -> DataProvider:
    kind = request.param
    if kind == "filesystem":
        p: DataProvider = _fs_provider_with_csv(tmp_path)
    elif kind == "sqlite":
        p = _sqlite_provider_with_seed(tmp_path)
    elif kind == "postgres":
        url = os.getenv("TEST_DATABASE_URL")
        if not url:
            pytest.skip("Set TEST_DATABASE_URL to run against real Postgres")
        p = PostgresDataProvider(url=url)
    else:  # pragma: no cover
        raise ValueError(f"Unknown provider kind: {kind}")
    try:
        yield p
    finally:
        p.close()


providers = pytest.mark.parametrize("provider", ["filesystem", "sqlite", "postgres"], indirect=True)


# ── Contract tests (parametrised over every provider) ───────────────────────


@providers
def test_list_active_returns_all_rows(provider: DataProvider) -> None:
    rows = provider.property_catalog.list_active()
    assert len(rows) == 5
    assert all(isinstance(r, PropertyToScrape) for r in rows)
    ids = {r.canonical_id for r in rows}
    assert ids == {"P001", "P002", "P003", "P004", "P005"}


@providers
def test_count_matches_list_length(provider: DataProvider) -> None:
    assert provider.property_catalog.count_active() == len(provider.property_catalog.list_active())


@providers
def test_limit_is_applied(provider: DataProvider) -> None:
    rows = provider.property_catalog.list_active(limit=2)
    assert len(rows) == 2


@providers
def test_filter_by_canonical_ids(provider: DataProvider) -> None:
    filters = CatalogFilters(canonical_ids=["P001", "P003"])
    rows = provider.property_catalog.list_active(filters=filters)
    assert {r.canonical_id for r in rows} == {"P001", "P003"}


@providers
def test_filter_by_property_type(provider: DataProvider) -> None:
    filters = CatalogFilters(property_types=["LEASE_UP"])
    rows = provider.property_catalog.list_active(filters=filters)
    assert {r.canonical_id for r in rows} == {"P002", "P005"}


@providers
def test_shard_partition_is_disjoint_and_complete(provider: DataProvider) -> None:
    """Every catalog row appears in exactly one shard; union covers all."""
    shard_count = 3
    union: set[str] = set()
    sizes: list[int] = []
    for idx in range(shard_count):
        filters = CatalogFilters(shard_index=idx, shard_count=shard_count)
        rows = provider.property_catalog.list_active(filters=filters)
        ids = {r.canonical_id for r in rows}
        # Disjoint
        assert union.isdisjoint(ids), f"shard {idx} overlaps prior shards"
        union.update(ids)
        sizes.append(len(rows))
    assert union == {"P001", "P002", "P003", "P004", "P005"}
    assert sum(sizes) == 5


@providers
def test_count_with_shard_matches_list_with_shard(provider: DataProvider) -> None:
    for idx in range(3):
        filters = CatalogFilters(shard_index=idx, shard_count=3)
        assert provider.property_catalog.count_active(filters=filters) == len(
            provider.property_catalog.list_active(filters=filters)
        )


@providers
def test_invalid_shard_returns_empty(provider: DataProvider) -> None:
    # shard_index >= shard_count is out-of-range and yields no rows.
    filters = CatalogFilters(shard_index=5, shard_count=3)
    assert provider.property_catalog.list_active(filters=filters) == []
    assert provider.property_catalog.count_active(filters=filters) == 0


@providers
def test_stable_order_across_calls(provider: DataProvider) -> None:
    """Same inputs → same row order. Required for shard determinism."""
    a = [r.canonical_id for r in provider.property_catalog.list_active()]
    b = [r.canonical_id for r in provider.property_catalog.list_active()]
    assert a == b


@providers
def test_order_is_canonical_id_ascending(provider: DataProvider) -> None:
    """Both sources MUST sort by canonical_id ascending so a given
    (shard_index, shard_count) picks the same rows regardless of which
    backend the runner is reading from. Without this guarantee, dev runs
    against CSV and prod runs against DB can disagree on shard membership.
    """
    ids = [r.canonical_id for r in provider.property_catalog.list_active()]
    assert ids == sorted(ids)


def test_csv_and_sql_sources_yield_identical_order(tmp_path: Path) -> None:
    """Cross-source order check: a CSV authored in shuffled order and a
    DB seeded from that same CSV must produce the same canonical_id
    sequence under list_active(). This is the property the shard
    contract relies on."""
    # Shuffled-on-disk CSV — authoring order is intentionally NOT sorted.
    shuffled = (
        "property_id,Property Name,Website\n"
        "P003,Pine Heights,https://pine.example.com\n"
        "P001,Maple Court,https://maple.example.com\n"
        "P005,Birch Place,https://birch.example.com\n"
        "P002,Oak Tower,https://oak.example.com\n"
        "P004,Cedar Park,https://cedar.example.com\n"
    )
    csv_path = tmp_path / "shuffled.csv"
    csv_path.write_text(shuffled, encoding="utf-8")

    csv_source = CsvPropertyCatalogSource(csv_path)
    csv_ids = [r.canonical_id for r in csv_source.list_active()]
    assert csv_ids == ["P001", "P002", "P003", "P004", "P005"]

    # Seed the SQL source with the same rows; verify identical order.
    sql_provider = SqliteDataProvider(url=f"sqlite:///{tmp_path / 'sql.db'}")
    try:
        for cid, name, url in [
            ("P003", "Pine Heights", "https://pine.example.com"),
            ("P001", "Maple Court", "https://maple.example.com"),
            ("P005", "Birch Place", "https://birch.example.com"),
            ("P002", "Oak Tower", "https://oak.example.com"),
            ("P004", "Cedar Park", "https://cedar.example.com"),
        ]:
            sql_provider.property_state.upsert(cid, {"proj_name": name, "website": url}, "2026-05-08")
        sql_ids = [r.canonical_id for r in sql_provider.property_catalog.list_active()]
    finally:
        sql_provider.close()

    assert csv_ids == sql_ids


# ── CSV-source-specific tests (no DB equivalent) ────────────────────────────


def test_csv_source_handles_bom_prefix(tmp_path: Path) -> None:
    """utf-8-sig must strip the BOM so 'property_id' parses as a column."""
    bom_csv = "﻿" + SAMPLE_CSV
    csv_path = tmp_path / "bom.csv"
    csv_path.write_text(bom_csv, encoding="utf-8")
    source = CsvPropertyCatalogSource(csv_path)
    rows = source.list_active()
    assert len(rows) == 5
    assert rows[0].canonical_id == "P001"


def test_csv_source_missing_file_returns_empty(tmp_path: Path) -> None:
    """A non-existent CSV is treated as an empty catalog, not an error."""
    source = CsvPropertyCatalogSource(tmp_path / "absent.csv")
    assert source.list_active() == []
    assert source.count_active() == 0


def test_csv_source_synthesises_id_when_missing(tmp_path: Path) -> None:
    """Rows missing every id alias get a `row_<i>` fallback."""
    csv_path = tmp_path / "noid.csv"
    csv_path.write_text(
        "Property Name,Website\nFoo,https://foo.example.com\nBar,https://bar.example.com\n",
        encoding="utf-8",
    )
    source = CsvPropertyCatalogSource(csv_path)
    rows = source.list_active()
    assert [r.canonical_id for r in rows] == ["row_0", "row_1"]


def test_focused_cohort_rows_are_hydrated_from_canonical_catalog(tmp_path: Path) -> None:
    cohort_path = tmp_path / "cohort.csv"
    cohort_path.write_text(
        "property_id,website\n22986,https://current.example.com/\n",
        encoding="utf-8",
    )
    canonical_path = tmp_path / "canonical.csv"
    canonical_path.write_text(
        "apartmentid,name,address,city,state,zip,website\n"
        "22986,Tuscany Hills,715 Ash Ln,San Marcos,CA,92069,http://legacy.example.com/\n",
        encoding="utf-8",
    )
    cohort = CsvPropertyCatalogSource(cohort_path).list_active()
    canonical = CsvPropertyCatalogSource(canonical_path).list_active()

    [hydrated] = hydrate_property_catalog_rows(cohort, canonical)
    flat = hydrated.as_csv_row()

    assert hydrated.canonical_id == "22986"
    assert hydrated.proj_name == "Tuscany Hills"
    assert hydrated.address == "715 Ash Ln"
    assert hydrated.city == "San Marcos"
    assert hydrated.state == "CA"
    assert hydrated.zip_code == "92069"
    assert hydrated.apartment_id == 22986
    # Explicit cohort URL remains authoritative while missing identity aliases
    # are supplied from the canonical row.
    assert hydrated.website == "https://current.example.com/"
    assert flat["Property Name"] == "Tuscany Hills"
    assert flat["Address"] == "715 Ash Ln"


def test_catalog_hydration_never_overwrites_explicit_cohort_identity() -> None:
    cohort = PropertyToScrape(
        canonical_id="7",
        url="https://cohort.example.com",
        proj_name="Cohort Name",
        address="7 Current St",
        raw={"name": "Cohort Name", "address": "7 Current St"},
    )
    canonical = PropertyToScrape(
        canonical_id="7",
        url="https://canonical.example.com",
        proj_name="Canonical Name",
        address="7 Old St",
        city="Austin",
        raw={"name": "Canonical Name", "address": "7 Old St", "city": "Austin"},
    )

    [hydrated] = hydrate_property_catalog_rows([cohort], [canonical])

    assert hydrated.url == "https://cohort.example.com"
    assert hydrated.proj_name == "Cohort Name"
    assert hydrated.address == "7 Current St"
    assert hydrated.city == "Austin"
    assert hydrated.raw["name"] == "Cohort Name"
    assert hydrated.raw["address"] == "7 Current St"


def test_as_csv_row_populates_both_v2_and_titlecase_aliases(tmp_path: Path) -> None:
    """The runner's _csv() fallbacks must hit on either alias style."""
    csv_path = _write_csv(tmp_path)
    source = CsvPropertyCatalogSource(csv_path)
    [first, *_] = source.list_active()
    flat = first.as_csv_row()
    # Original CSV keys preserved.
    assert flat["Property Name"] == "Maple Court"
    assert flat["Website"] == "https://maple.example.com"
    # Lowercase / V2 aliases populated.
    assert flat["proj_name"] == "Maple Court"
    assert flat["website"] == "https://maple.example.com"
    assert flat["zip_code"] == "78701"
    # Always-normalised keys.
    assert flat["property_id"] == "P001"
    assert flat["url"] == "https://maple.example.com"
