"""Shared LeaseLeads-embed recovery for Squarespace/Wix shells.

2026-05-19 deep-probe finding: 5 confirmed cases (ids 72391, 298586,
258789, 59649, 252116) are Squarespace marketing shells with a
LeaseLeads iframe embed:

    <iframe src="https://embed.leaseleads.co/{property_uuid}/floor-plans">

The iframe page itself is referrer-gated ("LeaseLeads: Forbidden") but
the **JSON API is public** (no auth, no referer check):

    GET https://api.leaseleads.co/api/v2/property/{uuid}              → property meta
    GET https://api.leaseleads.co/api/v2/property/{uuid}/floor-plans  → array of plans

Verified live on liveatlumina.com (UUID 9e5e0a14-…): 30 plans, each with
``name``, ``bedrooms``, ``bathrooms``, ``size_min``/``size_max``,
``price_min``/``price_max`` (numeric, ``0`` for waitlist), ``status``
("Available Now" | "Waitlist" | "Move In <MonthName Nth, YYYY>"),
``marketing_label`` (concession), inline ``units`` field.

Wired into ``squarespace_nopms.py`` + ``wix_nopms.py`` as a recovery
fallback (alongside the AppFolio-embed recovery). When the iframe isn't
on the live page, probes the known sub-paths
(``/all-floor-plans``, ``/pricing``, ``/floor-plans``) for it.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ma_poc.pms.adapters.base import AdapterContext

log = logging.getLogger(__name__)

# Matches the embed iframe src; capture group is the property UUID.
_LL_IFRAME_RE = re.compile(
    r"https?://embed\.leaseleads\.co/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
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
    plans: list[dict[str, Any]], url: str
) -> list[dict[str, str]]:
    """Map LeaseLeads ``/floor-plans`` JSON rows to plan-level unit dicts.

    Rents are numeric (``price_min``/``price_max``, ``0`` when on waitlist
    or call-for-pricing → no rent emitted). Status drives
    ``availability_status`` + ``availability_date`` when stated.
    """
    units: list[dict[str, str]] = []
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
    candidate = ""
    try:
        candidate = page.url or ""
    except Exception:
        candidate = ""
    if not candidate:
        candidate = getattr(ctx, "base_url", "") or ""
    try:
        p = urlparse(candidate)
    except Exception:
        return ""
    if not p.scheme or not p.netloc:
        return ""
    return urlunparse((p.scheme, p.netloc, "", "", "", ""))


async def recover_leaseleads_embed(
    page: Page,
    ctx: AdapterContext,
) -> list[dict[str, str]]:
    """Find an embedded LeaseLeads iframe + call its public API. Returns
    plan-level unit dicts, or ``[]`` when nothing is discoverable.
    Never raises.
    """
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return []

    try:
        scan = await evaluate(_LIVE_LL_SRC_JS)
    except Exception as exc:
        log.debug("LeaseLeads-embed scan failed err=%s", exc)
        return []
    if not isinstance(scan, dict):
        return []
    hits = scan.get("hits") or []
    if not isinstance(hits, list) or not hits:
        return []

    uuids: list[str] = []
    for h in hits:
        if not isinstance(h, str):
            continue
        m = _LL_IFRAME_RE.search(h)
        if m:
            uuids.append(m.group(1).lower())
    uuids = list(dict.fromkeys(uuids))  # de-dupe, preserve order
    if not uuids:
        return []

    api_url = f"{_LL_API_BASE}/{uuids[0]}/floor-plans"
    try:
        result = await evaluate(_fetch_api_js(uuids[0]))
    except Exception as exc:
        log.debug("LeaseLeads API fetch failed err=%s", exc)
        return []

    # Late import to avoid module-load circularity.
    from ma_poc.pms.adapters._universal_recovery import is_bot_block, mark_blocked

    # New wire format: {status, body}. Back-compat: a plain JSON string
    # (legacy / test mocks) is treated as a 200.
    status: int = 0
    body: str = ""
    if isinstance(result, dict):
        try:
            status = int(result.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        b = result.get("body")
        if isinstance(b, str):
            body = b
    elif isinstance(result, str):
        status = 200 if result else 0
        body = result

    if is_bot_block(status):
        mark_blocked(ctx, "leaseleads_embed", api_url, status)

    if not body:
        return []
    try:
        plans = json.loads(body)
    except json.JSONDecodeError:
        return []
    if not isinstance(plans, list):
        return []
    return parse_leaseleads_floorplans(plans, api_url)
