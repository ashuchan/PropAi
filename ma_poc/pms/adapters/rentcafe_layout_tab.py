"""RentCafe "floorplans-layout-tab" theme — bedroom-tab listing + per-plan
drill at ``/floorplans/{slug}``.

Some RentCafe-tagged sites use a "layout-tab" theme where the
``/floorplans`` page is a bedroom-tab listing (Studio / 1 Bed / 2 Bed)
and clicking a plan navigates to ``/floorplans/{bedroom-or-plan-slug}``
which exposes the unit-level roster.

Verified live 2026-05-21 on:
  - www.tudorplaceapts.com/floorplans → drill ``/floorplans/two-bedrooms``
    has tabular rows: ``#840_09 900 $1,765.00 Specials Available``
  - www.campobassoapts.com/floorplans → drill ``/floorplans/studio``
    has text-based: ``Apartment: # F_205 Starting at: $1,425.00``

Detection: the ``.page-content-floorplans.floorplans-layout-tab`` class
on the listing page is unique to this theme (does not collide with
Market Apartments, RentCafe modern unit-roster theme, or RealPage CWS).

Extraction:
  1. On the /floorplans listing page, find every drill anchor matching
     ``/floorplans/{slug}`` pattern.
  2. Fetch each drill in-session.
  3. Parse the drill's body text for unit rows. Two formats are
     tolerated by the same regex set:
       * Tudorplace tabular: ``#840_09 900 $1,765.00 Specials Available``
       * Campobasso text:    ``Apartment: # F_205 Starting at: $1,425.00``
  4. Join unit rows back to the plan via the drill slug.

Plan-level fallback when a drill has no parseable units (e.g. plans
listed but Waitlist).
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

# Identifies a /floorplans/{slug} drill URL on the SAME origin.
_DRILL_HREF_RE = re.compile(r"^/floorplans/[^/?#][^/?#]*/?$")

_RENTCAFE_LT_DOM_JS = r"""
async () => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  // The listing page exposes .page-content-floorplans.floorplans-layout-tab.
  // If we're not there, try /floorplans first.
  let doc = document;
  let onListingPage = !!document.querySelector('.page-content-floorplans.floorplans-layout-tab');
  if (!onListingPage) {
    try {
      const r = await fetch(location.origin + '/floorplans', {credentials: 'include'});
      if (r.ok) {
        const candidate = new DOMParser().parseFromString(await r.text(), 'text/html');
        if (candidate.querySelector('.page-content-floorplans.floorplans-layout-tab')) {
          doc = candidate;
          onListingPage = true;
        }
      }
    } catch (e) { /* fall through */ }
  }
  if (!onListingPage) {
    return {ok: false, reason: 'no .page-content-floorplans.floorplans-layout-tab listing'};
  }
  // Collect per-plan drill hrefs (/floorplans/{slug}) — exclude the
  // listing itself.
  const drillSet = new Set();
  const drillItems = [];
  const anchors = Array.from(doc.querySelectorAll('a[href]'));
  for (const a of anchors) {
    const href = a.getAttribute('href') || '';
    const path = href.startsWith('http')
      ? (function() { try { return new URL(href).pathname; } catch (e) { return ''; } })()
      : href.split('?')[0].split('#')[0];
    if (/^\/floorplans\/[^/?#][^/?#]*\/?$/.test(path) && !drillSet.has(path)) {
      drillSet.add(path);
      drillItems.push({
        path: path,
        anchorText: T(a),
      });
    }
  }
  if (drillItems.length === 0) {
    return {ok: false, reason: 'no /floorplans/{slug} drill anchors on listing'};
  }
  // For each drill, fetch and capture the body innerText.
  const plans = [];
  for (const item of drillItems) {
    let bodyText = '';
    let h1Text = '';
    try {
      const r = await fetch(location.origin + item.path, {credentials: 'include'});
      if (r.ok) {
        const drillDoc = new DOMParser().parseFromString(await r.text(), 'text/html');
        h1Text = T(drillDoc.querySelector('h1'));
        // Prefer the main content area when present; fall back to body.
        const main = drillDoc.querySelector('main, .page-content-availableunits, .page-content-floorplans');
        bodyText = T(main || drillDoc.body);
      }
    } catch (e) { /* skip */ }
    plans.push({
      drillPath: item.path,
      anchorText: item.anchorText,
      h1: h1Text,
      bodyText: bodyText.slice(0, 50000),
    });
  }
  return {ok: true, plans: plans};
}
"""

# Tudorplace-style row: "#840_09 900 $1,765.00 Specials Available Apply Now..."
_TABULAR_ROW_RE = re.compile(
    r"#\s*([A-Z0-9_\-]+)\s+(\d{2,5})\s+\$\s*([\d,]+(?:\.\d{2})?)",
    re.IGNORECASE,
)
# Campobasso-style row: "Apartment: # F_205 Starting at: $1,425.00"
_TEXT_ROW_RE = re.compile(
    r"Apartment\s*:\s*#?\s*([A-Z0-9_\-]+).*?Starting\s+at\s*:?\s*\$\s*([\d,]+(?:\.\d{2})?)",
    re.IGNORECASE | re.DOTALL,
)

_PLAN_SPECS_RE = re.compile(
    # Accept "Bed", "Beds", "Bedroom", "Bedrooms" without requiring a word
    # boundary right after the noun (Tudor Place uses "Beds", Campo Basso
    # "Bedroom"). Same for "Bath"/"Baths"/"Bathroom"/"Bathrooms".
    r"(\d+|studio)\s*(?:bed|bedroom)s?\b"
    r".*?(\d+(?:\.\d+)?)\s*(?:bath|bathroom)s?\b"
    r".*?(\d[\d,]*)\s*sq\.?\s*ft\.?",
    re.IGNORECASE | re.DOTALL,
)

# "4 Apartments Available" / "13 Available" → unit count hint
_AVAIL_COUNT_RE = re.compile(r"(\d+)\s+(?:Apartment|Unit)s?\s+Available", re.IGNORECASE)


def _parse_drill_units(body_text: str) -> list[dict]:
    """Extract unit rows from drill body text. Returns list of dicts
    with unit_number + sqft + rent + raw_text."""
    if not body_text:
        return []
    seen: set[str] = set()
    units: list[dict] = []
    # Tabular pattern first (more specific — has sqft column).
    for m in _TABULAR_ROW_RE.finditer(body_text):
        unit_no = m.group(1).strip()
        sqft = m.group(2).strip()
        rent = m.group(3).replace(",", "").split(".")[0]
        key = unit_no.upper()
        if key in seen:
            continue
        seen.add(key)
        units.append({
            "unit_number": unit_no,
            "sqft": sqft,
            "rent": rent,
        })
    # Text pattern second (Apartment: # X Starting at: $Y) — only adds
    # unit numbers not already captured.
    for m in _TEXT_ROW_RE.finditer(body_text):
        unit_no = m.group(1).strip()
        rent = m.group(2).replace(",", "").split(".")[0]
        key = unit_no.upper()
        if key in seen:
            continue
        seen.add(key)
        units.append({
            "unit_number": unit_no,
            "sqft": "",  # not present in text-style rows
            "rent": rent,
        })
    return units


def _parse_plan_specs(text: str) -> tuple[int | None, str, str]:
    """Extract (beds, baths, sqft) from drill text 'Studio 1 Bath 480 Sq. Ft.'
    or '2 Beds 1.5 Bath 900 Sq. Ft.'."""
    if not text:
        return None, "", ""
    if re.search(r"\bstudio\b", text, re.IGNORECASE):
        # Studio variant — try to find baths and sqft separately
        bm = re.search(r"(\d+(?:\.\d+)?)\s*bath", text, re.IGNORECASE)
        sm = re.search(r"(\d[\d,]*)\s*sq\.?\s*ft", text, re.IGNORECASE)
        return 0, (bm.group(1) if bm else ""), (sm.group(1).replace(",", "") if sm else "")
    m = _PLAN_SPECS_RE.search(text)
    if m:
        bed_v = m.group(1)
        beds = 0 if bed_v.lower() == "studio" else int(bed_v)
        return beds, m.group(2), m.group(3).replace(",", "")
    return None, "", ""


def parse_rentcafe_layout_tab(plans: list[dict], url: str) -> list[dict]:
    out: list[dict] = []
    for p in plans:
        if not isinstance(p, dict):
            continue
        drill = str(p.get("drillPath") or "")
        slug = drill.rstrip("/").split("/")[-1] if drill else ""
        anchor = str(p.get("anchorText") or "").strip()
        h1 = str(p.get("h1") or "").strip()
        body = str(p.get("bodyText") or "")
        plan_name = h1 or anchor or slug or ""
        beds, baths, sqft = _parse_plan_specs(body)
        units_parsed = _parse_drill_units(body)
        avail_match = _AVAIL_COUNT_RE.search(body)
        avail_count = avail_match.group(1) if avail_match else ""

        if not units_parsed:
            # No unit roster — emit plan-level row if there's a starting rent.
            # The anchor text often carries it, e.g. "2 Beds 1 Bath 13 Available $1,765.00".
            sp_match = re.search(r"Starting\s+at\s*:?\s*\$\s*([\d,]+)", body, re.IGNORECASE)
            sp_rent = None
            if sp_match:
                sp_rent = money_to_int(sp_match.group(1))
            if not sp_rent and anchor:
                am = re.search(r"\$\s*([\d,]+)", anchor)
                if am:
                    sp_rent = money_to_int(am.group(1))
            if plan_name or sp_rent is not None:
                out.append(
                    make_unit_dict(
                        floor_plan_name=plan_name,
                        bed_label=bed_label_from(beds, plan_name),
                        bedrooms=str(beds) if beds is not None else "",
                        bathrooms=baths,
                        sqft=sqft,
                        unit_number="",
                        rent_low=sp_rent,
                        rent_high=sp_rent,
                        rent_range=format_rent_range(sp_rent, sp_rent),
                        availability_status="AVAILABLE" if sp_rent is not None else "UNAVAILABLE",
                        available_units=avail_count,
                        source_api_url=url + drill if drill else url,
                        extraction_tier="TIER_1_DOM_RENTCAFE_LT",
                    )
                )
            continue

        for u in units_parsed:
            unit_no = u.get("unit_number") or ""
            row_sqft = u.get("sqft") or sqft
            rent_str = u.get("rent") or ""
            try:
                rent = int(rent_str) if rent_str else None
            except (TypeError, ValueError):
                rent = None
            if not unit_no and rent is None:
                continue
            out.append(
                make_unit_dict(
                    floor_plan_name=plan_name,
                    bed_label=bed_label_from(beds, plan_name),
                    bedrooms=str(beds) if beds is not None else "",
                    bathrooms=baths,
                    sqft=row_sqft,
                    unit_number=unit_no,
                    rent_low=rent,
                    rent_high=rent,
                    availability_status="AVAILABLE",
                    available_units="1",
                    source_api_url=url + drill if drill else url,
                    extraction_tier="TIER_1_DOM_RENTCAFE_LT",
                )
            )
    return out


class RentCafeLayoutTabAdapter:
    """RentCafe layout-tab theme — bedroom-tab listing + per-plan
    ``/floorplans/{slug}`` drill with unit roster."""

    pms_name: str = "rentcafe_layout_tab"
    _fingerprints: list[str] = [
        "floorplans-layout-tab",
        "page-content-floorplans",
        "page-content-availableunits",
    ]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used="TIER_1_DOM_RENTCAFE_LT")
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            result.confidence = 0.0
            result.errors.append("rentcafe_lt: no live page")
            return result
        try:
            payload = await evaluate(_RENTCAFE_LT_DOM_JS)
        except Exception as exc:
            log.debug("rentcafe_lt evaluate failed err=%s", exc)
            payload = None
        if not isinstance(payload, dict) or not payload.get("ok"):
            reason = payload.get("reason") if isinstance(payload, dict) else "non-dict payload"
            result.confidence = 0.0
            result.errors.append(f"rentcafe_lt: {reason}")
            return result
        plans = payload.get("plans") or []
        if not isinstance(plans, list) or not plans:
            result.confidence = 0.0
            result.errors.append("rentcafe_lt: zero plans in payload")
            return result
        winning = self._winning_url(page, ctx)
        rows = parse_rentcafe_layout_tab(plans, winning)
        if not rows:
            result.confidence = 0.0
            result.errors.append(
                f"rentcafe_lt: parser produced zero rows from {len(plans)} drills"
            )
            return result
        from ma_poc.extraction.post_process import post_process

        pp = post_process(rows, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted > 0:
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.winning_url = winning
            result.confidence = min(0.92, 0.7 + 0.02 * pp.n_admitted)
            return result
        result.confidence = 0.0
        result.errors.append(
            f"rentcafe_lt: {len(rows)} rows failed unit_validity post-process"
        )
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
        return urlunparse((p.scheme, p.netloc, "/floorplans", "", "", ""))

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
