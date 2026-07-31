"""RealPage CWS (Community Website Solution) — RPFP widget plan-level extractor.

Some RealPage-tagged sites use a CMS-style template called CWS (Community
Website Solution) that ships the "RPFP" (RealPage Floor Plans) widget
embedded inline on /Floor-Plans.aspx. The widget renders plan cards
client-side with all data in same-origin DOM — no XHR signature endpoint
to intercept.

Verified live 2026-05-21 on:
  - www.liveatpenthouse.com/Floor-Plans.aspx (CWS/2256871)
  - www.liveatshadowglen.com/Floor-Plans.aspx (CWS/2267476)

DOM contract:
  .rpfp-container.floorplans-widget-{N}
    .rpfp-filters ...
    .rpfp-body
      .rpfp-cards
        .rpfp-card.pricing-transparency-{boolean} × N (one per plan)
          .rpfp-card-inner ...
            .rpfp-info > .rpfp-info-top > .rpfp-details
              .rpfp-name      (plan name, e.g. "One Bedroom")
              .rent-container-details
              (card innerText format observed:
                "One Bedroom 1 Bed1 Bath 566 - 784 Sqft $2,100 - $2,120
                 Brochure Contact Us")

Two extraction paths:
  1. UNIT-LEVEL (flag ``ENABLE_CWS_GETUNITS``, 2026-07-19) — the property-hosted
     ``/CmsSiteManager/callback.aspx?act=Proxy/GetUnits&available=true`` proxy
     returns a clean ``{"units":[...]}`` roster (unitNumber/rent/squareFeet/
     numberOfBeds/floorplanName/internalAvailableDate). A static GET, no render,
     no siteid needed (property-hosted). Tried FIRST when the flag is on.
     **This refutes the long-standing note below** that CWS "doesn't publish a
     per-unit roster publicly" — it DOES, via the CmsSiteManager proxy.
     Live-verified identical across huntingtonwoods/keltonstation/thegarfield/
     capitalplace (roster-confirmation gap #3).
  2. PLAN-LEVEL (default) — the inline ``.rpfp-card`` widget below. The
     "CHECK AVAILABILITY" button links back to /Floor-Plans.aspx (no DOM drill)
     and "APPLY NOW" goes to the on-site.com application flow, so the DOM itself
     is plan-level only; the unit roster lives at the GetUnits proxy (path 1).
     Used as the fallback when GetUnits yields no available units.

Distinct from:
  * RealPageOllAdapter (handles ``leasing.realpage.com/RP.Leasing...``
    OLL workflow XHR) — different RealPage product.
  * Other ``.aspx`` legacy themes (e.g. ``.floorplan-block`` + ``.par-units``,
    handled by RentCafeUnitRosterAdapter).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import TYPE_CHECKING, Any

from ma_poc.config.feature_flags import enable_cws_getunits
from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

_RPFP_DOM_JS = r"""
async () => {
  const cont = document.querySelector('.rpfp-container');
  if (!cont) return {ok: false, reason: 'no .rpfp-container present'};
  const cards = Array.from(cont.querySelectorAll('.rpfp-card'));
  if (cards.length === 0) {
    return {ok: false, reason: '.rpfp-container present but no .rpfp-card children'};
  }
  const plans = cards.map((c) => {
    const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
    return {
      planName: T(c.querySelector('.rpfp-name')),
      cardText: T(c),
      classes: c.className || '',
    };
  });
  return {ok: true, plans: plans};
}
"""

# Cards' visible text typically looks like:
#   "One Bedroom 1 Bed1 Bath 566 - 784 Sqft $2,100 - $2,120 Brochure Contact Us"
# Regex anchors don't always have spaces between fields (e.g. "1 Bed1 Bath")
# so each pattern is tolerant of optional whitespace.
_BED_RE = re.compile(r"(\d+|studio)\s*Bed", re.IGNORECASE)
_BATH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Bath", re.IGNORECASE)
_SQFT_SINGLE_RE = re.compile(r"(\d[\d,]*)\s*sqft", re.IGNORECASE)
_SQFT_RANGE_RE = re.compile(r"(\d[\d,]*)\s*-\s*(\d[\d,]*)\s*sqft", re.IGNORECASE)
_PRICE_SINGLE_RE = re.compile(r"\$\s*([\d,]+)")
_PRICE_RANGE_RE = re.compile(r"\$\s*([\d,]+)\s*-\s*\$\s*([\d,]+)")


def _parse_card_text(text: str) -> dict:
    """Parse one .rpfp-card innerText into structured fields.

    Returns dict with keys: beds, baths, sqft_low, sqft_high, rent_low,
    rent_high. Missing values are None or empty string.
    """
    out: dict = {}
    bm = _BED_RE.search(text)
    if bm:
        v = bm.group(1)
        out["beds"] = 0 if v.lower() == "studio" else int(v)
    else:
        out["beds"] = None
    bath_m = _BATH_RE.search(text)
    out["baths"] = bath_m.group(1) if bath_m else ""
    range_m = _SQFT_RANGE_RE.search(text)
    if range_m:
        out["sqft_low"] = range_m.group(1).replace(",", "")
        out["sqft_high"] = range_m.group(2).replace(",", "")
    else:
        sm = _SQFT_SINGLE_RE.search(text)
        if sm:
            out["sqft_low"] = sm.group(1).replace(",", "")
            out["sqft_high"] = out["sqft_low"]
        else:
            out["sqft_low"] = ""
            out["sqft_high"] = ""
    price_range = _PRICE_RANGE_RE.search(text)
    if price_range:
        out["rent_low"] = money_to_int(price_range.group(1))
        out["rent_high"] = money_to_int(price_range.group(2))
    else:
        single = _PRICE_SINGLE_RE.search(text)
        if single:
            out["rent_low"] = money_to_int(single.group(1))
            out["rent_high"] = out["rent_low"]
        else:
            out["rent_low"] = None
            out["rent_high"] = None
    return out


# RealPage CWS GetUnits (2026-07-19, roster-confirmation gap #3) --------------
# The property-hosted CmsSiteManager proxy exposes a clean unit-level roster —
# refuting this adapter's original "CWS doesn't publish a per-unit roster
# publicly" assumption. ``available=true`` uses RealPage's own authoritative
# availability filter (returns only AVAILABLE_READY units = asking rent), so no
# client-side lease-status guessing. Bare form needs no siteid — the callback is
# property-hosted, so the host implies the property. Live-verified identical
# across huntingtonwoods/keltonstation/thegarfield/capitalplace.
_CWS_GETUNITS_QUERY = "act=Proxy/GetUnits&available=true&honordisplayorder=true"


def cws_getunits_url(base_url: str) -> str | None:
    """Build the property-hosted GetUnits endpoint from a property URL.

    Returns ``{scheme}://{host}/CmsSiteManager/callback.aspx?<query>`` or
    ``None`` when the base URL has no usable scheme/host.
    """
    try:
        p = urllib.parse.urlparse((base_url or "").strip())
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    return f"{p.scheme}://{p.netloc}/CmsSiteManager/callback.aspx?{_CWS_GETUNITS_QUERY}"


def _cws_avail_date(raw: Any) -> str:
    """Normalise ``internalAvailableDate`` ('2026-06-02 00:00 -0500') → date str."""
    if not isinstance(raw, str) or len(raw) < 10:
        return ""
    head = raw[:10]
    return head if re.fullmatch(r"\d{4}-\d{2}-\d{2}", head) else ""


def _cws_avail_status(lease_status: Any) -> str:
    """Map a CWS ``leaseStatus`` to AVAILABLE / UNAVAILABLE.

    The ``&available=true`` query param does NOT actually filter the roster.
    Live-verified 2026-07-31 on thewildsapts.com: 402 units = 41 ``AVAILABLE_READY``
    (each with a real ``internalAvailableDate``) + **361 ``LEASED``** (occupied, no
    date) — all previously mis-stamped AVAILABLE (a 402-available stabilized
    property). Vocabulary across 17 probed CWS properties / 1,121 units is exactly
    {``AVAILABLE_READY``, ``LEASED``}. Rule: an ``AVAILABLE``-prefixed status is
    on-market; anything else (LEASED / OCCUPIED / NOTICE / MODEL / …) is occupied.
    A missing/blank status preserves the prior AVAILABLE default so payloads that
    predate the field never regress.
    """
    s = str(lease_status or "").strip().upper()
    if not s:
        return "AVAILABLE"
    return "AVAILABLE" if s.startswith("AVAILABLE") else "UNAVAILABLE"


def parse_realpage_cws_getunits(body: str, url: str) -> list[dict[str, Any]]:
    """Parse a CWS ``GetUnits`` JSON body into unit-level dicts.

    Body shape: ``{"units": [{unitNumber, rent, squareFeet, numberOfBeds,
    numberOfBaths, floorplanName, floorNumber, buildingName,
    internalAvailableDate, leaseStatus, id, floorplanId, partnerPropertyId,
    ...}]}``. Per-unit availability is read from ``leaseStatus`` via
    ``_cws_avail_status`` — the ``available=true`` param does NOT filter the roster,
    so LEASED units leak in and must be marked UNAVAILABLE. Returns ``[]`` on
    non-JSON / no units. Never raises.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    units = data.get("units")
    if not isinstance(units, list):
        return []

    out: list[dict[str, Any]] = []
    for u in units:
        if not isinstance(u, dict):
            continue
        unit_no = str(u.get("unitNumber") or u.get("name") or "").strip()
        if not unit_no:
            continue
        rent = u.get("rent")
        if rent is None:
            rent = money_to_int(str(u.get("totalRent") or "")) or None
        elif isinstance(rent, str):
            rent = money_to_int(rent)
        sqft = u.get("squareFeet")
        beds = u.get("numberOfBeds")
        baths = u.get("numberOfBaths")
        floor = u.get("floorNumber")
        building = str(u.get("buildingName") or "").strip()
        if building.upper() in ("", "N/A"):
            building = ""

        source_ids: dict[str, Any] = {}
        if u.get("id") is not None:
            source_ids["realpage_cws_unit_id"] = u.get("id")
        if u.get("floorplanId") is not None:
            source_ids["floorplan_id"] = u.get("floorplanId")
        if u.get("partnerPropertyId"):
            source_ids["partner_property_id"] = u.get("partnerPropertyId")

        out.append(
            make_unit_dict(
                floor_plan_name=str(u.get("floorplanName") or "").strip(),
                bed_label=bed_label_from(
                    int(beds) if isinstance(beds, (int, float)) else None,
                    str(u.get("floorplanName") or ""),
                ),
                bedrooms=str(int(beds)) if isinstance(beds, (int, float)) else "",
                bathrooms=str(baths) if baths not in (None, "") else "",
                sqft=str(int(sqft)) if isinstance(sqft, (int, float)) else "",
                unit_number=unit_no,
                floor=str(int(floor)) if isinstance(floor, (int, float)) and floor else "",
                building=building,
                rent_low=int(rent) if isinstance(rent, (int, float)) else None,
                rent_high=int(rent) if isinstance(rent, (int, float)) else None,
                availability_status=_cws_avail_status(u.get("leaseStatus")),
                availability_date=_cws_avail_date(u.get("internalAvailableDate")),
                source_api_url=url,
                extraction_tier="TIER_1_API_REALPAGE_CWS_UNITS",
                source_ids=source_ids or None,
            )
        )
    return out


def parse_realpage_cws_plans(plans: list[dict], url: str) -> list[dict]:
    out: list[dict] = []
    for p in plans:
        if not isinstance(p, dict):
            continue
        name = str(p.get("planName") or "").strip()
        text = str(p.get("cardText") or "")
        parsed = _parse_card_text(text)
        beds = parsed.get("beds")
        baths = parsed.get("baths", "")
        # Prefer the high sqft when there's a range (more representative of
        # the plan's upper-bound layout); downstream unit-level work would
        # split into ranges, but for plan-only we keep the high value.
        sqft = parsed.get("sqft_high", "") or parsed.get("sqft_low", "")
        rent_low = parsed.get("rent_low")
        rent_high = parsed.get("rent_high")
        if not name and rent_low is None:
            continue
        out.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=baths,
                sqft=sqft,
                unit_number="",  # plan-level only
                rent_low=rent_low,
                rent_high=rent_high,
                rent_range=format_rent_range(rent_low, rent_high),
                availability_status="AVAILABLE" if rent_low is not None else "UNAVAILABLE",
                source_api_url=url,
                extraction_tier="TIER_1_DOM_REALPAGE_CWS",
            )
        )
    return out


class RealPageCwsAdapter:
    """RealPage CWS RPFP widget — plan-level extraction from
    ``.rpfp-container > .rpfp-cards > .rpfp-card``."""

    pms_name: str = "realpage_cws"
    _fingerprints: list[str] = [
        "cs-cdn.realpage.com/cws",
        "rpfp-container",
        "rpfp-card",
        "floorplans-widget",
    ]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used="TIER_1_DOM_REALPAGE_CWS")

        # GetUnits unit-level path (flag-gated). Property-hosted static JSON —
        # no live page needed. On success returns unit-level; on 0 available
        # units / error it falls through to the existing DOM plan-level parse.
        if enable_cws_getunits():
            gu = self._try_getunits(ctx)
            if gu is not None:
                return gu

        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            result.confidence = 0.0
            result.errors.append("realpage_cws: no live page")
            return result
        try:
            payload = await evaluate(_RPFP_DOM_JS)
        except Exception as exc:
            log.debug("realpage_cws evaluate failed err=%s", exc)
            payload = None
        if not isinstance(payload, dict) or not payload.get("ok"):
            reason = payload.get("reason") if isinstance(payload, dict) else "non-dict payload"
            result.confidence = 0.0
            result.errors.append(f"realpage_cws: {reason}")
            return result
        plans = payload.get("plans") or []
        if not isinstance(plans, list) or not plans:
            result.confidence = 0.0
            result.errors.append("realpage_cws: zero plans in payload")
            return result
        winning = self._winning_url(page, ctx)
        rows = parse_realpage_cws_plans(plans, winning)
        if not rows:
            result.confidence = 0.0
            result.errors.append(
                f"realpage_cws: parser produced zero rows from {len(plans)} cards"
            )
            return result
        from ma_poc.extraction.post_process import post_process

        pp = post_process(rows, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted > 0:
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.winning_url = winning
            # Plan-level confidence cap.
            result.confidence = min(0.85, 0.65 + 0.04 * pp.n_admitted)
            return result
        result.confidence = 0.0
        result.errors.append(
            f"realpage_cws: {len(rows)} rows failed unit_validity post-process"
        )
        return result

    def _try_getunits(self, ctx: AdapterContext) -> AdapterResult | None:
        """Try the property-hosted CWS GetUnits endpoint (static, no render).

        Returns an ``AdapterResult`` with unit-level rows on success, or
        ``None`` to signal "fall through to the DOM plan-level path" (no
        usable base URL, fetch error, non-JSON, or zero available units).
        Never raises.
        """
        # Prefer the post-redirect final URL, then base_url.
        base = ""
        fr = getattr(ctx, "fetch_result", None)
        if fr is not None:
            base = str(getattr(fr, "final_url", "") or "")
        base = base or (getattr(ctx, "base_url", "") or "")
        url = cws_getunits_url(base)
        if not url:
            return None
        try:
            from ma_poc.pms.adapters._probe import probe_get

            r = probe_get(url, timeout=20, unlocker=False)
        except Exception as exc:  # noqa: BLE001
            log.debug("realpage_cws getunits probe failed err=%s", exc)
            return None
        body = getattr(r, "text", "") or ""
        rows = parse_realpage_cws_getunits(body, url)
        if not rows:
            return None  # 0 available units → fall back to DOM plan-level

        from ma_poc.extraction.post_process import post_process

        pp = post_process(rows, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted <= 0:
            return None
        result = AdapterResult(tier_used="TIER_1_API_REALPAGE_CWS_UNITS")
        result.units = pp.admitted
        result.plan_summaries = pp.plan_summaries
        result.winning_url = url
        result.confidence = min(0.95, 0.7 + 0.05 * pp.n_admitted)
        result.api_responses.append(
            {"url": url, "status": getattr(r, "status_code", 200),
             "body": "<cws-getunits>", "via": "cws_getunits_probe"}
        )
        return result

    @staticmethod
    def _winning_url(page: Page, ctx: AdapterContext) -> str:
        try:
            return page.url or getattr(ctx, "base_url", "") or ""
        except Exception:
            return getattr(ctx, "base_url", "") or ""

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
