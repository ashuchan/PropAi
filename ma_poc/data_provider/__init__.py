"""Data provider abstraction — swappable FS / Postgres backend.

New code should obtain the active provider via `get_data_provider()`; existing
scraper and API-serving code is unaffected. Source is selected by the
`DATA_PROVIDER` env var (`filesystem` | `postgres`, default `filesystem`).
"""

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
    IssueEntry,
    LedgerEntry,
    PropertyIndexEntry,
    PropertyRecord,
    RunReport,
    RunSummary,
    UnitDiff,
    UnitIndexEntry,
)
from data_provider.dual_write import DualWriteDataProvider
from data_provider.factory import get_data_provider, reset_cached_provider
from data_provider.filesystem import FileSystemDataProvider
from data_provider.postgres import PostgresDataProvider
from data_provider.sqlite import SqliteDataProvider

__all__ = [
    "DataProvider",
    "IPropertyStateStore",
    "IUnitStateStore",
    "IRunStore",
    "IScrapeEventStore",
    "IProfileStore",
    "IExtractionResultStore",
    "PropertyIndexEntry",
    "UnitIndexEntry",
    "UnitDiff",
    "PropertyRecord",
    "RunReport",
    "RunSummary",
    "IssueEntry",
    "LedgerEntry",
    "get_data_provider",
    "reset_cached_provider",
    "FileSystemDataProvider",
    "PostgresDataProvider",
    "SqliteDataProvider",
    "DualWriteDataProvider",
]
