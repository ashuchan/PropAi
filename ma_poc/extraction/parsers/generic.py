"""GenericApiParser — wraps entrata.parse_api_responses().

parse_api_responses() expects a list[dict] of {"url": ..., "body": ...} records
and returns normalised unit dicts. This parser adapts it to the UnitParser
protocol: parse(body, url) where body is the list of api-response dicts.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Ensure scripts/ is importable (same pattern as extraction/engine/phases/).
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from entrata import parse_api_responses  # type: ignore[import]


class GenericApiParser:
    """Wraps ``parse_api_responses`` from scripts/entrata.py.

    ``body`` is expected to be a ``list[dict]`` of api-response records
    (each with ``url`` and ``body`` keys).  Any other type silently returns [].
    """

    def parse(self, body: Any, url: str) -> list[dict]:
        try:
            if not isinstance(body, list):
                return []
            return parse_api_responses(body) or []
        except Exception:
            return []
