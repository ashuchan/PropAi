"""
Integration helpers for daily_runner <-> PMS detection pipeline.

Provides pure-function utilities that mirror the profile-update and
report-enrichment logic that daily_runner.py performs after each scrape.
These are designed to be tested in isolation without launching Playwright.
"""

from __future__ import annotations

from typing import Any

# Minimum confidence required to overwrite a profile's api_provider.
PMS_CONFIDENCE_THRESHOLD = 0.80


def update_profile_from_scrape(
    profile_data: dict[str, Any],
    scrape_result: dict[str, Any],
) -> dict[str, Any]:
    """Apply PMS detection results from a scrape to a property profile.

    Rules:
      - If ``scrape_result["_detected_pms"]["pms"]`` exists and its
        ``confidence >= PMS_CONFIDENCE_THRESHOLD``, the profile's
        ``api_hints.api_provider`` is updated.
      - If confidence is below the threshold, the existing
        ``api_hints.api_provider`` is preserved.
      - If ``_detected_pms`` key is missing entirely, the profile is
        returned unchanged (no crash).

    Parameters
    ----------
    profile_data:
        A dict representation of the property profile.  Must contain
        at least ``{"api_hints": {"api_provider": ...}}``.
    scrape_result:
        The dict returned by ``pms.scraper.scrape()`` or
        ``scripts.entrata.scrape()``.

    Returns
    -------
    dict
        The (possibly updated) profile_data.  Mutates in place and
        also returns the reference for convenience.
    """
    detected = scrape_result.get("_detected_pms")
    if not isinstance(detected, dict):
        # Key missing or not a dict -- leave profile untouched.
        return profile_data

    pms = detected.get("pms")
    confidence = detected.get("confidence", 0.0)

    if pms and confidence >= PMS_CONFIDENCE_THRESHOLD:
        profile_data.setdefault("api_hints", {})["api_provider"] = pms
    # else: keep existing api_provider

    return profile_data


def add_pms_metrics_to_report(
    report: dict[str, Any],
    scrape_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Enrich a run report with PMS-breakdown metrics.

    Adds two keys under ``report["pms"]``:

    ``properties_by_pms``
        ``{pms_name: count}`` -- how many properties were detected as
        each PMS platform.

    ``llm_cost_by_pms``
        ``{pms_name: total_cost_usd}`` -- aggregate LLM spend per
        detected PMS, computed from ``_llm_interactions`` in each
        scrape result.

    Parameters
    ----------
    report:
        The run report dict (mutated in place).
    scrape_results:
        List of per-property scrape result dicts.

    Returns
    -------
    dict
        The enriched report.
    """
    properties_by_pms: dict[str, int] = {}
    llm_cost_by_pms: dict[str, float] = {}

    for sr in scrape_results:
        detected = sr.get("_detected_pms")
        if isinstance(detected, dict):
            pms = detected.get("pms", "unknown")
        else:
            pms = "unknown"

        properties_by_pms[pms] = properties_by_pms.get(pms, 0) + 1

        llm_interactions = sr.get("_llm_interactions") or []
        cost = sum(i.get("cost_usd", 0.0) for i in llm_interactions)
        llm_cost_by_pms[pms] = llm_cost_by_pms.get(pms, 0.0) + cost

    report["pms"] = {
        "properties_by_pms": properties_by_pms,
        "llm_cost_by_pms": llm_cost_by_pms,
    }
    return report
