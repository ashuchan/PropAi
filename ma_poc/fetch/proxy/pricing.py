"""Per-GB rates by tier for cost estimation.

These are ESTIMATES — authoritative cost comes from Bright Data's dashboard.
Used only for real-time visibility and budget alerts. Update when the
vendor invoice changes.
"""

from __future__ import annotations

from ma_poc.fetch.proxy.base import ProxyTier

USD_PER_GB: dict[ProxyTier, float] = {
    ProxyTier.DIRECT: 0.0,
    ProxyTier.DATACENTER: 0.50,
    ProxyTier.RESIDENTIAL: 6.00,
    # UNBLOCKER bills per request, not per GB — handled separately when wired.
    ProxyTier.UNBLOCKER: 0.0,
}


def estimate_cost_usd(tier: ProxyTier, bytes_transferred: int) -> float:
    """Estimate bandwidth cost in USD for a single fetch."""
    if bytes_transferred <= 0:
        return 0.0
    return (bytes_transferred / 1e9) * USD_PER_GB[tier]
