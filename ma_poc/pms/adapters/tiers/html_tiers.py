"""HTML-based extraction tiers: EmbeddedJson, JsonLd, DomScan."""

from __future__ import annotations

import time
from typing import Any


class EmbeddedJsonTier:
    """generic:embedded_json — SSR script tag extraction.

    Scans the page HTML for __NEXT_DATA__, __NUXT__, and window globals,
    then parses them with the daily-runner broad parser. Never raises.
    """

    @staticmethod
    def run(
        html: str,
        result: Any,
        log_attempt: Any,
        extract_embedded_blobs: Any,
        parse_api_responses: Any,
    ) -> list[dict[str, Any]]:
        """Run embedded JSON extraction.

        Returns a list of extracted units (may be empty). Side-effects:
        appends to result.errors on parse errors. Never raises.
        """
        t0 = time.monotonic()
        embedded_units: list[dict[str, Any]] = []

        try:
            embedded = extract_embedded_blobs(html)
            if embedded:
                try:
                    embedded_units = parse_api_responses(embedded) or []
                except Exception as exc:
                    embedded_units = []
                    result.errors.append(f"embedded-parse-error: {exc}")
            else:
                embedded = []
        except Exception:
            embedded = []

        log_attempt(
            "generic:embedded_json",
            "ran_units" if embedded_units else ("ran_empty" if embedded else "skipped"),
            units=len(embedded_units),
            reason=""
            if embedded_units
            else (
                f"{len(embedded)} SSR blob(s) had no unit signals"
                if embedded
                else "no __NEXT_DATA__/__NUXT__/window globals in HTML"
            ),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )

        return embedded_units


class JsonLdTier:
    """generic:jsonld — JSON-LD parser.

    Extracts Apartment/Offer schema from page HTML. Rejects results that
    contain only floor-plan names with no rent/sqft. Never raises.
    """

    @staticmethod
    def run(
        html: str,
        base_url: str,
        log_attempt: Any,
        extract_jsonld: Any,
    ) -> list[dict[str, Any]]:
        """Run JSON-LD extraction.

        Returns a list of extracted units (may be empty). Never raises.
        """
        t0 = time.monotonic()
        attempts_before = []  # placeholder — caller checks tier_key in attempts

        try:
            jsonld_units = extract_jsonld(html, base_url)
        except Exception:
            jsonld_units = []

        # Reject JSON-LD that only has floor-plan names (no rent/sqft).
        if jsonld_units:
            has_rent = any(
                u.get("market_rent_low") or u.get("market_rent_high") or u.get("rent_range")
                for u in jsonld_units
            )
            has_size = any(u.get("sqft") for u in jsonld_units)
            if not has_rent and not has_size:
                log_attempt(
                    "generic:jsonld",
                    "ran_empty",
                    units=len(jsonld_units),
                    reason="JSON-LD had floor-plan names only (no rent/sqft) — falling through",
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )
                return []

        if jsonld_units:
            log_attempt(
                "generic:jsonld",
                "ran_units",
                units=len(jsonld_units),
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
        else:
            log_attempt(
                "generic:jsonld",
                "ran_empty",
                reason="no Apartment/Offer schema in HTML",
                duration_ms=int((time.monotonic() - t0) * 1000),
            )

        return jsonld_units or []


class DomScanTier:
    """generic:dom_scan — CSS-selector DOM cascade.

    Scans container elements (.unit, .floor-plan, .pricing-card, …) for
    visible rent + structural signals. Catches static HTML sites where unit
    data lives in markup, not JSON. Passes saved field_selectors as hints
    when available. Never raises.
    """

    @staticmethod
    def run(
        html: str,
        base_url: str,
        profile: Any,
        result: Any,
        log_attempt: Any,
        extract_units_from_dom: Any,
    ) -> tuple[list[dict[str, Any]], bool, bool]:
        """Run DOM scan extraction.

        Returns (units, hints_attempted, hints_hit). Side-effects: appends
        to result.errors on parse errors. Never raises.
        """
        t0 = time.monotonic()
        dom_hints = None
        hints_attempted = False

        try:
            if profile is not None:
                fs = profile.dom_hints.field_selectors
                if fs and getattr(fs, "container", None):
                    dom_hints = fs
            hints_attempted = dom_hints is not None
        except Exception:
            dom_hints = None
            hints_attempted = False

        try:
            dom_units, _dom_hit_mode = extract_units_from_dom(html, base_url, hints=dom_hints)
        except Exception as exc:
            dom_units, _dom_hit_mode = [], "none"
            result.errors.append(f"dom-scan-error: {exc}")

        hints_hit = _dom_hit_mode == "hints"

        log_attempt(
            "generic:dom_scan",
            "ran_units" if dom_units else "ran_empty",
            units=len(dom_units),
            reason="" if dom_units else "no DOM container matched rent + structural signals",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )

        return dom_units, hints_attempted, hints_hit
