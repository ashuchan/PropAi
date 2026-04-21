"""Abstract base classes for the data-provider layer.

Every concrete provider (filesystem, postgres, dual-write) must implement
these. The provider facade (`DataProvider`) bundles the individual stores
and exposes a `transaction()` context manager for atomic multi-store writes.

Design rules:
  - Methods are sync. File and local-SQLite work is trivially sync; a PG
    implementation can wrap a SQLAlchemy session. Async variants can be
    added later without breaking consumers.
  - No method on a concrete provider may leak paths, SQL, or JSON — only DTOs.
  - Writes are eventually-consistent *within* a provider; consumers should
    call `transaction()` when they need a multi-call atomic boundary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import AbstractContextManager
from typing import Any

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


class IPropertyStateStore(ABC):
    """Current-state view of property_index.json (one row per canonical_id)."""

    @abstractmethod
    def get(self, canonical_id: str) -> PropertyIndexEntry | None: ...

    @abstractmethod
    def exists(self, canonical_id: str) -> bool: ...

    @abstractmethod
    def upsert(self, canonical_id: str, snapshot: dict[str, Any], run_date: str) -> bool:
        """Insert-or-update snapshot. Returns True if canonical_id is new."""

    @abstractmethod
    def all_canonical_ids(self) -> set[str]: ...


class IUnitStateStore(ABC):
    """Current-state view of unit_index.json (canonical_id → unit_id → snapshot)."""

    @abstractmethod
    def get_units(self, canonical_id: str) -> dict[str, UnitIndexEntry]: ...

    @abstractmethod
    def upsert_units(
        self,
        canonical_id: str,
        today_units: list[dict[str, Any]],
        run_date: str,
    ) -> UnitDiff: ...

    @abstractmethod
    def carry_forward_units(self, canonical_id: str, run_date: str) -> list[dict[str, Any]]: ...


class IRunStore(ABC):
    """Per-run artifacts under data/runs/{run_date}/ in the FS layout."""

    @abstractmethod
    def write_properties(self, run_date: str, properties: list[dict[str, Any]]) -> None: ...

    @abstractmethod
    def read_properties(self, run_date: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def write_report(self, run_date: str, report: RunReport) -> None: ...

    @abstractmethod
    def read_report(self, run_date: str) -> RunReport | None: ...

    @abstractmethod
    def append_issue(self, run_date: str, issue: IssueEntry) -> None: ...

    @abstractmethod
    def read_issues(self, run_date: str) -> Iterator[IssueEntry]: ...

    @abstractmethod
    def append_ledger_entry(self, run_date: str, entry: LedgerEntry) -> None: ...

    @abstractmethod
    def read_ledger(self, run_date: str) -> Iterator[LedgerEntry]: ...

    @abstractmethod
    def list_runs(self) -> list[str]:
        """Returns run_date strings (YYYY-MM-DD), sorted ascending."""


class IScrapeEventStore(ABC):
    """Append-only audit log of every scrape attempt."""

    @abstractmethod
    def append(self, event: ScrapeEvent) -> None: ...

    @abstractmethod
    def read_all(self) -> Iterator[ScrapeEvent]: ...

    @abstractmethod
    def read_for_property(self, property_id: str) -> Iterator[ScrapeEvent]: ...


class IProfileStore(ABC):
    """Per-property self-learning profile (config/profiles/*.json in FS)."""

    @abstractmethod
    def get(self, canonical_id: str) -> ScrapeProfile | None: ...

    @abstractmethod
    def put(self, profile: ScrapeProfile) -> None: ...

    @abstractmethod
    def list_ids(self) -> list[str]: ...

    @abstractmethod
    def delete(self, canonical_id: str) -> bool: ...


class IExtractionResultStore(ABC):
    """Per-property extraction result (tier + confidence + field values)."""

    @abstractmethod
    def write(self, run_date: str, result: ExtractionResult) -> None: ...

    @abstractmethod
    def read(self, run_date: str, property_id: str) -> ExtractionResult | None: ...


class DataProvider(ABC):
    """Facade bundling all stores. Obtain via `get_data_provider()`."""

    property_state: IPropertyStateStore
    unit_state: IUnitStateStore
    runs: IRunStore
    scrape_events: IScrapeEventStore
    profiles: IProfileStore
    extraction_results: IExtractionResultStore

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier, e.g. `filesystem` or `postgres`. Used in logs."""

    @abstractmethod
    def transaction(self) -> AbstractContextManager[None]:
        """Scope for atomic multi-store writes.

        FS provider: defers `save()` on in-memory indexes until the block
        exits cleanly. PG provider: wraps a SQLAlchemy session/txn.
        Exceptions inside the block must roll back any pending writes.
        """

    @abstractmethod
    def close(self) -> None:
        """Release resources (flush pending writes, close DB pool, …)."""
