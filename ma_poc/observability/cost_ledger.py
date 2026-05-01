"""Cost ledger — SQLite-backed running totals of LLM/Vision/Proxy costs.

Per-property and per-PMS rollups for reporting and SLO checking.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class CostLedger:
    """SQLite-backed cost accumulator.

    Args:
        db_path: Path to the SQLite database file.
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cost_entries (
                ts TEXT NOT NULL,
                property_id TEXT NOT NULL,
                pms TEXT,
                tier_used TEXT,
                category TEXT NOT NULL,
                cost_usd REAL NOT NULL,
                detail TEXT
            );
            CREATE INDEX IF NOT EXISTS cost_by_prop ON cost_entries(property_id);
            CREATE INDEX IF NOT EXISTS cost_by_pms ON cost_entries(pms);
            """
        )
        self._conn.commit()

    def record_llm(
        self,
        property_id: str,
        pms: str,
        tier: str,
        cost: float,
        model: str,
        tokens: int,
    ) -> None:
        """Record an LLM cost entry.

        Args:
            property_id: Canonical property ID.
            pms: Detected PMS name.
            tier: Tier used (e.g. 'generic:tier4_llm').
            cost: Cost in USD.
            model: Model name.
            tokens: Total tokens used.
        """
        self._insert(
            property_id,
            pms,
            tier,
            "llm",
            cost,
            {
                "model": model,
                "tokens": tokens,
            },
        )

    def record_vision(
        self,
        property_id: str,
        pms: str,
        tier: str,
        cost: float,
        model: str,
    ) -> None:
        """Record a Vision cost entry.

        Args:
            property_id: Canonical property ID.
            pms: Detected PMS name.
            tier: Tier used.
            cost: Cost in USD.
            model: Model name.
        """
        self._insert(
            property_id,
            pms,
            tier,
            "vision",
            cost,
            {
                "model": model,
            },
        )

    def record_proxy_bytes(
        self,
        property_id: str,
        pms: str,
        bytes_used: int,
        rate_per_mb: float,
    ) -> None:
        """Record proxy bandwidth cost.

        Args:
            property_id: Canonical property ID.
            pms: Detected PMS name.
            bytes_used: Bytes transferred.
            rate_per_mb: Cost per megabyte.
        """
        cost = (bytes_used / (1024 * 1024)) * rate_per_mb
        self._insert(
            property_id,
            pms,
            "",
            "proxy_mb",
            cost,
            {
                "bytes_used": bytes_used,
            },
        )

    def record_proxy_fetch(
        self,
        property_id: str,
        pms: str,
        proxy_tier: str,
        bytes_through_proxy: int,
        cost_usd: float,
    ) -> None:
        """Record a proxy-tiered fetch, tagging by Bright Data tier.

        Separate from ``record_proxy_bytes`` so the tier is stored in
        ``tier_used`` and queries like "how much did RESIDENTIAL cost us
        today" are a single GROUP BY.

        Args:
            property_id: Canonical property ID.
            pms: Detected PMS name.
            proxy_tier: One of direct/datacenter/residential/unblocker.
            bytes_through_proxy: Bytes that flowed through the proxy.
            cost_usd: Estimated cost for this fetch; see
                ``ma_poc.fetch.proxy.pricing.estimate_cost_usd``.
        """
        self._insert(
            property_id,
            pms,
            proxy_tier,
            "proxy_mb",
            cost_usd,
            {
                "bytes_through_proxy": bytes_through_proxy,
                "proxy_tier": proxy_tier,
            },
        )

    def record_fetch_tier(
        self,
        property_id: str,
        fetch_tier: str,
        cost_usd: float,
        body_bytes: int = 0,
        escalation_count: int = 0,
    ) -> None:
        """Record a fetch-tier escalation cost entry.

        Args:
            property_id: Canonical property ID.
            fetch_tier: FetchTier name (DIRECT, DC_PROXY, RESIDENTIAL, UNLOCKER).
            cost_usd: Estimated cost for this fetch.
            body_bytes: Response body size in bytes.
            escalation_count: Number of escalation hops taken.
        """
        self._insert(
            property_id,
            "",
            fetch_tier,
            "proxy_fetch_tier",
            cost_usd,
            {
                "fetch_tier": fetch_tier,
                "body_bytes": body_bytes,
                "escalation_count": escalation_count,
            },
        )

    def rollup_by_fetch_tier(self) -> dict[str, float]:
        """Aggregate proxy costs by fetch tier.

        Returns:
            Dict mapping fetch tier name to total cost in USD.
        """
        rows = self._conn.execute(
            """SELECT tier_used, SUM(cost_usd) as total
               FROM cost_entries
               WHERE category = 'proxy_fetch_tier'
               GROUP BY tier_used"""
        ).fetchall()
        return {row["tier_used"]: round(row["total"], 6) for row in rows}

    def rollup_by_pms(self) -> dict[str, dict[str, float]]:
        """Aggregate costs by PMS platform.

        Returns:
            Dict mapping PMS name to {category: total_cost}.
        """
        rows = self._conn.execute(
            """SELECT COALESCE(pms, 'unknown') as pms_name, category,
               SUM(cost_usd) as total
               FROM cost_entries GROUP BY pms_name, category"""
        ).fetchall()
        result: dict[str, dict[str, float]] = {}
        for row in rows:
            pms = row["pms_name"]
            cat = row["category"]
            result.setdefault(pms, {})[cat] = round(row["total"], 6)
        return result

    def total(self) -> dict[str, float]:
        """Get total costs by category.

        Returns:
            Dict mapping category to total cost in USD.
        """
        rows = self._conn.execute(
            "SELECT category, SUM(cost_usd) as total FROM cost_entries GROUP BY category"
        ).fetchall()
        return {row["category"]: round(row["total"], 6) for row in rows}

    def wasted_calls(self) -> list[dict[str, Any]]:
        """Find properties with LLM cost but zero extracted units.

        Returns:
            List of {property_id, cost_usd} for wasted calls.
        """
        rows = self._conn.execute(
            """SELECT property_id, SUM(cost_usd) as total_cost
               FROM cost_entries WHERE category IN ('llm', 'vision')
               GROUP BY property_id HAVING total_cost > 0"""
        ).fetchall()
        return [{"property_id": row["property_id"], "cost_usd": round(row["total_cost"], 6)} for row in rows]

    def _insert(
        self,
        property_id: str,
        pms: str,
        tier: str,
        category: str,
        cost: float,
        detail: dict[str, Any],
    ) -> None:
        """Insert a cost entry."""
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                """INSERT INTO cost_entries (ts, property_id, pms, tier_used, category, cost_usd, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (now, property_id, pms, tier, category, cost, json.dumps(detail)),
            )
            self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
