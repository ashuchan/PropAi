"""F6 — runner dispatch helper.

Lives outside ``jugnu_runner.py`` so tests can import it without
triggering ``jugnu_runner``'s ``dotenv.load_dotenv()`` side effect
(which pollutes env vars used by ``_live``-marked LLM tests).

Public surface:
- :func:`try_rentcafe_direct` — entry hook called by the runner.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.rentcafe_direct import fetch_direct, resolve_property_id

log = logging.getLogger(__name__)


_SUCCESS_TIERS: frozenset[str] = frozenset(
    {
        "TIER_1_API_RENTCAFE_DIRECT",
        "TIER_1_API_RENTCAFE_DIRECT_LIST_EMPTY",
    }
)


async def try_rentcafe_direct(
    task: Any,
    profile: Any,
    csv_row: dict[str, Any] | None,
) -> tuple[FetchResult | None, str | None]:
    """Try the RentCafe aggregator before the vanity-domain fetch.

    Returns ``(synthetic_fetch_result, property_id)`` on success, or
    ``(None, None)`` on any failure. Caller falls back to the existing
    L1 fetch on ``None`` (H4).

    Strategy:
      1. Use cached ``profile.api_hints.rentcafe_property_id`` if set.
      2. Otherwise resolve from CSV (name + city + zip + vanity_domain).
      3. Fetch the aggregator API by propertyId.
      4. Synthesize a FetchResult with ``network_log=[api_response]``
         so the existing :class:`RentCafeAdapter` (driven by
         ``scrape()``) parses the body unchanged — H7 invariant.

    Never raises.
    """
    cached_id: str | None = None
    if profile is not None:
        try:
            cached_id = getattr(profile.api_hints, "rentcafe_property_id", None)
        except Exception:
            cached_id = None

    if cached_id is None:
        row = csv_row or {}

        def _csv(*keys: str) -> str:
            for k in keys:
                v = row.get(k)
                if v not in (None, "", "null", "None"):
                    return str(v).strip()
            return ""

        property_name = _csv("name", "Name", "Property Name", "proj_name")
        city = _csv("city", "City")
        zip_code = _csv("zip", "Zip", "ZIP Code", "zip_code")
        website = _csv("website", "Website")
        vanity_domain: str | None = None
        if website:
            try:
                vanity_domain = urlparse(website).hostname
            except Exception:
                vanity_domain = None

        if not property_name or not zip_code:
            return None, None

        try:
            resolve_r = await resolve_property_id(
                property_name=property_name,
                city=city,
                zip_code=zip_code,
                vanity_domain=vanity_domain,
            )
            cached_id = resolve_r.property_id
        except Exception as exc:
            log.debug(
                "rentcafe_direct resolve_property_id raised for %s: %s",
                getattr(task, "property_id", "?"),
                exc,
            )
            return None, None

    if cached_id is None:
        return None, None

    try:
        direct_r = await fetch_direct(cached_id)
    except Exception as exc:
        log.debug(
            "rentcafe_direct fetch_direct raised for %s/%s: %s",
            getattr(task, "property_id", "?"),
            cached_id,
            exc,
        )
        return None, None

    if direct_r.tier_code not in _SUCCESS_TIERS:
        return None, None

    if not direct_r.api_responses:
        return None, None
    api = direct_r.api_responses[0]
    body = api.get("body")
    try:
        body_str = json.dumps(body) if isinstance(body, (dict, list)) else str(body)
    except (TypeError, ValueError):
        body_str = ""
    network_log = [
        {
            "url": api.get("url", ""),
            "status": api.get("status", 200),
            "content_type": "application/json",
            "body_size": len(body_str),
            "body": body_str,
        }
    ]
    synthetic = FetchResult(
        url=task.url,
        outcome=FetchOutcome.OK,
        status=200,
        body=body_str.encode("utf-8", errors="replace") if body_str else b"",
        headers={"content-type": "application/json"},
        render_mode=RenderMode.GET,
        final_url=api.get("url", task.url),
        attempts=1,
        elapsed_ms=direct_r.elapsed_ms,
        network_log=network_log,
        error_signature=direct_r.tier_code,
        proxy_used=None,
    )
    return synthetic, cached_id
