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


class BrightDataProvider:
    """Provider that constructs per-request proxy configs for Bright Data.

    Required env vars:
        BRIGHTDATA_CUSTOMER_ID
        BRIGHTDATA_DC_ZONE, BRIGHTDATA_DC_PASSWORD
        BRIGHTDATA_RESI_ZONE, BRIGHTDATA_RESI_PASSWORD

    UNBLOCKER is a future handoff and raises NotImplementedError.
    """

    def __init__(self) -> None:
        self.customer_id = self._require("BRIGHTDATA_CUSTOMER_ID")
        self.zones: dict[ProxyTier, BrightDataZone] = {
            ProxyTier.DATACENTER: BrightDataZone(
                zone_name=self._require("BRIGHTDATA_DC_ZONE"),
                password=self._require("BRIGHTDATA_DC_PASSWORD"),
            ),
            ProxyTier.RESIDENTIAL: BrightDataZone(
                zone_name=self._require("BRIGHTDATA_RESI_ZONE"),
                password=self._require("BRIGHTDATA_RESI_PASSWORD"),
            ),
        }

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
                "UNBLOCKER tier requires Web Unlocker integration — future handoff"
            )

        zone = self.zones[tier]
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
