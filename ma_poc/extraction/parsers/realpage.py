"""RealPageParser — wraps scrape_properties._realpage_units_from_body().

Handles RealPage API responses (api.ws.realpage.com /units endpoint).
Returns the *internal* shape (unit_id / market_rent_low / market_rent_high).
Callers that need adapter shape should use
``pms.adapters._daily_runner_parsers.realpage_units_to_adapter_shape`` or
translate manually.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Ensure scripts/ is importable (same pattern as extraction/engine/phases/).
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from scrape_properties import _realpage_units_from_body  # type: ignore[import]


class RealPageParser:
    """Wraps ``_realpage_units_from_body`` from scripts/scrape_properties.py.

    ``body`` must be the parsed JSON dict from a RealPage API endpoint.
    Any non-dict body or null /units response silently returns [].
    """

    def parse(self, body: Any, url: str) -> list[dict]:
        try:
            if not isinstance(body, dict):
                return []
            return _realpage_units_from_body(body, url) or []
        except Exception:
            return []
