"""DualWriteDataProvider — shadow-mode wrapper around two providers.

Writes go to both providers; reads come from the primary. Secondary
failures are logged but never propagated, so a broken PG shadow can't
take down the production (FS) path during cutover.

Typical use — Phase 5 of the PG migration:

    from data_provider.filesystem import FileSystemDataProvider
    from data_provider.postgres import PostgresDataProvider
    from data_provider.dual_write import DualWriteDataProvider

    provider = DualWriteDataProvider(
        primary=FileSystemDataProvider(...),
        secondary=PostgresDataProvider(url="postgresql+psycopg://..."),
    )

Once PG is validated the roles flip and `primary` becomes PG; FS stays
as the audit-trail secondary and can be removed later.

Env wiring: set `DATA_PROVIDER=dual` and (optionally)
`DUAL_PRIMARY=filesystem|postgres`, `DUAL_SECONDARY=postgres|filesystem`.
Defaults: primary=filesystem, secondary=postgres.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

from data_provider.contracts import (
    DataProvider,
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
    ScrapeEvent,
    ScrapeProfile,
    UnitDiff,
    UnitIndexEntry,
)

log = logging.getLogger(__name__)


def _shadow(label: str, fn, *args, **kwargs) -> None:
    """Call a secondary write, swallowing exceptions with a warning log."""
    try:
        fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        log.warning("Shadow write failed (%s): %s", label, e)


# ── Per-store dual wrappers ──────────────────────────────────────────────────


class _DualPropertyState(IPropertyStateStore):
    def __init__(self, p: IPropertyStateStore, s: IPropertyStateStore) -> None:
        self._p, self._s = p, s

    def get(self, canonical_id: str) -> PropertyIndexEntry | None:
        return self._p.get(canonical_id)

    def exists(self, canonical_id: str) -> bool:
        return self._p.exists(canonical_id)

    def upsert(self, canonical_id: str, snapshot: dict[str, Any], run_date: str) -> bool:
        is_new = self._p.upsert(canonical_id, snapshot, run_date)
        _shadow("property_state.upsert", self._s.upsert, canonical_id, snapshot, run_date)
        return is_new

    def all_canonical_ids(self) -> set[str]:
        return self._p.all_canonical_ids()


class _DualUnitState(IUnitStateStore):
    def __init__(self, p: IUnitStateStore, s: IUnitStateStore) -> None:
        self._p, self._s = p, s

    def get_units(self, canonical_id: str) -> dict[str, UnitIndexEntry]:
        return self._p.get_units(canonical_id)

    def upsert_units(
        self, canonical_id: str, today_units: list[dict[str, Any]], run_date: str
    ) -> UnitDiff:
        diff = self._p.upsert_units(canonical_id, today_units, run_date)
        _shadow("unit_state.upsert_units", self._s.upsert_units, canonical_id, today_units, run_date)
        return diff

    def carry_forward_units(
        self, canonical_id: str, run_date: str
    ) -> list[dict[str, Any]]:
        carried = self._p.carry_forward_units(canonical_id, run_date)
        _shadow("unit_state.carry_forward_units", self._s.carry_forward_units, canonical_id, run_date)
        return carried


class _DualRunStore(IRunStore):
    def __init__(self, p: IRunStore, s: IRunStore) -> None:
        self._p, self._s = p, s

    def write_properties(self, run_date: str, properties: list[dict[str, Any]]) -> None:
        self._p.write_properties(run_date, properties)
        _shadow("runs.write_properties", self._s.write_properties, run_date, properties)

    def read_properties(self, run_date: str) -> list[dict[str, Any]]:
        return self._p.read_properties(run_date)

    def write_report(self, run_date: str, report: RunReport) -> None:
        self._p.write_report(run_date, report)
        _shadow("runs.write_report", self._s.write_report, run_date, report)

    def read_report(self, run_date: str) -> RunReport | None:
        return self._p.read_report(run_date)

    def append_issue(self, run_date: str, issue: IssueEntry) -> None:
        self._p.append_issue(run_date, issue)
        _shadow("runs.append_issue", self._s.append_issue, run_date, issue)

    def read_issues(self, run_date: str) -> Iterator[IssueEntry]:
        return self._p.read_issues(run_date)

    def append_ledger_entry(self, run_date: str, entry: LedgerEntry) -> None:
        self._p.append_ledger_entry(run_date, entry)
        _shadow("runs.append_ledger_entry", self._s.append_ledger_entry, run_date, entry)

    def read_ledger(self, run_date: str) -> Iterator[LedgerEntry]:
        return self._p.read_ledger(run_date)

    def list_runs(self) -> list[str]:
        return self._p.list_runs()


class _DualScrapeEvents(IScrapeEventStore):
    def __init__(self, p: IScrapeEventStore, s: IScrapeEventStore) -> None:
        self._p, self._s = p, s

    def append(self, event: ScrapeEvent) -> None:
        self._p.append(event)
        _shadow("scrape_events.append", self._s.append, event)

    def read_all(self) -> Iterator[ScrapeEvent]:
        return self._p.read_all()

    def read_for_property(self, property_id: str) -> Iterator[ScrapeEvent]:
        return self._p.read_for_property(property_id)


class _DualProfiles(IProfileStore):
    def __init__(self, p: IProfileStore, s: IProfileStore) -> None:
        self._p, self._s = p, s

    def get(self, canonical_id: str) -> ScrapeProfile | None:
        return self._p.get(canonical_id)

    def put(self, profile: ScrapeProfile) -> None:
        self._p.put(profile)
        _shadow("profiles.put", self._s.put, profile)

    def list_ids(self) -> list[str]:
        return self._p.list_ids()

    def delete(self, canonical_id: str) -> bool:
        ok = self._p.delete(canonical_id)
        _shadow("profiles.delete", self._s.delete, canonical_id)
        return ok


class _DualExtractionResults(IExtractionResultStore):
    def __init__(self, p: IExtractionResultStore, s: IExtractionResultStore) -> None:
        self._p, self._s = p, s

    def write(self, run_date: str, result: ExtractionResult) -> None:
        self._p.write(run_date, result)
        _shadow("extraction_results.write", self._s.write, run_date, result)

    def read(self, run_date: str, property_id: str) -> ExtractionResult | None:
        return self._p.read(run_date, property_id)


# ── Facade ──────────────────────────────────────────────────────────────────


class DualWriteDataProvider(DataProvider):
    """Writes go to both providers; reads come from `primary`.

    The `transaction()` context coordinates both providers — enters both,
    commits both on success, rolls back both on error. If one's transaction
    fails to commit we surface the exception rather than silently diverging.
    """

    def __init__(
        self,
        primary: DataProvider,
        secondary: DataProvider,
    ) -> None:
        self._primary = primary
        self._secondary = secondary
        self._closed = False

        self.property_state = _DualPropertyState(primary.property_state, secondary.property_state)
        self.unit_state = _DualUnitState(primary.unit_state, secondary.unit_state)
        self.runs = _DualRunStore(primary.runs, secondary.runs)
        self.scrape_events = _DualScrapeEvents(primary.scrape_events, secondary.scrape_events)
        self.profiles = _DualProfiles(primary.profiles, secondary.profiles)
        self.extraction_results = _DualExtractionResults(
            primary.extraction_results, secondary.extraction_results
        )

    @property
    def name(self) -> str:
        return f"dual({self._primary.name}+{self._secondary.name})"

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._primary.transaction():
            try:
                with self._secondary.transaction():
                    yield
            except Exception:
                log.exception(
                    "Secondary transaction failed; primary will roll back too "
                    "to keep the two in sync"
                )
                raise

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._primary.close()
        finally:
            try:
                self._secondary.close()
            finally:
                self._closed = True
