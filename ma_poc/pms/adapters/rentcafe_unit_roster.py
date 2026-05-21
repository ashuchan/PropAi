"""RentCafe "modern" theme — inline ``.floorplan-block`` + ``.par-units > .unit-container``.

Some RentCafe-tagged sites (modern Site Manager theme) embed the unit-
level roster directly in the property page, NOT behind a SecureCafe
portal hop. The DOM contract (confirmed live 2026-05-21 on
www.thefrankestate.com and www.somaresidences.com/Floor-Plans.aspx):

  ``.floorplan-block`` plan cards
    id=``floorplan_{ID}``
    data-rent="1701.0"          (starting rent, float string)
    data-numunits="10"          (total advertised plans for this layout)
    data-bed="1" | "S"          ("S" = studio)
    data-bath="1"
    data-sqft="750"
    data-floorplan-name="1x1 Stella"
    data-building="N/A" | "1"

  ``.par-units`` unit-roster container (separate DOM subtree)
    id=``par_{ID}``             (SAME ``{ID}`` as the matching floorplan-block)
    > ``.unit-container`` × N   (one per available unit)
      Visible text shape:
        "Unit #08-7 750 sqft Available: NOW Lease Term: 12 ...
         Starting At: $1,694 Lease Now"

The adapter:
  1. Collects every ``.floorplan-block`` and its data-* attributes.
  2. For each plan, finds the matching ``.par-units#par_{ID}``.
  3. Walks each ``.unit-container`` inside, regex-parses the inner text
     for unit#/sqft/availability/lease term/rent.
  4. Joins unit rows to their plan via the shared ID.

Live-confirmed sites (HAR validation set):
  - thefrankestate.com — 9 plans, 11 unit-containers
  - somaresidences.com — 6 plans, 9 unit-containers

Distinct from the Market Apartments ``floorplan-block`` shape, which uses
``data-bedrooms`` / ``data-bathrooms`` / ``data-when`` instead, and is
guarded by the ``.floorplan-unit-single`` co-selector in the MA adapter.
The RentCafe modern theme here uses ``data-bed`` / ``data-bath`` / no
``.floorplan-unit-single``, so the MA adapter correctly does NOT route
to it.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

# The DOM JS walks the live page, returning a structured payload of
# plan cards + their associated unit-container rows.
_RENTCAFE_UR_DOM_JS = r"""
async () => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const A = (el, name) => (el ? (el.getAttribute(name) || '') : '');

  const fbs = Array.from(document.querySelectorAll('.floorplan-block'));
  if (fbs.length === 0) {
    return {ok: false, reason: 'no floorplan-block on page'};
  }
  // The adapter is gated on the presence of .par-units / .unit-container
  // so we don't fire on Market Apartments or other "floorplan-block" themes.
  const allParUnits = document.querySelectorAll('.par-units');
  if (allParUnits.length === 0) {
    return {ok: false, reason: 'no .par-units roster present (likely a different theme)'};
  }
  const planRows = fbs.map((fb) => {
    const id = (fb.id || '').replace(/^floorplan_/, '');
    const par = id ? document.getElementById('par_' + id) : null;
    const ucs = par ? Array.from(par.querySelectorAll('.unit-container')) : [];
    return {
      id: id,
      data: Object.assign({}, fb.dataset || {}),
      // 2026-05-21 HAR validation: thebeachapts.com renders the unit-
      // container with block-level child divs (.unit-number, .unit-sqft,
      // .available-now) that get concatenated WITHOUT spaces when read
      // via .innerText/.textContent. Reading from structured child
      // selectors is correct on both thefrankestate (which also has
      // these classes) and thebeachapts (where the unstructured text
      // would mis-extract). Still emit ``text`` as a fallback for older
      // RentCafe themes that lack the structured children.
      units: ucs.map((uc) => ({
        id: uc.id || '',
        text: T(uc),
        unitNumberSel: T(uc.querySelector('.unit-number')),
        unitSqftSel: T(uc.querySelector('.unit-sqft')),
        unitRentSel: T(uc.querySelector('.unit-rent, .pricing, .unit-price')),
        availableNow: !!uc.querySelector('.available-now'),
      })),
    };
  });
  return {ok: true, plans: planRows};
}
"""

_UNIT_NUMBER_RE = re.compile(r"Unit\s*#\s*([A-Z0-9\-]+)", re.IGNORECASE)
_SQFT_RE = re.compile(r"(\d[\d,]{0,4})\s*sqft", re.IGNORECASE)
_AVAIL_NOW_RE = re.compile(r"Available\s*:\s*now\b", re.IGNORECASE)
_AVAIL_DATE_RE = re.compile(
    r"Available\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+\d{1,2},?\s*\d{0,4})",
    re.IGNORECASE,
)
_LEASE_TERM_RE = re.compile(r"Lease\s*Term\s*:\s*(\d+)", re.IGNORECASE)
_RENT_RE = re.compile(r"\$\s*([\d,]+)")


def _parse_unit_text(text: str, structured: dict | None = None) -> dict[str, str]:
    """Parse a single ``.unit-container``.

    Prefers structured child selectors when provided (more reliable —
    handles thebeachapts where block-level divs concatenate without
    spaces in innerText). Falls back to regex on the unstructured text
    for older RentCafe themes.

    Sample input (thefrankestate, spaces preserved by CSS):
        ``Unit #08-7 750 sqft Available: NOW Lease Term: 12
          See Unit Amenities Starting At: $1,694 Lease Now ...``

    Sample input (thebeachapts, no spaces in innerText):
        ``Unit #123468 sqftAvailable: NOWLease Term: 9Inquire``
    but with structured selectors: unitNumberSel="Unit #123",
    unitSqftSel="468 sqft", availableNow=True.
    """
    out: dict[str, str] = {}
    # Unit number: try .unit-number first, then regex on full text.
    if structured and structured.get("unitNumberSel"):
        m = _UNIT_NUMBER_RE.search(str(structured["unitNumberSel"]))
        if m:
            out["unit_number"] = m.group(1)
    if "unit_number" not in out:
        m = _UNIT_NUMBER_RE.search(text)
        if m:
            out["unit_number"] = m.group(1)
    # Sqft: prefer .unit-sqft selector.
    if structured and structured.get("unitSqftSel"):
        m = _SQFT_RE.search(str(structured["unitSqftSel"]))
        if m:
            out["sqft"] = m.group(1).replace(",", "")
    if "sqft" not in out:
        m = _SQFT_RE.search(text)
        if m:
            out["sqft"] = m.group(1).replace(",", "")
    # Availability: .available-now boolean from structured probe is the
    # most reliable signal; fall back to regex.
    if structured and structured.get("availableNow"):
        out["availability_status"] = "AVAILABLE"
        out["availability_date"] = ""
    elif _AVAIL_NOW_RE.search(text):
        out["availability_status"] = "AVAILABLE"
        out["availability_date"] = ""
    else:
        m = _AVAIL_DATE_RE.search(text)
        if m:
            out["availability_status"] = "AVAILABLE"
            out["availability_date"] = m.group(1)
        else:
            out["availability_status"] = "AVAILABLE"  # default optimistic
            out["availability_date"] = ""
    m = _LEASE_TERM_RE.search(text)
    if m:
        out["lease_term"] = m.group(1)
    # Rent: prefer .unit-rent / .pricing selector, else regex.
    if structured and structured.get("unitRentSel"):
        m = _RENT_RE.search(str(structured["unitRentSel"]))
        if m:
            out["rent"] = m.group(1).replace(",", "")
    if "rent" not in out:
        m = _RENT_RE.search(text)
        if m:
            out["rent"] = m.group(1).replace(",", "")
    return out


def _bed_count(bed_attr: str) -> int | None:
    """Convert RentCafe ``data-bed`` value to bed count int.

    "S" → 0 (studio), digit string → int, else None.
    """
    if not bed_attr:
        return None
    b = bed_attr.strip().upper()
    if b == "S" or "STUDIO" in b:
        return 0
    if b.isdigit():
        return int(b)
    return None


def parse_rentcafe_unit_roster(plans: list[dict], url: str) -> list[dict]:
    """Build unit-level rows from the {.floorplan-block, .par-units}
    DOM extracted via JS."""
    out: list[dict] = []
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        data = plan.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        plan_name = str(data.get("floorplanName") or "").strip()
        bed_attr = str(data.get("bed") or "")
        beds = _bed_count(bed_attr)
        baths = str(data.get("bath") or "").strip()
        plan_sqft = str(data.get("sqft") or "").strip()
        plan_rent_raw = str(data.get("rent") or "").strip()
        plan_numunits = str(data.get("numunits") or "").strip()
        building = str(data.get("building") or "").strip()
        if building.upper() in ("N/A", "NONE", ""):
            building = ""
        try:
            plan_rent = int(float(plan_rent_raw)) if plan_rent_raw else None
        except (TypeError, ValueError):
            plan_rent = None

        units_raw = plan.get("units") or []
        if not isinstance(units_raw, list) or not units_raw:
            # Plan-level fallback row when no unit roster exists for this plan.
            if plan_name or plan_rent is not None:
                out.append(
                    make_unit_dict(
                        floor_plan_name=plan_name,
                        bed_label=bed_label_from(beds, plan_name),
                        bedrooms=str(beds) if beds is not None else "",
                        bathrooms=baths,
                        sqft=plan_sqft,
                        building=building,
                        unit_number="",
                        rent_low=plan_rent,
                        rent_high=plan_rent,
                        availability_status="AVAILABLE" if plan_rent is not None else "UNAVAILABLE",
                        available_units=plan_numunits,
                        source_api_url=url,
                        extraction_tier="TIER_1_DOM_RENTCAFE_UR",
                    )
                )
            continue

        for u in units_raw:
            if not isinstance(u, dict):
                continue
            text = str(u.get("text") or "")
            if not text:
                continue
            # Pass through the structured child selectors when DOM JS
            # provided them (newer payload shape from 2026-05-21);
            # _parse_unit_text falls back to regex for older payloads
            # that don't include the structured fields.
            structured = {
                k: u.get(k)
                for k in ("unitNumberSel", "unitSqftSel", "unitRentSel", "availableNow")
                if k in u
            }
            parsed = _parse_unit_text(text, structured or None)
            unit_no = parsed.get("unit_number", "")
            sqft = parsed.get("sqft") or plan_sqft
            rent_str = parsed.get("rent", "")
            try:
                rent = int(rent_str) if rent_str else plan_rent
            except (TypeError, ValueError):
                rent = plan_rent
            if not unit_no and rent is None:
                continue
            out.append(
                make_unit_dict(
                    floor_plan_name=plan_name,
                    bed_label=bed_label_from(beds, plan_name),
                    bedrooms=str(beds) if beds is not None else "",
                    bathrooms=baths,
                    sqft=sqft,
                    building=building,
                    unit_number=unit_no,
                    rent_low=rent,
                    rent_high=rent,
                    availability_status=parsed.get("availability_status", "AVAILABLE"),
                    available_units="1",
                    availability_date=parsed.get("availability_date", ""),
                    lease_term=parsed.get("lease_term", ""),
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_RENTCAFE_UR",
                )
            )
    return out


class RentCafeUnitRosterAdapter:
    """RentCafe modern theme — inline ``.floorplan-block`` + ``.par-units > .unit-container``.

    Adapter is gated on the presence of BOTH ``.floorplan-block`` AND
    ``.par-units``. Sites that have ``.floorplan-block`` without
    ``.par-units`` (e.g. Market Apartments Template A) do NOT route
    here — the MA adapter's own guard (``.floorplan-unit-single``) plus
    this adapter's complementary guard prevent cross-routing.
    """

    pms_name: str = "rentcafe_unit_roster"
    _fingerprints: list[str] = [
        # Generic RentCafe markers that ALSO commonly co-occur with this theme.
        "rentcafe",
        "yardi",
        # Theme-specific markers
        "par-units",
        "floorplan-block",
    ]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used="TIER_1_DOM_RENTCAFE_UR")
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            result.confidence = 0.0
            result.errors.append("rentcafe_ur: no live page")
            return result
        try:
            payload = await evaluate(_RENTCAFE_UR_DOM_JS)
        except Exception as exc:
            log.debug("rentcafe_ur evaluate failed err=%s", exc)
            payload = None
        if not isinstance(payload, dict) or not payload.get("ok"):
            result.confidence = 0.0
            reason = payload.get("reason") if isinstance(payload, dict) else "DOM JS returned non-dict"
            result.errors.append(f"rentcafe_ur: {reason}")
            return result
        plans = payload.get("plans") or []
        if not isinstance(plans, list) or not plans:
            result.confidence = 0.0
            result.errors.append("rentcafe_ur: zero plans in payload")
            return result

        winning = self._winning_url(page, ctx)
        units = parse_rentcafe_unit_roster(plans, winning)
        if not units:
            result.confidence = 0.0
            result.errors.append("rentcafe_ur: parser produced no rows")
            return result
        from ma_poc.extraction.post_process import post_process

        pp = post_process(units, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted > 0:
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.winning_url = winning
            result.confidence = min(0.93, 0.7 + 0.03 * pp.n_admitted)
            return result
        result.confidence = 0.0
        result.errors.append(
            f"rentcafe_ur: {len(units)} rows failed unit_validity post-process"
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
