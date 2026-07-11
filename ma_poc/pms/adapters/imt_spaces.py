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


_ARTICLE_TAG_RE = re.compile(r"<article\b[^>]*spaces-plan[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([a-zA-Z][a-zA-Z0-9\-]*)\s*=\s*"([^"]*)"')


def _camel_dataset_key(attr: str) -> str:
    """``data-spaces-sort-plan-name`` -> ``spacesSortPlanName`` (HTML
    dataset camel-casing, so the static path feeds the same parser as
    the live element.dataset path)."""
    parts = attr[len("data-"):].split("-")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def parse_imt_spaces_articles_from_html(html: str) -> list[dict]:
    """Static-HTML twin of the DOM JS: pull ``article.spaces-plan``
    opening tags out of raw HTML and emit the same plan-dict shape
    (``{classes, titleAttr, data:{camelCased}}``). IMT server-renders
    the data-* attributes, so no JS execution is needed."""
    if not html:
        return []
    plans: list[dict] = []
    for m in _ARTICLE_TAG_RE.finditer(html):
        tag = m.group(0)
        attrs = dict(_ATTR_RE.findall(tag))
        classes = attrs.get("class", "")
        if "spaces-plan" not in classes:
            continue
        data = {
            _camel_dataset_key(k): v
            for k, v in attrs.items()
            if k.lower().startswith("data-")
        }
        plans.append({
            "classes": classes,
            "titleAttr": attrs.get("title", ""),
            "data": data,
        })
    return plans


def _static_subpage_candidates(entry_url: str, entry_html: str) -> list[str]:
    """Candidate apartments-page URLs for the static fallback.

    2026-07-11 audit: IMT vanity landings (``/imtgallery421/``) don't
    embed plans — the canonical page is ``/properties/{slug}/apartments/``
    and the landing HTML references its own canonical slug. Emit those
    first, then the naive ``{entry}/apartments/`` shape as a last resort.
    """
    from urllib.parse import urlparse

    out: list[str] = []
    try:
        pu = urlparse(entry_url)
        origin = f"{pu.scheme}://{pu.netloc}"
    except Exception:
        return out
    seen: set[str] = set()
    for slug in re.findall(r"/properties/([a-z0-9\-]+)/", entry_html or "")[:6]:
        u = f"{origin}/properties/{slug}/apartments/"
        if u not in seen:
            seen.add(u)
            out.append(u)
    if entry_url:
        naive = entry_url.rstrip("/") + "/apartments/"
        if naive not in seen:
            out.append(naive)
    return out[:4]


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

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used="TIER_1_DOM_IMT_SPACES")
        plans: list[dict] = []
        winning = self._winning_url(page, ctx)

        # Live-DOM path (preferred when the orchestrator hands us a real
        # Playwright page whose DOM already carries the plan articles).
        evaluate = getattr(page, "evaluate", None)
        if callable(evaluate):
            try:
                payload = await evaluate(_IMT_SPACES_DOM_JS)
            except Exception as exc:
                log.debug("imt_spaces evaluate failed err=%s", exc)
                payload = None
            if isinstance(payload, dict) and payload.get("ok"):
                got = payload.get("plans") or []
                if isinstance(got, list):
                    plans = got

        # Static subpage fallback (2026-07-11 audit): vanity landing pages
        # (/imtgallery421/) don't carry the articles — they live on
        # /properties/{slug}/apartments/. Derive the canonical slug from
        # the entry HTML, probe the apartments page via curl, and parse
        # the server-rendered data-* attributes statically.
        if not plans:
            entry_html = ""
            fr = getattr(ctx, "fetch_result", None)
            raw = getattr(fr, "body", None) if fr is not None else None
            if isinstance(raw, bytes):
                entry_html = raw.decode("utf-8", errors="replace")
            elif isinstance(raw, str):
                entry_html = raw
            # Entry page itself may already carry the articles statically
            # (canonical /apartments/ URLs do).
            plans = parse_imt_spaces_articles_from_html(entry_html)
            if not plans and entry_html:
                try:
                    from ma_poc.pms.adapters._probe import probe_get

                    base_url = str(getattr(ctx, "base_url", "") or "")
                    for cand in _static_subpage_candidates(base_url, entry_html):
                        try:
                            resp = probe_get(cand, timeout=15)
                            body = getattr(resp, "text", "") or ""
                        except Exception:
                            continue
                        plans = parse_imt_spaces_articles_from_html(body)
                        if plans:
                            winning = cand
                            result.errors.append(
                                f"imt_spaces: static-subpage fallback via {cand}"
                            )
                            break
                except Exception as exc:  # noqa: BLE001 — fallback is best-effort
                    result.errors.append(f"imt_spaces: subpage probe failed: {exc}")

        if not plans:
            result.confidence = 0.0
            result.errors.append("imt_spaces: zero spaces-plan articles")
            return result
        rows = parse_imt_spaces_plans(plans, winning)
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
    def _winning_url(page: Page, ctx: AdapterContext) -> str:
        try:
            return page.url or getattr(ctx, "base_url", "") or ""
        except Exception:
            return getattr(ctx, "base_url", "") or ""

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
