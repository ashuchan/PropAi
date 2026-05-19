"""
ResMan Implicity adapter.

Research log
------------
ResMan's "Implicity" prospect portal is embedded as a cross-origin iframe on
the property marketing site:

    <iframe src="https://implicity.myresman.com/Portal/Applicants/Availability
                  ?a={account_id}&p={property_guid}">

The availability list is **move-in-date gated**: the un-parameterised page
renders only "no matching units" placeholders ($0, zero rows). Appending
``&MoveInDate={M/D/YYYY}`` makes the server render the full unit roster SSR —
no form interaction or XHR needed.

Verified live 2026-05-19 on regaliabellaterra.com (a real ``tier=NONE`` /
mislabelled-"Knock" zero-unit failure — the site has only a Knock *chat*
widget; the actual availability backend is this ResMan Implicity iframe):
``...Availability?a=1450&p=57495da9-...&MoveInDate=05/30/2026`` returned 15
``.unit.available-unit`` rows with full unit-level data.

Per-unit SSR DOM (stable class names):
  - ``.panel-heading``        — unit number ("21008")
  - ``.fv``                   — square footage ("728")
  - ``.unit-lease-term-col``  — lease term ("12 Months")
  - ``.unit-rent-value`` / ``.rent-value`` / ``.lease-terms-pricing`` text —
                                rent ("1,299.00"); take the min plausible
                                money token (lowest lease-term price)
  - row innerText carries "Bedrooms 1 Bathrooms 1.00 Building 2 Floor 1"
  - ``.unit-available-date`` text carries "Available on 7/15/2026"
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import TYPE_CHECKING

from ma_poc.pms.adapters._parsing import bed_label_from, make_unit_dict, money_to_int
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

# Implicity availability iframe URL — host is constant; a/p identify the
# property. Captured from the parent marketing page's iframe src.
_IMPLICITY_IFRAME_RE = re.compile(
    r"https?://implicity\.myresman\.com/Portal/Applicants/Availability"
    r"\?[^\s\"'<>]*",
    re.IGNORECASE,
)

# Near-future move-in date used to un-gate the availability list. 30 days
# comfortably clears the typical "available within N days" server filter
# (business_rules move_in_lag_days default is 14).
_MOVE_IN_LAG_DAYS = 30


def _move_in_date(today: datetime.date | None = None) -> str:
    d = (today or datetime.date.today()) + datetime.timedelta(days=_MOVE_IN_LAG_DAYS)
    return f"{d.month}/{d.day}/{d.year}"


# Runs in the page. Resolve the Implicity availability URL (from the current
# location if we are already on it, else from the parent page's iframe src),
# append MoveInDate, fetch it, and map the .unit.available-unit rows. Returns
# [] for non-ResMan pages so other sites are unaffected.
_RESMAN_DOM_JS = r"""
async (moveInDate) => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');

  let base = '';
  if (/implicity\.myresman\.com$/i.test(location.host)) {
    base = location.origin + location.pathname + location.search;
  } else {
    const ifr = Array.from(document.querySelectorAll('iframe'))
      .map((f) => f.src || '')
      .find((s) => /implicity\.myresman\.com\/Portal\/Applicants\/Availability/i.test(s));
    if (!ifr) return [];
    base = ifr;
  }
  let u;
  try { u = new URL(base); } catch (e) { return []; }
  u.searchParams.set('MoveInDate', moveInDate);

  let doc;
  try {
    const r = await fetch(u.toString(), {credentials: 'include'});
    if (!r.ok) return [];
    doc = new DOMParser().parseFromString(await r.text(), 'text/html');
  } catch (e) { return []; }

  return Array.from(doc.querySelectorAll('.unit.available-unit')).map((row) => ({
    unit: T(row.querySelector('.panel-heading')),
    sqft: T(row.querySelector('.fv')),
    term: T(row.querySelector('.unit-lease-term-col')),
    rentText: T(row.querySelector('.lease-terms-pricing'))
      || T(row.querySelector('.unit-rent-table')),
    availText: T(row.querySelector('.unit-available-date')),
    text: (row.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 400),
  }));
}
"""

_BED_RE = re.compile(r"Bedrooms\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
_BATH_RE = re.compile(r"Bathrooms\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
_BLDG_RE = re.compile(r"Building\s+(\S+)", re.IGNORECASE)
_FLOOR_RE = re.compile(r"Floor\s+(\d+)", re.IGNORECASE)
_AVAIL_RE = re.compile(r"Available\s+on\s+(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)
# Money like "1,299.00" (ResMan omits the $ sign). Capture the dollar
# (pre-decimal) part so "1,299.00" → 1299, not 299.
_MONEY_RE = re.compile(r"(\d[\d,]*)\.\d{2}\b")


def parse_resman_units(rows: list[dict[str, str]], url: str) -> list[dict[str, str]]:
    """Parse ResMan Implicity ``.unit.available-unit`` rows into unit dicts.

    True unit-level: one row per physical apartment (unit number, beds/baths,
    sqft, rent at the shown lease term, available date, building, floor).
    Rows with no numeric dimension are dropped by the caller's post_process
    validity gate.
    """
    units: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = row.get("text") or ""
        unit_no = (row.get("unit") or "").strip()

        bed_m = _BED_RE.search(text)
        bath_m = _BATH_RE.search(text)
        beds = int(float(bed_m.group(1))) if bed_m else None
        baths = bath_m.group(1) if bath_m else ""

        sqft_m = re.search(r"\d[\d,]*", row.get("sqft") or "")
        sqft = sqft_m.group(0).replace(",", "") if sqft_m else ""

        # Lowest plausible money token across the lease-term pricing table.
        # Rent lives in the row text as "...Rent 12 Months 1,299.00 Total
        # Rent ... 1,349.00"; fees (Valet Trash 25.00 etc.) are < 100 and
        # excluded by the threshold. The base rent is the lowest plausible
        # money token (below Total Rent + Other Charges).
        haystack = f"{row.get('rentText') or ''} {text}"
        money: list[int] = []
        for tok in _MONEY_RE.findall(haystack):
            val = money_to_int(tok)
            if val is not None and val > 100:
                money.append(val)
        rent = min(money) if money else None

        avail_m = _AVAIL_RE.search(row.get("availText") or text)
        availability_date = avail_m.group(1) if avail_m else ""

        bldg_m = _BLDG_RE.search(text)
        floor_m = _FLOOR_RE.search(text)

        if not unit_no and beds is None and not sqft:
            continue

        units.append(
            make_unit_dict(
                floor_plan_name="",
                bed_label=bed_label_from(beds, ""),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths),
                sqft=sqft,
                unit_number=unit_no,
                building=bldg_m.group(1) if bldg_m else "",
                floor=floor_m.group(1) if floor_m else "",
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                available_units="1",
                availability_date=availability_date,
                lease_term=(row.get("term") or "").strip(),
                source_api_url=url,
                extraction_tier="TIER_1_DOM_RESMAN_IMPLICITY",
            )
        )
    return units


class ResManAdapter:
    """ResMan Implicity adapter. Parses the move-in-date-gated SSR roster."""

    pms_name: str = "resman"
    _fingerprints: list[str] = ["implicity.myresman.com", "myresman.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract unit-level data from the ResMan Implicity availability iframe.

        Resolves the Implicity URL (current page or parent iframe src), appends
        a near-future ``MoveInDate`` to un-gate the roster, fetches it
        in-session, and parses the SSR ``.unit.available-unit`` rows.
        """
        result = AdapterResult(tier_used="TIER_1_DOM_RESMAN_IMPLICITY")

        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            result.confidence = 0.0
            result.errors.append("ResMan: no live page to parse")
            return result

        try:
            rows = await evaluate(_RESMAN_DOM_JS, _move_in_date())
        except Exception as exc:
            log.debug("ResMan Implicity evaluate failed err=%s", exc)
            rows = None

        if not isinstance(rows, list) or not rows:
            result.confidence = 0.0
            result.errors.append(
                "ResMan: no Implicity availability rows (no iframe / date-gated empty)"
            )
            return result

        units = parse_resman_units(rows, self._winning_url(page, ctx))
        if units:
            from ma_poc.extraction.post_process import post_process

            pp = post_process(units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                result.units = pp.admitted
                result.plan_summaries = pp.plan_summaries
                result.winning_url = self._winning_url(page, ctx)
                result.confidence = min(0.95, 0.7 + 0.05 * pp.n_admitted)
                return result
            result.errors.append(
                f"RESMAN_VALIDITY_REJECTED: {len(units)} rows failed "
                f"unit_validity (no numeric dimension)"
            )

        result.confidence = 0.0
        result.errors.append("ResMan: no parseable unit data")
        return result

    @staticmethod
    def _winning_url(page: Page, ctx: AdapterContext) -> str:
        try:
            return page.url or getattr(ctx, "base_url", "") or ""
        except Exception:
            return getattr(ctx, "base_url", "") or ""

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
