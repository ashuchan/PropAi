"""
RentVision adapter.

Research log
------------
RentVision is a multifamily marketing-site CMS. The property's own domain
serves a server-side-rendered ``/floorplans`` page whose plan grid carries
full plan-level data in the DOM — no unit XHR/API. jugnu had **no adapter
at all** for it (detector returned NONE), making it the single
highest-frequency missing-adapter cluster in the 2026-05-19 deep probe
(23 confirmed failed cases, all ``tier=NONE``).

Verified live (2026-05-19) against:
  - loftsatlittlecreek.com/floorplans — 45 ``.floorplanItem`` blocks,
    price RANGE shape ("Price $1,075 - $1,100"), "Call for Details!"
  - westgateirving.com/floorplans — 5 blocks, single-price shape
    ("Pricing Starting at $1,339"), "Only 1 Vacant Apartment Left!" /
    "Available" availability variants

Uniform DOM (both sites, identical class names):
  - ``.floorplanItem``           — one per plan; ``data-bedrooms`` /
                                    ``data-floorplan-id`` attributes
  - ``.floorplanName``           — plan name
  - ``.floorplanBeds``           — "Studio Bed" / "2 Bed"
  - ``.floorplanBaths``          — "1 Bath" / "2 Bath"
  - ``.floorplanSquareFootage``  — "926 Sq Ft square feet"
  - ``.floorplanPrice``          — "Pricing Starting at $1,339" |
                                    "Price $1,075 - $1,100" | "Call for Details"
  - ``.floorplanAvailability``   — "Available" | "Only N Vacant Apartment(s)
                                    Left!" | "Call for Details!"

The detection credit ("Powered by RentVision" / "created by RentVision" /
rentvision.com) is site-wide including the landing page, so the detector
fires before any hop; this adapter self-fetches ``/floorplans`` when the
live page is not already there.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

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

# Runs in the page. If the current document is not the /floorplans page,
# fetch it same-origin and parse with DOMParser (avoids a Python-side HTML
# regex on a 4 MB body and reuses the browser's parser). page.evaluate
# supports async functions.
_RENTVISION_DOM_JS = r"""
async () => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  let doc = document;
  if (!document.querySelector('.floorplanItem')) {
    try {
      const r = await fetch(location.origin + '/floorplans', {credentials: 'include'});
      if (r.ok) doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    } catch (e) { /* fall through — return [] below */ }
  }
  return Array.from(doc.querySelectorAll('.floorplanItem')).map((it) => ({
    name: T(it.querySelector('.floorplanName')),
    bedsAttr: it.getAttribute('data-bedrooms') || '',
    beds: T(it.querySelector('.floorplanBeds')),
    baths: T(it.querySelector('.floorplanBaths')),
    sqft: T(it.querySelector('.floorplanSquareFootage')),
    price: T(it.querySelector('.floorplanPrice')),
    avail: T(it.querySelector('.floorplanAvailability')),
  }));
}
"""

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_SQFT_RE = re.compile(r"([\d,]+)")
_MONEY_RE = re.compile(r"\$[\s]?[\d,]+")
_VACANT_RE = re.compile(r"(\d+)\s+vacant", re.IGNORECASE)


def parse_rentvision_cards(
    cards: list[dict[str, str]], url: str
) -> list[dict[str, str]]:
    """Parse RentVision ``.floorplanItem`` rows into plan-level unit dicts.

    Plan-level (one row per floor plan). ``available_units`` carries the
    per-plan vacant count when the page states one ("Only N Vacant
    Apartments Left!"). Rows with no numeric dimension are dropped by the
    caller's post_process validity gate.
    """
    units: list[dict[str, str]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        name = (card.get("name") or "").strip()
        beds_attr = (card.get("bedsAttr") or "").strip()
        beds_txt = card.get("beds") or ""
        if not name and not beds_attr and not beds_txt:
            continue

        if re.search(r"studio", beds_attr or beds_txt, re.IGNORECASE):
            beds: int | None = 0
        else:
            bm = _NUM_RE.search(beds_attr) or _NUM_RE.search(beds_txt)
            beds = int(float(bm.group(0))) if bm else None

        bath_m = _NUM_RE.search(card.get("baths") or "")
        baths = bath_m.group(0) if bath_m else ""

        sqft_m = _SQFT_RE.search(card.get("sqft") or "")
        sqft = sqft_m.group(1).replace(",", "") if sqft_m else ""

        price = card.get("price") or ""
        money = _MONEY_RE.findall(price)
        rent_lo = money_to_int(money[0]) if money else None
        rent_hi = money_to_int(money[-1]) if money else None
        rent_range = format_rent_range(rent_lo, rent_hi)

        avail = card.get("avail") or ""
        vac_m = _VACANT_RE.search(avail)
        available_units = vac_m.group(1) if vac_m else ""
        if re.search(r"waitlist", avail, re.IGNORECASE):
            status = "UNAVAILABLE"
        else:
            status = "AVAILABLE"

        units.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths),
                sqft=sqft,
                unit_number="",
                rent_range=rent_range,
                rent_low=rent_lo,
                rent_high=rent_hi,
                availability_status=status,
                available_units=available_units,
                source_api_url=url,
                extraction_tier="TIER_3_DOM_RENTVISION",
            )
        )
    return units


class RentVisionAdapter:
    """RentVision CMS adapter. Parses the SSR ``/floorplans`` plan grid."""

    pms_name: str = "rentvision"
    _fingerprints: list[str] = [
        "created by rentvision",
        "powered by rentvision",
        "rentvision.com",
    ]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract plan-level units from the RentVision ``/floorplans`` DOM.

        Uses the live page when it is already the floorplans page; otherwise
        self-fetches ``{origin}/floorplans`` in-session and parses it.
        """
        result = AdapterResult(tier_used="TIER_3_DOM_RENTVISION")

        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            result.confidence = 0.0
            result.errors.append("RentVision: no live page to parse")
            return result

        try:
            cards = await evaluate(_RENTVISION_DOM_JS)
        except Exception as exc:
            log.debug("RentVision DOM evaluate failed err=%s", exc)
            cards = None

        if not isinstance(cards, list) or not cards:
            result.confidence = 0.0
            result.errors.append("RentVision: no .floorplanItem blocks found")
            return result

        units = parse_rentvision_cards(cards, self._floorplans_url(page, ctx))
        if units:
            from ma_poc.extraction.post_process import post_process

            pp = post_process(units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                result.units = pp.admitted
                result.plan_summaries = pp.plan_summaries
                result.winning_url = self._floorplans_url(page, ctx)
                result.confidence = min(0.9, 0.65 + 0.05 * pp.n_admitted)
                return result
            result.errors.append(
                f"RENTVISION_VALIDITY_REJECTED: {len(units)} rows failed "
                f"unit_validity (no numeric dimension)"
            )

        result.confidence = 0.0
        result.errors.append("RentVision: no parseable plan data")
        return result

    @staticmethod
    def _floorplans_url(page: Page, ctx: AdapterContext) -> str:
        """Best-effort canonical ``{origin}/floorplans`` URL for provenance."""
        candidate = ""
        try:
            candidate = page.url or ""
        except Exception:
            candidate = ""
        if not candidate:
            candidate = getattr(ctx, "base_url", "") or ""
        try:
            p = urlparse(candidate)
        except Exception:
            return candidate
        if not p.scheme or not p.netloc:
            return candidate
        return urlunparse((p.scheme, p.netloc, "/floorplans", "", "", ""))

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
