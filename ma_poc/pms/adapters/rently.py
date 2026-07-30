"""Rently scattered-site portal — `secure.rently.com/api/properties/searchQuery`.

Some single-family / build-to-rent operators host their roster on Rently: the
property's own site redirects to ``u{managerID}.rently.com/propertiesSearch2``,
a JS SPA whose 52KB shell carries NO data — the homes load client-side from

    https://secure.rently.com/api/properties/searchQuery?pc=1&managerID={ID}

a plain JSON endpoint (``{"property_data": [ ... ]}``). Reverse-engineered
2026-07-30 by hooking XHR.open in the browser and re-triggering the search;
verified code-only (unlocker=False) on Jodeco Landing (managerID 62564, 5 homes).

Each entry is a scattered-site HOME, so — like AppFolio scattered listings
(#29/#34) — the street ADDRESS is the marketing identity: it lives in
``unit_name`` with ``unit_number`` left empty, and downstream anchors ``unit_id``
to it. ``floorplan.rent`` is the clean per-home rent (no fee-transparency here).
"""

from __future__ import annotations

import json
import re
from typing import Any

from ma_poc.pms.adapters._parsing import make_unit_dict

# ``u62564.rently.com`` → managerID 62564. The property's site 30x-redirects
# here, so this matches against ``fetch_result.final_url`` / the resolved host.
_RENTLY_HOST_RE = re.compile(r"\bu(\d+)\.rently\.com", re.IGNORECASE)

_SEARCH_BASE = "https://secure.rently.com/api/properties/searchQuery"

# "Aug 18, 2026" is the ready_date_text format; "Now" / "Today" / "Available"
# mean immediate availability (no invented future date — cf. #75).
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_READY_RE = re.compile(r"([A-Za-z]{3})[a-z]*\s+(\d{1,2}),?\s+(\d{4})")


def rently_manager_id(url_or_host: str) -> str | None:
    """Extract the Rently managerID from a ``u{ID}.rently.com`` URL/host."""
    m = _RENTLY_HOST_RE.search(url_or_host or "")
    return m.group(1) if m else None


def rently_search_url(manager_id: str) -> str:
    """Build the code-only searchQuery endpoint for a managerID."""
    return f"{_SEARCH_BASE}?pc=1&managerID={manager_id}"


def _ready_date_iso(raw: Any) -> str:
    """ISO date from Rently ``ready_date_text``; '' for Now/blank/unparseable."""
    s = str(raw or "").strip()
    if not s or s.lower() in ("now", "today", "available", "available now"):
        return ""
    m = _READY_RE.search(s)
    if not m:
        return ""
    mon = _MONTHS.get(m.group(1).lower())
    if not mon:
        return ""
    return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"


def _num_str(value: Any) -> str:
    """Format a JSON number (often a float like 4.0) as a clean string."""
    if value in (None, ""):
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    return str(int(f)) if f == int(f) else str(f)


def _rent_int(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def parse_rently_search(body: str, url: str) -> list[dict[str, Any]]:
    """Parse a Rently ``searchQuery`` JSON body into scattered-site unit rows.

    Returns one row per home (address = identity), or ``[]`` when the body is
    absent / not the expected ``{"property_data": [...]}`` shape. Never raises.
    """
    if not body or "property_data" not in body:
        return []
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return []
    homes = data.get("property_data") if isinstance(data, dict) else None
    if not isinstance(homes, list):
        return []

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for h in homes:
        if not isinstance(h, dict):
            continue
        address = str(h.get("address") or "").strip()
        rid = h.get("id")
        key = address or str(rid or "")
        if not key or key in seen:
            continue
        seen.add(key)
        fp = h.get("floorplan") if isinstance(h.get("floorplan"), dict) else {}
        rent = _rent_int(fp.get("rent"))
        source_ids: dict[str, Any] = {}
        if rid not in (None, ""):
            source_ids["rently_id"] = str(rid)
        if address:
            source_ids["rently_full_address"] = address
        rows.append(
            make_unit_dict(
                floor_plan_name="",  # scattered single-family: no floor plan
                bedrooms=_num_str(fp.get("bedrooms")),
                bathrooms=_num_str(fp.get("bathrooms")),
                sqft=_num_str(fp.get("size")),
                unit_number="",  # address is the identity (scattered-site, #29)
                unit_name=address,
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                availability_date=_ready_date_iso(h.get("ready_date_text")),
                source_api_url=url,
                extraction_tier="TIER_1_API_RENTLY",
                source_ids=source_ids or None,
            )
        )
    return rows


async def recover_rently(ctx: Any) -> list[dict[str, Any]]:
    """Cross-vendor misroute net (#89): recover a Rently-hosted roster.

    A property whose own site redirects to ``u{ID}.rently.com`` is detected as
    generic/plan-text (the SPA shell has no data) and returns nothing. This
    detects the rently host from the resolved final URL — or, if the redirect
    was client-side, from the fetched body — extracts the managerID, fetches
    the searchQuery JSON endpoint code-only (off the event loop), and parses it.
    Returns ``[]`` when the property is not Rently-hosted or the fetch fails.
    Never raises.
    """
    try:
        manager_id: str | None = None
        fr = getattr(ctx, "fetch_result", None)
        for cand in (
            str(getattr(fr, "final_url", "") or "") if fr is not None else "",
            str(getattr(ctx, "base_url", "") or ""),
        ):
            manager_id = rently_manager_id(cand)
            if manager_id:
                break
        if not manager_id:
            # Client-side redirect: the rently host is only in the body.
            body = getattr(fr, "body", None) if fr is not None else None
            if isinstance(body, bytes):
                body = body.decode("utf-8", "replace")
            if isinstance(body, str) and "rently.com" in body:
                manager_id = rently_manager_id(body)
        if not manager_id:
            return []

        import asyncio

        from ma_poc.pms.adapters._probe import probe_get

        url = rently_search_url(manager_id)
        r = await asyncio.to_thread(probe_get, url, timeout=20, unlocker=False)
        return parse_rently_search(getattr(r, "text", "") or "", url)
    except Exception:  # noqa: BLE001 — recovery net must never raise
        return []
