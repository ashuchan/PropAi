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
    # Clean "2a" tier: a REAL (non-stealth) browser rendering through a
    # residential proxy that passes JS challenges by waiting and ABORTS on
    # interactive captchas — never a solver. Appended (not inserted) to honor
    # "never reorder"; its ladder position is set explicitly in
    # tier_escalator._build_ladder (between RESIDENTIAL and FLARESOLVERR),
    # and its high int value simply means it is always >= any tier floor —
    # i.e. always available when its flag is on. See
    # fetch/providers/residential_render.py for the legal posture.
    RESIDENTIAL_RENDER = 7
