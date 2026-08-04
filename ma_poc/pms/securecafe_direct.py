"""Direct raw-GET shortcut for WARM RentCafe / SecureCafe profiles (task #21).

RentCafe's UNIT-LEVEL gold lives on the securecafe onlineleasing
``availableunits.aspx`` page. That page is Cloudflare-fronted (a plain GET 403s)
AND the canonical parser (``parse_securecafe_availableunits``) needs the RAW
server HTML — a browser RENDER mutates the DOM so the parser returns 0. So this
shortcut fetches the stored availableunits URL via ``hb_raw_get`` (HB in-page
same-origin fetch): HB's residential proxy clears CF and returns the RAW HTML,
free — no BrightData (Ankur's standing HB-over-BrightData preference). It parses
to real unit numbers + rent and skips the marketing-page render + link-hop.

Proven 2026-07-24: grantparkvillage → 25 gold units (0411 $1,361-$3,180) via HB,
no BrightData. Flag ``ENABLE_RENTCAFE_DIRECT_GET`` (default OFF). Never-raise:
any fetch/parse miss → ``None`` → fall through to the normal render path.
WARM/HOT only — a COLD winning_url is unproven.

NOTE: the legacy ``pms.rentcafe_direct/`` package is DEAD (never fires — gates on
``api_provider=='rentcafe'`` which no profile carries; wrong host; plan-level
only). THIS module is the working replacement for the unit-level render-skip.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def securecafe_availableunits_url(profile: Any) -> str | None:
    """Return the stored securecafe ``availableunits.aspx`` URL for *profile*,
    or None. The trigger is the winning_page_url being a securecafe available-
    units page — RentCafe profiles carry ``api_provider='unknown'``, so the URL
    (not a provider tag) is the reliable signal. Never raises.
    """
    if profile is None:
        return None
    try:
        url = getattr(profile.navigation, "winning_page_url", None) or ""
    except Exception:
        return None
    low = url.lower()
    if "securecafe.com" in low and "availableunits" in low:
        return url
    return None


def _is_warm(profile: Any) -> bool:
    """True iff the profile is WARM or HOT (a COLD winning_url is unproven)."""
    try:
        return str(getattr(profile.confidence, "maturity", "") or "").upper() in (
            "WARM",
            "HOT",
        )
    except Exception:
        return False


async def try_rentcafe_direct(
    task: Any, profile: Any, csv_row: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Raw-GET a WARM securecafe profile's stored ``availableunits.aspx`` via HB.

    Returns ``{"fetch_result": FetchResult, "result": <scrape-result dict>}`` on
    success — a COMPLETE, canonically-parsed result the caller uses in place of
    the render + ``scrape_jugnu`` — or ``None`` to fall through to the normal
    render path. Never raises.
    """
    from ma_poc.config.feature_flags import ENABLE_RENTCAFE_DIRECT_GET

    if not ENABLE_RENTCAFE_DIRECT_GET or not _is_warm(profile):
        return None
    url = securecafe_availableunits_url(profile)
    if not url:
        return None

    pid = getattr(task, "property_id", "?")
    from ma_poc.fetch.hyperbrowser_backend import hb_raw_get

    status, body = await hb_raw_get(
        url,
        pid,
        priority=True,
        reservation_reason="warm_profile_securecafe_route",
    )
    if status != 200 or not body:
        log.debug("rentcafe_direct %s: hb_raw_get non-200/empty (%s) — fall through", pid, status)
        return None

    # Parse with the canonical securecafe parser → real unit numbers + rent. A
    # 200 that yields 0 units (CF challenge slipped through, layout drift) falls
    # through to the render, which re-learns.
    try:
        from ma_poc.pms.adapters.rentcafe import parse_securecafe_availableunits

        units = parse_securecafe_availableunits(body, url) or []
    except Exception as exc:
        log.debug("rentcafe_direct %s: parse failed (%s) — fall through", pid, exc)
        return None
    if not units:
        log.debug("rentcafe_direct %s: 200 but parser yielded 0 units — fall through", pid)
        return None

    base_url = getattr(task, "url", url)
    from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
    from ma_poc.models.fetch_tier import FetchTier
    from ma_poc.pms.contracts import ExtractResult
    from ma_poc.pms.scraper import _empty_result

    result = _empty_result(base_url)
    result["units"] = units
    result["extraction_tier_used"] = "TIER_1_RENTCAFE_SECURECAFE_DIRECT"
    result["_property_id"] = pid
    result["_adapter_used"] = "rentcafe"
    result["api_calls_intercepted"] = [url]
    result["_extract_result"] = ExtractResult(
        property_id=str(pid),
        records=units,
        tier_used="TIER_1_RENTCAFE_SECURECAFE_DIRECT",
        adapter_name="rentcafe",
        winning_url=url,
        confidence=0.9,
    )

    # Direct shortcuts bypass ``scrape_jugnu`` and therefore its adapter-level
    # collection boundary. Apply the same fail-closed rule here, then apply it
    # once more at the runner's common final boundary as defence in depth.
    from ma_poc.pms.property_scope import apply_collection_scope_to_result

    apply_collection_scope_to_result(result, property_id=pid)
    if not result["units"]:
        log.warning("rentcafe_direct %s: configured collection scope yielded 0 units — fall through", pid)
        return None

    fr = FetchResult(
        url=url,
        outcome=FetchOutcome.OK,
        status=200,
        body=body.encode("utf-8"),
        headers={"content-type": "text/html"},
        render_mode=RenderMode.GET,
        final_url=url,
        attempts=1,
        elapsed_ms=0,
        network_log=[
            {
                "url": url,
                "status": 200,
                "content_type": "text/html",
                "body": body,
                "captcha_detected": False,
            }
        ],
        fetch_tier_used=int(FetchTier.HYPERBROWSER),
    )
    log.info(
        "rentcafe_direct: %s served %d units (%s) via HB in-page fetch — render skipped (%s)",
        pid,
        len(units),
        units[0].get("unit_number", "?") if units else "?",
        url,
    )
    return {"fetch_result": fr, "result": result}
