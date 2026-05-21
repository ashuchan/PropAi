"""Essex Property Trust adapter — UNIT-LEVEL (browser-intercept).

Research log (2026-05-17, user DevTools capture verified)
---------------------------------------------------------
Essex (a public multifamily REIT, essexapartmenthomes.com, ~250
communities) is tagged ``rentcafe`` by the detector but in production
0 of these reached Tier-1 — the marketing site is a Next.js/Vercel app
that returns an empty shell to static/automated fetch (prod
``no_body_short_circuit``), and the public ``securecafe`` portal our
RentCafe adapter probes is only exposed behind resident login.

The real per-unit data is a clean same-origin JSON API:

  GET https://www.essexapartmenthomes.com/api/properties/{propertyId}
      /units/{unitId}/availability?date=YYYY-MM-DD
  (Next.js route /api/properties/[propertyId]/units/[unitId]/availability)

  Response:
    {success, result:{property_id, floorplan_id, unit_id,
      start_date, end_date,
      pricing_by_date:[{date:ISO,
        terms_by_month:[{term_months, rent:"2487.00",
                         deposit:"600.00", apply_url}]}]}}

  (The ``apply_url`` reveals the leasing backend is Nestio/Funnel —
   nestiolistings.com companyID=18855 — but the Essex API is the clean
   surface, so we parse it directly.)

Access constraint
-----------------
NO Authorization/Bearer; cookies present are analytics/consent only.
BUT the endpoint is behind **Vercel Firewall bot protection** — a
plain server-side curl returns HTTP 429 "Vercel Security Check".
Therefore, exactly like the RealPage OLL adapter, the only viable
strategy is **browser-based Tier-1 API interception**: the pipeline's
patchright browser renders the property's floor-plans-and-pricing page
(passing the Vercel challenge as a legit browser), which fires the
per-unit ``/availability`` calls; this adapter parses those responses
from the captured network log (``ctx._api_responses``). The request is
never forged server-side.

Verified: city-view (property 492967, unit 6302379, floorplan
2101784) → 12-month rent $2,487, earliest availability 2026-05-17.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
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

_TIER = "TIER_1_API_ESSEX"

# 2026-05-18 (user HAR): the BULK endpoint
#   GET /api/properties/{propertyId}/availability?start_date&end_date&format=spa
# returns ALL units in one call and — unlike the per-unit route — clears
# the Vercel bot check under curl_cffi chrome impersonation (verified
# 200, 8 floorplans / 25 units, property 492967). Shape:
#   {"result":{"floorplans":[{...,"units":[{unit_id,name,floorplan_name,
#     beds,baths,sqft,minimum_rent,maximum_rent,availability_date,
#     specials,amenities,floorplate:{floor,building_name}}]}]}}
_PROP_ID_RE = re.compile(
    r'(?:data-property-id="|/api/properties/|"propertyId"\s*[:=]\s*"?)(\d{5,7})',
    re.IGNORECASE,
)


def _is_essex_bulk(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    r = body.get("result")
    return isinstance(r, dict) and isinstance(r.get("floorplans"), list)


def parse_essex_bulk(body: dict[str, Any], source_url: str) -> list[dict[str, Any]]:
    """Bulk ``/availability?format=spa`` -> all unit-level dicts."""
    r = body.get("result")
    if not isinstance(r, dict):
        return []
    out: list[dict[str, Any]] = []
    for fp in r.get("floorplans") or []:
        if not isinstance(fp, dict):
            continue
        for u in fp.get("units") or []:
            if not isinstance(u, dict):
                continue
            unit_no = str(u.get("name") or u.get("unit_id") or "").strip()
            if not unit_no:
                continue
            fp_name = str(
                u.get("floorplan_name") or fp.get("name") or ""
            ).strip()
            beds_raw = u.get("beds")
            baths_raw = u.get("baths")
            try:
                beds = int(float(beds_raw)) if beds_raw not in (None, "") else None
            except (TypeError, ValueError):
                beds = None
            try:
                baths = float(baths_raw) if baths_raw not in (None, "") else None
            except (TypeError, ValueError):
                baths = None
            sqft_raw = u.get("sqft")
            sqft = str(sqft_raw) if sqft_raw not in (None, "", 0) else ""
            rent_lo = money_to_int(str(u.get("minimum_rent") or "")) or None
            rent_hi = money_to_int(str(u.get("maximum_rent") or "")) or None
            if rent_lo is None and rent_hi is not None:
                rent_lo = rent_hi
            if rent_hi is None and rent_lo is not None:
                rent_hi = rent_lo
            avail = str(u.get("availability_date") or "")
            avail = avail[:10] if "T" in avail else avail
            concession = ""
            sp = u.get("specials")
            if isinstance(sp, list) and sp:
                first = sp[0]
                concession = str(
                    first.get("title") or first.get("description") or ""
                ).strip() if isinstance(first, dict) else str(first).strip()
            fpl = u.get("floorplate") if isinstance(u.get("floorplate"), dict) else {}
            floor = fpl.get("floor")
            building = fpl.get("building_name")
            out.append(
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
                    availability_date=avail,
                    source_api_url=source_url,
                    extraction_tier=_TIER,
                )
            )
    return out


async def _active_fetch_essex_bulk(
    page: Page | None, ctx: AdapterContext
) -> list[dict[str, Any]]:
    """Resolve propertyId from community HTML and GET the bulk API.

    curl_cffi (via probe_get) clears the Vercel bot check for the bulk
    route. Never raises.
    """
    # raw community HTML (raw > hydrated for the embedded propertyId)
    html = ""
    fr = getattr(ctx, "fetch_result", None)
    body = getattr(fr, "body", None)
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:
            body = None
    if isinstance(body, str) and _PROP_ID_RE.search(body):
        html = body
    if not html:
        base = getattr(ctx, "base_url", "") or ""
        if base:
            try:
                from ma_poc.pms.adapters._probe import probe_get

                rr = probe_get(base, timeout=20)
                if getattr(rr, "status_code", 0) == 200 and rr.text:
                    html = str(rr.text)
            except Exception:
                pass
    if not html and page is not None:
        try:
            html = await page.content()
        except Exception:
            html = ""
    m = _PROP_ID_RE.search(html or "")
    if not m:
        return []
    pid = m.group(1)
    d0 = date.today()
    d1 = d0 + timedelta(days=60)
    url = (
        f"https://www.essexapartmenthomes.com/api/properties/{pid}"
        f"/availability?start_date={d0}&end_date={d1}&format=spa"
    )
    try:
        from ma_poc.pms.adapters._probe import probe_get

        r = probe_get(
            url,
            timeout=20,
            headers={
                "accept": "application/json, text/plain, */*",
                "referer": "https://www.essexapartmenthomes.com/",
            },
        )
        if getattr(r, "status_code", 0) == 200:
            b = r.json()
            if _is_essex_bulk(b):
                return [{"url": url, "body": b}]
    except Exception:
        return []
    return []

# /api/properties/<pid>/units/<uid>/availability
_AVAIL_URL_RE = re.compile(
    r"/api/properties/\d+/units/\d+/availability", re.IGNORECASE
)
# 2026-05-21: the prior regex ``[\d.]+`` matched only the first run of
# digits-and-dots, so ``"$1,500"`` returned 1 (the ``,`` broke the run).
# The Essex API typically emits dot-decimal (``"2487.00"``) so the bug
# was latent in production, but any future caller passing a thousand-
# separated string would silently get a garbage rent. The replacement
# matches an optional thousands-separated integer plus an optional
# decimal fraction, then strips commas before float-parsing. Same fix
# as ``_parsing.money_to_int`` (Commit 4 of MAY13_API_TIER_PORT_PLAN.md).
_MONEY_RE = re.compile(r"\d[\d,]*(?:\.\d{1,2})?")


def _rent_to_int(val: Any) -> int | None:
    """Coerce a money value to int.

    Accepts:
      * Dot-decimal: ``"2487.00"`` -> 2487
      * Thousands-separated: ``"$1,500"`` / ``"1,234.56"`` -> 1500 / 1235
      * Plain numerics: ``1500`` / ``"1500"`` -> 1500

    Returns ``None`` on missing / unparseable input. Never raises.
    """
    if val is None:
        return None
    m = _MONEY_RE.search(str(val))
    if not m:
        return None
    cleaned = m.group(0).replace(",", "")
    try:
        return int(round(float(cleaned)))
    except (TypeError, ValueError):
        return None


def _is_essex_availability(body: Any, url: str) -> bool:
    if not isinstance(body, dict):
        return False
    if not (body.get("success") and isinstance(body.get("result"), dict)):
        return False
    r = body["result"]
    return "pricing_by_date" in r and "unit_id" in r


def parse_essex_availability(body: dict[str, Any], source_url: str) -> list[dict[str, Any]]:
    """One Essex ``/availability`` response → at most one unit-level dict.

    The endpoint is per-unit. The canonical asking rent is the
    **12-month** term on the unit's earliest available date (the page
    headlines 12mo as "Best Value"; 1–3-month terms are inflated
    short-stay premiums and are NOT the asking rent). Availability date
    = the first ``pricing_by_date`` entry whose ``terms_by_month`` is
    non-empty (an empty list means the unit is not available that day).
    Returns [] when no date has any term (unit not currently available).
    """
    r = body.get("result")
    if not isinstance(r, dict):
        return []
    unit_id = r.get("unit_id")
    if unit_id in (None, "", 0):
        return []
    fp_id = r.get("floorplan_id")

    avail_iso = ""
    chosen_terms: list[dict[str, Any]] = []
    for entry in r.get("pricing_by_date") or []:
        if not isinstance(entry, dict):
            continue
        terms = entry.get("terms_by_month") or []
        if terms:
            avail_iso = str(entry.get("date") or "")[:10]
            chosen_terms = [t for t in terms if isinstance(t, dict)]
            break
    if not chosen_terms:
        return []

    # Prefer the 12-month term; else the longest available term (closest
    # to a standard lease, not a short-stay premium).
    by_term = {
        int(t.get("term_months") or 0): t
        for t in chosen_terms
        if t.get("term_months")
    }
    pick = by_term.get(12) or (
        by_term[max(by_term)] if by_term else chosen_terms[0]
    )
    rent = _rent_to_int(pick.get("rent"))
    if rent is None:
        return []
    deposit = pick.get("deposit")

    return [
        make_unit_dict(
            unit_number=str(unit_id),
            floor_plan_name=str(fp_id or ""),
            rent_low=rent,
            rent_high=rent,
            deposit=str(deposit or ""),
            availability_status="AVAILABLE",
            availability_date=avail_iso,
            source_api_url=source_url,
            extraction_tier=_TIER,
        )
    ]


class EssexAdapter:
    """Essex ``/api/properties/{id}/units/{id}/availability`` extractor.

    Browser-intercept only (Vercel-bot-gated): parses the per-unit
    availability responses the rendered floor-plans-and-pricing page
    fires, from ``ctx._api_responses``. One unit per response; dedup by
    unit_id across the captured set.
    """

    pms_name: str = "essex"
    _fingerprints: list[str] = [
        "essexapartmenthomes.com",
        "/api/properties/",
    ]

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    def matches_response_body(self, body: Any) -> bool:
        return _is_essex_availability(body, "") or _is_essex_bulk(body)

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_TIER)
        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])

        # Primary: bulk /availability?format=spa (passively captured or
        # actively fetched). One call returns every unit.
        bulk_sources: list[dict[str, Any]] = [
            {"url": str(rsp.get("url", "")), "body": rsp.get("body")}
            for rsp in api_responses
            if _is_essex_bulk(rsp.get("body"))
        ]
        if not bulk_sources:
            bulk_sources = await _active_fetch_essex_bulk(page, ctx)
        if bulk_sources:
            bulk_units: list[dict[str, Any]] = []
            for src in bulk_sources:
                try:
                    bulk_units.extend(parse_essex_bulk(src["body"], src.get("url", "")))
                except Exception as exc:  # noqa: BLE001 — adapters never raise
                    result.errors.append(
                        f"essex-bulk-parse-error: {type(exc).__name__}: {exc}"
                    )
            if bulk_units:
                result.units = bulk_units
                result.winning_url = bulk_sources[0].get("url") or None
                result.confidence = min(0.90, 0.7 + 0.05 * len(bulk_units))
                return result

        all_units: list[dict[str, Any]] = []
        seen: set[str] = set()
        for resp in api_responses:
            body = resp.get("body")
            url = str(resp.get("url", ""))
            if not (_AVAIL_URL_RE.search(url) or _is_essex_availability(body, url)):
                continue
            if not isinstance(body, dict):
                continue
            try:
                units = parse_essex_availability(body, url)
            except Exception as exc:  # noqa: BLE001 — never raise from an adapter
                result.errors.append(f"essex-parse-error: {type(exc).__name__}: {exc}")
                continue
            for u in units:
                key = str(u.get("unit_number") or "")
                if key and key not in seen:
                    seen.add(key)
                    all_units.append(u)
                    result.api_responses.append(resp)

        if all_units:
            result.units = all_units
            result.winning_url = (
                result.api_responses[0].get("url") if result.api_responses else None
            )
            result.confidence = min(0.90, 0.7 + 0.05 * len(all_units))
        else:
            result.confidence = 0.0
            result.errors.append(
                "No Essex /availability data in captured API responses"
            )
        return result
