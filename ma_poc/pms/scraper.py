"""
Thin scraper orchestrator (Phase 5 + Jugnu J3 deltas).

Wires together detection -> resolution -> adapter extraction into a single
``scrape()`` coroutine that returns a legacy-compatible result dict augmented
with new detection/adapter metadata keys.

Jugnu deltas applied:
- Delta 2: scrape() accepts CrawlTask + FetchResult, does not fetch
- Delta 3: tier_used uses adapter:tier_key namespace
- Delta 4: event emission via observability.events
- Delta 7: cost accounting on ExtractResult
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.registry import get_adapter
from ma_poc.pms.detector import (
    DetectedPMS,
    collect_detector_signals,
    confirm_detection,
    detect_pms,
)
from ma_poc.pms.resolver import ResolvedTarget, resolve_target, resolve_target_from_html

if TYPE_CHECKING:
    pass  # Playwright Page type used only in type annotations

log = logging.getLogger(__name__)

# Network errors that indicate the site is unreachable — no point retrying
# or running any extraction tiers.
_UNREACHABLE_PATTERNS: tuple[str, ...] = (
    "ERR_SSL_PROTOCOL_ERROR",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_CONNECTION_REFUSED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_CONNECTION_RESET",
    "ERR_CERT_AUTHORITY_INVALID",
    "ERR_CERT_DATE_INVALID",
    "NS_ERROR_UNKNOWN_HOST",
    "net::ERR_",
)

_HTTPS_RE = re.compile(r"^http://", re.IGNORECASE)


def _normalize_url(url: str) -> str:
    """Ensure the URL uses https."""
    return _HTTPS_RE.sub("https://", url.strip())


def _hostname(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).hostname or url
    except Exception:
        return url


def _empty_result(base_url: str) -> dict[str, Any]:
    """Return the legacy result dict shape with all keys present."""
    return {
        "scraped_at": datetime.now(UTC).isoformat(),
        "property_name": _hostname(base_url),
        "base_url": base_url,
        "links_found": [],
        "property_links_crawled": [],
        "api_calls_intercepted": [],
        "units": [],
        "extraction_tier_used": None,
        "errors": [],
        "_property_id": "unknown",
        "_llm_interactions": [],
        "_detected_pms": {},
        "_resolved_target": {},
        "_adapter_used": "",
        "_fallback_chain": [],
    }


def _is_unreachable_error(error: Exception | str) -> bool:
    """Check if an error indicates the site is unreachable."""
    msg = str(error)
    return any(pat in msg for pat in _UNREACHABLE_PATTERNS)


def _detection_to_dict(det: DetectedPMS) -> dict[str, Any]:
    return {
        "pms": det.pms,
        "confidence": det.confidence,
        "evidence": list(det.evidence),
        "pms_client_account_id": det.pms_client_account_id,
        "recommended_strategy": det.recommended_strategy,
    }


def _resolved_to_dict(res: ResolvedTarget) -> dict[str, Any]:
    return {
        "original_url": res.original_url,
        "resolved_url": res.resolved_url,
        "hop_path": list(res.hop_path),
        "method": res.method,
        "final_detection": _detection_to_dict(res.final_detection),
    }


async def scrape(
    base_url: str,
    proxy: str | None = None,
    profile: Any | None = None,
    expected_total_units: int | None = None,
    *,
    page: Any | None = None,
    api_responses: list[dict[str, Any]] | None = None,
    fetch_result: Any | None = None,
    csv_row: dict[str, Any] | None = None,
    property_id: str | None = None,
    shared_budget: dict | None = None,
) -> dict[str, Any]:
    """Scrape a property URL through detect -> resolve -> adapt pipeline.

    Parameters
    ----------
    base_url : str
        Property marketing site URL.
    proxy : str | None
        Proxy URL (unused by this orchestrator; passed through for future use).
    profile : Any | None
        ScrapeProfile from the caller (forwarded to adapter context).
    expected_total_units : int | None
        Hint for expected unit count (forwarded to adapter context).
    page : Page | None
        Pre-created Playwright page for testing. If None, the orchestrator
        creates one internally (not yet implemented — callers must provide).
    api_responses : list[dict] | None
        Pre-captured API responses for testing. If None, uses whatever the
        page captured during load.

    Returns
    -------
    dict
        Legacy-compatible result dict with additional detection metadata.
    """
    base_url = _normalize_url(base_url)
    result = _empty_result(base_url)
    if property_id:
        result["_property_id"] = property_id
    fallback_chain: list[str] = []

    # --- Step 1: Initial offline detection from URL + CSV mgmt-prior ---
    # csv_row threads in the Management Company so MGMT_TO_PMS_PRIOR can fire
    # on vanity domains where URL alone gives no PMS signal.
    initial_detection = detect_pms(base_url, csv_row=csv_row)
    result["_detected_pms"] = _detection_to_dict(initial_detection)

    # --- Step 2: Navigate page (or use provided one) ---
    # Jugnu path: page may be None but fetch_result.body may carry raw HTML.
    # Adapters (via _get_page_html) now handle both modes — continue to
    # dispatch so HTML-only extractors can still run. Only short-circuit
    # when we have neither HTML nor pre-captured API responses (the LLM
    # rescue path works from api_responses alone, no page HTML needed).
    page_html: str | None = None
    if page is None and fetch_result is None and not api_responses:
        result["errors"].append("no page, no fetch_result, no api_responses provided")
        return result

    # --- Step 3: Check for unreachable errors ---
    # The page object may carry navigation errors from the caller.
    if page is not None:
        try:
            page_html = await page.content() if hasattr(page, "content") else None
        except Exception as exc:
            if _is_unreachable_error(exc):
                result["errors"].append(f"FAILED_UNREACHABLE: {exc}")
                return result
            page_html = None

    # Fall back to fetch_result.body if page didn't give us HTML.
    if not page_html and fetch_result is not None:
        body = getattr(fetch_result, "body", None)
        if isinstance(body, bytes):
            try:
                page_html = body.decode("utf-8", errors="replace")
            except Exception:
                page_html = None
        elif isinstance(body, str):
            page_html = body

    # Patch #6 (2026-05-06 audit) — detect cross-host redirects to a
    # different property (Pearl Midlane → Briscoe River Oaks class).
    # We don't suppress extraction here — the verdict pipeline handles
    # demotion — but we surface the redirect on the result so the report
    # shows it and downstream rules (e.g. cross_run_sanity) can flag it.
    if fetch_result is not None:
        _final_url = getattr(fetch_result, "final_url", "") or base_url
        _target_name = ""
        if csv_row:
            for k in ("Property Name", "name", "Name", "proj_name"):
                v = csv_row.get(k) if isinstance(csv_row, dict) else None
                if v:
                    _target_name = str(v).strip()
                    break
        _suspicious, _reason = is_suspicious_cross_host_redirect(
            base_url, _final_url, target_name=_target_name
        )
        if _suspicious:
            result["_suspicious_redirect"] = {
                "input_url": base_url,
                "final_url": _final_url,
                "reason": _reason,
            }
            result["errors"].append(
                f"SUSPICIOUS_REDIRECT: {_reason}. Extracted units may belong "
                f"to a different property."
            )

    # --- Step 4: Re-detect with page HTML if available ---
    if page_html:
        html_detection = detect_pms(base_url, csv_row=csv_row, page_html=page_html)
        if html_detection.confidence > initial_detection.confidence:
            initial_detection = html_detection
            result["_detected_pms"] = _detection_to_dict(initial_detection)

    # --- Telemetry A: detector signals ----------------------------------------
    # Attach raw detector inputs to the result so the per-property report can
    # render them, and emit DETECTOR_SIGNALS for ledger-level analytics.
    try:
        _signals = collect_detector_signals(base_url, csv_row, page_html)
        result["_detector_signals"] = _signals
        try:
            from ma_poc.observability.events import EventKind, emit

            emit(EventKind.DETECTOR_SIGNALS, result.get("_property_id") or "unknown", **_signals)
        except Exception:
            pass  # observability is best-effort
    except Exception:
        pass

    # --- Telemetry C: HTML characterization ----------------------------------
    # One-shot sketch of what we actually got back. Distinguishes a 200-OK
    # JS shell ("2KB of markup, 500KB of scripts, zero rent signals") from a
    # real SSR page. Rendered in the report, emitted to the ledger.
    if page_html:
        try:
            _html_char = _characterize_html(page_html)
            result["_html_characterization"] = _html_char
            try:
                from ma_poc.observability.events import EventKind, emit

                emit(EventKind.HTML_CHARACTERIZED, result.get("_property_id") or "unknown", **_html_char)
            except Exception:
                pass
        except Exception:
            pass

    # --- Step 5: Resolve target (CTA hop / iframe / redirect) ---
    # 2026-05-07 audit fix: resolve_target_from_html is the Jugnu-mode port
    # of resolve_target. The legacy version requires a live Playwright
    # `page` object (page.evaluate to scrape anchors) which Jugnu never
    # passes — so before this port, every smart-link-hop / portal-sublink /
    # candidate-dedup / word-boundary-keyword improvement made on the
    # resolver shipped in legacy mode but never reached production. The
    # 1000-property audit found 850 (85%) detected-as-known-PMS properties
    # that were extracted by some other tier because resolve_target never
    # ran. The HTML port runs steps 1-4 on the fetched body; step 5
    # (post-render redirect) is handled by fetch_result.final_url already.
    resolved: ResolvedTarget
    if page is not None:
        try:
            resolved = await resolve_target(page, base_url, initial_detection)
        except Exception:
            resolved = ResolvedTarget(
                original_url=base_url,
                resolved_url=base_url,
                hop_path=[base_url],
                final_detection=initial_detection,
                method="failed",
            )
    elif page_html:
        # Jugnu-mode: run the same CTA-hop logic against the fetched HTML.
        # Pull iframe srcs from the detector_signals event we already
        # collected, so we don't re-parse iframes from the body.
        _det_iframe_srcs = (result.get("_detector_signals") or {}).get("iframe_srcs_sample") or []
        try:
            resolved = resolve_target_from_html(
                page_html=page_html,
                original_url=base_url,
                initial_detection=initial_detection,
                iframe_srcs=list(_det_iframe_srcs) if _det_iframe_srcs else None,
            )
        except Exception:
            resolved = ResolvedTarget(
                original_url=base_url,
                resolved_url=base_url,
                hop_path=[base_url],
                final_detection=initial_detection,
                method="failed",
            )
    else:
        resolved = ResolvedTarget(
            original_url=base_url,
            resolved_url=base_url,
            hop_path=[base_url],
            final_detection=initial_detection,
            method="fetch_only",
        )
    result["_resolved_target"] = _resolved_to_dict(resolved)

    # Use the final detection from resolver (may have improved via hop)
    detection = resolved.final_detection

    # --- Step 6: Get adapter ---
    pms_name = detection.pms
    adapter = get_adapter(pms_name)
    adapter_name = getattr(adapter, "pms_name", "unknown")
    result["_adapter_used"] = adapter_name
    fallback_chain.append(adapter_name)

    # --- Step 7: Build context and extract ---
    # Phase 2: surface CSV metadata on the AdapterContext so the LLM prompt
    # (and any future context-aware adapter) can reference property name,
    # city, state, and management company. Helper handles the column-name
    # variants that show up across CSV formats.
    def _from_csv(*keys: str) -> str:
        if not csv_row:
            return ""
        for k in keys:
            v = csv_row.get(k)
            if v not in (None, "", "null", "None"):
                return str(v).strip()
        return ""

    expected_units = expected_total_units
    if expected_units is None:
        cu = _from_csv("Total Units", "Total Units (Est.)", "total_units")
        if cu:
            try:
                expected_units = int(float(cu))
            except (ValueError, TypeError):
                expected_units = None

    # Phase H / Fix 8: use shared_budget if provided (avoids double-allocation
    # when scrape_jugnu() has already computed the budget for this run).
    if shared_budget is not None:
        budget: dict = dict(shared_budget)
    else:
        budget = {"llm_api_calls": 3, "llm_dom_calls": 1, "llm_monolithic": 1, "link_hop": 3}
        if profile is not None:
            try:
                from ma_poc.services.source_planner import compute_budget
                from ma_poc.models.scrape_profile import ProfileMaturity
                is_cold = profile.confidence.maturity == ProfileMaturity.COLD
                budget = compute_budget(profile, is_cold=is_cold)
            except Exception:
                pass

    ctx = AdapterContext(
        base_url=resolved.resolved_url,
        detected=detection,
        profile=profile,
        expected_total_units=expected_units,
        property_id=property_id or "unknown",
        fetch_result=fetch_result,
        property_name=_from_csv("name", "Name", "Property Name", "proj_name"),
        city=_from_csv("city", "City"),
        state=_from_csv("state", "State"),
        zip_code=_from_csv("zip", "Zip", "zip_code", "ZIP Code"),
        pmc=_from_csv("Management Company", "pmc"),
        budget=budget,
    )

    # Phase F: populate cluster_key from PMS client account ID on first detection
    if profile is not None and detection is not None:
        pms_client_id = str(getattr(detection, "pms_client_account_id", "") or "")
        if pms_client_id and not profile.cluster_key:
            profile.cluster_key = pms_client_id
            log.info(
                "Cluster key set for %s: %s",
                profile.canonical_id,
                pms_client_id[:30],
            )
    # Attach API responses to context for generic adapter. Prefer the
    # explicit ``api_responses`` arg (tests pass this directly); otherwise
    # promote the L1 fetcher's captured ``network_log`` so adapters can
    # actually find unit APIs on a real RENDER-mode fetch.
    if api_responses is not None:
        ctx._api_responses = api_responses  # type: ignore[attr-defined]
    elif fetch_result is not None:
        network_log = getattr(fetch_result, "network_log", None) or []
        # network_log entries carry {url, status, content_type, body_size,
        # body} but ``body`` is a truncated string. Surface as-is — adapters
        # already handle both string and dict bodies. Parse JSON bodies so
        # the generic parser sees dicts/lists, not stringified payloads.
        import json as _json

        # Patch #7 (2026-05-06 audit) — body-truncation diagnostic for PMS
        # data hosts. NOTE: Phase A replay validation proved that the
        # original premise (sightmap/realpage bodies are widely truncated)
        # is FALSE: sightmap.com had 293 captures with 0 zero-rent
        # outcomes, realpage 87/0. The truncation case is rare. We keep
        # the diagnostic — it's cheap and catches future regressions —
        # but do NOT count it among meaningful recovery patches.
        _PMS_DATA_HOSTS = (
            "sightmap.com",
            "api.rentcafe.com",
            "api.ws.realpage.com",
            "onlineleasing.realpage.com",
            "api.appfolio.com",
            "api.gtmaservices.com",
            "api.entrata.com",
            "fortresstech.io",
            "funnelleasing.com",
            "knck.io",
            "hy.ly",
            "inventory.g5marketingcloud.com",
        )

        prepared: list[dict[str, Any]] = []
        truncation_diagnostics: list[dict[str, Any]] = []
        for entry in network_log:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url", "") or ""
            raw_body = entry.get("body")
            parsed_body: Any = raw_body
            parse_error: str = ""
            if isinstance(raw_body, str) and raw_body.strip().startswith(("{", "[")):
                try:
                    parsed_body = _json.loads(raw_body)
                except Exception as exc:
                    parsed_body = raw_body
                    parse_error = f"{type(exc).__name__}: {str(exc)[:80]}"

            # Patch #7 — detect truncation on known PMS hosts.
            url_lower = url.lower()
            if (
                parse_error
                and isinstance(raw_body, str)
                and any(host in url_lower for host in _PMS_DATA_HOSTS)
            ):
                truncation_diagnostics.append(
                    {
                        "url": url,
                        "body_size": len(raw_body),
                        "body_size_reported": entry.get("body_size"),
                        "parse_error": parse_error,
                        "host_class": next(
                            (h for h in _PMS_DATA_HOSTS if h in url_lower),
                            "",
                        ),
                    }
                )

            prepared.append(
                {
                    "url": url,
                    "body": parsed_body,
                    "status": entry.get("status"),
                    "content_type": entry.get("content_type"),
                    # Patch #7 — propagate parse_error so adapters can
                    # decide whether to re-fetch / fall back rather than
                    # silently returning 0 units.
                    "_body_parse_error": parse_error,
                }
            )
        ctx._api_responses = prepared  # type: ignore[attr-defined]
        if truncation_diagnostics:
            result["_pms_body_truncations"] = truncation_diagnostics

    # --- Step 6b: Router invariant (Change 2) ------------------------------
    # Before we hand control to the detected PMS adapter, ask the detector
    # whether any captured response body actually matches that PMS's
    # envelope. If none do, demote the detection to ``unknown`` and
    # re-select the generic adapter — which runs the full cascade and
    # (per Change 5) the LLM gate. This is the router's guard against
    # URL-based false positives (Windsor sites routed to RentCafe, Vegas
    # sites routed to SightMap) that Change 1's sub-tier codes made
    # diagnosable but didn't fix.
    responses_for_confirm = getattr(ctx, "_api_responses", []) or []
    confirmed_detection = confirm_detection(detection, responses_for_confirm)
    detection_confirmed = confirmed_detection.pms == detection.pms
    result["_detection_confirmed"] = {
        "confirmed": detection_confirmed,
        "initial_pms": detection.pms,
        "final_pms": confirmed_detection.pms,
        "evidence": list(confirmed_detection.evidence),
        "response_count": len(responses_for_confirm),
    }
    if not detection_confirmed:
        detection = confirmed_detection
        ctx.detected = detection
        pms_name = detection.pms
        adapter = get_adapter(pms_name)
        adapter_name = getattr(adapter, "pms_name", "unknown")
        # Overwrite the reported adapter_used and append to the fallback chain
        # so the report shows that the router stepped in.
        result["_adapter_used"] = adapter_name
        fallback_chain.append(adapter_name)
        result["_detected_pms"] = _detection_to_dict(detection)

    adapter_result: AdapterResult
    try:
        adapter_result = await adapter.extract(page, ctx)  # type: ignore[arg-type]
    except Exception as exc:
        if _is_unreachable_error(exc):
            result["errors"].append(f"FAILED_UNREACHABLE: {exc}")
            result["_fallback_chain"] = fallback_chain
            return result
        adapter_result = AdapterResult(errors=[str(exc)])

    # --- F2: LLM rescue for Tier-1 API adapters --------------------------------
    # When the adapter captures API responses but produces no substantive units,
    # hand the bodies to the LLM rescue service. Adapters never import this module.
    try:
        from ma_poc.observability.events import EventKind, emit
        from ma_poc.validation.schema_gate import property_passes_quality_gate

        profile_stats = getattr(getattr(ctx, "profile", None), "stats", None)
        consecutive_rescue_failures = getattr(profile_stats, "consecutive_llm_rescue_failures", 0)
        raw_api_responses = getattr(ctx, "_api_responses", []) or []
        page_unreachable = any("FAILED_UNREACHABLE" in str(e) for e in adapter_result.errors)

        needs_rescue = (
            not property_passes_quality_gate(adapter_result.units)
            and bool(raw_api_responses)
            and pms_name in {"generic", "entrata", "appfolio"}
            and consecutive_rescue_failures < 3
            and not page_unreachable
        )

        if needs_rescue:
            from ma_poc.services.llm_api_rescue import RescueInput, rescue_from_api_responses

            emit(
                EventKind.LLM_RESCUE_ATTEMPTED,
                ctx.property_id,
                source_adapter=pms_name,
                n_candidates=len(raw_api_responses),
            )

            rescue = await rescue_from_api_responses(
                RescueInput(
                    property_id=ctx.property_id,
                    property_context={
                        "name": getattr(ctx, "property_name", ""),
                        "website": ctx.base_url,
                        "city": getattr(ctx, "city", ""),
                        "expected_units": ctx.expected_total_units,
                    },
                    source_adapter=pms_name,
                    api_responses=raw_api_responses,
                    profile_snapshot=(
                        ctx.profile.model_dump(mode="json") if ctx.profile is not None else None
                    ),
                )
            )

            result["_rescue_cost_usd"] = rescue.cost_usd

            # Bridge rescue's per-URL blocklist into _llm_analysis_results so
            # profile_updater actually persists them. profile_updater reads
            # only that dict (looking for "noise:<reason>" sentinels) — until
            # this bridge existed, blocked_endpoints died at the rescue
            # boundary on every run, defeating the whole point of the cache.
            if rescue.blocked_endpoints:
                analysis = result.setdefault("_llm_analysis_results", {})
                for blocked_url, reason in rescue.blocked_endpoints:
                    # Don't clobber a successful-mapping entry for the same URL.
                    if blocked_url in analysis and isinstance(analysis[blocked_url], dict):
                        continue
                    analysis[blocked_url] = f"noise:{reason}"

            if rescue.units:
                adapter_result.units = rescue.units
                adapter_result.tier_used = rescue.tier_used
                if rescue.winning_url:
                    adapter_result.winning_url = rescue.winning_url
                adapter_result.llm_field_mappings = (
                    list(getattr(adapter_result, "llm_field_mappings", [])) + rescue.llm_field_mappings
                )
                adapter_result.blocked_endpoints = list(getattr(adapter_result, "blocked_endpoints", [])) + [
                    {"url_pattern": u, "reason": r} for u, r in rescue.blocked_endpoints
                ]
                adapter_result.confidence = max(getattr(adapter_result, "confidence", 0.0), rescue.confidence)
                emit(
                    EventKind.LLM_RESCUE_SUCCEEDED,
                    ctx.property_id,
                    tier=rescue.tier_used,
                    units=len(rescue.units),
                    cost=rescue.cost_usd,
                )
            else:
                emit(
                    EventKind.LLM_RESCUE_FAILED,
                    ctx.property_id,
                    errors=rescue.errors,
                    cost=rescue.cost_usd,
                )

            result["_rescue_attempted"] = True
            result["_rescue_succeeded"] = bool(rescue.units)
            result["_rescue_n_llm_calls"] = rescue.n_llm_calls
    except Exception as _rescue_exc:
        log.warning("F2 rescue orchestration failed for %s: %s", property_id, _rescue_exc)

    # --- Step 8: Fallback to generic if adapter returned empty ---
    if not adapter_result.units and pms_name != "unknown" and adapter_name != "generic":
        generic = get_adapter("unknown")  # resolves to generic
        generic_name = getattr(generic, "pms_name", "generic")
        fallback_chain.append(generic_name)

        # For detected-PMS failures, skip LLM in generic adapter UNLESS the
        # detected adapter actually returned units (F12). Threading
        # adapter_unit_count lets the generic adapter open the gate when the
        # PMS-specific path produced nothing — recovers ~100 props/run.
        fallback_ctx = AdapterContext(
            base_url=resolved.resolved_url,
            detected=detection,  # keeps original PMS so generic knows to skip LLM
            profile=profile,
            expected_total_units=ctx.expected_total_units,
            property_id=property_id or "unknown",
            fetch_result=fetch_result,
            property_name=ctx.property_name,
            city=ctx.city,
            state=ctx.state,
            zip_code=ctx.zip_code,
            pmc=ctx.pmc,
        )
        fallback_ctx._api_responses = getattr(ctx, "_api_responses", [])  # type: ignore[attr-defined]
        # F12: surface the upstream adapter's unit count so generic.extract
        # can decide whether the gate should stay shut. We're inside the
        # ``not adapter_result.units`` branch so this is always 0 here, but
        # we set it explicitly for clarity and to keep the contract obvious
        # to anyone reading.
        fallback_ctx.adapter_unit_count = len(adapter_result.units)  # type: ignore[attr-defined]

        try:
            fallback_result = await generic.extract(page, fallback_ctx)  # type: ignore[arg-type]
            if fallback_result.units:
                adapter_result = fallback_result
                result["_adapter_used"] = generic_name
        except Exception as exc:
            adapter_result.errors.append(f"generic-fallback-error: {exc}")

    # --- Step 9: Populate legacy result ---
    result["units"] = adapter_result.units
    result["extraction_tier_used"] = adapter_result.tier_used or None
    result["errors"].extend(adapter_result.errors)
    result["api_calls_intercepted"] = [r.get("url", "") for r in adapter_result.api_responses]
    # Surface full {url, body} records and the winning URL so downstream
    # (profile_updater, reporting) can learn from what worked.
    result["_raw_api_responses"] = list(adapter_result.api_responses)
    if adapter_result.winning_url:
        result["_winning_page_url"] = adapter_result.winning_url
    result["_fallback_chain"] = fallback_chain
    # Surface per-sub-tier attempts for the report. GenericAdapter attaches
    # these as ``_tier_attempts``; PMS-specific adapters don't currently, so
    # an empty list is fine.
    result["_tier_attempts"] = getattr(adapter_result, "_tier_attempts", [])
    # Phase D: provenanced merge output for source observers
    result["_merged_units"] = getattr(adapter_result, "_merged_units", [])
    result["_sources"] = getattr(adapter_result, "_sources", [])
    # Phase E: DOM hints attempt/hit flags for miss-counter in profile_updater
    result["_dom_hints_attempted"] = getattr(adapter_result, "_dom_hints_attempted", False)
    result["_dom_hints_hit"] = getattr(adapter_result, "_dom_hints_hit", False)
    # Surface LLM interactions + hints if the generic:llm sub-tier ran. These
    # drive cost accounting, the LLM Interactions report section, and the
    # profile updater (css_selectors, api_urls_with_data, platform_guess).
    adapter_llm = getattr(adapter_result, "_llm_interactions", None) or []
    if adapter_llm:
        result["_llm_interactions"] = list(adapter_llm)
    adapter_hints = getattr(adapter_result, "_llm_hints", None)
    if adapter_hints:
        result["_llm_hints"] = adapter_hints

    # Phase 3/4: surface the new learning payloads for profile_updater.
    # ``_llm_analysis_results`` is consumed by services.profile_updater to
    # write blocked_endpoints on ``noise`` verdicts; ``_llm_field_mappings``
    # becomes profile.api_hints.llm_field_mappings for deterministic replay
    # on subsequent runs. ``_llm_navigation_hints`` is consumed by the
    # link-hop in scrape_jugnu as a prioritised candidate list.
    analysis_results = getattr(adapter_result, "_llm_analysis_results", None)
    if analysis_results:
        result["_llm_analysis_results"] = dict(analysis_results)
    field_mappings = getattr(adapter_result, "_llm_field_mappings", None)
    if field_mappings:
        result["_llm_field_mappings"] = list(field_mappings)
    nav_hints = getattr(adapter_result, "_llm_navigation_hints", None)
    if nav_hints:
        result["_llm_navigation_hints"] = list(nav_hints)

    return result


# ---------------------------------------------------------------------------
# Jugnu J3 — new entry point that takes CrawlTask + FetchResult
# ---------------------------------------------------------------------------


_RENT_SIGNAL_RE = re.compile(r"\$\s?\d{3,4}(?:[,.]\d{3})?(?:/mo|\s*/\s*month)?", re.IGNORECASE)
_FRAMEWORK_HINTS: tuple[tuple[str, str], ...] = (
    ("__NEXT_DATA__", "next"),
    ("__NUXT__", "nuxt"),
    ("ng-app", "angular"),
    ("data-reactroot", "react"),
    ("__svelte", "svelte"),
    ("data-v-app", "vue"),
    ("static.parastorage.com", "wix"),
    ("squarespace.com", "squarespace"),
    ("cdn.shopify.com", "shopify"),
)


def _characterize_html(page_html: str) -> dict[str, Any]:
    """Compute coarse shape metrics on the fetched HTML.

    Never raises — all regex work is bounded by input size. Intended to be
    small (<200 bytes serialized) so it's cheap to ship with every event.
    """
    body_bytes = len(page_html.encode("utf-8", errors="ignore"))
    # Strip scripts/styles/comments to estimate "real" rendered text size.
    stripped = re.sub(
        r"<script.*?</script>|<style.*?</style>|<!--.*?-->",
        "",
        page_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text_bytes = len(re.sub(r"<[^>]+>", "", stripped).encode("utf-8", errors="ignore"))

    script_count = len(re.findall(r"<script\b", page_html, re.IGNORECASE))
    iframe_count = len(re.findall(r"<iframe\b", page_html, re.IGNORECASE))
    jsonld_types: list[str] = []
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page_html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        snippet = m.group(1)[:2000]
        types = re.findall(r'"@type"\s*:\s*"([^"]+)"', snippet)
        jsonld_types.extend(types)
        if len(jsonld_types) > 20:
            break

    frameworks = [label for needle, label in _FRAMEWORK_HINTS if needle in page_html]
    rent_signals = len(_RENT_SIGNAL_RE.findall(page_html))

    # SPA heuristic: lots of script, little text, no JSON-LD, rent signals nil.
    spa_score = 0.0
    if body_bytes > 0:
        script_ratio = 1.0 - min(1.0, text_bytes / max(1, body_bytes))
        spa_score += 0.4 * script_ratio
    if "__NEXT_DATA__" in page_html or "__NUXT__" in page_html:
        spa_score += 0.3
    if rent_signals == 0 and text_bytes < 5000:
        spa_score += 0.3
    spa_score = round(min(1.0, spa_score), 2)

    return {
        "body_bytes": body_bytes,
        "text_bytes": text_bytes,
        "script_count": script_count,
        "iframe_count": iframe_count,
        "jsonld_block_count": len(jsonld_types),
        "jsonld_types": jsonld_types[:10],
        "framework_hints": frameworks,
        "spa_confidence": spa_score,
        "rent_signal_count": rent_signals,
    }


# ── Link-hop (Phase-4 equivalent) ─────────────────────────────────────────
# When the entry URL produces no units, rank the internal links on the
# home page and re-fetch the top candidates. This is a one-level BFS capped
# at N sub-fetches so a failing property can't consume unbounded budget.
# Typical win case: RentCafe/Entrata/AppFolio vanity home pages that embed
# tracking scripts but don't carry unit data — the real portal is one
# "View Availability" click away.


def _augment_ranked_with_hints(
    ranked: list[tuple[str, int, str]],
    hints: list[str],
    base_url: str,
) -> list[tuple[str, int, str]]:
    """Push LLM-provided navigation hints to the top of the ranked list.

    When the monolithic LLM call returned ``units: []`` but filled in
    ``profile_hints.navigation_hint`` (e.g. "/Marketing/FloorPlans"), we want
    link-hop to try that URL first. The hint can be a relative path or a
    full URL — we resolve against ``base_url`` either way and deduplicate.

    Phase 5: acts on LLM diagnostic output that was previously discarded.
    """
    if not hints:
        return ranked
    seen_urls = {u for u, _, _ in ranked}
    augmented: list[tuple[str, int, str]] = []
    for raw in hints:
        raw_s = (raw or "").strip()
        if not raw_s:
            continue
        try:
            abs_url = urllib.parse.urljoin(base_url, raw_s)
        except Exception:
            continue
        if not abs_url.startswith(("http://", "https://")):
            continue
        if abs_url in seen_urls:
            continue
        seen_urls.add(abs_url)
        # Score 1000 so LLM hints always outrank keyword-matched links.
        augmented.append((abs_url, 1000, f"llm-hint:{raw_s[:60]}"))
    return augmented + ranked


_LINK_ANCHOR_KEYWORDS: tuple[tuple[str, int], ...] = (
    # (keyword, score) — anchor text, lowercased, substring match
    ("availability", 100),
    ("floor plan", 90),
    ("floor-plan", 90),
    ("floorplan", 85),
    ("pricing", 80),
    ("rent", 70),
    ("apartment", 60),
    ("unit", 55),
    ("lease", 50),
    ("tour", 40),
    ("apply", 30),
    ("schedule", 20),
)

_LINK_PATH_KEYWORDS: tuple[tuple[str, int], ...] = (
    # (substring, score) — matched against url path, lowercased
    ("/floor-plan", 95),
    ("/floorplan", 90),
    ("/availability", 95),
    ("/pricing", 80),
    ("/apartments", 70),
    ("/rent", 60),
    ("/units", 85),
    ("/leasing", 50),
    ("/lease", 45),
    ("/floorplans", 90),
    ("/availabilities", 95),
)

_LINK_HOST_KEYWORDS: tuple[tuple[str, int], ...] = (
    # (host suffix, score) — portals run on known subdomains
    (".rentcafe.com", 120),
    (".appfolio.com", 120),
    (".onlineleasing.realpage.com", 120),
    ("sightmap.com", 110),
    (".entrata.com", 115),
    ("commoncf.entrata.com", 115),
)


# Patch #4 (2026-05-06 audit, REVISED 2026-05-07 after web-validation).
# Iframe-PMS allowlist. When the entry HTML embeds an iframe whose src host
# is in this set, the iframe content IS the unit data source. Following the
# iframe as a link-hop target is the canonical recovery for widget-hosted
# unit data (Squarespace + sightmap, custom CMS + fortresstech, etc.).
#
# Validation history
# ------------------
# v1 (2026-05-06): broad allowlist included hy.ly, knck.io, funnelleasing,
#   marketapts.com, gounion.com, comms.entrata.com, popcard.rentcafe.com.
# v2 (2026-05-07): web-validation revealed those are MARKETING / CHAT / CRM
#   widgets, NOT unit data. Iframes from those hosts carry chat assistants,
#   tour schedulers, lead-capture popups — none have floor plans. Following
#   them wastes a link-hop and risks polluting extraction with marketing
#   text. Allowlist tightened to verified-unit-data hosts only.
#
# Verified unit-data iframe hosts (kept):
#   - sightmap.com           — interactive site-map widget; serves unit JSON
#   - fortresstech.io        — Carlson Place class; Squarespace + widget
#   - appfolio.com           — leasing/listing portal
#   - appfoliowebsites.com   — AppFolio CMS host with /listings sub-paths
#   - securecafe.com         — RentCafe leasing portal (login-required but
#                              floor-plan pages render unauthenticated)
#   - securecafenet.com      — RentCafe SSO variant
#   - onlineleasing.realpage.com — RealPage leasing portal
#
# Removed (verified marketing / chat / CRM, NOT unit data):
#   - hy.ly / my.hy.ly        — Hyly: marketing automation + virtual
#                               leasing assistant (chat). NO unit data.
#   - knck.io / doorway.knck.io — Knock: CRM + chatbot + tour scheduler.
#                               NO unit data.
#   - funnelleasing.com /
#     integrations.funnelleasing.com — Funnel: chat widget + appointment
#                               scheduling. NO unit data.
#   - popcard.rentcafe.com    — RentCafe popup/lead capture. NO unit data.
#   - comms.entrata.com       — Entrata communications/chat. NO unit data.
#   - marketapts.com, gtmaservices.com, gounion.com, myresman.com —
#     unverified; held out pending live-fetch evidence rather than included
#     speculatively.
#
# Note: rentcafe.com / entrata.com root domains are NOT in the allowlist
# because most subdomains under them are marketing/chat. Specific portal
# paths (e.g. *.securecafe.com or onlineleasing.realpage.com) are.
_IFRAME_PMS_HOSTS: tuple[str, ...] = (
    "sightmap.com",
    "fortresstech.io",
    "appfolio.com",
    "appfoliowebsites.com",
    "securecafe.com",
    "securecafenet.com",
    "onlineleasing.realpage.com",
)


# Patch #9 (2026-05-06 audit) — broader allowlist for portal URLs found
# *inside* captured API JSON bodies. JSON link fields like
# `floor_plan_link`, `applicant_link`, `leasing_url` are hyperlinks the
# site itself provides — they almost always point to unit data when they
# point to a PMS root domain (rentcafe.com, entrata.com root). Distinct
# from iframe-direct-follow because iframes can be chat/widgets while
# JSON-emitted hyperlinks are typically meaningful pointers.
_PORTAL_LINK_HOSTS: tuple[str, ...] = _IFRAME_PMS_HOSTS + (
    "rentcafe.com",
    "entrata.com",
    "yardi.com",
    "smartrent.com",
    "loftliving.com",
    "on-site.com",
    "myresman.com",
)


# Patch #12 (2026-05-07 audit) — PMS fingerprint → expected API host map.
# When a property's static fingerprint (HTML/script-source signature) says
# it's on a particular PMS, we expect to capture an API URL on that PMS's
# host during page render. If we don't, the marketing site is a "shell"
# that links out to a separate leasing portal we never followed. This map
# defines: detected PMS → host substring(s) we'd expect in captured APIs.
_PMS_TO_EXPECTED_API_HOSTS: dict[str, tuple[str, ...]] = {
    "rentcafe":  ("rentcafe.com",),
    "realpage":  ("ws.realpage.com", "onlineleasing.realpage.com"),
    "entrata":   ("entrata.com",),
    "appfolio":  ("appfolio.com", "appfolio-listings"),
    "onesite":   ("onesite",),
    "sightmap":  ("sightmap.com",),
    "avalonbay": ("avaloncommunities.com", "avb.api"),
}


def _pms_fingerprint_without_api_capture(result: dict, page_html: str | None) -> bool:
    """Patch #12 — return True iff the property has a known-PMS fingerprint
    matched but no captured API on that PMS's host AND current extraction
    is incomplete (rent missing on at least some units).

    This is the strongest signal we have that the homepage is a marketing
    shell pointing to a separate leasing portal: we detected the PMS via
    static signatures (script src host, meta tags), but the page never made
    an XHR to the PMS data API. The unit data lives on a portal one hop
    away — link-hop should fire to find and follow it.

    Production audit (2026-05-06 run) showed this pattern on 850 of 1000
    sampled properties (85%); triggering link-hop here is the largest
    single recovery opportunity in the audit.

    Completeness gate (added after dry-run showed 117 already-GOOD
    properties would have triggered): require rent populated on < 90% of
    units before firing the hop. Already-rent-full extractions don't need
    a re-fetch from the canonical source.
    """
    # ---- Completeness gate: skip if already substantially complete ----
    units = result.get("units") or []
    if units:
        n = len(units)
        # Match both v1 and v2 unit shapes — v2 uses rent_low/rent_high,
        # v1 uses rent_range string.
        n_with_rent = sum(
            1 for u in units
            if (
                (isinstance(u, dict) and (u.get("rent_low") or u.get("rent_high")))
                or (isinstance(u, dict) and isinstance(u.get("rent_range"), str) and "$" in u.get("rent_range", ""))
            )
        )
        if n > 0 and n_with_rent / n >= 0.9:
            return False  # Already 90%+ rent-populated — don't waste hop.
    detected_pms_dict = result.get("_detected_pms") or {}
    evidence = detected_pms_dict.get("evidence") or []
    detected_pms = (detected_pms_dict.get("pms") or "").lower()

    # Static fingerprints either appear in the detected.pms field or in the
    # detector_signals' fingerprints_matched list. Normalize.
    matched_pmses: set[str] = set()
    if detected_pms and detected_pms != "unknown":
        matched_pmses.add(detected_pms)
    detector_signals = result.get("_detector_signals") or {}
    for fp in (detector_signals.get("fingerprints_matched") or []):
        matched_pmses.add(str(fp).lower())

    # Filter to PMSes for which we actually have an adapter — we shouldn't
    # hop on `marketing_knock` / `marketing_hyly` / `wix` / `squarespace`
    # fingerprints since those aren't unit-data sources.
    relevant_pmses = matched_pmses & set(_PMS_TO_EXPECTED_API_HOSTS.keys())
    if not relevant_pmses:
        return False

    # What APIs were captured?
    captured_apis = result.get("_raw_api_responses") or []
    captured_urls = []
    for r in captured_apis:
        if isinstance(r, dict):
            u = r.get("url") or ""
            if u:
                captured_urls.append(u.lower())

    # For each relevant PMS, did we capture any API on its expected hosts?
    for pms in relevant_pmses:
        expected_hosts = _PMS_TO_EXPECTED_API_HOSTS.get(pms, ())
        if any(any(h in u for h in expected_hosts) for u in captured_urls):
            # We DID capture this PMS's API → not a misroute case.
            return False

    # No PMS API was captured for any matched PMS — likely shell site.
    # Sanity check: is there at least an anchor to a PMS host in the HTML?
    # (If not, link-hop will fail anyway and we shouldn't waste budget.)
    if page_html and len(page_html) > 100:
        page_lower = page_html.lower()
        for pms in relevant_pmses:
            for host in _PMS_TO_EXPECTED_API_HOSTS.get(pms, ()):
                if host in page_lower:
                    return True
        # No anchor either — but still flag, link-hop's keyword ranker may
        # find something else. Cheap to attempt.
        return True
    return True


def _extract_portal_links_from_api_bodies(api_responses: list[dict]) -> list[str]:
    """Patch #9 (2026-05-06 audit) — recursively scan captured API JSON
    bodies for string fields whose value is a URL pointing to a known PMS
    portal host (rentcafe, securecafe, sightmap, fortresstech, etc.).
    These are typically `floor_plan_link`, `applicant_link`, `resident_link`,
    `availability_url` keys on a property metadata response.

    Birch Run (id 14182) is the canonical case: a Supabase
    /rest/v1/properties response carried a working RentCafe portal URL
    in the `floor_plan_link` field — never followed. ~50–100 properties
    in the run had a similar pattern.

    Returns deduplicated list of portal URLs. Pure / never raises.
    """
    if not api_responses:
        return []
    out: list[str] = []
    seen: set[str] = set()
    # Patch #9 — use the broader _PORTAL_LINK_HOSTS, not the strict
    # iframe-only allowlist. JSON-emitted hyperlinks to rentcafe.com /
    # entrata.com etc. are nearly always meaningful pointers; only iframe
    # embeds need the stricter allowlist.
    portal_hosts = _PORTAL_LINK_HOSTS

    def _walk(node: object, depth: int = 0) -> None:
        if depth > 8:  # bound recursion
            return
        if isinstance(node, dict):
            for v in node.values():
                _walk(v, depth + 1)
        elif isinstance(node, list):
            for item in node:
                _walk(item, depth + 1)
        elif isinstance(node, str):
            s = node.strip()
            if not s.startswith(("http://", "https://")):
                return
            s_lower = s.lower()
            if not any(host in s_lower for host in portal_hosts):
                return
            if s in seen:
                return
            seen.add(s)
            out.append(s)

    for resp in api_responses:
        if not isinstance(resp, dict):
            continue
        body = resp.get("body")
        if body is None:
            continue
        try:
            _walk(body)
        except Exception:
            continue
    return out


def _extract_iframe_pms_urls(entry_html: str | None) -> list[str]:
    """Return iframe src URLs in entry_html whose host matches a known PMS
    portal allowlist. Pure regex over the HTML — never raises.
    """
    if not entry_html or len(entry_html) < 50:
        return []
    import re
    iframe_re = re.compile(r"<iframe\b[^>]*\bsrc\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
    out: list[str] = []
    seen: set[str] = set()
    for m in iframe_re.finditer(entry_html):
        src = (m.group(1) or "").strip()
        if not src.startswith(("http://", "https://", "//")):
            continue
        if src.startswith("//"):
            src = "https:" + src
        src_lower = src.lower()
        if not any(host in src_lower for host in _IFRAME_PMS_HOSTS):
            continue
        if src in seen:
            continue
        seen.add(src)
        out.append(src)
    return out


def is_suspicious_cross_host_redirect(
    input_url: str,
    final_url: str,
    target_name: str = "",
) -> tuple[bool, str]:
    """Patch #6 (2026-05-06 audit) — detect when the L1 fetcher followed a
    redirect to a host that doesn't belong to the target property.

    Pearl Midlane (id 41008) redirected pearlmidlane.com → thebriscoeriveroaks.com,
    a different property entirely. We then extracted JSON-LD from Briscoe's
    homepage and stored 13 of Briscoe's floor plans against Pearl Midlane's
    apartment_id. ~40–60 properties in the run had this pattern.

    Returns (is_suspicious, reason). Suspicious iff:
      - Hosts differ (after stripping leading 'www.')
      - The final host is NOT a known PMS portal/widget host (those are
        legitimate redirects — RentCafe, Sightmap, etc.)
      - The final host shares NO token with the input host AND no token
        with the target property name.

    Pure / never raises. When inputs are malformed or hosts can't be parsed,
    returns (False, '') so callers fall through to default behaviour.
    """
    if not input_url or not final_url:
        return False, ""
    try:
        in_host = (urllib.parse.urlparse(input_url).hostname or "").lower().lstrip(".")
        fi_host = (urllib.parse.urlparse(final_url).hostname or "").lower().lstrip(".")
    except Exception:
        return False, ""
    if not in_host or not fi_host:
        return False, ""
    in_host = in_host.removeprefix("www.")
    fi_host = fi_host.removeprefix("www.")
    if in_host == fi_host:
        return False, ""
    # Known portal/widget hosts are legitimate redirect targets.
    if any(host in fi_host for host in _IFRAME_PMS_HOSTS):
        return False, ""
    if any(fi_host.endswith(suf) for suf, _ in _LINK_HOST_KEYWORDS):
        return False, ""
    # Token overlap check (whole tokens after splitting on . and -).
    in_tokens = _slug_tokens(in_host.replace(".", " ").replace("-", " "))
    in_tokens -= {"www", "com", "net", "org", "co", "us", "io", "the", "apartments", "apts"}
    fi_tokens = _slug_tokens(fi_host.replace(".", " ").replace("-", " "))
    fi_tokens -= {"www", "com", "net", "org", "co", "us", "io", "the", "apartments", "apts"}
    name_tokens = _slug_tokens(target_name) if target_name else set()
    if (in_tokens & fi_tokens) or (name_tokens and (name_tokens & fi_tokens)):
        return False, ""
    # Substring check for compound hosts: 'livethemarion.com' →
    # 'marionapartments.com' shares no whole token but both contain 'marion'.
    # We use only the FIRST property-specific name token to avoid
    # false-negatives on shared-neighbourhood naming (e.g. Pearl Midlane
    # River Oaks vs The Briscoe River Oaks both contain "river"/"oaks" —
    # those are not the property's distinguishing identifier).
    _name_filler = {"the", "at", "of", "on", "by", "apartments", "apts", "lofts", "place", "house", "homes", "residences", "tower", "park", "river", "oaks", "ridge", "hill", "creek", "pointe", "view", "village", "crossing", "square", "manor"}
    name_tokens_ordered = [t for t in target_name.lower().split() if t and t.isalnum()] if target_name else []
    primary_name_token = next(
        (t for t in name_tokens_ordered if t not in _name_filler and len(t) >= 4),
        "",
    )
    if primary_name_token and primary_name_token in fi_host:
        return False, ""
    # Substring check between input host tokens (≥5 chars) and final host:
    # captures rebrand cases without a CSV name. Only consider the longest
    # input token, again to avoid neighbourhood-name false-negatives.
    long_in_tokens = sorted({t for t in in_tokens if len(t) >= 5}, key=len, reverse=True)
    if long_in_tokens and (long_in_tokens[0] in fi_host or fi_host.split(".", 1)[0] in long_in_tokens[0]):
        return False, ""
    # Patch #6 (REVISED 2026-05-07 after web-validation showed 37% false-
    # positive rate). PMC umbrella redirects are LEGITIMATE and produce full
    # rent — flagging them as suspicious clutters telemetry without
    # functional value. Heuristic: hosts whose name CONTAINS one of these
    # umbrella tokens are a Property-Management-Company portfolio site that
    # legitimately serves data for the redirected property.
    _PMC_UMBRELLA_TOKENS = (
        "communities", "living", "management", "properties", "realty",
        "group", "homes", "residential", "apartmenthomes", "apts",
    )
    if any(tok in fi_host for tok in _PMC_UMBRELLA_TOKENS):
        return False, ""
    return True, (
        f"redirect from {in_host} to {fi_host} — no host-token or name-token overlap; "
        f"likely cross-portfolio leak (target={target_name!r})"
    )


def _augment_ranked_with_iframe_pms(
    ranked: list[tuple[str, int, str]],
    iframe_urls: list[str],
) -> list[tuple[str, int, str]]:
    """Push iframe-PMS URLs to the top of the ranked candidates.

    Iframe-hosted PMS widgets (Sightmap, Fortresstech, Knock, etc.) are
    nearly always the canonical source of unit data on multi-tier CMS sites
    (Squarespace + widget, Wix + widget, marketing-React + widget). Score
    them higher than even LLM navigation hints (1000) because they're a
    structural/signal-level certainty — there's literally an iframe in the
    DOM pointing at unit JSON.
    """
    if not iframe_urls:
        return ranked
    seen_urls = {u for u, _, _ in ranked}
    augmented: list[tuple[str, int, str]] = []
    for url in iframe_urls:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        augmented.append((url, 1100, "iframe-pms"))
    return augmented + ranked

# Skip these link shapes outright — they're never availability pages.
_LINK_SKIP_PATTERNS: tuple[str, ...] = (
    "tel:",
    "mailto:",
    "javascript:",
    "#",
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".mp4",
    ".mov",
    "/blog/",
    "/news/",
    "/privacy",
    "/terms",
    "/accessibility",
    "/sitemap",
    "facebook.com/",
    "twitter.com/",
    "instagram.com/",
    "linkedin.com/",
    "youtube.com/",
    "/contact",
    "/careers",
    "/jobs",
)


# Patch #5 (2026-05-06 audit) — generic floor-plan/availability path tokens
# that don't carry property-specific meaning. When the link slug is one of
# these (e.g. `/floorplans`, `/availability`), we don't penalise for
# missing target-name overlap because such paths apply to the whole
# property and are the canonical sub-page regardless of slug naming.
_GENERIC_SUBPAGE_TOKENS = frozenset(
    {
        "floor",
        "floorplan",
        "floorplans",
        "floor-plans",
        "floor_plans",
        "plans",
        "availability",
        "available",
        "availabilities",
        "apartments",
        "apartment",
        "pricing",
        "rates",
        "rent",
        "rentals",
        "leasing",
        "lease",
        "units",
        "specials",
        "tour",
        "schedule",
        "amenities",
        "gallery",
        "contact",
        "about",
        "neighborhood",
    }
)


def _slug_tokens(text: str) -> set[str]:
    """Tokenise a property name or URL slug into lowercase word tokens.
    Used by Patch #5 to compare a candidate link's slug against the target
    property's name. Empty / single-letter tokens are dropped.
    """
    if not text:
        return set()
    import re

    raw = re.split(r"[^a-z0-9]+", text.lower().strip())
    return {tok for tok in raw if len(tok) > 1}


def _rank_internal_links(
    page_html: str,
    base_url: str,
    limit: int = 5,
    *,
    target_name: str = "",
) -> list[tuple[str, int, str]]:
    """Rank internal links on a page for likelihood of carrying unit data.

    Scores each link by anchor text, path keywords, and host (portal
    subdomains). Returns ``[(url, score, anchor_text), ...]`` sorted best
    first. Never raises — parser errors yield an empty list.

    Patch #5 (2026-05-06 audit) — when ``target_name`` is provided, links
    whose URL slug or anchor text shares ANY non-generic token with the
    target receive a +50 bonus; links whose slug is property-specific
    (i.e. not in _GENERIC_SUBPAGE_TOKENS) but shares NO token with the
    target receive a -150 penalty (removes them from contention unless
    nothing else exists). This prevents cross-portfolio sister-property
    leakage like Carlson Place hopping to /apartments-on-7th.
    """
    if not page_html:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    try:
        soup = BeautifulSoup(page_html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(page_html, "html.parser")
        except Exception:
            return []

    try:
        base = urllib.parse.urlparse(base_url)
    except Exception:
        return []
    base_host = (base.hostname or "").lower()

    # Patch #5 — derive target tokens from the property name AND the
    # base-URL host (the host often encodes the property name, e.g.
    # 'livethemarion.com' → tokens {'live', 'themarion', 'marion'}).
    # Only apply when an explicit target_name was passed; without one we
    # fall back to legacy ranker behaviour (no bonus, no penalty) so callers
    # that don't yet pass a name aren't penalised.
    target_tokens: set[str] = set()
    if target_name:
        target_tokens = _slug_tokens(target_name)
        if base_host:
            host_tokens = _slug_tokens(base_host.replace(".", " ").replace("-", " "))
            host_tokens -= {"www", "com", "net", "org", "co", "us", "io"}
            target_tokens |= host_tokens

    candidates: dict[str, tuple[int, str]] = {}
    for a in soup.find_all("a", href=True):
        raw_href = a.get("href") or ""
        href = (raw_href if isinstance(raw_href, str) else " ".join(raw_href)).strip()
        if not href:
            continue
        lower = href.lower()
        if any(skip in lower for skip in _LINK_SKIP_PATTERNS):
            continue

        # Resolve relative → absolute
        try:
            resolved = urllib.parse.urljoin(base_url, href)
        except Exception:
            continue
        if not resolved.startswith(("http://", "https://")):
            continue

        try:
            parsed = urllib.parse.urlparse(resolved)
        except Exception:
            continue
        link_host = (parsed.hostname or "").lower()
        link_path = (parsed.path or "").lower()

        anchor = (a.get_text(" ", strip=True) or "").lower()[:120]

        score = 0
        for kw, weight in _LINK_ANCHOR_KEYWORDS:
            if kw in anchor:
                score += weight
        for kw, weight in _LINK_PATH_KEYWORDS:
            if kw in link_path:
                score += weight
        for suffix, weight in _LINK_HOST_KEYWORDS:
            if link_host.endswith(suffix):
                score += weight

        # Patch #5 — target-name token bonus / sister-property penalty.
        # Skip the check when no target_name was provided (e.g. tests, or
        # when the planner couldn't surface the property name).
        if target_tokens:
            slug_text = (link_path or "") + " " + (anchor or "")
            slug_tokens = _slug_tokens(slug_text)
            specific_slug_tokens = slug_tokens - _GENERIC_SUBPAGE_TOKENS
            shared = slug_tokens & target_tokens
            if shared:
                # Slug carries a property-name token → decisive boost. Set
                # high enough to outrank the strongest generic combination
                # (anchor "availability"=100 + path "/availability"=95 = 195),
                # because a property-name-matched path is the cleanest
                # possible signal that this is the right sub-page.
                score += 250
            elif specific_slug_tokens:
                # Property-specific tokens that DON'T match the target — this
                # is most likely a sister property in a multi-property
                # portfolio (e.g. Carlson Place's /apartments-on-7th).
                score -= 150

        # Stay on-site or go to a known portal subdomain
        is_same_site = (
            link_host == base_host
            or link_host.endswith("." + base_host)
            or base_host.endswith("." + link_host)
        )
        is_portal = any(link_host.endswith(suf) for suf, _ in _LINK_HOST_KEYWORDS)
        if not (is_same_site or is_portal):
            continue

        # Skip the base URL itself
        if resolved.rstrip("/") == base_url.rstrip("/"):
            continue
        if score <= 0:
            continue

        # Keep best score per URL
        existing = candidates.get(resolved)
        if existing is None or score > existing[0]:
            candidates[resolved] = (score, anchor)

    ranked = sorted(
        ((u, s, a) for u, (s, a) in candidates.items()),
        key=lambda t: -t[1],
    )
    return ranked[:limit]


async def _try_link_hop(
    entry_url: str,
    entry_page_html: str,
    detected: DetectedPMS,
    profile: Any,
    expected_total_units: int | None,
    property_id: str,
    csv_row: dict[str, Any] | None,
    max_hops: int = 3,
    llm_navigation_hints: list[str] | None = None,
    visited_urls: set[str] | None = None,
    shared_budget: dict | None = None,
    api_responses: list[dict] | None = None,
) -> dict[str, Any] | None:
    """One-level BFS over home-page links when primary extraction is empty.

    Fetches up to ``max_hops`` candidate URLs via the L1 fetcher, re-runs
    ``scrape()`` on each, and returns the first sub-result that yields
    units. Returns ``None`` if no hop recovered data.

    ``llm_navigation_hints`` (Phase 5) takes priority over keyword-ranked
    candidates — if the LLM already diagnosed where data lives, we try
    that URL first instead of guessing from anchor text.

    Phase 9 — H5 invariant: ``visited_urls`` blocks fetch cycles. The
    entry URL is auto-added to prevent re-fetching the home page.
    ``max_hops`` caps the bounded BFS at 3 by default (never deeper).
    """
    visited: set[str] = set(visited_urls) if visited_urls else set()
    visited.add(entry_url)

    # Patch #5 (2026-05-06 audit) — pass target property name to the ranker
    # so links to sister properties in the same portfolio (e.g. Carlson
    # Place's /apartments-on-7th) are demoted in favour of generic
    # /floorplans-style paths or property-name-matched paths.
    _target_name = ""
    if csv_row:
        for k in ("Property Name", "name", "Name", "proj_name"):
            v = csv_row.get(k) if isinstance(csv_row, dict) else None
            if v:
                _target_name = str(v).strip()
                break
    ranked = _rank_internal_links(
        entry_page_html, entry_url, limit=max_hops, target_name=_target_name
    )
    # Patch #4 (2026-05-06 audit) — surface iframe-PMS hosts BEFORE applying
    # the max_hops cap. Iframe widgets (Sightmap, Fortresstech, Knock, Hyly,
    # Funnel, securecafe, etc.) are the canonical unit source on multi-tier
    # CMS sites; ranking them at 1100 ensures they outrank both keyword
    # candidates and LLM hints.
    iframe_pms_urls = _extract_iframe_pms_urls(entry_page_html)
    if iframe_pms_urls:
        ranked = _augment_ranked_with_iframe_pms(ranked, iframe_pms_urls)
    # Patch #9 (2026-05-06 audit) — also augment with portal links found
    # inside captured API JSON bodies (e.g. supabase property-metadata
    # response carrying a `floor_plan_link` to a RentCafe portal).
    portal_links_in_json: list[str] = []
    if api_responses:
        portal_links_in_json = _extract_portal_links_from_api_bodies(api_responses)
        if portal_links_in_json:
            ranked = _augment_ranked_with_iframe_pms(ranked, portal_links_in_json)
    if llm_navigation_hints:
        ranked = _augment_ranked_with_hints(ranked, llm_navigation_hints, entry_url)
    # Cap to keep budget bounded with augmentations merged in. The cap
    # grows with the number of explicit hints (LLM + iframe-PMS + portal-in-JSON)
    # so high-priority candidates aren't dropped before the keyword-ranked
    # generic candidates.
    extra_priority_n = (
        len(llm_navigation_hints or []) + len(iframe_pms_urls) + len(portal_links_in_json)
    )
    ranked = ranked[: max(max_hops, extra_priority_n + 1)]
    # Phase 9: drop URLs already visited (cycle break)
    ranked = [(u, s, a) for (u, s, a) in ranked if u not in visited]
    # Phase 9: hard-cap at max_hops (defensive — _rank_internal_links has
    # its own limit, but enforcing here protects against augment bypass).
    # When iframe-PMS hits exist, allow up to max_hops + iframe count so
    # we never drop a structural-certainty hop in favour of a keyword guess.
    effective_cap = max_hops + len(iframe_pms_urls) + len(portal_links_in_json)
    ranked = ranked[:effective_cap]
    if not ranked:
        return None

    try:
        from ma_poc.fetch import fetch as jugnu_fetch
    except ImportError:
        return None
    from ma_poc.discovery.contracts import CrawlTask, TaskReason
    from ma_poc.fetch.contracts import RenderMode
    from ma_poc.observability.events import EventKind, emit

    emit(
        EventKind.LINK_HOP_STARTED,
        property_id,
        entry_url=entry_url,
        candidates=[{"url": u, "score": s, "anchor": a[:60]} for u, s, a in ranked],
    )

    # Phase 4: track which sub-URLs were tried and whether they produced
    # data. profile_updater consumes this dict to persist
    # profile.navigation.explored_links (skip-next-run) and
    # profile.navigation.availability_links (prioritise-next-run).
    explored: dict[str, bool] = {}

    for idx, (sub_url, score, anchor) in enumerate(ranked, 1):
        if sub_url in visited:
            # Phase 9: defensive — should already be filtered above, but
            # double-check to enforce H5 invariant under all code paths.
            continue
        visited.add(sub_url)
        sub_task = CrawlTask(
            url=sub_url,
            property_id=property_id,
            priority=0,
            budget_ms=35000,
            reason=TaskReason.SCHEDULED,
            render_mode=RenderMode.RENDER,
            parent_task_id=None,
        )
        try:
            sub_fetch = await jugnu_fetch(sub_task)
        except Exception as exc:
            emit(EventKind.LINK_HOP_FETCHED, property_id, url=sub_url, error=str(exc)[:200], hop_index=idx)
            continue

        outcome_val = (
            sub_fetch.outcome.value if hasattr(sub_fetch.outcome, "value") else str(sub_fetch.outcome)
        )
        emit(
            EventKind.LINK_HOP_FETCHED,
            property_id,
            url=sub_url,
            outcome=outcome_val,
            elapsed_ms=sub_fetch.elapsed_ms,
            body_bytes=len(sub_fetch.body) if sub_fetch.body else 0,
            hop_index=idx,
            score=score,
            anchor=anchor[:60],
        )

        if outcome_val != "OK":
            explored[sub_url] = False
            continue

        # Re-run extraction on the sub-page via ``scrape()`` (not
        # ``scrape_jugnu``) so link-hop doesn't recurse — scrape_jugnu is
        # where the hop kicks in, scrape() itself only extracts.
        try:
            sub_result = await scrape(
                base_url=sub_url,
                profile=profile,
                expected_total_units=expected_total_units,
                page=None,
                fetch_result=sub_fetch,
                csv_row=csv_row,
                property_id=property_id,
                shared_budget=shared_budget,
            )
        except Exception as exc:
            log.warning("link-hop scrape failed for %s: %s", sub_url, exc)
            explored[sub_url] = False
            continue

        had_data = bool(sub_result.get("units"))
        explored[sub_url] = had_data
        if had_data:
            sub_result["_link_hop_from"] = entry_url
            sub_result["_link_hop_depth"] = 1
            sub_result["_link_hop_score"] = score
            sub_result["_link_hop_anchor"] = anchor
            # Merge explored history so the profile updater (Phase 4) can
            # record which links the crawler already tried.
            existing_explored = sub_result.get("_explored_links") or {}
            existing_explored.update(explored)
            sub_result["_explored_links"] = existing_explored
            emit(
                EventKind.LINK_HOP_RECOVERED,
                property_id,
                entry_url=entry_url,
                sub_url=sub_url,
                units=len(sub_result["units"]),
                tier=sub_result.get("extraction_tier_used"),
                hop_index=idx,
                score=score,
            )
            return sub_result

    # No hop recovered — return None but stash the explored map on the
    # outer link-hop caller via a sentinel dict. The caller (scrape_jugnu)
    # can drop it onto the final empty result so learning still happens on
    # failure too.
    if explored:
        return {"_units_empty": True, "_explored_links": explored}
    return None


async def scrape_jugnu(
    task: Any,
    fetch_result: Any,
    page: Any | None = None,
    profile: Any | None = None,
    expected_total_units: int | None = None,
    csv_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Jugnu L3 entry point — scrape using pre-fetched result.

    Delta 2: Does not fetch. Short-circuits on non-OK outcome.
    Delta 4: Emits extraction events.
    Delta 7: Populates _extract_result with cost accounting.

    Parameters
    ----------
    task : CrawlTask
        The crawl task (from L2).
    fetch_result : FetchResult
        The L1 fetch result (already completed).
    page : Page | None
        Playwright page (for RENDER mode). None for HEAD/GET.
    profile : ScrapeProfile | None
        Profile from the profile store.
    expected_total_units : int | None
        Hint for expected unit count.

    Returns
    -------
    dict
        Legacy-compatible 46-key result dict.
    """
    from ma_poc.observability.events import EventKind, emit
    from ma_poc.pms.contracts import ExtractResult

    base_url = task.url if hasattr(task, "url") else str(task)
    property_id = task.property_id if hasattr(task, "property_id") else "unknown"

    # Phase G2: compute budget here so link-hop guard can respect it.
    _jugnu_budget: dict = {"llm_api_calls": 3, "llm_dom_calls": 1, "llm_monolithic": 1, "link_hop": 3}
    if profile is not None:
        try:
            from ma_poc.services.source_planner import compute_budget as _cb
            from ma_poc.models.scrape_profile import ProfileMaturity as _PM
            _jugnu_budget = _cb(profile, is_cold=profile.confidence.maturity == _PM.COLD)
        except Exception:
            pass

    # Delta 2: short-circuit on non-OK fetch
    if hasattr(fetch_result, "outcome"):
        outcome_val = (
            fetch_result.outcome.value
            if hasattr(fetch_result.outcome, "value")
            else str(fetch_result.outcome)
        )
        if outcome_val != "OK":
            result = _empty_result(base_url)
            result["_property_id"] = property_id
            result["extraction_tier_used"] = "generic:no_body_short_circuit"
            result["errors"].append(
                f"FAILED_UNREACHABLE: fetch_outcome={outcome_val} "
                f"sig={getattr(fetch_result, 'error_signature', None)}"
            )
            # Attach the diagnostic so the report can render *why* it failed.
            try:
                fd = fetch_result.to_dict() if hasattr(fetch_result, "to_dict") else {}
                fd["body_bytes"] = 0
                fd["captcha_detected"] = False
                fd["captcha_provider"] = None
                result["_fetch_diagnostic"] = fd
            except Exception:
                pass
            result["_extract_result"] = ExtractResult(
                property_id=property_id,
                records=[],
                tier_used="generic:no_body_short_circuit",
                adapter_name="none",
                winning_url=None,
                confidence=0.0,
                errors=[f"fetch_outcome={outcome_val}"],
            )
            return result

    # Delta 4: emit PMS detection event — forward fetch_result so adapters
    # can work from fetch_result.body when no live page is available.
    result = await scrape(
        base_url=base_url,
        profile=profile,
        expected_total_units=expected_total_units,
        page=page,
        fetch_result=fetch_result,
        csv_row=csv_row,
        property_id=property_id,
        shared_budget=_jugnu_budget,
    )
    result["_property_id"] = property_id

    # Telemetry B: attach fetch diagnostic (error_signature, final_url, body
    # size, captcha, proxy, identity) so the per-property report can render
    # it without reaching back into L5 events.
    if fetch_result is not None:
        try:
            fd = fetch_result.to_dict() if hasattr(fetch_result, "to_dict") else {}
        except Exception:
            fd = {}
        body = getattr(fetch_result, "body", None)
        fd["body_bytes"] = len(body) if body else 0
        captcha_flag, captcha_provider = False, None
        if body:
            try:
                from ma_poc.fetch.captcha_detect import looks_like_captcha

                captcha_flag, captcha_provider = looks_like_captcha(body)
            except Exception:
                pass
        fd["captcha_detected"] = captcha_flag
        fd["captcha_provider"] = captcha_provider
        result["_fetch_diagnostic"] = fd

    # Delta 4: emit events
    detected_pms = result.get("_detected_pms", {})
    emit(
        EventKind.PMS_DETECTED,
        property_id,
        pms=detected_pms.get("pms", "unknown"),
        confidence=detected_pms.get("confidence", 0.0),
    )

    adapter_name = result.get("_adapter_used", "unknown")
    emit(EventKind.ADAPTER_SELECTED, property_id, adapter_name=adapter_name)

    # ── Option B: one-level link-hop when primary extraction is empty ──
    # Fires when (a) main returned no units (legacy path) or (b) Phase G2:
    # main produced units but the planner says they're incomplete — hop to a
    # sub-page that might supply the missing field group (e.g. rent lives on
    # /availability, floor-plan physical data lives on /floor-plans).
    # Budget cap: _jugnu_budget["link_hop"] == 0 → never hop.
    should_hop = False
    if fetch_result is not None and _jugnu_budget.get("link_hop", 0) > 0:
        if not result.get("units"):
            should_hop = True
        elif result.get("_llm_navigation_hints"):
            # Patch #3 (2026-05-06 audit) — when the LLM explicitly emits a
            # navigation_hint (e.g. "/onlineleasing/.../floorplans.aspx" for
            # a React/Supabase site, "/floor-plans" for a misclassified site
            # whose homepage didn't expose nav links), hop unconditionally
            # even if main tiers extracted *something*. The "something" is
            # almost always a hero card, a property metadata blob, or a
            # marketing snippet — the LLM is telling us the real data lives
            # one level deeper. Without this trigger, ~150–200 properties in
            # the 2026-05-06 run had a working sub-page URL identified by
            # the LLM but never followed.
            should_hop = True
        elif _pms_fingerprint_without_api_capture(result, page_html):
            # Patch #12 (2026-05-07 audit) — biggest single recovery path.
            # When a property has a PMS fingerprint matched (rentcafe,
            # entrata, realpage, appfolio, onesite, sightmap) AND no
            # captured API URL has that PMS host, the data is one hop away
            # on the leasing portal. Production audit: 850 of 1000 sampled
            # properties (85%) are PMS-fingerprinted but extracted by some
            # other tier — usually TIER_4_LLM_DOM scraping marketing copy
            # rather than the canonical PMS portal. Triggering link-hop
            # here finds the rentcafe/realpage/etc. anchor in the
            # marketing site's HTML and routes to the real data source.
            should_hop = True
        else:
            # Phase G2: consult planner when main has units
            try:
                from ma_poc.services.source_planner import evaluate_completeness, plan_next_action
                from ma_poc.models.source import SourceId, from_legacy_unit
                _pu = [
                    from_legacy_unit(u, SourceId.API_GENERIC_NARROW, base_url, "", 0.85)
                    for u in (result.get("units") or [])
                ]
                _report = evaluate_completeness(_pu)
                # Patch #1 (2026-05-06 audit) — pass entry HTML to planner
                # so it can apply the homepage-only structural rule. We
                # decode here once; the same value is reused below for the
                # actual link-hop call.
                _planner_html: str | None = None
                _body = getattr(fetch_result, "body", None)
                if isinstance(_body, bytes):
                    try:
                        _planner_html = _body.decode("utf-8", errors="replace")
                    except Exception:
                        _planner_html = None
                elif isinstance(_body, str):
                    _planner_html = _body
                _decision = plan_next_action(
                    _report,
                    sources_already_run=set(),
                    budget_remaining=dict(_jugnu_budget),
                    pms_name=detected_pms.get("pms", "unknown"),
                    entry_html=_planner_html,
                    entry_url=base_url,
                    visited_urls={base_url},
                )
                if _decision.action == "ESCALATE_LINK_HOP":
                    should_hop = True
            except Exception:
                pass

    if should_hop:
        body = getattr(fetch_result, "body", None)
        entry_html: str | None = None
        if isinstance(body, bytes):
            try:
                entry_html = body.decode("utf-8", errors="replace")
            except Exception:
                entry_html = None
        elif isinstance(body, str):
            entry_html = body

        if entry_html and len(entry_html) > 500:
            try:
                detected = DetectedPMS(
                    pms=detected_pms.get("pms", "unknown"),
                    confidence=float(detected_pms.get("confidence", 0.0)),
                )
                # Phase 5: feed LLM navigation hints (if any) into the
                # ranker so they outrank keyword candidates.
                # Patch #10 (2026-05-06 audit) — populate `links_found` on
                # the result so the per-property report reflects what the
                # ranker actually surfaced. Pre-patch: every report showed
                # "Internal links discovered: 0" across the entire run,
                # blinding any debugging effort. We capture the ranked
                # output and stuff it into the result dict.
                _ranked_preview = []
                try:
                    _target_for_links = ""
                    if csv_row:
                        for k in ("Property Name", "name", "Name", "proj_name"):
                            v = csv_row.get(k) if isinstance(csv_row, dict) else None
                            if v:
                                _target_for_links = str(v).strip()
                                break
                    _ranked_preview = _rank_internal_links(
                        entry_html, base_url, limit=20, target_name=_target_for_links
                    )
                    _iframe_preview = _extract_iframe_pms_urls(entry_html)
                    _portal_in_json_preview = _extract_portal_links_from_api_bodies(
                        result.get("_raw_api_responses") or []
                    )
                    result["links_found"] = (
                        [u for u, _, _ in _ranked_preview]
                        + _iframe_preview
                        + _portal_in_json_preview
                    )
                    result["_link_candidates_detail"] = {
                        "ranked": [
                            {"url": u, "score": s, "anchor": a}
                            for u, s, a in _ranked_preview
                        ],
                        "iframe_pms": _iframe_preview,
                        "portal_in_json": _portal_in_json_preview,
                    }
                except Exception:
                    pass
                hop_result = await _try_link_hop(
                    entry_url=base_url,
                    entry_page_html=entry_html,
                    detected=detected,
                    profile=profile,
                    expected_total_units=expected_total_units,
                    property_id=property_id,
                    csv_row=csv_row,
                    max_hops=3,
                    llm_navigation_hints=result.get("_llm_navigation_hints"),
                    visited_urls={base_url},  # Phase 9: cycle protection (H5)
                    shared_budget=_jugnu_budget,
                    # Patch #9 — pass captured API responses so the ranker
                    # can surface portal URLs embedded in JSON bodies
                    # (e.g. supabase floor_plan_link → RentCafe portal).
                    api_responses=result.get("_raw_api_responses"),
                )
            except Exception as exc:
                log.warning("link-hop orchestration failed for %s: %s", property_id, exc)
                hop_result = None

            if hop_result and hop_result.get("units"):
                main_units = result.get("units") or []
                sub_units = hop_result.get("units") or []
                # Phase 9: when both main and sub-page produced units, merge
                # them by identity + max-confidence-per-field rather than
                # destructively overwriting. Both routes preserve telemetry.
                if main_units and sub_units:
                    try:
                        from ma_poc.models.source import (
                            ExtractedSource,
                            SourceId,
                            envelope_hash_of,
                            from_legacy_unit,
                            to_legacy_unit,
                        )
                        from ma_poc.services.source_merger import merge_sources
                        main_h = envelope_hash_of(main_units)
                        sub_h = envelope_hash_of(sub_units)
                        main_src = ExtractedSource(
                            source_id=SourceId.API_GENERIC_NARROW,
                            source_url=base_url,
                            envelope_hash=main_h,
                            units=[
                                from_legacy_unit(u, SourceId.API_GENERIC_NARROW, base_url, main_h, 0.85)
                                for u in main_units
                            ],
                            has_unit_ids=any(u.get("unit_number") or u.get("unit_id") for u in main_units),
                            is_floor_plan_level=False,
                        )
                        sub_url_winner = hop_result.get("_winning_page_url") or hop_result.get("_link_hop_from") or ""
                        sub_src = ExtractedSource(
                            source_id=SourceId.API_GENERIC_NARROW,
                            source_url=str(sub_url_winner),
                            envelope_hash=sub_h,
                            units=[
                                from_legacy_unit(u, SourceId.API_GENERIC_NARROW, str(sub_url_winner), sub_h, 0.85)
                                for u in sub_units
                            ],
                            has_unit_ids=any(u.get("unit_number") or u.get("unit_id") for u in sub_units),
                            is_floor_plan_level=False,
                        )
                        merged = merge_sources([main_src, sub_src], property_id)
                        if merged:
                            legacy = [to_legacy_unit(u) for u in merged]
                            for u in legacy:
                                u.pop("_provenance", None)
                            result["units"] = legacy
                            result["extraction_tier_used"] = "TIER_MERGED_CROSS_PAGE"
                            # Telemetry — additive (sub-page contributions appended).
                            for k in (
                                "_raw_api_responses",
                                "_llm_interactions",
                                "_llm_field_mappings",
                                "_tier_attempts",
                            ):
                                main_v = result.get(k)
                                sub_v = hop_result.get(k)
                                if isinstance(main_v, list) and isinstance(sub_v, list):
                                    result[k] = list(main_v) + list(sub_v)
                                elif sub_v is not None and main_v is None:
                                    result[k] = sub_v
                            for k in ("_winning_page_url", "_adapter_used"):
                                if hop_result.get(k) and not result.get(k):
                                    result[k] = hop_result[k]
                        else:
                            # Merge produced nothing usable — fall back to overwrite path.
                            for k in (
                                "units",
                                "extraction_tier_used",
                                "api_calls_intercepted",
                                "_winning_page_url",
                                "_raw_api_responses",
                                "_adapter_used",
                                "_fallback_chain",
                                "_tier_attempts",
                                "_llm_interactions",
                                "_llm_hints",
                                "_llm_analysis_results",
                                "_llm_field_mappings",
                                "_explored_links",
                            ):
                                if k in hop_result:
                                    result[k] = hop_result[k]
                    except Exception as exc:
                        log.warning("Phase 9 merge fallback for %s: %s", property_id, exc)
                        for k in (
                            "units",
                            "extraction_tier_used",
                            "api_calls_intercepted",
                            "_winning_page_url",
                            "_raw_api_responses",
                            "_adapter_used",
                            "_fallback_chain",
                            "_tier_attempts",
                            "_llm_interactions",
                            "_llm_hints",
                            "_llm_analysis_results",
                            "_llm_field_mappings",
                            "_explored_links",
                        ):
                            if k in hop_result:
                                result[k] = hop_result[k]
                else:
                    # Main empty (active path today): copy sub-page extraction
                    # fields wholesale. Telemetry from main is preserved
                    # because we only copy the listed extraction keys.
                    for k in (
                        "units",
                        "extraction_tier_used",
                        "api_calls_intercepted",
                        "_winning_page_url",
                        "_raw_api_responses",
                        "_adapter_used",
                        "_fallback_chain",
                        "_tier_attempts",
                        "_llm_interactions",
                        "_llm_hints",
                        "_llm_analysis_results",
                        "_llm_field_mappings",
                        "_explored_links",
                    ):
                        if k in hop_result:
                            result[k] = hop_result[k]
                for k in ("_link_hop_from", "_link_hop_depth", "_link_hop_score", "_link_hop_anchor"):
                    if k in hop_result:
                        result[k] = hop_result[k]
                result["_link_hop_success"] = True
            elif hop_result and hop_result.get("_units_empty"):
                # Phase 4: link-hop failed to recover data but we still
                # learned which sub-URLs had nothing. Feed that into the
                # profile so subsequent runs skip them.
                result["_explored_links"] = hop_result.get("_explored_links") or {}
                # Update adapter_name so downstream events see the real winner.
                adapter_name = result.get("_adapter_used", adapter_name)

    # Phase 3 — post-extraction CSV snap. Runs *after* extraction (H4) so
    # any record that hits the canonical floor-plan list inherits the
    # canonical name + a stable floor_plan_id. Records that don't snap fall
    # through to the merge cascade with their attribute-only identity intact.
    extracted_units = result.get("units") or []
    if extracted_units:
        try:
            from ma_poc.services.floorplan_snap import snap_units

            snapped = snap_units(extracted_units, property_id)
            result["units"] = snapped
            # Telemetry: how many rows snapped, and which reason set fired.
            snap_reasons: dict[str, int] = {}
            for u in snapped:
                r = u.get("floor_plan_snap_reason")
                if r:
                    snap_reasons[r] = snap_reasons.get(r, 0) + 1
            if snap_reasons:
                # Surface a summary for the per-property report; observability
                # below uses EventKind.EXTRACT_FLOOR_PLAN_SNAP per property.
                result["_floor_plan_snap_summary"] = snap_reasons
                try:
                    emit(
                        EventKind.EXTRACT_FLOOR_PLAN_SNAP,
                        property_id,
                        snap_reasons=snap_reasons,
                        unit_count=len(snapped),
                    )
                except Exception:
                    pass  # observability is best-effort
        except Exception as exc:  # noqa: BLE001
            log.warning("floorplan_snap failed for %s: %s", property_id, exc)

    # Phase 6 — aggregate property-level amenities and emit observation event.
    # Phase 7 — emit concessions observation event when present. Both are
    # purely observation (H7); they cannot fail the scrape.
    try:
        from ma_poc.reporting.observation_reports import aggregate_property_amenities

        units_now = result.get("units") or []
        explicit = (
            result.get("property_amenities")
            if isinstance(result.get("property_amenities"), list)
            else None
        )
        amenities = aggregate_property_amenities(units_now, explicit)
        result["property_amenities"] = amenities
        if amenities:
            try:
                emit(
                    EventKind.EXTRACT_AMENITIES_OBSERVED,
                    property_id,
                    count=len(amenities),
                    source_tier=result.get("extraction_tier_used") or "unknown",
                )
            except Exception:
                pass

        # Concession event — fires once per property when any unit carries
        # a concession_text. The full per-property detail goes into the
        # concessions report at run end.
        for u in units_now:
            text = u.get("concession_text")
            if isinstance(text, str) and text.strip():
                try:
                    emit(
                        EventKind.EXTRACT_CONCESSION_OBSERVED,
                        property_id,
                        source=u.get("concession_source") or "unspecified",
                        has_value=u.get("concession_value") is not None,
                    )
                except Exception:
                    pass
                break

        # Phase 4 — flag floor-plan-grain records for the report's
        # availability_quantity_observed counter.
        avail_records = sum(1 for u in units_now if u.get("availability_count"))
        if avail_records:
            try:
                emit(
                    EventKind.EXTRACT_AVAILABILITY_QUANTITY,
                    property_id,
                    record_count=avail_records,
                )
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001
        log.warning("observation hook failed for %s: %s", property_id, exc)

    tier_used = result.get("extraction_tier_used") or "unknown"
    if result.get("units"):
        emit(EventKind.TIER_WON, property_id, tier_used=tier_used)
    else:
        emit(EventKind.TIER_FAILED, property_id, tier_used=tier_used)

    # Delta 7: build ExtractResult with cost accounting
    extract_result = ExtractResult(
        property_id=property_id,
        records=result.get("units", []),
        tier_used=tier_used,
        adapter_name=adapter_name,
        winning_url=base_url,
        confidence=1.0 if result.get("units") else 0.0,
        llm_cost_usd=sum(i.get("cost_usd", 0) for i in result.get("_llm_interactions", [])),
        llm_calls=len(result.get("_llm_interactions", [])),
        errors=result.get("errors", []),
    )
    result["_extract_result"] = extract_result

    return result
