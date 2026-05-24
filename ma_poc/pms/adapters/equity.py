"""
Equity Residential adapter.

Research log (2026-05-18, user DevTools + probe verified)
--------------------------------------------------------
Single REIT, single host (equityapartments.com). AngularJS site behind
Cloudflare. Critically — and contrary to an initial mis-read — the
per-unit availability is **server-rendered into the property-page
HTML** (NOT SightMap, NOT an XHR, NOT bot-walled). curl_cffi chrome
impersonation clears the Cloudflare page check and returns the full
200 HTML with every available unit inlined. DevTools confirmed: zero
unit-data XHRs; the unit list is in the markup.

Per-unit block shape (verified, Westerly propertyId 4274, 16 units):

  <!-- ledgerId: 24112, buildingId: 1, unitId: 175 -->
  <ea5-unit value="vm.BedroomTypes[0].AvailableUnits[0]" ...>
   <div class="unit"><div class="unit-expanded-card">
    <span class="pricing">$1,150</span>
    <span class="time-period">12 mo</span>
    0 Bed <b>/</b> 1 Bath
    <span>576 sq.ft.</span>
    Available 6/4/2026
    <img class="static" src=".../4274-FP-383881-0-1-576" alt="S3" />
   ...
  </ea5-unit>

``unitId`` is the unit identifier; the floorplan img ``alt`` is the
plan name. Generalises across all ~31 Equity properties (one shared
Angular template). Deterministic, public, no render required.
"""

from __future__ import annotations

import html as _html
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

OLL_TIER = "TIER_1_API_EQUITY"

# unitId is alphanumeric: numeric on most properties (175) but
# alphanumeric on NYC stock (11E, 02I). ledgerId/buildingId stay numeric.
_UNIT_BLOCK_RE = re.compile(
    r"<!--\s*ledgerId:\s*(\d+),\s*buildingId:\s*(\d+),\s*unitId:\s*([A-Za-z0-9]+)\s*-->"
    r"(.*?)</ea5-unit>",
    re.IGNORECASE | re.DOTALL,
)
_FLOOR_RE = re.compile(r">\s*Floor\s+(\w+)\s*<", re.IGNORECASE)
_PRICE_RE = re.compile(r'class="pricing"\s*>\s*\$?\s*([\d,]+)', re.IGNORECASE)
_TERM_RE = re.compile(r'class="time-period"\s*>\s*([^<]+)', re.IGNORECASE)
_BEDBATH_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*Bed\s*(?:<b>\s*/\s*</b>|/)\s*([\d.]+)\s*Bath",
    re.IGNORECASE,
)
_STUDIO_RE = re.compile(r"\bStudio\b", re.IGNORECASE)
_SQFT_RE = re.compile(r"([\d,]+)\s*sq\.?\s*ft", re.IGNORECASE)
_AVAIL_RE = re.compile(r"Available\s+(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE)
_FP_ALT_RE = re.compile(r'class="static"[^>]*\balt="([^"]{1,40})"', re.IGNORECASE)


def _mdy_to_iso(s: str) -> str:
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s or "")
    if not m:
        return ""
    mo, da, yr = m.groups()
    if len(yr) == 2:
        yr = "20" + yr
    return f"{yr}-{int(mo):02d}-{int(da):02d}"


def parse_equity_units(html: str, url: str) -> list[dict[str, str]]:
    """Parse Equity's server-rendered per-unit blocks into unit dicts."""
    d = _html.unescape(html or "")
    units: list[dict[str, str]] = []
    for m in _UNIT_BLOCK_RE.finditer(d):
        _ledger, building, unit_id, seg = m.groups()
        pm = _PRICE_RE.search(seg)
        rent = money_to_int(pm.group(1)) if pm else None
        if rent is None:
            continue  # no price -> not a genuine available unit row
        tm = _TERM_RE.search(seg)
        lease_term = tm.group(1).strip() if tm else ""
        bb = _BEDBATH_RE.search(seg)
        if bb:
            beds: int | None = int(float(bb.group(1)))
            try:
                baths: float | None = float(bb.group(2))
            except (TypeError, ValueError):
                baths = None
        elif _STUDIO_RE.search(seg):
            beds, baths = 0, None
        else:
            beds, baths = None, None
        sm = _SQFT_RE.search(seg)
        sqft = sm.group(1).replace(",", "") if sm else ""
        am = _AVAIL_RE.search(seg)
        avail = _mdy_to_iso(am.group(1)) if am else ""
        fp = _FP_ALT_RE.search(seg)
        fp_name = fp.group(1).strip() if fp else ""
        flm = _FLOOR_RE.search(seg)
        floor = flm.group(1).strip() if flm else ""

        units.append(
            make_unit_dict(
                floor_plan_name=fp_name,
                bed_label=bed_label_from(beds, fp_name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=(
                    str(int(baths)) if baths is not None and baths == int(baths)
                    else (str(baths) if baths is not None else "")
                ),
                sqft=sqft,
                unit_number=str(unit_id),
                floor=floor,
                building=str(building),
                rent_range=format_rent_range(rent, rent),
                rent_low=rent,
                rent_high=rent,
                lease_term=lease_term,
                availability_status="AVAILABLE",
                availability_date=avail,
                source_api_url=url,
                extraction_tier=OLL_TIER,
            )
        )
    return units


def _html_for(ctx: AdapterContext, page: Page | None) -> tuple[str, str]:
    """Best HTML for parsing + the source URL.

    The unit blocks are in the raw server HTML; curl_cffi clears the
    Cloudflare page check, so a probe_get refetch is the most reliable
    source. Prefer an already-captured body that has the marker.
    """
    base = getattr(ctx, "base_url", "") or ""
    fr = getattr(ctx, "fetch_result", None)
    body = getattr(fr, "body", None)
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:
            body = None
    # Only trust an already-captured body if it carries REAL rendered
    # prices. The pipeline's patchright render can leave the ``ea5-unit``
    # template present but with empty Angular ``{{}}`` bindings (server
    # values wiped by client hydration / CF), which parses to 0 units —
    # whereas the raw server HTML (curl_cffi, below) has them inlined.
    if (
        isinstance(body, str)
        and "ledgerId:" in body
        and _PRICE_RE.search(body) is not None
    ):
        return body, base
    if base:
        try:
            from ma_poc.pms.adapters._probe import probe_get

            # Same-origin (base is ctx.base_url) — Equity property pages
            # live on equityapartments.com/... — gate Layer 1 declines proxy.
            r = probe_get(base, ctx=ctx, stage="equity_property", timeout=20)
            if getattr(r, "status_code", 0) == 200 and r.text:
                return str(r.text), base
        except Exception:
            pass
    if isinstance(body, str) and body:
        return body, base
    return "", base


class EquityAdapter:
    """Equity Residential (equityapartments.com) PMS adapter.

    Tier labels (post-2026-05-20 cluster #7 fix):

      ``TIER_1_API_EQUITY``                — real unit-level data parsed
      ``TIER_1_API_EQUITY_EMPTY``          — ea5-unit blocks parsed but
                                             post_process rejected all rows
      ``TIER_1_API_EQUITY_NO_RESPONSE``    — no ea5-unit blocks in the
                                             fetched HTML (page may be a
                                             redirect/legacy URL, or the
                                             Cloudflare check returned a
                                             challenge body the parser
                                             can't handle)

    Empty-exit labels trigger the scraper's Path B/C retry + Step 8
    generic fallback. Previously the bare success label was kept on
    every failure path, blocking both recovery paths."""

    pms_name: str = "equity"
    _fingerprints: list[str] = ["equityapartments.com", "ea5-unit"]

    async def try_dom(self, page: Any, html: str, ctx: AdapterContext) -> Any:
        """2026-05-24 Phase 1 cascade hook — deterministic DOM extraction
        for Equity Residential ``<ea5-unit>`` blocks.

        Wraps the existing ``parse_equity_units`` regex path (~31
        properties on a shared Angular template, regex over HTML
        comments + ea5-unit segment). Units are routed through the
        shared dq_guards before return.

        Returns ``AdapterDomResult.empty(...)`` when the HTML lacks
        ``ea5-unit`` markers OR the parser returns 0 candidates.
        """
        from ma_poc.pms.adapters.base import AdapterDomResult

        if not html or "ea5-unit" not in html:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_EQUITY",
                reason="no_ea5_unit_marker",
            )
        try:
            url = getattr(ctx, "base_url", "") or ""
            raw_units = parse_equity_units(html, url)
        except Exception as e:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_EQUITY",
                reason=f"parse_exception:{type(e).__name__}",
            )
        if not raw_units:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_EQUITY",
                reason="parser_silent_empty",
            )
        try:
            from ma_poc.extraction.dq_guards import apply_unit_guards
            guarded = apply_unit_guards(
                raw_units,
                property_id=getattr(ctx, "property_id", ""),
                source_html=html,
                detect_same_rent=True,
            )
        except Exception:
            guarded = raw_units
        if not guarded:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_EQUITY",
                reason="dq_guards_rejected_all",
            )
        return AdapterDomResult(
            units=guarded,
            plan_summaries=[],
            tier_used="TIER_3_DOM_EQUITY",
            selector_signature="ea5-unit+ledgerId-comment",
            confidence=0.95,
            debug={"raw_count": len(raw_units), "guarded_count": len(guarded)},
        )

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=OLL_TIER)
        html, url = _html_for(ctx, page)
        all_units = parse_equity_units(html, url) if html else []

        if all_units:
            from ma_poc.extraction.post_process import post_process

            _pp_parsed = len(all_units)
            _pp = post_process(all_units, property_id=getattr(ctx, "property_id", None))
            if _pp.n_admitted > 0:
                result.units = _pp.admitted
                result.plan_summaries = _pp.plan_summaries
                result.winning_url = url or None
                result.confidence = min(0.90, 0.7 + 0.05 * _pp.n_admitted)
            else:
                # ea5-unit blocks parsed but validity gate rejected all rows.
                # Surface as empty-exit so Path B/C + Step 8 can engage.
                result.tier_used = f"{OLL_TIER}_EMPTY"
                result.confidence = 0.0
                result.errors.append(
                    f"EQUITY_VALIDITY_REJECTED: {_pp_parsed} parsed rows "
                    f"failed unit_validity"
                )
        else:
            # No ea5-unit blocks at all. The cluster #7 pattern:
            # equityapartments.com URLs that returned a non-Angular body
            # (legacy .aspx redirect, Cloudflare challenge, or genuine
            # no-availability). Empty-exit label lets the retry/fallback
            # paths run.
            result.tier_used = f"{OLL_TIER}_NO_RESPONSE"
            result.confidence = 0.0
            result.errors.append(
                "No Equity unit data (ea5-unit blocks not found in HTML)"
            )
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
