"""
Residential proxy rotation + per-domain failure-rate tracking.

Acceptance criteria (CLAUDE.md PR-01):
- proxy_manager.py handles rotation
- Auto-escalate domains with rolling failure rate >2% to proxy
- Reads creds from env (PROXY_HOST/PORT/USERNAME/PASSWORD); never commits secrets
- Per-domain failure rate computed from a 7-day rolling window of attempts
"""
from __future__ import annotations

import os
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse


@dataclass
class ProxyCredentials:
    host: str
    port: int
    username: str
    password: str
    provider: str = "brightdata"

    @classmethod
    def from_env(cls) -> Optional["ProxyCredentials"]:
        host = os.getenv("PROXY_HOST", "").strip()
        port = os.getenv("PROXY_PORT", "").strip()
        user = os.getenv("PROXY_USERNAME", "").strip()
        pwd = os.getenv("PROXY_PASSWORD", "").strip()
        if not all([host, port, user, pwd]):
            return None
        try:
            port_int = int(port)
        except ValueError:
            return None
        return cls(
            host=host,
            port=port_int,
            username=user,
            password=pwd,
            provider=os.getenv("PROXY_PROVIDER", "brightdata"),
        )

    def as_playwright(self) -> dict[str, str]:
        return {
            "server": f"http://{self.host}:{self.port}",
            "username": self.username,
            "password": self.password,
        }


@dataclass
class _DomainStats:
    attempts: deque[tuple[datetime, bool]] = field(default_factory=lambda: deque(maxlen=2000))
    forced_proxy: bool = False


class ProxyManager:
    """
    Tracks per-domain success/failure and decides whether to inject proxy.

    Threshold: domains with >2% failure rate over the rolling 7-day window are
    auto-escalated to use the residential proxy on subsequent requests.
    """

    ROLLING_WINDOW = timedelta(days=7)
    THRESHOLD = 0.02

    def __init__(self, creds: Optional[ProxyCredentials] = None) -> None:
        self.creds = creds or ProxyCredentials.from_env()
        self._stats: dict[str, _DomainStats] = defaultdict(_DomainStats)

    @staticmethod
    def domain_of(url: str) -> str:
        host = urlparse(url).hostname or url
        return host.lower()

    def record(self, url: str, success: bool) -> None:
        domain = self.domain_of(url)
        self._stats[domain].attempts.append((datetime.now(timezone.utc), success))
        if self._failure_rate(domain) > self.THRESHOLD:
            self._stats[domain].forced_proxy = True

    def _failure_rate(self, domain: str) -> float:
        cutoff = datetime.now(timezone.utc) - self.ROLLING_WINDOW
        rolling = [s for ts, s in self._stats[domain].attempts if ts >= cutoff]
        if not rolling:
            return 0.0
        failures = sum(1 for ok in rolling if not ok)
        return failures / len(rolling)

    def should_use_proxy(self, url: str) -> bool:
        if not self.creds:
            return False
        return self._stats[self.domain_of(url)].forced_proxy

    def proxy_config_for(self, url: str) -> Optional[dict[str, str]]:
        if not self.should_use_proxy(url):
            return None
        assert self.creds is not None
        return self.creds.as_playwright()

    def force_proxy(self, url: str) -> None:
        """Manual escalation hook (e.g. on bot-block detection)."""
        self._stats[self.domain_of(url)].forced_proxy = True
