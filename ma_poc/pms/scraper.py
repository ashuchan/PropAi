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
from ma_poc.pms.detector import DetectedPMS, collect_detector_signals, detect_pms
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
    # when we have neither.
    page_html: str | None = None
    if page is None and fetch_result is None:
        result["errors"].append("no page and no fetch_result provided")
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
            emit(EventKind.DETECTOR_SIGNALS,
                 result.get("_property_id") or "unknown",
                 **_signals)
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
                emit(EventKind.HTML_CHARACTERIZED,
                     result.get("_property_id") or "unknown",
                     **_html_char)
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
            prepared.append({
                "url": entry.get("url", ""),
                "body": parsed_body,
                "status": entry.get("status"),
                "content_type": entry.get("content_type"),
            })
        ctx._api_responses = prepared  # type: ignore[attr-defined]

    adapter_result: AdapterResult
    try:
        adapter_result = await adapter.extract(page, ctx)
    except Exception as exc:
        if _is_unreachable_error(exc):
            result["errors"].append(f"FAILED_UNREACHABLE: {exc}")
            result["_fallback_chain"] = fallback_chain
            return result
        adapter_result = AdapterResult(errors=[str(exc)])

    # --- Step 8: Fallback to generic if adapter returned empty ---
    if not adapter_result.units and pms_name != "unknown" and adapter_name != "generic":
        generic = get_adapter("unknown")  # resolves to generic
        generic_name = getattr(generic, "pms_name", "generic")
        fallback_chain.append(generic_name)

        # For detected-PMS failures, skip LLM in generic adapter
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

        try:
            fallback_result = await generic.extract(page, fallback_ctx)
            if fallback_result.units:
                adapter_result = fallback_result
                result["_adapter_used"] = generic_name
        except Exception as exc:
            adapter_result.errors.append(f"generic-fallback-error: {exc}")

    # --- Step 9: Populate legacy result ---
    result["units"] = adapter_result.units
    result["extraction_tier_used"] = adapter_result.tier_used or None
    result["errors"].extend(adapter_result.errors)
    result["api_calls_intercepted"] = [
        r.get("url", "") for r in adapter_result.api_responses
    ]
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
        "", page_html, flags=re.IGNORECASE | re.DOTALL,
    )
    text_bytes = len(re.sub(r"<[^>]+>", "", stripped).encode("utf-8", errors="ignore"))

    script_count = len(re.findall(r"<script\b", page_html, re.IGNORECASE))
    iframe_count = len(re.findall(r"<iframe\b", page_html, re.IGNORECASE))
    jsonld_types: list[str] = []
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page_html, flags=re.IGNORECASE | re.DOTALL,
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
    ("availability", 100), ("floor plan", 90), ("floor-plan", 90),
    ("floorplan", 85), ("pricing", 80), ("rent", 70), ("apartment", 60),
    ("unit", 55), ("lease", 50), ("tour", 40), ("apply", 30),
    ("schedule", 20),
)

_LINK_PATH_KEYWORDS: tuple[tuple[str, int], ...] = (
    # (substring, score) — matched against url path, lowercased
    ("/floor-plan", 95), ("/floorplan", 90), ("/availability", 95),
    ("/pricing", 80), ("/apartments", 70), ("/rent", 60), ("/units", 85),
    ("/leasing", 50), ("/lease", 45), ("/floorplans", 90),
    ("/availabilities", 95),
)

_LINK_HOST_KEYWORDS: tuple[tuple[str, int], ...] = (
    # (host suffix, score) — portals run on known subdomains
    (".rentcafe.com", 120), (".appfolio.com", 120),
    (".onlineleasing.realpage.com", 120), ("sightmap.com", 110),
    (".entrata.com", 115), ("commoncf.entrata.com", 115),
)

# Skip these link shapes outright — they're never availability pages.
_LINK_SKIP_PATTERNS: tuple[str, ...] = (
    "tel:", "mailto:", "javascript:", "#", ".pdf", ".jpg", ".jpeg",
    ".png", ".gif", ".webp", ".svg", ".mp4", ".mov", "/blog/",
    "/news/", "/privacy", "/terms", "/accessibility", "/sitemap",
    "facebook.com/", "twitter.com/", "instagram.com/", "linkedin.com/",
    "youtube.com/", "/contact", "/careers", "/jobs",
)


def _rank_internal_links(
    page_html: str, base_url: str, limit: int = 5,
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
        href = (a.get("href") or "").strip()
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
        is_same_site = link_host == base_host or link_host.endswith("." + base_host) or base_host.endswith("." + link_host)
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
    detected: "DetectedPMS",
    profile: Any,
    expected_total_units: int | None,
    property_id: str,
    csv_row: dict[str, Any] | None,
    max_hops: int = 3,
    llm_navigation_hints: list[str] | None = None,
) -> dict[str, Any] | None:
    """One-level BFS over home-page links when primary extraction is empty.

    Fetches up to ``max_hops`` candidate URLs via the L1 fetcher, re-runs
    ``scrape()`` on each, and returns the first sub-result that yields
    units. Returns ``None`` if no hop recovered data.

    ``llm_navigation_hints`` (Phase 5) takes priority over keyword-ranked
    candidates — if the LLM already diagnosed where data lives, we try
    that URL first instead of guessing from anchor text.
    """
    ranked = _rank_internal_links(entry_page_html, entry_url, limit=max_hops)
    if llm_navigation_hints:
        ranked = _augment_ranked_with_hints(ranked, llm_navigation_hints, entry_url)
        # Cap to keep budget bounded even with hints merged in.
        ranked = ranked[: max(max_hops, len(llm_navigation_hints) + 1)]
    if not ranked:
        return None

    try:
        from ma_poc.fetch import fetch as jugnu_fetch
    except ImportError:
        try:
            from fetch import fetch as jugnu_fetch  # type: ignore[import-not-found]
        except ImportError:
            return None
    from ma_poc.discovery.contracts import CrawlTask, TaskReason
    from ma_poc.fetch.contracts import RenderMode
    from ma_poc.observability.events import EventKind, emit

    emit(EventKind.LINK_HOP_STARTED, property_id,
         entry_url=entry_url,
         candidates=[{"url": u, "score": s, "anchor": a[:60]} for u, s, a in ranked])

    # Phase 4: track which sub-URLs were tried and whether they produced
    # data. profile_updater consumes this dict to persist
    # profile.navigation.explored_links (skip-next-run) and
    # profile.navigation.availability_links (prioritise-next-run).
    explored: dict[str, bool] = {}

    for idx, (sub_url, score, anchor) in enumerate(ranked, 1):
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
            emit(EventKind.LINK_HOP_FETCHED, property_id,
                 url=sub_url, error=str(exc)[:200], hop_index=idx)
            continue

        outcome_val = (
            sub_fetch.outcome.value if hasattr(sub_fetch.outcome, "value")
            else str(sub_fetch.outcome)
        )
        emit(EventKind.LINK_HOP_FETCHED, property_id,
             url=sub_url, outcome=outcome_val,
             elapsed_ms=sub_fetch.elapsed_ms,
             body_bytes=len(sub_fetch.body) if sub_fetch.body else 0,
             hop_index=idx, score=score, anchor=anchor[:60])

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
            emit(EventKind.LINK_HOP_RECOVERED, property_id,
                 entry_url=entry_url, sub_url=sub_url, units=len(sub_result["units"]),
                 tier=sub_result.get("extraction_tier_used"),
                 hop_index=idx, score=score)
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
    from ma_poc.pms.contracts import ExtractResult, ProfileHints

    base_url = task.url if hasattr(task, "url") else str(task)
    property_id = task.property_id if hasattr(task, "property_id") else "unknown"

    # Delta 2: short-circuit on non-OK fetch
    if hasattr(fetch_result, "outcome"):
        outcome_val = fetch_result.outcome.value if hasattr(fetch_result.outcome, "value") else str(fetch_result.outcome)
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
    emit(EventKind.PMS_DETECTED, property_id,
         pms=detected_pms.get("pms", "unknown"),
         confidence=detected_pms.get("confidence", 0.0))

    adapter_name = result.get("_adapter_used", "unknown")
    emit(EventKind.ADAPTER_SELECTED, property_id, adapter_name=adapter_name)

    # ── Option B: one-level link-hop when primary extraction is empty ──
    # If scrape() returned no units but the fetch was successful and we have
    # HTML, rank internal links by keyword + portal-subdomain and re-fetch
    # the top candidates. This catches vanity-domain + portal-subpage sites
    # (RentCafe/Entrata/AppFolio) where the home page references the PMS
    # but the actual unit data lives at /floor-plans, /availability, or on
    # a .rentcafe.com subdomain.
    if not result.get("units") and fetch_result is not None:
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
                )
            except Exception as exc:
                log.warning("link-hop orchestration failed for %s: %s",
                            property_id, exc)
                hop_result = None

            if hop_result and hop_result.get("units"):
                # Merge: keep the entry-URL telemetry (detector signals,
                # html characterization, fetch diagnostic), but replace
                # extraction fields with the sub-page's.
                for k in ("units", "extraction_tier_used",
                          "api_calls_intercepted", "_winning_page_url",
                          "_raw_api_responses", "_adapter_used",
                          "_fallback_chain", "_tier_attempts",
                          "_llm_interactions", "_llm_hints",
                          "_llm_analysis_results", "_llm_field_mappings",
                          "_explored_links"):
                    if k in hop_result:
                        result[k] = hop_result[k]
                for k in ("_link_hop_from", "_link_hop_depth",
                          "_link_hop_score", "_link_hop_anchor"):
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
        llm_cost_usd=sum(
            i.get("cost_usd", 0)
            for i in result.get("_llm_interactions", [])
        ),
        llm_calls=len(result.get("_llm_interactions", [])),
        errors=result.get("errors", []),
    )
    result["_extract_result"] = extract_result

    return result
