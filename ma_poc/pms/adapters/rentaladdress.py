"""RentalAddress.com adapter (2026-05-25, regr#8 / Cedar Ridge).

User-flagged: Cedar Ridge (pid 218586, cedarridgeapts.rentaladdress.com)
extracted 2 plan rows with floor_plan_name="598 Bedroom / 1 Bath" and
area=-1 — the sqft value "598" got pulled INTO the plan name string
because the generic plan-text parser had no idea how to read the
operator's clean ``.square_feet.value`` div structure.

The RentalAddress.com platform serves SSR /floor_plans pages with a
deterministic label/value div layout:

  <div class="floor_plan_list" data-property-name="...">
    <div class="floor_plan row1">
      <div class="floor_plan_name">1 Bedroom/ 1 Bath</div>
      <div class="floor_plan_content">
        <div class="text">
          <div class="summary">
            <div class="row">
              <div class="square_feet label">Square Feet</div>
              <div class="square_feet value">598</div>
            </div>
            <div class="row">
              <div class="bedrooms label">Bedrooms</div>
              <div class="bedrooms value">1</div>
            </div>
            <div class="row">
              <div class="bathrooms label">Bathrooms</div>
              <div class="bathrooms value">1 Bath</div>
            </div>
            <div class="row">
              <div class="rent label">Rent</div>
              <div class="rent value">$1,475</div>
            </div>
            <div class="row">
              <div class="deposit label">Deposit</div>
              <div class="deposit value">$500 - $800</div>
            </div>
          </div>
        </div>
      </div>
    </div>
    ... more .floor_plan blocks ...
  </div>

Per-unit availability is NOT published on the platform — every plan
has a "Check Availability" CTA link (``/apply_online``) instead of a
per-unit roster. So this adapter emits at the PLAN_LEVEL tier
suffix and the Phase 16 has_rent guard (commit e024aa1) correctly
gates the date fallback for these synthetic rows.

Cohort: 1 confirmed property in canary 1ef1060 (Cedar Ridge); the
parser is small and broadly correct so any sister site on the same
platform recovers for free.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

from bs4 import BeautifulSoup

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

_TIER = "TIER_1_DOM_RENTALADDRESS_PLAN_LEVEL"

_BED_RE = re.compile(r"(\d+)\s*(?:Bed|BR)", re.IGNORECASE)
_BATH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:Bath|BA)", re.IGNORECASE)
_SQFT_RE = re.compile(r"(\d[\d,]*)\s*(?:sq\s*ft|sqft|ft[²2])?", re.IGNORECASE)
_MONEY_RE = re.compile(r"\$([\d,]+)")


def _summary_value(summary_div: Any, field: str) -> str:
    """Read the ``<div class="{field} value">VALUE</div>`` text from
    the .summary block. Returns "" when absent."""
    if summary_div is None:
        return ""
    # Match by class containing both field and "value"
    for div in summary_div.find_all("div", class_=lambda c: bool(c) and "value" in c.split()):
        cls = div.get("class") or []
        if field in cls:
            return div.get_text(strip=True)
    return ""


def parse_rentaladdress_floorplans(html: str, source_url: str) -> list[dict[str, str]]:
    """Parse the RentalAddress.com /floor_plans SSR HTML into plan-level
    unit dicts. Returns one row per .floor_plan block."""
    if not html or "floor_plan_list" not in html:
        return []

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    units: list[dict[str, str]] = []

    for plan in soup.select(".floor_plan_list .floor_plan"):
        name_el = plan.find(class_="floor_plan_name")
        plan_name = name_el.get_text(strip=True) if name_el else ""
        if not plan_name:
            continue

        summary = plan.find(class_="summary")

        sqft_raw = _summary_value(summary, "square_feet")
        bedrooms_raw = _summary_value(summary, "bedrooms")
        bathrooms_raw = _summary_value(summary, "bathrooms")
        rent_raw = _summary_value(summary, "rent")

        # Sqft is plain integer string; extract digits defensively.
        sqft_m = _SQFT_RE.search(sqft_raw or "")
        sqft = sqft_m.group(1).replace(",", "") if sqft_m else ""

        # Bedrooms is plain integer (1, 2, 3) or "Studio".
        if re.search(r"\bStudio\b", bedrooms_raw or "", re.IGNORECASE):
            beds = 0
        else:
            bed_m = _BED_RE.search(bedrooms_raw or "") or re.match(
                r"^\s*(\d+)\s*$", bedrooms_raw or ""
            )
            beds = int(bed_m.group(1)) if bed_m else None

        # Bathrooms format: "1 Bath" / "2 Bath" / "1.5 Bath" / "2 Baths".
        bath_m = _BATH_RE.search(bathrooms_raw or "") or re.match(
            r"^\s*(\d+(?:\.\d+)?)\s*$", bathrooms_raw or ""
        )
        baths = bath_m.group(1) if bath_m else ""

        # Rent format: "$1,475" or "$1,475 - $1,750"
        rent_matches = _MONEY_RE.findall(rent_raw or "")
        rent_lo = money_to_int(rent_matches[0]) if rent_matches else None
        rent_hi = money_to_int(rent_matches[-1]) if rent_matches else None

        units.append(
            make_unit_dict(
                floor_plan_name=plan_name,
                bed_label=bed_label_from(beds, plan_name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths),
                sqft=sqft,
                unit_number="",  # plan-level — operator publishes CTA, not per-unit
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi,
                availability_status="UNKNOWN",  # CTA-only; no per-unit available info
                source_api_url=source_url,
                extraction_tier=_TIER,
            )
        )
    return units


def _floorplans_url(page: Page | None, ctx: AdapterContext) -> str:
    """Best-effort canonical ``{origin}/floor_plans`` URL."""
    candidate = ""
    if page is not None:
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
    return urlunparse((p.scheme, p.netloc, "/floor_plans", "", "", ""))


class RentalAddressAdapter:
    """RentalAddress.com plan-level extractor."""

    pms_name: str = "rentaladdress"
    _fingerprints: list[str] = ["rentaladdress.com", "floor_plan_list"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract plan-level rows from the RentalAddress.com /floor_plans page."""
        result = AdapterResult(tier_used=_TIER)

        # Prefer the rendered page; fall back to self-fetch when not yet there.
        html = ""
        fr = getattr(ctx, "fetch_result", None)
        body = getattr(fr, "body", None) if fr is not None else None
        if isinstance(body, bytes):
            try:
                html = body.decode("utf-8", errors="replace")
            except Exception:
                html = ""
        elif isinstance(body, str):
            html = body
        if html and "floor_plan_list" not in html and page is not None:
            try:
                html = await page.content()
            except Exception:
                html = html

        floorplans_url = _floorplans_url(page, ctx)
        if "floor_plan_list" not in html:
            # Self-fetch /floor_plans
            try:
                import httpx
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,*/*;q=0.8",
                }
                async with httpx.AsyncClient(
                    timeout=15.0, follow_redirects=True, headers=headers
                ) as c:
                    r = await c.get(floorplans_url)
                if r.status_code == 200:
                    html = r.text
            except Exception as exc:
                log.debug("rentaladdress self-fetch failed url=%s err=%s", floorplans_url, exc)

        units = parse_rentaladdress_floorplans(html or "", floorplans_url)
        if units:
            from ma_poc.extraction.post_process import post_process

            pp = post_process(units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                result.units = pp.admitted
                result.plan_summaries = pp.plan_summaries
                result.winning_url = floorplans_url
                result.confidence = min(0.90, 0.65 + 0.05 * pp.n_admitted)
                return result
            result.errors.append(
                f"RENTALADDRESS_VALIDITY_REJECTED: {len(units)} rows failed unit_validity"
            )

        result.confidence = 0.0
        result.errors.append("rentaladdress: no .floor_plan blocks found")
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
