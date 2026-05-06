"""generic:api_narrow — narrow generic API parser tier."""

from __future__ import annotations

import time
from typing import Any


class ApiNarrowTier:
    """Sub-tier 1: narrow generic API parser.

    Tries to find a unit list in each API response and parse it with
    parse_generic_api. Only processes responses that pass the unit-signal
    heuristic to avoid wasting time on analytics/config payloads.
    """

    @staticmethod
    def run(
        api_responses: list[dict[str, Any]],
        result: Any,
        log_attempt: Any,
        find_unit_list: Any,
        has_unit_signals: Any,
        parse_api: Any,
    ) -> list[dict[str, Any]]:
        """Run the narrow API parser.

        Returns a list of extracted units (may be empty). Side-effects:
        appends matching api_responses to result.api_responses.
        Never raises.
        """
        t0 = time.monotonic()
        all_units: list[dict[str, Any]] = []

        try:
            for resp in api_responses:
                body = resp.get("body")
                items = find_unit_list(body)
                if items and has_unit_signals(items):
                    url = resp.get("url", "")
                    try:
                        units = parse_api(items, url)
                    except Exception:
                        units = []
                    if units:
                        all_units.extend(units)
                        result.api_responses.append(resp)
        except Exception:
            pass

        elapsed = int((time.monotonic() - t0) * 1000)

        if api_responses:
            log_attempt(
                "generic:api_narrow",
                "ran_units" if all_units else "ran_empty",
                units=len(all_units),
                reason="" if all_units else "no items matched unit-signal heuristic",
                duration_ms=elapsed,
            )
        else:
            log_attempt(
                "generic:api_narrow",
                "skipped",
                reason="no captured API responses",
                duration_ms=0,
            )

        return all_units
