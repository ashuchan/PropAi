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
# Heuristic: at least N rent-shaped tokens ($1234 or $1234/mo) anywhere in
# the body. Five distinct hits is uncommon outside an actual pricing page.
_RICH_HOP_RENT_TOKEN_RE = re.compile(r"\$\d{3,4}")
_RICH_HOP_MIN_RENT_TOKENS = 5


def _link_hop_is_rich(fetch_result: Any) -> bool:
    """Bug 5 alignment: should this hop's body trigger a cost-cap refresh?

    True only when the body is large enough AND carries a positive content
    signal (JSON-LD FloorPlan/Apartment OR enough rent-shaped tokens).
    Filters out redirect bodies, login walls, and Cloudflare interstitials
    that the previous unconditional refresh blindly subsidised.
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
    rent_hits = sum(1 for _ in _RICH_HOP_RENT_TOKEN_RE.finditer(body_str))
    return rent_hits >= _RICH_HOP_MIN_RENT_TOKENS


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
        # Allow-list also widened to include ``onesite`` and ``amli`` per the
        # May-8 implementation plan F1.3.
        # F1.2: also short-circuit on captcha — bodies captured behind a
        # Cloudflare interstitial are noise the rescue can't extract from.
        captcha_detected = bool(getattr(fetch_result, "captcha_detected", False))
        needs_rescue = (
            not property_passes_quality_gate(adapter_result.units)
            and bool(raw_api_responses)
            and adapter_name in {"generic", "entrata", "appfolio", "onesite", "amli"}
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
                    source_adapter=adapter_name,
                    api_responses=raw_api_responses,
                    profile_snapshot=(
                        ctx.profile.model_dump(mode="json") if ctx.profile is not None else None
                    ),
                )
            )

            result["_rescue_cost_usd"] = rescue.cost_usd

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


# Sentinel score for LLM-emitted navigation hints. Detected downstream
# (anchor prefix ``llm-hint:``) by ``_try_link_hop`` to refresh the
# monolithic LLM budget before scraping the suggested URL — the LLM has
# already diagnosed where unit data lives, so we trust it enough to
# grant one more monolithic shot on that page even if the entry page
# burned the original budget on a no-content marketing shell.
_LLM_HINT_SCORE = 10_000
_LLM_HINT_ANCHOR_PREFIX = "llm-hint:"


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
        if abs_url in hinted_urls:
            continue
        hinted_urls.add(abs_url)
        augmented.append(
            (abs_url, _LLM_HINT_SCORE, f"{_LLM_HINT_ANCHOR_PREFIX}{raw_s[:60]}")
        )
    rest = [(u, s, a) for (u, s, a) in ranked if u not in hinted_urls]
    return augmented + rest


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


def _rank_internal_links(
    page_html: str,
    base_url: str,
    limit: int = 5,
) -> list[tuple[str, int, str]]:
    """Rank internal links on a page for likelihood of carrying unit data.

    Scores each link by anchor text, path keywords, and host (portal
    subdomains). Returns ``[(url, score, anchor_text), ...]`` sorted best
    first. Never raises — parser errors yield an empty list.
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

    ranked = _rank_internal_links(entry_page_html, entry_url, limit=max_hops)
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
        try:
            nav = profile.navigation
            wpu = getattr(nav, "winning_page_url", None)
            if isinstance(wpu, str) and wpu and wpu not in visited:
                # Highest possible score so it always lands first.
                profile_top.append((wpu, _LLM_HINT_SCORE + 1, "profile:winning_page_url"))
            for link in getattr(nav, "availability_links", []) or []:
                if isinstance(link, str) and link and link not in visited:
                    profile_top.append((link, _LLM_HINT_SCORE, "profile:availability_link"))
            for dead in getattr(nav, "explored_links", []) or []:
                if isinstance(dead, str) and dead:
                    explored_skip.add(dead)
        except Exception:
            # Profile access is best-effort — never let a malformed
            # profile sink the link-hop entirely.
            pass

    # Merge profile-top candidates ahead of the existing ranking, dedup
    # by URL (profile entry wins on collision since it carries the
    # higher score).
    if profile_top:
        seen_in_top = {u for u, _, _ in profile_top}
        ranked = profile_top + [
            (u, s, a) for (u, s, a) in ranked if u not in seen_in_top
        ]

    # Phase 9: drop URLs already visited (cycle break) — and skip the
    # profile's recorded dead ends so we don't re-pay for them.
    ranked = [
        (u, s, a) for (u, s, a) in ranked
        if u not in visited and u not in explored_skip
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

        # Bug 5 alignment (2026-05-09 deep-dive): if this hop's body looks
        # rich (≥50KB AND JSON-LD FloorPlan/Apartment OR ≥5 rent tokens),
        # raise the cost cap on the shared budget so the sub-page's LLM
        # rescue can fire even if the entry page already burned the cap.
        # Gated by the richness predicate so login walls / redirects /
        # CF interstitials don't silently buy themselves more budget.
        is_llm_hint = anchor.startswith(_LLM_HINT_ANCHOR_PREFIX) or score == _LLM_HINT_SCORE
        if shared_budget is not None and (_link_hop_is_rich(sub_fetch) or is_llm_hint):
            _refresh_cost_cap_for_hop(
                shared_budget,
                property_id=property_id,
                sub_url=sub_url,
                hop_index=idx,
            )

        # Navigation-hint trust: when the LLM explicitly named this URL
        # as the place where unit data lives, it is high-confidence
        # diagnostic output. Reset ``llm_monolithic`` so the hinted page
        # can use the monolithic tier even if the entry page consumed
        # the per-property counter on its own no-content rescue.
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
                    max_hops=3,
                    llm_navigation_hints=result.get("_llm_navigation_hints"),
                    visited_urls={base_url},  # Phase 9: cycle protection (H5)
                    shared_budget=_jugnu_budget,
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
