"""365 ResidentServices (Apollo / collapsar theme) adapter.

A self-hosted multifamily CMS used by a handful of operators. Identical
template across instances:

  - Marker: ``cdn.365residentservices.com`` in HTML (asset host).
  - Plan page: ``GET {domain}/Marketing/FloorPlans`` — SSR plan cards in
    ``.floorplan-tile`` containers.
  - Unit detail: ``GET {domain}/Marketing/FloorPlans/Units/{guid}`` — SSR
    per-unit rows (unit-level drill, not yet exercised here; future).

Per-card DOM (verified live 2026-05-19 on rusticwoodsapts.com /
waterfordpoint.us — selectors byte-identical):
  - ``.floorplan-tile``         — one per plan
  - ``.title-row``              — "Sedona 1 Bed 1 Bath 675 sqft" (header)
  - ``.list-divider``           — "1 Bed 1 Bath 675 sqft"  (clean specs;
                                   "Studio" → 0 beds)
  - ``.pricing``                — "$759 per month" | "$849 - $899 per month"
  - ``.availability``           — "3 Units Available" | "1 Unit Available" |
                                   "Join Waitlist"

Plan-level for this first cut; the per-unit ``/Units/{guid}`` drill is a
follow-up enhancement (the GUID is captured but not yet fetched).

Probed cluster size: 4 in the 351-row deep-probe sample (16196, 1777,
16754, 60939) — all the same template. Detector_hop_gap was the failure
mode (jugnu tier=NONE because the CMS marker wasn't fingerprinted).
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

# Self-fetch /Marketing/FloorPlans if the live page isn't already there;
# parse the .floorplan-tile cards via DOMParser. Returns [] for non-365rs
# pages so unrelated sites are unaffected.
_RS365_DOM_JS = r"""
async () => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  let doc = document;
  if (!document.querySelector('.floorplan-tile')) {
    try {
      const r = await fetch(location.origin + '/Marketing/FloorPlans', {credentials: 'include'});
      if (r.ok) doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    } catch (e) { /* fall through */ }
  }
  return Array.from(doc.querySelectorAll('.floorplan-tile')).map((t) => ({
    title: T(t.querySelector('.title-row')),
    specs: T(t.querySelector('.list-divider')),
    pricing: T(t.querySelector('.pricing')),
    availability: T(t.querySelector('.availability')),
  }));
}
"""

_BED_RE = re.compile(r"(\d+)\s*Bed", re.IGNORECASE)
_BATH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Bath", re.IGNORECASE)
_SQFT_RE = re.compile(r"(\d[\d,]*)\s*sqft", re.IGNORECASE)
_MONEY_RE = re.compile(r"\$([\d,]+)")
_AVAIL_COUNT_RE = re.compile(r"(\d+)\s+Units?\s+Available", re.IGNORECASE)


def _plan_name(title: str, specs: str) -> str:
    """Extract the plan name from ``title`` by stripping the specs suffix.

    Title is e.g. "Sedona 1 Bed 1 Bath 675 sqft" or "Stafford Studio 1 Bath
    392 sqft Special". The name is everything up to the first occurrence
    of ``Studio`` or ``\\d+\\s*Bed`` — whichever comes first.
    """
    title = (title or "").strip()
    if not title:
        return ""
    studio_m = re.search(r"\bStudio\b", title, re.IGNORECASE)
    bed_m = re.search(r"\d+\s*Bed", title, re.IGNORECASE)
    candidates = [m.start() for m in (studio_m, bed_m) if m]
    if not candidates:
        return title
    return title[: min(candidates)].strip()


def parse_residentservices365_tiles(
    tiles: list[dict[str, str]], url: str
) -> list[dict[str, str]]:
    """Parse 365 ResidentServices ``.floorplan-tile`` rows into plan-level
    unit dicts. ``available_units`` carries the per-plan count when stated.
    """
    units: list[dict[str, str]] = []
    for t in tiles:
        if not isinstance(t, dict):
            continue
        title = (t.get("title") or "").strip()
        specs = (t.get("specs") or "").strip()
        name = _plan_name(title, specs)
        if not name:
            continue

        if re.search(r"\bStudio\b", specs, re.IGNORECASE):
            beds: int | None = 0
        else:
            bm = _BED_RE.search(specs)
            beds = int(bm.group(1)) if bm else None
        bath_m = _BATH_RE.search(specs)
        baths = bath_m.group(1) if bath_m else ""
        sqft_m = _SQFT_RE.search(specs)
        sqft = sqft_m.group(1).replace(",", "") if sqft_m else ""

        pricing = t.get("pricing") or ""
        money = _MONEY_RE.findall(pricing)
        rent_lo = money_to_int(money[0]) if money else None
        rent_hi = money_to_int(money[-1]) if money else None

        avail = t.get("availability") or ""
        count_m = _AVAIL_COUNT_RE.search(avail)
        available_units = count_m.group(1) if count_m else ""
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
                unit_number="",  # plan-level
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi,
                availability_status=status,
                available_units=available_units,
                source_api_url=url,
                extraction_tier="TIER_1_DOM_365RESIDENTSERVICES",
            )
        )
    return units


class Residentservices365Adapter:
    """365 ResidentServices Apollo/collapsar CMS adapter."""

    pms_name: str = "residentservices365"
    _fingerprints: list[str] = ["365residentservices.com", "/Marketing/FloorPlans"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract plan-level units from the SSR ``/Marketing/FloorPlans`` grid."""
        result = AdapterResult(tier_used="TIER_1_DOM_365RESIDENTSERVICES")

        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            result.confidence = 0.0
            result.errors.append("365rs: no live page to parse")
            return result

        try:
            tiles = await evaluate(_RS365_DOM_JS)
        except Exception as exc:
            log.debug("365rs DOM evaluate failed err=%s", exc)
            tiles = None

        if not isinstance(tiles, list) or not tiles:
            result.confidence = 0.0
            result.errors.append(
                "365rs: no .floorplan-tile blocks found at /Marketing/FloorPlans"
            )
            return result

        units = parse_residentservices365_tiles(tiles, self._winning_url(page, ctx))
        if units:
            from ma_poc.extraction.post_process import post_process

            pp = post_process(units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                result.units = pp.admitted
                result.plan_summaries = pp.plan_summaries
                result.winning_url = self._winning_url(page, ctx)
                result.confidence = min(0.9, 0.65 + 0.05 * pp.n_admitted)
                return result
            result.errors.append(
                f"RS365_VALIDITY_REJECTED: {len(units)} rows failed unit_validity"
            )

        result.confidence = 0.0
        result.errors.append("365rs: no parseable plan data")
        return result

    @staticmethod
    def _winning_url(page: Page, ctx: AdapterContext) -> str:
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
        return urlunparse((p.scheme, p.netloc, "/Marketing/FloorPlans", "", "", ""))

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
