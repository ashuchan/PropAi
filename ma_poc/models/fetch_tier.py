from enum import IntEnum


class FetchTier(IntEnum):
    """Network-cost tiers for the fetch ladder.

    IntEnum so escalation arithmetic (tier + 1) is natural.
    Order is strict: lower = cheaper. Never reorder.
    """

    DIRECT = 0
    STEALTH_LOCAL = 1   # reserved; collapses into DIRECT in v1
    DC_PROXY = 2
    RESIDENTIAL = 3
    # FlareSolverr (3.5 cost-wise): local Docker service that solves CF JS
    # challenges using real undetected Chrome. Free per-request but requires
    # a running FlareSolverr instance (ENABLE_FLARESOLVERR_TIER=true).
    # Sits between RESIDENTIAL and UNLOCKER; tried when BOT_BLOCKED body
    # contains CF JS challenge patterns.
    FLARESOLVERR = 4
    UNLOCKER = 5
    DLQ_PARK = 6        # terminal — never actually fetched
