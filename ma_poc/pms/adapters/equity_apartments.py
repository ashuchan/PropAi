"""Equity Apartments — custom REIT platform with inline unit roster.

Equity Apartments operates ~300 multifamily properties on a custom
platform built on top of Yardi (BLDGID / UNITID / CATEGORY conventions
appear in embedded JSON). The marketing site renders unit cards
inline in an Angular app at
``equityapartments.com/{city}/{neighborhood}/{property-slug}``.

Verified live 2026-05-21 on
www.equityapartments.com/los-angeles/financial-district/pegasus-apartments
(30 ``.unit-expanded-card`` elements, 15 currently visible).

DOM contract:
  <li class="list-group-item row unit cardExpanded2021">
    <div class="col-xs-12 unit-expanded-card">
      <div class="col-xs-4 specs">
        <p class="pricing-container">
          <span class="pricing">$1,660</span>
        </p>
        <p class="description">0 Bed / 1 Bath  488 sq. ft. / Floor 7</p>
        <p>Available 5/27/2026</p>
      </div>
      <a href="/UnitFees/{prop_id}/{building_id}/{unit_id}">What's my total cost?</a>
    </div>
  </li>

Unit number comes from the ``/UnitFees/{prop}/{bldg}/{unit}`` href
(no other authoritative source in the rendered DOM).

Hidden cards (``ng-hide`` class on the parent ``<li>``) are skipped —
those are filtered-out variants. Only visible cards represent actually-
available units.

Distinct from RentCafe / Entrata / RealPage adapters — Equity has
their own consumption layer over Yardi.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)


_EQUITY_DOM_JS = r"""
async () => {
  const T = (el) => (el ? el.innerText.replace(/\s+/g, ' ').trim() : '');
  const cards = Array.from(document.querySelectorAll('.unit-expanded-card'));
  if (cards.length === 0) return {ok: false, reason: 'no .unit-expanded-card elements'};
  const units = [];
  for (const card of cards) {
    const li = card.closest('li.unit');
    if (li && li.classList.contains('ng-hide')) continue;  // hidden variant
    const text = T(card);
    if (!text) continue;
    const unitFeesLink = card.querySelector('a[href*="/UnitFees/"]');
    const unitFeesHref = unitFeesLink ? unitFeesLink.getAttribute('href') : '';
    const priceEl = card.querySelector('.pricing');
    const priceText = T(priceEl);
    units.push({
      text: text,
      unitFeesHref: unitFeesHref,
      priceText: priceText,
    });
  }
  return {ok: units.length > 0, units: units};
}
"""


_UNITFEES_RE = re.compile(r"/UnitFees/(\d+)/(\d+)/(\d+)")
_BED_BATH_RE = re.compile(r"(\d+|studio)\s*Bed\b.*?(\d+(?:\.\d+)?)\s*Bath\b", re.IGNORECASE)
_SQFT_RE = re.compile(r"(\d[\d,]*)\s*sq\.?\s*ft\.?", re.IGNORECASE)
_FLOOR_RE = re.compile(r"Floor\s*(\d+)", re.IGNORECASE)
_AVAIL_RE = re.compile(r"Available\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE)
_PRICE_RE = re.compile(r"\$\s*([\d,]+)")


def _parse_equity_unit(card_data: dict) -> dict | None:
    """Parse one .unit-expanded-card into a structured unit dict."""
    text = str(card_data.get("text") or "")
    if not text:
        return None
    out: dict[str, str] = {}
    href = str(card_data.get("unitFeesHref") or "")
    uf_match = _UNITFEES_RE.search(href)
    if uf_match:
        out["property_id"] = uf_match.group(1)
        out["building_id"] = uf_match.group(2)
        out["unit_number"] = uf_match.group(3)
    # Price — prefer the explicit .pricing element when present, else
    # the first $X in card text.
    price_text = str(card_data.get("priceText") or "")
    pm = _PRICE_RE.search(price_text or text)
    if pm:
        out["rent"] = pm.group(1).replace(",", "")
    bb = _BED_BATH_RE.search(text)
    if bb:
        bed_v = bb.group(1)
        out["beds"] = "0" if bed_v.lower() == "studio" else bed_v
        out["baths"] = bb.group(2)
    sq = _SQFT_RE.search(text)
    if sq:
        out["sqft"] = sq.group(1).replace(",", "")
    fl = _FLOOR_RE.search(text)
    if fl:
        out["floor"] = fl.group(1)
    av = _AVAIL_RE.search(text)
    if av:
        out["availability_date"] = av.group(1)
    if "unit_number" not in out and "rent" not in out:
        return None
    return out


def parse_equity_apartments(units: list[dict], url: str) -> list[dict]:
    out: list[dict] = []
    for u in units:
        if not isinstance(u, dict):
            continue
        parsed = _parse_equity_unit(u)
        if not parsed:
            continue
        beds_str = parsed.get("beds", "")
        try:
            beds_int: int | None = int(beds_str) if beds_str else None
        except (TypeError, ValueError):
            beds_int = None
        rent_str = parsed.get("rent", "")
        try:
            rent: int | None = int(rent_str) if rent_str else None
        except (TypeError, ValueError):
            rent = None
        out.append(
            make_unit_dict(
                floor_plan_name="",  # Equity doesn't expose a plan code on the card
                bed_label=bed_label_from(beds_int, ""),
                bedrooms=beds_str,
                bathrooms=parsed.get("baths", ""),
                sqft=parsed.get("sqft", ""),
                floor=parsed.get("floor", ""),
                building=parsed.get("building_id", ""),
                unit_number=parsed.get("unit_number", ""),
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                available_units="1",
                availability_date=parsed.get("availability_date", ""),
                source_api_url=url,
                extraction_tier="TIER_1_DOM_EQUITY_APARTMENTS",
            )
        )
    return out


class EquityApartmentsAdapter:
    """Equity Apartments REIT — inline ``.unit-expanded-card`` extraction."""

    pms_name: str = "equity_apartments"
    _fingerprints: list[str] = [
        "equityapartments.com",
        "media.equityapartments.com",
        "unit-expanded-card",
        "/UnitFees/",
    ]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used="TIER_1_DOM_EQUITY_APARTMENTS")
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            result.confidence = 0.0
            result.errors.append("equity_apartments: no live page")
            return result
        try:
            payload = await evaluate(_EQUITY_DOM_JS)
        except Exception as exc:
            log.debug("equity_apartments evaluate failed err=%s", exc)
            payload = None
        if not isinstance(payload, dict) or not payload.get("ok"):
            reason = payload.get("reason") if isinstance(payload, dict) else "non-dict payload"
            result.confidence = 0.0
            result.errors.append(f"equity_apartments: {reason}")
            return result
        units = payload.get("units") or []
        if not isinstance(units, list) or not units:
            result.confidence = 0.0
            result.errors.append("equity_apartments: zero visible units")
            return result
        winning = self._winning_url(page, ctx)
        rows = parse_equity_apartments(units, winning)
        if not rows:
            result.confidence = 0.0
            result.errors.append(
                f"equity_apartments: parser produced no rows from {len(units)} cards"
            )
            return result
        from ma_poc.extraction.post_process import post_process

        pp = post_process(rows, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted > 0:
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.winning_url = winning
            result.confidence = min(0.93, 0.7 + 0.02 * pp.n_admitted)
            return result
        result.confidence = 0.0
        result.errors.append(
            f"equity_apartments: {len(rows)} rows failed unit_validity post-process"
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
