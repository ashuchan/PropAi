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
# 2026-05-20: ``\\?`` before each quote class so the regex also matches the
# JSON-escaped form ``knockDoorway.init(\"a8e...\",\"community\",\"69e...\")``
# emitted by Next.js / Nuxt SSR bundles (the call is inside a string-encoded
# JS source until the browser executes the bundle). Confirmed live against
# 3 raw-fetched HTMLs (flatirondistrictataustinranch, altaaptstarga,
# unionthompson) on 2026-05-20: original regex 0/3, relaxed regex 3/3.
_KNOCK_INIT_RE = re.compile(
    r"knockDoorway\.init\s*\(\s*\\?['\"]([a-f0-9]{20,40})\\?['\"]\s*,\s*"
    r"\\?['\"](community|application|public)\\?['\"]\s*,\s*"
    r"\\?['\"]([a-zA-Z0-9_-]{8,40})\\?['\"]",
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
    """Backward-compat wrapper — returns only the unit-level rows.

    Equivalent to ``parse_knock_payload(...)[0]``. Existing callers that
    only need unit rows (e.g. internal tests, third-party consumers) keep
    working unchanged. New code should call :func:`parse_knock_payload`
    so it also receives plan-level summaries for layouts with no
    available units.
    """
    units, _plans = parse_knock_payload(units_payload)
    return units


def parse_knock_payload(
    units_payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert Knock's ``units_data`` envelope into (units, plan_summaries).

    Two outputs:

    * ``units`` — one per available unit. Skips units flagged
      ``hidden`` / ``leased`` / ``reserved`` (no useful rent) and filters
      out records without a price in the $200-$50K range.
    * ``plan_summaries`` — one per ``layout`` (floor plan) that did NOT
      contribute any unit to the ``units`` list. Layouts with at least
      one surviving unit row are skipped — preserving the "no duplicate
      between unit and floor-plan rows" invariant. Rows have no
      ``unit_number`` so the post-process classifier (or downstream v2
      formatter) routes them under ``floor_plans[]``.

    The Knock community-API returns BOTH a per-unit ``units`` array and
    a parent ``layouts`` array describing every advertised floor plan.
    Pre-2026-05-22 only ``units`` was surfaced; layouts with zero
    available units (or whose units were all filtered as
    hidden/leased/reserved/no-rent) silently disappeared from the
    output. Sierra Vista (PID 77913) was the canonical case: 5 layouts,
    1 unit emitted, 0 floor_plans surfaced.
    """
    units: list[dict[str, Any]] = []
    units_data = units_payload.get("units_data", {})
    raw_units = units_data.get("units") or []
    layouts_list = units_data.get("layouts") or []
    layouts: dict[Any, dict[str, Any]] = {
        layout.get("id"): layout for layout in layouts_list if isinstance(layout, dict)
    }
    # Track which layout ids end up represented by an emitted unit row,
    # so the plan_summaries pass can skip them (no duplication).
    covered_layout_ids: set[Any] = set()

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

        # 2026-05-19 capture-first: Knock payload carries concession as
        # `SpecialsDescription`/specials on the unit or layout. Was
        # unmapped (Knock = 9k genuine units, 0% concession). Alias-
        # tolerant; raw; empty when no active special (correct).
        concession = ""
        for _src in (u, layout):
            for _ck in ("SpecialsDescription", "specialsDescription",
                        "specials_description", "specials", "special",
                        "concession", "concessions", "leasingSpecial",
                        "incentive", "promotion", "offer"):
                _cv = _src.get(_ck) if isinstance(_src, dict) else None
                if isinstance(_cv, str) and _cv.strip():
                    concession = _cv.strip()
                    break
            if concession:
                break

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
                "concession": concession,
                "extraction_tier": "TIER_1_KNOCK_API",
            }
        )
        if layout_id is not None:
            covered_layout_ids.add(layout_id)

    # 2026-05-22 Fix 3 — layouts without an emitted unit become plan_summaries.
    # Iterate the original list (not the dict) so source order is preserved.
    plan_summaries: list[dict[str, Any]] = []
    for layout in layouts_list:
        if not isinstance(layout, dict):
            continue
        lid = layout.get("id")
        if lid is None or lid in covered_layout_ids:
            continue

        # Layout rent: Knock returns various shapes — minPrice / maxPrice
        # on the layout, or marketRent / askingPrice. Defensive: fall back
        # to None when nothing parses (the row is still useful as a
        # plan_summary without rent — the website lists the floor plan).
        layout_rent_low = (
            _to_int(layout.get("minPrice"))
            or _to_int(layout.get("min_price"))
            or _to_int(layout.get("price"))
            or _to_int(layout.get("marketRent"))
            or _to_int(layout.get("askingPrice"))
        )
        layout_rent_high = (
            _to_int(layout.get("maxPrice"))
            or _to_int(layout.get("max_price"))
            or layout_rent_low
        )
        # Reject obviously bogus rents (same gate as the unit path).
        if layout_rent_low is not None and not (200 <= layout_rent_low <= 50_000):
            layout_rent_low = None
        if layout_rent_high is not None and not (200 <= layout_rent_high <= 50_000):
            layout_rent_high = None

        layout_sqft = (
            _to_int(layout.get("area"))
            or _to_int(layout.get("square_feet"))
            or _to_int(layout.get("sqft"))
        )
        layout_beds = layout.get("bedrooms")
        layout_baths = layout.get("bathrooms")
        plan_summaries.append(
            {
                "unit_number": "",  # required: empty → classify() returns "plan"
                "floor_plan_name": str(layout.get("name") or ""),
                "bedrooms": str(layout_beds) if layout_beds is not None else "",
                "bathrooms": str(layout_baths) if layout_baths is not None else "",
                "sqft": str(layout_sqft) if layout_sqft else "",
                "market_rent_low": layout_rent_low,
                "market_rent_high": layout_rent_high,
                "rent_range": (
                    str(layout_rent_low)
                    if layout_rent_low and layout_rent_low == layout_rent_high
                    else (
                        f"{layout_rent_low}-{layout_rent_high}"
                        if layout_rent_low and layout_rent_high
                        else ""
                    )
                ),
                "availability_status": "UNKNOWN",
                "availability_date": "",
                "building": "",
                "concession": "",
                "extraction_tier": "TIER_1_KNOCK_API",
                "floor_plan_id": str(lid),
            }
        )

    return units, plan_summaries


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

        Two extraction paths:

        1. **knockDoorway.init() in static HTML** (existing, 2026-04-30):
           extract ``(public_key, kind, comm_id)`` from the JS init call,
           then call ``/v1/property/community/{comm_id}``.

        2. **By-domain resolver** (added 2026-05-20 per
           ``project_jsonld_recovery_2026-05-20.md``): when the init call
           isn't in static HTML — common on Aspen Square / brand portfolio
           sites where Knock loads dynamically — query
           ``/v1/profile?code=w&domain={SITE_URL}`` to resolve a property_id
           directly from the marketing-site URL, then hit
           ``/v1/property/{property_id}/units``.

        The ``page`` argument is unused — Knock units come from a public
        JSON API, no rendering is required.
        """
        from ma_poc.pms.adapters._adapter_telemetry import log_adapter_stage

        pid_for_log = str(getattr(ctx, "property_id", "") or "unknown")
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

        # Path 1: knockDoorway.init() in static HTML.
        public_key, kind, comm_id = find_knock_ids(html) if html else (None, None, None)
        log_adapter_stage(
            "knock",
            pid_for_log,
            "ids_search",
            "found_static" if (public_key and comm_id) else "no_static_ids",
            reason=(
                f"public_key={'set' if public_key else 'none'} "
                f"comm_id={comm_id!r} kind={kind!r} body_len={len(html)}"
            ),
            has_static_init=bool(public_key and comm_id),
        )
        if public_key and comm_id:
            try:
                units, plan_summaries = await _fetch_knock_units(
                    comm_id, kind or "community", ctx=ctx
                )
            except Exception as exc:
                result.errors.append(f"knock-api-error: {exc}")
                units, plan_summaries = [], []
            # 2026-05-22 Fix 3: emit even when ``units`` is empty, provided
            # plan_summaries contains floor-plan-only rows. Knock returns
            # ``layouts`` (advertised floor plans) alongside ``units``; a
            # property with 0 currently-available units but ≥1 layout is
            # still a successful extraction at the plan-summary granularity.
            if units or plan_summaries:
                result.units = units
                result.plan_summaries = plan_summaries
                result.winning_url = (
                    f"https://doorway-api.knockrentals.com/v1/property/community/{comm_id}"
                )
                result.confidence = min(
                    0.9, 0.6 + 0.02 * (len(units) + 0.5 * len(plan_summaries))
                )
                return result
            result.errors.append("knock-adapter: Doorway API returned no units for community_id")

        # Path 2: by-domain resolver. Trigger when the static HTML doesn't
        # have the init call AND there's a credible signal that Knock is
        # the primary inventory backend (NOT just a UTM tracking tag on a
        # RentCafe-hosted site — see project_jsonld_recovery memo's
        # utm_knock-is-red-herring rule).
        base_url = str(getattr(ctx, "base_url", "") or "")
        if base_url and _should_try_knock_by_domain(html, base_url):
            try:
                pid, units, plan_summaries = await _fetch_knock_units_by_domain(
                    base_url, html, ctx=ctx
                )
            except Exception as exc:
                result.errors.append(
                    f"knock-by-domain-error: {type(exc).__name__}: {str(exc)[:120]}"
                )
                pid, units, plan_summaries = None, [], []
            if pid and (units or plan_summaries):
                result.units = units
                result.plan_summaries = plan_summaries
                result.winning_url = (
                    f"https://doorway-api.knockrentals.com/v1/property/{pid}/units"
                )
                result.tier_used = "TIER_1_KNOCK_API_BY_DOMAIN"
                result.confidence = min(
                    0.9, 0.6 + 0.02 * (len(units) + 0.5 * len(plan_summaries))
                )
                return result
            if pid is None:
                result.errors.append(
                    "knock-by-domain: /v1/profile resolver returned no property_id"
                )
            elif not units and not plan_summaries:
                result.errors.append(
                    f"knock-by-domain: property_id={pid} /units returned no units"
                )

        if not (public_key and comm_id) and not result.units:
            result.errors.append("knock-adapter: no knockDoorway.init() call in HTML")
        return result


async def _fetch_knock_units(
    comm_id: str,
    kind: str = "community",
    ctx: Any | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Two-step Doorway API fetch: community → numeric_id → units.

    Returns ``(units, plan_summaries)`` — see :func:`parse_knock_payload`.

    2026-05-24 R1 sweep fix: route through ``probe_get(ctx=ctx,
    stage="knock_probe")`` instead of bare ``httpx.AsyncClient``. The
    doorway-api.knockrentals.com host is CF-fronted; bare-httpx requests
    from cloud-run egress IPs are rate-limited / occasionally blocked.
    Routing through the gate lets ``PROBE_PROXY_URL`` + Web-Unlocker
    escalation handle the CF challenge transparently.
    """
    from ma_poc.pms.adapters._probe import probe_get

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
        try:
            r = probe_get(
                units_url, ctx=ctx, stage="knock_probe",
                headers=headers, timeout=15,
            )
        except Exception:
            return [], []
        if getattr(r, "status_code", None) != 200:
            return [], []
        try:
            return parse_knock_payload(r.json())
        except Exception:
            return [], []

    # Community-keyed: fetch property meta first.
    community_url = f"{base}/community/{comm_id}"
    try:
        r = probe_get(
            community_url, ctx=ctx, stage="knock_probe",
            headers=headers, timeout=15,
        )
    except Exception:
        return [], []
    if getattr(r, "status_code", None) != 200:
        return [], []
    try:
        prop_data = r.json().get("property") or {}
    except Exception:
        return [], []
    numeric_id = prop_data.get("id")
    if not numeric_id:
        return [], []
    units_url = f"{base}/{numeric_id}/units"
    try:
        r2 = probe_get(
            units_url, ctx=ctx, stage="knock_probe",
            headers=headers, timeout=15,
        )
    except Exception:
        return [], []
    if getattr(r2, "status_code", None) != 200:
        return [], []
    try:
        return parse_knock_payload(r2.json())
    except Exception:
        return [], []


# 2026-05-20: Knock-by-domain fallback signal detection.
#
# The JSON-LD recovery probe (project_jsonld_recovery_2026-05-20.md)
# confirmed two reliable signals AND one red-herring:
#
#   ✓ doorway-api.knockrentals.com URL in static HTML (any context) → Knock
#   ✓ knockrentals.com/widget URL → Knock
#   ✓ Aspen Square brand portfolio (aspensquare.com/apartments/{state}/{city}/{slug})
#     — verified live: every Aspen Square site has Knock as primary inventory
#   ✗ ``?utm_knock=`` URL param ALONE is NOT reliable — RentCafe-hosted
#     properties (10X Iona Lakes, Main Street Square) use this UTM for lead
#     tracking while their actual inventory lives in RentCafe + SecureCafe.
#     Only treat utm_knock as a Knock signal when RentCafe is ABSENT.

_KNOCK_API_HOST_RE = re.compile(
    r"doorway-api\.knockrentals\.com|knockrentals\.com/widget", re.IGNORECASE
)
_RENTCAFE_PRESENCE_RE = re.compile(r"resource\.rentcafe\.com", re.IGNORECASE)
_ASPEN_SQUARE_URL_RE = re.compile(
    r"https?://(?:www\.)?aspensquare\.com/apartments/[a-z-]+/[a-z-]+/[a-z0-9-]+",
    re.IGNORECASE,
)

# 2026-05-21 port (Fix 5a): Aspen Square + similar Knock-backed brands
# embed the Knock community hash in their static SSR HTML as a
# JSON-stringified config blob — typically inside a Next.js bundle or
# React Server Component data dump. The actual call is
# ``knockDoorway.init(<api_token>, "community", <community_hash>)`` but
# that call is INSIDE a string-encoded JS module that the browser
# executes — so the literal init call doesn't appear in static HTML.
# What DOES appear is the SSR-emitted config:
#
#   ":\"community\",\"enabled\":true,\"propertyId\":\"<16-char hex>\","
#   "apiToken\":\"<32-char hex>\""
#
# Captures both unescaped (``"propertyId":"<hex>"``) and JSON-escaped
# (``\"propertyId\":\"<hex>\"``) forms. Verified live 2026-05-20 against
# 4 Aspen Square properties (Adley 72nd, The Avenue Cabot, Edgewood
# Court, Country Manor) — all four carry a UNIQUE community hash + the
# SHARED Aspen Square api_token.
_KNOCK_COMMUNITY_HASH_RE = re.compile(
    r'propertyId\\?"\s*:\s*\\?"([a-f0-9]{14,18})\\?"', re.IGNORECASE
)


def find_knock_community_hash(html: str) -> str | None:
    """Extract the Knock community hash from a JSON-embedded config blob.

    Returns the 16-char hex hash on match, ``None`` otherwise. Used by the
    by-domain resolver for sites (like Aspen Square) that don't fire the
    ``knockDoorway.init()`` literal call in static HTML but DO ship the
    config object via SSR.
    """
    if not html:
        return None
    m = _KNOCK_COMMUNITY_HASH_RE.search(html)
    return m.group(1) if m else None


def _should_try_knock_by_domain(html: str, base_url: str) -> bool:
    """Decide whether to fire the Knock-by-domain resolver.

    Returns ``True`` only when there's positive evidence Knock is the
    primary inventory backend (not just a UTM-tracking layer on a
    RentCafe-hosted property).
    """
    # Aspen Square brand always uses Knock — match the URL directly without
    # requiring the JS bundle to expose the API host in static HTML.
    if base_url and _ASPEN_SQUARE_URL_RE.match(base_url):
        return True
    if not html:
        return False
    lo = html.lower()
    # Hard signal: Knock API host referenced anywhere in the HTML.
    if _KNOCK_API_HOST_RE.search(html):
        # Exclude RentCafe-hosted properties — Knock is just lead tracking
        # there, the inventory lives in RentCafe / SecureCafe.
        if _RENTCAFE_PRESENCE_RE.search(html):
            return False
        return True
    # Soft signal: ``utm_knock=`` URL param, but only when there's no
    # RentCafe CDN load (otherwise it's the utm_knock red-herring case).
    if "utm_knock=" in lo and not _RENTCAFE_PRESENCE_RE.search(html):
        return True
    return False


async def _fetch_knock_units_by_domain(
    base_url: str,
    html: str = "",
    ctx: Any | None = None,
) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve property_id from marketing-site URL via Knock Doorway,
    then fetch units.

    Two-path strategy (2026-05-21 port from feature branch):

    1. **Community-hash path** (preferred, verified 4/4 on Aspen Square):
       Extract the community hash from ``html`` (SSR-embedded JSON
       config), call ``/v1/property/community/{hash}`` to bootstrap and
       get the numeric property_id, then call ``/v1/property/{pid}/units``.
       This is the ONLY path that works on Aspen Square sites — the
       ``/v1/profile?domain=`` resolver returns 400 without a community
       bootstrap first.

    2. **Profile-by-domain path** (legacy fallback): call ``/v1/profile``
       with the URL as the ``domain`` param. Works on sites that publish
       the API host in static HTML but don't ship the SSR config blob.

    Args:
        base_url: The property's marketing-site URL.
        html: Static HTML body (optional). When supplied, the community-
            hash path is tried first.

    Returns:
        ``(property_id, units)``. ``property_id`` is ``None`` when both
        paths return nothing; ``units`` is the parsed list (may be empty
        if the property exists in Knock but has no available units).

    Never raises — exceptions return ``(None, [])``.
    """
    # 2026-05-24 R1 sweep fix: route every Knock Doorway call through
    # ``probe_get(ctx, stage="knock_probe")`` so the proxy gate fires
    # uniformly across the community-hash and profile-by-domain paths.
    from urllib.parse import quote

    from ma_poc.pms.adapters._probe import probe_get

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Origin": "https://doorway.knck.io",
        "Accept": "application/json",
    }

    def _fetch_units(pid_str: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        units_url = f"https://doorway-api.knockrentals.com/v1/property/{pid_str}/units"
        try:
            ur = probe_get(
                units_url, ctx=ctx, stage="knock_probe",
                headers=headers, timeout=15,
            )
        except Exception:
            return [], []
        if getattr(ur, "status_code", None) != 200:
            return [], []
        try:
            return parse_knock_payload(ur.json())
        except Exception:
            return [], []

    try:
        # Path 1: community-hash from SSR-embedded JSON config.
        community_hash = find_knock_community_hash(html) if html else None
        if community_hash:
            boot_url = (
                f"https://doorway-api.knockrentals.com/v1/property/"
                f"community/{community_hash}"
            )
            try:
                br = probe_get(
                    boot_url, ctx=ctx, stage="knock_probe",
                    headers=headers, timeout=15,
                )
            except Exception:
                br = None
            if br is not None and getattr(br, "status_code", None) == 200:
                try:
                    boot_body = br.json()
                except Exception:
                    boot_body = {}
                pid = (boot_body.get("property") or {}).get("id")
                if pid:
                    pid_str = str(pid)
                    units, plans = _fetch_units(pid_str)
                    return pid_str, units, plans

        # Path 2: legacy /v1/profile?domain= resolver.
        profile_url = (
            "https://doorway-api.knockrentals.com/v1/profile"
            f"?code=w&domain={quote(base_url, safe='')}&refresh=true"
        )
        try:
            pr = probe_get(
                profile_url, ctx=ctx, stage="knock_probe",
                headers=headers, timeout=15,
            )
        except Exception:
            return None, [], []
        if getattr(pr, "status_code", None) != 200:
            return None, [], []
        try:
            profile_body = pr.json()
        except Exception:
            return None, [], []
        pid = (profile_body.get("profile") or {}).get("property")
        if not pid:
            return None, [], []
        pid_str = str(pid)
        units, plans = _fetch_units(pid_str)
        return pid_str, units, plans
    except Exception:
        return None, [], []
