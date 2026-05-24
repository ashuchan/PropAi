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

from ma_poc.pms.adapters._probe import (
    reset_clearance_cookies,
    set_clearance_cookies,
)
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

# Property-level concession/special phrasing on marketing/RC pages.
# Deterministic, capture-first (raw matched phrase). Real patterns
# observed on RC /floorplans (probe 2026-05-19): "1 Month FREE",
# "8 Weeks Free", "Move-in Special", "Look & Lease", "$X off".
# Broadened 2026-05-19 to the empirically-closed family set (grind:
# cohort 22 + random 20/20). DETECTION trigger only — it fires capture
# of the enclosing clause window; the deterministic concession_normalize
# parser decides structure downstream. Every alternative is anchored on
# offer context (weeks/months/rent/$+off/lease/move-in/special/bonus)
# so bare-amenity "free" (wifi/parking/fitness) and rent-financing
# (FLEX) do NOT trigger.
_CW_NUM = (
    r"(?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve)"
)
_PROPERTY_CONCESSION_RE = re.compile(
    rf"\b{_CW_NUM}[\s-]*(?:full[\s-]+)?(?:weeks?|months?)['’]?s?[\s-]*"
    r"(?:of[\s-]+)?(?:rent[\s-]+)?(?:free|complimentary|on\s+us)\b"
    rf"|\b(?:rent[\s-]?)?free\s+(?:for\s+)?(?:{_CW_NUM}\s+)?(?:full\s+)?"
    r"(?:weeks?|months?)\b"
    rf"|\b(?:first|1st)\s+(?:{_CW_NUM}\s+)?(?:full\s+)?months?\b"
    r"[^.!?]{0,40}\bfree\b"
    r"|\bfree\s+rent\b|\bmonths?\s+on\s+us\b"
    r"|\$\s?\d{1,3}(?:,\d{3})*\s*(?:off|gift\s*card|credit|cash|savings|"
    r"welcome\s+bonus)\b"
    r"|\bsave\s+(?:up\s+to\s+)?\$\s?\d"
    r"|\$\s?\d{2,5}\s+(?:welcome\s+)?bonus\b"
    r"|\breduced\s+rents?\b|\brent\s+as\s+low\s+as\s+\$"
    r"|\blook[\s-]*(?:and|&|\+|n)?[\s-]*lease\b"
    r"|\b(?:move[- ]?in|mi)\s+special\b"
    r"|\b(?:rent|lease|deposit|move[- ]?in)\s+special\b"
    r"|\blimited[- ]time\s+(?:offer|special|savings|deal)\b"
    r"|\breduced\s+deposit\b"
    r"|\bwaived\s+(?:application|admin(?:istration)?|amenity|"
    r"move[- ]?in|deposit)\s*fees?\b",
    re.IGNORECASE,
)


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
    #
    # 2026-05-13 (teammate analysis — cross-cutting C1/C2/C8): when the
    # fetch redirected to a different domain (e.g.
    # ``elevatetosequoia.com`` → ``elevatetoriveroaks.com``), running the
    # detector against the original ``base_url`` host produces 0 fingerprint
    # matches and ``pms=unknown``. The HTML body belongs to the redirect
    # target, so the detector must run against ``fetch_result.final_url`` to
    # see the same hostname as the body.
    _effective_url = base_url
    if fetch_result is not None:
        _final_url = str(getattr(fetch_result, "final_url", "") or "")
        if _final_url:
            _effective_url = _final_url
    initial_detection = detect_pms(_effective_url, csv_row=csv_row)
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

    # --- Property-level concession capture (2026-05-19; window 2026-05-19b) ---
    # Deterministic non-LLM phrase scrape over page_html — $0, no extra
    # fetch, capture-first. The detector only TRIGGERS capture; we then
    # store the enclosing CLAUSE WINDOW (not the bare regex fragment) so
    # the downstream concession_normalize parser sees the full offer
    # ("...REDUCED RENT + 6 Weeks FREE..." not just "6 Weeks FREE").
    # concessions_text stays raw (capture-first); schema_v2 derives the
    # structured concessions_json from it.
    if page_html and not result.get("concessions_text"):
        try:
            # 2026-05-20 (concession-leak fix): the previous flat-text
            # build stripped ``<tag>`` markers but kept ``<script>`` and
            # ``<style>`` BODIES — adjacent JS code (e.g. Woodland Creek's
            # PropLeadSource ``href.indexOf("?") == -1`` block) and CSS
            # rules leaked into the ±200-char window around the
            # concession-pattern match. Of 49,677 captured concessions
            # in the 2026-05-19 feature canary, 49.9% were polluted this
            # way and 10,102 (~20%) hit the 300-char cap with junk-only
            # content — truncating the real offer entirely.
            # Strip script/style BLOCKS before tag-stripping so the
            # match window sees only visible text.
            _no_code = re.sub(
                r"<(script|style|noscript)\b[^>]*>.*?</\1>",
                " ",
                page_html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            _flat = re.sub(
                r"\s+", " ", re.sub(r"<[^>]+>", " ", _no_code)
            )
            _cm = _PROPERTY_CONCESSION_RE.search(_flat)
            if _cm:
                _s, _e = _cm.span()
                _win = _flat[max(0, _s - 200):_e + 200]
                _off = _s - max(0, _s - 200)
                # 2026-05-20 (header-only fix): the matched sentence is
                # often a banner header — "Limited Time Offer!",
                # "Move-in Special!", "Don't Miss Out!" — terminated by
                # ``!`` while the actionable body ("Move in by 6/15 and
                # get 1 month free rent.") lives in the NEXT sentence.
                # Single-sentence pick dropped the body entirely. Real
                # bytes from feature canary 2026-05-19 cid 74567
                # (Woodland Creek): all 46 rows captured just "Limited
                # Time Offer!" — the regex's ``limited time offer``
                # alternative anchored on the header, then sentence-
                # split discarded the body two sentences over. Fix:
                # start at the matched sentence and walk FORWARD while
                # the running total stays under 300 chars. Up to 2
                # extra sentences — bounded to keep the field
                # representative and avoid greedy capture of unrelated
                # marketing copy that follows.
                _parts = re.split(r"(?<=[.!?|•·])\s+", _win)
                _idx, _acc = -1, 0
                for _i, _p in enumerate(_parts):
                    if _acc <= _off < _acc + len(_p) + 1:
                        _idx = _i
                        break
                    _acc += len(_p) + 1
                if _idx >= 0:
                    _seg = _parts[_idx]
                    # Extend forward; cap at 300 chars total so the
                    # downstream truncation doesn't lop off the body
                    # we just rescued.
                    for _nxt in _parts[_idx + 1:_idx + 3]:
                        _candidate = (_seg + " " + _nxt).strip()
                        if len(_candidate) > 300:
                            break
                        _seg = _candidate
                else:
                    _seg = _win
                result["concessions_text"] = _seg.strip()[:300]
        except Exception:
            pass

    # --- Step 4: Re-detect with page HTML if available ---
    if page_html:
        html_detection = detect_pms(_effective_url, csv_row=csv_row, page_html=page_html)
        if html_detection.confidence > initial_detection.confidence:
            initial_detection = html_detection
            result["_detected_pms"] = _detection_to_dict(initial_detection)

    # --- Step 4b: detection rescue via curl_cffi homepage refetch -----------
    # 2026-05-17 iter-11 (canary 842-pool deep-probe): the patchright-
    # rendered page_html frequently hides PMS markers that ARE present in
    # the raw server HTML (post-render mutation / behind menus / CF shell).
    # Same root-cause class as the securecafe iter-9 fix. When detection is
    # still unknown/custom, curl_cffi-refetch the homepage (CF-bypass,
    # proxy-independent) and re-detect on that. Recovers the misclassified-
    # but-adapter-exists clusters (RealPage/OneSite, Funnel, Spherexx,
    # ResMan, Entrata, securecafe, …) the 842 probe surfaced.
    if initial_detection.pms in ("unknown", "custom"):
        try:
            from urllib.parse import urlparse as _up

            _p = _up(_effective_url)
            if _p.scheme and _p.netloc:
                from ma_poc.pms.adapters._probe import probe_get as _creqd_get

                _root = f"{_p.scheme}://{_p.netloc}"
                # iter-12 (2026-05-17, 604-probe finding): the PMS marker
                # is frequently NOT on the homepage but on the floorplan/
                # availability sub-page (the 604 unit-level-via-LLM/DOM
                # probe found markers on /floorplans/ etc. for ~270
                # proxy-independent sites the homepage-only rescue missed).
                # Try the homepage AND the common floorplan sub-paths;
                # first one that yields a confident PMS wins.
                for _suffix in (
                    "/",
                    "/floorplans/",
                    "/floor-plans/",
                    "/floorplans",
                    "/floor-plans",
                    "/availability/",
                    "/apartments/",
                ):
                    try:
                        _rr = _creqd_get(_root + _suffix, timeout=15)
                    except Exception:
                        continue
                    if _rr.status_code != 200 or not _rr.text:
                        continue
                    _rd = detect_pms(
                        _effective_url, csv_row=csv_row, page_html=_rr.text
                    )
                    if (
                        _rd.pms not in ("unknown", "custom")
                        and _rd.confidence > initial_detection.confidence
                    ):
                        initial_detection = _rd
                        result["_detected_pms"] = _detection_to_dict(
                            initial_detection
                        )
                        result["_detection_rescued"] = {
                            "via": f"curl_cffi_refetch:{_suffix}",
                            "pms": _rd.pms,
                        }
                        break
        except Exception as _dr_exc:  # pragma: no cover - defensive
            log.warning("detection-rescue failed for %s: %s", property_id, _dr_exc)

    # --- Telemetry A: detector signals ----------------------------------------
    # Attach raw detector inputs to the result so the per-property report can
    # render them, and emit DETECTOR_SIGNALS for ledger-level analytics.
    try:
        _signals = collect_detector_signals(_effective_url, csv_row, page_html)
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

    # Cookie-mint reuse (option b): install the clearance cookies the
    # fetcher's patchright render earned by passing the CF/DataDome
    # challenge, scoped to this property's adapter dispatch. The cheap
    # curl_cffi active-fetch in MAAC/Irvine/Cortland/Essex/Equity/
    # SightMap-iframe then reuses the solved clearance instead of hitting
    # the wall again. Unconditional set (empty when no challenge solved)
    # so a recursive link-hop scrape can't leave stale clearance behind;
    # reset before every return below.
    _clr_token = set_clearance_cookies(
        getattr(fetch_result, "clearance_cookies", None)
    )

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

    # --- Pattern B reveal: click "View Availability" / "Show Units" cards ----
    # Some marketing sites render plan cards collapsed and only reveal
    # rents on click (no XHR). When the rendered HTML has < 3 dollar
    # amounts AND has reveal-shaped button text, click them and let the
    # adapter see the post-click DOM via page.content(). No-ops when
    # ``page is None`` (Jugnu fetch-only path) or when the page already
    # has rent content. See ``ma_poc.pms.interactive_reveal`` for the
    # full design rationale and gating heuristics.
    try:
        from ma_poc.pms.interactive_reveal import maybe_reveal as _maybe_reveal

        reveal_telemetry = await _maybe_reveal(page, page_html=page_html)
        result["_interactive_reveal"] = reveal_telemetry
    except Exception as exc:  # pragma: no cover - defensive
        result["_interactive_reveal"] = {
            "triggered": False,
            "reason": f"exception: {exc}",
        }

    adapter_result: AdapterResult
    try:
        adapter_result = await adapter.extract(page, ctx)  # type: ignore[arg-type]
    except Exception as exc:
        if _is_unreachable_error(exc):
            result["errors"].append(f"FAILED_UNREACHABLE: {exc}")
            result["_fallback_chain"] = fallback_chain
            reset_clearance_cookies(_clr_token)
            return result
        adapter_result = AdapterResult(errors=[str(exc)])

    # --- Path B/C: empty-exit + quality-gate retry with next-best PMS ---------
    # Three retry triggers, same mechanism:
    #   * Path B (``empty_exit``): adapter self-reports an empty-exit label
    #     (see ``ma_poc.pms.empty_exit``) AND produces no units.
    #   * Path C (``quality_gate``): adapter produced units but they fail
    #     the dimension gate — name-only stubs with no beds/baths/sqft.
    #   * Path C (``no_rent``): adapter produced units with dimensions but
    #     no rent across the board — the JSON-LD inflated-SUCCESS shape
    #     (`inferred_*` UIDs synthesized from name+beds+baths+sqft, no
    #     `offers.price`). Covers the 1,031-prop inflated-SUCCESS bucket
    #     identified in project_jsonld_recovery_2026-05-20.
    #   * Path C (``no_area``): adapter produced units with rent but no
    #     sqft/area across the board — partial extraction shape.
    #
    # On trigger: find the next PMS candidate and re-dispatch on the
    # same page. Bounded by ``PATH_B_MAX_RETRIES`` (default 2).
    #
    # Win condition: retry result must have units AND pass the dimension
    # gate AND have a rent signal. Retries with same-or-worse quality
    # are not promoted.
    #
    # **Plan-level fallback**: when the BASELINE adapter returned
    # plan-level rows (units with dims but no rent) and all retries
    # failed, the baseline is restored and the result is marked with a
    # ``_PLAN_LEVEL`` tier suffix + ``_verdict_quality=SUCCESS_PLAN_LEVEL``
    # so the data isn't lost — just honestly flagged. Per the
    # project_jsonld_recovery memo: "getting floor plan level data is
    # okay but just should be flagged".
    #
    # Feature flag: ``PATH_B_RETRY_ENABLED=0`` falls back to telemetry-only.
    try:
        import os as _retry_os

        from ma_poc.observability.events import EventKind as _RetryEventKind
        from ma_poc.observability.events import emit as _retry_emit
        from ma_poc.pms.adapters.registry import get_adapter as _retry_get_adapter
        from ma_poc.pms.detector import detect_pms_candidates
        from ma_poc.pms.empty_exit import empty_exit_reason, is_empty_exit
        from ma_poc.validation.schema_gate import (
            property_has_area_signal as _retry_area_signal,
            property_has_rent_signal as _retry_rent_signal,
            property_passes_quality_gate as _retry_quality_gate,
        )

        _PATH_B_RETRY_ENABLED = (
            _retry_os.environ.get("PATH_B_RETRY_ENABLED", "1").lower()
            not in {"0", "false", "no", ""}
        )
        _PATH_B_MAX_RETRIES = int(
            _retry_os.environ.get("PATH_B_MAX_RETRIES", "2")
        )

        def _retry_trigger_reason(res: "AdapterResult") -> str | None:
            """Return ``"empty_exit"`` / ``"quality_gate"`` / ``"no_rent"``
            / ``"no_area"`` / None. None means the adapter is fine."""
            if is_empty_exit(res.tier_used) and not res.units:
                return "empty_exit"
            if res.units:
                if not _retry_quality_gate(res.units):
                    return "quality_gate"
                if not _retry_rent_signal(res.units):
                    return "no_rent"
                if not _retry_area_signal(res.units):
                    return "no_area"
            return None

        def _retry_win_condition(res: "AdapterResult") -> bool:
            """A retry winner must have units AND pass dimension gate AND
            have a rent signal. Same-or-worse quality is not promoted."""
            return bool(
                res.units
                and _retry_quality_gate(res.units)
                and _retry_rent_signal(res.units)
            )

        # Preserve the baseline (initial-adapter) result so plan-level
        # data isn't lost if all retries fail. Only relevant when the
        # baseline HAS units — for empty_exit triggers there's nothing
        # to preserve.
        _baseline_result: "AdapterResult | None" = (
            adapter_result if adapter_result.units else None
        )
        _baseline_adapter_name = adapter_name
        _retry_tried_pms: set[str] = {adapter_name}
        _retry_attempt = 0
        _retry_won = False
        _trigger_reason = _retry_trigger_reason(adapter_result)
        _initial_trigger_reason = _trigger_reason
        # The "current" result we evaluate triggers against. Starts as
        # the baseline; rolls forward to the latest attempt. Does NOT
        # mutate the public ``adapter_result`` until a win is confirmed.
        _current_result = adapter_result
        while (
            _trigger_reason is not None
            and _retry_attempt < _PATH_B_MAX_RETRIES
        ):
            _next_candidates = detect_pms_candidates(
                url=getattr(ctx, "base_url", "") or "",
                csv_row=None,
                page_html=page_html,
                exclude=_retry_tried_pms,
                max_candidates=_PATH_B_MAX_RETRIES,
            )
            if not _next_candidates:
                break
            _next_cand = _next_candidates[0]
            _previous_tier = _current_result.tier_used or ""
            _previous_pms = (
                adapter_name if _retry_attempt == 0 else _baseline_adapter_name
            )

            # Telemetry-only mode — emit and stop (no re-dispatch).
            if not _PATH_B_RETRY_ENABLED:
                _retry_emit(
                    _RetryEventKind.RETRY_WOULD_DISPATCH,
                    property_id=getattr(ctx, "property_id", "") or "",
                    previous_pms=_previous_pms,
                    previous_tier=_previous_tier,
                    empty_exit_reason=empty_exit_reason(_previous_tier) or "",
                    trigger_reason=_trigger_reason,
                    next_pms=_next_cand.pms,
                    next_confidence=_next_cand.confidence,
                    remaining_candidates=len(_next_candidates),
                )
                break

            _retry_attempt += 1
            _retry_emit(
                _RetryEventKind.RETRY_DISPATCHED,
                property_id=getattr(ctx, "property_id", "") or "",
                attempt=_retry_attempt,
                previous_pms=_previous_pms,
                previous_tier=_previous_tier,
                empty_exit_reason=empty_exit_reason(_previous_tier) or "",
                trigger_reason=_trigger_reason,
                next_pms=_next_cand.pms,
                next_confidence=_next_cand.confidence,
            )

            _retry_tried_pms.add(_next_cand.pms)
            _new_adapter = _retry_get_adapter(_next_cand.pms)
            _new_adapter_name = getattr(_new_adapter, "pms_name", _next_cand.pms)
            # Update ctx so the new adapter sees the right detection.
            ctx.detected = _next_cand
            try:
                _new_result = await _new_adapter.extract(page, ctx)  # type: ignore[arg-type]
            except Exception as _retry_exc:
                fallback_chain.append(
                    f"retry_failed:{_new_adapter_name}:{type(_retry_exc).__name__}"
                )
                break

            fallback_chain.append(f"retry:{_new_adapter_name}")
            if _retry_win_condition(_new_result):
                # WIN — promote
                _retry_emit(
                    _RetryEventKind.RETRY_SUCCESS,
                    property_id=getattr(ctx, "property_id", "") or "",
                    attempt=_retry_attempt,
                    previous_pms=_previous_pms,
                    previous_tier=_previous_tier,
                    trigger_reason=_trigger_reason,
                    won_pms=_new_adapter_name,
                    won_tier=_new_result.tier_used or "",
                    unit_count=len(_new_result.units),
                )
                adapter_result = _new_result
                adapter = _new_adapter
                adapter_name = _new_adapter_name
                result["_adapter_used"] = _new_adapter_name
                result["_detected_pms"] = _detection_to_dict(_next_cand)
                _retry_won = True
                break
            # Retry didn't recover real units — roll forward and try next.
            _current_result = _new_result
            _trigger_reason = _retry_trigger_reason(_current_result)

        # Plan-level fallback: all retries failed AND we had baseline
        # plan-level rows. Per the project_jsonld_recovery memo:
        # "getting floor plan level data is okay but just should be
        # flagged and one another path should be retried ... if unit
        # then pick that ... else floor plan".
        if (
            not _retry_won
            and _baseline_result is not None
            and _baseline_result.units
            and _initial_trigger_reason in {"quality_gate", "no_rent", "no_area"}
        ):
            adapter_result = _baseline_result
            # Stamp the tier with a _PLAN_LEVEL suffix and surface the
            # honest verdict on the property result dict.
            _baseline_tier = _baseline_result.tier_used or ""
            if _baseline_tier and "_PLAN_LEVEL" not in _baseline_tier:
                adapter_result.tier_used = f"{_baseline_tier}_PLAN_LEVEL"
            result["_verdict_quality"] = "SUCCESS_PLAN_LEVEL"
            result["_plan_level_reason"] = _initial_trigger_reason
    except Exception:  # pragma: no cover — Path B/C must never block scrape
        pass

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
        needs_rescue = (
            not property_passes_quality_gate(adapter_result.units)
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

    # --- Step 7b: Entrata→SightMap secondary adapter -----------------------
    # Many Entrata-hosted communities embed a SightMap interactive map that
    # carries the real unit inventory (the Entrata floorplan module returns
    # nothing). Both fingerprints match; the router commits to Entrata, it
    # yields 0 units, and we'd otherwise drop to the generic LLM cascade.
    #
    # Gate on a BROAD SightMap signal — any sightmap.com reference in the
    # rendered HTML (loader script, not just an <iframe> embed code) OR a
    # SightMap-shaped body already in the captured network log. SightMap
    # frequently injects its iframe/API *after* the HTML the scraper
    # captured (e.g. tarowalk.com: sightmap.com is only a script host, no
    # embed code in page_html), so the old find_sightmap_embed_codes()
    # precondition silently missed every dynamically-loaded SightMap.
    # Delegate actual discovery to SightMapAdapter — it already handles
    # captured-response parsing + iframe + direct-API fallback and
    # self-gates (SIGHTMAP_NO_RESPONSE) when there's genuinely nothing.
    if (
        not adapter_result.units
        and pms_name == "entrata"
        and page_html
    ):
        try:
            from ma_poc.pms.adapters.sightmap import (
                SightMapAdapter,
                _is_sightmap_response,
            )

            captured = getattr(ctx, "_api_responses", []) or []
            sm_signal = "sightmap.com" in page_html.lower() or any(
                _is_sightmap_response(r.get("body")) for r in captured
            )

            # iter-5: Entrata "engrain" sites embed the SightMap only on the
            # floorplan sub-page (/<city>/<slug>/conventional/), NOT on the
            # homepage the scraper captured. When there's no SightMap signal
            # in page_html, discover that sub-page from the nav links,
            # curl_cffi-fetch it (Entrata sub-pages are Cloudflare-fronted;
            # the embed code IS in the server HTML — confirmed chaseknolls
            # → sightmap.com/embed/n9w63m8jw71), and splice it into the
            # ctx fetch_result body so SightMapAdapter's own iframe/embed
            # discovery finds it.
            # iter-19: the canary/prod render is proxy-less; CF-fronted
            # Entrata/prospectportal sites return a CF challenge shell as
            # page_html, so the sightmap.com signal is invisible and the
            # /conventional/ discovery below (also keyed off page_html /
            # captured net) finds nothing — the SightMap embed is never
            # reached and ~250 dual-fingerprint sites misroute to a
            # 0-unit Entrata result. Recover by proxied-probe of the
            # homepage (the proven iter-13 securecafe pattern: probe_get
            # clears CF and the server HTML carries the sightmap loader),
            # then splice it so SightMapAdapter's own discovery fires.
            if not sm_signal:
                try:
                    from urllib.parse import urlparse as _up19

                    from ma_poc.pms.adapters._probe import probe_get as _pg19

                    _fr19 = getattr(ctx, "fetch_result", None)
                    _origin19 = str(getattr(_fr19, "final_url", "") or "") or (
                        getattr(ctx, "base_url", "") or ""
                    )
                    _p19 = _up19(_origin19)
                    if _p19.scheme and _p19.netloc:
                        _home19 = f"{_p19.scheme}://{_p19.netloc}/"
                        _r19 = _pg19(_home19, timeout=25)
                        _h19 = (_r19.text or "") if _r19.status_code == 200 else ""
                        if "sightmap.com" in _h19.lower():
                            if _fr19 is not None:
                                _fr19.body = _h19  # type: ignore[attr-defined]
                            sm_signal = True
                            fallback_chain.append("entrata:home_proxied_for_sightmap")
                except Exception as _hp_exc:  # pragma: no cover - defensive
                    log.warning(
                        "Entrata homepage proxied-probe failed for %s: %s",
                        property_id,
                        _hp_exc,
                    )

            if not sm_signal:
                import re as _re

                _ENTRATA_FP_SUBPATH = _re.compile(
                    r"""["']((?:https?://[^"'/]+)?/[a-z0-9-]+/[a-z0-9-]+/"""
                    r"""(?:conventional|student|senior|affordable)/?)["']""",
                    _re.IGNORECASE,
                )
                m = _ENTRATA_FP_SUBPATH.search(page_html)
                if not m:
                    # iter-8: same fix class as iter-7 securecafe — the
                    # rendered body often lacks the /conventional/ nav link
                    # (patchright DOM vs raw HTML / behind a menu), but the
                    # scraper's network log captured the floorplan sub-page
                    # request. Scan captured response URLs as a 2nd source.
                    for _resp in getattr(ctx, "_api_responses", []) or []:
                        _u = str(_resp.get("url", "") or "")
                        _mm = _ENTRATA_FP_SUBPATH.search(f'"{_u}"')
                        if _mm:
                            m = _mm
                            break
                if m:
                    sub = m.group(1)
                    if sub.startswith("/"):
                        from urllib.parse import urlparse as _up

                        _fr = getattr(ctx, "fetch_result", None)
                        _base = str(getattr(_fr, "final_url", "") or "") or (
                            getattr(ctx, "base_url", "") or ""
                        )
                        _p = _up(_base)
                        if _p.scheme and _p.netloc:
                            sub = f"{_p.scheme}://{_p.netloc}{sub}"
                    try:
                        from ma_poc.pms.adapters._probe import probe_get as _pg

                        _r = _pg(sub, timeout=25)
                        if _r.status_code == 200 and "sightmap.com" in (_r.text or "").lower():
                            # Splice sub-page HTML in so SightMapAdapter's
                            # _entry_html_from_ctx picks up the embed code.
                            # 2026-05-24: FetchResult is a frozen dataclass —
                            # direct ``_fr2.body = ...`` raises
                            # ``cannot assign to field 'body'``. Use
                            # ``dataclasses.replace`` to mint a new immutable
                            # record and swap it onto the (mutable) ctx. Also
                            # encode str→bytes because the contract types
                            # ``body`` as ``bytes | None`` and downstream
                            # ``_entry_html_from_ctx`` decodes either way.
                            _fr2 = getattr(ctx, "fetch_result", None)
                            if _fr2 is not None:
                                import dataclasses as _dc

                                _new_body = (
                                    _r.text.encode("utf-8", "replace")
                                    if isinstance(_r.text, str)
                                    else (_r.text or b"")
                                )
                                ctx.fetch_result = _dc.replace(
                                    _fr2, body=_new_body
                                )
                            sm_signal = True
                            fallback_chain.append("entrata:fp_subpage_fetched")
                    except Exception as _sub_exc:  # pragma: no cover
                        log.warning(
                            "Entrata fp-subpage fetch failed for %s: %s",
                            property_id,
                            _sub_exc,
                        )

            if sm_signal:
                sm_result = await SightMapAdapter().extract(page, ctx)  # type: ignore[arg-type]
                if sm_result.units:
                    adapter_result = sm_result
                    adapter_name = "sightmap"
                    result["_adapter_used"] = "sightmap"
                    fallback_chain.append("sightmap:entrata_secondary")
        except Exception as _sm_exc:  # pragma: no cover - defensive
            log.warning(
                "Entrata→SightMap secondary failed for %s: %s",
                property_id,
                _sm_exc,
            )

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

    # --- Step 8b: Universal embed-recovery as the cross-vendor misroute net ---
    # Closes the "detector picked the wrong primary, generic also returned 0,
    # but the site really has an AppFolio iframe / LeaseLeads embed / ResMan
    # portal / generic SSR plan grid one nav-hop deep" gap. The four
    # recoveries are the same chain wired into wix/squarespace_nopms; here we
    # also fire them when ANY non-syndication primary mis-routed.
    # Idempotent: ``recover_universal_embed`` sets
    # ``ctx._embed_recovery_attempted`` so the syndication adapters' inline
    # run (when this is a wix/squarespace property) isn't repeated.
    if not adapter_result.units and page is not None:
        try:
            from ma_poc.pms.adapters._universal_recovery import (
                already_attempted as _ur_attempted,
            )
            from ma_poc.pms.adapters._universal_recovery import (
                get_blocks as _ur_get_blocks,
            )
            from ma_poc.pms.adapters._universal_recovery import (
                recover_universal_embed as _ur_recover,
            )

            if not _ur_attempted(ctx):
                _ur_units, _ur_tier, _ur_winner = await _ur_recover(page, ctx)
                if _ur_units:
                    from ma_poc.extraction.post_process import (
                        post_process as _ur_pp,
                    )

                    _ur_post = _ur_pp(
                        _ur_units, property_id=getattr(ctx, "property_id", None)
                    )
                    if _ur_post.n_admitted > 0:
                        adapter_result.units = _ur_post.admitted
                        adapter_result.plan_summaries = _ur_post.plan_summaries
                        adapter_result.tier_used = _ur_tier
                        adapter_result.confidence = min(
                            0.92, 0.65 + 0.04 * _ur_post.n_admitted
                        )
                        fallback_chain.append(f"universal_recovery:{_ur_winner}")

            # Bot-block telemetry: when a recovery sub-fetch hit a wall
            # (401/403/429/503), record it on the fallback chain so DLQ/
            # triage can distinguish "routing-correct but bot-walled,
            # worth a proxy/Camoufox retry" from "no signal anywhere".
            # Emitted regardless of whether the chain ultimately recovered
            # units (an AppFolio block on a property that later resolved
            # via generic_dom is still useful signal).
            _ur_blocks = _ur_get_blocks(ctx)
            if _ur_blocks:
                # Deduplicate by (recovery, status) — one entry per
                # unique block kind is enough for triage.
                _seen: set[tuple[str, int]] = set()
                for _b in _ur_blocks:
                    _rec = str(_b.get("recovery") or "")
                    _st = int(_b.get("status") or 0)
                    if not _rec or not _st or (_rec, _st) in _seen:
                        continue
                    _seen.add((_rec, _st))
                    fallback_chain.append(
                        f"universal_recovery_blocked:{_rec}:{_st}"
                    )
        except Exception as exc:
            adapter_result.errors.append(f"universal-recovery-error: {exc}")

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

    # Leasing-portal pointers discovered in embedded JSON blobs (Jonah
    # widget configs, headless WordPress marketing shells, etc. that
    # delegate unit data to SightMap / RealPage OLL / RentCafe / etc.).
    # Forwarded to link-hop, which fetches each URL and lets the host's
    # fingerprint route it to the matching PMS adapter.
    portal_hints = getattr(adapter_result, "_embedded_portal_hints", None)
    if portal_hints:
        result["_embedded_portal_hints"] = list(portal_hints)

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

    # 2026-05-23: propagate the operator-no-availability flag the
    # generic adapter sets when the page carries an explicit "no units
    # available" statement (krcapartments cohort). The runner reads
    # ``result["_operator_no_availability"]`` and passes it to
    # ``compute_verdict`` so the property is classified
    # SUCCESS_NO_AVAILABILITY instead of FAILED_NO_DATA.
    if getattr(adapter_result, "_operator_no_availability", False):
        result["_operator_no_availability"] = True

    reset_clearance_cookies(_clr_token)
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

# RC5: maps FetchOutcome values to the verdict prefix written into errors[].
# Module-level so tests can import rather than redefine.
_OUTCOME_VERDICT_PREFIX: dict[str, str] = {
    "EMPTY_BODY": "FAILED_FETCH_EMPTY",
    "DEAD_URL":   "FAILED_DEAD_URL",
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
    for path in template_paths:
        prior_url = urllib.parse.urljoin(entry_url, path)
        # urljoin returns entry_url itself when path is "" or "/", and
        # may produce malformed output when entry_url is unparseable.
        # Filter both — we only emit strictly-distinct sub-page URLs.
        if prior_url and prior_url != entry_url:
            out.append((prior_url, _PMS_PRIOR_SCORE, anchor))
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

        # Anchor-link elevation: a link that actually exists on the page and
        # carries floor-plan / availability signals should outrank guessed
        # template priors (PMS_PRIOR=5000, UNIVERSAL_PRIOR=4500) because the
        # page author placed it there intentionally. Two tiers:
        #
        #   Strong anchor only (anchor_score > 50): e.g. "Floor Plans" anchor
        #   on a page that uses a non-standard path. Lift above PMS_PRIOR so
        #   it's tried before template guesses like /floorplans.aspx.
        #   → floor = PMS_PRIOR_SCORE + 100 (= 5_100)
        #
        #   Anchor + path both signal intent: doubly-confirmed, highest
        #   confidence for a page-discovered link.
        #   → floor = PMS_PRIOR_SCORE + 600 (= 5_600)
        _anchor_score = sum(w for kw, w in _LINK_ANCHOR_KEYWORDS if kw in anchor)
        _path_score = sum(w for kw, w in _LINK_PATH_KEYWORDS if kw in link_path)
        if _anchor_score > 0 and _path_score > 0:
            # Both anchor text and path keyword signal intent — highest confidence.
            score = max(score, _PMS_PRIOR_SCORE + 600)
        elif _anchor_score > 50:
            # Strong anchor text alone (e.g. "floor plans", "availability") —
            # outrank template priors since the link is real, not a guess.
            score = max(score, _PMS_PRIOR_SCORE + 100)

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
    embedded_portal_hints: list[tuple[str, str]] | None = None,
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
            from services.profile_updater import _is_infra_api_url as _infra_check
        except Exception:
            _infra_check = None  # type: ignore[assignment]
        try:
            nav = profile.navigation
            wpu = getattr(nav, "winning_page_url", None)
            # Guard: never inject infra/media API endpoints as hop candidates.
            # These were saved before _is_infra_api_url guarded the persist
            # path, so they may still exist in older profiles. Checking here
            # prevents wasting hop #1 on an endpoint that hard-fails (HTTP
            # 400/401) before the actual floor-plans page is ever reached.
            _wpu_is_infra = bool(_infra_check and isinstance(wpu, str) and _infra_check(wpu))
            if isinstance(wpu, str) and wpu and wpu not in visited and not _wpu_is_infra:
                # Highest possible score so it always lands first.
                profile_top.append((wpu, _LLM_HINT_SCORE + 1, "profile:winning_page_url"))
            for link in getattr(nav, "availability_links", []) or []:
                if isinstance(link, str) and link and link not in visited:
                    profile_top.append((link, _LLM_HINT_SCORE, "profile:availability_link"))
            for dead in getattr(nav, "explored_links", []) or []:
                if isinstance(dead, str) and dead:
                    explored_skip.add(dead)
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
    pms_priors = _pms_priors_for(detected, entry_url)

    # Leasing-portal pointers from embedded JSON (Jonah Digital widget
    # config etc. point at SightMap / RealPage OLL). High-confidence —
    # an extractor told us this is where units live — so score above
    # PMS template priors.
    portal_candidates: list[tuple[str, int, str]] = []
    if embedded_portal_hints:
        for hint in embedded_portal_hints:
            try:
                url_s, portal_name = hint
            except Exception:
                continue
            url_s = str(url_s or "").strip()
            if not url_s or url_s in visited:
                continue
            # 2026-05-13 (C5 OneSite, teammate analysis): when the embedded
            # portal is RealPage Online Leasing (onlineleasing.realpage.com),
            # individual unit application URLs (``?UnitId=N`` / ``?MoveInDate=``)
            # are application FORM shells — 59KB body, 1.5KB text, 0 unit
            # signals — not the floor-plan grid. Hopping them burns LLM
            # budget on dozens of empty shells (PID 264372 ellisonpreserve
            # had 81 such hops, $0.016 wasted). Strip the query params so the
            # candidate becomes the property-level URL ``{id}.onlineleasing.
            # realpage.com/`` which DOES carry the floor-plan grid.
            if "onlineleasing.realpage.com" in url_s.lower():
                _q_pos = url_s.find("?")
                if _q_pos > 0:
                    _stripped = url_s[:_q_pos]
                    if _stripped not in visited:
                        url_s = _stripped
                    else:
                        # Property-level URL already queued / visited;
                        # skip this per-unit application shell entirely.
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

    while queue_idx < len(queue):
        sub_url, score, anchor = queue[queue_idx]
        idx = queue_idx + 1
        queue_idx += 1

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
            # Record this URL as blocked/failed in the session so adapter-level
            # hint synthesisers (securecafe_portal_detected, etc.) don't re-queue
            # it on subsequent hop pages. Works for BOT_BLOCKED, HARD_FAIL, and
            # TRANSIENT — any outcome that is not "OK" is not worth retrying
            # within the same scrape session.
            if shared_budget is not None:
                _blocked_set: set[str] = shared_budget.setdefault("_session_blocked_urls", set())
                _blocked_set.add(sub_url)
            # Signal back to profile_updater that profile:winning_page_url
            # hard-failed so it can be cleared and the profile reset to COLD.
            if anchor == "profile:winning_page_url" and shared_budget is not None:
                shared_budget["_winning_page_url_hop_outcome"] = "profile:winning_page_url:failed"
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
            explored[sub_url] = False
            log.debug(
                "link-hop %s: silent redirect to homepage (%s) — skipping extraction",
                sub_url, _sub_final_url,
            )
            continue

        # Property sub-path priors: when the hop URL has 3+ path segments
        # (property-specific, not just a top-level prior guess), dynamically
        # add /floorplans and /floor-plans relative to that URL. This catches
        # sites like amli.com where the PMS prior is appended to the base
        # domain instead of the property's own deep URL.
        try:
            _parsed_sub = urllib.parse.urlparse(sub_url)
            _hop_path_parts = [p for p in _parsed_sub.path.split("/") if p]
            # 2026-05-13 (C6 AppFolio login bug, teammate analysis): strip
            # the query + fragment before constructing the subpath URL.
            # Without this strip, hop URLs like
            #   dlandgroup.appfolio.com/oportal/users/log_in?UnitId=42
            # become
            #   dlandgroup.appfolio.com/oportal/users/log_in?UnitId=42/floorplans
            # — nonsense that returns the login form for every hop.
            # Also skip subpath construction when the path itself is clearly
            # an auth/login endpoint (oportal/users/log_in, /sign_in, etc.).
            _sub_path_lower = _parsed_sub.path.lower()
            _LOGIN_PATH_SIGNATURES = (
                "/log_in", "/login", "/sign_in", "/signin", "/auth/", "/oauth/",
                "/users/log_in", "/users/sign_in",
            )
            _is_login_path = any(sig in _sub_path_lower for sig in _LOGIN_PATH_SIGNATURES)
            _sub_url_no_query = urllib.parse.urlunparse(
                (_parsed_sub.scheme, _parsed_sub.netloc, _parsed_sub.path, "", "", "")
            )
            if (
                len(_hop_path_parts) >= 3
                and dynamic_appended < max_dynamic_appends
                and not _is_login_path
            ):
                # 2026-05-20: extended set of property-level sub-paths the
                # dynamic appender queues when link-hop is already at a
                # 3+-segment URL (i.e. a brand-CMS property page like
                # ``/apartments/ca/san-jose/villas-willow-glen``). The
                # original 4-path list missed ``/availability`` and its
                # variants, which is where the actual unit roster lives
                # on many sites that show only a price-range slider or
                # filter UI on the floor-plans page (IMT, TGM, others
                # observed in the 2026-05-20 random-30 sample). The
                # universal-priors list already includes /availability
                # for level-1 hops; this completes the cascade so when
                # we're already on /floor-plans, we discover deeper
                # /availability + /our-apartments paths too.
                _prop_sub_paths = (
                    "/floorplans",
                    "/floor-plans",
                    "/pricing",
                    "/apartments-pricing",
                    "/availability",
                    "/view-availability",
                    "/our-apartments",
                    "/apartments",
                    "/units",
                    "/leasing",
                )
                for _psp in _prop_sub_paths:
                    _psp_url = _sub_url_no_query.rstrip("/") + _psp
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

            # Track the page that has delivered the most units so the caller
            # can promote it to winning_page_url if it beats the current record.
            unit_count = len(sub_result.get("units") or [])
            if unit_count > _best_units_page[1]:
                _best_units_page = (sub_url, unit_count)

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
                                # 2026-05-13: also accept the per-plan detail
                                # URL conventions that show up on card-based
                                # marketing sites:
                                #   /apartment/<slug>   (liveatsurf pattern)
                                #   /home/<slug>        (some Greystar sites)
                                #   /property/<slug>    (PMC portfolio detail
                                #                       pages — princeton mgmt)
                                # These were excluded by the previous
                                # short-list, so floor-plan accumulation never
                                # walked into per-card detail pages even when
                                # the resolver landed on the index page.
                                _path_kw_match = any(
                                    kw in sub_path_l
                                    for kw, _ in _LINK_PATH_KEYWORDS
                                    if kw in ("/floorplan", "/floor-plan",
                                              "/availability", "/units",
                                              "/conventional", "/apartments",
                                              "/apartment/", "/home/",
                                              "/property/", "/communities/")
                                )
                                if not _path_kw_match:
                                    continue
                        fp_hints.append((lnk_url, "html_subpage"))
            if fp_hints and not _in_floorplan_accumulation:
                # Mark accumulation mode so recursive sub-pages are merged,
                # not treated as new floor-plan index pages.
                _in_floorplan_accumulation = True
                _first_successful_result = sub_result
                _accumulated_units.extend(sub_result.get("units") or [])
                # Checkpoint partial results so the timeout handler can
                # salvage accumulated units if the property wall-clock budget
                # expires mid-hop. Write to both shared_budget (in-process
                # visibility) and _external_partial_ref (survives coroutine
                # cancellation — the dict lives in _process_one's scope).
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

            elif _in_floorplan_accumulation:
                # Accumulating sub-page units — merge into the running total.
                _accumulated_units.extend(sub_result.get("units") or [])
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
            # 2026-05-20 cluster #4 soft-404 recovery: some marketing
            # sites return HTTP 404 for valid property URLs (e.g.
            # liveatcrossroadsranch.com/home: 404 with 59 KB of real
            # content + nav-link to /apartments/.../floor-plans where
            # the SightMap embed lives). Main's prod extracts these
            # via TIER_1_API_SIGHTMAP after link-hop. Short-circuiting
            # on DEAD_URL drops the property entirely.
            # Conservative recovery: when outcome=DEAD_URL AND the body
            # has substantive apartment-shaped content (>=10 KB AND
            # >=1 inventory nav link), skip the short-circuit and let
            # the rest of the pipeline (link-hop, generic adapter, F2
            # LLM rescue) try. Genuine 404s have empty/minimal bodies
            # and don't trip this gate.
            _soft_404_recovery = False
            if outcome_val == "DEAD_URL":
                _fr_body = getattr(fetch_result, "body", None) or b""
                _body_size = len(_fr_body) if isinstance(_fr_body, (bytes, str)) else 0
                if _body_size >= 10_000:
                    try:
                        _body_str = (
                            _fr_body.decode("utf-8", errors="replace")
                            if isinstance(_fr_body, bytes)
                            else _fr_body
                        ).lower()
                        # Nav-link markers — any one is enough.
                        _SOFT_404_MARKERS = (
                            "/floor-plans",
                            "/floorplans",
                            "/availability",
                            "/available-units",
                            "/availableunits",
                            "/apartments/",
                            "sightmap.com/embed/",
                            "rentcafe.com",
                            "knockdoorway",
                        )
                        if any(m in _body_str for m in _SOFT_404_MARKERS):
                            _soft_404_recovery = True
                    except Exception:  # pragma: no cover — defensive
                        pass

            if _soft_404_recovery:
                # Fall through to extraction. Record the recovery on the
                # result dict so reports can distinguish "soft-404 recovered"
                # from "genuine 200 OK".
                result["_soft_404_recovery"] = True
                result["_soft_404_status"] = getattr(fetch_result, "status_code", None)
                # Don't short-circuit; continue past this block.
            else:
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

    # 2026-05-13 (C1 SGCaptcha wall, teammate analysis): when the fetch
    # outcome is technically OK but the page redirected to
    # ``/.well-known/sgcaptcha/`` (the SGCaptcha interstitial), the body
    # is a ~12KB challenge page with 0 floor-plan signals. Running the full
    # tier cascade against it wastes ~25 seconds per property and trips the
    # LLM tiers against 200-byte HTML shells. Estimated 85+ properties hit
    # this daily. Emit a dedicated tier code so reports distinguish the
    # captcha wall from a genuine extraction failure.
    _sgcaptcha_walled = False
    _final_url = ""
    if fetch_result is not None:
        _final_url = str(getattr(fetch_result, "final_url", "") or "")
        if "/.well-known/sgcaptcha/" in _final_url.lower():
            _sgcaptcha_walled = True
    if _sgcaptcha_walled:
        result = _empty_result(base_url)
        result["_property_id"] = property_id
        result["extraction_tier_used"] = "generic:sgcaptcha_wall"
        result["errors"].append(
            f"SGCAPTCHA_WALL: final_url={_final_url[:120]} "
            "(SGCaptcha interstitial — full tier cascade skipped)"
        )
        try:
            fd = fetch_result.to_dict() if hasattr(fetch_result, "to_dict") else {}
            body = getattr(fetch_result, "body", None)
            fd["body_bytes"] = len(body) if body else 0
            fd["captcha_detected"] = True
            fd["captcha_provider"] = "sgcaptcha"
            result["_fetch_diagnostic"] = fd
        except Exception:
            pass
        result["_extract_result"] = ExtractResult(
            property_id=property_id,
            records=[],
            tier_used="generic:sgcaptcha_wall",
            adapter_name="none",
            winning_url=None,
            confidence=0.0,
            errors=[f"sgcaptcha_wall: final_url={_final_url[:80]}"],
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
                    max_hops=7,
                    llm_navigation_hints=result.get("_llm_navigation_hints"),
                    embedded_portal_hints=result.get("_embedded_portal_hints"),
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
