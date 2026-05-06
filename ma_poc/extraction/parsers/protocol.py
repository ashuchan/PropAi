"""UnitParser protocol — structural interface for all host-specific parsers."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class UnitParser(Protocol):
    """Structural protocol for unit-data parsers.

    Each concrete parser wraps one pure parsing function and translates
    arbitrary raw API bodies into a list of normalised unit dicts.

    Contract:
    - Never raises — returns [] on any error or empty input.
    - ``body`` may be any JSON-deserialised type (dict, list, None, …).
    - ``url`` is the source endpoint URL (used for tier tagging / logging).
    """

    def parse(self, body: Any, url: str) -> list[dict]: ...
