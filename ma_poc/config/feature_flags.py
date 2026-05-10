"""Central feature-flag registry for the fetch-tier escalation ladder.

All flags are read from environment variables at import time.
Reload the module (importlib.reload) to pick up env changes in tests.
"""

from __future__ import annotations

import os
from typing import Final

ENABLE_TIER_ESCALATION: Final[bool] = (
    os.environ.get("ENABLE_TIER_ESCALATION", "false").lower() == "true"
)

# Provider-tier flags — keyed off the master flag. If master is off, all are off.
ENABLE_DC_PROXY_TIER: Final[bool] = (
    ENABLE_TIER_ESCALATION
    and os.environ.get("ENABLE_DC_PROXY_TIER", "true").lower() == "true"
)
ENABLE_RESIDENTIAL_TIER: Final[bool] = (
    ENABLE_TIER_ESCALATION
    and os.environ.get("ENABLE_RESIDENTIAL_TIER", "false").lower() == "true"
)
ENABLE_UNLOCKER_TIER: Final[bool] = (
    ENABLE_TIER_ESCALATION
    and os.environ.get("ENABLE_UNLOCKER_TIER", "false").lower() == "true"
)


def enable_degraded_mapping_persist() -> bool:
    """PR 1 (2026-05-10): degraded LlmFieldMapping persistence kill switch.

    When True (default), `save_llm_field_mapping` persists mappings that
    have a non-empty ``response_envelope`` even when ``json_paths`` is
    empty (LLM extracted units via semantic understanding without
    articulating per-field paths). Replay on these is a no-op (the cascade
    falls through to other tiers), but the URL itself is now known to the
    profile and can be prioritised by the cascade — and the entry is the
    foundation for an offline LLM-pass that fills in ``json_paths`` later.

    Read each call (not at import) so a flip via env var doesn't require
    a process restart — the next ``save_llm_field_mapping`` call sees
    the new value.
    """
    return os.environ.get("ENABLE_DEGRADED_MAPPING_PERSIST", "true").lower() == "true"
