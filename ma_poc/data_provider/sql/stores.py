"""SQL implementations of the six data-provider stores.

Each store holds a reference to a `_SessionHolder` (shared by the provider)
and opens short-lived sessions for single operations. Inside a
`provider.transaction()` block all stores reuse the same session so writes
commit atomically.

Dialect assumptions: Postgres or SQLite (via `engine.dialect_insert()`). No
raw SQL — everything goes through SQLAlchemy core/ORM so the same code
runs on both dialects.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from data_provider.contracts import (
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
    RunSummary,
    ScrapeEvent,
    ScrapeProfile,
    UnitDiff,
    UnitIndexEntry,
)
from data_provider.sql.engine import dialect_insert
from data_provider.sql.models import (
    ExtractionResultRow,
    PropertyRow,
    PropertySnapshotRow,
    RunIssueRow,
    RunLedgerRow,
    RunReportRow,
    RunRow,
    ScrapeEventRow,
    ScrapeProfileRow,
    UnitRow,
)

log = logging.getLogger(__name__)


# ── Session holder — shared between provider and stores ─────────────────────


class _SessionHolder:
    """Owns the sessionmaker and tracks the active transaction session.

    `scope()` returns a session context manager:
      - inside `provider.transaction()`: reuses the active session; commit
        and close are handled by the provider.
      - outside: opens a fresh session, auto-commits on clean exit, rolls
        back on exception.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory
        self.active: Session | None = None
        self._engine = session_factory.kw["bind"]

    @property
    def engine(self) -> Any:
        return self._engine

    @contextmanager
    def scope(self) -> Iterator[Session]:
        if self.active is not None:
            yield self.active
            return
        s = self._factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()


# ── Helpers ─────────────────────────────────────────────────────────────────


# Columns on PropertyRow that the upsert treats as first-class (everything
# else on the snapshot dict falls into the JSON `extra` column). Names match
# the V2 property schema (`scripts/schema_v2.build_v2_property`) plus internal
# state-tracking columns.
_PROPERTY_COLS = {
    "canonical_id",
    # V2 data fields
    "apartment_id",
    "proj_name",
    "address",
    "city",
    "state",
    "zip_code",
    "country",
    "phone",
    "email_address",
    "website",
    "pmc",
    "website_design",
    "concessions",
    # State-tracking (use left(last_seen_at, 10) if you need a date string)
    "first_seen_date",
    "last_seen_at",
    "last_scrape_status",
    "last_units_count",
}

_UNIT_COLS = {
    "canonical_id",
    "unit_id",
    # V2 data fields
    "beds",
    "baths",
    "floor_plan_name",
    "floor_plan_id",
    "floor_plan_name_provenance",
    "source_unit_id",
    "canonical_unit_id",
    "unit_name",
    "floor",
    "building",
    "building_id",
    "building_id_source",
    "area",
    "area_sqft",
    "area_is_published",
    "area_low",
    "area_high",
    "area_range",
    "area_range_raw",
    "area_value_type",
    "area_provenance",
    "area_source_url",
    "rent_low",
    "rent_high",
    "rent_range",
    "rent_range_raw",
    "rent_is_range",
    "rent_provenance",
    "date_captured",
    "available_date",
    "available_date_raw",
    "availability_date_provenance",
    "availability_status",
    "lease_term",
    "move_in_date",
    "extraction_tier",
    "source_ids",
    "source_response_sha256",
    "source_response_url",
    "source_record_locator",
    "source_parent_record_locator",
    "source_asset_url",
    "source_asset_sha256",
    "identity_quality",
    "unit_id_aliases",
    "unit_id_alias_sources",
    "unit_history_key",
    "unit_history_key_basis",
    "unit_history_key_quality",
    "unit_history_key_version",
    # State-tracking
    "first_seen_date",
    "last_seen_at",
    "carryforward_days",
    "disappeared_since",
    "last_absent_date",
    "concessions",
    "amenities",
    "changed_fields",
    # Informational drift hash — never used for merge / dedup, written on
    # every upsert so SQL drift queries don't have to unpack the JSON
    # ``extra`` column.
    "data_sha256",
}


def _split_known_extra(data: dict[str, Any], known: set[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    k = {col: data[col] for col in known if col in data}
    x = {col: v for col, v in data.items() if col not in known}
    return k, x


# Per-table cache of {column_name -> declared_max_length} for VARCHAR/String
# columns. Built once on first use so we can clip oversize strings at the
# write boundary instead of letting Postgres reject the row with
# `value too long for type character varying(N)` (sqlstate 22001).
#
# Why clip rather than fail loudly: this layer is downstream of LLM
# extractors and adapters that occasionally emit pathological values
# (e.g. a 450-char marketing blurb mis-extracted into floor_plan_name).
# A single bad row used to roll back the entire 499-property shard's
# Stage 1 sync transaction, leaving the DB missing the whole shard for
# days at a time. Clipping at 256 chars keeps the data flowing; the
# raw value is still in the source GCS payload if anyone needs it.
_VARCHAR_LIMITS: dict[type, dict[str, int]] = {}


def _varchar_limits(model_cls: type) -> dict[str, int]:
    cache = _VARCHAR_LIMITS.get(model_cls)
    if cache is not None:
        return cache
    out: dict[str, int] = {}
    table = getattr(model_cls, "__table__", None)
    if table is not None:
        for col in table.columns:
            length = getattr(col.type, "length", None)
            if isinstance(length, int) and length > 0:
                out[col.name] = length
    _VARCHAR_LIMITS[model_cls] = out
    return out


def _clip_to_column_limits(
    values: dict[str, Any],
    model_cls: type,
    *,
    log_prefix: str = "",
) -> dict[str, Any]:
    """Truncate string values whose length exceeds the column's declared max.

    Returns the same dict (mutated). Logs a WARNING for each column we
    clip so the upstream extractor bug is visible rather than silently
    swallowed. The PK columns are clipped too — long unit_ids are real
    (some portals emit URL-encoded blob keys); we'd rather have a
    truncated id than no row at all.
    """
    limits = _varchar_limits(model_cls)
    if not limits:
        return values
    for col, max_len in limits.items():
        v = values.get(col)
        if v is None or not isinstance(v, str):
            continue
        if len(v) <= max_len:
            continue
        log.warning(
            "%s clipping oversize value for %s.%s: len=%d limit=%d sample=%r",
            log_prefix or "_clip_to_column_limits",
            model_cls.__name__,
            col,
            len(v),
            max_len,
            v[:80],
        )
        values[col] = v[:max_len]
    return values


def _hydrate_property(row: PropertyRow) -> PropertyIndexEntry:
    base = {
        "canonical_id": row.canonical_id,
        # V2 data fields
        "apartment_id": row.apartment_id,
        "proj_name": row.proj_name,
        "address": row.address,
        "city": row.city,
        "state": row.state,
        "zip_code": row.zip_code,
        "country": row.country,
        "phone": row.phone,
        "email_address": row.email_address,
        "website": row.website,
        "pmc": row.pmc,
        "website_design": row.website_design,
        "concessions": row.concessions,
        # State-tracking
        "first_seen_date": row.first_seen_date,
        "last_seen_at": row.last_seen_at,
        "last_scrape_status": row.last_scrape_status,
        "last_units_count": row.last_units_count,
    }
    if row.extra:
        base.update(row.extra)
    return PropertyIndexEntry(**base)


def _hydrate_unit(row: UnitRow) -> UnitIndexEntry:
    base = {
        "unit_id": row.unit_id,
        # V2 data fields
        "beds": row.beds,
        "baths": row.baths,
        "floor_plan_name": row.floor_plan_name,
        "floor_plan_id": row.floor_plan_id,
        "floor_plan_name_provenance": row.floor_plan_name_provenance,
        "source_unit_id": row.source_unit_id,
        "canonical_unit_id": row.canonical_unit_id,
        "unit_name": row.unit_name,
        "floor": row.floor,
        "building": row.building,
        "building_id": row.building_id,
        "building_id_source": row.building_id_source,
        "area": row.area,
        "area_sqft": row.area_sqft,
        "area_is_published": row.area_is_published,
        "area_low": row.area_low,
        "area_high": row.area_high,
        "area_range": row.area_range,
        "area_range_raw": row.area_range_raw,
        "area_value_type": row.area_value_type,
        "area_provenance": row.area_provenance,
        "area_source_url": row.area_source_url,
        "rent_low": row.rent_low,
        "rent_high": row.rent_high,
        "rent_range": row.rent_range,
        "rent_range_raw": row.rent_range_raw,
        "rent_is_range": row.rent_is_range,
        "rent_provenance": row.rent_provenance,
        "date_captured": row.date_captured,
        "available_date": row.available_date,
        "available_date_raw": row.available_date_raw,
        "availability_date_provenance": row.availability_date_provenance,
        "availability_status": row.availability_status,
        "lease_term": row.lease_term,
        "move_in_date": row.move_in_date,
        "extraction_tier": row.extraction_tier,
        "source_ids": row.source_ids,
        "source_response_sha256": row.source_response_sha256,
        "source_response_url": row.source_response_url,
        "source_record_locator": row.source_record_locator,
        "source_parent_record_locator": row.source_parent_record_locator,
        "source_asset_url": row.source_asset_url,
        "source_asset_sha256": row.source_asset_sha256,
        "identity_quality": row.identity_quality,
        "unit_id_aliases": row.unit_id_aliases or [],
        "unit_id_alias_sources": row.unit_id_alias_sources or [],
        "unit_history_key": row.unit_history_key,
        "unit_history_key_basis": row.unit_history_key_basis,
        "unit_history_key_quality": row.unit_history_key_quality,
        "unit_history_key_version": row.unit_history_key_version,
        # State-tracking
        "first_seen_date": row.first_seen_date,
        "last_seen_at": row.last_seen_at,
        "carryforward_days": row.carryforward_days or 0,
        "disappeared_since": row.disappeared_since,
        "last_absent_date": row.last_absent_date,
        "concessions": row.concessions,
        "amenities": row.amenities,
        "changed_fields": row.changed_fields or [],
        "data_sha256": row.data_sha256,
    }
    if row.extra:
        base.update(row.extra)
    return UnitIndexEntry(**base)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _seen_at_iso(run_date: str) -> str:
    """Date-anchored ``last_seen_at`` timestamp.

    Thin wrapper around :func:`ma_poc.core.identity.seen_at_iso`
    — kept here under the underscore-prefixed legacy name so internal call
    sites don't need updating. The lazy import preserves the
    ``data_provider`` ↔ ``scripts`` boundary (the latter shouldn't be on
    sys.path during alembic migrations).
    """
    from ma_poc.core.identity import seen_at_iso

    return seen_at_iso(run_date)


# ── IPropertyStateStore ──────────────────────────────────────────────────────


class SqlPropertyStateStore(IPropertyStateStore):
    def __init__(self, holder: _SessionHolder) -> None:
        self._h = holder

    def get(self, canonical_id: str) -> PropertyIndexEntry | None:
        with self._h.scope() as s:
            row = s.get(PropertyRow, canonical_id)
            return _hydrate_property(row) if row else None

    def exists(self, canonical_id: str) -> bool:
        with self._h.scope() as s:
            return s.get(PropertyRow, canonical_id) is not None

    def upsert(self, canonical_id: str, snapshot: dict[str, Any], run_date: str) -> bool:
        with self._h.scope() as s:
            existing = s.get(PropertyRow, canonical_id)
            is_new = existing is None

            merged = dict(snapshot)
            # Drop `country=None` so the column's server_default ('US') wins.
            # Scraper payloads often emit `country: null` because property
            # websites rarely echo the country; NULL in the table isn't
            # meaningful for our US-only dataset.
            if merged.get("country") is None:
                merged.pop("country", None)
            # Upstream callers may still pass `last_seen_date` from legacy FS
            # payloads — it's no longer a column, drop it silently.
            merged.pop("last_seen_date", None)
            merged["canonical_id"] = canonical_id
            # Anchor the date portion to ``run_date`` so day-level queries
            # against ``properties.last_seen_at`` agree with the same field
            # on ``units`` (both populated via :func:`_seen_at_iso`).
            merged["last_seen_at"] = _seen_at_iso(run_date)
            if is_new:
                merged["first_seen_date"] = run_date
            else:
                merged.setdefault(
                    "first_seen_date",
                    existing.first_seen_date or run_date,
                )

            known, extra = _split_known_extra(merged, _PROPERTY_COLS)
            values = {**known, "extra": extra}
            _clip_to_column_limits(values, PropertyRow, log_prefix=f"property upsert cid={canonical_id}")

            stmt = dialect_insert(self._h.engine, PropertyRow).values(**values)
            # On conflict, update every column except the PK.
            update_cols = {k: stmt.excluded[k] for k in values if k != "canonical_id"}
            stmt = stmt.on_conflict_do_update(
                index_elements=[PropertyRow.canonical_id],
                set_=update_cols,
            )
            s.execute(stmt)
            return is_new

    def all_canonical_ids(self) -> set[str]:
        with self._h.scope() as s:
            rows = s.execute(select(PropertyRow.canonical_id)).scalars().all()
            return set(rows)


# ── IPropertyCatalogSource ───────────────────────────────────────────────────


class SqlPropertyCatalogSource(IPropertyCatalogSource):
    """Catalog backed by the `properties` table.

    Stable order is enforced via `ORDER BY canonical_id` so shard slicing
    yields the same rows for the same `(shard_index, shard_count)` across
    processes — the contract's stable-order requirement.

    Sharding is done in SQL via OFFSET/LIMIT computed from the post-filter
    count: identical semantics to the CSV source's pure-Python slice.
    """

    def __init__(self, holder: _SessionHolder) -> None:
        self._h = holder

    @staticmethod
    def _row_to_dto(row: PropertyRow) -> PropertyToScrape:
        extra = dict(row.extra or {})
        # `extra` may carry the original CSV cell values (Property Type,
        # Building Type, etc.) when the row was seeded via the ingest
        # script. Surface them as `raw` so dict-style consumers in the
        # runner keep finding their old keys.
        property_type = extra.pop("property_type", None) or extra.pop("Property Type", None)
        return PropertyToScrape(
            canonical_id=row.canonical_id,
            url=row.website or "",
            proj_name=row.proj_name,
            address=row.address,
            city=row.city,
            state=row.state,
            zip_code=row.zip_code,
            country=row.country,
            phone=row.phone,
            email_address=row.email_address,
            website=row.website,
            pmc=row.pmc,
            apartment_id=row.apartment_id,
            property_type=property_type,
            raw=extra,
        )

    @staticmethod
    def _apply_filters(stmt: Any, filters: CatalogFilters | None) -> Any:
        if filters is None:
            return stmt
        if filters.canonical_ids:
            stmt = stmt.where(PropertyRow.canonical_id.in_(filters.canonical_ids))
        # `property_types` lives in the `extra` JSON; SQL filtering across
        # JSON fields is dialect-specific and rarely needed for 500 rows.
        # Apply in-memory after fetch to keep this dialect-agnostic.
        return stmt

    @staticmethod
    def _apply_post_filters(rows: list[PropertyRow], filters: CatalogFilters | None) -> list[PropertyRow]:
        if filters is None or not filters.property_types:
            return rows
        wanted = {t.upper() for t in filters.property_types}
        out: list[PropertyRow] = []
        for r in rows:
            extra = r.extra or {}
            t = extra.get("property_type") or extra.get("Property Type") or ""
            if str(t).upper() in wanted:
                out.append(r)
        return out

    def list_active(
        self,
        *,
        limit: int | None = None,
        filters: CatalogFilters | None = None,
    ) -> list[PropertyToScrape]:
        with self._h.scope() as s:
            stmt = select(PropertyRow).order_by(PropertyRow.canonical_id)
            stmt = self._apply_filters(stmt, filters)
            rows = list(s.execute(stmt).scalars().all())

        # Post-filters that are easier in Python than SQL (JSON-extra lookups).
        rows = self._apply_post_filters(rows, filters)

        # Shard slicing operates on the filtered set — identical to the
        # CSV source so a shard scrapes the same rows regardless of source.
        if filters is not None and filters.shard_index is not None and filters.shard_count is not None:
            idx = filters.shard_index
            count = filters.shard_count
            if count <= 0 or idx < 0 or idx >= count:
                return []
            # Strided (round-robin) — see filesystem._apply_shard: spreads each
            # shared backend host evenly across shards so the divide-by-tasks
            # aggregate rate cap holds. Must match the CSV source exactly.
            rows = rows[idx::count]

        if filters is not None and filters.start_index:
            rows = rows[filters.start_index :]
        if limit is not None:
            rows = rows[:limit]
        return [self._row_to_dto(r) for r in rows]

    def count_active(self, *, filters: CatalogFilters | None = None) -> int:
        with self._h.scope() as s:
            stmt = select(PropertyRow).order_by(PropertyRow.canonical_id)
            stmt = self._apply_filters(stmt, filters)
            rows = list(s.execute(stmt).scalars().all())
        rows = self._apply_post_filters(rows, filters)
        # Match list_active's shard math so callers can size shards correctly.
        if filters is not None and filters.shard_index is not None and filters.shard_count is not None:
            idx = filters.shard_index
            count = filters.shard_count
            if count <= 0 or idx < 0 or idx >= count:
                return 0
            # Strided count — must match list_active's rows[idx::count] slice.
            return len(rows[idx::count])
        return len(rows)


# ── IUnitStateStore ──────────────────────────────────────────────────────────


class SqlUnitStateStore(IUnitStateStore):
    """SQL port of core.state_store.StateStore.upsert_units.

    Diff semantics match the FS store 1:1: `new`, `updated`, `unchanged`,
    `disappeared`. `disappeared_since` is set on prior units that didn't
    reappear today; those rows are kept so a re-appearance can be detected.
    """

    # Fields whose change between runs counts as "updated" in the diff.
    _CHANGE_KEYS = ("rent_low", "rent_high", "available_date", "concessions")
    # Maps each V2 column to the source-key fallback chain we accept on input.
    # Source dicts may already be V2-shaped (preferred) or carry legacy v1
    # names from older callers — we look up both so adapters don't have to
    # rename every field before calling upsert_units.
    _SNAPSHOT_SOURCES: dict[str, tuple[str, ...]] = {
        "beds": ("beds", "bedrooms", "_bedrooms"),
        "baths": ("baths", "bathrooms", "_bathrooms"),
        "floor_plan_name": ("floor_plan_name", "_floor_plan"),
        "floor_plan_id": ("floor_plan_id",),
        "floor_plan_name_provenance": ("floor_plan_name_provenance",),
        "source_unit_id": ("source_unit_id",),
        "canonical_unit_id": ("canonical_unit_id", "unit_id"),
        "unit_name": ("unit_name",),
        "floor": ("floor", "_floor"),
        "building": ("building", "_building", "building_name"),
        "building_id": ("building_id",),
        "building_id_source": ("building_id_source",),
        "area": ("area", "sqft", "_sqft"),
        "area_sqft": ("area_sqft",),
        "area_is_published": ("area_is_published",),
        "area_low": ("area_low",),
        "area_high": ("area_high",),
        "area_range": ("area_range",),
        "area_range_raw": ("area_range_raw",),
        "area_value_type": ("area_value_type",),
        "area_provenance": ("area_provenance",),
        "area_source_url": ("area_source_url",),
        "rent_low": ("rent_low", "market_rent_low"),
        "rent_high": ("rent_high", "market_rent_high"),
        "rent_range": ("rent_range",),
        "rent_range_raw": ("rent_range_raw", "_rent_range_raw"),
        "rent_is_range": ("rent_is_range",),
        "rent_provenance": ("rent_provenance",),
        "date_captured": ("date_captured",),
        "available_date": ("available_date",),
        "available_date_raw": ("available_date_raw", "_available_date_raw"),
        "availability_date_provenance": ("availability_date_provenance",),
        "availability_status": ("availability_status", "_availability_status"),
        "lease_term": ("lease_term", "_lease_term"),
        "move_in_date": ("move_in_date", "_move_in_date"),
        "extraction_tier": ("extraction_tier", "_extraction_tier"),
        "source_ids": ("source_ids",),
        "source_response_sha256": ("source_response_sha256",),
        "source_response_url": ("source_response_url",),
        "source_record_locator": ("source_record_locator",),
        "source_parent_record_locator": ("source_parent_record_locator",),
        "source_asset_url": ("source_asset_url",),
        "source_asset_sha256": ("source_asset_sha256",),
        "identity_quality": ("identity_quality",),
        "unit_id_aliases": ("unit_id_aliases",),
        "unit_id_alias_sources": ("unit_id_alias_sources",),
        "unit_history_key": ("unit_history_key",),
        "unit_history_key_basis": ("unit_history_key_basis",),
        "unit_history_key_quality": ("unit_history_key_quality",),
        "unit_history_key_version": ("unit_history_key_version",),
        "concessions": ("concessions",),
        "amenities": ("amenities",),
    }

    def __init__(self, holder: _SessionHolder) -> None:
        self._h = holder

    @staticmethod
    def _read_first(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for k in keys:
            if d.get(k) is not None:
                return d[k]
        return None

    def get_units(self, canonical_id: str) -> dict[str, UnitIndexEntry]:
        with self._h.scope() as s:
            rows = s.execute(select(UnitRow).where(UnitRow.canonical_id == canonical_id)).scalars().all()
            return {r.unit_id: _hydrate_unit(r) for r in rows}

    def upsert_units(
        self,
        canonical_id: str,
        today_units: list[dict[str, Any]],
        run_date: str,
    ) -> UnitDiff:
        # Lazy import — keeps data_provider importable when scripts/ is not
        # on sys.path (e.g. during alembic migrations).
        from ma_poc.core.identity import (
            assign_fallback_unit_id,
            compute_unit_data_sha256,
            synthesize_unkeyable_id,
        )

        with self._h.scope() as s:
            prior_rows = (
                s.execute(select(UnitRow).where(UnitRow.canonical_id == canonical_id)).scalars().all()
            )
            prior_by_id: dict[str, UnitRow] = {r.unit_id: r for r in prior_rows}

            diff = UnitDiff(input_count=len(today_units))
            current_ids: set[str] = set()

            for u in today_units:
                # Stamp the data hash first so synthesize_unkeyable_id can
                # reuse it. Explicit ``not in`` instead of ``setdefault`` so
                # we don't recompute a 50K-element-payload SHA on records
                # that already have one set upstream — Python evaluates the
                # default argument unconditionally.
                if "data_sha256" not in u:
                    u["data_sha256"] = compute_unit_data_sha256(u)

                uid = str(u.get("unit_id") or "").strip()
                if not uid:
                    # No-drop contract: try the natural / fingerprint /
                    # floor-plan-only fallback first; if even that returns
                    # None, synthesize a stable id from the payload hash and
                    # insert as a new unit. Records never silently disappear.
                    derived = assign_fallback_unit_id(u, canonical_id)
                    if not derived:
                        derived = synthesize_unkeyable_id(u, canonical_id)
                        diff.synthetic_key_used += 1
                    uid = derived
                current_ids.add(uid)

                # Begin with the complete incoming unit so additive output
                # fields are retained in ``extra`` instead of being silently
                # discarded by this fixed-column state boundary. First-class
                # columns below are then normalised from their alias chains.
                snap: dict[str, Any] = {
                    **u,
                    "canonical_id": canonical_id,
                    "unit_id": uid,
                    # Date portion = run_date so day-level "was this unit
                    # observed on day X?" queries are deterministic; time
                    # portion = wall-clock for within-run ordering.
                    "last_seen_at": _seen_at_iso(run_date),
                    "carryforward_days": 0,
                    "data_sha256": u["data_sha256"],
                }
                for col, sources in self._SNAPSHOT_SOURCES.items():
                    snap[col] = self._read_first(u, sources)
                # A legacy caller may omit canonical_unit_id even though the
                # state layer just resolved/fabricated ``uid`` above.
                snap["canonical_unit_id"] = snap.get("canonical_unit_id") or uid

                prior = prior_by_id.get(uid)
                if prior is None:
                    diff.new.append(uid)
                    snap["first_seen_date"] = run_date
                    snap["changed_fields"] = []
                else:
                    changed = [k for k in self._CHANGE_KEYS if getattr(prior, k) != snap.get(k)]
                    if changed:
                        diff.updated.append(uid)
                    else:
                        diff.unchanged.append(uid)
                    snap["changed_fields"] = changed
                    snap["first_seen_date"] = prior.first_seen_date or run_date

                known, extra = _split_known_extra(snap, _UNIT_COLS)
                values = {**known, "extra": extra}
                _clip_to_column_limits(values, UnitRow, log_prefix=f"upsert_units cid={canonical_id}")
                stmt = dialect_insert(self._h.engine, UnitRow).values(**values)
                update_cols = {k: stmt.excluded[k] for k in values if k not in ("canonical_id", "unit_id")}
                stmt = stmt.on_conflict_do_update(
                    index_elements=[UnitRow.canonical_id, UnitRow.unit_id],
                    set_=update_cols,
                )
                s.execute(stmt)

            for uid, prior in prior_by_id.items():
                if uid in current_ids:
                    continue
                diff.disappeared.append(uid)
                if prior.disappeared_since is None:
                    prior.disappeared_since = run_date
                prior.last_absent_date = run_date

            return diff

    def carry_forward_units(self, canonical_id: str, run_date: str) -> list[dict[str, Any]]:
        with self._h.scope() as s:
            rows = s.execute(select(UnitRow).where(UnitRow.canonical_id == canonical_id)).scalars().all()
            out: list[dict[str, Any]] = []
            seen_at = _seen_at_iso(run_date)
            for r in rows:
                if r.disappeared_since:
                    continue
                r.carryforward_days = (r.carryforward_days or 0) + 1
                # Anchor to run_date so a carry-forward on day X registers
                # as "seen on day X" for day-level queries — same contract
                # as a fresh upsert.
                r.last_seen_at = seen_at
                carried = _hydrate_unit(r).model_dump()
                carried["carryforward_days"] = r.carryforward_days
                out.append(carried)
            return out


# ── IRunStore ────────────────────────────────────────────────────────────────


class SqlRunStore(IRunStore):
    def __init__(self, holder: _SessionHolder) -> None:
        self._h = holder

    def _touch_run(self, session: Session, run_date: str) -> None:
        """Ensure a row exists in `runs` so `list_runs()` can see this run."""
        if session.get(RunRow, run_date) is None:
            session.add(RunRow(run_date=run_date))

    def write_properties(self, run_date: str, properties: list[dict[str, Any]]) -> None:
        """Write/replace this batch's property snapshots for ``run_date``.

        Semantics (multi-shard safe): delete rows for THIS BATCH's
        canonical_ids at ``run_date``, then insert the batch. Rows
        belonging to other shards (different canonical_ids) are left
        untouched.

        The previous implementation did ``DELETE WHERE run_date=X`` before
        insert — correct for a single-shard writer, catastrophic in the
        10-shard Cloud Run job where every shard wiped the prior shard's
        rows. The result was ~50 snapshots surviving a run that produced
        500, i.e. a 10× data-loss bug.

        Rows whose canonical_id is None can't be deduped across retries
        (no natural key) — we still insert them so the data isn't lost,
        but callers are expected to always emit a canonical_id.
        """
        with self._h.scope() as s:
            self._touch_run(s, run_date)
            cids = [self._extract_canonical_id(p) for p in properties]
            my_cids = {c for c in cids if c is not None}
            if my_cids:
                s.execute(
                    delete(PropertySnapshotRow).where(
                        PropertySnapshotRow.run_date == run_date,
                        PropertySnapshotRow.canonical_id.in_(my_cids),
                    )
                )
            for ordinal, (cid, payload) in enumerate(zip(cids, properties, strict=True)):
                s.add(
                    PropertySnapshotRow(
                        run_date=run_date,
                        canonical_id=cid,
                        ordinal=ordinal,
                        payload=payload,
                    )
                )

    @staticmethod
    def _extract_canonical_id(payload: dict[str, Any]) -> str | None:
        meta = payload.get("_meta") or {}
        for key in ("canonical_id", "Unique ID", "unique_id", "Property ID", "property_id"):
            v = meta.get(key) if isinstance(meta, dict) else None
            if v:
                return str(v)
            v = payload.get(key)
            if v:
                return str(v)
        return None

    def read_properties(self, run_date: str) -> list[dict[str, Any]]:
        with self._h.scope() as s:
            rows = (
                s.execute(
                    select(PropertySnapshotRow)
                    .where(PropertySnapshotRow.run_date == run_date)
                    .order_by(PropertySnapshotRow.ordinal)
                )
                .scalars()
                .all()
            )
            return [r.payload for r in rows]

    def write_report(self, run_date: str, report: RunReport) -> None:
        with self._h.scope() as s:
            self._touch_run(s, run_date)
            totals = report.totals.model_dump(mode="json")
            known_keys = {
                "run_date",
                "generated_at",
                "totals",
                "tier_distribution",
                "cost",
                "slo_violations",
            }
            body = report.model_dump(mode="json")
            extra = {k: v for k, v in body.items() if k not in known_keys}
            values = {
                "run_date": run_date,
                "generated_at": report.generated_at,
                "totals": totals,
                "tier_distribution": report.tier_distribution or {},
                "cost": report.cost or {},
                "slo_violations": report.slo_violations or [],
                "extra": extra,
            }
            stmt = dialect_insert(self._h.engine, RunReportRow).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[RunReportRow.run_date],
                set_={k: stmt.excluded[k] for k in values if k != "run_date"},
            )
            s.execute(stmt)

    def read_report(self, run_date: str) -> RunReport | None:
        with self._h.scope() as s:
            row = s.get(RunReportRow, run_date)
            if row is None:
                return None
            totals = row.totals or {}
            body: dict[str, Any] = {
                "run_date": row.run_date,
                "generated_at": row.generated_at,
                "totals": RunSummary(**totals),
                "tier_distribution": row.tier_distribution or {},
                "cost": row.cost or {},
                "slo_violations": row.slo_violations or [],
            }
            if row.extra:
                body.update(row.extra)
            return RunReport(**body)

    def append_issue(self, run_date: str, issue: IssueEntry) -> None:
        with self._h.scope() as s:
            self._touch_run(s, run_date)
            next_seq = self._next_seq(s, RunIssueRow, run_date)
            data = issue.model_dump(mode="json")
            ts = data.get("timestamp")
            s.add(
                RunIssueRow(
                    run_date=run_date,
                    seq=next_seq,
                    severity=issue.severity,
                    code=issue.code,
                    message=issue.message,
                    canonical_id=issue.canonical_id,
                    row_index=issue.row_index,
                    details=issue.details,
                    timestamp=str(ts) if ts else None,
                )
            )

    def read_issues(self, run_date: str) -> Iterator[IssueEntry]:
        with self._h.scope() as s:
            rows = (
                s.execute(
                    select(RunIssueRow).where(RunIssueRow.run_date == run_date).order_by(RunIssueRow.seq)
                )
                .scalars()
                .all()
            )
        for r in rows:
            yield IssueEntry(
                severity=r.severity,
                code=r.code,
                message=r.message,
                canonical_id=r.canonical_id,
                row_index=r.row_index,
                details=r.details,
                timestamp=r.timestamp,
            )

    def append_ledger_entry(self, run_date: str, entry: LedgerEntry) -> None:
        with self._h.scope() as s:
            self._touch_run(s, run_date)
            next_seq = self._next_seq(s, RunLedgerRow, run_date)
            data = entry.model_dump(mode="json")
            known = {
                "canonical_id",
                "row_index",
                "status",
                "units_count",
                "carry_forward_used",
                "scrape_failed",
                "error_count",
                "warning_count",
                "url",
                "timestamp",
            }
            extra = {k: v for k, v in data.items() if k not in known}
            ts = data.get("timestamp")
            s.add(
                RunLedgerRow(
                    run_date=run_date,
                    seq=next_seq,
                    canonical_id=entry.canonical_id,
                    row_index=entry.row_index,
                    status=entry.status,
                    units_count=entry.units_count,
                    carry_forward_used=entry.carry_forward_used,
                    scrape_failed=entry.scrape_failed,
                    error_count=entry.error_count,
                    warning_count=entry.warning_count,
                    url=entry.url,
                    timestamp=str(ts) if ts else None,
                    extra=extra or None,
                )
            )

    def read_ledger(self, run_date: str) -> Iterator[LedgerEntry]:
        with self._h.scope() as s:
            rows = (
                s.execute(
                    select(RunLedgerRow).where(RunLedgerRow.run_date == run_date).order_by(RunLedgerRow.seq)
                )
                .scalars()
                .all()
            )
        for r in rows:
            base = {
                "canonical_id": r.canonical_id,
                "row_index": r.row_index,
                "status": r.status,
                "units_count": r.units_count,
                "carry_forward_used": r.carry_forward_used,
                "scrape_failed": r.scrape_failed,
                "error_count": r.error_count,
                "warning_count": r.warning_count,
                "url": r.url,
                "timestamp": r.timestamp,
            }
            if r.extra:
                base.update(r.extra)
            yield LedgerEntry(**base)

    def list_runs(self) -> list[str]:
        with self._h.scope() as s:
            rows = s.execute(select(RunRow.run_date).order_by(RunRow.run_date)).scalars().all()
            return list(rows)

    @staticmethod
    def _next_seq(session: Session, row_cls: type, run_date: str) -> int:
        """Return the next `seq` value for an append-only per-run table."""
        current = session.execute(
            select(row_cls.seq).where(row_cls.run_date == run_date).order_by(row_cls.seq.desc()).limit(1)
        ).scalar()
        return (current or 0) + 1


# ── IScrapeEventStore ────────────────────────────────────────────────────────


class SqlScrapeEventStore(IScrapeEventStore):
    def __init__(self, holder: _SessionHolder) -> None:
        self._h = holder

    def append(self, event: ScrapeEvent) -> None:
        with self._h.scope() as s:
            data = event.model_dump(mode="python")
            ts = data.get("scrape_timestamp")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            row = ScrapeEventRow(
                event_id=event.event_id,
                property_id=event.property_id,
                scrape_timestamp=ts,
                extraction_tier=event.extraction_tier,
                change_detection_result=(
                    event.change_detection_result.value if event.change_detection_result else None
                ),
                scrape_outcome=event.scrape_outcome.value,
                failure_reason=event.failure_reason,
                page_load_ms=event.page_load_ms,
                proxy_used=event.proxy_used,
                proxy_provider=event.proxy_provider,
                vision_fallback_used=event.vision_fallback_used,
                banner_capture_attempted=event.banner_capture_attempted,
                banner_concession_found=event.banner_concession_found,
                accuracy_sample_selected=event.accuracy_sample_selected,
                raw_html_path=event.raw_html_path,
                screenshot_path=event.screenshot_path,
                confidence_score=event.confidence_score,
            )
            s.merge(row)  # idempotent on event_id

    def read_all(self) -> Iterator[ScrapeEvent]:
        with self._h.scope() as s:
            rows = s.execute(select(ScrapeEventRow).order_by(ScrapeEventRow.scrape_timestamp)).scalars().all()
        for r in rows:
            yield self._hydrate(r)

    def read_for_property(self, property_id: str) -> Iterator[ScrapeEvent]:
        with self._h.scope() as s:
            rows = (
                s.execute(
                    select(ScrapeEventRow)
                    .where(ScrapeEventRow.property_id == property_id)
                    .order_by(ScrapeEventRow.scrape_timestamp)
                )
                .scalars()
                .all()
            )
        for r in rows:
            yield self._hydrate(r)

    @staticmethod
    def _hydrate(r: ScrapeEventRow) -> ScrapeEvent:
        return ScrapeEvent(
            event_id=r.event_id,
            property_id=r.property_id,
            scrape_timestamp=r.scrape_timestamp,
            extraction_tier=r.extraction_tier,
            change_detection_result=r.change_detection_result,  # type: ignore[arg-type]
            scrape_outcome=r.scrape_outcome,  # type: ignore[arg-type]
            failure_reason=r.failure_reason,
            page_load_ms=r.page_load_ms,
            proxy_used=r.proxy_used,
            proxy_provider=r.proxy_provider,
            vision_fallback_used=r.vision_fallback_used,
            banner_capture_attempted=r.banner_capture_attempted,
            banner_concession_found=r.banner_concession_found,
            accuracy_sample_selected=r.accuracy_sample_selected,
            raw_html_path=r.raw_html_path,
            screenshot_path=r.screenshot_path,
            confidence_score=r.confidence_score,
        )


# ── IProfileStore ────────────────────────────────────────────────────────────


class SqlProfileStore(IProfileStore):
    def __init__(self, holder: _SessionHolder) -> None:
        self._h = holder

    def get(self, canonical_id: str) -> ScrapeProfile | None:
        with self._h.scope() as s:
            row = s.get(ScrapeProfileRow, canonical_id)
            if row is None:
                return None
            return ScrapeProfile.model_validate(row.payload)

    def put(self, profile: ScrapeProfile) -> None:
        with self._h.scope() as s:
            body = profile.model_dump(mode="json")
            values = {
                "canonical_id": profile.canonical_id,
                "version": profile.version,
                "schema_version": profile.schema_version,
                "created_at": profile.created_at,
                "updated_at": profile.updated_at,
                "updated_by": profile.updated_by,
                "payload": body,
            }
            stmt = dialect_insert(self._h.engine, ScrapeProfileRow).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[ScrapeProfileRow.canonical_id],
                set_={k: stmt.excluded[k] for k in values if k != "canonical_id"},
            )
            s.execute(stmt)

    def list_ids(self) -> list[str]:
        with self._h.scope() as s:
            rows = (
                s.execute(select(ScrapeProfileRow.canonical_id).order_by(ScrapeProfileRow.canonical_id))
                .scalars()
                .all()
            )
            return list(rows)

    def iter_profiles_by_cluster_key(self, cluster_key: str, limit: int = 100) -> list[ScrapeProfile]:
        """Return profiles sharing ``cluster_key`` (arch-hardening #1 warm-start).

        ``cluster_key`` lives inside the JSON ``payload`` column, so this filters
        with the dialect-portable JSON accessor (``payload ->> 'cluster_key'`` on
        Postgres, ``JSON_EXTRACT`` on SQLite) and bounds the result with LIMIT.

        NOTE (prod scale): there is no index on the JSON key today, so this is a
        sequential scan. It only runs for COLD properties that carry a
        cluster_key (a small subset), but a production rollout at 5k+ profiles
        should add a generated ``cluster_key`` column + b-tree index. Defensive:
        any error (dialect/type edge) returns [] so warm-start no-ops rather
        than failing the scrape.
        """
        if not cluster_key:
            return []
        try:
            with self._h.scope() as s:
                stmt = (
                    select(ScrapeProfileRow.payload)
                    .where(ScrapeProfileRow.payload["cluster_key"].as_string() == cluster_key)
                    .limit(limit)
                )
                payloads = s.execute(stmt).scalars().all()
            out: list[ScrapeProfile] = []
            for p in payloads:
                try:
                    out.append(ScrapeProfile.model_validate(p))
                except Exception:
                    continue
            return out
        except Exception as exc:  # dialect/type edge — degrade to no-op
            log.warning("iter_profiles_by_cluster_key failed: %s", exc)
            return []

    def delete(self, canonical_id: str) -> bool:
        with self._h.scope() as s:
            row = s.get(ScrapeProfileRow, canonical_id)
            if row is None:
                return False
            s.delete(row)
            return True


# ── IExtractionResultStore ───────────────────────────────────────────────────


class SqlExtractionResultStore(IExtractionResultStore):
    def __init__(self, holder: _SessionHolder) -> None:
        self._h = holder

    def write(self, run_date: str, result: ExtractionResult) -> None:
        with self._h.scope() as s:
            values = {
                "run_date": run_date,
                "property_id": result.property_id,
                "tier": int(result.tier) if result.tier is not None else None,
                "status": result.status.value,
                "confidence_score": result.confidence_score,
                "raw_fields": result.raw_fields,
                "field_confidences": result.field_confidences,
                "low_confidence_fields": result.low_confidence_fields,
                "timestamp": result.timestamp,
                "error_message": result.error_message,
            }
            stmt = dialect_insert(self._h.engine, ExtractionResultRow).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    ExtractionResultRow.run_date,
                    ExtractionResultRow.property_id,
                ],
                set_={k: stmt.excluded[k] for k in values if k not in ("run_date", "property_id")},
            )
            s.execute(stmt)

    def read(self, run_date: str, property_id: str) -> ExtractionResult | None:
        with self._h.scope() as s:
            row = s.get(ExtractionResultRow, (run_date, property_id))
            if row is None:
                return None
            return ExtractionResult(
                property_id=row.property_id,
                tier=row.tier,  # type: ignore[arg-type]
                status=row.status,  # type: ignore[arg-type]
                confidence_score=row.confidence_score,
                raw_fields=row.raw_fields or {},
                field_confidences=row.field_confidences or {},
                low_confidence_fields=row.low_confidence_fields or [],
                timestamp=row.timestamp,
                error_message=row.error_message,
            )
