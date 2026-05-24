"""RealPage CWS (Community Website Solution) — RPFP widget plan-level extractor.

Some RealPage-tagged sites use a CMS-style template called CWS (Community
Website Solution) that ships the "RPFP" (RealPage Floor Plans) widget
embedded inline on /Floor-Plans.aspx. The widget renders plan cards
client-side with all data in same-origin DOM — no XHR signature endpoint
to intercept.

Verified live 2026-05-21 on:
  - www.liveatpenthouse.com/Floor-Plans.aspx (CWS/2256871)
  - www.liveatshadowglen.com/Floor-Plans.aspx (CWS/2267476)

DOM contract:
  .rpfp-container.floorplans-widget-{N}
    .rpfp-filters ...
    .rpfp-body
      .rpfp-cards
        .rpfp-card.pricing-transparency-{boolean} × N (one per plan)
          .rpfp-card-inner ...
            .rpfp-info > .rpfp-info-top > .rpfp-details
              .rpfp-name      (plan name, e.g. "One Bedroom")
              .rent-container-details
              (card innerText format observed:
                "One Bedroom 1 Bed1 Bath 566 - 784 Sqft $2,100 - $2,120
                 Brochure Contact Us")

Plan-level only — RealPage CWS doesn't publish a per-unit roster
publicly; the "CHECK AVAILABILITY" button links back to the same
/Floor-Plans.aspx page (no drill). The "APPLY NOW" link goes to the
on-site.com leasing application form which isn't a public unit roster.

Distinct from:
  * RealPageOllAdapter (handles ``leasing.realpage.com/RP.Leasing...``
    OLL workflow XHR) — different RealPage product.
  * Other ``.aspx`` legacy themes (e.g. ``.floorplan-block`` + ``.par-units``,
    handled by RentCafeUnitRosterAdapter).
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

_RPFP_DOM_JS = r"""
async () => {
  const cont = document.querySelector('.rpfp-container');
  if (!cont) return {ok: false, reason: 'no .rpfp-container present'};
  const cards = Array.from(cont.querySelectorAll('.rpfp-card'));
  if (cards.length === 0) {
    return {ok: false, reason: '.rpfp-container present but no .rpfp-card children'};
  }
  const plans = cards.map((c) => {
    const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
    return {
      planName: T(c.querySelector('.rpfp-name')),
      cardText: T(c),
      classes: c.className || '',
    };
  });
  return {ok: true, plans: plans};
}
"""

# Cards' visible text typically looks like:
#   "One Bedroom 1 Bed1 Bath 566 - 784 Sqft $2,100 - $2,120 Brochure Contact Us"
# Regex anchors don't always have spaces between fields (e.g. "1 Bed1 Bath")
# so each pattern is tolerant of optional whitespace.
_BED_RE = re.compile(r"(\d+|studio)\s*Bed", re.IGNORECASE)
_BATH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Bath", re.IGNORECASE)
_SQFT_SINGLE_RE = re.compile(r"(\d[\d,]*)\s*sqft", re.IGNORECASE)
_SQFT_RANGE_RE = re.compile(r"(\d[\d,]*)\s*-\s*(\d[\d,]*)\s*sqft", re.IGNORECASE)
_PRICE_SINGLE_RE = re.compile(r"\$\s*([\d,]+)")
_PRICE_RANGE_RE = re.compile(r"\$\s*([\d,]+)\s*-\s*\$\s*([\d,]+)")


def _parse_card_text(text: str) -> dict:
    """Parse one .rpfp-card innerText into structured fields.

    Returns dict with keys: beds, baths, sqft_low, sqft_high, rent_low,
    rent_high. Missing values are None or empty string.
    """
    out: dict = {}
    bm = _BED_RE.search(text)
    if bm:
        v = bm.group(1)
        out["beds"] = 0 if v.lower() == "studio" else int(v)
    else:
        out["beds"] = None
    bath_m = _BATH_RE.search(text)
    out["baths"] = bath_m.group(1) if bath_m else ""
    range_m = _SQFT_RANGE_RE.search(text)
    if range_m:
        out["sqft_low"] = range_m.group(1).replace(",", "")
        out["sqft_high"] = range_m.group(2).replace(",", "")
    else:
        sm = _SQFT_SINGLE_RE.search(text)
        if sm:
            out["sqft_low"] = sm.group(1).replace(",", "")
            out["sqft_high"] = out["sqft_low"]
        else:
            out["sqft_low"] = ""
            out["sqft_high"] = ""
    price_range = _PRICE_RANGE_RE.search(text)
    if price_range:
        out["rent_low"] = money_to_int(price_range.group(1))
        out["rent_high"] = money_to_int(price_range.group(2))
    else:
        single = _PRICE_SINGLE_RE.search(text)
        if single:
            out["rent_low"] = money_to_int(single.group(1))
            out["rent_high"] = out["rent_low"]
        else:
            out["rent_low"] = None
            out["rent_high"] = None
    return out


def parse_realpage_cws_json(data: dict, url: str) -> list[dict]:
    """Parse RealPage CWS GetFloorPlans XHR JSON into unit dicts.

    The /CmsSiteManager/callback.aspx?act=Proxy/GetFloorPlans endpoint
    returns ``{floorplans: [{name, bedRooms, bathRooms,
    minimumSquareFeet, maximumSquareFeet, minimumMarketRent,
    maximumMarketRent, numberOfUnitsDisplay, ...}]}``. Each floorplan
    is plan-level (multi-unit). 2026-05-24 verified live on
    thebeachapts.com (7 plans), liveatpenthouse.com (5 plans),
    liveatshadowglen.com (1 plan).
    """
    if not isinstance(data, dict):
        return []
    fps = data.get("floorplans") or []
    if not isinstance(fps, list):
        return []
    out: list[dict] = []
    for fp in fps:
        if not isinstance(fp, dict):
            continue
        name = str(fp.get("name") or "").strip()
        # bedRooms: "S" for studio, or numeric string ("1", "2"...).
        bed_str = str(fp.get("bedRooms", "") or "").strip()
        if bed_str.upper() == "S":
            beds: int | None = 0
        else:
            try:
                beds = int(bed_str) if bed_str else None
            except (TypeError, ValueError):
                beds = None
        baths_str = str(fp.get("bathRooms", "") or "").strip()
        # Sqft — pick max-of-range for plan summary; downstream
        # unit-level work would split into ranges.
        sqft_max = fp.get("maximumSquareFeet")
        sqft_min = fp.get("minimumSquareFeet")
        sqft_val = sqft_max if sqft_max not in (None, "", "0", 0) else sqft_min
        sqft = str(sqft_val or "").strip() if sqft_val not in (None, "") else ""
        # Rent — minimumMarketRent / maximumMarketRent as floats.
        def _to_int(v: object) -> int | None:
            if v in (None, ""):
                return None
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return None
        rent_low = _to_int(fp.get("minimumMarketRent"))
        rent_high = _to_int(fp.get("maximumMarketRent"))
        if rent_low is None and rent_high is None:
            # Skip plans that don't publish rent (the rpfp widget
            # filters them out too — "Contact Us" cards).
            continue
        if not name and rent_low is None:
            continue
        out.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name) if beds is not None else "",
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=baths_str,
                sqft=sqft,
                unit_number="",  # plan-level only
                rent_low=rent_low,
                rent_high=rent_high if rent_high is not None else rent_low,
                rent_range=format_rent_range(rent_low, rent_high if rent_high is not None else rent_low),
                availability_status="AVAILABLE",
                # numberOfUnitsDisplay tells us how many units the
                # property advertises for this plan — useful when the
                # canonical plan-level guard expects available_units.
                available_units=str(fp.get("numberOfUnitsDisplay") or "").strip(),
                source_api_url=url,
                extraction_tier="TIER_1_API_REALPAGE_CWS",
            )
        )
    return out


async def _probe_realpage_cws_xhr(origin: str) -> list[dict]:
    """Same-origin probe of /CmsSiteManager/callback.aspx?act=Proxy/GetFloorPlans.

    Returns parsed plan-level unit dicts on success, ``[]`` on any
    failure (404, timeout, malformed JSON, CF block). Best-effort —
    never raises. Uses ``probe_get`` (curl_cffi + chrome120) so CF-
    fronted properties get the same TLS impersonation as other adapter
    probes.
    """
    if not origin:
        return []
    try:
        from ma_poc.pms.adapters._probe import probe_get
    except ImportError:
        return []
    url = origin.rstrip("/") + "/CmsSiteManager/callback.aspx?act=Proxy/GetFloorPlans"
    try:
        r = probe_get(url, timeout=20)
    except Exception:
        return []
    if getattr(r, "status_code", 0) != 200:
        return []
    body = getattr(r, "text", "") or ""
    if not body:
        return []
    import json as _json
    try:
        data = _json.loads(body)
    except _json.JSONDecodeError:
        return []
    return parse_realpage_cws_json(data, url)


def parse_realpage_cws_html(html: str, url: str) -> list[dict]:
    """Body-fallback parser — when no live Playwright page is available
    (Jugnu pipeline dispatches with ``page=None`` and the L1 fetcher's
    post-render snapshot on ``ctx.fetch_result.body``), find the
    ``.rpfp-card`` divs in the snapshot HTML and feed each one's text
    through the same ``_parse_card_text`` regex pipeline that the
    ``page.evaluate`` path uses.

    Assumes the Jugnu L1 fetcher captures the post-render DOM (waits
    for ``networkidle``). When the rpfp widget is purely JS-rendered
    and the snapshot was taken too early, this returns ``[]``.
    """
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
    cards = soup.select(".rpfp-container .rpfp-card") or soup.select(".rpfp-card")
    if not cards:
        return []
    plans: list[dict] = []
    for card in cards:
        name_el = card.select_one(".rpfp-name")
        plans.append({
            "planName": (name_el.get_text(" ", strip=True) if name_el else "").strip(),
            "cardText": card.get_text(" ", strip=True),
            "classes": " ".join(card.get("class") or []),
        })
    return parse_realpage_cws_plans(plans, url)


def parse_realpage_cws_plans(plans: list[dict], url: str) -> list[dict]:
    out: list[dict] = []
    for p in plans:
        if not isinstance(p, dict):
            continue
        name = str(p.get("planName") or "").strip()
        text = str(p.get("cardText") or "")
        parsed = _parse_card_text(text)
        beds = parsed.get("beds")
        baths = parsed.get("baths", "")
        # Prefer the high sqft when there's a range (more representative of
        # the plan's upper-bound layout); downstream unit-level work would
        # split into ranges, but for plan-only we keep the high value.
        sqft = parsed.get("sqft_high", "") or parsed.get("sqft_low", "")
        rent_low = parsed.get("rent_low")
        rent_high = parsed.get("rent_high")
        if not name and rent_low is None:
            continue
        out.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=baths,
                sqft=sqft,
                unit_number="",  # plan-level only
                rent_low=rent_low,
                rent_high=rent_high,
                rent_range=format_rent_range(rent_low, rent_high),
                availability_status="AVAILABLE" if rent_low is not None else "UNAVAILABLE",
                source_api_url=url,
                extraction_tier="TIER_1_DOM_REALPAGE_CWS",
            )
        )
    return out


class RealPageCwsAdapter:
    """RealPage CWS RPFP widget — plan-level extraction from
    ``.rpfp-container > .rpfp-cards > .rpfp-card``."""

    pms_name: str = "realpage_cws"
    _fingerprints: list[str] = [
        "cs-cdn.realpage.com/cws",
        "rpfp-container",
        "rpfp-card",
        "floorplans-widget",
    ]

    async def try_dom(self, page: Any, html: str, ctx: AdapterContext) -> Any:
        """2026-05-24 Phase 1 cascade hook — deterministic DOM extraction
        for RealPage CWS ``.rpfp-card`` widget. Wraps
        ``parse_realpage_cws_html`` and routes units through dq_guards.
        """
        from ma_poc.pms.adapters.base import AdapterDomResult

        if not html or "rpfp-card" not in html:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_REALPAGE_CWS",
                reason="no_rpfp_card_marker",
            )
        try:
            url = getattr(ctx, "base_url", "") or ""
            raw_units = parse_realpage_cws_html(html, url)
        except Exception as e:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_REALPAGE_CWS",
                reason=f"parse_exception:{type(e).__name__}",
            )
        if not raw_units:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_REALPAGE_CWS",
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
                tier="TIER_3_DOM_REALPAGE_CWS",
                reason="dq_guards_rejected_all",
            )
        return AdapterDomResult(
            units=guarded,
            plan_summaries=[],
            tier_used="TIER_3_DOM_REALPAGE_CWS",
            selector_signature="rpfp-container>rpfp-card",
            confidence=0.85 if len(guarded) >= 3 else 0.7,
            debug={"raw_count": len(raw_units), "guarded_count": len(guarded)},
        )

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used="TIER_1_API_REALPAGE_CWS")

        # 2026-05-24: three-tier fallback cascade for RealPage CWS:
        #   1. XHR probe to /CmsSiteManager/callback.aspx?act=Proxy/GetFloorPlans
        #      (most reliable — same-origin JSON with plan-level data
        #      including beds/baths/sqft/rent_range/numberOfUnitsDisplay).
        #      Verified live 2026-05-24 on thebeachapts (7 plans),
        #      liveatpenthouse (5), liveatshadowglen (1).
        #   2. page.evaluate(_RPFP_DOM_JS) when a live page is available.
        #   3. BeautifulSoup body-fallback for the L1-captured snapshot
        #      (only works if patchright waited long enough for the rpfp
        #      widget to render — observed false on production canary).
        winning = self._winning_url(page, ctx)
        rows: list[dict] = []

        # Tier 1: XHR probe (highest reliability — no JS render needed).
        origin = self._origin(page, ctx)
        if origin:
            try:
                rows = await _probe_realpage_cws_xhr(origin)
            except Exception as exc:
                log.debug("realpage_cws XHR probe failed err=%s", exc)

        # Tier 2: page.evaluate fallback when XHR returned nothing.
        if not rows:
            evaluate = getattr(page, "evaluate", None) if page is not None else None
            if callable(evaluate):
                try:
                    payload = await evaluate(_RPFP_DOM_JS)
                except Exception as exc:
                    log.debug("realpage_cws evaluate failed err=%s", exc)
                    payload = None
                if isinstance(payload, dict) and payload.get("ok"):
                    plans = payload.get("plans") or []
                    if isinstance(plans, list) and plans:
                        rows = parse_realpage_cws_plans(plans, winning)

        # Tier 3: BeautifulSoup body-fallback (last resort).
        if not rows:
            fr = getattr(ctx, "fetch_result", None)
            raw = getattr(fr, "body", None) if fr is not None else None
            body_str = ""
            if isinstance(raw, bytes):
                body_str = raw.decode("utf-8", errors="replace")
            elif isinstance(raw, str):
                body_str = raw
            if body_str:
                rows = parse_realpage_cws_html(body_str, winning)

        # Promote the tier label when XHR won so downstream reports
        # can distinguish the high-confidence API tier from the
        # DOM-fallback paths.
        if rows and rows[0].get("extraction_tier") == "TIER_1_API_REALPAGE_CWS":
            result.tier_used = "TIER_1_API_REALPAGE_CWS"
        elif rows:
            result.tier_used = "TIER_1_DOM_REALPAGE_CWS"
        if not rows:
            result.confidence = 0.0
            result.errors.append(
                f"realpage_cws: parser produced zero rows from {len(plans)} cards"
            )
            return result
        from ma_poc.extraction.post_process import post_process

        pp = post_process(rows, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted > 0:
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.winning_url = winning
            # Plan-level confidence cap.
            result.confidence = min(0.85, 0.65 + 0.04 * pp.n_admitted)
            return result
        result.confidence = 0.0
        result.errors.append(
            f"realpage_cws: {len(rows)} rows failed unit_validity post-process"
        )
        return result

    @staticmethod
    def _origin(page: Page | None, ctx: AdapterContext) -> str:
        """Return ``scheme://host`` derived from the live page or
        ``ctx.fetch_result.final_url`` / ``ctx.base_url``. Used as the
        base for the GetFloorPlans XHR probe."""
        from urllib.parse import urlparse, urlunparse
        candidate = ""
        if page is not None:
            try:
                candidate = getattr(page, "url", "") or ""
            except Exception:
                candidate = ""
        if not candidate:
            fr = getattr(ctx, "fetch_result", None)
            if fr is not None:
                final = getattr(fr, "final_url", None)
                if final:
                    candidate = str(final)
        if not candidate:
            candidate = getattr(ctx, "base_url", "") or ""
        try:
            p = urlparse(candidate)
        except Exception:
            return ""
        if not p.scheme or not p.netloc:
            return ""
        return urlunparse((p.scheme, p.netloc, "", "", "", ""))

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
