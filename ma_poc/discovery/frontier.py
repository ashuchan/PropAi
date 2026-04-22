"""Persistent URL frontier — SQLite-backed store of URLs to visit.

Survives crashes and process restarts. Tracks per-URL fetch state,
consecutive failures, and parking status.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from ..fetch.contracts import FetchOutcome

log = logging.getLogger(__name__)


class Frontier:
    """SQLite-backed URL frontier for discovery scheduling.

    Args:
        db_path: Path to the SQLite database file.
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS frontier (
                url TEXT PRIMARY KEY,
                host TEXT NOT NULL,
                property_id TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_attempted TEXT,
                last_outcome TEXT,
                consecutive_failures INTEGER DEFAULT 0,
                is_parked INTEGER DEFAULT 0,
                depth INTEGER DEFAULT 0,
                source TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS frontier_property ON frontier(property_id);
            CREATE INDEX IF NOT EXISTS frontier_host ON frontier(host);
            """
        )
        self._conn.commit()

    def upsert_url(self, url: str, property_id: str, depth: int, source: str) -> None:
        """Insert or update a URL in the frontier.

        Args:
            url: The URL to track.
            property_id: Canonical property ID.
            depth: 0 = entry URL, 1 = discovered link.
            source: 'csv', 'sitemap', or 'link_exploration'.
        """
        host = urlparse(url).netloc
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """INSERT INTO frontier (url, host, property_id, first_seen, depth, source)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET
                 property_id = excluded.property_id,
                 depth = MIN(frontier.depth, excluded.depth)""",
            (url, host, property_id, now, depth, source),
        )
        self._conn.commit()

    def mark_attempt(self, url: str, outcome: FetchOutcome) -> None:
        """Record the result of a fetch attempt.

        Args:
            url: The URL that was fetched.
            outcome: The fetch outcome.
        """
        now = datetime.now(UTC).isoformat()
        if outcome == FetchOutcome.OK or outcome == FetchOutcome.NOT_MODIFIED:
            self._conn.execute(
                """UPDATE frontier SET last_attempted = ?, last_outcome = ?,
                   consecutive_failures = 0 WHERE url = ?""",
                (now, outcome.value, url),
            )
        else:
            self._conn.execute(
                """UPDATE frontier SET last_attempted = ?, last_outcome = ?,
                   consecutive_failures = consecutive_failures + 1 WHERE url = ?""",
                (now, outcome.value, url),
            )
        self._conn.commit()

    def park(self, property_id: str) -> None:
        """Park all URLs for a property (DLQ).

        Args:
            property_id: The property to park.
        """
        self._conn.execute(
            "UPDATE frontier SET is_parked = 1 WHERE property_id = ?",
            (property_id,),
        )
        self._conn.commit()

    def unpark(self, property_id: str) -> None:
        """Unpark all URLs for a property.

        Args:
            property_id: The property to unpark.
        """
        self._conn.execute(
            "UPDATE frontier SET is_parked = 0 WHERE property_id = ?",
            (property_id,),
        )
        self._conn.commit()

    def property_urls(self, property_id: str) -> list[dict[str, object]]:
        """Get all URLs for a property.

        Args:
            property_id: The canonical property ID.

        Returns:
            List of frontier entry dicts.
        """
        rows = self._conn.execute("SELECT * FROM frontier WHERE property_id = ?", (property_id,)).fetchall()
        return [dict(r) for r in rows]

    def by_host(self, host: str) -> list[dict[str, object]]:
        """Get all URLs for a host (for rate-limit planning).

        Args:
            host: The hostname.

        Returns:
            List of frontier entry dicts.
        """
        rows = self._conn.execute("SELECT * FROM frontier WHERE host = ?", (host,)).fetchall()
        return [dict(r) for r in rows]

    def get_entry(self, url: str) -> dict[str, object] | None:
        """Get the frontier entry for a single URL.

        Args:
            url: The URL to look up.

        Returns:
            Dict of the entry, or None if not found.
        """
        row = self._conn.execute("SELECT * FROM frontier WHERE url = ?", (url,)).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
