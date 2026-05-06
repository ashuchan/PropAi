"""generic:api_broad — broad parser + host-specific (SightMap/RealPage) tier."""

from __future__ import annotations

import time
from typing import Any


class ApiBroadTier:
    """Sub-tier 2: broad generic API parser + host-specific parsers.

    Runs after ApiNarrowTier when narrow parsing produced no units.
    Tries SightMap and RealPage host-specific parsers first, then falls
    back to the daily-runner broad parser. Never raises.
    """

    @staticmethod
    def run(
        api_responses: list[dict[str, Any]],
        result: Any,
        log_attempt: Any,
        parse_sightmap: Any,
        parse_api_responses: Any,
    ) -> list[dict[str, Any]]:
        """Run the broad API parser.

        Returns a list of extracted units (may be empty). Side-effects:
        appends matching api_responses to result.api_responses and
        result.errors for parse errors. Never raises.
        """
        t0 = time.monotonic()
        all_units: list[dict[str, Any]] = []

        try:
            for resp in api_responses:
                url = resp.get("url") or ""
                body = resp.get("body")
                host_units: list[dict[str, Any]] = []
                if body is not None and "sightmap.com" in url.lower():
                    try:
                        host_units = parse_sightmap(body, url) or []
                    except Exception as exc:
                        result.errors.append(f"sightmap-parse-error: {exc}")
                if host_units:
                    all_units.extend(host_units)
                    result.api_responses.append(resp)
        except Exception:
            pass

        if not all_units:
            try:
                broad = parse_api_responses(list(api_responses)) or []
            except Exception as exc:
                broad = []
                result.errors.append(f"daily-runner-parser-error: {exc}")
            if broad:
                all_units.extend(broad)
                # parse_api_responses tags each unit with source_api_url;
                # surface the first as winning_url if we don't have one.
                if not result.api_responses:
                    first_url = next(
                        (u.get("source_api_url") for u in broad if u.get("source_api_url")),
                        None,
                    )
                    if first_url:
                        for resp in api_responses:
                            if resp.get("url") == first_url:
                                result.api_responses.append(resp)
                                break

        log_attempt(
            "generic:api_broad",
            "ran_units" if all_units else "ran_empty",
            units=len(all_units),
            reason="" if all_units else "broad parser + host-specific found no units",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )

        return all_units
