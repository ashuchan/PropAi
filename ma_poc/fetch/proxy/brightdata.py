"""Bright Data proxy provider implementation.

Credentials are loaded from env at construction time. In production the env
is populated from Secret Manager; in local dev from ``.env``. Unit tests
never hit Bright Data — the integration test in this package is gated by
BRIGHTDATA_INTEGRATION_TEST.

Bright Data encodes tier, session, country, and city into the *username*
string, not separate parameters. Current username format:

    brd-customer-{id}-zone-{zone}-country-{cc}-session-{sid}

Ports: 33335 for HTTP/HTTPS (22225 is deprecated). Host: brd.superproxy.io.

Do not log the password.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass

from ma_poc.fetch.proxy.base import ProxyConfig, ProxyTier

log = logging.getLogger(__name__)

BRIGHTDATA_HOST = os.environ.get("BRIGHTDATA_HOST", "brd.superproxy.io")
BRIGHTDATA_PORT = int(os.environ.get("BRIGHTDATA_PORT", "33335"))


@dataclass(frozen=True)
class BrightDataZone:
    """A single Bright Data zone — one per tier."""

    zone_name: str
    password: str


_ENV_KEYS_BY_TIER: dict[ProxyTier, tuple[str, str]] = {
    ProxyTier.DATACENTER: ("BRIGHTDATA_DC_ZONE", "BRIGHTDATA_DC_PASSWORD"),
    ProxyTier.RESIDENTIAL: ("BRIGHTDATA_RESI_ZONE", "BRIGHTDATA_RESI_PASSWORD"),
}


class BrightDataProvider:
    """Provider that constructs per-request proxy configs for Bright Data.

    Required env var (always):
        BRIGHTDATA_CUSTOMER_ID

    Tier-specific env vars (resolved lazily on first use of the tier):
        DATACENTER  -> BRIGHTDATA_DC_ZONE, BRIGHTDATA_DC_PASSWORD
        RESIDENTIAL -> BRIGHTDATA_RESI_ZONE, BRIGHTDATA_RESI_PASSWORD

    Construction only verifies CUSTOMER_ID is present. Tier zones are
    materialised on first ``get_config(tier=...)`` call (2026-05-24 —
    previously both DC + RESI zones were eagerly required at __init__,
    which crashed the residential-only deployment scenario where DC
    zone/password secrets are not provisioned).

    UNBLOCKER tier raises NotImplementedError — Web Unlocker has its
    own provider (``fetch/providers/unlocker.py``) that does not use
    BrightDataProvider at all.
    """

    def __init__(self) -> None:
        self.customer_id = self._require("BRIGHTDATA_CUSTOMER_ID")
        # Lazy zone cache — populated on first ``get_config`` call for
        # each tier. Tiers whose env vars are missing surface a clear
        # RuntimeError at the call site (not at construction) so
        # deployments wired for only one tier don't crash at startup.
        self._zones: dict[ProxyTier, BrightDataZone] = {}

    @staticmethod
    def _require(key: str) -> str:
        val = os.environ.get(key)
        if not val:
            raise RuntimeError(
                f"{key} is required for BrightDataProvider. "
                "Set it via Secret Manager in prod or .env in dev. "
                "See BRIGHT_DATA_SETUP.md for credential sourcing."
            )
        return val

    def _zone_for(self, tier: ProxyTier) -> BrightDataZone:
        """Resolve the zone+password for *tier*, caching on first hit.

        Raises ``RuntimeError`` with a tier-named message when the env
        vars for that specific tier are missing. The error fires only
        when the tier is actually used — not at provider construction —
        so a deployment that enables only RESIDENTIAL doesn't need the
        DC zone secrets, and vice versa.
        """
        cached = self._zones.get(tier)
        if cached is not None:
            return cached
        if tier not in _ENV_KEYS_BY_TIER:
            raise RuntimeError(f"No env-key mapping for ProxyTier {tier!r}")
        zone_env, pwd_env = _ENV_KEYS_BY_TIER[tier]
        zone = BrightDataZone(
            zone_name=self._require(zone_env),
            password=self._require(pwd_env),
        )
        self._zones[tier] = zone
        return zone

    def get_config(
        self,
        *,
        tier: ProxyTier,
        canonical_id: str,
        country: str = "us",
    ) -> ProxyConfig:
        if tier == ProxyTier.DIRECT:
            return ProxyConfig(tier=ProxyTier.DIRECT)

        if tier == ProxyTier.UNBLOCKER:
            raise NotImplementedError(
                "UNBLOCKER tier is handled by fetch/providers/unlocker.py — "
                "BrightDataProvider does not own it"
            )

        zone = self._zone_for(tier)
        session_id = self._session_id(canonical_id)
        username = self._build_username(zone.zone_name, country, session_id)
        return ProxyConfig(
            tier=tier,
            server=f"http://{BRIGHTDATA_HOST}:{BRIGHTDATA_PORT}",
            username=username,
            password=zone.password,
            session_id=session_id,
        )

    def _session_id(self, canonical_id: str) -> str:
        # Stable short hash: retries on the same property stick to the same IP
        # up to the provider's session TTL. hashlib, never built-in hash().
        digest = hashlib.sha256(canonical_id.encode()).hexdigest()
        return f"s{digest[:10]}"

    def _build_username(self, zone: str, country: str, session_id: str) -> str:
        # brd-customer-{id}-zone-{zone}-country-{cc}-session-{sid}
        return (
            f"brd-customer-{self.customer_id}"
            f"-zone-{zone}"
            f"-country-{country.lower()}"
            f"-session-{session_id}"
        )
