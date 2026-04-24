"""Proxy abstraction — tier enum, config dataclass, provider protocol."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import quote


class ProxyTier(StrEnum):
    """Tiers of escalation. Order matters: higher ordinal = more expensive."""

    DIRECT = "direct"
    DATACENTER = "datacenter"
    RESIDENTIAL = "residential"
    UNBLOCKER = "unblocker"

    def next_tier(self) -> ProxyTier | None:
        """Return the next-higher tier, or None if already at the top."""
        order = [
            ProxyTier.DIRECT,
            ProxyTier.DATACENTER,
            ProxyTier.RESIDENTIAL,
            ProxyTier.UNBLOCKER,
        ]
        idx = order.index(self)
        return order[idx + 1] if idx + 1 < len(order) else None


@dataclass(frozen=True)
class ProxyConfig:
    """Everything a fetcher needs to route a request through a proxy.

    A ``server=None`` config is the direct-connection case.
    """

    tier: ProxyTier
    server: str | None = None
    username: str | None = None
    password: str | None = None
    session_id: str | None = None

    @property
    def is_direct(self) -> bool:
        return self.server is None

    def to_playwright(self) -> dict[str, str] | None:
        """Format for ``playwright ... launch(proxy=...)``. None = no proxy."""
        if self.is_direct or self.server is None:
            return None
        out: dict[str, str] = {"server": self.server}
        if self.username is not None:
            out["username"] = self.username
        if self.password is not None:
            out["password"] = self.password
        return out

    def to_httpx(self) -> dict[str, str] | None:
        """Format for ``httpx.AsyncClient(proxies=...)``. None = no proxy."""
        if self.is_direct or self.server is None:
            return None
        user = quote(self.username or "", safe="")
        pwd = quote(self.password or "", safe="")
        scheme, rest = self.server.split("://", 1)
        if user or pwd:
            url = f"{scheme}://{user}:{pwd}@{rest}"
        else:
            url = self.server
        return {"http://": url, "https://": url}

    def to_httpx_url(self) -> str | None:
        """Single proxy URL form, as httpx >=0.26 accepts via ``proxy=``."""
        mapping = self.to_httpx()
        if mapping is None:
            return None
        return mapping["https://"]


class ProxyProvider(Protocol):
    """Every provider implementation satisfies this interface."""

    def get_config(
        self,
        *,
        tier: ProxyTier,
        canonical_id: str,
        country: str = "us",
    ) -> ProxyConfig:
        """Return the proxy config for this property at this tier."""
        ...
