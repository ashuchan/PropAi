"""Venterra Living in-house (eOnlineLease) adapter.

Venterra runs its own leasing platform (``online.venterraliving.com/eOnlineLease``)
and SSRs the full unit roster into its marketing pages as a static JS island::

    var vt_units = [{"unit_code":"TX4FV-01-0116","unit_name":"0116",
      "unit_parent_floorplan_code":"660-A","unit_bedrooms":"1","unit_bathrooms":"1",
      "unit_sqft":"684","unit_rent_min":"1159","unit_rent_max":"1222",
      "unit_available":"1","unit_available_on":"2026-09-29",
      "unit_specials_message":"$500 gift card. Limited time only", ...}, ...];

It is proper JSON (quoted keys) — parseable straight from the page body with NO
render and NO cross-host API. The 2026-07-18 roster-confirmation sweep mis-routed
these props to a co-resident SightMap embed + ``needs_render``; the island is
right there in the SSR HTML. Live-verified across forest-view (20 units),
canton-mill (19), thomasglen (11) — a genuine Tier-1 unit-level surface.

Flag-gated via ``ENABLE_VENTERRA_ADAPTER`` (default off): the detector marker can
win co-residence against SightMap, so a canary measures the recovery.
"""

from __future__ import annotations

import json
import logging
import re
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

log = logging.getLogger(__name__)

_FLOOR_NUM_RE = re.compile(r"^\s*(\d+)")


def _extract_js_array(text: str, var_name: str) -> str:
    """Return the balanced ``[...]`` array literal assigned to ``var_name``.

    Scans ``<var_name> = [`` and brace-matches ``[``/``]`` while respecting
    double-quoted strings (and their escapes), so a ``]`` inside a value or a
    nested array does not terminate early. Returns ``""`` when absent/unbalanced.
    """
    m = re.search(re.escape(var_name) + r"\s*=\s*\[", text)
    if not m:
        return ""
    start = m.end() - 1  # index of the opening '['
    depth = 0
    in_str = False
    esc = False
    for k in range(start, len(text)):
        c = text[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return text[start : k + 1]
    return ""


def _int_or_none(raw: Any) -> int | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    v = money_to_int(str(raw))
    return v if v else None


def parse_venterra_units(body: str, url: str) -> list[dict[str, Any]]:
    """Parse the ``vt_units`` island in a Venterra page body into unit dicts.

    Returns ``[]`` when the island is absent or empty. Never raises.
    """
    if not body or "vt_units" not in body:
        return []
    arr_str = _extract_js_array(body, "vt_units")
    if not arr_str:
        return []
    try:
        units = json.loads(arr_str)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(units, list):
        return []

    out: list[dict[str, Any]] = []
    for u in units:
        if not isinstance(u, dict):
            continue
        unit_no = str(u.get("unit_name") or "").strip()
        if not unit_no:
            continue
        rent_low = _int_or_none(u.get("unit_rent_min")) or _int_or_none(u.get("unit_rent_market"))
        rent_high = _int_or_none(u.get("unit_rent_max")) or rent_low
        beds_raw = u.get("unit_bedrooms")
        beds = _int_or_none(beds_raw)
        plan = str(u.get("unit_parent_floorplan_code") or "").strip()
        floor_m = _FLOOR_NUM_RE.match(str(u.get("unit_floor_level") or ""))
        available = str(u.get("unit_available") or "").strip() == "1"
        special = str(u.get("unit_specials_message") or "").strip()

        source_ids: dict[str, Any] = {}
        if u.get("unit_code"):
            source_ids["venterra_unit_code"] = u.get("unit_code")
        if plan:
            source_ids["floorplan_code"] = plan

        out.append(
            make_unit_dict(
                floor_plan_name=plan,
                bed_label=bed_label_from(beds, plan),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(u.get("unit_bathrooms") or "").strip(),
                sqft=str(_int_or_none(u.get("unit_sqft")) or "") or "",
                unit_number=unit_no,
                floor=floor_m.group(1) if floor_m else "",
                rent_low=rent_low,
                rent_high=rent_high,
                rent_range=format_rent_range(rent_low, rent_high),
                concession_text=special or None,
                availability_status="AVAILABLE" if available else "UNAVAILABLE",
                availability_date=str(u.get("unit_available_on") or "").strip(),
                source_api_url=url,
                extraction_tier="TIER_1_DOM_VENTERRA",
                source_ids=source_ids or None,
            )
        )
    return out


class VenterraAdapter:
    """Venterra Living in-house adapter — parses the static ``vt_units`` island
    from the already-fetched marketing page body (no render, no extra fetch)."""

    pms_name: str = "venterra"
    _fingerprints: list[str] = [
        "vt_units",
        "online.venterraliving.com/eonlinelease",
        "venterraliving.com",
    ]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Parse the vt_units island from ctx.fetch_result.body. Never raises."""
        result = AdapterResult(tier_used="TIER_1_DOM_VENTERRA")

        fr = getattr(ctx, "fetch_result", None)
        body = getattr(fr, "body", None) if fr is not None else None
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        if not isinstance(body, str) or not body:
            # Fall back to the live page content if the fetch body isn't threaded.
            content = getattr(page, "content", None)
            if callable(content):
                try:
                    body = await content()
                except Exception:  # noqa: BLE001
                    body = ""
        if not isinstance(body, str) or not body:
            result.confidence = 0.0
            result.errors.append("venterra: no page body available")
            return result

        winning = getattr(ctx, "base_url", "") or ""
        try:
            winning = page.url or winning
        except Exception:  # noqa: BLE001
            pass
        rows = parse_venterra_units(body, winning)
        if not rows:
            result.confidence = 0.0
            result.errors.append("venterra: no vt_units island / zero units")
            return result

        from ma_poc.extraction.post_process import post_process

        pp = post_process(rows, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted <= 0:
            result.confidence = 0.0
            result.errors.append(
                f"venterra: {len(rows)} rows failed unit_validity post-process"
            )
            return result
        result.units = pp.admitted
        result.plan_summaries = pp.plan_summaries
        result.winning_url = winning
        result.confidence = min(0.95, 0.7 + 0.05 * pp.n_admitted)
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
