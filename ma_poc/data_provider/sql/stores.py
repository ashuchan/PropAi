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
    IPropertyStateStore,
    IRunStore,
    IScrapeEventStore,
    IUnitStateStore,
)
from data_provider.dtos import (
    ExtractionResult,
    IssueEntry,
    LedgerEntry,
    PropertyIndexEntry,
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
    "area",
    "rent_low",
    "rent_high",
    "date_captured",
    "available_date",
    "lease_term",
    "move_in_date",
    # State-tracking
    "first_seen_date",
    "last_seen_at",
    "carryforward_days",
    "disappeared_since",
    "last_absent_date",
    "concessions",
    "changed_fields",
}


def _split_known_extra(data: dict[str, Any], known: set[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    k = {col: data[col] for col in known if col in data}
    x = {col: v for col, v in data.items() if col not in known}
    return k, x


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
        "area": row.area,
        "rent_low": row.rent_low,
        "rent_high": row.rent_high,
        "date_captured": row.date_captured,
        "available_date": row.available_date,
        "lease_term": row.lease_term,
        "move_in_date": row.move_in_date,
        # State-tracking
        "first_seen_date": row.first_seen_date,
        "last_seen_at": row.last_seen_at,
        "carryforward_days": row.carryforward_days or 0,
        "disappeared_since": row.disappeared_since,
        "last_absent_date": row.last_absent_date,
        "concessions": row.concessions,
        "changed_fields": row.changed_fields or [],
    }
    if row.extra:
        base.update(row.extra)
    return UnitIndexEntry(**base)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
            merged["last_seen_at"] = _utc_now_iso()
            if is_new:
                merged["first_seen_date"] = run_date
            else:
                merged.setdefault(
                    "first_seen_date",
                    existing.first_seen_date or run_date,
                )

            known, extra = _split_known_extra(merged, _PROPERTY_COLS)
            values = {**known, "extra": extra}

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


# ── IUnitStateStore ──────────────────────────────────────────────────────────


class SqlUnitStateStore(IUnitStateStore):
    """SQL port of scripts.state_store.StateStore.upsert_units.

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
        "area": ("area", "sqft", "_sqft"),
        "rent_low": ("rent_low", "market_rent_low"),
        "rent_high": ("rent_high", "market_rent_high"),
        "date_captured": ("date_captured",),
        "available_date": ("available_date",),
        "lease_term": ("lease_term", "_lease_term"),
        "move_in_date": ("move_in_date", "_move_in_date"),
        "concessions": ("concessions",),
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
        with self._h.scope() as s:
            prior_rows = (
                s.execute(select(UnitRow).where(UnitRow.canonical_id == canonical_id)).scalars().all()
            )
            prior_by_id: dict[str, UnitRow] = {r.unit_id: r for r in prior_rows}

            diff = UnitDiff()
            current_ids: set[str] = set()

            for u in today_units:
                uid = str(u.get("unit_id") or "").strip()
                if not uid:
                    continue
                current_ids.add(uid)

                snap: dict[str, Any] = {
                    "canonical_id": canonical_id,
                    "unit_id": uid,
                    "last_seen_at": _utc_now_iso(),
                    "carryforward_days": 0,
                }
                for col, sources in self._SNAPSHOT_SOURCES.items():
                    snap[col] = self._read_first(u, sources)

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
            for r in rows:
                if r.disappeared_since:
                    continue
                r.carryforward_days = (r.carryforward_days or 0) + 1
                r.last_seen_at = _utc_now_iso()
                out.append(
                    {
                        "unit_id": r.unit_id,
                        # V2 data fields
                        "beds": r.beds,
                        "baths": r.baths,
                        "floor_plan_name": r.floor_plan_name,
                        "area": r.area,
                        "rent_low": r.rent_low,
                        "rent_high": r.rent_high,
                        "date_captured": r.date_captured,
                        "available_date": r.available_date,
                        "lease_term": r.lease_term,
                        "move_in_date": r.move_in_date,
                        "concessions": r.concessions,
                        # State-tracking
                        "carryforward_days": r.carryforward_days,
                    }
                )
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
            for ordinal, (cid, payload) in enumerate(zip(cids, properties)):
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
