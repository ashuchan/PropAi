"""Health-weighted proxy pool with rotation and sticky sessions.

Each proxy starts at health=1.0. Failures degrade health; successes restore it.
Proxies below health<0.25 are quarantined (skipped).
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

log = logging.getLogger(__name__)


@dataclass
class ProxyHealth:
    """Mutable health state for a single proxy."""

    url: str
    health: float = 1.0
    consecutive_failures: int = 0
    last_used: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __repr__(self) -> str:
        """Repr with credentials redacted."""
        return f"ProxyHealth(url={_redact(self.url)!r}, health={self.health:.2f})"


def _redact(url: str) -> str:
    """Redact credentials from a proxy URL."""
    return re.sub(r"://[^@]+@", "://***@", url)


class ProxyPool:
    """Health-weighted proxy pool.

    Args:
        urls: List of proxy URLs (with credentials).
    """

    def __init__(self, urls: list[str]) -> None:
        self._proxies: dict[str, ProxyHealth] = {url: ProxyHealth(url=url) for url in urls}
        self._sticky: dict[str, str] = {}

    def pick(self, sticky_key: str | None = None) -> str | None:
        """Select a proxy, preferring healthier ones.

        Args:
            sticky_key: If provided, returns the same proxy for the same key.

        Returns:
            Proxy URL, or None if pool is empty/all quarantined.
        """
        if not self._proxies:
            return None

        # Sticky session lookup
        if sticky_key and sticky_key in self._sticky:
            url = self._sticky[sticky_key]
            if url in self._proxies and self._proxies[url].health >= 0.25:
                return url

        # Filter to healthy proxies
        healthy = [p for p in self._proxies.values() if p.health >= 0.25]
        if not healthy:
            return None

        # Weighted random selection by health
        weights = [p.health for p in healthy]
        chosen = random.choices(healthy, weights=weights, k=1)[0]
        chosen.last_used = datetime.now(UTC)

        if sticky_key:
            self._sticky[sticky_key] = chosen.url

        return chosen.url

    def mark_success(self, proxy_url: str) -> None:
        """Record a successful use of a proxy.

        Args:
            proxy_url: The proxy that succeeded.
        """
        if proxy_url in self._proxies:
            p = self._proxies[proxy_url]
            p.health = min(1.0, p.health + 0.05)
            p.consecutive_failures = 0

    def mark_failure(self, proxy_url: str, reason: str) -> None:
        """Record a failed use of a proxy.

        Args:
            proxy_url: The proxy that failed.
            reason: Human-readable failure reason.
        """
        if proxy_url in self._proxies:
            p = self._proxies[proxy_url]
            p.health = max(0.1, p.health - 0.25)
            p.consecutive_failures += 1
            log.warning(
                "Proxy %s failure (%s), health=%.2f, consecutive=%d",
                _redact(proxy_url),
                reason,
                p.health,
                p.consecutive_failures,
            )

    def health_snapshot(self) -> list[dict[str, object]]:
        """Return a snapshot of all proxy health states for dashboards.

        Returns:
            List of dicts with redacted URLs and health metrics.
        """
        return [
            {
                "url": _redact(p.url),
                "health": round(p.health, 2),
                "consecutive_failures": p.consecutive_failures,
                "last_used": p.last_used.isoformat(),
            }
            for p in self._proxies.values()
        ]
