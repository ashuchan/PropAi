from enum import IntEnum


class FetchTier(IntEnum):
    """Network-cost tiers for the fetch ladder.

    IntEnum so escalation arithmetic (tier + 1) is natural.
    Order is strict: lower = cheaper. Never reorder.
    """

    DIRECT = 0
    STEALTH_LOCAL = 1  # reserved; collapses into DIRECT in v1
    DC_PROXY = 2
    RESIDENTIAL = 3
    UNLOCKER = 4
    DLQ_PARK = 5  # terminal — never actually fetched
