"""G5 Marketing Cloud adapter.

Research log
------------
G5 (https://www.getg5.com/) is the marketing platform powering Morgan
Properties and many other multifamily operators (Aimco, Bell Partners, ZRS,
JMG, BH Companies). The platform exposes a public GraphQL inventory API at
``inventory.g5marketingcloud.com/graphql``. Discovered 2026-05-13 via Chrome
MCP probe of www.morgan-properties.com/apartments/pa/harrisburg/kings-manor-apartments/.

Key findings (2026-05-13)
-------------------------
* Endpoint: ``POST https://inventory.g5marketingcloud.com/graphql``
* No auth required for read queries (introspection enabled).
* Property identified by ``locationUrn`` — the full G5 slug, e.g.
  ``g5-cl-1jsdmzcxpf-king-s-manor-apartments``. Slug is discoverable from
  any ``g5-cl-...`` reference in the property's HTML (image CDN paths
  carry it: ``g5-assets-cld-res.cloudinary.com/.../g5-cl-{slug}/uploads/...``).
* Query path that returns unit-level data:
  ``apartmentComplex(locationUrn:$urn){apartments(perPage:200){...}}``
  — each apartment carries ``name``, ``availabilityDate``, ``prices`` (list
  of priced lease terms), ``sqftDisplay``, plus a joined ``floorplan`` with
  ``beds``/``baths``/``sqft``/``name``.
* Sample: King's Manor returned 21 apartments, every one with
  ``availabilityDate`` populated — a strict improvement over the current
  generic Tier 1 pipeline which loses the date.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


_G5_ENDPOINT = "https://inventory.g5marketingcloud.com/graphql"

# Property URN regex — matches ``g5-cl-<id>[-<slug>]`` anywhere in the
# rendered HTML. The image CDN paths carry the slug consistently so this
# captures it on every G5-hosted property page.
_G5_URN_RE = re.compile(r"g5-cl-[a-z0-9]+(?:-[a-z0-9]+)*", re.IGNORECASE)

# GraphQL query — returns the unit + floor-plan join in one round trip.
# perPage:200 covers any normal property; pagination would need the API's
# total-count hint (not currently used).
_G5_UNITS_QUERY = (
    "query($urn:String!){apartmentComplex(locationUrn:$urn){"
    "id name "
    "apartments(perPage:200){"
    "id name displayName building availabilityDate sqftDisplay "
    "prices{value formattedPrice priceType} "
    "floorplan{id name beds baths sqft sqftDisplay}"
    "}}}"
)

_TIER_BASE = "TIER_1_API_G5"
_TIER_NO_URN = f"{_TIER_BASE}_NO_URN"
_TIER_API_ERROR = f"{_TIER_BASE}_API_ERROR"
_TIER_EMPTY = f"{_TIER_BASE}_EMPTY"


def find_g5_urn(html: str) -> str | None:
    """Return the longest ``g5-cl-...`` slug in *html*, or None.

    Longest match wins because the same property might have both the bare
    ``g5-cl-<id>`` and the full ``g5-cl-<id>-<name-slug>`` form; the
    GraphQL API accepts either but the longer form is unambiguous.
    """
    if not html or "g5-cl-" not in html.lower():
        return None
    matches = {m.group(0).lower() for m in _G5_URN_RE.finditer(html)}
    if not matches:
        return None
    return max(matches, key=len)


def _price_to_int(prices: Any) -> int | None:
    """Pull the first sensible price out of G5's ``prices: [...]`` list.

    G5 lists multiple ``priceType`` entries (``min_rent``, ``rate``,
    lease-term-specific). Prefer ``min_rent`` then ``rate`` then any.
    """
    if not isinstance(prices, list):
        return None
    by_type: dict[str, float | None] = {}
    fallback: float | None = None
    for p in prices:
        if not isinstance(p, dict):
            continue
        val = p.get("value")
        try:
            n = float(val) if val is not None else None
        except (TypeError, ValueError):
            n = None
        if n is None:
            continue
        pt = (p.get("priceType") or "").lower()
        if pt and pt not in by_type:
            by_type[pt] = n
        if fallback is None:
            fallback = n
    for key in ("min_rent", "rate", "market_rent"):
        if by_type.get(key) is not None:
            return int(by_type[key])  # type: ignore[arg-type]
    return int(fallback) if fallback is not None else None


def _sqft_to_int(val: Any) -> int | None:
    """G5 sqft can be int or display string like '950'."""
    if val is None:
        return None
    if isinstance(val, int):
        return val if 50 < val < 20_000 else None
    if isinstance(val, str):
        m = re.search(r"(\d{2,5})", val)
        if m:
            n = int(m.group(1))
            return n if 50 < n < 20_000 else None
    return None


def parse_g5_apartments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert G5's ``apartmentComplex.apartments`` list into unit dicts."""
    ac = (payload.get("data") or {}).get("apartmentComplex") or {}
    apts = ac.get("apartments") or []
    out: list[dict[str, Any]] = []
    for a in apts:
        if not isinstance(a, dict):
            continue
        rent = _price_to_int(a.get("prices"))
        if not rent or not (200 <= rent <= 50_000):
            continue
        fp = a.get("floorplan") or {}
        beds = fp.get("beds")
        baths = fp.get("baths")
        sqft = _sqft_to_int(fp.get("sqft")) or _sqft_to_int(a.get("sqftDisplay"))
        unit_number = a.get("name") or a.get("displayName") or ""
        avail = a.get("availabilityDate") or ""
        out.append(
            {
                "unit_number": str(unit_number),
                "floor_plan_name": str(fp.get("name") or ""),
                "bedrooms": str(beds) if beds is not None else "",
                "bathrooms": str(baths) if baths is not None else "",
                "sqft": str(sqft) if sqft else "",
                "market_rent_low": rent,
                "market_rent_high": rent,
                "rent_range": str(rent),
                "availability_status": "AVAILABLE",
                "availability_date": str(avail)[:30],
                "building": str(a.get("building") or ""),
                "extraction_tier": _TIER_BASE,
            }
        )
    return out


class G5Adapter:
    """Adapter for G5-hosted multifamily marketing sites.

    G5 sites embed unit data via JS calls to ``inventory.g5marketingcloud.com``
    that PropAi's network capture often misses (the call fires after the
    settle window). The adapter discovers the property URN from any
    ``g5-cl-...`` slug in the rendered HTML and queries the GraphQL API
    directly. No browser required for the API call itself.
    """

    pms_name: str = "g5"
    _fingerprints: list[str] = [
        "inventory.g5marketingcloud.com",
        "g5-assets-cld-res.cloudinary.com",
        "g5marketingcloud",
    ]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Discover URN → fetch units → emit AdapterResult."""
        result = AdapterResult(tier_used=_TIER_BASE)

        fr = getattr(ctx, "fetch_result", None)
        body = getattr(fr, "body", None) if fr is not None else None
        html = ""
        if isinstance(body, bytes):
            try:
                html = body.decode("utf-8", errors="replace")
            except Exception:
                html = ""
        elif isinstance(body, str):
            html = body

        urn = find_g5_urn(html) if html else None
        if not urn:
            result.tier_used = _TIER_NO_URN
            result.errors.append("g5-adapter: no g5-cl-... URN in rendered HTML")
            return result

        try:
            payload = await _fetch_g5_units(urn)
        except Exception as exc:
            result.tier_used = _TIER_API_ERROR
            result.errors.append(
                f"g5-api-error: urn={urn!r} {type(exc).__name__}: {str(exc)[:120]}"
            )
            return result

        if not payload:
            result.tier_used = _TIER_API_ERROR
            result.errors.append(f"g5-api: empty response for urn={urn!r}")
            return result

        units = parse_g5_apartments(payload)
        if not units:
            result.tier_used = _TIER_EMPTY
            result.errors.append(
                f"g5-api: returned 0 parseable units for urn={urn!r} "
                "(property may have no live inventory)"
            )
            return result

        result.units = units
        result.winning_url = f"{_G5_ENDPOINT} (urn={urn})"
        result.confidence = min(0.95, 0.7 + 0.02 * len(units))
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)


async def _fetch_g5_units(urn: str) -> dict[str, Any] | None:
    """Hit the G5 GraphQL endpoint with our units query."""
    import httpx

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"query": _G5_UNITS_QUERY, "variables": {"urn": urn}}
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
        r = await c.post(_G5_ENDPOINT, json=payload, headers=headers)
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except Exception:
            return None
