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
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import make_unit_dict
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

_TIER = "TIER_1_API_ESSEX"

# /api/properties/<pid>/units/<uid>/availability
_AVAIL_URL_RE = re.compile(
    r"/api/properties/\d+/units/\d+/availability", re.IGNORECASE
)
_MONEY_RE = re.compile(r"[\d.]+")


def _rent_to_int(val: Any) -> int | None:
    """``"2487.00"`` → 2487; junk/empty → None."""
    if val is None:
        return None
    m = _MONEY_RE.search(str(val))
    if not m:
        return None
    try:
        return int(round(float(m.group(0))))
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
        return _is_essex_availability(body, "")

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_TIER)
        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
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
