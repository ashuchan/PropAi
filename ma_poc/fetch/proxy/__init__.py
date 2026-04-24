"""Tiered proxy abstraction for the L1 Fetch layer.

Public API:
    ProxyTier, ProxyConfig, ProxyProvider  (base)
    BrightDataProvider                     (Bright Data implementation)
    NoneProvider                           (direct-connection, no proxy)
    ProxySelector, ProxyOverride           (tier picker)
    EscalationDecision, decide             (tier-change decision)
    estimate_cost_usd, USD_PER_GB          (cost estimation)
"""

from ma_poc.fetch.proxy.base import (
    ProxyConfig,
    ProxyProvider,
    ProxyTier,
)
from ma_poc.fetch.proxy.brightdata import BrightDataProvider
from ma_poc.fetch.proxy.escalation import (
    DEESCALATE_SUCCESS_STREAK,
    ESCALATE_IMMEDIATELY_ON_SIGNAL,
    ESCALATE_IMMEDIATELY_ON_STATUS,
    TIMEOUT_FAIL_STREAK_THRESHOLD,
    EscalationDecision,
    decide,
)
from ma_poc.fetch.proxy.none_provider import NoneProvider
from ma_poc.fetch.proxy.pricing import USD_PER_GB, estimate_cost_usd
from ma_poc.fetch.proxy.selector import ProxyOverride, ProxySelector

__all__ = [
    "DEESCALATE_SUCCESS_STREAK",
    "ESCALATE_IMMEDIATELY_ON_SIGNAL",
    "ESCALATE_IMMEDIATELY_ON_STATUS",
    "TIMEOUT_FAIL_STREAK_THRESHOLD",
    "USD_PER_GB",
    "BrightDataProvider",
    "EscalationDecision",
    "NoneProvider",
    "ProxyConfig",
    "ProxyOverride",
    "ProxyProvider",
    "ProxySelector",
    "ProxyTier",
    "decide",
    "estimate_cost_usd",
]
