"""Camden Property Trust (REIT) adapter — camdenliving.com.

Camden runs a proprietary Next.js site (no PMS host fingerprint) that SSRs the
available-unit roster into the page's ``__NEXT_DATA__`` island at
``props.pageProps.suggestedFloorPlans`` — one object per floorplan, each carrying
a representative available unit::

    {"name":"1.1D","unitNumber":"9040","monthlyRent":2199,"squareFeet":799,
     "bedrooms":"1","bathrooms":"1","available":true,
     "moveInDate":"2026-09-25T00:00:00.000Z","availableUnitIds":["9020","9040"],
     "realPageUnitId":248,"realPageFloorPlanId":5}

Present on BOTH the landing page and ``/availability`` — a static parse, no
render, no cross-host API. The roster-confirmation sweep flagged Camden as a
NEW-adapter gap (``engrainSightMapId=null``, no existing adapter). Live-verified
across 6 props (fallsgrove/south-charlotte/gallery/southline/buckhead/noma);
generalizes portfolio-wide (~170 properties). Fully-leased props carry no
``suggestedFloorPlans`` and yield nothing (fall through).

Emits one row per floorplan = the representative available unit with its real
unitNumber + monthlyRent + squareFeet (accurate per-unit rent). Flag-gated via
``ENABLE_CAMDEN_ADAPTER`` (default off).
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)


def _int_or_none(raw: Any) -> int | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    v = money_to_int(str(raw))
    return v if v else None


def parse_camden_units(body: str, url: str) -> list[dict[str, Any]]:
    """Parse ``suggestedFloorPlans`` from a Camden page's ``__NEXT_DATA__``.

    Returns ``[]`` when the island / array is absent or empty. Never raises.
    """
    if not body or "__NEXT_DATA__" not in body:
        return []
    m = _NEXT_DATA_RE.search(body)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, TypeError):
        return []
    try:
        plans = data["props"]["pageProps"]["suggestedFloorPlans"]
    except (KeyError, TypeError):
        return []
    if not isinstance(plans, list):
        return []

    out: list[dict[str, Any]] = []
    for p in plans:
        if not isinstance(p, dict):
            continue
        unit_no = str(p.get("unitNumber") or p.get("unitName") or "").strip()
        if not unit_no:
            continue
        rent = _int_or_none(p.get("monthlyRent"))
        beds = _int_or_none(p.get("bedrooms"))
        plan_name = str(p.get("name") or "").strip()
        move_in = str(p.get("moveInDate") or "")
        avail_date = move_in[:10] if re.match(r"\d{4}-\d{2}-\d{2}", move_in) else ""

        source_ids: dict[str, Any] = {}
        if p.get("realPageUnitId") is not None:
            source_ids["realpage_unit_id"] = p.get("realPageUnitId")
        if p.get("realPageFloorPlanId") is not None:
            source_ids["realpage_floorplan_id"] = p.get("realPageFloorPlanId")

        out.append(
            make_unit_dict(
                floor_plan_name=plan_name,
                bed_label=bed_label_from(beds, plan_name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(p.get("bathrooms") or "").strip(),
                sqft=str(_int_or_none(p.get("squareFeet")) or "") or "",
                unit_number=unit_no,
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE" if p.get("available") else "UNAVAILABLE",
                availability_date=avail_date,
                source_api_url=url,
                extraction_tier="TIER_1_DOM_CAMDEN",
                source_ids=source_ids or None,
            )
        )
    return out


class CamdenAdapter:
    """Camden REIT adapter — parses suggestedFloorPlans from __NEXT_DATA__ in the
    already-fetched page body (no render, no extra fetch)."""

    pms_name: str = "camden"
    _fingerprints: list[str] = ["camdenliving.com", "suggestedfloorplans"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Parse suggestedFloorPlans from ctx.fetch_result.body. Never raises."""
        result = AdapterResult(tier_used="TIER_1_DOM_CAMDEN")

        fr = getattr(ctx, "fetch_result", None)
        body = getattr(fr, "body", None) if fr is not None else None
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        if not isinstance(body, str) or not body:
            content = getattr(page, "content", None)
            if callable(content):
                try:
                    body = await content()
                except Exception:  # noqa: BLE001
                    body = ""
        if not isinstance(body, str) or not body:
            result.confidence = 0.0
            result.errors.append("camden: no page body available")
            return result

        winning = getattr(ctx, "base_url", "") or ""
        try:
            winning = page.url or winning
        except Exception:  # noqa: BLE001
            pass
        rows = parse_camden_units(body, winning)
        if not rows:
            result.confidence = 0.0
            result.errors.append("camden: no suggestedFloorPlans / zero units")
            return result

        from ma_poc.extraction.post_process import post_process

        pp = post_process(rows, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted <= 0:
            result.confidence = 0.0
            result.errors.append(
                f"camden: {len(rows)} rows failed unit_validity post-process"
            )
            return result
        result.units = pp.admitted
        result.plan_summaries = pp.plan_summaries
        result.winning_url = winning
        result.confidence = min(0.95, 0.7 + 0.05 * pp.n_admitted)
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
