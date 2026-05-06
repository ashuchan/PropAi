"""LLM-based extraction tiers: LlmApiTargeted, LlmDomTargeted, LlmMonolithic."""

from __future__ import annotations

import time
from typing import Any


class LlmApiTargetedTier:
    """generic:llm_api_targeted — analyze_api_with_llm (max 3 calls).

    For each captured API response with unit-like signals that the
    deterministic parsers couldn't unwrap, asks the LLM to both extract
    units AND return json_paths + response_envelope for deterministic
    replay on the next run. Never raises.
    """

    @staticmethod
    async def run(
        api_responses: list[dict[str, Any]],
        property_context: dict[str, Any],
        property_id: str,
        api_llm_budget: int,
        result: Any,
        log_attempt: Any,
        find_unit_list: Any,
        has_unit_signals: Any,
        analyze_api_with_llm: Any,
        llm_interactions: list[dict[str, Any]],
        llm_field_mappings: list[dict[str, Any]],
        llm_analysis_results: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Run targeted API LLM analysis.

        Returns a list of extracted units (may be empty). Side-effects:
        appends to result.errors, llm_interactions, llm_field_mappings,
        and llm_analysis_results. Never raises.
        """
        targeted_units: list[dict[str, Any]] = []

        if not api_responses or api_llm_budget <= 0:
            log_attempt(
                "generic:llm_api_targeted",
                "skipped",
                units=0,
                reason="no API responses with unit signals",
                duration_ms=0,
            )
            return targeted_units

        t0 = time.monotonic()
        api_calls_made = 0

        try:
            for resp in api_responses:
                if api_calls_made >= api_llm_budget:
                    break
                body = resp.get("body")
                items = find_unit_list(body)
                if not items or not has_unit_signals(items):
                    continue
                url = resp.get("url", "")
                try:
                    units, mapping, is_noise, interaction = await analyze_api_with_llm(
                        resp,
                        property_context,
                        property_id,
                    )
                except Exception as exc:
                    result.errors.append(f"api-analysis-error: {exc}")
                    continue
                api_calls_made += 1
                if interaction:
                    llm_interactions.append(interaction)
                if is_noise:
                    llm_analysis_results[url] = "noise:no_unit_data"
                    continue
                if units:
                    targeted_units.extend(units)
                    if mapping:
                        llm_field_mappings.append(mapping)
                        llm_analysis_results[url] = mapping
                        if not result.api_responses:
                            result.api_responses.append(resp)
        except Exception:
            pass

        log_attempt(
            "generic:llm_api_targeted",
            "ran_units" if targeted_units else ("ran_empty" if api_calls_made else "skipped"),
            units=len(targeted_units),
            reason=""
            if targeted_units
            else (
                f"{api_calls_made} API(s) analysed, no units"
                if api_calls_made
                else "no API responses with unit signals"
            ),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )

        return targeted_units


class LlmDomTargetedTier:
    """generic:llm_dom_targeted — analyze_dom_with_llm (1 call per property).

    Extracts the rent-bearing DOM section (not the full page) and asks
    the LLM to return units AND CSS selectors for deterministic replay.
    Never raises.
    """

    @staticmethod
    async def run(
        html: str | None,
        base_url: str,
        property_context: dict[str, Any],
        property_id: str,
        dom_llm_budget: int,
        result: Any,
        log_attempt: Any,
        extract_rent_dom_section: Any,
        analyze_dom_with_llm: Any,
        llm_interactions: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], Any]:
        """Run targeted DOM LLM analysis.

        Returns (units, selectors). Side-effects: appends to result.errors
        and llm_interactions. Never raises.
        """
        dom_units: list[dict[str, Any]] = []
        selectors = None

        if not html or dom_llm_budget <= 0:
            return dom_units, selectors

        dom_section_html = None
        try:
            dom_section_html = extract_rent_dom_section(html)
        except Exception:
            pass

        if not dom_section_html:
            return dom_units, selectors

        t0 = time.monotonic()
        try:
            dom_units, selectors, interaction = await analyze_dom_with_llm(
                dom_section_html,
                base_url,
                property_context,
                property_id,
            )
        except Exception as exc:
            result.errors.append(f"dom-analysis-error: {exc}")
            dom_units, selectors, interaction = [], None, None

        if interaction:
            llm_interactions.append(interaction)

        log_attempt(
            "generic:llm_dom_targeted",
            "ran_units" if dom_units else "ran_empty",
            units=len(dom_units or []),
            reason="" if dom_units else "targeted DOM LLM returned no units",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )

        return dom_units or [], selectors


class LlmMonolithicTier:
    """generic:llm — monolithic LLM fallback.

    Only fires when targeted tiers (api + dom) both returned empty.
    Sends full HTML + top-3 APIs to the LLM — broadest coverage, highest
    token cost, runs last. Never raises.
    """

    @staticmethod
    async def run(
        html: str,
        api_responses: list[dict[str, Any]],
        property_context: dict[str, Any],
        property_id: str,
        result: Any,
        log_attempt: Any,
        prepare_llm_input: Any,
        extract_with_llm: Any,
        llm_interactions: list[dict[str, Any]],
        llm_navigation_hints: list[str],
    ) -> list[dict[str, Any]]:
        """Run monolithic LLM extraction.

        Returns a list of extracted units (may be empty). Side-effects:
        appends to result.errors, llm_interactions, and
        llm_navigation_hints. Never raises.
        """
        t0 = time.monotonic()
        llm_units: list[dict[str, Any]] = []

        try:
            llm_input = prepare_llm_input(html, api_responses, property_context)
            llm_units, hints, _raw, interaction = await extract_with_llm(
                llm_input,
                property_id=property_id,
            )
            if interaction:
                llm_interactions.append(interaction)
            if isinstance(hints, dict):
                nav = hints.get("navigation_hint") or ""
                if nav:
                    llm_navigation_hints.append(str(nav))
            log_attempt(
                "generic:llm",
                "ran_units" if llm_units else "ran_empty",
                units=len(llm_units or []),
                reason="" if llm_units else "LLM returned no structured units",
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
            if llm_units:
                return llm_units, hints  # type: ignore[return-value]
        except Exception as exc:
            log_attempt(
                "generic:llm",
                "errored",
                reason=str(exc)[:200],
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
            result.errors.append(f"llm-tier-error: {exc}")

        return [], None  # type: ignore[return-value]
