"""
Knock vendor adapter — Doorway public API.

Research log
------------
Knock's web widget (``doorway.knck.io/latest/doorway.min.js``) is initialised
in static HTML with::

    window.knockDoorway.init('<public_key>', 'community', '<community_id>');

Where ``<public_key>`` is a 32-char hex application key and ``<community_id>``
is the property's identifier (16+ char hex). With those in hand, Knock's
public Doorway API returns full unit data without authentication:

  GET ``doorway-api.knockrentals.com/v1/property/community/<community_id>``
    → ``{property: {id: <numeric_id>, ...}}``

  GET ``doorway-api.knockrentals.com/v1/property/<numeric_id>/units``
    → ``{units_data: {units: [...], layouts: [...]}}``

Each unit entry observed in the wild::

    { area: 1200, available: true, availableOn: "2026-05-30",
      bathrooms: 2, bedrooms: 2, displayPrice: "2409", price: "2409",
      knockPrice: null, hidden: false, leased: false, occupied: false,
      reserved: false, layoutId: null, layoutName: null,
      name: "M6-202", propertyId: 2023560, ... }

Source: 2026-04-30 failure-recovery investigation. 26 of 38 Knock-flagged
properties recovered (619 units total) via this path.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

# Pattern matches the static-HTML init call. Captures (public_key, kind, id).
_KNOCK_INIT_RE = re.compile(
    r"knockDoorway\.init\s*\(\s*['\"]([a-f0-9]{20,40})['\"]\s*,\s*"
    r"['\"](community|application|public)['\"]\s*,\s*"
    r"['\"]([a-zA-Z0-9_-]{8,40})['\"]",
    re.IGNORECASE,
)

_RENT_INT_RE = re.compile(r"(\d[\d,]*)")


def _to_int(v: Any) -> int | None:
    """Best-effort int conversion for Knock's mixed rent fields (str / int / float)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        n = int(v)
        return n if 0 < n < 1_000_000 else None
    if isinstance(v, str):
        m = _RENT_INT_RE.search(v)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                return None
    return None


def find_knock_ids(html: str) -> tuple[str | None, str | None, str | None]:
    """Return ``(public_key, kind, id)`` if a Knock init call is present.

    ``kind`` is one of ``community`` | ``application`` | ``public``.
    """
    if not html or "knock" not in html.lower():
        return None, None, None
    m = _KNOCK_INIT_RE.search(html)
    if m:
        return m.group(1), m.group(2).lower(), m.group(3)
    return None, None, None


def parse_knock_units(units_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert Knock's ``units_data.units`` array into standard unit dicts.

    Skips units flagged ``hidden`` / ``leased`` / ``reserved`` (no useful
    rent), and filters out any record without a price in the $200-$50K range.
    """
    units: list[dict[str, Any]] = []
    units_data = units_payload.get("units_data", {})
    raw_units = units_data.get("units") or []
    layouts_list = units_data.get("layouts") or []
    layouts: dict[Any, dict[str, Any]] = {
        layout.get("id"): layout for layout in layouts_list if isinstance(layout, dict)
    }

    for u in raw_units:
        if not isinstance(u, dict):
            continue
        if u.get("hidden") or u.get("leased") or u.get("reserved"):
            continue

        rent = (
            _to_int(u.get("price"))
            or _to_int(u.get("displayPrice"))
            or _to_int(u.get("knockPrice"))
            or _to_int(u.get("min_rent"))
            or _to_int(u.get("rent"))
        )
        if not rent or not (200 <= rent <= 50_000):
            continue

        layout_id = u.get("layoutId") or u.get("layout_id") or u.get("layout")
        layout = layouts.get(layout_id, {}) if layout_id else {}

        beds = u.get("bedrooms")
        if beds is None:
            beds = layout.get("bedrooms")
        baths = u.get("bathrooms")
        if baths is None:
            baths = layout.get("bathrooms")
        sqft = (
            _to_int(u.get("area"))
            or _to_int(u.get("square_feet"))
            or _to_int(u.get("sqft"))
            or _to_int(layout.get("area"))
            or _to_int(layout.get("square_feet"))
        )
        unit_number = (
            u.get("name") or u.get("unit_number") or u.get("apartment_number") or ""
        )
        avail = (
            u.get("availableOn")
            or u.get("available_on")
            or u.get("ready_date")
            or ""
        )
        status = "AVAILABLE" if (u.get("available") and not u.get("occupied")) else "UNAVAILABLE"

        units.append(
            {
                "unit_number": str(unit_number),
                "floor_plan_name": str(u.get("layoutName") or layout.get("name") or ""),
                "bedrooms": str(beds) if beds is not None else "",
                "bathrooms": str(baths) if baths is not None else "",
                "sqft": str(sqft) if sqft else "",
                "market_rent_low": rent,
                "market_rent_high": rent,
                "rent_range": str(rent),
                "availability_status": status,
                "availability_date": str(avail)[:30],
                "building": str(u.get("buildingName") or ""),
                "extraction_tier": "TIER_1_KNOCK_API",
            }
        )
    return units


class KnockAdapter:
    """Adapter for Knock-managed properties.

    Public-API path: extract the ``community_id`` from the static page,
    then call ``doorway-api.knockrentals.com`` directly. No browser needed.
    Falls back to the generic cascade if the init call isn't found in the
    static HTML.
    """

    pms_name = "knock"

    def __init__(self) -> None:
        self._fingerprints: list[str] = [
            "doorway.knck.io",
            "knockDoorway",
            "doorway-api.knockrentals.com",
        ]

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units via Knock's Doorway API.

        The ``page`` argument is unused — Knock units come from a public
        JSON API, no rendering is required. We use the entry HTML (already
        in ctx.fetch_result.body) to find the community_id.
        """
        result = AdapterResult(tier_used="TIER_1_KNOCK_API")

        # Pull HTML from the L1 fetch result.
        fr = getattr(ctx, "fetch_result", None)
        body = getattr(fr, "body", None) if fr is not None else None
        html: str = ""
        if isinstance(body, bytes):
            try:
                html = body.decode("utf-8", errors="replace")
            except Exception:
                html = ""
        elif isinstance(body, str):
            html = body

        if not html:
            result.errors.append("knock-adapter: no entry HTML to scan for init call")
            return result

        public_key, kind, comm_id = find_knock_ids(html)
        if not (public_key and comm_id):
            result.errors.append("knock-adapter: no knockDoorway.init() call in HTML")
            return result

        # Hit the Doorway API. Two-step: community → property metadata → units.
        try:
            units = await _fetch_knock_units(comm_id, kind or "community")
        except Exception as exc:
            result.errors.append(f"knock-api-error: {exc}")
            return result

        if not units:
            result.errors.append("knock-adapter: Doorway API returned no units")
            return result

        result.units = units
        result.winning_url = (
            f"https://doorway-api.knockrentals.com/v1/property/community/{comm_id}"
        )
        result.confidence = min(0.9, 0.6 + 0.02 * len(units))
        return result


async def _fetch_knock_units(comm_id: str, kind: str = "community") -> list[dict[str, Any]]:
    """Two-step Doorway API fetch: community → numeric_id → units.

    This is its own coroutine so the adapter ``extract`` method stays
    focused on orchestration.
    """
    import httpx

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Origin": "https://doorway.knck.io",
        "Accept": "application/json",
    }
    base = "https://doorway-api.knockrentals.com/v1/property"
    if kind == "numeric_property":
        # Community API was already short-circuited to a numeric id by the
        # caller; hit /units directly.
        units_url = f"{base}/{comm_id}/units"
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
            r = await c.get(units_url, headers=headers)
            if r.status_code != 200:
                return []
            try:
                return parse_knock_units(r.json())
            except Exception:
                return []

    # Community-keyed: fetch property meta first.
    community_url = f"{base}/community/{comm_id}"
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
        r = await c.get(community_url, headers=headers)
        if r.status_code != 200:
            return []
        try:
            prop_data = r.json().get("property") or {}
        except Exception:
            return []
        numeric_id = prop_data.get("id")
        if not numeric_id:
            return []
        units_url = f"{base}/{numeric_id}/units"
        r2 = await c.get(units_url, headers=headers)
        if r2.status_code != 200:
            return []
        try:
            return parse_knock_units(r2.json())
        except Exception:
            return []
