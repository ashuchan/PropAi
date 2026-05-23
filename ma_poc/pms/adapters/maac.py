"""
MAAC (Mid-America Apartment Communities) adapter.

Research log
------------
Single REIT, single host (maac.com). Next.js/Vercel marketing app whose
community page server-renders only an i18n label dictionary in
``__NEXT_DATA__`` — the per-unit rent is loaded client-side from a
public same-origin JSON API.

Real payload inspected (user DevTools capture, 2026-05-18):
  GET https://www.maac.com/api/properties/{ULID}/units/available/
  -> 200, application/json, list[ ~89 unit objects ], NO auth / NO cookie.
  Unit object (confirmed): apartmentName ("3572TB"), minimumRent (1575),
  maximumRent (2095), floorplanName, beds, baths, sqft, availableDate,
  specials[], floor, building, rentCafePropertyId ("611784").

The ``{ULID}`` (26-char Crockford ULID, ``propertyIntegrationID``) is
embedded in the community page HTML next to the numeric propertyID:
  "propertyID":{"value":"611784"},"propertyIntegrationID":"01JQW6W1..."
(HTML-entity-encoded as ``&quot;`` in the server markup).

Recipe (fully deterministic, no render strictly required):
  1. read community-page HTML (already-loaded page)
  2. regex propertyIntegrationID -> ULID
  3. GET /api/properties/{ULID}/units/available/ via the page's browser
     context (inherits cookies/proxy; clears Cloudflare)
  4. parse list -> unit dicts -> post_process
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

OLL_TIER = "TIER_1_API_MAAC"

# propertyIntegrationID":"01JQW6W1SB8ZEZHCDC99BEDPVF" — tolerant of both
# raw JSON quotes and HTML-entity-encoded (&quot;) server markup.
_INTEGRATION_RE = re.compile(
    r"propertyIntegrationID(?:&quot;|&#34;|[\"'])\s*:\s*"
    r"(?:&quot;|&#34;|[\"'])(01[0-9A-HJKMNP-TV-Z]{24})",
    re.IGNORECASE,
)
_ULID_FALLBACK_RE = re.compile(r"\b(01[0-9A-HJKMNP-TV-Z]{24})\b")


def parse_maac_units(items: list[dict[str, Any]], url: str) -> list[dict[str, str]]:
    """Parse MAAC ``units/available`` objects into standard unit dicts.

    Rent IS per-unit here (minimumRent/maximumRent), so no summary
    fallback is needed — unlike AvalonBay.
    """
    units: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        unit_no = str(
            item.get("apartmentName")
            or item.get("unitName")
            or item.get("unitNumber")
            or item.get("id")
            or ""
        ).strip()
        fp_name = str(item.get("floorplanName") or item.get("floorPlanName") or "").strip()
        beds_raw = item.get("beds")
        baths_raw = item.get("baths")
        try:
            beds = int(float(beds_raw)) if beds_raw not in (None, "") else None
        except (TypeError, ValueError):
            beds = None
        try:
            baths = float(baths_raw) if baths_raw not in (None, "") else None
        except (TypeError, ValueError):
            baths = None
        sqft_raw = item.get("sqft")
        sqft = str(sqft_raw) if sqft_raw not in (None, "", 0) else ""

        rent_lo = money_to_int(str(item.get("minimumRent") or "")) or None
        rent_hi = money_to_int(str(item.get("maximumRent") or "")) or None
        if rent_lo is None and rent_hi is not None:
            rent_lo = rent_hi
        if rent_hi is None and rent_lo is not None:
            rent_hi = rent_lo

        avail_date = str(item.get("availableDate") or "").strip()
        if avail_date and "T" in avail_date:
            avail_date = avail_date.split("T")[0]

        concession = ""
        specials = item.get("specials")
        if isinstance(specials, list) and specials:
            first = specials[0]
            if isinstance(first, dict):
                concession = str(
                    first.get("title")
                    or first.get("specialTitle")
                    or first.get("description")
                    or ""
                ).strip()
            elif isinstance(first, str):
                concession = first.strip()
        elif isinstance(specials, str):
            concession = specials.strip()

        floor = item.get("floor")
        building = item.get("building")

        units.append(
            make_unit_dict(
                floor_plan_name=fp_name,
                bed_label=bed_label_from(beds, fp_name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=(
                    str(int(baths)) if baths is not None and baths == int(baths)
                    else (str(baths) if baths is not None else "")
                ),
                sqft=sqft,
                unit_number=unit_no,
                floor=str(floor) if floor not in (None, "") else "",
                building=str(building) if building not in (None, "") else "",
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi,
                concession=concession,
                availability_status="AVAILABLE",
                availability_date=avail_date,
                source_api_url=url,
                extraction_tier=OLL_TIER,
            )
        )
    return units


def _extract_integration_id(html: str) -> str | None:
    """Pull the MAAC ``propertyIntegrationID`` ULID from community HTML.

    Prefer the labelled match; fall back to the first non-unit ULID only
    if exactly one distinct ULID is present (avoids picking a unit id).
    """
    m = _INTEGRATION_RE.search(html or "")
    if m:
        return m.group(1)
    distinct = sorted(set(_ULID_FALLBACK_RE.findall(html or "")))
    if len(distinct) == 1:
        return distinct[0]
    return None


def _units_from_body(body: Any) -> list[dict[str, Any]]:
    """Locate the unit list in a MAAC API body (top-level list or nested)."""
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return body
    if isinstance(body, dict):
        for key in ("units", "availableUnits", "data", "results", "response"):
            cand = body.get(key)
            if isinstance(cand, list) and cand and isinstance(cand[0], dict):
                return cand
            if isinstance(cand, dict):
                for k2 in ("units", "availableUnits", "results"):
                    c2 = cand.get(k2)
                    if isinstance(c2, list) and c2 and isinstance(c2[0], dict):
                        return c2
    return []


def _is_maac_units(items: list[dict[str, Any]]) -> bool:
    if not items:
        return False
    first = items[0]
    return isinstance(first, dict) and (
        "apartmentName" in first
        or ("minimumRent" in first and "floorplanName" in first)
    )


async def _community_html(page: Page | None, ctx: AdapterContext) -> str:
    """Best raw HTML for ULID extraction.

    ``propertyIntegrationID`` is HTML-entity-encoded in the RAW server
    markup but is consumed/transformed in the hydrated DOM, so prefer the
    raw fetch body and a curl_cffi refetch over ``page.content()``.
    """
    fr = getattr(ctx, "fetch_result", None)
    body = getattr(fr, "body", None)
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:
            body = None
    if isinstance(body, str) and "propertyIntegrationID" in body:
        return body
    base = getattr(ctx, "base_url", "") or ""
    if base:
        try:
            from ma_poc.pms.adapters._probe import probe_get

            # Same-origin (base is ctx.base_url) — gate Layer 1 declines proxy.
            r = probe_get(base, ctx=ctx, stage="maac_community", timeout=20)
            if getattr(r, "status_code", 0) == 200 and r.text:
                return str(r.text)
        except Exception:
            pass
    if isinstance(body, str) and body:
        return body
    if page is not None:
        try:
            return await page.content()
        except Exception:
            return ""
    return ""


async def _active_fetch_maac(page: Page | None, ctx: AdapterContext) -> list[dict[str, Any]]:
    """Resolve the ULID from raw community HTML and GET the public units API.

    The units API (``/api/properties/{ULID}/units/available/``) is public
    (no auth, no cookie — confirmed), so curl_cffi via ``probe_get`` is
    the primary path. Falls back to the browser context. Never raises.
    """
    out: list[dict[str, Any]] = []
    html = await _community_html(page, ctx)
    ulid = _extract_integration_id(html)
    if not ulid:
        return out
    url = f"https://www.maac.com/api/properties/{ulid}/units/available/"

    try:
        from ma_poc.pms.adapters._probe import probe_get

        # url is www.maac.com/api/... — typically same-eTLD+1 with the
        # property's marketing host (maac.com tenants live on maac.com/...).
        # Gate Layer 1 (same_origin) declines proxy in the common case.
        # Cross-tenant edge cases (rare) fall through to Layer 4.
        r = probe_get(
            url,
            ctx=ctx,
            stage="maac_units",
            timeout=20,
            headers={"accept": "application/json, text/plain, */*"},
        )
        if getattr(r, "status_code", 0) == 200:
            body = r.json()
            items = _units_from_body(body)
            if _is_maac_units(items):
                out.append({"url": url, "body": items})
                return out
    except Exception:
        pass

    if page is not None:
        try:
            resp = await page.context.request.get(
                url,
                headers={
                    "accept": "application/json, text/plain, */*",
                    "x-requested-with": "XMLHttpRequest",
                },
                timeout=15000,
            )
            if resp.ok:
                items = _units_from_body(await resp.json())
                if _is_maac_units(items):
                    out.append({"url": url, "body": items})
        except Exception:
            return out
    return out


class MaacAdapter:
    """Mid-America Apartment Communities (MAAC) PMS adapter."""

    pms_name: str = "maac"
    _fingerprints: list[str] = ["maac.com", "propertyIntegrationID"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units via the public ``/api/properties/{ULID}/units/available/``.

        Fast path: a passively-captured units response (pipeline already
        on the available-apartments page). Otherwise actively resolve the
        ULID from the community HTML and fetch the API.
        """
        result = AdapterResult(tier_used=OLL_TIER)
        all_units: list[dict[str, str]] = []

        sources: list[dict[str, Any]] = []
        for resp in getattr(ctx, "_api_responses", []) or []:
            items = _units_from_body(resp.get("body"))
            if _is_maac_units(items):
                sources.append({"url": resp.get("url", ""), "body": items})

        if not sources:
            sources = await _active_fetch_maac(page, ctx)

        for src in sources:
            parsed = parse_maac_units(src["body"], src.get("url", ""))
            if parsed:
                all_units.extend(parsed)
                result.api_responses.append(src)

        if all_units:
            from ma_poc.extraction.post_process import post_process

            _pp_parsed = len(all_units)
            _pp = post_process(all_units, property_id=getattr(ctx, "property_id", None))
            if _pp.n_admitted > 0:
                result.units = _pp.admitted
                result.plan_summaries = _pp.plan_summaries
                result.winning_url = (
                    result.api_responses[0].get("url") if result.api_responses else None
                )
                result.confidence = min(0.90, 0.7 + 0.05 * _pp.n_admitted)
            else:
                result.confidence = 0.0
                result.errors.append(
                    f"MAAC_VALIDITY_REJECTED: {_pp_parsed} parsed rows "
                    f"failed unit_validity"
                )
        else:
            result.confidence = 0.0
            result.errors.append(
                "No MAAC unit data (propertyIntegrationID not found or API empty)"
            )

        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
