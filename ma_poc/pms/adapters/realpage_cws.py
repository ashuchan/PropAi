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

Plan-level only — RealPage CWS doesn't publish a per-unit roster
publicly; the "CHECK AVAILABILITY" button links back to the same
/Floor-Plans.aspx page (no drill). The "APPLY NOW" link goes to the
on-site.com leasing application form which isn't a public unit roster.

Distinct from:
  * RealPageOllAdapter (handles ``leasing.realpage.com/RP.Leasing...``
    OLL workflow XHR) — different RealPage product.
  * Other ``.aspx`` legacy themes (e.g. ``.floorplan-block`` + ``.par-units``,
    handled by RentCafeUnitRosterAdapter).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

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

    @staticmethod
    def _winning_url(page: Page, ctx: AdapterContext) -> str:
        try:
            return page.url or getattr(ctx, "base_url", "") or ""
        except Exception:
            return getattr(ctx, "base_url", "") or ""

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
