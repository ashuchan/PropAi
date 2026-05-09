"""Filesystem implementation of the data-provider interfaces.

Wraps the existing `core.state_store.StateStore` and
`services.profile_store.ProfileStore` so the scraper pipeline keeps writing
the exact same files on disk. The provider is a typed, swappable facade on
top of that layout — not a replacement for it.

Layout (paths below are rooted at `base_dir` / `config_dir`):

    {base_dir}/state/property_index.json      — IPropertyStateStore
    {base_dir}/state/unit_index.json          — IUnitStateStore
    {base_dir}/runs/{date}/properties.json    — IRunStore.write/read_properties
    {base_dir}/runs/{date}/report.json        — IRunStore.write/read_report
    {base_dir}/runs/{date}/report.md          — IRunStore.write_report (markdown)
    {base_dir}/runs/{date}/issues.jsonl       — IRunStore.append/read_issues
    {base_dir}/runs/{date}/ledger.jsonl       — IRunStore.append/read_ledger
    {base_dir}/scrape_events.jsonl            — IScrapeEventStore
    {base_dir}/extraction_output/{pid}/{date}.json  — IExtractionResultStore
    {config_dir}/profiles/{canonical_id}.json — IProfileStore
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from data_provider.contracts import (
    DataProvider,
    IExtractionResultStore,
    IProfileStore,
    IPropertyCatalogSource,
    IPropertyStateStore,
    IRunStore,
    IScrapeEventStore,
    IUnitStateStore,
)
from data_provider.dtos import (
    CatalogFilters,
    ExtractionResult,
    IssueEntry,
    LedgerEntry,
    PropertyIndexEntry,
    PropertyToScrape,
    RunReport,
    ScrapeEvent,
    ScrapeProfile,
    UnitDiff,
    UnitIndexEntry,
)

log = logging.getLogger(__name__)

# `scripts/` and `services/` import each other with absolute module paths
# (`from models.scrape_profile import ...`). We need to ensure ma_poc is on
# sys.path before importing them — the runner scripts do this implicitly by
# being invoked from the package root, but library consumers may not.
_MA_POC_ROOT = Path(__file__).resolve().parent.parent
if str(_MA_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_MA_POC_ROOT))

from ma_poc.core.state_store import StateStore as _StateStore  # noqa: E402
from services.profile_store import ProfileStore as _ProfileStore  # noqa: E402

# ── Helpers ──────────────────────────────────────────────────────────────────


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def _append_jsonl(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, default=str, ensure_ascii=False))
        f.write("\n")


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                log.warning("Skipping malformed JSONL line in %s", path)
                continue


# ── V1 → V2 key translation for the on-disk state files ─────────────────────
#
# property_index.json and unit_index.json were written before the V2 schema
# rename. The DTOs returned by this provider are V2-shaped, so we translate
# at the read boundary. Keys not present in the map pass through unchanged
# (and end up on the DTO's `extra` allow-list when there's no field for them).

_V1_TO_V2_PROPERTY_KEYS: dict[str, str] = {
    "name": "proj_name",
    "zip": "zip_code",
}

_V1_TO_V2_UNIT_KEYS: dict[str, str] = {
    "bedrooms": "beds",
    "bathrooms": "baths",
    "sqft": "area",
    "market_rent_low": "rent_low",
    "market_rent_high": "rent_high",
}

# Keys the legacy JSON state files still include but that were removed from
# the DTO / DB schema. Strip on read so they don't leak into pydantic extras.
_DROPPED_STATE_KEYS: frozenset[str] = frozenset({"last_seen_date"})


def _v1_to_v2_property_keys(body: dict[str, Any]) -> dict[str, Any]:
    return {_V1_TO_V2_PROPERTY_KEYS.get(k, k): v for k, v in body.items() if k not in _DROPPED_STATE_KEYS}


def _v1_to_v2_unit_keys(body: dict[str, Any]) -> dict[str, Any]:
    return {_V1_TO_V2_UNIT_KEYS.get(k, k): v for k, v in body.items() if k not in _DROPPED_STATE_KEYS}


# Inverse maps for the write path — callers pass V2-shaped dicts but the
# underlying StateStore (scripts/state_store.py) writes the legacy V1 file
# format and diffs on V1 keys, so we translate back at the boundary.
_V2_TO_V1_PROPERTY_KEYS: dict[str, str] = {v: k for k, v in _V1_TO_V2_PROPERTY_KEYS.items()}
_V2_TO_V1_UNIT_KEYS: dict[str, str] = {v: k for k, v in _V1_TO_V2_UNIT_KEYS.items()}


def _v2_to_v1_property_keys(body: dict[str, Any]) -> dict[str, Any]:
    return {_V2_TO_V1_PROPERTY_KEYS.get(k, k): v for k, v in body.items()}


def _v2_to_v1_unit_keys(body: dict[str, Any]) -> dict[str, Any]:
    return {_V2_TO_V1_UNIT_KEYS.get(k, k): v for k, v in body.items()}


# ── IPropertyStateStore / IUnitStateStore ────────────────────────────────────


class FsPropertyStateStore(IPropertyStateStore):
    """Wraps the property_index half of the existing StateStore."""

    def __init__(self, shared: _StateStore, owner: FileSystemDataProvider) -> None:
        self._s = shared
        self._owner = owner

    def _ensure_loaded(self) -> None:
        if not self._owner._loaded:
            self._s.load()
            self._owner._loaded = True

    def get(self, canonical_id: str) -> PropertyIndexEntry | None:
        self._ensure_loaded()
        raw = self._s.get_property(canonical_id)
        if raw is None:
            return None
        # property_index.json was written with V1 field names (`name`, `zip`).
        # The PropertyIndexEntry DTO is V2-shaped, so translate at the boundary
        # — anything we don't translate explicitly is passed through and ends
        # up on the DTO's `extra` allow-list.
        body = {k: v for k, v in raw.items() if k != "canonical_id"}
        body = _v1_to_v2_property_keys(body)
        return PropertyIndexEntry(canonical_id=canonical_id, **body)

    def exists(self, canonical_id: str) -> bool:
        self._ensure_loaded()
        return self._s.is_known(canonical_id)

    def upsert(self, canonical_id: str, snapshot: dict[str, Any], run_date: str) -> bool:
        self._ensure_loaded()
        # property_index.json is still V1-shaped on disk; translate the
        # V2-named snapshot back to V1 before delegating to StateStore.
        is_new = self._s.upsert_property(
            canonical_id,
            _v2_to_v1_property_keys(snapshot),
            run_date,
        )
        self._owner._dirty = True
        return is_new

    def all_canonical_ids(self) -> set[str]:
        self._ensure_loaded()
        return self._s.all_canonical_ids()


class FsUnitStateStore(IUnitStateStore):
    """Wraps the unit_index half of the existing StateStore."""

    def __init__(self, shared: _StateStore, owner: FileSystemDataProvider) -> None:
        self._s = shared
        self._owner = owner

    def _ensure_loaded(self) -> None:
        if not self._owner._loaded:
            self._s.load()
            self._owner._loaded = True

    def get_units(self, canonical_id: str) -> dict[str, UnitIndexEntry]:
        self._ensure_loaded()
        raw = self._s.get_units(canonical_id)
        # unit_index.json was written with V1 field names; translate to V2 at
        # the boundary (see FsPropertyStateStore.get for the rationale).
        return {
            uid: UnitIndexEntry(
                unit_id=uid,
                **_v1_to_v2_unit_keys({k: v for k, v in rec.items() if k != "unit_id"}),
            )
            for uid, rec in raw.items()
        }

    def upsert_units(
        self,
        canonical_id: str,
        today_units: list[dict[str, Any]],
        run_date: str,
    ) -> UnitDiff:
        self._ensure_loaded()
        # unit_index.json is still V1-shaped on disk and StateStore diffs on
        # V1 keys (`market_rent_low/high`), so translate V2 → V1 inbound.
        v1_units = [_v2_to_v1_unit_keys(u) for u in today_units]
        diff = self._s.upsert_units(canonical_id, v1_units, run_date)
        self._owner._dirty = True
        return UnitDiff(**diff)

    def carry_forward_units(self, canonical_id: str, run_date: str) -> list[dict[str, Any]]:
        self._ensure_loaded()
        # StateStore returns V1-keyed dicts; translate outbound so callers
        # see the V2 contract (`rent_low/high`, `beds`, `baths`, `area`, …).
        result = self._s.carry_forward_units(canonical_id, run_date)
        self._owner._dirty = True
        return [_v1_to_v2_unit_keys(u) for u in result]


# ── IRunStore ────────────────────────────────────────────────────────────────


class FsRunStore(IRunStore):
    def __init__(self, base_dir: Path) -> None:
        self._runs = base_dir / "runs"

    def _run_dir(self, run_date: str) -> Path:
        return self._runs / run_date

    def write_properties(self, run_date: str, properties: list[dict[str, Any]]) -> None:
        _atomic_write_json(self._run_dir(run_date) / "properties.json", properties)

    def read_properties(self, run_date: str) -> list[dict[str, Any]]:
        path = self._run_dir(run_date) / "properties.json"
        if not path.exists():
            return []
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Could not read %s: %s", path, e)
            return []
        if not isinstance(data, list):
            log.warning("%s is not a JSON array; ignoring", path)
            return []
        return data

    def write_report(self, run_date: str, report: RunReport) -> None:
        run_dir = self._run_dir(run_date)
        payload = report.model_dump(mode="json")
        _atomic_write_json(run_dir / "report.json", payload)
        # Markdown is a presentational view — let reporting.run_report.build()
        # continue to own it. The provider only guarantees report.json.

    def read_report(self, run_date: str) -> RunReport | None:
        path = self._run_dir(run_date) / "report.json"
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Could not read %s: %s", path, e)
            return None
        return RunReport.model_validate(data)

    def append_issue(self, run_date: str, issue: IssueEntry) -> None:
        _append_jsonl(
            self._run_dir(run_date) / "issues.jsonl",
            issue.model_dump(mode="json"),
        )

    def read_issues(self, run_date: str) -> Iterator[IssueEntry]:
        for obj in _read_jsonl(self._run_dir(run_date) / "issues.jsonl"):
            try:
                yield IssueEntry.model_validate(obj)
            except Exception as e:  # noqa: BLE001
                log.warning("Skipping malformed issue: %s", e)

    def append_ledger_entry(self, run_date: str, entry: LedgerEntry) -> None:
        _append_jsonl(
            self._run_dir(run_date) / "ledger.jsonl",
            entry.model_dump(mode="json"),
        )

    def read_ledger(self, run_date: str) -> Iterator[LedgerEntry]:
        for obj in _read_jsonl(self._run_dir(run_date) / "ledger.jsonl"):
            try:
                yield LedgerEntry.model_validate(obj)
            except Exception as e:  # noqa: BLE001
                log.warning("Skipping malformed ledger row: %s", e)

    def list_runs(self) -> list[str]:
        if not self._runs.exists():
            return []
        return sorted(p.name for p in self._runs.iterdir() if p.is_dir())


# ── IScrapeEventStore ────────────────────────────────────────────────────────


class FsScrapeEventStore(IScrapeEventStore):
    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, event: ScrapeEvent) -> None:
        _append_jsonl(self._path, event.model_dump(mode="json"))

    def read_all(self) -> Iterator[ScrapeEvent]:
        for obj in _read_jsonl(self._path):
            try:
                yield ScrapeEvent.model_validate(obj)
            except Exception as e:  # noqa: BLE001
                log.warning("Skipping malformed ScrapeEvent: %s", e)

    def read_for_property(self, property_id: str) -> Iterator[ScrapeEvent]:
        for ev in self.read_all():
            if ev.property_id == property_id:
                yield ev


# ── IProfileStore ────────────────────────────────────────────────────────────


class FsProfileStore(IProfileStore):
    """Wraps the existing services.profile_store.ProfileStore."""

    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir
        self._store = _ProfileStore(base_dir)

    def get(self, canonical_id: str) -> ScrapeProfile | None:
        return self._store.load(canonical_id)

    def put(self, profile: ScrapeProfile) -> None:
        self._store.save(profile)

    def list_ids(self) -> list[str]:
        if not self._base.exists():
            return []
        ids: list[str] = []
        for p in self._base.iterdir():
            if p.is_file() and p.suffix == ".json" and not p.name.startswith("_"):
                ids.append(p.stem)
        return sorted(ids)

    def delete(self, canonical_id: str) -> bool:
        path = self._base / f"{canonical_id}.json"
        if not path.exists():
            return False
        path.unlink()
        return True


# ── IExtractionResultStore ───────────────────────────────────────────────────


class FsExtractionResultStore(IExtractionResultStore):
    """Per-property extraction JSON under data/extraction_output/{pid}/{date}.json."""

    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir / "extraction_output"

    def _path(self, property_id: str, run_date: str) -> Path:
        return self._base / property_id / f"{run_date}.json"

    def write(self, run_date: str, result: ExtractionResult) -> None:
        _atomic_write_json(
            self._path(result.property_id, run_date),
            result.model_dump(mode="json"),
        )

    def read(self, run_date: str, property_id: str) -> ExtractionResult | None:
        path = self._path(property_id, run_date)
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Could not read %s: %s", path, e)
            return None
        return ExtractionResult.model_validate(data)


# ── IPropertyCatalogSource (CSV) ─────────────────────────────────────────────


class CsvPropertyCatalogSource(IPropertyCatalogSource):
    """CSV-backed catalog. Used as the seed source before DB ingest, and
    as a back-compat override (`--csv <path>` on the runner)."""

    def __init__(self, csv_path: Path | str) -> None:
        self._path = Path(csv_path)

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value in (None, "", "null", "None"):
            return None
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _pick(row: dict[str, Any], *keys: str) -> Any:
        for k in keys:
            v = row.get(k)
            if v not in (None, "", "null", "None"):
                return v
        return None

    @classmethod
    def _resolve_canonical_id(cls, row: dict[str, Any], file_index: int) -> str:
        """Compute the canonical_id for a CSV row. Falls back to a stable
        `row_<i>` synthesised from the original *file* row index so the
        same row keeps the same id even after we re-sort the list."""
        cid = cls._pick(row, "property_id", "Unique ID", "Property ID", "apartmentid")
        return str(cid) if cid is not None else f"row_{file_index}"

    @classmethod
    def _row_to_dto(cls, row: dict[str, Any], canonical_id: str) -> PropertyToScrape:
        url = cls._pick(row, "url", "Website", "website") or ""
        return PropertyToScrape(
            canonical_id=canonical_id,
            url=str(url),
            proj_name=cls._pick(row, "name", "Name", "Property Name", "proj_name"),
            address=cls._pick(row, "address", "Address", "Property Address"),
            city=cls._pick(row, "city", "City"),
            state=cls._pick(row, "state", "State"),
            zip_code=cls._pick(row, "zip_code", "ZIP Code", "zip", "Zip"),
            phone=cls._pick(row, "phone", "Phone"),
            email_address=cls._pick(row, "email_address", "Email", "email"),
            website=cls._pick(row, "website", "Website"),
            pmc=cls._pick(row, "pmc", "Management Company"),
            apartment_id=cls._coerce_int(cls._pick(row, "apartment_id", "apartmentid", "Unique ID")),
            property_type=cls._pick(row, "type", "Type", "Property Type"),
            raw=row,
        )

    def _read_rows(self) -> list[tuple[str, dict[str, Any]]]:
        """Read the CSV and return `(canonical_id, row_dict)` pairs sorted
        by canonical_id ascending — matches the SQL source's
        `ORDER BY canonical_id`. Both sources thus emit identical row
        order, so `--shard-index N --shard-count K` picks the same rows
        regardless of which catalog backend the runner is reading from.
        """
        if not self._path.exists():
            log.warning("CSV catalog source: %s not found; returning empty list", self._path)
            return []
        pairs: list[tuple[str, dict[str, Any]]] = []
        with open(self._path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                row_dict = {k: v for k, v in row.items()}
                pairs.append((self._resolve_canonical_id(row_dict, i), row_dict))
        pairs.sort(key=lambda p: p[0])
        return pairs

    @staticmethod
    def _apply_filters(
        pairs: list[tuple[str, dict[str, Any]]],
        filters: CatalogFilters | None,
    ) -> list[tuple[str, dict[str, Any]]]:
        if filters is None:
            return pairs
        out = pairs
        if filters.canonical_ids:
            wanted = set(filters.canonical_ids)
            out = [(cid, r) for cid, r in out if cid in wanted]
        if filters.property_types:
            wanted_types = {t.upper() for t in filters.property_types}
            out = [
                (cid, r)
                for cid, r in out
                if str(r.get("type") or r.get("Type") or r.get("Property Type") or "").upper()
                in wanted_types
            ]
        return out

    @staticmethod
    def _apply_shard(
        pairs: list[tuple[str, dict[str, Any]]],
        filters: CatalogFilters | None,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Slice by (shard_index, shard_count). Pure-Python equivalent of
        SQL OFFSET/LIMIT — call after `_apply_filters` so the shard index
        is computed against the filtered row set."""
        if filters is None or filters.shard_index is None or filters.shard_count is None:
            return pairs
        idx = filters.shard_index
        count = filters.shard_count
        if count <= 0 or idx < 0 or idx >= count:
            return []
        # Ceiling division so the last shard absorbs the remainder.
        per_shard = (len(pairs) + count - 1) // count
        start = idx * per_shard
        end = min(start + per_shard, len(pairs))
        return pairs[start:end]

    def list_active(
        self,
        *,
        limit: int | None = None,
        filters: CatalogFilters | None = None,
    ) -> list[PropertyToScrape]:
        pairs = self._read_rows()
        pairs = self._apply_filters(pairs, filters)
        pairs = self._apply_shard(pairs, filters)
        if filters is not None and filters.start_index:
            pairs = pairs[filters.start_index :]
        if limit is not None:
            pairs = pairs[:limit]
        return [self._row_to_dto(row, cid) for cid, row in pairs]

    def count_active(self, *, filters: CatalogFilters | None = None) -> int:
        pairs = self._read_rows()
        pairs = self._apply_filters(pairs, filters)
        pairs = self._apply_shard(pairs, filters)
        return len(pairs)


# ── DataProvider facade ─────────────────────────────────────────────────────


class FileSystemDataProvider(DataProvider):
    """FS-backed provider preserving the existing on-disk layout.

    Lazy loading: property/unit indexes are loaded into memory on first
    access and flushed either on `transaction()` exit or on `close()`. This
    matches the legacy StateStore semantics (load-once, save-once-per-run)
    while making multi-step writes atomic.
    """

    def __init__(
        self,
        base_dir: Path | str,
        config_dir: Path | str,
        *,
        scrape_events_path: Path | str | None = None,
        catalog_csv_path: Path | str | None = None,
    ) -> None:
        self._base = Path(base_dir)
        self._config = Path(config_dir)
        self._loaded: bool = False
        self._dirty: bool = False
        self._in_txn: bool = False
        self._closed: bool = False

        self._shared_state = _StateStore(self._base / "state")
        self.property_state = FsPropertyStateStore(self._shared_state, self)
        self.unit_state = FsUnitStateStore(self._shared_state, self)
        self.runs = FsRunStore(self._base)
        self.scrape_events = FsScrapeEventStore(
            Path(scrape_events_path) if scrape_events_path else self._base / "scrape_events.jsonl"
        )
        self.profiles = FsProfileStore(self._config / "profiles")
        self.extraction_results = FsExtractionResultStore(self._base)
        self.property_catalog = CsvPropertyCatalogSource(
            Path(catalog_csv_path) if catalog_csv_path else self._config / "properties.csv"
        )

    @property
    def name(self) -> str:
        return "filesystem"

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Defer state-index writes until the block exits without error.

        Nested transactions are a no-op on the inner level — only the
        outermost block decides whether to flush.
        """
        outermost = not self._in_txn
        self._in_txn = True
        try:
            yield
        except Exception:
            # On error, drop in-memory state so the next loader re-reads disk.
            if outermost:
                self._loaded = False
                self._dirty = False
                self._in_txn = False
            raise
        else:
            if outermost:
                if self._dirty:
                    self._shared_state.save()
                    self._dirty = False
                self._in_txn = False

    def close(self) -> None:
        if self._closed:
            return
        if self._dirty and not self._in_txn:
            self._shared_state.save()
            self._dirty = False
        self._closed = True
