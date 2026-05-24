"""
Cortland adapter.

Research log
------------
Single operator, single host (cortland.com). Server-rendered marketing
site. The per-property ``/available-apartments/`` page embeds a clean
``preload = {"floorplans": {...}}`` JSON blob (HTML-entity-encoded in
the markup). Discovered via user HAR + probe (2026-05-18).

Structure (verified, cortland-brier-creek, 34 priced units):
  preload.floorplans[<fpId>] = {
    id, title, bedroom, bathroom, square_feet, floors[], min_rent,
    max_rent, apartments[<unitId>], availability[<date>],
    availprice: { "<unitId>": {apartment_number, date(epoch ms), price} }
  }

``availprice`` is the genuine unit-level map: real apartment_number +
per-unit price + availability epoch. No auth, no API call — the data is
in the page HTML. Strictly richer than the prior partial TIER_3_DOM
path (which missed ~40% of units).

Recipe (deterministic, public):
  1. fetch {property_base}/available-apartments/
  2. brace-match the floorplans object out of ``preload = {...}``
  3. flatten floorplans[].availprice -> unit dicts -> post_process
"""

from __future__ import annotations

import html as _html
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

OLL_TIER = "TIER_1_API_CORTLAND"


def _epoch_to_date(v: Any) -> str:
    """Cortland ``date`` is epoch milliseconds -> ``YYYY-MM-DD``."""
    try:
        ms = int(v)
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def _extract_floorplans(html: str) -> dict[str, Any]:
    """Brace-match the ``floorplans`` object out of the embedded preload.

    The blob is HTML-entity-encoded in the markup, so unescape first.
    """
    d = _html.unescape(html or "")
    i = d.find('"floorplans"')
    if i < 0:
        return {}
    j = d.find("{", i)
    if j < 0:
        return {}
    depth = 0
    end = -1
    for k in range(j, len(d)):
        c = d[k]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = k + 1
                break
    if end < 0:
        return {}
    try:
        obj = json.loads(d[j:end])
    except (json.JSONDecodeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def parse_cortland_units(floorplans: dict[str, Any], url: str) -> list[dict[str, str]]:
    """Flatten ``floorplans[].availprice`` into standard unit dicts."""
    units: list[dict[str, str]] = []
    for fp in floorplans.values():
        if not isinstance(fp, dict):
            continue
        fp_name = str(fp.get("title") or "").strip()
        beds_raw = fp.get("bedroom")
        baths_raw = fp.get("bathroom")
        try:
            beds = int(float(beds_raw)) if beds_raw not in (None, "") else None
        except (TypeError, ValueError):
            beds = None
        try:
            baths = float(baths_raw) if baths_raw not in (None, "") else None
        except (TypeError, ValueError):
            baths = None
        sqft_raw = fp.get("square_feet")
        sqft = str(sqft_raw) if sqft_raw not in (None, "", 0) else ""

        # 2026-05-19 capture-first: Cortland preload carries a specials/
        # concession flag/text at floorplan level (probe saw
        # `specials_flag` + concession phrases). Alias-tolerant; raw
        # passthrough; empty when no active special (correct, not a bug).
        _conc = ""
        for _ck in ("specials", "special", "concession", "concessions",
                    "specials_description", "specials_text", "promotion",
                    "offer", "incentive"):
            _cv = fp.get(_ck)
            if isinstance(_cv, str) and _cv.strip():
                _conc = _cv.strip()
                break

        availprice = fp.get("availprice")
        if not isinstance(availprice, dict):
            continue
        for info in availprice.values():
            if not isinstance(info, dict):
                continue
            unit_no = str(info.get("apartment_number") or "").strip()
            rent = money_to_int(str(info.get("price") or "")) or None
            avail_date = _epoch_to_date(info.get("date"))
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
                    unit_number=unit_no,
                    rent_range=format_rent_range(rent, rent),
                    rent_low=rent,
                    rent_high=rent,
                    availability_status="AVAILABLE",
                    availability_date=avail_date,
                    concession=_conc,
                    source_api_url=url,
                    extraction_tier=OLL_TIER,
                )
            )
    return units


async def _fetch_available_html(page: Page | None, ctx: AdapterContext) -> tuple[str, str]:
    """Return (html, url) for the page that carries the preload blob.

    Prefer ``{base}/available-apartments/`` (richest — has availprice).
    Fall back to the raw fetch body / current page if it already has it.
    """
    base = (getattr(ctx, "base_url", "") or "").rstrip("/")
    target = f"{base}/available-apartments/" if base else ""
    if target:
        try:
            from ma_poc.pms.adapters._probe import probe_get

            # ctx + stage thread through the proxy gate. Cortland's
            # /available-apartments/ is same-origin with ctx.base_url,
            # so the gate's Layer 1 (same_origin) declines proxy and
            # this stays on the cheap direct path with clearance cookies.
            r = probe_get(target, ctx=ctx, stage="cortland_api", timeout=20)
            if getattr(r, "status_code", 0) == 200 and r.text and '"floorplans"' in r.text:
                return str(r.text), target
        except Exception:
            pass
    fr = getattr(ctx, "fetch_result", None)
    body = getattr(fr, "body", None)
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:
            body = None
    if isinstance(body, str) and '"floorplans"' in body:
        return body, getattr(ctx, "base_url", "") or ""
    if page is not None:
        try:
            h = await page.content()
            if h and '"floorplans"' in h:
                return h, getattr(ctx, "base_url", "") or ""
        except Exception:
            pass
    return "", target or (getattr(ctx, "base_url", "") or "")


class CortlandAdapter:
    """Cortland PMS adapter (embedded preload JSON)."""

    pms_name: str = "cortland"
    _fingerprints: list[str] = ["cortland.com", "available-apartments"]

    async def try_dom(self, page: Any, html: str, ctx: AdapterContext) -> Any:
        """2026-05-24 Phase 1 cascade hook — DOM fallback when the main
        ``extract`` path missed.

        Cortland sites embed unit data inline as ``preload = {...}`` JSON
        in the HTML (HTML-entity-encoded, the existing
        ``_extract_floorplans`` brace-matches it). This is a true
        DOM-fallback: the data IS in the captured HTML body, even when
        the live-page Playwright path failed (page.evaluate timeout, JS
        error, etc.). We wrap the existing parser against the captured
        body and route units through dq_guards.

        Live-verified: PIDs 13898 + 78693 fixtures (cortland.com/
        cortland-peachtree-corners + cortland-west-houston, 2026-05-24)
        both have ``preload = {`` blob in 600KB+ captured HTML.
        """
        from ma_poc.pms.adapters.base import AdapterDomResult

        if not html or "preload" not in html or "availprice" not in html:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_CORTLAND_PRELOAD",
                reason="no_preload_availprice_markers",
            )
        try:
            url = getattr(ctx, "base_url", "") or ""
            floorplans = _extract_floorplans(html)
            raw_units = parse_cortland_units(floorplans, url) if floorplans else []
        except Exception as e:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_CORTLAND_PRELOAD",
                reason=f"parse_exception:{type(e).__name__}",
            )
        if not raw_units:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_CORTLAND_PRELOAD",
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
                tier="TIER_3_DOM_CORTLAND_PRELOAD",
                reason="dq_guards_rejected_all",
            )
        return AdapterDomResult(
            units=guarded,
            plan_summaries=[],
            tier_used="TIER_3_DOM_CORTLAND_PRELOAD",
            selector_signature="preload-floorplans-availprice",
            confidence=0.9 if len(guarded) >= 3 else 0.75,
            debug={"raw_count": len(raw_units), "guarded_count": len(guarded)},
        )

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=OLL_TIER)

        html, url = await _fetch_available_html(page, ctx)
        floorplans = _extract_floorplans(html)
        all_units = parse_cortland_units(floorplans, url) if floorplans else []

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
                result.confidence = 0.0
                result.errors.append(
                    f"CORTLAND_VALIDITY_REJECTED: {_pp_parsed} parsed rows "
                    f"failed unit_validity"
                )
        else:
            result.confidence = 0.0
            result.errors.append(
                "No Cortland unit data (preload floorplans/availprice not found)"
            )

        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
