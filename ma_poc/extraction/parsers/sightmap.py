"""SightMapParser — wraps entrata._parse_sightmap_payload().

Handles sightmap.com API responses: joins units[] to floor_plans[] by
floor_plan_id so each unit gets name/beds/baths plus price/sqft/availability.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Ensure scripts/ is importable (same pattern as extraction/engine/phases/).
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from entrata import _parse_sightmap_payload  # type: ignore[import]


class SightMapParser:
    """Wraps ``_parse_sightmap_payload`` from scripts/entrata.py.

    ``body`` must be the parsed JSON dict from a sightmap.com API endpoint.
    Any non-dict or malformed body silently returns [].
    """

    def parse(self, body: Any, url: str) -> list[dict]:
        try:
            if not isinstance(body, dict):
                return []
            return _parse_sightmap_payload(body, url) or []
        except Exception:
            return []
