"""IMT "Spaces" theme — IMT Residential portfolio custom CMS.

IMT Residential operates ~25 multifamily properties fleet-wide on a
custom CMS branded as "Spaces". The platform renders plan cards as
``<article class="spaces-plan">`` elements with data attributes
carrying all the plan-level inventory: rent, sqft, bed count, bath
count, soonest-available date, plan name, plan id.

DOM contract (verified live 2026-05-21 on
www.imtresidential.com/properties/imt-sorrento-valley/apartments/,
57 spaces-plan articles):

  <article class="spaces-plan spaces__plan upgraded 2bed 2bath
                  price_2500plus tag-upgraded
                  spaces-community-imt-sorrento-valley
                  spaces-market-all-communities spaces-market-california
                  floor_36248 patio-or-balcony washer-and-dryer ..."
           title="B2 Upgrade"
           data-spaces-obj="plan"
           data-spaces-plan="147445"               (plan id)
           data-spaces-soonest="2026-07-07"        (ISO available date)
           data-spaces-sort-price="3195"           (numeric rent)
           data-spaces-sort-area="878"             (sqft)
           data-spaces-sort-bed="2"                (bed count)
           data-spaces-bath-count="2"              (bath count)
           data-spaces-sort-plan-name="B2 Upgrade" (plan name)
           data-spaces-sort-date="1783382400"      (unix avail)
           data-spaces-available="true">
    ...
  </article>

Plan-level only — IMT does NOT publish per-unit roster publicly. Each
``<article>`` represents one floor-plan layout; multiple units of the
same plan are aggregated. The class list also encodes bedroom-count
("2bed"), bath-count ("2bath"), price bucket ("price_2500plus"), and
the community slug ("spaces-community-imt-sorrento-valley") for filter-
matching but the data-* attrs are the authoritative source.

Portfolio leverage: imtresidential.com hosts every IMT property under
``/properties/{slug}/apartments/``. One adapter unlocks ~25 sites.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

_IMT_SPACES_DOM_JS = r"""
async () => {
  const arts = Array.from(document.querySelectorAll('article.spaces-plan, article[class*="spaces-plan"]'));
  if (arts.length === 0) {
    return {ok: false, reason: 'no article.spaces-plan elements on page'};
  }
  const plans = arts.map((a) => ({
    classes: a.className || '',
    titleAttr: a.getAttribute('title') || '',
    data: Object.assign({}, a.dataset || {}),
  }));
  return {ok: true, plans: plans};
}
"""


def _parse_int(s: str) -> int | None:
    if not s:
        return None
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        try:
            return int(float(str(s).strip()))
        except (TypeError, ValueError):
            return None


def _normalize_iso_date(s: str) -> str:
    """``data-spaces-soonest`` is ISO ``YYYY-MM-DD`` already — pass through
    if valid, else empty."""
    if not s:
        return ""
    s = s.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    return ""


def parse_imt_spaces_html(html: str, url: str) -> list[dict]:
    """Body-fallback parser for Jugnu (page=None). Walks article.spaces-plan
    elements in the snapshot HTML mirroring ``_IMT_SPACES_DOM_JS``. Returns
    ``[]`` when BeautifulSoup is unavailable or no matching articles exist."""
    if not html:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return []
    arts = soup.select("article.spaces-plan, article[class*='spaces-plan']")
    if not arts:
        return []
    plans: list[dict] = []
    for a in arts:
        # Translate HTML kebab-case data attrs to JS camelCase keys to
        # match what _IMT_SPACES_DOM_JS produces via element.dataset
        # (e.g. data-spaces-sort-bed -> spacesSortBed).
        data: dict[str, str] = {}
        for attr_name, attr_val in (a.attrs or {}).items():
            if not attr_name.startswith("data-"):
                continue
            key_parts = attr_name[len("data-"):].split("-")
            cam = key_parts[0] + "".join(p.title() for p in key_parts[1:])
            if isinstance(attr_val, list):
                attr_val = " ".join(attr_val)
            data[cam] = str(attr_val or "")
        plans.append({
            "classes": " ".join(a.get("class") or []),
            "titleAttr": a.get("title", "") or "",
            "data": data,
        })
    return parse_imt_spaces_plans(plans, url)


def parse_imt_spaces_plans(plans: list[dict], url: str) -> list[dict]:
    """Emit plan-level rows from IMT Spaces ``article.spaces-plan`` data."""
    out: list[dict] = []
    for p in plans:
        if not isinstance(p, dict):
            continue
        data = p.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        # Plan name — title attr or data-spaces-sort-plan-name (prefer title).
        plan_name = (
            str(p.get("titleAttr") or "").strip()
            or str(data.get("spacesSortPlanName") or "").strip()
        )
        beds = _parse_int(str(data.get("spacesSortBed") or ""))
        baths_raw = str(data.get("spacesBathCount") or "").strip()
        try:
            # Baths can be float (1.5); keep as string so downstream gets the literal.
            float(baths_raw)
            baths = baths_raw
        except (TypeError, ValueError):
            baths = ""
        sqft = str(data.get("spacesSortArea") or "").strip()
        rent = _parse_int(str(data.get("spacesSortPrice") or ""))
        avail_date = _normalize_iso_date(str(data.get("spacesSoonest") or ""))
        is_available = str(data.get("spacesAvailable") or "").lower() == "true"
        status = "AVAILABLE" if is_available else "UNAVAILABLE"

        if not plan_name and rent is None:
            continue

        out.append(
            make_unit_dict(
                floor_plan_name=plan_name,
                bed_label=bed_label_from(beds, plan_name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=baths,
                sqft=sqft,
                unit_number="",  # plan-level only
                rent_low=rent,
                rent_high=rent,
                rent_range=format_rent_range(rent, rent),
                availability_status=status,
                availability_date=avail_date,
                source_api_url=url,
                extraction_tier="TIER_1_DOM_IMT_SPACES",
            )
        )
    return out


class ImtSpacesAdapter:
    """IMT "Spaces" theme — plan-level extraction from ``article.spaces-plan``
    data-* attributes. Plan-only by design (IMT doesn't publish per-unit
    public roster)."""

    pms_name: str = "imt_spaces"
    _fingerprints: list[str] = [
        "imtresidential.com",
        "spaces-plan",
        "spaces-community-",
        "data-spaces-",
    ]

    async def try_dom(self, page: Any, html: str, ctx: AdapterContext) -> Any:
        """2026-05-24 Phase 1 cascade hook — deterministic DOM extraction
        for IMT 'Spaces' theme. Wraps ``parse_imt_spaces_html`` (BS4 over
        ``article.spaces-plan`` ``data-spaces-*`` attributes) and routes
        units through dq_guards.

        IMT is plan-only by design — emitted to ``plan_summaries``
        rather than ``units`` so the partition contract is preserved.
        """
        from ma_poc.pms.adapters.base import AdapterDomResult

        if not html or "spaces-plan" not in html:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_IMT_SPACES",
                reason="no_spaces_plan_marker",
            )
        try:
            url = getattr(ctx, "base_url", "") or ""
            raw_units = parse_imt_spaces_html(html, url)
        except Exception as e:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_IMT_SPACES",
                reason=f"parse_exception:{type(e).__name__}",
            )
        if not raw_units:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_IMT_SPACES",
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
                tier="TIER_3_DOM_IMT_SPACES",
                reason="dq_guards_rejected_all",
            )
        return AdapterDomResult(
            units=guarded,
            plan_summaries=[],
            tier_used="TIER_3_DOM_IMT_SPACES",
            selector_signature="article.spaces-plan[data-spaces-*]",
            confidence=0.85 if len(guarded) >= 3 else 0.7,
            debug={"raw_count": len(raw_units), "guarded_count": len(guarded)},
        )

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used="TIER_1_DOM_IMT_SPACES")
        # 2026-05-24: page.evaluate when a live page is available, else
        # BeautifulSoup body-walk (Jugnu page=None contract).
        winning = self._winning_url(page, ctx)
        rows: list[dict] = []
        evaluate = getattr(page, "evaluate", None) if page is not None else None
        if callable(evaluate):
            try:
                payload = await evaluate(_IMT_SPACES_DOM_JS)
            except Exception as exc:
                log.debug("imt_spaces evaluate failed err=%s", exc)
                payload = None
            if isinstance(payload, dict) and payload.get("ok"):
                plans = payload.get("plans") or []
                if isinstance(plans, list) and plans:
                    rows = parse_imt_spaces_plans(plans, winning)
        if not rows:
            fr = getattr(ctx, "fetch_result", None)
            raw = getattr(fr, "body", None) if fr is not None else None
            body_str = (
                raw.decode("utf-8", errors="replace") if isinstance(raw, bytes)
                else raw if isinstance(raw, str) else ""
            )
            if body_str:
                rows = parse_imt_spaces_html(body_str, winning)
        if not rows:
            result.confidence = 0.0
            result.errors.append(
                f"imt_spaces: parser produced zero rows from {len(plans)} articles"
            )
            return result
        from ma_poc.extraction.post_process import post_process

        pp = post_process(rows, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted > 0:
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.winning_url = winning
            # Plan-level extraction caps confidence below the unit-level
            # tier so the orchestrator prefers a real unit-tier adapter
            # when one is available.
            result.confidence = min(0.85, 0.65 + 0.02 * pp.n_admitted)
            return result
        result.confidence = 0.0
        result.errors.append(
            f"imt_spaces: {len(rows)} rows failed unit_validity post-process"
        )
        return result

    @staticmethod
    def _winning_url(page: Page | None, ctx: AdapterContext) -> str:
        if page is not None:
            try:
                u = getattr(page, "url", None)
                if u:
                    return u
            except Exception:
                pass
        fr = getattr(ctx, "fetch_result", None)
        if fr is not None:
            final = getattr(fr, "final_url", None)
            if final:
                return str(final)
        return getattr(ctx, "base_url", "") or ""

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
