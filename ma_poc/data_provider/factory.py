"""Env-driven factory for DataProvider instances.

Reads `DATA_PROVIDER` from the environment (via ma_poc/.env if python-dotenv
has loaded it). Supported values: `filesystem` (default), `postgres`.

Path overrides:
  - DATA_DIR           — base directory for data (default: ./data).
  - CONFIG_DIR         — base directory for config (default: ./config).
  - SCRAPE_EVENTS_PATH — absolute path for scrape_events.jsonl
                          (default: {DATA_DIR}/scrape_events.jsonl).
  - DATABASE_URL       — Postgres connection string (when DATA_PROVIDER=postgres).

The returned provider is cached per-process. Use `reset_cached_provider()`
in tests to pick up env-var changes.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from data_provider.contracts import DataProvider
from data_provider.dual_write import DualWriteDataProvider
from data_provider.filesystem import FileSystemDataProvider
from data_provider.postgres import PostgresDataProvider
from data_provider.sqlite import SqliteDataProvider

log = logging.getLogger(__name__)

_SUPPORTED = {"filesystem", "fs", "postgres", "pg", "sqlite", "dual"}
_cached: Optional[DataProvider] = None


def _resolve_base_dirs() -> tuple[Path, Path]:
    """Resolve data + config directories relative to the ma_poc package root.

    `DATA_DIR=./data` in ma_poc/.env is interpreted relative to the ma_poc
    directory, not to CWD, so runners invoked from any cwd get the same paths.
    """
    ma_poc_root = Path(__file__).resolve().parent.parent
    data = Path(os.getenv("DATA_DIR", "./data"))
    config = Path(os.getenv("CONFIG_DIR", "./config"))
    if not data.is_absolute():
        data = (ma_poc_root / data).resolve()
    if not config.is_absolute():
        config = (ma_poc_root / config).resolve()
    return data, config


def get_data_provider(
    *,
    override: str | None = None,
    force_new: bool = False,
) -> DataProvider:
    """Return the active data provider, creating it on first call.

    Args:
        override: Force a specific provider name ("filesystem" | "postgres"),
            ignoring the env var. Useful for tests.
        force_new: Bypass the process-level cache and build a fresh instance.
    """
    global _cached
    if _cached is not None and not force_new and override is None:
        return _cached

    name = (override or os.getenv("DATA_PROVIDER") or "filesystem").strip().lower()
    if name not in _SUPPORTED:
        log.warning(
            "Unknown DATA_PROVIDER=%r; falling back to filesystem. "
            "Supported: %s",
            name,
            sorted(_SUPPORTED),
        )
        name = "filesystem"

    data_dir, config_dir = _resolve_base_dirs()

    def build(kind: str) -> DataProvider:
        if kind in ("filesystem", "fs"):
            return FileSystemDataProvider(
                base_dir=data_dir,
                config_dir=config_dir,
                scrape_events_path=os.getenv("SCRAPE_EVENTS_PATH"),
            )
        if kind in ("postgres", "pg"):
            return PostgresDataProvider(url=os.getenv("DATABASE_URL"))
        if kind == "sqlite":
            return SqliteDataProvider(url=os.getenv("DATABASE_URL"))
        raise ValueError(f"Unknown provider kind: {kind!r}")

    if name == "dual":
        primary_kind = (os.getenv("DUAL_PRIMARY") or "filesystem").strip().lower()
        secondary_kind = (os.getenv("DUAL_SECONDARY") or "postgres").strip().lower()
        provider: DataProvider = DualWriteDataProvider(
            primary=build(primary_kind),
            secondary=build(secondary_kind),
        )
    else:
        provider = build(name)

    log.info(
        "Data provider initialised: name=%s data_dir=%s config_dir=%s",
        provider.name, data_dir, config_dir,
    )

    if not force_new and override is None:
        _cached = provider
    return provider


def reset_cached_provider() -> None:
    """Drop the cached provider so the next `get_data_provider()` re-reads env."""
    global _cached
    if _cached is not None:
        try:
            _cached.close()
        except Exception:  # noqa: BLE001
            log.exception("Error closing cached data provider")
    _cached = None
