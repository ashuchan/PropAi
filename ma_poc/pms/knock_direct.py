"""Direct-GET shortcut for WARM Knock profiles (task #21 — the warm=fast lever).

Knock's doorway-api (``doorway-api.knockrentals.com/v1/property/{id}/units``) is
a PUBLIC GET — no auth, no Cloudflare, no cookies. Proven 2026-07-24: 6/6 warm
Knock profiles returned the full gold roster (unit#, rent, beds, sqft,
availability) from a plain GET, byte-for-byte matching the render's
``TIER_1_KNOCK_API`` extraction (Mosby 73 units both ways).

The render is only ever needed ONCE (COLD) to *discover* the internal
property-id baked into the endpoint URL — it lives in the page JS, not the CSV.
Once WARM the profile stores that endpoint in ``known_endpoints`` (the id is
stable → direct replay is valid on every subsequent run), so we can skip the
~10-45s render and just GET the endpoint (<1s). The JSON is handed to the
existing ``TIER_1_KNOCK_API`` parser via ``FetchResult.network_log`` (which
``scrape_jugnu`` promotes into ``ctx._api_responses``) — no parser reinvention.

Flag-gated (``ENABLE_KNOCK_DIRECT_GET``, default OFF — repo convention for new
fetch routes). Never-raise: any failure (non-200, empty body, unparseable, no
units) returns ``None`` so the caller falls through to the normal render path.
WARM/HOT only — a COLD profile's endpoint is unproven and may be stale/wrong.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_KNOCK_HOST = "doorway-api.knockrentals.com"


def _endpoint_url(e: Any) -> str:
    """Pull the URL string off a known_endpoint entry (pydantic model or dict)."""
    if isinstance(e, dict):
        return e.get("url_pattern") or e.get("url") or ""
    return getattr(e, "url_pattern", None) or getattr(e, "url", None) or ""


def knock_units_endpoint(profile: Any) -> str | None:
    """Return the stored doorway-api ``/units`` endpoint for *profile*, or None.

    None when the profile is missing, is not a Knock profile, or has no
    ``/units`` endpoint stored. This is the dispatch trigger — Knock profiles
    carry ``api_provider='unknown'``, so the endpoint (not a provider tag) is
    the reliable signal. Never raises.
    """
    if profile is None:
        return None
    try:
        eps = getattr(profile.api_hints, "known_endpoints", None) or []
    except Exception:
        return None
    for e in eps:
        url = _endpoint_url(e)
        if url and _KNOCK_HOST in url and "/units" in url:
            return url
    return None


def _is_warm(profile: Any) -> bool:
    """True iff the profile is WARM or HOT (a COLD endpoint is unproven)."""
    try:
        return str(getattr(profile.confidence, "maturity", "") or "").upper() in (
            "WARM",
            "HOT",
        )
    except Exception:
        return False


async def try_knock_direct(
    task: Any, profile: Any, csv_row: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Direct-GET a WARM Knock profile's stored ``/units`` endpoint.

    Returns ``{"fetch_result": FetchResult, "result": <scrape-result dict>}`` on
    success — a COMPLETE, pre-extracted result the caller uses in place of the
    render + ``scrape_jugnu`` — or ``None`` to fall through to the normal render
    path.

    The roster is parsed with the CANONICAL ``pms.adapters.knock.parse_knock_units``
    (the same function the ``TIER_1_KNOCK_API`` render path uses), so the
    unit_ids are the real Knock unit numbers (``name`` → ``315``, ``207``…) —
    NOT the synthetic ``inferred_*`` hashes the generic cascade assigns. Routing
    the raw JSON through ``scrape_jugnu`` instead would misroute to the generic
    adapter (detection needs HTML fingerprints to pick the Knock adapter), so we
    parse here and hand back a finished result.

    Fires only when ``ENABLE_KNOCK_DIRECT_GET`` is on AND the profile is
    WARM/HOT AND a doorway-api ``/units`` endpoint is stored AND the parse yields
    a non-empty roster. Never raises.
    """
    from ma_poc.config.feature_flags import ENABLE_KNOCK_DIRECT_GET

    if not ENABLE_KNOCK_DIRECT_GET or not _is_warm(profile):
        return None
    endpoint = knock_units_endpoint(profile)
    if not endpoint:
        return None

    pid = getattr(task, "property_id", "?")
    from ma_poc.services.profile_route_quarantine import route_is_quarantined

    if route_is_quarantined(pid, endpoint):
        log.warning("knock_direct %s: confirmed contaminated route quarantined", pid)
        return None
    endpoint_match = re.search(r"/v1/property/(\d+)/units(?:[/?#]|$)", endpoint)
    if not endpoint_match:
        log.warning("knock_direct %s: malformed units endpoint — route rejected", pid)
        return None
    metadata_url = endpoint[: endpoint_match.start()] + f"/v1/property/{endpoint_match.group(1)}"
    try:
        from ma_poc.pms.adapters._probe import probe_get

        # unlocker=False: Knock's API is a clean public GET; never spend the
        # Web-Unlocker cap on it (and it's compliance-forced-off anyway). The
        # sync curl_cffi GET runs off-loop so it never stalls the AsyncPool.
        # retries=2 (2026-07-31, fetch reliability): the only fallback today is
        # a full ~10-45s render, so a transient soft-403 forces the render this
        # fast path exists to skip. A bounded compliance-safe plain re-GET
        # (unlocker stays False under COMPLIANCE_MODE) recovers it far cheaper.
        meta_r = await asyncio.to_thread(
            probe_get, metadata_url, unlocker=False, retries=2, timeout=20
        )
        r = await asyncio.to_thread(
            probe_get, endpoint, unlocker=False, retries=2, timeout=20
        )
    except Exception as exc:
        log.debug("knock_direct GET raised for %s: %s", pid, exc)
        return None

    meta_status = int(getattr(meta_r, "status_code", 0) or 0)
    meta_body = getattr(meta_r, "text", "") or ""
    status = int(getattr(r, "status_code", 0) or 0)
    body = getattr(r, "text", "") or ""
    if status != 200 or not body:
        log.debug("knock_direct %s: non-200/empty (status=%s) — fall through", pid, status)
        return None

    # Parse with the canonical Knock parser → real unit numbers + rent. A 200
    # with an empty/renamed shape yields [] and falls through to the render
    # (which re-learns), so a drifted endpoint never silently degrades quality.
    try:
        metadata_payload = json.loads(meta_body) if meta_status == 200 and meta_body else {}
        from ma_poc.pms.property_identity import (
            MATCH,
            configured_identity_from_csv,
            evaluate_observed_from_csv,
            identity_is_configured,
            knock_observed_identity,
        )

        observed_identity = knock_observed_identity(metadata_payload)
        identity = evaluate_observed_from_csv(csv_row, observed_identity)
        if identity_is_configured(configured_identity_from_csv(csv_row)) and identity.status != MATCH:
            log.warning(
                "knock_direct %s: property identity %s (%s) — route rejected",
                pid,
                identity.status,
                ",".join(identity.evidence),
            )
            return None
        payload = json.loads(body)
        from ma_poc.pms.adapters.knock import parse_knock_units

        units = parse_knock_units(payload)
    except Exception as exc:
        log.debug("knock_direct %s: parse failed (%s) — fall through", pid, exc)
        return None
    if not units:
        log.debug("knock_direct %s: 200 but parser yielded 0 units — fall through", pid)
        return None

    base_url = getattr(task, "url", endpoint)
    from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
    from ma_poc.models.fetch_tier import FetchTier
    from ma_poc.pms.contracts import ExtractResult
    from ma_poc.pms.scraper import _empty_result

    # Complete scrape-result dict — same shape scrape_jugnu returns, so the
    # caller's post-processing (verdict, v2 format, state, profile update) works
    # unchanged. Tier is labelled *_DIRECT so run reports can distinguish the
    # render-skipped path from the rendered TIER_1_KNOCK_API.
    result = _empty_result(base_url)
    result["units"] = units
    result["extraction_tier_used"] = "TIER_1_KNOCK_API_DIRECT"
    result["_property_id"] = pid
    result["_adapter_used"] = "knock"
    result["api_calls_intercepted"] = [endpoint]
    from ma_poc.pms.source_provenance import build_unit_source_provenance

    result["_unit_source_provenance"] = [
        build_unit_source_provenance(
            provider="knock",
            source_url=endpoint,
            body=payload,
            unit_count=len(units),
            identity=identity,
        )
    ]
    result["_extract_result"] = ExtractResult(
        property_id=str(pid),
        records=units,
        tier_used="TIER_1_KNOCK_API_DIRECT",
        adapter_name="knock",
        winning_url=endpoint,
        confidence=0.9,
    )

    # Synthetic OK/GET FetchResult so the caller's fetch_result.ok() / verdict /
    # carry-forward logic behaves as if a successful static fetch happened.
    fr = FetchResult(
        url=endpoint,
        outcome=FetchOutcome.OK,
        status=200,
        body=body.encode("utf-8"),
        headers={"content-type": "application/json"},
        render_mode=RenderMode.GET,
        final_url=endpoint,
        attempts=1,
        elapsed_ms=int(getattr(r, "elapsed_ms", 0) or 0),
        network_log=[
            {
                "url": metadata_url,
                "status": meta_status,
                "content_type": "application/json",
                "body": meta_body,
                "captcha_detected": False,
            },
            {
                "url": endpoint,
                "status": 200,
                "content_type": "application/json",
                "body": body,
                "captcha_detected": False,
            }
        ],
        fetch_tier_used=int(FetchTier.DIRECT),
    )
    log.info(
        "knock_direct: %s served %d units (%s) via direct GET — render skipped (%s)",
        pid,
        len(units),
        units[0].get("unit_number", "?") if units else "?",
        endpoint,
    )
    return {"fetch_result": fr, "result": result}
