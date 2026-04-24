"""Direct-connection provider — returns no-proxy config for every tier."""

from __future__ import annotations

from ma_poc.fetch.proxy.base import ProxyConfig, ProxyTier


class NoneProvider:
    """Provider that never proxies — useful for testing and local dev."""

    def get_config(
        self,
        *,
        tier: ProxyTier,  # noqa: ARG002
        canonical_id: str,  # noqa: ARG002
        country: str = "us",  # noqa: ARG002
    ) -> ProxyConfig:
        return ProxyConfig(tier=ProxyTier.DIRECT)
