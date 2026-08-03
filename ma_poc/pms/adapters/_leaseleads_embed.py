"""Shared LeaseLeads-embed recovery for Squarespace/Wix shells.

2026-05-19 deep-probe finding: 5 confirmed cases (ids 72391, 298586,
258789, 59649, 252116) are Squarespace marketing shells with a
LeaseLeads iframe embed:

    <iframe src="https://embed.leaseleads.co/{property_uuid}/floor-plans">

The iframe page itself is referrer-gated ("LeaseLeads: Forbidden") but
the **JSON API is public** (no auth, no referer check):

    GET https://api.leaseleads.co/api/v2/property/{uuid}              → property meta
    GET https://api.leaseleads.co/api/v2/property/{uuid}/floor-plans  → array of plans

Re-verified live on 2026-08-01 across three independent properties (Tribeca,
Lumina, and Emerson Park): 64/64 available rows had distinct native
``unit_id`` values, visible unit numbers, positive rents, and an
``available_on`` date.  The inline ``units.data`` roster is therefore emitted
at unit level when present; only a genuinely roster-less response falls back
to the historical plan summaries.

Wired into ``squarespace_nopms.py`` + ``wix_nopms.py`` as a recovery
fallback (alongside the AppFolio-embed recovery). When the iframe isn't
on the live page, probes the known sub-paths
(``/all-floor-plans``, ``/pricing``, ``/floor-plans``) for it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
    rent_in_sanity_range,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ma_poc.pms.adapters.base import AdapterContext

log = logging.getLogger(__name__)

# Matches the embed iframe src; capture group is the property UUID.  The
# protocol-relative form is what Squarespace publishes in its static HTML.
_LL_IFRAME_RE = re.compile(
    r"(?:https?:)?//embed\.leaseleads\.co/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_LL_INIT_RE = re.compile(
    r"LeaseLeadsEmbed\(\s*['\"]"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"['\"]",
    re.IGNORECASE,
)

# Sub-paths the LeaseLeads iframe is embedded on. Ordered by observed frequency.
_LL_EMBED_SUBPATHS: tuple[str, ...] = (
    "/all-floor-plans",
    "/pricing",
    "/floor-plans",
    "/floorplans",
)

_LL_API_BASE = "https://api.leaseleads.co/api/v2/property"
_LL_API_HOST = "api.leaseleads.co"
_ATTEMPTED_ATTR = "_leaseleads_embed_attempted"

# Status text variants observed live.
_AVAILABLE_NOW_RE = re.compile(r"available\s*now", re.IGNORECASE)
_MOVE_IN_RE = re.compile(r"move\s*in\s+(.+)$", re.IGNORECASE)
_WAITLIST_RE = re.compile(r"waitlist", re.IGNORECASE)


# JS run in the live page: harvest any LeaseLeads iframe URL already
# present; if absent, probe the known sub-paths for it (in-session fetch).
_LIVE_LL_SRC_JS = r"""
async () => {
  const direct = [];
  document.querySelectorAll('iframe').forEach((f) => {
    const s = f.src || '';
    if (/embed\.leaseleads\.co\/[0-9a-f-]{36}/i.test(s)) direct.push(s);
  });
  if (direct.length) return {hits: direct, source: 'live'};

  const probes = ["/all-floor-plans", "/pricing", "/floor-plans", "/floorplans"];
  for (const p of probes) {
    try {
      const r = await fetch(location.origin + p, {credentials: 'include'});
      if (!r.ok) continue;
      const t = await r.text();
      const m = t.match(/https?:\/\/embed\.leaseleads\.co\/[0-9a-f-]{36}/i);
      if (m) return {hits: [m[0]], source: 'probe:' + p};
    } catch (e) { /* next */ }
  }
  return {hits: [], source: 'none'};
}
"""


def _fetch_api_js(uuid: str) -> str:
    """JS that fetches the LeaseLeads API and returns ``{status, body}``.
    Runs in the live page so cookies/origin/headers are the browser's.

    The dict-shape return lets ``recover_leaseleads_embed`` distinguish a
    real 200-with-empty-body (no plans configured) from a 401/403/429/503
    bot-wall on ``api.leaseleads.co`` — recorded for triage telemetry.
    """
    safe_uuid = re.sub(r"[^0-9a-fA-F-]", "", uuid)
    url = f"{_LL_API_BASE}/{safe_uuid}/floor-plans"
    return f"""
(async () => {{
  try {{
    const r = await fetch({json.dumps(url)}, {{credentials: 'include'}});
    return {{status: r.status, body: r.ok ? await r.text() : ''}};
  }} catch (e) {{ return {{status: 0, body: ''}}; }}
}})()
"""


def extract_leaseleads_uuids(html: str) -> list[str]:
    """Return unique UUIDs explicitly published in the marketing HTML."""
    found: list[str] = []
    for pattern in (_LL_IFRAME_RE, _LL_INIT_RE):
        for match in pattern.finditer(html or ""):
            value = match.group(1).casefold()
            if value not in found:
                found.append(value)
    return found


def _body_from_ctx(ctx: AdapterContext) -> str:
    fr = getattr(ctx, "fetch_result", None)
    body = getattr(fr, "body", None) if fr is not None else None
    if isinstance(body, bytes):
        return body.decode("utf-8", "replace")
    return body if isinstance(body, str) else ""


def _page_url(page: Any, ctx: AdapterContext) -> str:
    fr = getattr(ctx, "fetch_result", None)
    final_url = str(getattr(fr, "final_url", "") or "") if fr is not None else ""
    if final_url:
        return final_url
    try:
        page_url = str(getattr(page, "url", "") or "")
    except Exception:
        page_url = ""
    return page_url or str(getattr(ctx, "base_url", "") or "")


def _canonical_host(value: object) -> str:
    try:
        host = (urlparse(str(value or "")).hostname or "").casefold()
    except Exception:
        return ""
    return host.removeprefix("www.")


def _norm(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


_ADDRESS_ALIASES = {
    "avenue": "ave",
    "boulevard": "blvd",
    "circle": "cir",
    "court": "ct",
    "drive": "dr",
    "highway": "hwy",
    "lane": "ln",
    "parkway": "pkwy",
    "place": "pl",
    "road": "rd",
    "street": "st",
}


def _norm_address(value: object) -> str:
    return " ".join(_ADDRESS_ALIASES.get(token, token) for token in _norm(value).split())


def _name_tokens(value: object) -> set[str]:
    ignored = {
        "apartment",
        "apartments",
        "at",
        "community",
        "homes",
        "of",
        "the",
    }
    return {token for token in _norm(value).split() if token not in ignored}


def _provider_identity_matches(
    meta: dict[str, Any], uuid: str, ctx: AdapterContext, origin: str
) -> bool:
    """Fail closed unless the public UUID is the configured property."""
    if str(meta.get("id") or "").strip().casefold() != uuid:
        return False
    if not origin or _canonical_host(meta.get("domain")) != _canonical_host(origin):
        return False

    expected_name = _name_tokens(getattr(ctx, "property_name", ""))
    provider_name = _name_tokens(meta.get("name"))
    if not expected_name or not provider_name:
        return False
    if not (expected_name <= provider_name or provider_name <= expected_name):
        return False

    address = meta.get("address")
    if not isinstance(address, dict):
        return False
    expected_street = _norm_address(getattr(ctx, "address", ""))
    expected_city = _norm(getattr(ctx, "city", ""))
    expected_zip = str(getattr(ctx, "zip_code", "") or "").strip()[:5]
    provider_street = _norm_address(address.get("street"))
    provider_city = _norm(address.get("city"))
    provider_zip = str(address.get("post_code") or "").strip()[:5]
    return bool(
        expected_street
        and expected_street == provider_street
        and expected_city
        and expected_city == provider_city
        and expected_zip
        and expected_zip == provider_zip
    )


def _money(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        parsed = int(round(float(value)))
    else:
        parsed = money_to_int(str(value))
    return parsed if rent_in_sanity_range(parsed) else None


def _native_rows(
    plans: list[dict[str, Any]], url: str, property_uuid: str
) -> tuple[list[dict[str, Any]], bool, bool]:
    """Return ``(rows, roster_present, contamination_free)``."""
    rows: list[dict[str, Any]] = []
    native_ids: set[str] = set()
    roster_present = False
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        container = plan.get("units")
        if isinstance(container, dict):
            raw_units = container.get("data")
            if raw_units is None:
                raw_units = []
            if not isinstance(raw_units, list):
                return [], True, False
        elif isinstance(container, list):
            raw_units = container
        else:
            raw_units = []
        if raw_units:
            roster_present = True

        plan_uuid = str(plan.get("id") or "").strip()
        external_plan_id = str(plan.get("external_id") or "").strip()
        plan_property_id = str(plan.get("property_id") or "").strip().casefold()
        if plan_property_id and plan_property_id != property_uuid:
            return [], True, False

        name = str(plan.get("name") or "").strip()
        beds_raw = plan.get("bedrooms")
        try:
            beds = int(beds_raw) if beds_raw is not None else None
        except (TypeError, ValueError):
            beds = None
        baths_raw = plan.get("bathrooms")
        baths = str(baths_raw) if baths_raw not in (None, "") else ""
        marketing = str(plan.get("marketing_label") or "").strip()

        for raw in raw_units:
            if not isinstance(raw, dict):
                return [], True, False
            raw_property_id = str(raw.get("property_id") or "").strip().casefold()
            raw_plan_uuid = str(raw.get("floorplan_uuid") or "").strip()
            raw_plan_id = str(raw.get("floorplan_id") or "").strip()
            if (
                raw_property_id != property_uuid
                or (plan_uuid and raw_plan_uuid and raw_plan_uuid != plan_uuid)
                or (
                    external_plan_id
                    and raw_plan_id
                    and raw_plan_id != external_plan_id
                )
            ):
                return [], True, False

            native_id = str(raw.get("unit_id") or "").strip()
            unit_number = str(raw.get("unit_number") or "").strip()
            rent_lo = _money(raw.get("price_min")) or _money(raw.get("price"))
            rent_hi = _money(raw.get("price_max")) or rent_lo
            if not native_id or native_id in native_ids:
                return [], True, False
            if not unit_number or rent_lo is None:
                continue

            sqft_raw = raw.get("size") or plan.get("size_min")
            try:
                sqft = str(int(float(str(sqft_raw)))) if sqft_raw not in (None, "") else ""
            except (TypeError, ValueError):
                sqft = ""
            deposit_low = _money(raw.get("deposit_min"))
            deposit_high = _money(raw.get("deposit_max"))
            deposit = format_rent_range(deposit_low, deposit_high)
            row = make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=baths,
                sqft=sqft,
                unit_number=unit_number,
                unit_name=unit_number,
                floor=str(raw.get("floor") or "").strip(),
                building=str(raw.get("building") or "").strip(),
                rent_low=rent_lo,
                rent_high=rent_hi,
                deposit=deposit,
                concession=str(raw.get("marketing_label") or marketing).strip(),
                availability_status="AVAILABLE",
                available_units="1",
                availability_date=str(raw.get("available_on") or "").strip(),
                source_api_url=url,
                extraction_tier="TIER_1_API_LEASELEADS_UNITS",
                source_ids={"leaseleads_unit_id": native_id},
            )
            row.update(
                {
                    "provider_native_unit_id": native_id,
                    "source_property_id": property_uuid,
                    "source_property_name": "",
                    "source_property_provenance": (
                        "published_leaseleads_uuid_provider_identity_bound"
                    ),
                }
            )
            rows.append(row)
            native_ids.add(native_id)
    return rows, roster_present, True


def _parse_status(status: str) -> tuple[str, str]:
    """Return (availability_status, availability_date)."""
    if not status:
        return "AVAILABLE", ""
    if _WAITLIST_RE.search(status):
        return "UNAVAILABLE", ""
    if _AVAILABLE_NOW_RE.search(status):
        return "AVAILABLE", ""
    m = _MOVE_IN_RE.search(status)
    if m:
        return "AVAILABLE", m.group(1).strip()
    return "AVAILABLE", ""


def parse_leaseleads_floorplans(
    plans: list[dict[str, Any]],
    url: str,
    *,
    property_uuid: str = "",
) -> list[dict[str, Any]]:
    """Map LeaseLeads ``/floor-plans`` JSON to units or plan summaries.

    Native ``units.data`` rows take precedence when the provider publishes a
    roster.  Returning the plan shells alongside those rows would silently
    re-introduce plan-level records into a unit-level result, so the historical
    plan parser runs only when no inline roster exists anywhere in the payload.
    """
    if property_uuid:
        native, roster_present, clean = _native_rows(plans, url, property_uuid)
        if roster_present:
            return native if clean else []

    units: list[dict[str, Any]] = []
    for p in plans:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        beds_raw = p.get("bedrooms")
        try:
            beds: int | None = int(beds_raw) if beds_raw is not None else None
        except (TypeError, ValueError):
            beds = None
        baths_raw = p.get("bathrooms")
        baths = str(baths_raw) if baths_raw not in (None, "") else ""

        sqft_min = p.get("size_min")
        sqft = str(int(sqft_min)) if isinstance(sqft_min, (int, float)) and sqft_min > 0 else ""

        price_min = p.get("price_min")
        price_max = p.get("price_max")
        try:
            rent_lo = int(price_min) if isinstance(price_min, (int, float)) and price_min > 0 else None
            rent_hi = int(price_max) if isinstance(price_max, (int, float)) and price_max > 0 else None
        except (TypeError, ValueError):
            rent_lo = rent_hi = None

        status, avail_date = _parse_status(str(p.get("status") or ""))

        marketing = str(p.get("marketing_label") or "").strip()

        if not name and beds is None and rent_lo is None:
            continue

        units.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=baths,
                sqft=sqft,
                unit_number="",
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi,
                availability_status=status,
                availability_date=avail_date,
                concession=marketing,
                source_api_url=url,
                extraction_tier="TIER_1_API_LEASELEADS",
            )
        )
    return units


def _origin(page: Page, ctx: AdapterContext) -> str:
    """scheme://host for provenance."""
    candidate = _page_url(page, ctx)
    try:
        p = urlparse(candidate)
    except Exception:
        return ""
    if not p.scheme or not p.netloc:
        return ""
    return urlunparse((p.scheme, p.netloc, "", "", "", ""))


async def _fetch_api_payload(
    url: str, *, referer: str, origin: str
) -> tuple[int, Any, str]:
    """Fetch one referrer-gated public API payload; never use an unlocker."""

    def _run() -> tuple[int, Any, str]:
        try:
            from ma_poc.pms.adapters._probe import probe_get

            response = probe_get(
                url,
                unlocker=False,
                retries=1,
                timeout=25,
                headers={
                    "Accept": "application/json",
                    "Origin": origin,
                    "Referer": referer,
                },
            )
        except Exception:
            return 0, None, url
        try:
            status = int(getattr(response, "status_code", 0) or 0)
        except (TypeError, ValueError):
            status = 0
        final_url = str(getattr(response, "url", "") or url)
        expected = urlparse(url)
        observed = urlparse(final_url)
        if (
            status != 200
            or (observed.hostname or "").casefold() != _LL_API_HOST
            or observed.path.rstrip("/") != expected.path.rstrip("/")
        ):
            return status, None, final_url
        try:
            payload = response.json()
        except Exception:
            try:
                payload = json.loads(str(getattr(response, "text", "") or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                return status, None, final_url
        return status, payload, final_url

    try:
        return await asyncio.to_thread(_run)
    except Exception:
        return 0, None, url


async def recover_leaseleads_embed(
    page: Page,
    ctx: AdapterContext,
) -> list[dict[str, Any]]:
    """Recover the exact page-published LeaseLeads property.

    Production commonly dispatches adapters with ``page=None``.  Scan the
    already-rendered fetch body first, then use the live-page scan only as a
    dynamic-marker fallback.  The provider metadata must match the configured
    domain, name, street, city, and ZIP before any rows are emitted.
    """
    if bool(getattr(ctx, _ATTEMPTED_ATTR, False)):
        return []

    uuids = extract_leaseleads_uuids(_body_from_ctx(ctx))
    evaluate = getattr(page, "evaluate", None)
    if not uuids and callable(evaluate):
        try:
            scan = await evaluate(_LIVE_LL_SRC_JS)
        except Exception as exc:
            log.debug("LeaseLeads-embed scan failed err=%s", exc)
            scan = None
        if isinstance(scan, dict):
            hits = scan.get("hits") or []
            if isinstance(hits, list):
                for hit in hits:
                    if not isinstance(hit, str):
                        continue
                    match = _LL_IFRAME_RE.search(hit)
                    if match:
                        value = match.group(1).casefold()
                        if value not in uuids:
                            uuids.append(value)
    # A marketing page publishing multiple property UUIDs is a portfolio
    # boundary, not an exact-property surface.  Reject rather than guessing.
    if len(uuids) != 1:
        return []
    try:
        setattr(ctx, _ATTEMPTED_ATTR, True)
    except Exception:
        pass

    uuid = uuids[0]
    referer = _page_url(page, ctx)
    origin = _origin(page, ctx)
    if not referer or not origin:
        return []
    meta_url = f"{_LL_API_BASE}/{uuid}"
    api_url = f"{meta_url}/floor-plans"
    meta_status, meta, _meta_final = await _fetch_api_payload(
        meta_url, referer=referer, origin=origin
    )
    plans_status, plans, plans_final = await _fetch_api_payload(
        api_url, referer=referer, origin=origin
    )

    from ma_poc.pms.adapters._universal_recovery import is_bot_block, mark_blocked

    if is_bot_block(meta_status):
        mark_blocked(ctx, "leaseleads_embed", meta_url, meta_status)
    if is_bot_block(plans_status):
        mark_blocked(ctx, "leaseleads_embed", api_url, plans_status)
    if not isinstance(meta, dict) or not isinstance(plans, list):
        return []
    if not _provider_identity_matches(meta, uuid, ctx, origin):
        return []
    rows = parse_leaseleads_floorplans(
        plans,
        plans_final or api_url,
        property_uuid=uuid,
    )
    for row in rows:
        if isinstance(row, dict):
            row["source_property_name"] = str(meta.get("name") or "").strip()
            address = meta.get("address") or {}
            if isinstance(address, dict):
                row["source_property_address"] = ", ".join(
                    value
                    for value in (
                        str(address.get("street") or "").strip(),
                        str(address.get("city") or "").strip(),
                        (
                            f"{address.get('state') or ''} "
                            f"{address.get('post_code') or ''}"
                        ).strip(),
                    )
                    if value
                )
    return rows
