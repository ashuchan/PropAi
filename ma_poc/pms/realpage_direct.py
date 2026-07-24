"""Direct raw-GET shortcut for WARM RealPage CWS / LeaseStar profiles (task #21).

``api.ws.realpage.com`` floorplans+units is a RENDER-FREE API: it's auth-gated by
``x-ws-authkey``, but that key is a PUBLIC per-property widget credential sitting
in the property page's STATIC HTML (``RPFP_config``). So for a WARM CWS profile we
fetch the property page via ``hb_raw_get`` (HB in-page fetch → raw HTML, any CF
cleared by HB's residential proxy, no BrightData), then reuse the EXISTING
``generic._probe_realpage_cws`` — it regexes ``propertyId``+``apiKey`` from the
HTML and GETs ``/units`` with the key (plain httpx; ``api.ws.realpage.com`` is
Akamai auth-gated, NOT CF-IP-blocked, so it works from GCP with no proxy). This
skips the ~10-45s render; today that probe only runs as a generic sub-tier AFTER
a render, so the shortcut simply promotes it pre-render.

EXCLUDES OLL (``leasing.realpage.com`` / ``onlineleasing.realpage.com``) — that
surface is DataDome + a stateful browser-minted token, render-required. The
trigger is specifically an ``api.ws.realpage.com`` endpoint.

Flag ``ENABLE_REALPAGE_DIRECT_GET`` (default OFF). Never-raise. WARM/HOT only.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_CWS_HOST = "api.ws.realpage.com"


def _endpoint_url(e: Any) -> str:
    if isinstance(e, dict):
        return e.get("api_url_pattern") or e.get("url_pattern") or e.get("url") or ""
    return (
        getattr(e, "api_url_pattern", None)
        or getattr(e, "url_pattern", None)
        or getattr(e, "url", None)
        or ""
    )


def is_realpage_cws(profile: Any) -> bool:
    """True iff the profile carries an ``api.ws.realpage.com`` endpoint (CWS /
    LeaseStar). Excludes OLL (leasing/onlineleasing.realpage.com), which needs a
    render. Never raises.
    """
    if profile is None:
        return False
    urls: list[str] = []
    try:
        api = profile.api_hints
        for coll in (
            getattr(api, "known_endpoints", None) or [],
            getattr(api, "llm_field_mappings", None) or [],
        ):
            urls += [_endpoint_url(e) for e in coll]
    except Exception:
        return False
    try:
        urls.append(getattr(profile.navigation, "winning_page_url", None) or "")
    except Exception:
        pass
    return any(_CWS_HOST in (u or "").lower() for u in urls)


def _property_page_url(profile: Any, task: Any) -> str:
    """The page that carries RPFP_config — the property's marketing entry_url."""
    try:
        entry = getattr(profile.navigation, "entry_url", None) or ""
    except Exception:
        entry = ""
    return entry or getattr(task, "url", "") or ""


def _is_warm(profile: Any) -> bool:
    try:
        return str(getattr(profile.confidence, "maturity", "") or "").upper() in (
            "WARM",
            "HOT",
        )
    except Exception:
        return False


async def try_realpage_direct(
    task: Any, profile: Any, csv_row: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Fetch a WARM CWS profile's property page via HB, harvest the public
    apiKey, and GET the units API — all render-free.

    Returns ``{"fetch_result": FetchResult, "result": <scrape-result dict>}`` on
    success, or ``None`` to fall through to the render. Never raises.
    """
    from ma_poc.config.feature_flags import ENABLE_REALPAGE_DIRECT_GET

    if not ENABLE_REALPAGE_DIRECT_GET or not _is_warm(profile):
        return None
    if not is_realpage_cws(profile):
        return None
    page_url = _property_page_url(profile, task)
    if not page_url:
        return None

    pid = getattr(task, "property_id", "?")
    from ma_poc.fetch.hyperbrowser_backend import hb_raw_get

    status, html = await hb_raw_get(page_url, pid)
    if status != 200 or not html:
        log.debug("realpage_direct %s: property page fetch non-200/empty (%s) — fall through", pid, status)
        return None

    # Reuse the existing CWS probe: regex propertyId+apiKey from RPFP_config, GET
    # /units with x-ws-authkey (plain httpx, no proxy). Returns [] on any miss.
    try:
        from ma_poc.pms.adapters.generic import _probe_realpage_cws

        units = await _probe_realpage_cws(html)
    except Exception as exc:
        log.debug("realpage_direct %s: _probe_realpage_cws failed (%s) — fall through", pid, exc)
        return None
    if not units:
        log.debug("realpage_direct %s: no RPFP_config/units on property page — fall through", pid)
        return None

    base_url = getattr(task, "url", page_url)
    from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
    from ma_poc.models.fetch_tier import FetchTier
    from ma_poc.pms.contracts import ExtractResult
    from ma_poc.pms.scraper import _empty_result

    result = _empty_result(base_url)
    result["units"] = units
    result["extraction_tier_used"] = "TIER_1_REALPAGE_CWS_DIRECT"
    result["_property_id"] = pid
    result["_adapter_used"] = "realpage"
    result["api_calls_intercepted"] = [page_url]
    result["_extract_result"] = ExtractResult(
        property_id=str(pid),
        records=units,
        tier_used="TIER_1_REALPAGE_CWS_DIRECT",
        adapter_name="realpage",
        winning_url=page_url,
        confidence=0.9,
    )

    fr = FetchResult(
        url=page_url,
        outcome=FetchOutcome.OK,
        status=200,
        body=html.encode("utf-8"),
        headers={"content-type": "text/html"},
        render_mode=RenderMode.GET,
        final_url=page_url,
        attempts=1,
        elapsed_ms=0,
        network_log=[],
        fetch_tier_used=int(FetchTier.HYPERBROWSER),
    )
    log.info(
        "realpage_direct: %s served %d units (%s) via HB page + CWS API — render skipped (%s)",
        pid,
        len(units),
        units[0].get("unit_number", "?") if units else "?",
        page_url,
    )
    return {"fetch_result": fr, "result": result}
