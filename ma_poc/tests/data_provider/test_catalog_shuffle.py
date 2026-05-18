"""Tests for Bug #5 Layer 3 (2026-05-18) — deterministic catalog shuffle.

When ``CatalogFilters.shuffle_seed`` is provided, the catalog source must
shuffle rows BEFORE applying the shard partition. Shuffle is deterministic
by seed so re-runs of the same date land the same property in the same
shard (debug reproducibility); different dates rotate the distribution so
no single shard is always the "Essex shard".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ma_poc.data_provider.dtos import CatalogFilters
from ma_poc.data_provider.filesystem import CsvPropertyCatalogSource


def _write_csv(path: Path, n_essex: int = 27, n_other: int = 100) -> None:
    """Build a synthetic CSV with N contiguous essex rows + N other rows.

    Mimics the production layout where a management-company's properties
    sit contiguously in the catalog.
    """
    header = "apartmentid,name,address,city,state,zip,website\n"
    rows = []
    for i in range(n_essex):
        rows.append(
            f"E{i:04d},Essex Property {i},{i} Main St,Beverly Hills,CA,90210,"
            f"https://www.essexapartmenthomes.com/p/{i}\n"
        )
    for i in range(n_other):
        rows.append(
            f"O{i:04d},Other Property {i},{i} Elm St,Boston,MA,02110,"
            f"https://other-{i}.example.com/\n"
        )
    path.write_text(header + "".join(rows), encoding="utf-8")


# ── Shuffle is deterministic per seed ────────────────────────────────────────


def test_shuffle_with_same_seed_is_deterministic(tmp_path: Path) -> None:
    """Same seed -> same row order across separate list_active calls."""
    csv_path = tmp_path / "props.csv"
    _write_csv(csv_path, n_essex=10, n_other=10)
    src = CsvPropertyCatalogSource(csv_path)

    r1 = src.list_active(filters=CatalogFilters(shuffle_seed="2026-05-18"))
    r2 = src.list_active(filters=CatalogFilters(shuffle_seed="2026-05-18"))
    assert [p.canonical_id for p in r1] == [p.canonical_id for p in r2]


def test_shuffle_with_different_seed_produces_different_order(tmp_path: Path) -> None:
    csv_path = tmp_path / "props.csv"
    _write_csv(csv_path, n_essex=10, n_other=10)
    src = CsvPropertyCatalogSource(csv_path)

    r1 = src.list_active(filters=CatalogFilters(shuffle_seed="2026-05-18"))
    r2 = src.list_active(filters=CatalogFilters(shuffle_seed="2026-05-19"))
    # Different seeds should produce different orderings.
    assert [p.canonical_id for p in r1] != [p.canonical_id for p in r2]


def test_no_seed_preserves_csv_order(tmp_path: Path) -> None:
    """When no shuffle_seed is set, ordering is the pre-fix CSV order
    (essex rows contiguously first). Backwards-compat guard for callers
    that don't pass the seed."""
    csv_path = tmp_path / "props.csv"
    _write_csv(csv_path, n_essex=5, n_other=5)
    src = CsvPropertyCatalogSource(csv_path)

    rows = src.list_active(filters=CatalogFilters())  # no shuffle_seed
    cids = [p.canonical_id for p in rows]
    # First 5 should be Essex (E0000..E0004) in original CSV order.
    assert cids[0] == "E0000"
    assert cids[4] == "E0004"


# ── Shuffle breaks domain clustering across shards ───────────────────────────


def test_shuffle_spreads_essex_across_shards(tmp_path: Path) -> None:
    """Core invariant: with the shuffle, essex's contiguous rows should
    land in MANY shards rather than clustering in a few. Without
    shuffle, 27 essex rows partition into 27 // (127 // shards_per) ≈
    a small number of shards. With shuffle, they spread roughly
    proportionally."""
    csv_path = tmp_path / "props.csv"
    _write_csv(csv_path, n_essex=27, n_other=73)  # 100 total, mirrors prod shape
    src = CsvPropertyCatalogSource(csv_path)

    shards_with_essex_shuffled: set[int] = set()
    for shard_idx in range(20):  # simulate 20 shards
        filters = CatalogFilters(
            shard_index=shard_idx,
            shard_count=20,
            shuffle_seed="2026-05-18",
        )
        for p in src.list_active(filters=filters):
            if p.canonical_id.startswith("E"):
                shards_with_essex_shuffled.add(shard_idx)
                break

    # Without shuffle, the 27 essex rows fit in roughly 27/(100/20) = 5-6
    # contiguous shards. With shuffle, they should hit ≥10 of 20 shards.
    assert len(shards_with_essex_shuffled) >= 10, (
        f"Expected essex rows to spread across ≥10 of 20 shards; "
        f"hit only {len(shards_with_essex_shuffled)}: {sorted(shards_with_essex_shuffled)}"
    )


def test_shuffle_preserves_total_row_count(tmp_path: Path) -> None:
    """No rows lost or duplicated by the shuffle — invariant across
    seeds, shard counts."""
    csv_path = tmp_path / "props.csv"
    _write_csv(csv_path, n_essex=10, n_other=20)  # 30 total
    src = CsvPropertyCatalogSource(csv_path)

    all_cids = set()
    for shard_idx in range(5):
        filters = CatalogFilters(
            shard_index=shard_idx,
            shard_count=5,
            shuffle_seed="2026-05-18",
        )
        for p in src.list_active(filters=filters):
            assert p.canonical_id not in all_cids, f"Duplicate {p.canonical_id}"
            all_cids.add(p.canonical_id)
    assert len(all_cids) == 30


def test_shuffle_seed_is_stable_per_property(tmp_path: Path) -> None:
    """A property landing in shard N with seed=2026-05-18 must STILL land
    in shard N when shard_count is the same and seed is the same. This is
    the reproducibility guarantee — re-running today's run hits the same
    shard distribution."""
    csv_path = tmp_path / "props.csv"
    _write_csv(csv_path, n_essex=5, n_other=5)
    src = CsvPropertyCatalogSource(csv_path)

    location: dict[str, int] = {}
    for shard_idx in range(3):
        filters = CatalogFilters(
            shard_index=shard_idx,
            shard_count=3,
            shuffle_seed="2026-05-18",
        )
        for p in src.list_active(filters=filters):
            location[p.canonical_id] = shard_idx

    # Re-run with same seed + shard_count: must yield identical mapping.
    location_2: dict[str, int] = {}
    for shard_idx in range(3):
        filters = CatalogFilters(
            shard_index=shard_idx,
            shard_count=3,
            shuffle_seed="2026-05-18",
        )
        for p in src.list_active(filters=filters):
            location_2[p.canonical_id] = shard_idx

    assert location == location_2
