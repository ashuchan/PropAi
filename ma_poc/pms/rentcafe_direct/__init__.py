"""F4-F6 — RentCafe aggregator-API direct path.

Bypasses the per-vanity Cloudflare wall by going to RentCafe's centralized
aggregator endpoints when (a) ``profile.api_provider == "rentcafe"`` and
(b) a propertyId is cached or resolvable.

Public surface:
- :func:`resolve_property_id` — (name, city, zip, vanity_domain) → propertyId
- :func:`fetch_direct` — propertyId → aggregator API responses
- :data:`TIER_CODES` — frozenset of failure-mode tier strings

Falls back to the existing vanity-domain pipeline on any failure (H4).
"""

from __future__ import annotations

from .fetcher import TIER_CODES, DirectFetchResult, fetch_direct
from .propertyid_resolver import ResolveResult, resolve_property_id
from .runner_dispatch import try_rentcafe_direct

__all__ = [
    "DirectFetchResult",
    "ResolveResult",
    "TIER_CODES",
    "fetch_direct",
    "resolve_property_id",
    "try_rentcafe_direct",
]
