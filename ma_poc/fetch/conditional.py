"""Conditional GET cache — stores ETag and Last-Modified per URL.

SQLite-backed. The fetched_at column enables cache expiry after N days
(forced full re-fetch as a safety net for stale parsers).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)


class ConditionalCache:
    """SQLite-backed cache of (url -> (etag, last_modified, fetched_at)).

    Thread-safe via SQLite's built-in locking.
    """

    def __init__(self, db_path: Path) -> None:
        """Initialise the cache, creating the table if needed.

        Args:
            db_path: Path to the SQLite database file.
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS conditional_cache (
                url TEXT PRIMARY KEY,
                etag TEXT,
                last_modified TEXT,
                fetched_at TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def read(self, url: str) -> tuple[str | None, str | None]:
        """Look up cached conditional headers for a URL.

        Args:
            url: The URL to look up.

        Returns:
            Tuple of (etag, last_modified). Both None if not cached.
        """
        row = self._conn.execute(
            "SELECT etag, last_modified FROM conditional_cache WHERE url = ?",
            (url,),
        ).fetchone()
        if row is None:
            return None, None
        return row[0], row[1]

    def write(self, url: str, etag: str | None, last_modified: str | None) -> None:
        """Insert or update the cache entry for a URL.

        Args:
            url: The URL to cache.
            etag: ETag header value, or None.
            last_modified: Last-Modified header value, or None.
        """
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                """INSERT INTO conditional_cache (url, etag, last_modified, fetched_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET
                     etag = excluded.etag,
                     last_modified = excluded.last_modified,
                     fetched_at = excluded.fetched_at""",
                (url, etag, last_modified, now),
            )
            self._conn.commit()

    def expire_older_than(self, days: int) -> int:
        """Remove cache entries older than the given number of days.

        Args:
            days: Entries older than this many days are removed.

        Returns:
            Number of entries removed.
        """
        cutoff = datetime.now(UTC).isoformat()
        # SQLite datetime comparison works on ISO strings
        cursor = self._conn.execute(
            """DELETE FROM conditional_cache
               WHERE julianday(?) - julianday(fetched_at) > ?""",
            (cutoff, days),
        )
        self._conn.commit()
        removed = cursor.rowcount
        if removed > 0:
            log.info("Expired %d conditional cache entries older than %d days", removed, days)
        return removed

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
