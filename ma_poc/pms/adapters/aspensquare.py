"""Aspen Square Management adapter.

Aspen Square is a multifamily operator running a custom Strapi CMS at
``aspensquare.com``. Multi-property operator (memory-catalogued cluster):
each community lives at ``aspensquare.com/apartments/{state}/{city}/
{community}`` and the unit roster at the per-plan drill page
``.../{community}/floor-plans/{plan-slug}``.

Live-verified 2026-05-19 on southwood-acres + the-woodhaven drill (3
unit-level rows: 13-115 $2,043 Available Now; 10-63 $2,043 06/06/2026;
10-57 $2,023 08/07/2026).

Two-stage SSR DOM:

* Community page (``.aspen-c-full-width-card`` × N plans):
  - ``.aspen-c-heading``                  plan name ("The Woodhaven")
  - ``.aspen-c-badge__text``              avail status / count
                                            ("3 Available" / "Limited Availability")
  - ``.aspen-c-full-width-card__features``  "1 bed1 bath425 sq ft"
  - ``.aspen-c-full-width-card__detail--heading`` plan rent ("Call For Pricing")
  - ``a[href*="/floor-plans/"]``          drill URL → unit-level

* Drill page (``.aspen-c-unit-row`` rows, ``.aspen-c-table__cell`` cells):
  - cell[0] = unit number ("13 - 115")
  - cell[1] = rent ("$2,043")
  - cell[2] = availability ("Available Now" | "MM/DD/YYYY")

Probed cluster size: 3 in the deep-probe sample (16186, 6526, 14907) —
all aspensquare.com. Failure mode pre-fix: ``tier=NONE`` (no adapter).
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

# Runs in the live page. If the document already has plan cards, parses
# them; otherwise fetches the community page (``location.pathname`` if
# we're somewhere under /apartments/, else does nothing). For each plan
# card discovered, fetches the per-plan drill ``/floor-plans/{slug}``
# in-session and joins unit-level rows back to plan dims.
_ASPENSQUARE_DOM_JS = r"""
async () => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  let doc = document;
  if (!document.querySelector('.aspen-c-full-width-card')) {
    // Try to fetch the community page if our pathname is the drill page
    // ('.../floor-plans/{slug}' → strip last 2 segments to get community).
    let communityPath = location.pathname;
    if (/\/floor-plans\/[^/]+\/?$/.test(communityPath)) {
      communityPath = communityPath.replace(/\/floor-plans\/[^/]+\/?$/, '');
    }
    try {
      const r = await fetch(location.origin + communityPath, {credentials: 'include'});
      if (r.ok) doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    } catch (e) { /* fall through */ }
  }

  const cards = Array.from(doc.querySelectorAll('.aspen-c-full-width-card')).map((c) => {
    const a = c.querySelector('a[href*="/floor-plans/"]');
    return {
      name: T(c.querySelector('.aspen-c-heading')),
      specs: T(c.querySelector('.aspen-c-full-width-card__features')),
      planRent: T(c.querySelector('.aspen-c-full-width-card__detail--heading')),
      badge: T(c.querySelector('.aspen-c-badge__text')),
      drillPath: a ? a.getAttribute('href') : '',
    };
  });

  const out = [];
  for (const card of cards) {
    if (!card.drillPath) {
      out.push({...card, units: []});
      continue;
    }
    let drillDoc = null;
    try {
      const r = await fetch(location.origin + card.drillPath, {credentials: 'include'});
      if (r.ok) drillDoc = new DOMParser().parseFromString(await r.text(), 'text/html');
    } catch (e) { /* drill failed → plan-level only */ }
    const units = drillDoc
      ? Array.from(drillDoc.querySelectorAll('.aspen-c-unit-row'))
          .filter((r) => !/aspen-u-is-hidden/.test(r.className))
          .map((r) => {
            const cells = Array.from(r.querySelectorAll('.aspen-c-table__cell')).map(T);
            return {unit: cells[0] || '', rent: cells[1] || '', avail: cells[2] || ''};
          })
      : [];
    out.push({...card, units});
  }
  return out;
}
"""

_BED_RE = re.compile(r"(\d+)\s*bed", re.IGNORECASE)
_BATH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*bath", re.IGNORECASE)
_SQFT_RE = re.compile(r"(\d[\d,]*)\s*sq", re.IGNORECASE)
_MONEY_RE = re.compile(r"\$([\d,]+)")
_AVAIL_COUNT_RE = re.compile(r"(\d+)\s*Available", re.IGNORECASE)


def _parse_specs(specs: str) -> tuple[int | None, str, str]:
    """'1 bed1 bath425 sq ft' → (1, '1', '425')."""
    if not specs:
        return None, "", ""
    # AspenSquare concatenates fields ("Studio1 bath350 sq ft"), so no
    # trailing word boundary — use a negative lookahead to avoid "studios".
    if re.search(r"\bstudio(?![a-z])", specs, re.IGNORECASE):
        beds: int | None = 0
    else:
        bm = _BED_RE.search(specs)
        beds = int(bm.group(1)) if bm else None
    bath_m = _BATH_RE.search(specs)
    baths = bath_m.group(1) if bath_m else ""
    sqft_m = _SQFT_RE.search(specs)
    sqft = sqft_m.group(1).replace(",", "") if sqft_m else ""
    return beds, baths, sqft


def parse_aspensquare_cards(
    cards: list[dict[str, object]], url: str
) -> list[dict[str, str]]:
    """Emit unit-level rows when the drill page yielded units; else
    plan-level rows. Both tier as ``TIER_1_DOM_ASPENSQUARE``.
    """
    out: list[dict[str, str]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        name = str(card.get("name") or "").strip()
        specs = str(card.get("specs") or "")
        beds, baths, sqft = _parse_specs(specs)

        plan_rent_str = str(card.get("planRent") or "")
        money = _MONEY_RE.findall(plan_rent_str)
        plan_rent_lo = money_to_int(money[0]) if money else None
        plan_rent_hi = money_to_int(money[-1]) if money else None

        badge = str(card.get("badge") or "")
        count_m = _AVAIL_COUNT_RE.search(badge)
        plan_avail_count = count_m.group(1) if count_m else ""

        units_raw = card.get("units") or []

        if isinstance(units_raw, list) and units_raw:
            # Unit-level rows from the drill table.
            for u in units_raw:
                if not isinstance(u, dict):
                    continue
                unit_no = str(u.get("unit") or "").strip()
                rent_str = str(u.get("rent") or "")
                u_money = _MONEY_RE.findall(rent_str)
                rent = money_to_int(u_money[0]) if u_money else None
                avail = str(u.get("avail") or "").strip()
                status = "AVAILABLE"
                avail_date = ""
                if re.match(r"available\s+now", avail, re.IGNORECASE):
                    avail_date = ""
                elif re.match(r"\d{1,2}/\d{1,2}/\d{2,4}", avail):
                    avail_date = avail
                if not unit_no and rent is None:
                    continue
                out.append(
                    make_unit_dict(
                        floor_plan_name=name,
                        bed_label=bed_label_from(beds, name),
                        bedrooms=str(beds) if beds is not None else "",
                        bathrooms=str(baths),
                        sqft=sqft,
                        unit_number=unit_no,
                        rent_low=rent,
                        rent_high=rent,
                        availability_status=status,
                        available_units="1",
                        availability_date=avail_date,
                        source_api_url=url,
                        extraction_tier="TIER_1_DOM_ASPENSQUARE",
                    )
                )
        elif name:
            # Plan-level fallback (no drill / no units).
            status = (
                "UNAVAILABLE"
                if re.search(r"waitlist|limited", badge, re.IGNORECASE) and plan_rent_lo is None
                else "AVAILABLE"
            )
            out.append(
                make_unit_dict(
                    floor_plan_name=name,
                    bed_label=bed_label_from(beds, name),
                    bedrooms=str(beds) if beds is not None else "",
                    bathrooms=str(baths),
                    sqft=sqft,
                    unit_number="",
                    rent_range=format_rent_range(plan_rent_lo, plan_rent_hi),
                    rent_low=plan_rent_lo,
                    rent_high=plan_rent_hi,
                    availability_status=status,
                    available_units=plan_avail_count,
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_ASPENSQUARE",
                )
            )
    return out


class AspenSquareAdapter:
    """Aspen Square Management adapter — community page + per-plan unit drill."""

    pms_name: str = "aspensquare"
    _fingerprints: list[str] = ["aspensquare.com", "static.aspensquare.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Discover plans on the community page, drill each /floor-plans/
        {slug} for unit rows, emit unit-level dicts.

        2026-05-20: when no live Playwright page is available (L1-only
        pipeline mode), fall back to the Knock-community-hash path which
        works from the static HTML body alone. Every Aspen Square site
        embeds a Knock community hash + apiToken in the SSR-rendered
        config blob; calling Knock's public API resolves to unit-level
        inventory without needing the live DOM. Verified live 4/4 on
        Adley 72nd / The Avenue / Edgewood Court / Country Manor.
        """
        result = AdapterResult(tier_used="TIER_1_DOM_ASPENSQUARE")
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            # L1-only fallback: try the Knock community-hash recovery.
            knock_units = await self._try_knock_community_fallback(ctx, result)
            if knock_units:
                return knock_units
            result.confidence = 0.0
            result.errors.append("aspensquare: no live page to parse")
            return result

        try:
            cards = await evaluate(_ASPENSQUARE_DOM_JS)
        except Exception as exc:
            log.debug("AspenSquare DOM evaluate failed err=%s", exc)
            cards = None

        if not isinstance(cards, list) or not cards:
            result.confidence = 0.0
            result.errors.append("aspensquare: no .aspen-c-full-width-card blocks found")
            return result

        units = parse_aspensquare_cards(cards, self._winning_url(page, ctx))
        if units:
            from ma_poc.extraction.post_process import post_process

            pp = post_process(units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                # 2026-05-22 dup-prevention: see comment in apts247.py.
                result.units = pp.units
                result.plan_summaries = pp.plan_summaries
                result.winning_url = self._winning_url(page, ctx)
                result.confidence = min(0.95, 0.7 + 0.04 * pp.n_admitted)
                return result
            result.errors.append(
                f"ASPENSQUARE_VALIDITY_REJECTED: {len(units)} rows failed unit_validity"
            )

        result.confidence = 0.0
        result.errors.append("aspensquare: no parseable plan/unit data")
        return result

    @staticmethod
    def _winning_url(page: Page, ctx: AdapterContext) -> str:
        try:
            return page.url or getattr(ctx, "base_url", "") or ""
        except Exception:
            return getattr(ctx, "base_url", "") or ""

    @staticmethod
    async def _try_knock_community_fallback(
        ctx: AdapterContext, result: AdapterResult
    ) -> AdapterResult | None:
        """When the live-page DOM path can't run, try Knock-by-domain
        recovery from the static HTML body. Returns a populated
        ``AdapterResult`` (with ``tier_used`` stamped) on success, or
        ``None`` to fall through to the failure path.

        Every Aspen Square site embeds a Knock community hash + apiToken
        in the SSR config (verified 4/4 live 2026-05-20). The two-call
        Knock API resolves the property_id and returns unit-level
        inventory without auth.
        """
        fr = getattr(ctx, "fetch_result", None)
        body = getattr(fr, "body", None) if fr is not None else None
        if isinstance(body, bytes):
            try:
                html = body.decode("utf-8", errors="replace")
            except Exception:
                return None
        elif isinstance(body, str):
            html = body
        else:
            return None
        base_url = str(getattr(ctx, "base_url", "") or "")
        if not html or not base_url:
            return None
        try:
            from ma_poc.pms.adapters.knock import (
                _fetch_knock_units_by_domain,
                find_knock_community_hash,
            )
        except ImportError:
            return None
        # Only fire when the static HTML actually carries the Knock
        # config blob — otherwise the API calls would burn time on a
        # property that isn't really Knock-backed.
        if not find_knock_community_hash(html):
            return None
        try:
            pid, units, knock_plans = await _fetch_knock_units_by_domain(
                base_url, html
            )
        except Exception as exc:
            result.errors.append(
                f"aspensquare-knock-fallback-error: {type(exc).__name__}: "
                f"{str(exc)[:120]}"
            )
            return None
        if not pid or (not units and not knock_plans):
            return None
        from ma_poc.extraction.post_process import post_process

        pp = post_process(units, property_id=getattr(ctx, "property_id", None))
        # 2026-05-22 Fix 3 (downstream): merge Knock's layout-level
        # plan_summaries with any plan rows the post_process classifier
        # routed. The post_process pass only sees the unit rows here so
        # its plan_summaries list is empty; the Knock-derived
        # plan_summaries cover floor plans with no available units.
        merged_plans = list(pp.plan_summaries) + list(knock_plans)
        if pp.n_admitted == 0 and not merged_plans:
            return None
        # 2026-05-22 dup-prevention: see comment in apts247.py.
        result.units = pp.units
        result.plan_summaries = merged_plans
        result.tier_used = "TIER_1_API_ASPENSQUARE_KNOCK"
        result.winning_url = (
            f"https://doorway-api.knockrentals.com/v1/property/{pid}/units"
        )
        # Slight confidence boost when plan_summaries are present even
        # without admitted units (we DID extract something useful).
        score_basis = pp.n_admitted + 0.5 * len(merged_plans)
        result.confidence = min(0.92, 0.65 + 0.04 * score_basis)
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
