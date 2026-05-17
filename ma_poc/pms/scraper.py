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

import hashlib
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
from ma_poc.pms.resolver import ResolvedTarget, resolve_target

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


# Telemetry keys whose values are list-typed and concatenate across the
# main + sub-page sources. Order matters for downstream readers
# (cost_ledger walks _llm_interactions in arrival order), so we always
# place main before sub.
_MERGE_LIST_KEYS: tuple[str, ...] = (
    "_raw_api_responses",
    "_llm_interactions",
    "_llm_field_mappings",
    "_tier_attempts",
)

# Telemetry keys whose values are dict-typed and merge by key. Sub-page
# entries take priority on collision because the link-hop is the path
# that produced the data the merger ultimately kept. Without this list,
# self-learning artifacts (mappings, blocklist classifications, CSS
# selectors) discovered while crawling the sub-page were silently
# dropped on the TIER_MERGED_CROSS_PAGE path — see
# tests/integration/test_phase9_merge_preserves_learning.py.
_MERGE_DICT_KEYS: tuple[str, ...] = (
    "_llm_analysis_results",
    "_llm_hints",
    "_explored_links",
)


def _merge_post_hop_telemetry(
    result: dict[str, Any],
    hop_result: dict[str, Any],
) -> None:
    """Combine self-learning telemetry from main + sub-page extractions.

    Mutates ``result`` in place. Used after Phase 9 cross-page merge
    succeeds (TIER_MERGED_CROSS_PAGE) so the profile_updater sees every
    mapping, noise classification, CSS selector, and link-hop outcome the
    sub-page produced — not just main's.

    Rules:
      - List telemetry concatenates: ``main + sub`` (preserves arrival
        order). Lone-side values pass through unchanged.
      - Dict telemetry merges with sub winning collisions: link-hop is
        the data-bearing path, so its hints/classifications take priority.
        Lone-side dicts pass through unchanged.

    Why a helper: the merge block in scrape() is buried inside a
    multi-level nested async path, making the previous bug (omission of
    ``_llm_analysis_results`` from the merged keys) hard to spot. Lifting
    the rules to a named function with explicit key lists makes future
    additions a one-line edit and lets the integration test pin behaviour
    without spinning up the whole pipeline.
    """
    for k in _MERGE_LIST_KEYS:
        main_v = result.get(k)
        sub_v = hop_result.get(k)
        if isinstance(main_v, list) and isinstance(sub_v, list):
            result[k] = list(main_v) + list(sub_v)
        elif sub_v is not None and main_v is None:
            result[k] = sub_v
    for k in _MERGE_DICT_KEYS:
        main_v = result.get(k)
        sub_v = hop_result.get(k)
        if isinstance(main_v, dict) and isinstance(sub_v, dict):
            # Sub-page wins collisions — see _MERGE_DICT_KEYS docstring.
            result[k] = {**main_v, **sub_v}
        elif sub_v is not None and main_v is None:
            result[k] = sub_v
    # Provenance fields — back-fill from sub-page when main never set them.
    # The link-hop did the data-bearing work so its winning_page_url /
    # adapter_used are the right answer when main has nothing to say.
    for k in ("_winning_page_url", "_adapter_used"):
        if hop_result.get(k) and not result.get(k):
            result[k] = hop_result[k]

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


# F0.1: hard ceiling so a misconfigured PROPERTY_LLM_COST_CAP_HOP_BONUS_USD
# (or many compounding hops, in a future per-hop variant) cannot uncap spend.
_COST_CAP_HOP_CEILING_MULTIPLIER = 3.0

# Bug 5 alignment (2026-05-09 deep-dive): minimum body bytes a hop must have
# before we consider granting a fresh LLM rescue budget. Below this the body
# is almost certainly a redirect, login wall, or near-empty SPA shell —
# bumping the cap there is wasted spend.
_RICH_HOP_MIN_BODY_BYTES = 50_000

# Cheap content markers that suggest unit-bearing structured data is present.
# Either marker + body size threshold qualifies the hop as "rich."
_RICH_HOP_JSONLD_MARKERS = ("FloorPlan", "ApartmentComplex", "Apartment\"")


def _link_hop_is_rich(fetch_result: Any) -> bool:
    """Bug 5 alignment: should this hop's body trigger a cost-cap refresh?

    True only when the body is large enough AND carries a positive content
    signal (JSON-LD FloorPlan/Apartment OR ≥2 structural floor-plan signal
    types from the signal engine).  Filters out redirect bodies, login walls,
    and Cloudflare interstitials that the previous unconditional refresh
    blindly subsidised.
    """
    if fetch_result is None:
        return False
    body = getattr(fetch_result, "body", None)
    if not body:
        return False
    if isinstance(body, bytes):
        if len(body) < _RICH_HOP_MIN_BODY_BYTES:
            return False
        try:
            body_str = body.decode("utf-8", errors="replace")
        except Exception:
            return False
    elif isinstance(body, str):
        if len(body) < _RICH_HOP_MIN_BODY_BYTES:
            return False
        body_str = body
    else:
        return False

    for marker in _RICH_HOP_JSONLD_MARKERS:
        if marker in body_str:
            return True
    from ma_poc.pms.signal_engine.floor_plan_signals import (
        has_floor_plan_signals,
        SIGNAL_THRESHOLD_STRUCTURAL,
    )
    return has_floor_plan_signals(body_str, SIGNAL_THRESHOLD_STRUCTURAL)


def _refresh_cost_cap_for_hop(
    budget: dict[str, Any],
    *,
    property_id: str | None = None,
    sub_url: str | None = None,
    hop_index: int | None = None,
) -> bool:
    """Grant a cost-cap bonus before/during a rich link-hop session.

    F0.1 + Bug 5 alignment: link-hop sub-pages (``/availability``,
    ``/floor-plans``) are where the unit data typically lives. When the
    entry page exhausted the per-property LLM cost cap on its own (e.g.
    low-content marketing shell + an expensive monolithic call), the
    sub-page never gets to use the LLM rescue path and the property fails.
    Caller gates on ``_link_hop_is_rich`` before calling so we don't
    subsidise login walls or redirects.

    Bounded by ``base_cap × _COST_CAP_HOP_CEILING_MULTIPLIER`` (default 3×)
    so a misconfigured env var cannot create runaway spend. Mutates
    ``budget`` in place — the same dict reference flows into the hopped
    sub-page via ``shared_budget`` so the new cap is observed by the
    GenericAdapter cost gate.

    Returns True if the cap was actually raised (so callers can decide
    whether to emit telemetry).
    """
    try:
        from ma_poc.services.source_planner import (
            get_property_llm_cost_cap_hop_bonus_usd,
            get_property_llm_cost_cap_usd,
        )
    except Exception:
        return False
    base = get_property_llm_cost_cap_usd()
    bonus = get_property_llm_cost_cap_hop_bonus_usd()
    ceiling = base * _COST_CAP_HOP_CEILING_MULTIPLIER
    try:
        current = float(budget.get("_cost_cap_usd", base) or base)
    except (TypeError, ValueError):
        current = base
    new_cap = min(current + bonus, ceiling)
    if new_cap <= current:
        # Already at ceiling — no-op. Caller can still observe the attempt
        # via the return value if it wants to surface "would have refreshed
        # but was clamped" telemetry. Today we keep the helper silent.
        return False
    budget["_cost_cap_usd"] = new_cap

    if property_id:
        try:
            from ma_poc.observability.events import EventKind, emit

            emit(
                EventKind.LINK_HOP_BUDGET_REFRESH,
                property_id,
                sub_url=sub_url,
                hop_index=hop_index,
                old_cap_usd=round(current, 4),
                new_cap_usd=round(new_cap, 4),
                ceiling_usd=round(ceiling, 4),
            )
        except Exception:
            # Telemetry must never break the cap refresh.
            pass
    return True


def _refresh_monolithic_budget_for_llm_hint(
    budget: dict[str, Any],
    *,
    property_id: str | None = None,
    sub_url: str | None = None,
    hop_index: int | None = None,
) -> bool:
    """Grant a fresh monolithic LLM call when hopping to an LLM-hinted URL.

    The LLM only emits ``navigation_hint`` when it has diagnosed that the
    unit data lives on a different page — i.e. it has already paid the
    introspection cost on the entry page and is telling us where to look.
    In that case the entry-page call legitimately consumed the
    per-property ``llm_monolithic`` counter (default = 1) and any sub-page
    rescue would be denied. We treat the LLM's hint as high-confidence
    evidence and refresh the counter to at least 1 so the hinted page can
    use the monolithic LLM tier if its deterministic parsers also miss.

    Mutates ``budget`` in place — the same dict reference flows into the
    sub-page's ``scrape()`` call via ``shared_budget`` so the new counter
    is observed by the GenericAdapter cost gate.

    Returns True when the counter was actually raised.
    """
    try:
        current = int(budget.get("llm_monolithic", 0) or 0)
    except (TypeError, ValueError):
        current = 0
    if current >= 1:
        return False
    budget["llm_monolithic"] = 1
    if property_id:
        try:
            from ma_poc.observability.events import EventKind, emit

            # NOTE: do not use ``kind=`` as a kwarg — that collides with
            # ``emit(kind: EventKind, ...)``'s positional parameter and
            # raised TypeError silently swallowed by the except below.
            # Use ``refresh_kind`` so analysers can distinguish counter
            # refreshes from cost-cap refreshes.
            emit(
                EventKind.LINK_HOP_BUDGET_REFRESH,
                property_id,
                sub_url=sub_url,
                hop_index=hop_index,
                refresh_kind="llm_monolithic_counter",
                old_value=current,
                new_value=1,
            )
        except Exception:
            pass
    return True


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
    hop_depth: int = 0,
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
    shared_budget : dict | None
        Per-property LLM budget. **Mutated in place** when provided —
        sub-tier LLM calls in GenericAdapter decrement
        ``llm_api_calls`` / ``llm_dom_calls`` / ``llm_monolithic`` against
        this dict and accumulate cost into ``_cost_usd_spent``. Pass the
        same dict reference into recursive ``scrape()`` calls (e.g.
        link-hop) so decrements compose; passing a fresh copy reverts to
        the pre-Fix#2 behaviour where one property could fire 20 LLM
        calls (1 entry × 5 + 3 hops × 5).

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
    # resolve_target uses the live page for CTA-hop; skip it if we're in
    # fetch-only mode (no page) — adapters will work from the fetched HTML
    # of the original URL.
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
    #
    # IMPORTANT — this is a reference, not a copy. Link-hop reuses the same
    # dict across the entry page and each sub-page so LLM-call decrements
    # propagate up; otherwise each link-hop sub-page would silently get a
    # fresh 5-call budget (3+1+1) and one property could fire 20 LLM calls
    # via 1 entry + 3 hops × 5 = 20. The earlier `dict(shared_budget)` copy
    # was the root cause of the per-day stuck-shard burns.
    if shared_budget is not None:
        budget: dict = shared_budget
    else:
        # F0.1: include _cost_cap_usd in the fallback so the env override
        # still applies on the no-profile path. compute_budget below also
        # injects it; this keeps both branches consistent.
        from ma_poc.services.source_planner import get_property_llm_cost_cap_usd
        budget = {
            "llm_api_calls": 3,
            "llm_dom_calls": 1,
            "llm_monolithic": 1,
            "link_hop": 3,
            "_cost_cap_usd": get_property_llm_cost_cap_usd(),
        }
        if profile is not None:
            try:
                from ma_poc.services.source_planner import compute_budget
                from ma_poc.models.scrape_profile import ProfileMaturity
                is_cold = profile.confidence.maturity == ProfileMaturity.COLD
                budget = compute_budget(profile, is_cold=is_cold)
            except Exception:
                pass

    _html_char = result.get("_html_characterization") or {}
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
        floor_plan_signal_count=int(_html_char.get("floor_plan_signal_count", 0) or 0),
        hop_depth=hop_depth,
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

    # Bug #5 fix (2026-05-16): populate candidate_portal_urls from
    # entry-page iframe / inline-JS / data-attribute scan so the Entrata
    # adapter's ``_probe_known_endpoints`` can hit ``comms.entrata.com/
    # widget?...`` URLs via ``page.evaluate(fetch ...)`` and inherit the
    # CF clearance cookie. ``_extract_portal_iframe_hints`` already runs
    # later (line ~970) to feed link-hop candidates; we mirror it here so
    # the adapter sees the URLs first, before link-hop scheduling.
    if page_html:
        try:
            _entry_portal_hints = _extract_portal_iframe_hints(page_html)
        except Exception:
            _entry_portal_hints = []
        if _entry_portal_hints:
            ctx.candidate_portal_urls = [u for u, _ in _entry_portal_hints]
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

        prepared: list[dict[str, Any]] = []
        for entry in network_log:
            if not isinstance(entry, dict):
                continue
            raw_body = entry.get("body")
            parsed_body: Any = raw_body
            if isinstance(raw_body, str) and raw_body.strip().startswith(("{", "[")):
                try:
                    parsed_body = _json.loads(raw_body)
                except Exception:
                    parsed_body = raw_body
            prepared.append(
                {
                    "url": entry.get("url", ""),
                    "body": parsed_body,
                    "status": entry.get("status"),
                    "content_type": entry.get("content_type"),
                    # F1.2: forward the per-entry captcha flag so the LLM
                    # rescue's _filter_candidates can drop interstitial
                    # bodies. Populated by Fetcher._do_render's network_log
                    # capture; default False keeps non-render paths safe.
                    "captcha_detected": bool(entry.get("captcha_detected", False)),
                }
            )
        ctx._api_responses = prepared  # type: ignore[attr-defined]

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

        # F1.3 / Bug 2 (2026-05-09): gate on ``adapter_name`` (resolved adapter
        # the scraper actually called) — NOT ``pms_name`` (URL-based detection
        # which can be ``unknown`` after F0.2 demotion). The previous
        # ``pms_name`` gate locked rescue out of every detection-demoted
        # property, costing ~300 properties/run.
        # F1.2: also short-circuit on captcha — bodies captured behind a
        # Cloudflare interstitial are noise the rescue can't extract from.
        # Bug D (2026-05-11): the allow-list is owned by ``llm_api_rescue.py``
        # (it enforces the same invariant) and imported here. Drift between
        # the two gates caused ``unsupported adapter`` rejections for 427
        # OneSite + AMLI properties/day until the import landed. P2 — see
        # docs/2026_05_11_regressions_fix_design.md.
        from ma_poc.services.llm_api_rescue import SUPPORTED_ADAPTERS

        captcha_detected = bool(getattr(fetch_result, "captcha_detected", False))
        # D16 post-partition guard: a plan-only site emits zero rows into
        # ``adapter_result.units`` but populates ``plan_summaries`` with
        # successfully-extracted plan rows. Treat that as "adapter found
        # something" — skip rescue, otherwise we re-run the LLM on the same
        # API bodies the adapter already classified as plan-aggregate.
        adapter_found_plan_rows = bool(getattr(adapter_result, "plan_summaries", None))
        needs_rescue = (
            not property_passes_quality_gate(adapter_result.units)
            and not adapter_found_plan_rows
            and bool(raw_api_responses)
            and adapter_name in SUPPORTED_ADAPTERS
            and consecutive_rescue_failures < 3
            and not page_unreachable
            and not captcha_detected
        )

        if not needs_rescue and captcha_detected and bool(raw_api_responses):
            # F1.2: surface the bot-block separately from FAILED_NO_DATA so
            # the run report doesn't bury captcha pages in the generic
            # extraction-failure bucket.
            emit(
                EventKind.LLM_RESCUE_SKIPPED,
                ctx.property_id,
                source_adapter=adapter_name,
                reason="captcha_detected",
            )
            result["_rescue_skipped_reason"] = "captcha_detected"

        if needs_rescue:
            from ma_poc.services.llm_api_rescue import RescueInput, rescue_from_api_responses

            emit(
                EventKind.LLM_RESCUE_ATTEMPTED,
                ctx.property_id,
                source_adapter=adapter_name,
                n_raw_candidates=len(raw_api_responses),
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
                    source_adapter=adapter_name,
                    api_responses=raw_api_responses,
                    profile_snapshot=(
                        ctx.profile.model_dump(mode="json") if ctx.profile is not None else None
                    ),
                )
            )

            result["_rescue_cost_usd"] = rescue.cost_usd
            result["_rescue_n_filtered_candidates"] = rescue.n_filtered_candidates

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

            # F1.4 (2026-05-08 implementation plan): bridge BOTH
            # ``rescue.blocked_endpoints`` (always) AND
            # ``rescue.llm_field_mappings`` (success only) into
            # ``adapter_result._llm_analysis_results`` so the lift at the
            # bottom of this function picks them up coherently. Replaces
            # the prior ``result["_llm_analysis_results"]`` write that was
            # silently overwritten by the in-line generic-LLM tier.
            #
            # CRITICAL key normalization: rescue emits ``envelope`` but
            # ``profile_updater.save_llm_field_mapping`` reads
            # ``response_envelope``. Without this rename, the persisted
            # ``LlmFieldMapping`` has empty ``response_envelope`` →
            # replay returns empty → quality_score=0.4 → mapping persists
            # but never short-circuits the LLM cost on subsequent runs.
            # Normalizing here (not at rescue source) avoids changing the
            # ``RescueOutput.llm_field_mappings`` contract for any other
            # consumer.
            if rescue.blocked_endpoints or (rescue.units and rescue.llm_field_mappings):
                existing = getattr(adapter_result, "_llm_analysis_results", None) or {}
                if not isinstance(existing, dict):
                    existing = {}

                for blocked_url, reason in rescue.blocked_endpoints:
                    # Don't clobber a successful-mapping entry for the same URL.
                    if blocked_url in existing and isinstance(existing[blocked_url], dict):
                        continue
                    existing[blocked_url] = f"noise:{reason}"

                if rescue.units:
                    for m in rescue.llm_field_mappings:
                        url_key = m.get("api_url_pattern") or rescue.winning_url
                        if not url_key:
                            continue
                        normalized = dict(m)
                        if "envelope" in normalized and "response_envelope" not in normalized:
                            normalized["response_envelope"] = normalized.pop("envelope")
                        # Don't overwrite an earlier good mapping for the same URL.
                        prior = existing.get(url_key)
                        if isinstance(prior, dict):
                            continue
                        existing[url_key] = normalized

                adapter_result._llm_analysis_results = existing  # type: ignore[attr-defined]

            result["_rescue_attempted"] = True
            result["_rescue_succeeded"] = bool(rescue.units)
            result["_rescue_n_llm_calls"] = rescue.n_llm_calls
    except Exception as _rescue_exc:
        log.warning("F2 rescue orchestration failed for %s: %s", property_id, _rescue_exc)

    # --- Step 8: Fallback to generic if adapter returned empty ---
    # D16 post-partition guard: ``plan_summaries`` carries successfully-
    # extracted plan-aggregate rows. Treat them as "adapter found something"
    # — skipping the generic re-extraction avoids re-running 5 tiers on the
    # same page when the dedicated adapter already classified the available
    # rows. Plan-only sites legitimately have no unit-level inventory.
    _adapter_has_plan_rows = bool(getattr(adapter_result, "plan_summaries", None))
    if (
        not adapter_result.units
        and not _adapter_has_plan_rows
        and pms_name != "unknown"
        and adapter_name != "generic"
    ):
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
            hop_depth=ctx.hop_depth,
            floor_plan_signal_count=ctx.floor_plan_signal_count,
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
            # Always promote portal hints from the generic fallback even when
            # it extracted 0 units. The fallback's embedded-JSON tier may have
            # found a leasing-portal pointer (SightMap embed URL, RealPage OLL
            # config) inside an SSR blob — those hints must reach link-hop
            # regardless of whether the fallback recovered any units here.
            _fallback_portal_hints = getattr(
                fallback_result, "_embedded_portal_hints", None
            )
            if _fallback_portal_hints:
                _existing_portal = getattr(
                    adapter_result, "_embedded_portal_hints", None
                ) or []
                adapter_result._embedded_portal_hints = (  # type: ignore[attr-defined]
                    _existing_portal + _fallback_portal_hints
                )
        except Exception as exc:
            adapter_result.errors.append(f"generic-fallback-error: {exc}")

    # --- Step 9: Populate legacy result ---
    result["units"] = adapter_result.units
    # D16: surface plan-aggregate rows separately from unit-level rows.
    # Adapters populate ``plan_summaries`` from ``PostProcessResult.plan_summaries``;
    # downstream consumers (reporting, run_report) read the count without
    # conflating with the unit-level inventory.
    result["plan_summaries"] = list(getattr(adapter_result, "plan_summaries", []) or [])
    # D16: cross-page unit-number dedup + inverse-B4 telemetry. Stashed
    # under ``_post_process_meta`` so the run report can aggregate them
    # without scanning every property's full PostProcessResult. Sourced
    # from the typed ``AdapterResult.post_process_meta`` field
    # (``None`` when the adapter took an early-exit path before
    # ``post_process``).
    if adapter_result.post_process_meta:
        result["_post_process_meta"] = dict(adapter_result.post_process_meta)
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

    # Leasing-portal pointers discovered in embedded JSON blobs (Jonah
    # widget configs, headless WordPress marketing shells, etc. that
    # delegate unit data to SightMap / RealPage OLL / RentCafe / etc.).
    # Forwarded to link-hop, which fetches each URL and lets the host's
    # fingerprint route it to the matching PMS adapter.
    portal_hints = getattr(adapter_result, "_embedded_portal_hints", None)
    if portal_hints:
        result["_embedded_portal_hints"] = list(portal_hints)

    # Scan entry-page raw HTML for portal iframes (sightmap.com/embed/,
    # AppFolio widget, etc.). detect_embedded_portal_urls (called inside
    # generic adapter) only walks embedded JSON blobs; raw <iframe src>
    # attributes are surfaced separately by _extract_portal_iframe_hints.
    # A page can legitimately carry BOTH outer-HTML units AND an iframe
    # portal containing more (or higher-quality) data — PID 229594
    # venterraliving is the canonical case: 4 outer FP signals AND a
    # sightmap.com/embed/n9w6mmzrv71 iframe with 50+ units behind it.
    if page_html:
        try:
            _entry_iframe_hints = _extract_portal_iframe_hints(page_html)
        except Exception:
            _entry_iframe_hints = []
        if _entry_iframe_hints:
            _existing_hints = list(result.get("_embedded_portal_hints") or [])
            _seen_urls = {u for u, _ in _existing_hints}
            for _ih_url, _ih_name in _entry_iframe_hints:
                if _ih_url not in _seen_urls:
                    _existing_hints.append((_ih_url, f"iframe:{_ih_name}"))
                    _seen_urls.add(_ih_url)
                    # Telemetry: surface unknown-portal discoveries so
                    # cross-run aggregation can identify trending vendors
                    # we haven't catalogued. Hosts with high success rates
                    # are promotion candidates to _PORTAL_URL_PATTERNS.
                    if _ih_name.startswith("unknown:"):
                        try:
                            from ma_poc.observability.events import EventKind
                            emit(
                                EventKind.EMBEDDED_PORTAL_UNKNOWN_HOST_SEEN,
                                property_id,
                                host=_ih_name.split(":", 1)[1],
                                url=_ih_url[:300],
                                source="iframe_src",
                            )
                        except Exception:
                            pass
            result["_embedded_portal_hints"] = _existing_hints

    floorplan_hints = getattr(adapter_result, "_embedded_floorplan_subpage_hints", None)
    if floorplan_hints:
        result["_embedded_floorplan_subpage_hints"] = list(floorplan_hints)

    # Surface property-level amenities collected by any LLM tier so the
    # ``aggregate_property_amenities`` step downstream finally has data
    # to read. Adapter writes the cross-tier-deduped list to
    # ``adapter_result._property_amenities`` (see GenericAdapter); we
    # promote it here onto ``result["property_amenities"]`` as the
    # ``explicit`` source for the existing aggregator.
    prop_amen = getattr(adapter_result, "_property_amenities", None)
    if prop_amen:
        result["property_amenities"] = list(prop_amen)

    return result


# ---------------------------------------------------------------------------
# Jugnu J3 — new entry point that takes CrawlTask + FetchResult
# ---------------------------------------------------------------------------


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

    # B1: floor-plan structural signal count replaces the $NNN rent-signal count.
    # clean_text is already stripped of HTML tags (re-use the tag-stripped version).
    from ma_poc.pms.signal_engine.floor_plan_signals import (
        count_floor_plan_signals,
        count_floor_plan_signal_cardinality,
    )
    clean_text = re.sub(r"<[^>]+>", "", stripped)
    floor_plan_signals = count_floor_plan_signals(clean_text)
    # RC-CARD (2026-05-15 PM): uncapped cardinality metrics. The existing
    # floor_plan_signal_count is the 0-4 type-score (preserved); these
    # new fields expose volume so a 50-unit listings page can be
    # distinguished from a 5-signal stub when ranking sources downstream.
    # See FloorPlanSignalCardinality docstring + cloud_run_2026-05-15
    # TRIAGE for the motivation (fp_signal_count caps at 4, hiding the
    # difference between 25-signal real listings and 5-signal stubs).
    fp_card = count_floor_plan_signal_cardinality(clean_text, page_html)

    # SPA heuristic: lots of script, little text, no floor-plan signals.
    spa_score = 0.0
    if body_bytes > 0:
        script_ratio = 1.0 - min(1.0, text_bytes / max(1, body_bytes))
        spa_score += 0.4 * script_ratio
    if "__NEXT_DATA__" in page_html or "__NUXT__" in page_html:
        spa_score += 0.3
    if floor_plan_signals == 0 and text_bytes < 5000:
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
        "floor_plan_signal_count": floor_plan_signals,
        # Cardinality fields (RC-CARD 2026-05-15) — uncapped volume.
        "fp_unique_rents": fp_card.unique_rents,
        "fp_unique_bed_bath_pairs": fp_card.unique_bed_bath_pairs,
        "fp_listing_rows_with_rent": fp_card.listing_rows_with_rent,
        "fp_jsonld_offer_count": fp_card.jsonld_offer_count,
        "fp_jsonld_apartment_count": fp_card.jsonld_apartment_count,
        "fp_property_value_bed_bath": fp_card.property_value_bed_bath,
        "fp_matched_signal_bytes": fp_card.matched_signal_bytes,
    }


# ── Link-hop (Phase-4 equivalent) ─────────────────────────────────────────
# When the entry URL produces no units, rank the internal links on the
# home page and re-fetch the top candidates. This is a one-level BFS capped
# at N sub-fetches so a failing property can't consume unbounded budget.
# Typical win case: RentCafe/Entrata/AppFolio vanity home pages that embed
# tracking scripts but don't carry unit data — the real portal is one
# "View Availability" click away.


# Sentinel score for LLM-emitted navigation hints. Detected downstream
# Phase 2: scoring constants imported from signal_engine.defaults — single
# source of truth. Aliases preserved here so existing code at call sites
# compiles without changes until Phase 4 cleanup removes the definitions.
from ma_poc.pms.signal_engine.defaults import (  # noqa: E402
    LLM_HINT_SCORE as _LLM_HINT_SCORE,
    EMBEDDED_PORTAL_SCORE as _EMBEDDED_PORTAL_SCORE,
    PMS_PRIOR_SCORE as _PMS_PRIOR_SCORE,
    DEFAULT_ANCHOR_KEYWORDS as _LINK_ANCHOR_KEYWORDS,
    DEFAULT_PATH_KEYWORDS as _LINK_PATH_KEYWORDS,
    DEFAULT_HOST_KEYWORDS as _LINK_HOST_KEYWORDS,
    DEFAULT_PMS_PRIORS as _PMS_SUB_PATH_PRIORS,
    DEFAULT_UNIVERSAL_PRIORS as _UNIVERSAL_SUB_PATH_PRIORS,
)

_LLM_HINT_ANCHOR_PREFIX = "llm-hint:"
_EMBEDDED_PORTAL_ANCHOR_PREFIX = "embedded-portal:"

# Maximum number of floor-plan sub-pages to accumulate before stopping.
# Prevents timeout spirals where Entrata/ProspectPortal sites with 10+
# individual /floorplans/<name> sub-pages exhaust the 600 s budget.
_MAX_FLOORPLAN_ACCUM_PAGES = 8

# Pre-compiled regex for extracting iframe src attributes from raw HTML.
# Used to discover portal iframes (AppFolio, SightMap, etc.) on hop pages
# that have no floor-plan signals in the outer DOM.
_IFRAME_SRC_RE = re.compile(
    r'<iframe[^>]+\bsrc=["\']([^"\'>\s]+)["\']',
    re.IGNORECASE,
)

# Bug #4 fix (2026-05-16): lazy-loaded iframes use ``data-src`` /
# ``data-iframe-src`` so the real URL isn't in ``src=`` until JS hydration
# fires. SightMap properties on Squarespace/Wix Studio embed via lazy-load
# pattern; static-HTML iframe scan misses them entirely. Canonical victims:
# PID 16139 chaseknollsapts.com, 20959 dovevalleyapts.com — both have
# ``sightmap.com/embed/`` in HTML (so the PMS detector routes correctly)
# but the URL is buried in ``data-*`` attributes the original scan ignored.
_IFRAME_DATA_SRC_RE = re.compile(
    r'<iframe[^>]+\bdata(?:-iframe)?-src=["\']([^"\'>\s]+)["\']',
    re.IGNORECASE,
)

# Bug #4 fix (2026-05-16): catch-all for SightMap embed URLs anywhere in
# HTML — covers data-attribute placeholders, inline JS config blobs, and
# rendered iframe URLs that ``_IFRAME_SRC_RE`` / ``_IFRAME_DATA_SRC_RE``
# miss because the attribute name varies per CMS. The 11-char embed-ID
# alphabet is empirically observed: lowercase alphanumeric, occasional
# hyphens. Matches: ``https://sightmap.com/embed/n9w6mmzrv71``, ``https://
# embed.engrain.com/{id}``.
_SIGHTMAP_EMBED_URL_RE = re.compile(
    r'https?://(?:sightmap\.com/embed|embed\.engrain\.com)/[a-zA-Z0-9_-]{4,}',
    re.IGNORECASE,
)

# Anchor hrefs — second pass of _extract_portal_iframe_hints, catches
# portal URLs linked rather than iframed.
_ANCHOR_HREF_RE = re.compile(
    r'<a[^>]+\bhref=["\']([^"\'>\s]+)["\']',
    re.IGNORECASE,
)

# Quoted absolute URL inside any HTML — third pass. Catches portal URLs
# embedded in inline JavaScript objects (e.g. mark-taylor.com ships
# ``"yardi_apply_now_link":"https://9026050.onlineleasing.realpage.com/..."``
# inside a regular <script> tag whose JSON-string-literal members are not
# parsed by extract_embedded_blobs_from_html). Pattern is conservative —
# requires the URL to be wrapped in single or double quotes so we don't
# match URL-like substrings in comments or stack traces.
_QUOTED_URL_RE = re.compile(
    r'''["'](https?://[^"'\s<>]+)["']''',
)

# Regexes used by _strip_html_for_dedup. Visible text only — script/style
# blocks and their content are removed, tags stripped, whitespace collapsed.
# Same SPA shell delivered under different URL paths (Angular tab-switcher,
# AppFolio /sign_in/* auth wall) shares the same stripped text even when the
# raw body differs by 26KB of inline state (timestamps, CSRF, chunk URLs).
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html_for_dedup(html: str) -> str:
    """Strip HTML to visible text for dedup hashing.

    Removes <script> and <style> blocks (including content), all tags, then
    collapses whitespace. The result is a deterministic representation of
    what a reader sees — robust against inline-state churn that breaks raw
    byte hashing.
    """
    if not html:
        return ""
    s = _SCRIPT_STYLE_RE.sub(" ", html)
    s = _TAG_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def _extract_portal_iframe_hints(html: str) -> list[tuple[str, str]]:
    """Extract leasing-portal URLs from <iframe src> AND <a href> attributes.

    Two discovery paths:
      • iframe src — covers cross-origin embeds (AppFolio listings iframe,
        SightMap embed, etc.)
      • anchor href — covers portal CTAs ("Apply Now" links to
        ``{property-id}.onlineleasing.realpage.com``, etc.) which previously
        only got the low keyword-anchor score and lost to 404-ing PMS priors

    Hits are returned with their portal-name tag; the caller promotes them
    to ``_EMBEDDED_PORTAL_SCORE`` so they outrank PMS priors.

    Args:
        html: Rendered outer-frame HTML of the hop or entry page.

    Returns:
        List of ``(url, portal_name)`` tuples in iframe-first then
        anchor-first order. Duplicates by URL are dropped.
    """
    try:
        from ma_poc.pms.adapters._html_extract import _PORTAL_URL_PATTERNS
    except Exception:
        return []

    hints: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _scan(regex: re.Pattern[str]) -> None:
        for match in regex.finditer(html):
            src = match.group(1).strip()
            if not src or not src.startswith("http") or src in seen:
                continue
            lower = src.lower()
            # SightMap SDK loader: the JS bundle at sightmap.com/embed/api.js
            # matches the same "sightmap.com/embed/" needle as the real
            # iframe data URL (sightmap.com/embed/{embedId}). All SightMap
            # properties load the SDK from the same URL — body=5649 bytes,
            # zero unit data. The 3rd-pass quoted-URL scan picks it up when
            # the iframe is JS-injected at runtime. Skip the SDK and let the
            # iframe-src / anchor scan catch the real data URL when present.
            # See data/reports/cloud_run_2026-05-15/TRIAGE.md RC #3a.
            if lower.endswith("/embed/api.js") or "/embed/api.js?" in lower:
                continue
            # 2026-05-17 — apply the portal-infra blacklist before matching
            # known patterns. The bare ``.yardi.com`` needle at
            # _html_extract.py:1112 was matching ``resources.yardi.com/
            # legal/cookie-notice/`` and queuing it as ``portal_name=yardi``
            # at score 10110, beating per-plan detail URLs (PID 52331).
            # The blacklist filter was only running in the 6th-pass
            # unknown-portal scan; lift it here so known patterns can't
            # admit Yardi's own corporate/consent infra.
            try:
                from ma_poc.pms.adapters._html_extract import _is_portal_infra_blacklisted
                if _is_portal_infra_blacklisted(src):
                    continue
            except Exception:
                pass
            for needle, portal_name in _PORTAL_URL_PATTERNS:
                if needle in lower:
                    seen.add(src)
                    hints.append((src, portal_name))
                    break

    _scan(_IFRAME_SRC_RE)
    _scan(_IFRAME_DATA_SRC_RE)  # Bug #4 fix: lazy-loaded iframes
    _scan(_ANCHOR_HREF_RE)
    _scan(_QUOTED_URL_RE)

    # Bug #4 fix (2026-05-16): explicit SightMap embed-URL scan. Belt-and-
    # braces for cases where the URL is buried in a context the four passes
    # above don't match (data-* attribute we haven't enumerated, inline JS
    # object literal without quote-wrapping, etc.). Hits go to the
    # ``sightmap`` portal tag directly.
    for match in _SIGHTMAP_EMBED_URL_RE.finditer(html):
        src = match.group(0).strip()
        if src and src not in seen:
            # Skip the SDK loader — same rule as the iframe-src scan.
            lower = src.lower()
            if lower.endswith("/embed/api.js") or "/embed/api.js?" in lower:
                continue
            seen.add(src)
            hints.append((src, "sightmap"))

    # 4th pass: inline-JS PMS init parser. Catches portals whose iframe is
    # injected by JS at runtime (no static <iframe src>) — the entry HTML
    # contains only an init call like SightMap.init({sightmap_id: "x"}) or a
    # data-* attribute placeholder. We extract the property-specific key and
    # synthesize the canonical data URL ourselves.
    # See data/canary/local_runs/fix-validation-2026-05-15/UNCHANGED_FAIL_ANALYSIS.md
    # PID 20959 (dovevalleyapts.com) — SightMap iframe is JS-injected.
    for synth_url, portal_name in _scan_inline_js_pms_init(html):
        if synth_url not in seen:
            seen.add(synth_url)
            hints.append((synth_url, portal_name))

    # 5th pass: AppFolio slug synthesis. AppFolio CMS marketing sites often
    # embed Pay-Rent / Tenant-Portal iframes at
    # {slug}.appfolio.com/connect/users/sign_in (auth shells with zero unit
    # data) but NOT a listings iframe. We detect the slug via the
    # _APPFOLIO_SUBDOMAIN_RE regex and synthesize {slug}.appfolio.com/listings
    # so the AppFolio adapter's existing SSR-DOM / API-capture paths can run.
    # See data/canary/local_runs/fix-validation-2026-05-15/UNCHANGED_FAIL_ANALYSIS.md
    # PIDs 259733, 11399, 50178 — AppFolio cluster.
    has_listings = any("/listings" in u.lower() for u, _ in hints)
    if not has_listings:
        try:
            from ma_poc.pms.detector import _APPFOLIO_SUBDOMAIN_RE

            appfolio_slugs: set[str] = set()
            for m in _APPFOLIO_SUBDOMAIN_RE.finditer(html):
                slug = m.group(1).lower()
                # Skip generic / non-tenant subdomains.
                if slug not in {"www", "app", "apartments", "widgets", "account", "static"}:
                    appfolio_slugs.add(slug)
            for slug in appfolio_slugs:
                synthetic = f"https://{slug}.appfolio.com/listings"
                if synthetic not in seen:
                    seen.add(synthetic)
                    # Prepend so the listings URL fires before any AppFolio
                    # auth-side hits the caller's hop queue.
                    hints.insert(0, (synthetic, "appfolio"))
        except Exception:
            pass

    # 6th pass: generic unknown-portal discovery. Catches NEW vendor iframes
    # we haven't catalogued in _PORTAL_URL_PATTERNS yet. Walks every
    # <iframe src>, skips anything on the infra blacklist (analytics, maps,
    # chat, social, parastorage CDN, etc.), and queues remaining cross-origin
    # iframes as candidates. Score is LOWER than known-portal (10_000) but
    # higher than PMS_PRIOR (5_000) so a NEW vendor still beats template
    # guesses without overshadowing known-good portals.
    #
    # The user's 2026-05-15 architectural feedback: hardcoded patterns
    # prevent the system from learning at runtime. This generic pass +
    # the telemetry event below close the loop:
    # 1. Unknown vendor iframe → queued + telemetry emit
    # 2. If the URL yields units, the success-rate metric improves immediately
    # 3. Cross-run aggregation of `embedded_portal.unknown_host_seen` events
    #    surfaces trending hosts → promote to hardcoded list in a follow-up
    # 4. Per-property profile (Phase 3, deferred) persists known-good hosts
    #    so the same property's next run skips the discovery probe
    try:
        from ma_poc.pms.adapters._html_extract import (
            _is_portal_infra_blacklisted,
            _iframe_host,
        )

        # Discover hosts already hinted (any known-portal pass plus the
        # 4th/5th passes above) so the unknown scan doesn't duplicate them.
        already_hosted = {_iframe_host(u) for u, _ in hints if u}
        unknown_emit_count = 0
        # Hard cap so a page with many vendor iframes (e.g. social embeds,
        # video players) can't blow up the hop queue. 3 is enough to catch
        # the property's leasing widget while leaving budget for PMS priors.
        _UNKNOWN_PORTAL_CAP = 3
        # Score positioning (revised 2026-05-15 PM):
        #   - PROFILE_WINNING: 10_001
        #   - LLM_HINT / EXTERNAL_PORTAL (known): 10_000
        #   - PROFILE_NAV_HINT / API_RESPONSE: 9_500
        #   - strong anchor (apply/floor-plan + path match): 5_600
        #   - PMS_PRIOR: 5_000
        #   - UNIVERSAL_PRIOR: 4_500
        #   - INTERNAL_LINK base + keyword bonuses: 4_000–4_300
        #
        # The original 9_000 placed unknown iframes ABOVE every real anchor on
        # the page, even ones with floor-plan keywords. Cloud run 2026-05-15
        # caught chat widgets (my.hy.ly/chat/ssid), graphic embeds
        # (canva.com/design/.../view), and similar non-leasing iframes ranked
        # at 9_000 — outranking the actual `<a href="/floor-plans">` link.
        # Lowered to 4_700 so unknown portals still beat PMS_PRIOR (when a
        # known PMS template doesn't apply) and UNIVERSAL_PRIOR fallbacks,
        # but lose to a real on-page anchor with a floor-plan keyword. Known
        # portals (in _PORTAL_URL_PATTERNS) still score 10_000 via the
        # earlier passes — this change only affects the open-by-default
        # 6th-pass discovery for vendors we haven't catalogued.
        _UNKNOWN_PORTAL_SCORE = 4_700

        for match in _IFRAME_SRC_RE.finditer(html):
            if unknown_emit_count >= _UNKNOWN_PORTAL_CAP:
                break
            src = match.group(1).strip()
            if not src or not src.startswith("http") or src in seen:
                continue
            # Skip the SightMap SDK loader (already handled in the known scan).
            lower = src.lower()
            if lower.endswith("/embed/api.js") or "/embed/api.js?" in lower:
                continue
            host = _iframe_host(src)
            if not host or host in already_hosted:
                continue
            if _is_portal_infra_blacklisted(src):
                continue
            # Queue as unknown — same shape as known-portal hints, but the
            # portal_name marks it for telemetry / debugging.
            seen.add(src)
            already_hosted.add(host)
            hints.append((src, f"unknown:{host}"))
            unknown_emit_count += 1
    except Exception:
        pass

    return hints


# ---------------------------------------------------------------------------
# Inline-JS PMS init pattern library
# ---------------------------------------------------------------------------
# Each entry: (compiled regex with ONE capture group for the key, pms_name,
# url_synth_fn(key) -> str|None). Patterns deliberately match across
# whitespace/quote-style variants. Order matters only for tie-breaking;
# different patterns can match the same key on the same page and the
# synthesized URL dedups via the caller's ``seen`` set.

# RC-6 (2026-05-16): SightMap embed IDs are base-32-ish tokens — short
# mixed alphanumeric strings (e.g. ``n9w6mmzrv71``, ``m9pz438qvk1``,
# ``9zw4n95zv87``). Regex patterns matching ``[\w-]{6,}`` happily admit
# common English words like ``standard``, ``default``, ``api``, ``embed``
# because they have ≥6 letters — synthesising ``sightmap.com/embed/standard``
# (a 404) which then trips TIER_1_API_SIGHTMAP_SHAPE_REJECTED. 22 of 26
# SightMap_SHAPE_REJECTED failures in the 2026-05-16 cloud run.
# ``_is_plausible_sightmap_id`` enforces the canonical shape: at least
# one digit AND one letter, total 6-32 chars, no common English placeholder.
_SIGHTMAP_ID_BLACKLIST: frozenset[str] = frozenset({
    "standard", "default", "embed", "embedded", "preview", "demo",
    "example", "sample", "template", "config", "settings", "options",
    "loading", "error", "unknown", "placeholder", "widget", "iframe",
    "container", "wrapper", "anonymous", "undefined", "null",
})


def _is_plausible_sightmap_id(key: str) -> bool:
    """Filter SightMap-id captures to the canonical token shape.

    Real SightMap embed IDs are mixed alphanumeric (base-32-ish) tokens.
    Common false positives from regex captures: English placeholder
    words (``standard``, ``default``, ``api``). Reject those even when
    they pass the ``[\\w-]{6,}`` length bar.
    """
    if not key:
        return False
    if key.lower() in _SIGHTMAP_ID_BLACKLIST:
        return False
    if not (6 <= len(key) <= 32):
        return False
    has_letter = any(c.isalpha() for c in key)
    has_digit = any(c.isdigit() for c in key)
    # Real embed IDs always mix letters AND digits. ``configapi`` is alpha-
    # only, ``12345678`` is digit-only — both bogus.
    return has_letter and has_digit


def _synth_sightmap_url(key: str) -> str | None:
    """URL-synth helper for SightMap inline-JS init patterns.

    Returns ``None`` when the captured key isn't a plausible SightMap
    embed ID — the caller treats ``None`` as "don't queue this hop".
    """
    if not _is_plausible_sightmap_id(key):
        return None
    return f"https://sightmap.com/embed/{key}"

_INLINE_JS_INIT_PATTERNS: list[tuple[re.Pattern[str], str, "Callable[[str], str | None]"]] = [
    # SightMap (Engrain) — JS init: `SightMap.init({sightmap_id: "n9w6mmzrv71"})`
    # or `new SightMap({...})` or `Engrain.SightMap.create({"map_id": ...})`.
    # Also covers data-* placeholders: `<div data-sightmap-id="n9w6mmzrv71">`.
    # RC-6 (2026-05-16): all SightMap synthesizers funnel through
    # ``_synth_sightmap_url`` which rejects placeholder words (``standard``,
    # ``default``, ``api``) and alpha-only / digit-only captures via
    # ``_is_plausible_sightmap_id``. PIDs 26523 / 20959 / 22 other
    # SightMap_SHAPE_REJECTED failures synthesized non-existent embed
    # URLs like ``sightmap.com/embed/standard`` that 404'd.
    (
        re.compile(r'sightmap[_\-]?id["\']?\s*[:=]\s*["\']([\w-]{6,})["\']', re.IGNORECASE),
        "sightmap",
        _synth_sightmap_url,
    ),
    (
        re.compile(r'data-sightmap[_\-]?id\s*=\s*["\']([\w-]{6,})["\']', re.IGNORECASE),
        "sightmap",
        _synth_sightmap_url,
    ),
    # Bug #4 follow-up (2026-05-16): the Playwright forensic on PID 16139
    # chaseknollsapts.com found `sightmap.com/embed/api` in the rendered
    # HTML but neither `sightmap_id` nor `data-sightmap-id` matched. The
    # site uses an SDK init pattern that names the property key
    # differently (`embed_id` / `mapId` / `mapID`) — see playbook §15.
    # Three new patterns cover the most common alternative SDK shapes
    # observed across SightMap integrations:
    #   1. `embed_id` / `embedId` (engrain.com SDK newer versions)
    #   2. `mapId` / `map_id` (some white-label embeds)
    #   3. `<sightmap-app embed="...">` / `<sightmap-embed id="...">`
    #      custom elements (used by SDK v3+)
    # The 6+-char minimum guards against false positives on tiny IDs
    # (e.g. "1" or "abc") that aren't real SightMap embed tokens.
    (
        re.compile(
            r'(?:embed[_\-]?id|map[_\-]?id)["\']?\s*[:=]\s*["\']([\w-]{6,})["\']',
            re.IGNORECASE,
        ),
        "sightmap",
        _synth_sightmap_url,
    ),
    (
        re.compile(
            r'<sightmap[\w-]*[^>]*\b(?:embed|id|map[_\-]?id)\s*=\s*["\']([\w-]{6,})["\']',
            re.IGNORECASE,
        ),
        "sightmap",
        _synth_sightmap_url,
    ),
    # Engrain CDN SDK loader leaves a global config blob. Look for any
    # quoted string within ~200 chars of `engrain.com/embed` or
    # `SightMap` that matches the SightMap embed-ID alphabet+length.
    # This is intentionally a "best-effort last resort" — fires only
    # when the canonical `sightmap_id` / `data-sightmap-id` haven't.
    (
        re.compile(
            r'(?:SightMap|engrain\.com/embed)[\s\S]{0,200}?["\']([a-z0-9]{8,16})["\']',
            re.IGNORECASE,
        ),
        "sightmap",
        _synth_sightmap_url,
    ),
    # D14a (2026-05-16): G5 Marketing Cloud floor-plans-plus widget config.
    # PID 161 Hickory Mill (and the entire Morgan Properties portfolio):
    #   <script id="floor-plans-plus-config" type="application/json">
    #   {..., "sightmapID": "ryzvg6mywln", "inventoryHost": "...", ...}
    # The sightmapID is the Engrain embed ID that the runtime widget
    # injects as an iframe. Extracting it server-side and queueing the
    # canonical sightmap.com/embed/{id} URL routes the property through
    # the existing Sightmap adapter, which already knows how to parse
    # Engrain's API responses.
    (
        re.compile(
            r'["\']sightmapid["\']\s*[:=]\s*["\']([a-z0-9]{6,16})["\']',
            re.IGNORECASE,
        ),
        "sightmap",
        _synth_sightmap_url,
    ),
    # D19a (2026-05-16): SightMap clientKey + sightmapID → DIRECT API URL.
    # The embed URL `sightmap.com/embed/{id}` is an empty SPA shell; real
    # unit data arrives via a separate XHR to
    # `sightmap.com/app/api/v1/{clientKey}/sightmaps/{sightmap_id}`.
    # When both keys appear in the same JSON config block (G5 widget,
    # SightMap SDK init), synthesise the direct API URL so the existing
    # SightMap adapter (body-shape-aware at pms/adapters/sightmap.py:155-186)
    # gets fed the unit JSON without needing the SPA to boot.
    # Two-group pattern: group(1)=clientKey, group(2)=sightmapID.
    # The order can be either way in source — pattern matches both.
    (
        re.compile(
            r'["\']clientkey["\']\s*[:=]\s*["\']([\w\-]{6,})["\']'
            r'[\s\S]{0,400}?'
            r'["\']sightmapid["\']\s*[:=]\s*["\']([a-z0-9]{6,16})["\']',
            re.IGNORECASE,
        ),
        "sightmap",
        lambda client_key, sightmap_id: (
            f"https://sightmap.com/app/api/v1/{client_key}/sightmaps/{sightmap_id}"
        ),
    ),
    (
        # Same as above but with the keys in reverse order in source.
        re.compile(
            r'["\']sightmapid["\']\s*[:=]\s*["\']([a-z0-9]{6,16})["\']'
            r'[\s\S]{0,400}?'
            r'["\']clientkey["\']\s*[:=]\s*["\']([\w\-]{6,})["\']',
            re.IGNORECASE,
        ),
        "sightmap",
        lambda sightmap_id, client_key: (
            f"https://sightmap.com/app/api/v1/{client_key}/sightmaps/{sightmap_id}"
        ),
    ),
    # D17a (2026-05-16): hy.ly Marketing Cloud bootstrap.
    # Pattern: `my.hy.ly/mktg/fjs/{tenant}/0.js?pid={pid}`. The bootstrap
    # script returns an HTML shell; the actual unit data loads via runtime
    # XHRs to `my.hy.ly/api/properties/{pid}/...`. Synthesise the most
    # likely data-endpoint URLs as a guess — even if they 404, the
    # `embedded_portal.unknown_host_seen` telemetry shows what was
    # attempted, which is the path to learn the right endpoints. After
    # one Playwright forensic confirms the actual API shape, this list
    # can be tightened.
    # Property example: PID 1085 Sound at Peninsula
    # (livesoundatpeninsulajax.com); ZRS Management portfolio uses this.
    (
        re.compile(
            r'my\.hy\.ly/mktg/fjs/(\w+)/[\w.]+\?[^"\']*?pid=(\d{8,})',
            re.IGNORECASE,
        ),
        "hyly",
        lambda tenant, pid: [
            f"https://my.hy.ly/api/properties/{pid}/availabilities",
            f"https://my.hy.ly/api/properties/{pid}/floorplans",
            f"https://my.hy.ly/api/{tenant}/properties/{pid}",
        ],
    ),
    # AppFolio CMS slug — covers both data-attr and JS config patterns.
    # The slug-to-listings synthesis also happens via the dedicated
    # _synthesize_appfolio_listings_urls helper below, but this catches
    # cases where the AppFolio host doesn't appear in any href at all.
    (
        re.compile(r'data-appfolio[_\-]?(?:tenant|slug)\s*=\s*["\']([\w-]+)["\']', re.IGNORECASE),
        "appfolio",
        lambda k: f"https://{k}.appfolio.com/listings",
    ),
    # RealPage OneSite — JS embed or data attr declaring the client ID.
    # Synthesizes the canonical `{clientId}.onlineleasing.realpage.com` host.
    (
        re.compile(
            r'data-(?:realpage[_\-])?client[_\-]?id\s*=\s*["\'](\d{6,})["\']',
            re.IGNORECASE,
        ),
        "realpage_oll",
        lambda k: f"https://{k}.onlineleasing.realpage.com/",
    ),
    # Funnel / FortressTech — UUID-style portal IDs embedded in JS state.
    # The UUID is the property-specific portal address.
    (
        re.compile(r'fortresstech\.io/[\w/-]*?/([0-9a-f]{8}-[0-9a-f-]{20,})', re.IGNORECASE),
        "fortresstech",
        lambda k: f"https://portal.fortresstech.io/{k}",
    ),
    # Hyly — typical config: `hyly.config = { propertyId: "..." }`
    (
        re.compile(r'hy(?:ly)?[._]config[\s\S]{0,200}?propertyid["\']?\s*[:=]\s*["\']([\w-]+)["\']', re.IGNORECASE),
        "hyly",
        lambda k: f"https://my.hy.ly/property/{k}",
    ),
    # FunnelLeasing portal — `funnelleasing.com/embed/{guid}`
    (
        re.compile(r'funnelleasing\.com/embed/([\w-]{8,})', re.IGNORECASE),
        "funnel",
        lambda k: f"https://www.funnelleasing.com/embed/{k}",
    ),
]


def _scan_inline_js_pms_init(html: str) -> list[tuple[str, str]]:
    """Parse inline JavaScript and data-* attributes for PMS init patterns.

    Synthesizes the canonical iframe / data URL from the property-specific
    key(s) (sightmap_id, appfolio slug, realpage client ID, fortresstech UUID,
    hyly propertyId, funnel portal GUID, plus combined clientKey+resourceId
    shapes per D19a). Hits are returned as ``(synthesized_url, portal_name)``
    tuples.

    Pattern entries support two shapes:
      • Single-group (existing): `url_fn(key: str) -> str | None`
      • Multi-group (D19a, 2026-05-16): the regex has 2+ capture groups, and
        `url_fn(*groups) -> str | list[str] | None`. Used when a vendor's
        canonical API URL requires BOTH a tenant/clientKey AND a resourceId
        (SightMap `app/api/v1/{clientKey}/sightmaps/{id}`) or when one
        bootstrap pattern should synthesise MULTIPLE candidate API URLs
        (hy.ly: `/availabilities` AND `/floorplans` from the same `pid`).

    Pure function — never raises. Designed to be cheap (small set of regexes,
    no DOM parsing) so it can run on every entry-page HTML.
    """
    if not html or len(html) < 100:
        return []
    out: list[tuple[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()  # (pms_name, key) dedup within scan
    for pattern, pms_name, url_fn in _INLINE_JS_INIT_PATTERNS:
        try:
            for match in pattern.finditer(html):
                groups = tuple((g or "").strip() for g in match.groups())
                # Require at least one non-empty group.
                if not any(groups):
                    continue
                key_id = "|".join(groups)
                if (pms_name, key_id) in seen_keys:
                    continue
                seen_keys.add((pms_name, key_id))
                # Call url_fn with positional args matching the capture-group
                # count. Fall back to single-arg call (groups[0]) when the
                # url_fn signature only accepts one parameter — preserves
                # backward compatibility with existing 1-group entries that
                # use `lambda k: ...`.
                try:
                    url = url_fn(*groups)
                except TypeError:
                    url = url_fn(groups[0]) if groups else None
                except Exception:
                    url = None
                if not url:
                    continue
                # url may be a string OR a list of strings (D19a allows one
                # pattern to fan out into multiple candidate API URLs).
                if isinstance(url, str):
                    out.append((url, pms_name))
                elif isinstance(url, (list, tuple)):
                    for u in url:
                        if isinstance(u, str) and u:
                            out.append((u, pms_name))
        except Exception:
            continue
    return out


# RC5: maps FetchOutcome values to the verdict prefix written into errors[].
# Module-level so tests can import rather than redefine.
#
# Bug #2 fix (2026-05-16): added CANCELLED (from commit 1d068dd's
# per-property-timeout cancel path in fetch/fetcher.py) and the four other
# outcomes that previously fell through to the FAILED_UNREACHABLE default at
# scraper.py:~2994. Without these mappings, a property cancelled mid-fetch
# was indistinguishable from a DNS/TCP failure — blocking the analyzer from
# separating recoverable-by-retrying issues from permanent infrastructure
# breakage.
_OUTCOME_VERDICT_PREFIX: dict[str, str] = {
    "EMPTY_BODY":   "FAILED_FETCH_EMPTY",
    "DEAD_URL":     "FAILED_DEAD_URL",
    "CANCELLED":    "FAILED_TIMEOUT",
    "RATE_LIMITED": "FAILED_RATE_LIMITED",
    "HARD_FAIL":    "FAILED_HARD_FAIL",
    "PROXY_ERROR":  "FAILED_PROXY",
    "BOT_BLOCKED":  "FAILED_BOT_BLOCKED",
    "TRANSIENT":    "FAILED_TRANSIENT",
}


def _augment_ranked_with_hints(
    ranked: list[tuple[str, int, str]],
    hints: list[str],
    base_url: str,
) -> list[tuple[str, int, str]]:
    """Push LLM-provided navigation hints to the top of the ranked list.

    When an LLM call returned ``units: []`` but filled in
    ``profile_hints.navigation_hint`` (e.g. "/Marketing/FloorPlans"), we
    want link-hop to try that URL first. The hint can be a relative path
    or a full URL — we resolve against ``base_url`` either way and
    deduplicate.

    LLM hints get the highest sentinel score (``_LLM_HINT_SCORE``) and
    are returned at the head of the list. If the same URL was also
    keyword-ranked we drop the keyword duplicate so the LLM-anchored
    entry is the one that fires; its anchor prefix is what
    ``_try_link_hop`` keys off to refresh the monolithic LLM budget.
    """
    if not hints:
        return ranked
    from ma_poc.pms.signal_engine.filters import is_infra_url as _infra_check
    augmented: list[tuple[str, int, str]] = []
    hinted_urls: set[str] = set()
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
        if abs_url in hinted_urls or _infra_check(abs_url):
            continue
        hinted_urls.add(abs_url)
        augmented.append(
            (abs_url, _LLM_HINT_SCORE, f"{_LLM_HINT_ANCHOR_PREFIX}{raw_s[:60]}")
        )
    rest = [(u, s, a) for (u, s, a) in ranked if u not in hinted_urls]
    return augmented + rest


# Phase 2: _PMS_PRIOR_SCORE, _PMS_SUB_PATH_PRIORS, _UNIVERSAL_SUB_PATH_PRIORS
# are now imported from signal_engine.defaults above. Definitions removed.


def _pms_priors_for(
    detected: DetectedPMS | None,
    entry_url: str,
) -> list[tuple[str, int, str]]:
    """Generate template-prior candidates for ``_try_link_hop``.

    Returns ``(url, score, anchor)`` tuples — same shape as the keyword
    ranker and the LLM-hint augmenter — so the merge loop in
    ``_try_link_hop`` can consume them uniformly.

    Two paths:
      * **PMS-specific** — when ``detected.pms`` is recognised AND has an
        entry in ``_PMS_SUB_PATH_PRIORS``, use that PMS's preferred
        ordering. Anchor labelled ``pms_prior:<name>`` for telemetry.
      * **Universal fallback** — when ``detected`` is None, ``detected.pms``
        is ``"unknown"``, or the PMS lacks a registered entry, use
        ``_UNIVERSAL_SUB_PATH_PRIORS``. Anchor labelled
        ``pms_prior:universal``. This decouples recovery from
        fingerprint recognition — sites on unrecognised CMSes still
        get a fair shot at the canonical multifamily sub-paths.

    ``entry_url`` is the absolute homepage URL; priors are resolved against
    it via ``urljoin``. URLs identical to ``entry_url`` (e.g., when
    ``urljoin`` collapses an empty path) are filtered out.

    Pure function. Caller is responsible for dedup against visited /
    profile-top / keyword candidates.
    """
    pms = detected.pms if detected is not None else "unknown"
    template_paths = _PMS_SUB_PATH_PRIORS.get(pms)
    if template_paths:
        anchor = f"pms_prior:{pms}"
    else:
        # Universal fallback — fires for ``unknown`` AND for any future
        # PMS that hasn't been registered with a specific prior tuple.
        # See ``_UNIVERSAL_SUB_PATH_PRIORS`` for the rationale.
        template_paths = _UNIVERSAL_SUB_PATH_PRIORS
        anchor = "pms_prior:universal"

    out: list[tuple[str, int, str]] = []
    seen: set[str] = set()
    # Detect multi-property hosts: when entry_url has a non-trivial path
    # (>=2 chars after host, ignoring trailing slash), the property lives at a
    # sub-path and `/floorplans` resolved via urljoin would land on the host
    # root — wrong site for multi-property hosts (e.g. PID 46179
    # hayloftapartmenthomes.com/east-hampton-estates 2026-05-15). Also generate
    # priors RELATIVE to the entry path so we try /east-hampton-estates/floor-plans
    # as well as /floor-plans. See data/reports/cloud_run_2026-05-15/TRIAGE.md RC #12.
    try:
        _entry_parsed = urllib.parse.urlparse(entry_url)
        _entry_path = _entry_parsed.path.rstrip("/")
        _has_property_subpath = bool(_entry_path) and _entry_path not in ("", "/")
    except Exception:
        _entry_path = ""
        _has_property_subpath = False

    for path in template_paths:
        prior_url = urllib.parse.urljoin(entry_url, path)
        if prior_url and prior_url != entry_url and prior_url not in seen:
            out.append((prior_url, _PMS_PRIOR_SCORE, anchor))
            seen.add(prior_url)
        # Property-subpath variant for multi-property hosts. Only emit when
        # entry_url has a real sub-path AND the universal prior is absolute
        # (starts with /); skip already-rooted priors.
        if _has_property_subpath and path.startswith("/"):
            try:
                _subpath_prior = urllib.parse.urlunparse(
                    _entry_parsed._replace(path=_entry_path + path)
                )
                if _subpath_prior and _subpath_prior != entry_url and _subpath_prior not in seen:
                    # Slight boost so the sub-path variant outranks the host-root
                    # variant for multi-property hosts where the host root often 404s.
                    out.append((_subpath_prior, _PMS_PRIOR_SCORE + 10, f"{anchor}:prop_subpath"))
                    seen.add(_subpath_prior)
            except Exception:
                pass
    return out


# Phase 2: _LINK_ANCHOR_KEYWORDS, _LINK_PATH_KEYWORDS, _LINK_HOST_KEYWORDS
# are now imported from signal_engine.defaults above. Definitions removed.

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
    # Static asset extensions — JS/CSS/font/icon bundles can never carry unit data.
    # PID 1074 (carltonequities AppFolio) 2026-05-16: 19 hops, half of which went
    # to `/listings/assets/listings/appfolio_touch-*.js`, `ie-shims-*.js`,
    # `application2-*.js`, `place_holder-*.png`. Every JS/font asset costs a
    # fetch round-trip + a tier cascade against ~1KB of JS source.
    ".js",
    ".css",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".ico",
    ".map",   # source map files
    # WordPress CMS asset / REST paths — top hop destinations on PARTIAL pids
    # 2026-05-16 (resources.yardi.com): /wp-content 420 hits, /wp-json 267,
    # /wp-includes 73, /xmlrpc.php 24. These are theme assets / API endpoints
    # that NEVER carry apartment unit data.
    "/wp-content/",
    "/wp-json/",
    "/wp-includes/",
    "/wp-admin/",
    "/xmlrpc.php",
    # Yardi corporate marketing CDN — 489 hits to /legal alone on 2026-05-16.
    # `resources.yardi.com` is the Yardi marketing/legal content site, not a
    # property data host.
    "resources.yardi.com",
    "www.yardi.com",
    # Ad / tracker beacons that surface as anchors on some sites. These ALWAYS
    # 404 or return non-JSON tracker payloads.
    "online-metrix.net",
    "doubleclick.net/pagead",
    "googletagmanager.com",
    "google-analytics.com",
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
    # Auth-wall patterns — these URLs require credentials and will always return
    # a login page rather than unit data.  AppFolio /connect/users/sign_in/* is
    # the canonical offender: every floor-plan sub-path gets appended to the
    # sign-in URL, burning one LLM call per variant.  Filter them before ranking.
    "/sign_in",
    "/signin",
    "/login",
    "/log_in",   # AppFolio oportal/users/log_in
    "/log-in",
    "/auth",
    "/sso",
    # AppFolio per-listing rental-application form — `/listings/rental_applications/new?listable_uid=*`.
    # Pure application form, never the unit listing page. Each unique listable_uid
    # generates a NEW skip-list miss; we keep hopping until budget exhausts.
    # 14 of 181 PARTIAL pids on 2026-05-16 (PID 1074 hit this 4× in one run).
    "/rental_applications",
    # Entrata CMS module dead-ends — these are auth / detail shell paths that
    # the Entrata template renders on every property page (e.g. "Apply Now"
    # CTA → `/Apartments/module/application_authentication/`). They have an
    # "apply" anchor that scores 30 + "/apartments" path keyword 80 → the
    # _rank_internal_links floor at scraper.py:1767-1771 lifts them to 5600,
    # outranking real anchors. The endpoint returns a 307KB apply portal HTML
    # with zero unit data on every variant. Companion to the subpath-
    # composition guard at scraper.py:2397-2425 (which prevents derived
    # /floorplans suffixes but not the bare endpoint).
    # NOTE: /Apartments/module/widgets/ is INTENTIONALLY excluded — that IS
    # Entrata's data widget endpoint (entrata.py:10,56).
    # PIDs 37719 (1611onlakeunion), 16139 (chaseknollsapts) — 2026-05-15.
    "/module/application_authentication",
    "/module/application-authentication",
    "/module/unit-detail",
    "/module/unit_detail",
)


# ── D2 (2026-05-16): URL-shape scoring patterns ──────────────────────────────
# When ranking anchors, the URL STRUCTURE (path shape) is a more reliable
# signal than the anchor TEXT. A bare anchor "<a href='/floorplans/1-bed-1-bath'>
# View Details</a>" is high-value because the URL is a slugged-child path —
# the anchor text "View Details" appears on galleries / news / amenities too,
# so we never key off it directly. The url_shape_score below is composable
# with anchor_score / path_keyword_score (additive, not bucketed), and
# explicit anti-signals subtract.
#
# Higher score wins. Floors below are scored as deltas added on top of the
# base internal-link score (4_000 from DEFAULT_KIND_BASE_SCORES).
#
# Reference: C:/tmp/rp_sx_analysis_2026_05_14/DETAIL_PAGE_GAPS.md
_URL_SHAPE_PATTERNS: tuple[tuple[re.Pattern[str], int, str], ...] = (
    # NOTE: regexes are tried in order; first match wins. Order matters
    # AND so does score — the FIRST match's score is what gets added.
    #
    # Yield-per-hop ordering (D11, 2026-05-16):
    #   canonical_inventory (one fetch → ALL units)         HIGHEST
    #   rentcafe_per_plan   (one fetch → ~20-60 units)      mid
    #   slugged_plan_detail (one fetch → ~5-30 units)       mid
    #   per_unit_detail     (one fetch → 1 unit)            low
    #
    # Pre-D11 ordering put per_unit_detail above canonical_inventory,
    # which caused PID 2982 Cortland on Pike to consume 7 hops on 7
    # per-unit pricing pages (= 7 units) while missing the single
    # `/available-apartments/` page that holds all 79 data-unit-id cards.
    #
    # Patterns use `re.search` (no start anchor) because portfolio sites
    # nest paths like `/apartments/{property-slug}/available-apartments/`,
    # but end-anchor (`/?$`) keeps each pattern matching the URL terminus.

    # Canonical inventory page — THE JACKPOT URL. One fetch returns every
    # available unit's data-unit-id, rent, sqft, availability date in a
    # single HTML response. Cortland's `/available-apartments/` = 79 cards.
    # Pattern intentionally does NOT match `/available-apartments/{slug}/
    # pricing/` shape (per_unit_detail handles that) — only the bare
    # `/available-apartments/` terminus.
    (
        re.compile(r"/(available[-_]?apartments?|all[-_]?availability|availability[-_]?list)/?$", re.IGNORECASE),
        7_500,
        "canonical_inventory",
    ),
    # RentCafe per-plan availability URL (PID 5822 The Izzy).
    # Shape: /floorplans/{slug}-{id}/fp_name/occupancy_type/{type}/
    # One fetch = ~20-60 units (option-row table per per-plan page).
    (
        re.compile(r"/floor[-_]?plans?/[a-z0-9\-_]+/fp_name/occupancy_type/[a-z\-]+/?$", re.IGNORECASE),
        5_500,
        "rentcafe_per_plan",
    ),
    # Generic slugged plan-detail (PID 3483 Brook `/floorplans/1-bed-1-bath`,
    # PID 52331 alexandriacarmel `/floorplans/the-diplomat-1-br-1-ba`).
    # One fetch = ~5-30 units. Restricted to `/floor-plans|units|residences/
    # {slug}` — NOT `/apartments/{slug}` (ambiguous: portfolio sites use
    # `/apartments/{property-slug}/` for sibling-property links).
    #
    # 2026-05-17 — boosted 5_000 → 6_500 so per-plan detail pages outrank
    # bare floor-plan-index pages (1_500) and generic anchor-discovered
    # links (~5_100). The Diplomat URL on 52331 was at 5980 (slugged
    # detail 5000 + anchor 88 + path keyword); under the boost it lands
    # ~7500, ahead of every entry candidate except the SecureCafe portal
    # itself (10120). That brings the productive per-plan crawl forward
    # in the hop queue before the property hits the wallclock budget.
    (
        re.compile(r"/(?:floor[-_]?plans?|units?|residences?)/[a-z0-9][a-z0-9\-_]+/?$", re.IGNORECASE),
        6_500,
        "slugged_plan_detail",
    ),
    # Per-unit detail page (Cortland `/available-apartments/1n-131/pricing/`).
    # One fetch = ONE unit. Lowest-yield-per-hop tier among "real data"
    # patterns. Kept above bare_index because per-unit pages do contain
    # real unit data, but below canonical / per-plan since a typical hop
    # budget should be spent on multi-unit pages first.
    # D11 (2026-05-16): score 6_500 → 4_700 (below canonical 7_500 and
    # per-plan 5_000-5_500 but above bare_index 1_500).
    (
        re.compile(
            r"/(?:available[-_]?apartments?|floor[-_]?plans?|apartments?|units?)/"
            r"[a-z0-9][a-z0-9\-_]+/(pricing|details?|availability|info)/?$",
            re.IGNORECASE,
        ),
        4_700,
        "per_unit_detail",
    ),
    # Bare index page — discovery, not data. Should rank below detail pages
    # so the cascade prefers detail when both are present.
    # Use `^/{kw}/?$` (start-anchored) for the index — we ONLY want to match
    # the literal `/floorplans/` etc. at root, not `/apartments/{prop}/`.
    (
        re.compile(r"^/(?:floor[-_]?plans?|availability|leasing|find[-_]?(?:a[-_]?)?home|rent)/?$", re.IGNORECASE),
        1_500,
        "bare_index",
    ),
    # Marketing aggregate — lower than the real index, never the data page.
    (
        re.compile(r"/(?:featured[-_]?(?:floor[-_]?plans?|apartments?)|virtual[-_]?tour|models?)/?", re.IGNORECASE),
        500,
        "marketing_aggregate",
    ),
    # Anti-signals — explicit non-inventory paths. Negative score so the
    # additive sum can never elevate them above the base internal-link tier.
    # Use start-anchor `^/` since these are TOP-LEVEL paths like /tour/, /gallery/
    # — we don't want to penalize `/apartments/{slug}/photos/{n}` (which still
    # may be a per-unit page); only penalise when the URL itself IS the page.
    (
        re.compile(
            r"^/(?:schedule[-_]?(?:a[-_]?)?tour|tour|gallery|photos?|amenities?|"
            r"neighborhood|reviews?|about|contact|faq|policies?|residents?|"
            r"news|blog|events?|sitemap|careers?|privacy|terms|accessibility|"
            r"login|sign[-_]?(?:up|in)|register|application|account)/?",
            re.IGNORECASE,
        ),
        -2_000,
        "anti_signal_non_inventory",
    ),
)


def _url_shape_score(link_path: str) -> tuple[int, str]:
    """Return (score_delta, shape_label) for a resolved URL path.

    Reads only the URL path — never the anchor text. The shape labels are
    surfaced into the anchor field for telemetry so we can confirm a hop
    fired because the URL pattern matched the canonical_inventory class.
    """
    if not link_path:
        return 0, ""
    for pattern, delta, label in _URL_SHAPE_PATTERNS:
        if pattern.search(link_path):
            return delta, label
    return 0, ""


#: Path shape for a per-floorplan detail page. Matches
#: ``/floor-plans/{slug}`` / ``/floorplans/{slug}`` / ``/plans/{slug}`` /
#: ``/units/{slug}`` where the slug is at least 8 chars and contains at
#: least one alphanumeric edge character. The trailing slash is optional.
_PER_FLOORPLAN_DETAIL_PATH_RE = re.compile(
    r"/(?:floor[-_]?plans?|plans|units)/(?P<slug>[a-z0-9][a-z0-9\-_]{6,}[a-z0-9])(?:/|$)",
    re.IGNORECASE,
)

#: Bed/bath/studio token that must appear in the slug for a candidate to
#: count as a per-plan detail page. ``the-diplomat-1-br-1-ba`` matches;
#: ``community-gallery`` does not. Filters generic ``/units/{slug}`` and
#: ``/plans/{slug}`` shapes that aren't per-floorplan inventory pages.
_PER_FLOORPLAN_DETAIL_SLUG_TOKEN_RE = re.compile(
    r"(?:br|bed|bedroom|ba|bath|bathroom|studio)",
    re.IGNORECASE,
)

#: Minimum entry-ranker score for a candidate to qualify as a per-plan
#: detail page. Anchor-discovered links score ~4_000 base + keyword
#: bonuses; this floor lets through real anchors while filtering random
#: site-wide nav links (contact / privacy / careers) that score lower.
_PER_FLOORPLAN_MIN_QUEUE_SCORE = 4_000


def _discover_cross_host_per_plan_urls(
    queue: list[tuple[str, int, str]],
    visited: set[str],
    sub_url: str,
    existing_fp_hints: list[tuple[str, str]],
) -> list[str]:
    """Return per-floorplan detail URLs found in *queue* but not yet hopped.

    Companion to the floor-plan accumulation logic in :func:`_try_link_hop`.
    Called when a hop produces plan-shaped rows (one row per floor plan
    rather than per physical apartment) and the same-host ``fp_hints``
    discovery (HTML anchor scrape on the sub-page) came up empty.

    The entry-page candidate queue may already contain per-plan detail
    URLs (anchor-discovered, ranked at 5_000+) on a different host than
    the just-finished hop — they wouldn't be queued by the same-host
    accumulator. This function scans the remaining queue and returns
    any candidate matching :data:`_PER_FLOORPLAN_DETAIL_PATH_RE` plus
    :data:`_PER_FLOORPLAN_DETAIL_SLUG_TOKEN_RE` plus the queue-score
    floor.

    Real-world case (PID 52331 alexandriacarmel, 2026-05-17): SecureCafe
    portal at ``alexandriacarmel.securecafe.com`` returns 10 plan rows;
    the per-plan detail pages live on ``alexandriacarmel.com/floorplans/
    the-diplomat-1-br-1-ba`` (different host). Without this discovery
    the link-hop loop would return on the first hop with units (LEAF
    return) and never crawl them.

    Args:
        queue: The link-hop candidate queue, tuples of ``(url, score, anchor)``.
        visited: URLs already fetched in this property's hop walk.
        sub_url: The URL of the hop that just produced plan-shaped rows
            (excluded from matches so we don't re-queue it).
        existing_fp_hints: ``(url, kind)`` tuples already in fp_hints —
            used to dedup the result.

    Returns:
        A list of URLs from *queue* that look like per-floorplan detail
        pages and aren't yet in *visited* / *existing_fp_hints*. Empty
        when no candidate matches. Order preserved from the input queue.
    """
    matched: list[str] = []
    if not queue:
        return matched
    existing_hint_urls = {url for url, _ in existing_fp_hints}
    for candidate_url, candidate_score, _anchor in queue:
        if candidate_url in visited:
            continue
        if candidate_url == sub_url:
            continue
        if (candidate_score or 0) < _PER_FLOORPLAN_MIN_QUEUE_SCORE:
            continue
        try:
            path = urllib.parse.urlparse(candidate_url).path or ""
        except Exception:
            continue
        slug_match = _PER_FLOORPLAN_DETAIL_PATH_RE.search(path)
        if not slug_match:
            continue
        slug = slug_match.group("slug")
        if not _PER_FLOORPLAN_DETAIL_SLUG_TOKEN_RE.search(slug):
            continue
        if candidate_url in existing_hint_urls:
            continue
        matched.append(candidate_url)
        existing_hint_urls.add(candidate_url)
    return matched


def _rank_internal_links(
    page_html: str,
    base_url: str,
    limit: int = 5,
    landed_url: str | None = None,
) -> list[tuple[str, int, str]]:
    """Rank internal links on a page for likelihood of carrying unit data.

    Scores each link by anchor text, path keywords, and host (portal
    subdomains). Returns ``[(url, score, anchor_text), ...]`` sorted best
    first. Never raises — parser errors yield an empty list.

    When ``landed_url`` is provided (entry-page final URL after redirects,
    e.g. 1701arch.com → livethearch.com), its host is ALSO accepted as
    same-site so cross-domain anchors on the redirected page survive the
    same-site filter. Relative anchors (``href="floorplans/"``) are
    resolved against ``landed_url`` so they pick up the post-redirect
    host instead of the CSV's pre-redirect host.
    See data/canary/local_runs/fix-validation-2026-05-15/UNCHANGED_FAIL_ANALYSIS.md
    PIDs 53592 (livethearch.com) and 119144 (windsorcommunities.com).
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
    # Landed host = post-redirect host; used as a secondary same-site reference
    # AND as the URL-resolution base for relative anchors when the entry page
    # redirected to a different host.
    landed_host = ""
    resolve_base = base_url
    if landed_url:
        try:
            _landed_parsed = urllib.parse.urlparse(landed_url)
            landed_host = (_landed_parsed.hostname or "").lower()
            if landed_host and landed_host != base_host:
                resolve_base = landed_url
        except Exception:
            pass

    candidates: dict[str, tuple[int, str]] = {}

    # D2 (2026-05-16): compute fp_signal context boost from the source page.
    # When the page we're discovering links FROM has ≥3 floor-plan signals,
    # its same-host anchors are more likely to also have data — the page
    # author is in the inventory-display context. +800 helps real per-plan
    # / per-unit anchors outrank infra-iframe noise scored at 10_000.
    _ctx_boost = 0
    try:
        from ma_poc.pms.signal_engine.floor_plan_signals import (
            count_floor_plan_signals,
        )
        _src_fp_count = count_floor_plan_signals(soup.get_text(" ", strip=True)[:200_000])
        if _src_fp_count >= 3:
            _ctx_boost = 800
    except Exception:
        pass

    # Build a unified iterable of (href_value, anchor_text) from both
    # <a href> links and <form action> attributes. Form actions are scored
    # the same way as links — some Entrata / Yardi custom-domain sites
    # put the availability URL in a search form action rather than a link.
    def _href_anchor_pairs() -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for a in soup.find_all("a", href=True):
            raw = a.get("href") or ""
            href = (raw if isinstance(raw, str) else " ".join(raw)).strip()
            pairs.append((href, (a.get_text(" ", strip=True) or "").lower()[:120]))
        for form in soup.find_all("form", action=True):
            raw = form.get("action") or ""
            action = (raw if isinstance(raw, str) else " ".join(raw)).strip()
            if action:
                # Use the form's submit-button text or a generic anchor label
                btn = form.find(attrs={"type": "submit"})
                label = (btn.get_text(" ", strip=True) if btn else "").lower()[:120] or "form"
                pairs.append((action, label))
        return pairs

    for href, anchor in _href_anchor_pairs():
        if not href:
            continue
        lower = href.lower()
        if any(skip in lower for skip in _LINK_SKIP_PATTERNS):
            continue

        # Resolve relative → absolute. Use landed_url as the base when the
        # entry page redirected to a different host so relative anchors
        # like href="floorplans/" land on the post-redirect host
        # (e.g. windsorcommunities.com/properties/windsor-burnet/floorplans/)
        # rather than the CSV pre-redirect host.
        try:
            resolved = urllib.parse.urljoin(resolve_base, href)
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

        # D2 additive composition:
        #   anchor_text_score: existing keyword bag (max ~+500)
        #   path_keyword_score: existing keyword bag (max ~+200)
        #   host_score: existing host keyword bag (max +120, but +2_000 for
        #     known leasing-portal hosts)
        #   url_shape_score: NEW — structural URL pattern recognition
        #     (-2_000 anti-signals → +4_500 per-unit detail)
        #   ctx_boost: NEW — +800 when source page has ≥3 fp_signals
        anchor_score = 0
        for kw, weight in _LINK_ANCHOR_KEYWORDS:
            if kw in anchor:
                anchor_score += weight
        path_score = 0
        for kw, weight in _LINK_PATH_KEYWORDS:
            if kw in link_path:
                path_score += weight
        host_score = 0
        for suffix, weight in _LINK_HOST_KEYWORDS:
            if link_host.endswith(suffix):
                host_score += weight
        url_shape, shape_label = _url_shape_score(link_path)
        score = anchor_score + path_score + host_score + url_shape + _ctx_boost

        # Stay on-site or go to a known portal subdomain. "Same-site" matches
        # the CSV base host OR the post-redirect landed host — the latter
        # admits cross-domain anchors on redirected entry pages (e.g.
        # 1701arch.com landed on livethearch.com, anchors point at
        # livethearch.com/.../conventional/).
        def _matches_host(target_host: str) -> bool:
            if not target_host:
                return False
            return (
                link_host == target_host
                or link_host.endswith("." + target_host)
                or target_host.endswith("." + link_host)
            )

        is_same_site = _matches_host(base_host) or _matches_host(landed_host)
        is_portal = any(link_host.endswith(suf) for suf, _ in _LINK_HOST_KEYWORDS)
        if not (is_same_site or is_portal):
            continue

        # Skip the base URL itself
        if resolved.rstrip("/") == base_url.rstrip("/"):
            continue
        if score <= 0:
            continue

        # D2 (2026-05-16): replace bucketed floor-lift with additive
        # composition. The pre-D2 design FLATTENED any link with
        # anchor+path keywords to a uniform PMS_PRIOR_SCORE+600 = 5_600,
        # making `/floorplans/` (index) score-equal to `/floorplans/{slug}`
        # (per-plan detail) and `/available-apartments/` (canonical
        # inventory). With url_shape_score now graded, that ceiling would
        # cap the genuinely-best URLs. We keep ONE narrow safety lift:
        # when a same-site anchor has BOTH anchor and path keywords AND
        # url_shape didn't already award it ≥3_000, lift to PMS_PRIOR+100
        # so it still beats `_UNIVERSAL_PRIOR=4_500`. Detail-class shapes
        # (≥3_000 url_shape) are already on their own additive trajectory
        # (typical: 4000 base + 3000 shape + 800 ctx = 7_800).
        if anchor_score > 0 and path_score > 0 and url_shape < 3_000:
            score = max(score, _PMS_PRIOR_SCORE + 100)
        elif anchor_score > 50 and url_shape < 3_000:
            score = max(score, _PMS_PRIOR_SCORE + 100)

        # Annotate anchor with shape label so telemetry shows WHY the link
        # ranked where it did — invaluable when debugging "why did we hop
        # to the index instead of the per-plan page?" questions.
        labelled_anchor = (
            f"{anchor[:40]} [{shape_label}]" if shape_label else anchor[:60]
        )

        # Keep best score per URL
        existing = candidates.get(resolved)
        if existing is None or score > existing[0]:
            candidates[resolved] = (score, labelled_anchor)

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
    embedded_portal_hints: list[tuple[str, str]] | None = None,
    visited_urls: set[str] | None = None,
    shared_budget: dict | None = None,
    browser_page: Any | None = None,
    landed_url: str | None = None,
) -> dict[str, Any] | None:
    """One-level BFS over home-page links when primary extraction is empty.

    Fetches up to ``max_hops`` candidate URLs via the L1 fetcher, re-runs
    ``scrape()`` on each, and returns the first sub-result that yields
    units. Returns ``None`` if no hop recovered data.

    ``llm_navigation_hints`` (Phase 5) takes priority over keyword-ranked
    candidates — if the LLM already diagnosed where data lives, we try
    that URL first instead of guessing from anchor text.

    ``embedded_portal_hints`` carries ``(url, portal_name)`` tuples
    surfaced by the generic adapter's embedded-JSON tier when it found
    a third-party leasing-portal pointer (SightMap, RealPage OLL,
    RentCafe, etc.) inside an SSR config blob. These are top-priority
    candidates because the host fingerprint will route them to a
    PMS-specific adapter that knows how to extract from them.

    Phase 9 — H5 invariant: ``visited_urls`` blocks fetch cycles. The
    entry URL is auto-added to prevent re-fetching the home page.
    ``max_hops`` caps the bounded BFS at 3 by default. Portal hints
    discovered during sub-fetches add up to ``max_hops`` extra slots
    to the queue (capped so a malicious page can't blow the budget).
    """
    visited: set[str] = set(visited_urls) if visited_urls else set()
    visited.add(entry_url)
    # Add landed_url to visited so we don't re-fetch the post-redirect
    # entry. _rank_internal_links's own same-URL skip already handles this
    # for the base case, but explicit add covers any path variations.
    if landed_url and landed_url != entry_url:
        visited.add(landed_url)

    ranked = _rank_internal_links(
        entry_page_html, entry_url, limit=max_hops, landed_url=landed_url
    )
    if llm_navigation_hints:
        ranked = _augment_ranked_with_hints(ranked, llm_navigation_hints, entry_url)
        # Cap to keep budget bounded even with hints merged in.
        ranked = ranked[: max(max_hops, len(llm_navigation_hints) + 1)]

    # Profile-driven navigation memory (Bug 3.1). The profile records
    # ``winning_page_url`` (yesterday's URL that produced units) and
    # ``availability_links`` (every sub-URL that previously yielded
    # data). We inject those as the highest-priority candidates so a
    # property that succeeded via ``/floor-plans`` last run skips the
    # anchor-text re-discovery step entirely. ``explored_links`` is the
    # complementary skip-list — sub-URLs that returned empty in past
    # runs are filtered out so we don't re-pay for known dead ends.
    profile_top: list[tuple[str, int, str]] = []
    explored_skip: set[str] = set()
    if profile is not None:
        from ma_poc.pms.signal_engine.filters import is_infra_url as _infra_check
        try:
            nav = profile.navigation
            wpu = getattr(nav, "winning_page_url", None)
            # Self-fetch suppression: if winning_page_url equals (or is path-
            # equivalent to) the entry URL, do NOT inject it as a hop
            # candidate. Otherwise the homepage prior consumes the top hop
            # slot, ranks above every real anchor (LLM_HINT_SCORE+1 = 10001),
            # and the runner re-extracts the same body it just fetched —
            # PID 246152 oxfordlakeview observed 2026-05-14: single candidate
            # was the homepage itself, the legit `/Marketing/FloorPlans`
            # anchor was squeezed out. _normalize_url collapses trailing
            # slashes / lowercase host so the comparison handles common
            # variants.
            def _same_as_entry(u: str) -> bool:
                try:
                    return _normalize_url(u) == _normalize_url(entry_url)
                except Exception:
                    return u == entry_url
            # Guard: never inject infra/media API endpoints as hop candidates.
            if (
                isinstance(wpu, str)
                and wpu
                and wpu not in visited
                and not _infra_check(wpu)
                and not _same_as_entry(wpu)
            ):
                profile_top.append((wpu, _LLM_HINT_SCORE + 1, "profile:winning_page_url"))
            for link in getattr(nav, "availability_links", []) or []:
                if (
                    isinstance(link, str)
                    and link
                    and link not in visited
                    and not _infra_check(link)
                    and not _same_as_entry(link)
                ):
                    profile_top.append((link, _LLM_HINT_SCORE, "profile:availability_link"))
            for dead in getattr(nav, "explored_links", []) or []:
                if isinstance(dead, str) and dead:
                    explored_skip.add(dead)
            # Dead-link TTL blocklist: skip URLs confirmed dead within
            # the last 14 days so stale universal-prior 404s don't burn
            # hop budget on repeated runs.
            _dead_links: dict[str, str] = getattr(nav, "dead_links", {}) or {}
            _dead_ttl_days = 14
            import datetime as _dt
            _now = _dt.datetime.now(_dt.timezone.utc)
            for dead_url, dead_ts in list(_dead_links.items()):
                try:
                    age_days = (_now - _dt.datetime.fromisoformat(dead_ts)).days
                    if age_days < _dead_ttl_days:
                        explored_skip.add(dead_url)
                except Exception:
                    pass
            # Priority pages were explored in past runs and land in
            # explored_links, but they must NEVER be skipped — they are
            # the highest-confidence candidates.  Strip them back out.
            _priority_urls: set[str] = set()
            if isinstance(wpu, str) and wpu:
                _priority_urls.add(wpu)
            for link in getattr(nav, "availability_links", []) or []:
                if isinstance(link, str) and link:
                    _priority_urls.add(link)
            explored_skip -= _priority_urls
        except Exception:
            # Profile access is best-effort — never let a malformed
            # profile sink the link-hop entirely.
            pass

    # Bug B (P4): PMS fingerprint priors. Template-derived sub-paths for
    # the detected PMS. Slot between profile-top (highest) and keyword-
    # ranked (lowest) in score. When neither profile nor keyword ranker
    # produces candidates (SPA marketing shell with detected PMS — the
    # 2026-05-11 Bug B shape), this guarantees at least one well-typed
    # candidate to try. See docs/2026_05_11_regressions_fix_design.md.
    # Use landed_url (post-redirect host) as the prior-synthesis base when
    # the entry redirected to a different host. For 1701arch.com →
    # livethearch.com / windsorburnet.com → windsorcommunities.com, priors
    # synthesised against entry_url would land on the wrong host.
    _prior_base = landed_url if (landed_url and landed_url != entry_url) else entry_url
    pms_priors = _pms_priors_for(detected, _prior_base)

    # Leasing-portal pointers from embedded JSON (Jonah Digital widget
    # config etc. point at SightMap / RealPage OLL). High-confidence —
    # an extractor told us this is where units live — so score above
    # PMS template priors.
    portal_candidates: list[tuple[str, int, str]] = []
    if embedded_portal_hints:
        from ma_poc.pms.signal_engine.filters import is_infra_url as _infra_check_portal
        for hint in embedded_portal_hints:
            try:
                url_s, portal_name = hint
            except Exception:
                continue
            url_s = str(url_s or "").strip()
            if not url_s or url_s in visited or _infra_check_portal(url_s):
                continue
            portal_candidates.append(
                (url_s, _EMBEDDED_PORTAL_SCORE, f"{_EMBEDDED_PORTAL_ANCHOR_PREFIX}{portal_name}")
            )

    # Merge all candidate sources and rank by SCORE, not source-list order.
    # Sources contribute candidates at their own confidence levels:
    #   profile.winning_page_url     → 10_001 (highest — yesterday's win)
    #   profile.availability_links   → 10_000
    #   LLM navigation_hint          → 10_000 (Phase 5 cross-tier signal)
    #   embedded leasing-portal hint → 10_000 (embedded-JSON cross-tier signal)
    #   PMS template priors (specific or universal) → 5_000
    #   keyword anchor ranker        → 0-200
    # Score-based sort means a future source addition lands cleanly by
    # picking its confidence level without touching this merge.
    # Stable sort preserves source order within the same score (e.g. when
    # two LLM hints both score 10_000, they keep their emission order).
    # Dedup is first-seen-after-sort, so highest-scored entry for any
    # given URL keeps the slot.
    if profile_top or pms_priors or portal_candidates:
        combined: list[tuple[str, int, str]] = (
            list(profile_top)
            + list(portal_candidates)
            + list(pms_priors)
            + list(ranked)
        )
        combined.sort(key=lambda triple: triple[1], reverse=True)
        seen_urls: set[str] = set()
        merged: list[tuple[str, int, str]] = []
        for u, s, a in combined:
            if u not in seen_urls:
                seen_urls.add(u)
                merged.append((u, s, a))
        ranked = merged

    # Phase 2: delegate final ordering to SourceRanker.
    # Convert (url, score, anchor) tuples to SourceSignals keyed by anchor
    # prefix, run through SourceRanker, convert back. The ranker uses the
    # same constants (from signal_engine.defaults) so scores are identical;
    # this makes SourceRanker the canonical ordering authority.
    try:
        from ma_poc.pms.signal_engine.defaults import create_default_ranker as _mk_ranker
        from ma_poc.pms.signal_engine.models import SourceKind as _SK, SourceSignal as _SS

        def _anchor_to_kind(anchor: str) -> _SK:
            a = anchor.lower()
            if a.startswith("llm-hint:"):
                return _SK.LLM_HINT
            if a.startswith("profile:winning"):
                return _SK.PROFILE_WINNING
            if a.startswith("profile:"):
                return _SK.PROFILE_NAV_HINT
            if a.startswith("embedded-portal:"):
                return _SK.EXTERNAL_PORTAL
            if a.startswith("pms_prior:"):
                return _SK.PMS_PRIOR
            return _SK.INTERNAL_LINK

        _ranker = _mk_ranker()
        _signals = []
        for u, s, a in ranked:
            _kind = _anchor_to_kind(a)
            # Signals whose score was set by a trusted source (profile layer,
            # LLM hint, or an anchor link elevated above PMS_PRIOR_SCORE by
            # keyword matching in _rank_internal_links) must have their score
            # preserved. If we hand them to the SourceRanker without an
            # override, the ranker would re-score them from scratch — for
            # INTERNAL_LINK that means base=4_000 + keyword bonus, which is
            # below PMS_PRIOR (5_000) and causes real page anchors to lose
            # to guessed template priors.
            _is_profile_scored = (
                _kind == _SK.PROFILE_WINNING
                or a.lower().startswith("profile:availability_link")
                # Anchor links that _rank_internal_links boosted above the
                # PMS_PRIOR threshold carry a meaningful score: preserve it
                # so they outrank template priors in the final sort.
                or (_kind == _SK.INTERNAL_LINK and s > _PMS_PRIOR_SCORE)
            )
            _signals.append(
                _SS(
                    kind=_kind,
                    url=u,
                    anchor_text=a,
                    profile_score_override=(s if _is_profile_scored else None),
                )
            )
        _ranked_signals = _ranker.rank(_signals)
        # Reconstruct (url, score, anchor) format for the rest of _try_link_hop
        ranked = [
            (rs.signal.url or "", rs.composite_score, rs.signal.anchor_text or "")
            for rs in _ranked_signals
        ]
    except Exception:
        pass  # Degrade gracefully — existing score-based sort already in ranked

    # Phase 9: drop URLs already visited (cycle break) — and skip the
    # profile's recorded dead ends so we don't re-pay for them.
    # Carve-out: high-score sources (PMS priors, embedded portal hints,
    # LLM nav-hints, anchor links elevated above PMS_PRIOR_SCORE) survive
    # the explored_skip filter. Otherwise a single empty-extraction run
    # poisons the candidate forever (Profile Bug 1 extension — wpu and
    # availability_links are already carved out earlier).
    ranked = [
        (u, s, a) for (u, s, a) in ranked
        if u not in visited and (s >= _PMS_PRIOR_SCORE or u not in explored_skip)
    ]
    # Phase 9: hard-cap at max_hops (defensive — _rank_internal_links has
    # its own limit, but enforcing here protects against augment-with-hints
    # bypassing the limit). Bumped slightly when profile candidates are
    # present so winning_page_url + availability_links don't squeeze out
    # the keyword-ranked fallbacks entirely.
    cap = max_hops + (1 if profile_top else 0)
    ranked = ranked[:cap]
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

    # Iterate as a queue (not a fixed for-loop) so that hints discovered
    # mid-iteration — leasing-portal pointers AND floor-plan sub-page
    # links — can be fetched in the same pass.
    queue: list[tuple[str, int, str]] = list(ranked)
    queue_idx = 0
    dynamic_appended = 0
    max_dynamic_appends = max_hops

    # When the first success is a floor-plan INDEX page (detects sub-pages),
    # we accumulate units across all sub-pages rather than returning early.
    # ``_first_successful_result`` holds the base result; ``_accumulated_units``
    # merges all unit lists.
    _first_successful_result: dict[str, Any] | None = None
    _accumulated_units: list[dict[str, Any]] = []
    _in_floorplan_accumulation = False

    # Rule: once profile:winning_page_url delivers >1 units, skip lower-scored
    # candidates (priors, generic links) — they are speculative and waste hops.
    # Profile-level and LLM-hint-level signals (score >= _LLM_HINT_SCORE) are
    # still followed.  Floor-plan sub-page accumulation is unaffected.
    _winning_page_satisfied = False

    # Track which page produced the most units so the caller can promote it
    # to winning_page_url if it outperforms the current recorded winner.
    _best_units_page: tuple[str, int] = ("", 0)  # (url, unit_count)

    # LLM CSS selectors cached from the first sub-page in accumulation mode.
    # Passed via shared_budget so subsequent sub-pages skip the LLM DOM call.
    _fp_llm_selectors: dict[str, Any] | None = None

    # Body-hash dedup: prevents LLM analysis on pages whose visible text is
    # identical to one already analysed (JS-silent-redirect cycles, AppFolio
    # /sign_in/* auth walls, SPA tab-switcher shells). Hashes stripped text
    # (script/style/tags removed, whitespace collapsed) rather than raw bytes
    # so inline-state churn (timestamps, CSRF, Angular chunk URLs) doesn't
    # mask a real duplicate. Seeded with the entry page so redirect-to-entry
    # cases are caught immediately.
    _seen_body_hashes: set[str] = {
        hashlib.sha256(
            _strip_html_for_dedup(entry_page_html).encode("utf-8")
        ).hexdigest()[:16]
    }

    # Count of pages processed while in floor-plan accumulation mode.
    # Capped at _MAX_FLOORPLAN_ACCUM_PAGES to prevent timeout spirals on
    # sites with many individual /floorplans/<name> sub-pages.
    _accum_pages_fetched = 0

    # Shard_84 defensive backstop (2026-05-16): cumulative hop-fetch time
    # budget. Even with hop COUNT capped, a single slow hop can park
    # for 35s × multiple retries × 7 hops = enough to exhaust the
    # per-property 600s budget. Cap the SUM of hop-fetch elapsed times
    # at 240s — generous enough for legitimate slow sites (Cloud Run
    # observed p95 hop time ~6s, p99 ~25s) but tight enough that one
    # property can't burn 80% of its budget in hops alone.
    _HOP_CUMULATIVE_BUDGET_MS = 240_000
    _hop_elapsed_total_ms = 0

    while queue_idx < len(queue):
        sub_url, score, anchor = queue[queue_idx]
        idx = queue_idx + 1
        queue_idx += 1

        # Shard_84 fix: hard-stop when cumulative hop time exceeds budget.
        # The check runs BEFORE fetching the next sub_url so we don't
        # spend an additional 25s on a hop we already can't afford.
        if _hop_elapsed_total_ms >= _HOP_CUMULATIVE_BUDGET_MS:
            log.info(
                "link-hop cumulative budget exhausted for %s: %dms used, "
                "%d hops remaining — stopping hop cascade",
                property_id, _hop_elapsed_total_ms, len(queue) - queue_idx,
            )
            try:
                emit(
                    EventKind.LINK_HOP_FETCHED,
                    property_id,
                    url=sub_url,
                    error="cumulative_hop_budget_exhausted",
                    hop_index=idx,
                    elapsed_total_ms=_hop_elapsed_total_ms,
                )
            except Exception:
                pass
            break

        # Skip lower-scored priors once the profile winning page delivered data.
        if _winning_page_satisfied and score < _LLM_HINT_SCORE and not _in_floorplan_accumulation:
            continue
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
            reuse_page=browser_page,  # Pattern A: share entry-page session
        )
        try:
            sub_fetch = await jugnu_fetch(sub_task)
        except Exception as exc:
            emit(EventKind.LINK_HOP_FETCHED, property_id, url=sub_url, error=str(exc)[:200], hop_index=idx)
            continue

        outcome_val = (
            sub_fetch.outcome.value if hasattr(sub_fetch.outcome, "value") else str(sub_fetch.outcome)
        )

        # Retry TRANSIENT failures once for property-specific URLs (3+ path
        # segments).  Entrata /conventional/ and ProspectPortal /brookhaven/
        # /royale/conventional/ pages occasionally time out on the first attempt
        # when the browser pool is under memory pressure; a single retry with the
        # same budget_ms succeeds most of the time.  Generic top-level priors
        # (/availability, /floorplans) are NOT retried — TRANSIENT there usually
        # means a bot-block or dead link, not a load-timing issue.
        if outcome_val == "TRANSIENT" and not (sub_fetch.body):
            try:
                _path_parts = [
                    p for p in urllib.parse.urlparse(sub_url).path.split("/") if p
                ]
            except Exception:
                _path_parts = []
            if len(_path_parts) >= 3:
                log.debug(
                    "link-hop %s: TRANSIENT on property-specific URL — retrying once",
                    sub_url,
                )
                try:
                    sub_fetch = await jugnu_fetch(sub_task)
                    outcome_val = (
                        sub_fetch.outcome.value
                        if hasattr(sub_fetch.outcome, "value")
                        else str(sub_fetch.outcome)
                    )
                except Exception:
                    pass  # keep original TRANSIENT outcome

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

        # Shard_84 fix (2026-05-16): accumulate hop-fetch elapsed_ms into
        # the cumulative budget tracker. Counted regardless of outcome —
        # a TRANSIENT/timeout hop also spent the time. Retries (in the
        # block above) ALSO add their elapsed_ms via the second
        # sub_fetch assignment, so retried hops cost their full clock
        # time toward the budget.
        try:
            _hop_elapsed_total_ms += int(sub_fetch.elapsed_ms or 0)
        except Exception:
            pass

        # B2: block the resolved final URL so redirect-identical pages are never
        # processed twice in the same session (e.g. two different requested paths
        # that both redirect to /floorplans would otherwise burn extraction twice).
        # If the final URL was ALREADY visited (redirect back to a page we already
        # have results for), skip extraction entirely — the body-hash dedup will
        # also catch JS-silent-redirects, but this short-circuits before the hash.
        _sub_final = getattr(sub_fetch, "final_url", None) or ""
        if _sub_final and _sub_final != sub_url:
            if _sub_final in visited:
                log.debug(
                    "link-hop %s: server redirect to already-visited %s — skipping",
                    sub_url, _sub_final,
                )
                # B3 writer fix: cycle, not a dead URL. Don't poison
                # explored_links — the redirect target may be valid next
                # run from a different entry path.
                continue
            visited.add(_sub_final)

        if outcome_val != "OK":
            explored[sub_url] = False
            # Record this URL as blocked/failed in the session so adapter-level
            # hint synthesisers (securecafe_portal_detected, etc.) don't re-queue
            # it on subsequent hop pages.
            if shared_budget is not None:
                _blocked_set: set[str] = shared_budget.setdefault("_session_blocked_urls", set())
                _blocked_set.add(sub_url)
            # Persist confirmed-dead URLs (DEAD_URL / HARD_FAIL with tiny body)
            # to the profile blocklist so future runs skip them within the TTL.
            _body_bytes = len(sub_fetch.body) if sub_fetch.body else 0
            if outcome_val in ("DEAD_URL", "HARD_FAIL") and _body_bytes < 2048 and profile is not None:
                import datetime as _dt
                _dead_ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
                _nav = getattr(profile, "navigation", None)
                _dl = getattr(_nav, "dead_links", None) if _nav is not None else None
                if isinstance(_dl, dict):
                    _dl[sub_url] = _dead_ts
            # Signal back to profile_updater that profile:winning_page_url
            # hard-failed so it can be cleared and the profile reset to COLD.
            if anchor == "profile:winning_page_url" and shared_budget is not None:
                shared_budget["_winning_page_url_hop_outcome"] = "profile:winning_page_url:failed"
            continue

        # Body-hash dedup: if this hop's visible text is identical to a page
        # we already analysed (AppFolio /sign_in/* auth wall, JS-silent-redirect
        # to homepage, SPA tab-switcher URLs that return the same rendered shell
        # with different inline state like timestamps/CSRF/Angular chunk URLs),
        # skip extraction entirely. The entry page is pre-seeded into
        # _seen_body_hashes so redirect-to-entry cases are caught on the first
        # hop without a separate URL check.
        #
        # We hash STRIPPED TEXT (script/style removed, whitespace collapsed),
        # not raw bytes. Raw-byte hashing missed SPA shell loops (PID 229594
        # venterraliving) where bodies differed by 26KB of inline state but
        # the rendered text was byte-identical.
        _hop_body_bytes = sub_fetch.body or b""
        _hop_body_text = _hop_body_bytes.decode("utf-8", errors="replace")
        _hop_stripped = _strip_html_for_dedup(_hop_body_text)
        _hop_body_hash = hashlib.sha256(_hop_stripped.encode("utf-8")).hexdigest()[:16]
        if _hop_body_hash in _seen_body_hashes:
            log.debug(
                "link-hop %s: duplicate visible text (hash=%s) — auth wall or content loop, skipping",
                sub_url, _hop_body_hash,
            )
            # B3 writer fix: content loop within a single session, not a
            # dead URL. The same URL fetched on a different day from a
            # different entry path may yield different content (auth
            # cookies, A/B variants).
            continue
        _seen_body_hashes.add(_hop_body_hash)

        # Iframe portal scan: scan EVERY hop body for portal iframes
        # (sightmap.com, securecafe.com, AppFolio widget, etc.) regardless of
        # whether the outer HTML has floor-plan signals. A page can legitimately
        # have BOTH outer-HTML signals AND a portal iframe carrying the actual
        # unit data — PID 229594 venterraliving is the canonical example
        # (sightmap.com/embed/n9w6mmzrv71 iframe + 4 outer FP signals).
        # Queue the iframes as high-priority portal hops; do NOT skip the
        # outer-page extraction (it may also yield units).
        try:
            from ma_poc.pms.signal_engine.floor_plan_signals import (
                has_floor_plan_signals as _has_fp,
                SIGNAL_THRESHOLD_ANY as _FP_ANY,
            )
            _outer_has_fp = _has_fp(_hop_body_text, _FP_ANY)
        except Exception:
            _outer_has_fp = True  # safe default

        _iframe_portal_hints = _extract_portal_iframe_hints(_hop_body_text)
        if _iframe_portal_hints:
            log.debug(
                "link-hop %s: found %d portal iframe(s) — queueing as hops "
                "(outer_has_fp=%s)",
                sub_url, len(_iframe_portal_hints), _outer_has_fp,
            )
            for _ih_url, _ih_name in _iframe_portal_hints:
                if _ih_url not in visited and not any(u == _ih_url for u, _, _ in queue):
                    queue.append((
                        _ih_url,
                        _EMBEDDED_PORTAL_SCORE,
                        f"{_EMBEDDED_PORTAL_ANCHOR_PREFIX}iframe:{_ih_name}",
                    ))
                    dynamic_appended += 1
            # If outer HTML has no FP signals at all, the page is just a
            # marketing/portal shell — skip its extraction entirely (LLM
            # would burn budget on nothing). When outer HAS signals, fall
            # through to extraction normally; the iframe candidate is in
            # the queue for later.
            if not _outer_has_fp:
                # B3 writer fix: this URL is a routing hop (portal iframe
                # found, we're going inside it instead). The outer URL
                # isn't dead — it's just not where the units live. Don't
                # poison explored_links.
                continue

        # Bug 5 alignment (2026-05-09 deep-dive): if this hop's body looks
        # rich (≥50KB AND JSON-LD FloorPlan/Apartment OR ≥5 rent tokens),
        # raise the cost cap on the shared budget so the sub-page's LLM
        # rescue can fire even if the entry page already burned the cap.
        # Gated by the richness predicate so login walls / redirects /
        # CF interstitials don't silently buy themselves more budget.
        is_portal_hint = anchor.startswith(_EMBEDDED_PORTAL_ANCHOR_PREFIX)
        is_llm_hint = (
            (anchor.startswith(_LLM_HINT_ANCHOR_PREFIX) or score == _LLM_HINT_SCORE)
            and not is_portal_hint
        )
        if shared_budget is not None and (
            _link_hop_is_rich(sub_fetch) or is_llm_hint or is_portal_hint
        ):
            _refresh_cost_cap_for_hop(
                shared_budget,
                property_id=property_id,
                sub_url=sub_url,
                hop_index=idx,
            )

        # Silent homepage redirect guard: when a hop URL (e.g. /availability)
        # returns HTTP 200 but the server silently redirected back to the
        # homepage (same host, same or root path), the response body is
        # identical to what we already scraped. Running extraction again wastes
        # LLM budget and consumes a hop slot. Detect by comparing the redirect's
        # final_url host+path against the entry_url.
        _sub_final_url = getattr(sub_fetch, "final_url", None) or sub_url
        try:
            _entry_parsed = urllib.parse.urlparse(entry_url)
            _sub_final_parsed = urllib.parse.urlparse(_sub_final_url)
            _same_host = (
                (_sub_final_parsed.hostname or "") == (_entry_parsed.hostname or "")
            )
            _entry_path = (_entry_parsed.path or "/").rstrip("/") or "/"
            _sub_path = (_sub_final_parsed.path or "/").rstrip("/") or "/"
            _redirected_to_entry = _same_host and _sub_path in (_entry_path, "/", "")
        except Exception:
            _redirected_to_entry = False

        if _redirected_to_entry and _sub_final_url != sub_url:
            # The server redirected our sub-path hop back to the homepage —
            # identical content, no value in running extraction again.
            # B3 writer fix: this is a server-side transient redirect, not
            # a dead URL. The site may serve different content from this
            # URL once a session cookie or query param is in place.
            log.debug(
                "link-hop %s: silent redirect to homepage (%s) — skipping extraction",
                sub_url, _sub_final_url,
            )
            # Bug #6 enhancement (2026-05-16): persist the
            # redirected-to-entry URL into explored_links so subsequent
            # runs don't re-try it. Until today, the skip only saved
            # runtime on this run; next run would queue the same dead
            # URL again because nothing wrote the discovery.
            # Canonical victims: 1611onlakeunion.com hops 1/4/7/8 and
            # 1701arch.com hops 3/4/6/7 (2026-05-16 cloud run) burned
            # 7+ hop budget slots per property on URLs that always
            # redirect to the homepage.
            try:
                _explored_redirect = shared_budget.setdefault(
                    "_redirect_to_entry_urls", []
                )
                if sub_url not in _explored_redirect:
                    _explored_redirect.append(sub_url)
            except Exception:
                pass
            continue

        # Property sub-path priors: when the hop URL has 3+ path segments
        # (property-specific, not just a top-level prior guess), dynamically
        # add /floorplans and /floor-plans relative to that URL. This catches
        # sites like amli.com where the PMS prior is appended to the base
        # domain instead of the property's own deep URL.
        # Skip when in accumulation mode — the index page already queued its
        # sub-pages; adding more Entrata-style subpath variants here would create
        # a timeout spiral (e.g. chopakaapartments.com /conventional/floorplans,
        # /conventional/pricing, etc. all returning the same 4-unit listing).
        try:
            _hop_parsed = urllib.parse.urlparse(sub_url)
            _hop_path_parts = [p for p in _hop_parsed.path.split("/") if p]
            # Entrata module dead-ends: when the hop URL ends in a known Entrata
            # auth/widget module path, appending /floorplans etc. just produces
            # more auth-protected variants that return the same 300K shell.
            # PID 37719 1611onlakeunion.com 2026-05-15 burned 4 hops on
            # /Apartments/module/application_authentication/{floorplans,floor-plans,
            # pricing,apartments-pricing}. Strip these segments before composing
            # subpaths — let the universal-prior path try /floorplans at host root.
            # See data/reports/cloud_run_2026-05-15/TRIAGE.md RC #5.
            _ENTRATA_MODULE_DEAD_ENDS = (
                "application_authentication",
                "application-authentication",
                "unit-detail",
                "unit_detail",
            )
            _has_entrata_dead_end = any(
                p.lower() in _ENTRATA_MODULE_DEAD_ENDS for p in _hop_path_parts
            )
            if (
                len(_hop_path_parts) >= 3
                and dynamic_appended < max_dynamic_appends
                and not _in_floorplan_accumulation
                and not _has_entrata_dead_end
            ):
                _prop_sub_paths = ("/floorplans", "/floor-plans", "/pricing", "/apartments-pricing")
                # Compose subpaths into the PATH component only, preserving query
                # string and fragment. Naïve `sub_url + _psp` corrupts URLs with a
                # query string (e.g. `?id=X` + `/floorplans` -> `?id=X/floorplans`)
                # which many SPAs return 200 OK for, creating an extraction spiral.
                _hop_base_path = _hop_parsed.path.rstrip("/")
                for _psp in _prop_sub_paths:
                    _new_path = _hop_base_path + _psp
                    _psp_url = urllib.parse.urlunparse(_hop_parsed._replace(path=_new_path))
                    if _psp_url not in visited and not any(u == _psp_url for u, _, _ in queue):
                        queue.append((_psp_url, _PMS_PRIOR_SCORE + 200, f"prop_subpath:{_psp}"))
                        dynamic_appended += 1
                        if dynamic_appended >= max_dynamic_appends:
                            break
        except Exception:
            pass

        # Navigation-hint trust: when the LLM explicitly named this URL
        # as the place where unit data lives, it is high-confidence
        # diagnostic output. Reset ``llm_monolithic`` so the hinted page
        # can use the monolithic tier even if the entry page consumed
        # the per-property counter on its own no-content rescue. Portal
        # hints don't get this — the portal's own PMS adapter does the
        # work, no monolithic LLM call expected.
        if shared_budget is not None and is_llm_hint:
            _refresh_monolithic_budget_for_llm_hint(
                shared_budget,
                property_id=property_id,
                sub_url=sub_url,
                hop_index=idx,
            )

        # Re-run extraction on the sub-page via ``scrape()`` (not
        # ``scrape_jugnu``) so link-hop doesn't recurse — scrape_jugnu is
        # where the hop kicks in, scrape() itself only extracts.
        try:
            sub_result = await scrape(
                base_url=sub_url,
                profile=profile,
                expected_total_units=expected_total_units,
                # Pass the shared browser page so adapter probes (e.g. Entrata's
                # _probe_known_endpoints) can fire via page.evaluate() on the hop
                # page.  With Pattern A (reuse_page), the shared page has already
                # navigated to sub_url, so page.url reflects the hop URL and
                # _origin_from_ctx() derives the correct probe origin.
                # Falls back to None when no shared browser session is active.
                page=browser_page,
                fetch_result=sub_fetch,
                csv_row=csv_row,
                property_id=property_id,
                shared_budget=shared_budget,
                # Mark this scrape as a hop so the RC3 monolithic-LLM deferral
                # gate in signal_engine/decider.py does NOT fire — Rule 2 is
                # designed for the entry page only (defer the heavyweight LLM
                # to a hop that's about to happen). On a hop page, the LLM
                # should run immediately; deferring again is the cascading
                # bug that left 290347 + 246710 with no LLM run on any page.
                hop_depth=1,
            )
        except Exception as exc:
            log.warning("link-hop scrape failed for %s: %s", sub_url, exc)
            explored[sub_url] = False
            continue

        # 2026-05-17 Bug C fix — count plan_summaries as data too. PIDs
        # like 300327 flatson10th had LLM_DOM extract 6+1+6=13 rows on
        # SecureCafe portal hops; after the validity/classify gates, all
        # were partitioned to plan_summaries (no available_date / floor /
        # building) and ``sub_result["units"]`` was empty. With only
        # ``units`` counted as data, every hop looked dead → ``_try_link_hop``
        # returned ``_units_empty`` → the entry's RentCafe NO_RESPONSE
        # verdict shipped with 0 units. Recognising plan-level rows as
        # data here lets them propagate to the entry result and ship under
        # ``floor_plans[]`` post Fix 1.
        had_data = bool(sub_result.get("units")) or bool(sub_result.get("plan_summaries"))
        # B3 writer fix: only record had_data=True ("this URL gave us units,
        # prioritise it next run" → availability_links). A successful
        # 200-OK fetch that produced 0 units is NOT a dead URL — extraction
        # may succeed tomorrow under a different LLM completion, a freshly
        # added FieldCombination, or a relaxed gate. Recording False here
        # poisoned known-good endpoints permanently — the dominant cause of
        # 147/209 generic and 186/221 Entrata zero-hop failures observed
        # on 2026-05-14.
        if had_data:
            explored[sub_url] = True
            sub_result["_link_hop_from"] = entry_url
            sub_result["_link_hop_depth"] = 1
            sub_result["_link_hop_score"] = score
            sub_result["_link_hop_anchor"] = anchor
            # Merge explored history so the profile updater (Phase 4) can
            # record which links the crawler already tried.
            existing_explored = sub_result.get("_explored_links") or {}
            existing_explored.update(explored)
            sub_result["_explored_links"] = existing_explored

            # Track the page that has delivered the most units so the caller
            # can promote it to winning_page_url if it beats the current record.
            unit_count = len(sub_result.get("units") or [])
            if unit_count > _best_units_page[1]:
                _best_units_page = (sub_url, unit_count)

            # RC-PARTIAL (2026-05-15 PM): checkpoint EVERY non-empty sub_result
            # to _external_partial_ref, not just floor-plan-accumulation hits.
            # Pre-fix, the _external_partial_ref write at lines 2649-2651 only
            # fired inside the floor-plan-accumulation branch (gated on
            # `_any_rent` at line 2637). PIDs whose units came from a single
            # JSON-LD hop (no rent, e.g. AMLI's plan-only schema) returned
            # via the single-hop path at line ~2737 without ever updating
            # _external_partial_ref — so when the property wallclock expired
            # later (additional hops or LLM monolithic), the timeout handler
            # at scripts/runners/jugnu.py:391 saw _partial_state.get("units")
            # == None and SILENTLY DISCARDED the units. Cloud run 2026-05-15:
            # 5 of 6 AMLI PIDs (239952 24u, 249898 38u, 261770 60u, 40185
            # 15u, 62778 36u) lost their data this way; only 40193 (which
            # hit the accumulation branch) had the rescue fire.
            #
            # Maintain "best units" semantics so a subsequent empty hop
            # doesn't overwrite the running best — we keep whichever hop's
            # result has the most units. This mirrors _best_units_page above.
            if shared_budget is not None:
                _ext_ref = shared_budget.get("_external_partial_ref")
                if isinstance(_ext_ref, dict):
                    _existing = _ext_ref.get("units") or []
                    if unit_count > len(_existing):
                        _ext_ref["units"] = list(sub_result.get("units") or [])
                        _ext_ref["best_units_page"] = sub_url

            # Once profile:winning_page_url delivers >1 units on a WARM/HOT
            # profile with no recent failures (successfully yielded data in
            # the last 3 days by proxy), skip lower-scored priors.
            # Cold profiles or profiles with consecutive failures must still
            # explore — their winning_page_url may be stale.
            if anchor.startswith("profile:winning_page_url") and unit_count > 1:
                # Only apply the skip for WARM/HOT profiles whose last run
                # succeeded (consecutive_failures == 0) — a reliable proxy
                # for "winning_page_url is valid within the last 3 days".
                # Cold profiles or ones with recent failures must still
                # explore to rediscover the correct page.
                _profile_conf = getattr(profile, "confidence", None) if profile else None
                _profile_maturity = str(getattr(_profile_conf, "maturity", "COLD") or "COLD").upper()
                _profile_failures = int(getattr(_profile_conf, "consecutive_failures", 99) or 0)
                if _profile_maturity in ("WARM", "HOT") and _profile_failures == 0:
                    _winning_page_satisfied = True

            # Floor-plan index accumulation: after a successful sub-page
            # scrape, run _rank_internal_links on the sub-page HTML to
            # find floor-plan sub-sub-page links (e.g. /floorplans/the-edgefield/).
            # If any score ≥ 88 (same-prefix high-confidence links), add
            # them to the queue and continue accumulating — don't return
            # early. Catches Jonah-style index pages where dom_scan finds
            # 5 available units on the index but each plan sub-page carries
            # the FULL unit list as embedded JSON.
            #
            # Prefer embedded hints (pre-scroll, JSON blob still present)
            # and fall back to HTML link discovery (post-scroll, JS has
            # rendered the card links but consumed the JSON config).
            fp_hints = sub_result.get("_embedded_floorplan_subpage_hints") or []
            if not fp_hints and not _in_floorplan_accumulation:
                # HTML fallback: look for same-host sub-page links that
                # score highly from the page's rendered anchor tags.
                sub_html = (
                    (sub_fetch.body.decode("utf-8", errors="replace"))
                    if (hasattr(sub_fetch, "body") and sub_fetch.body)
                    else ""
                )
                if sub_html:
                    import re as _re_fp
                    # Hash-based URL pattern: /floorplans/unit-{32+hex}/
                    # These are single-unit detail pages, not plan-type
                    # sub-pages. Skip them — they waste hops for 1 unit
                    # each while plan-type pages (/floorplans/the-name/)
                    # carry the full unit list.
                    _HASH_PATH_RE = _re_fp.compile(
                        r"/(?:unit|apt|apartment)-[0-9a-f]{16,}/", _re_fp.IGNORECASE
                    )
                    sub_links = _rank_internal_links(sub_html, sub_url, limit=20)
                    for lnk_url, lnk_score, lnk_anchor in sub_links:
                        if lnk_score < 88 or lnk_url in visited:
                            continue
                        if _HASH_PATH_RE.search(lnk_url):
                            continue  # skip unit-detail pages
                        if any(u == lnk_url for u, _, _ in queue):
                            continue
                        # For portal-domain hops (e.g. ProspectPortal),
                        # only follow sub-paths that contain floor-plan
                        # keywords — skip photo/amenity/contact pages.
                        parsed_sub = None
                        try:
                            import urllib.parse as _up
                            parsed_sub = _up.urlparse(lnk_url)
                        except Exception:
                            pass
                        if parsed_sub is not None:
                            sub_path_l = (parsed_sub.path or "").lower()
                            base_path_l = (
                                _up.urlparse(sub_url).path or ""
                            ).lower()
                            # Same-prefix check: only follow links that
                            # are sub-paths of the current URL (avoids
                            # collecting site-wide nav links like
                            # /photos, /amenities, /contact).
                            if not sub_path_l.startswith(base_path_l.rstrip("/")):
                                # Allow if the link scores on a floor-plan
                                # path keyword specifically.
                                _path_kw_match = any(
                                    kw in sub_path_l
                                    for kw, _ in _LINK_PATH_KEYWORDS
                                    if kw in ("/floorplan", "/floor-plan",
                                              "/availability", "/units",
                                              "/conventional", "/apartments")
                                )
                                if not _path_kw_match:
                                    continue
                        fp_hints.append((lnk_url, "html_subpage"))

            # Cross-host per-plan detail discovery (2026-05-17). When the
            # same-host HTML fallback above came up empty AND the hop's
            # rows look like plan-summaries (beds/baths/sqft + rent, no
            # per-apartment identity), check the entry-page candidate
            # queue for any URL whose shape matches a per-floorplan
            # detail page. See ``_discover_cross_host_per_plan_urls`` for
            # the matching rules. Real-world case: PID 52331
            # alexandriacarmel — SecureCafe portal returns 10 plan rows
            # while per-plan detail pages live cross-host on the
            # marketing site (different host than the hop) and would
            # otherwise never be crawled.
            if not fp_hints and not _in_floorplan_accumulation:
                _index_units_for_match = sub_result.get("units") or []
                _has_plan_shape = any(
                    isinstance(u, dict)
                    and (u.get("bedrooms") is not None or u.get("beds") is not None)
                    and (u.get("market_rent_low") is not None or u.get("market_rent_high") is not None)
                    for u in _index_units_for_match
                )
                if _has_plan_shape:
                    _per_plan_matched = _discover_cross_host_per_plan_urls(
                        queue=queue,
                        visited=visited,
                        sub_url=sub_url,
                        existing_fp_hints=fp_hints,
                    )
                    if _per_plan_matched:
                        for _candidate_url in _per_plan_matched:
                            fp_hints.append(
                                (_candidate_url, "cross_host_per_plan_url")
                            )
                        log.info(
                            "cross-host per-plan discovery for %s: "
                            "matched %d entry-queue candidates by URL shape "
                            "from plan-index hop %s",
                            property_id, len(_per_plan_matched), sub_url[:80],
                        )

            if fp_hints and not _in_floorplan_accumulation:
                # Guard: enter accumulation when the index page has any
                # genuine floor-plan content — at least one bed/bath/sqft/
                # plan-name signal per unit. The earlier gate required
                # non-null RENT to enter accumulation, which rejected
                # LEASE_UP properties, "Starting from / Call for Pricing"
                # marketing pages, JSON-LD ApartmentComplex summaries, and
                # any property whose index lists plan structure but defers
                # pricing to the sub-pages — exactly the case where
                # accumulation is most valuable (sub-pages CARRY the rent).
                #
                # The original concern was Entrata /conventional/ summary
                # cards producing duplicate-plan-name records. That's a
                # dedup concern, not an admission concern — handled by the
                # accumulation-loop dedup at lines ~2820+ which keys on
                # (unit_id|unit_number, floor_plan_name, rent_low). Two
                # plan-name-only cards with the same name dedup to one.
                #
                # 2026-05-16 — see TRIAGE for cloud_run_2026-05-15: this
                # gate prevented ~5 of 6 AMLI timeout-killed PIDs (and an
                # unknown number of LEASE_UP properties) from accumulating
                # sub-page units even though their index pages carried
                # full floor-plan structure.
                _index_units = sub_result.get("units") or []
                _has_fp_data = any(
                    (
                        u.get("beds") is not None
                        or u.get("bedrooms") is not None
                        or u.get("baths") is not None
                        or u.get("bathrooms") is not None
                        or u.get("sqft")
                        or u.get("area")
                        or u.get("floor_plan_name")
                        or u.get("floorplan_name")
                        or u.get("rent_low")
                        or u.get("rent_high")
                        or u.get("asking_rent")
                    )
                    for u in _index_units
                )
                if _has_fp_data:
                    # Mark accumulation mode so recursive sub-pages are merged,
                    # not treated as new floor-plan index pages.
                    _in_floorplan_accumulation = True
                    _first_successful_result = sub_result
                    # D7 (2026-05-16): use merge_into_result_units instead of
                    # blind .extend() so cross-hop emissions of the same unit
                    # (rank R0a uid match, R0 fp_id match) dedup. Pre-D7 this
                    # was .extend() which produced 29 records for 12 real units
                    # on PID 722 Canyon Ridge.
                    try:
                        from ma_poc.pms.adapters._merge_fns import (
                            merge_into_result_units,
                        )
                        _new_units = sub_result.get("units") or []
                        _accumulated_units = merge_into_result_units(
                            _accumulated_units, _new_units,
                            property_id=property_id,
                        )
                    except Exception as _merge_err:
                        log.debug(
                            "D7 merge fallback (extend) for %s: %s",
                            property_id, _merge_err,
                        )
                        _accumulated_units.extend(sub_result.get("units") or [])
                    # Checkpoint partial results so the timeout handler can
                    # salvage accumulated units if the property wall-clock budget
                    # expires mid-hop.
                    if shared_budget is not None:
                        shared_budget["_partial_units"] = list(_accumulated_units)
                        shared_budget["_partial_result"] = sub_result
                        _ext_ref = shared_budget.get("_external_partial_ref")
                        if isinstance(_ext_ref, dict):
                            _ext_ref["units"] = list(_accumulated_units)
                    # Cache the LLM DOM selectors from this index page so
                    # sub-pages can replay them without another LLM call.
                    if _fp_llm_selectors is None:
                        _css = (sub_result.get("_llm_hints") or {}).get("css_selectors")
                        if isinstance(_css, dict) and _css.get("container"):
                            _fp_llm_selectors = _css
                            if shared_budget is not None:
                                shared_budget["_fp_css_hint"] = _css
                    # Queue all floor-plan sub-page hints (within dynamic cap).
                    for fp_url, fp_kind in fp_hints:
                        if dynamic_appended >= max_dynamic_appends:
                            break
                        if fp_url in visited or any(u == fp_url for u, _, _ in queue):
                            continue
                        queue.append(
                            (fp_url, _EMBEDDED_PORTAL_SCORE,
                             f"{_EMBEDDED_PORTAL_ANCHOR_PREFIX}{fp_kind}")
                        )
                        dynamic_appended += 1
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
                    # Continue the loop — DON'T return yet.
                    continue
                else:
                    log.debug(
                        "link-hop %s: floor-plan index has %d units but NO "
                        "fp-data signal (no beds/baths/sqft/plan_name/rent) "
                        "— returning as leaf (no accumulation)",
                        sub_url, len(_index_units),
                    )

            elif _in_floorplan_accumulation:
                # Accumulating sub-page units — merge into the running total.
                # D7 (2026-05-16): use rank-ladder merge instead of blind
                # extend so the same unit emitted from multiple per-plan
                # hop pages dedups at R0a (uid match).
                try:
                    from ma_poc.pms.adapters._merge_fns import (
                        merge_into_result_units,
                    )
                    _new_units = sub_result.get("units") or []
                    _accumulated_units = merge_into_result_units(
                        _accumulated_units, _new_units,
                        property_id=property_id,
                    )
                except Exception as _merge_err:
                    log.debug(
                        "D7 merge fallback (extend) for %s: %s",
                        property_id, _merge_err,
                    )
                    _accumulated_units.extend(sub_result.get("units") or [])
                _accum_pages_fetched += 1
                if shared_budget is not None:
                    shared_budget["_partial_units"] = list(_accumulated_units)
                    shared_budget["_partial_result"] = _first_successful_result or sub_result
                    _ext_ref = shared_budget.get("_external_partial_ref")
                    if isinstance(_ext_ref, dict):
                        _ext_ref["units"] = list(_accumulated_units)
                # Cache selectors from first sub-page that has them.
                if _fp_llm_selectors is None:
                    _css = (sub_result.get("_llm_hints") or {}).get("css_selectors")
                    if isinstance(_css, dict) and _css.get("container"):
                        _fp_llm_selectors = _css
                        if shared_budget is not None:
                            shared_budget["_fp_css_hint"] = _css
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
                if _accum_pages_fetched >= _MAX_FLOORPLAN_ACCUM_PAGES:
                    log.debug(
                        "link-hop %s: floor-plan accumulation cap (%d pages) reached — stopping",
                        entry_url, _MAX_FLOORPLAN_ACCUM_PAGES,
                    )
                    break
                continue

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
            sub_result["_best_units_page"] = _best_units_page[0] or sub_url
            sub_result["_best_units_count"] = _best_units_page[1]
            return sub_result

        # Dynamic discovery: a sub-fetch may itself have surfaced
        # leasing-portal pointers (e.g. /floorplans/ inlines a SightMap
        # embed URL). Harvest them into the queue so they're fetched in
        # the same link-hop pass instead of waiting for the next run.
        if dynamic_appended < max_dynamic_appends:
            sub_portal_hints = sub_result.get("_embedded_portal_hints") or []
            for hint in sub_portal_hints:
                if dynamic_appended >= max_dynamic_appends:
                    break
                try:
                    url_s, portal_name = hint
                except Exception:
                    continue
                url_s = str(url_s or "").strip()
                if not url_s or url_s in visited or url_s in explored_skip:
                    continue
                # Don't re-queue if already in the queue.
                if any(u == url_s for u, _, _ in queue):
                    continue
                queue.append(
                    (url_s, _EMBEDDED_PORTAL_SCORE,
                     f"{_EMBEDDED_PORTAL_ANCHOR_PREFIX}{portal_name}")
                )
                dynamic_appended += 1

    # If we were in floor-plan accumulation mode, return the merged result.
    # Deduplicate by (unit_id or unit_number + floor_plan_name + rent_low).
    if _in_floorplan_accumulation and _first_successful_result is not None:
        seen_ids: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for u in _accumulated_units:
            key = (
                u.get("unit_id") or u.get("unit_number") or "",
                u.get("floor_plan_name") or u.get("floor_plan_id") or "",
                str(u.get("rent_low") or u.get("market_rent_low") or ""),
            )
            key_str = "|".join(key)
            if key_str not in seen_ids:
                seen_ids.add(key_str)
                deduped.append(u)
        _first_successful_result["units"] = deduped
        existing_explored = _first_successful_result.get("_explored_links") or {}
        existing_explored.update(explored)
        _first_successful_result["_explored_links"] = existing_explored
        # Expose the page that delivered the most units so the profile updater
        # can promote it to winning_page_url.
        if _best_units_page[0]:
            _first_successful_result["_best_units_page"] = _best_units_page[0]
            _first_successful_result["_best_units_count"] = _best_units_page[1]
        return _first_successful_result

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
    partial_state: dict[str, Any] | None = None,
    force_cold: bool = False,
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
    force_cold : bool
        When True, treat the property as a fresh COLD scrape: clear
        ``profile.navigation.winning_page_url``, ``availability_links``,
        ``explored_links``, ``dead_links``, and ``dom_hints.field_selectors``
        for THIS run only — do not mutate the persisted profile. Used by
        the runner's recovery loop when a WARM/HOT profile fails today
        (PMS provider can change without notice; the prior winning URL or
        cached selectors may be stale).

    Returns
    -------
    dict
        Legacy-compatible 46-key result dict.
    """
    from ma_poc.observability.events import EventKind, emit
    from ma_poc.pms.contracts import ExtractResult

    base_url = task.url if hasattr(task, "url") else str(task)
    property_id = task.property_id if hasattr(task, "property_id") else "unknown"

    # Cold-profile retry: when force_cold=True, build an in-memory copy of
    # the profile with the stale navigation / DOM hints cleared so this
    # run behaves as if the property had never been scraped before. The
    # persisted profile is NOT mutated — the runner only flips the flag
    # for the retry attempt.
    if force_cold and profile is not None:
        try:
            from ma_poc.models.scrape_profile import ProfileMaturity as _PM_CLR
            profile = profile.model_copy(deep=True)
            nav = getattr(profile, "navigation", None)
            if nav is not None:
                nav.winning_page_url = None
                nav.availability_links = []
                nav.explored_links = []
                nav.dead_links = {}
            dh = getattr(profile, "dom_hints", None)
            if dh is not None:
                # Don't carry the prior selector set — it's a likely
                # reason the warm run failed today.
                dh.field_selectors = None
            # Force COLD maturity so the planner gives a generous budget
            # (consecutive_successes is NOT reset on the persisted profile;
            # this is in-memory only).
            try:
                profile.confidence.maturity = _PM_CLR.COLD
            except Exception:
                pass
        except Exception:
            # Defensive: if cloning fails, fall back to using the original
            # profile rather than crashing the retry.
            pass

    # Phase G2: compute budget here so link-hop guard can respect it.
    # F0.1: env-driven _cost_cap_usd injected via the fallback dict and
    # via compute_budget() so both code paths agree on the cap.
    from ma_poc.services.source_planner import get_property_llm_cost_cap_usd
    _jugnu_budget: dict = {
        "llm_api_calls": 3,
        "llm_dom_calls": 1,
        "llm_monolithic": 1,
        "link_hop": 3,
        "_cost_cap_usd": get_property_llm_cost_cap_usd(),
    }
    if profile is not None:
        try:
            from ma_poc.services.source_planner import compute_budget as _cb
            from ma_poc.models.scrape_profile import ProfileMaturity as _PM
            _jugnu_budget = _cb(profile, is_cold=profile.confidence.maturity == _PM.COLD)
        except Exception:
            pass

    # Store a reference to the caller-supplied partial_state dict inside the
    # budget dict. Because _jugnu_budget is passed by reference into scrape()
    # and then into _try_link_hop, any accumulated units written to
    # shared_budget["_external_partial_ref"] are visible in the caller's
    # _partial_state even after asyncio.wait_for cancels this coroutine.
    if partial_state is not None:
        _jugnu_budget["_external_partial_ref"] = partial_state

    # Delta 2: short-circuit on non-OK fetch
    # RC5: EMPTY_BODY gets a distinct verdict prefix so dashboards can
    # distinguish "server returned 200 but no content" from real unreachable.
    # (_OUTCOME_VERDICT_PREFIX is a module-level constant — see above.)
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
            _verdict_prefix = _OUTCOME_VERDICT_PREFIX.get(outcome_val, "FAILED_UNREACHABLE")
            result["errors"].append(
                f"{_verdict_prefix}: fetch_outcome={outcome_val} "
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

    # RC-PARTIAL (2026-05-15 PM): mirror the entry-page result's units to
    # _external_partial_ref so the per-property timeout handler in
    # scripts/runners/jugnu.py:387 sees them. Pre-fix only the
    # _try_link_hop floor-plan-accumulation branch updated this ref —
    # if the entry-page extraction produced units but cancellation hit
    # AFTER entry returned and DURING link_hop (additional discovery,
    # LLM monolithic on a hop, etc.), the entry units would be silently
    # discarded by the timeout handler. Now: any entry-page result with
    # ≥1 unit is checkpointed before link_hop runs. Subsequent hop
    # writes use the "most units wins" rule at scraper.py:2580 so a
    # hop with MORE units replaces the entry value, but a hop with
    # FEWER (or zero) does NOT overwrite the entry's checkpoint.
    if partial_state is not None:
        try:
            _entry_units = result.get("units") if isinstance(result, dict) else None
            if _entry_units:
                _existing = partial_state.get("units") or []
                if len(_entry_units) > len(_existing):
                    partial_state["units"] = list(_entry_units)
                    partial_state["best_units_page"] = base_url
        except Exception:
            pass  # never let partial-state book-keeping mask the scrape result

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
                _decision = plan_next_action(
                    _report,
                    sources_already_run=set(),
                    budget_remaining=dict(_jugnu_budget),
                    pms_name=detected_pms.get("pms", "unknown"),
                )
                if _decision.action == "ESCALATE_LINK_HOP":
                    should_hop = True
            except Exception:
                pass

    if should_hop:
        # Bug 5 alignment (2026-05-09 deep-dive): the per-hop cost-cap
        # refresh is now gated on _link_hop_is_rich and applied INSIDE
        # _try_link_hop after each hop fetch returns OK — see the call
        # site near LINK_HOP_FETCHED. Removing the unconditional pre-loop
        # refresh stops us from subsidising login walls and redirects.
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
                # Extract the post-redirect landed URL from the fetcher so
                # the link-hop ranker can: (a) accept cross-domain anchors
                # on the landed host as same-site, (b) resolve relative
                # anchors against the post-redirect base, and (c) synthesise
                # PMS priors against the post-redirect host. Without this,
                # 1701arch.com (CSV) → livethearch.com (landed) loses the
                # "Floor Plans" anchor at livethearch.com/.../conventional/.
                # See data/canary/local_runs/fix-validation-2026-05-15/
                # UNCHANGED_FAIL_ANALYSIS.md PIDs 53592 + 119144.
                _landed_url = getattr(fetch_result, "final_url", None) or None
                if _landed_url == base_url:
                    _landed_url = None
                _initial_visited = {base_url}
                if _landed_url:
                    _initial_visited.add(_landed_url)

                # Shard_84 fix (2026-05-16): early-termination hop budget.
                # When the entry page has ZERO floor-plan signals AND no
                # embedded portal hints AND no profile.winning_page_url,
                # there's no positive evidence that this property has
                # unit data anywhere. The hop scheduler would otherwise
                # burn 7 hops × ~25s each = up to 175s on PMS-prior
                # guesses that overwhelmingly return the homepage shell
                # again (see Bug #6 redirect-loop diagnosis). Cap at 2
                # hops in this scenario — enough to try /floor-plans and
                # /availability one time, fail fast, and surrender the
                # budget to the LLM rescue path.
                #
                # Canonical victims on 2026-05-16 shard_84: PIDs 63485,
                # 63486, 63534, 6395 — each burned 9-11 hops on dead
                # RentCafe/RealPage portal cycles before the 600s
                # wallclock killed the property.
                _entry_fp_count = int(
                    (result.get("_html_characterization") or {}).get(
                        "floor_plan_signal_count", 0
                    ) or 0
                )
                _has_portal_hints = bool(result.get("_embedded_portal_hints"))
                _has_winning_page = False
                try:
                    _wpu = (
                        profile.navigation.winning_page_url if profile else None
                    )
                    _has_winning_page = bool(_wpu)
                except Exception:
                    _has_winning_page = False
                _max_hops_effective = 7
                if (
                    _entry_fp_count == 0
                    and not _has_portal_hints
                    and not _has_winning_page
                ):
                    _max_hops_effective = 2
                    log.debug(
                        "link-hop budget capped at 2 for %s: entry has "
                        "0 fp_signals, no portal hints, no winning_page_url",
                        property_id,
                    )

                # Phase 5: feed LLM navigation hints (if any) into the
                # ranker so they outrank keyword candidates.
                hop_result = await _try_link_hop(
                    entry_url=base_url,
                    entry_page_html=entry_html,
                    detected=detected,
                    profile=profile,
                    expected_total_units=expected_total_units,
                    property_id=property_id,
                    csv_row=csv_row,
                    max_hops=_max_hops_effective,
                    llm_navigation_hints=result.get("_llm_navigation_hints"),
                    embedded_portal_hints=result.get("_embedded_portal_hints"),
                    visited_urls=_initial_visited,  # Phase 9: cycle protection (H5)
                    shared_budget=_jugnu_budget,
                    browser_page=page,  # Pattern A: share entry-page session
                    landed_url=_landed_url,
                )
            except Exception as exc:
                log.warning("link-hop orchestration failed for %s: %s", property_id, exc)
                hop_result = None

            # 2026-05-17 Bug C fix — accept hop_result that has plan_summaries
            # even when units is empty. RentCafe SecureCafe hops + similar
            # LLM-DOM extractions on portal pages often emit rows with rent
            # + dimensions but no per-unit signal (no available_date / floor
            # / building), so post_process classifies them as plan-level.
            # Pre-fix this whole branch was gated on ``units`` only, so the
            # extracted rows never reached the entry result.
            _hop_has_data = bool(hop_result) and (
                bool(hop_result.get("units")) or bool(hop_result.get("plan_summaries"))
            )
            if _hop_has_data:
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
                            # Combine telemetry from main + sub-page so the
                            # self-learning loop sees every mapping, blocked
                            # endpoint, CSS selector, and explored link the
                            # link-hop discovered. The previous inline loop
                            # only handled list-typed keys, silently dropping
                            # ``_llm_analysis_results`` / ``_llm_hints`` /
                            # ``_explored_links`` (all dict-typed) and costing
                            # TIER_MERGED_CROSS_PAGE wins their persistence.
                            _merge_post_hop_telemetry(result, hop_result)
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
                            # 2026-05-17 Bug C: also propagate plan_summaries from
                            # hop_result so plan-level rows extracted on hop pages
                            # reach the v2 ``floor_plans[]`` output (post Fix 1).
                            "plan_summaries",
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

    # Surface budget-level signals back to the result dict so profile_updater
    # can read them without needing direct access to the budget object.
    if _jugnu_budget.get("_winning_page_url_hop_outcome"):
        result["_winning_page_url_hop_outcome"] = _jugnu_budget["_winning_page_url_hop_outcome"]

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
