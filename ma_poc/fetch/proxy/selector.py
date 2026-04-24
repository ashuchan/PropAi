"""Selects which proxy tier to use for a given property fetch.

Reads the persisted ``proxy_tier`` on the property profile and returns a
``ProxyTier``. A ``ProxyOverride`` wins over the profile (used by the
``--proxy`` CLI flag on ``jugnu_runner``). New properties with no stored
tier default to ``DATACENTER`` — cheaper than residential, less
reputation-stained than GCP egress across all properties.
"""

from __future__ import annotations

from dataclasses import dataclass

from ma_poc.fetch.proxy.base import ProxyTier


@dataclass(frozen=True)
class ProxyOverride:
    """Force a specific tier regardless of profile — testing / manual ops."""

    tier: ProxyTier


class ProxySelector:
    def __init__(self, default_tier: ProxyTier = ProxyTier.DATACENTER) -> None:
        self.default_tier = default_tier

    def pick(
        self,
        *,
        profile_tier: str | None,
        override: ProxyOverride | None = None,
    ) -> ProxyTier:
        if override is not None:
            return override.tier
        if profile_tier is None:
            return self.default_tier
        try:
            return ProxyTier(profile_tier)
        except ValueError:
            # Corrupt profile value — fall back to default rather than crash
            return self.default_tier
