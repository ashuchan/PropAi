"""Direct raw-GET shortcut for WARM SightMap profiles (task #21, warm=fast).

SightMap's unit API (``sightmap.com/app/api/v1/{client_key}/sightmaps/{id}``)
returns the full gold roster (unit#, rent, beds, sqft, availability) as JSON.
It is CF-fronted (blocks datacenter/GCP IPs) and content-negotiates (a browser
navigation returns the HTML embed, not JSON), so we fetch it via ``hb_raw_get``
(HB in-page same-origin fetch): HB's residential proxy clears CF and the in-page
``fetch(Accept: json)`` returns the RAW JSON, free — no BrightData. Once WARM the
API URL (with its per-property ``client_key``+``sightmap_id``, discovered COLD
from the embed) is stored, so we skip the ~10-45s render and GET it directly.

**No tier field discriminates a SightMap-winner from an Entrata-winner that only
carries SightMap as a secondary null-price hint** (both look identical in the
profile). So instead of a tier gate we use a CONTENT GUARD: parse, then require
≥1 rent-bearing unit. Entrata-hint props (e.g. 1759 — 197 null-price units) fail
the guard → fall through to the render, which gets their real (Entrata) gold. A
high partial-join drop also falls through (mirrors the adapter's
SIGHTMAP_PARTIAL_JOIN guard).

Flag ``ENABLE_SIGHTMAP_DIRECT_GET`` (default OFF). Never-raise. WARM/HOT only.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

_HOST = "sightmap.com"


def _endpoint_url(e: Any) -> str:
    if isinstance(e, dict):
        return e.get("api_url_pattern") or e.get("url_pattern") or e.get("url") or ""
    return (
        getattr(e, "api_url_pattern", None)
        or getattr(e, "url_pattern", None)
        or getattr(e, "url", None)
        or ""
    )


def _with_scheme(url: str) -> str:
    return url if url.startswith(("http://", "https://")) else "https://" + url


def sightmap_api_url(profile: Any) -> str | None:
    """Return the stored SightMap ``/sightmaps/`` API URL for *profile*, or None.

    Prefers ``api_hints.llm_field_mappings[].api_url_pattern`` (carries the FULL
    URL) over ``known_endpoints`` (sometimes truncated). Never raises.
    """
    if profile is None:
        return None
    try:
        api = profile.api_hints
    except Exception:
        return None
    for coll in (
        getattr(api, "llm_field_mappings", None) or [],
        getattr(api, "known_endpoints", None) or [],
    ):
        for e in coll:
            url = _endpoint_url(e)
            if url and _HOST in url and "/sightmaps/" in url:
                return _with_scheme(url)
    return None


def _is_warm(profile: Any) -> bool:
    try:
        return str(getattr(profile.confidence, "maturity", "") or "").upper() in (
            "WARM",
            "HOT",
        )
    except Exception:
        return False


def _rent_bearing(units: list[dict[str, Any]]) -> int:
    n = 0
    for u in units:
        r = u.get("market_rent_low") or u.get("market_rent_high")
        if isinstance(r, (int, float)) and r > 0:
            n += 1
            continue
        rr = str(u.get("rent_range") or "")
        if any(c.isdigit() for c in rr) and "$" in rr:
            n += 1
    return n


async def try_sightmap_direct(
    task: Any, profile: Any, csv_row: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Raw-GET a WARM SightMap profile's stored API URL via HB in-page fetch.

    Returns ``{"fetch_result": FetchResult, "result": <scrape-result dict>}`` on
    success, or ``None`` to fall through to the render. Never raises.
    """
    from ma_poc.config.feature_flags import ENABLE_SIGHTMAP_DIRECT_GET

    if not ENABLE_SIGHTMAP_DIRECT_GET or not _is_warm(profile):
        return None
    url = sightmap_api_url(profile)
    if not url:
        return None

    pid = getattr(task, "property_id", "?")
    from ma_poc.services.profile_route_quarantine import route_is_quarantined

    if route_is_quarantined(pid, url):
        log.warning("sightmap_direct %s: confirmed contaminated route quarantined", pid)
        return None
    from ma_poc.fetch.hyperbrowser_backend import hb_raw_get

    status, body = await hb_raw_get(url, pid)
    if status != 200 or not body:
        log.debug("sightmap_direct %s: hb_raw_get non-200/empty (%s) — fall through", pid, status)
        return None

    # parse_sightmap_payload needs a DICT (not the raw JSON string).
    try:
        from ma_poc.pms.adapters.sightmap import parse_sightmap_payload

        payload = json.loads(body)
        from ma_poc.pms.property_identity import (
            MATCH,
            configured_identity_from_csv,
            evaluate_observed_from_csv,
            identity_is_configured,
            sightmap_observed_identity,
        )

        observed_identity = sightmap_observed_identity(payload)
        identity = evaluate_observed_from_csv(csv_row, observed_identity)
        # A warm direct replay is detached from the marketing page that first
        # discovered it.  When CSV identity is available it therefore needs
        # positive vendor metadata, not merely a parseable roster.
        if identity_is_configured(configured_identity_from_csv(csv_row)) and identity.status != MATCH:
            log.warning(
                "sightmap_direct %s: property identity %s (%s) — route rejected",
                pid,
                identity.status,
                ",".join(identity.evidence),
            )
            return None

        units, dropped = parse_sightmap_payload(payload, url)
    except Exception as exc:
        log.debug("sightmap_direct %s: parse failed (%s) — fall through", pid, exc)
        return None
    if not units:
        return None

    # Partial-join guard (mirror the adapter's SIGHTMAP_PARTIAL_JOIN): if >20% of
    # raw units couldn't be joined to a floor plan, the payload is unreliable →
    # let the render re-derive.
    total = len(units) + int(dropped or 0)
    if total and (dropped / total) > 0.20:
        log.debug("sightmap_direct %s: partial join %d/%d dropped — fall through", pid, dropped, total)
        return None

    # Content guard: no rent-bearing unit → this is the Entrata-hint / null-price
    # case (SightMap is a secondary signal here). Fall through to the render,
    # which gets the real gold; do NOT ship a null-price plan-level result.
    if _rent_bearing(units) < 1:
        log.debug("sightmap_direct %s: 0 rent-bearing units (null-price/entrata-hint) — fall through", pid)
        return None

    base_url = getattr(task, "url", url)
    from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
    from ma_poc.models.fetch_tier import FetchTier
    from ma_poc.pms.contracts import ExtractResult
    from ma_poc.pms.scraper import _empty_result

    result = _empty_result(base_url)
    result["units"] = units
    result["extraction_tier_used"] = "TIER_1_API_SIGHTMAP_DIRECT"
    result["_property_id"] = pid
    result["_adapter_used"] = "sightmap"
    result["api_calls_intercepted"] = [url]
    from ma_poc.pms.source_provenance import build_unit_source_provenance

    result["_unit_source_provenance"] = [
        build_unit_source_provenance(
            provider="sightmap",
            source_url=url,
            body=payload,
            unit_count=len(units),
            identity=identity,
        )
    ]
    result["_extract_result"] = ExtractResult(
        property_id=str(pid),
        records=units,
        tier_used="TIER_1_API_SIGHTMAP_DIRECT",
        adapter_name="sightmap",
        winning_url=url,
        confidence=0.9,
    )

    fr = FetchResult(
        url=url,
        outcome=FetchOutcome.OK,
        status=200,
        body=body.encode("utf-8"),
        headers={"content-type": "application/json"},
        render_mode=RenderMode.GET,
        final_url=url,
        attempts=1,
        elapsed_ms=0,
        network_log=[
            {
                "url": url,
                "status": 200,
                "content_type": "application/json",
                "body": body,
                "captcha_detected": False,
            }
        ],
        fetch_tier_used=int(FetchTier.HYPERBROWSER),
    )
    log.info(
        "sightmap_direct: %s served %d units (%s) via HB in-page fetch — render skipped (%s)",
        pid,
        len(units),
        units[0].get("unit_number", "?") if units else "?",
        url,
    )
    return {"fetch_result": fr, "result": result}
