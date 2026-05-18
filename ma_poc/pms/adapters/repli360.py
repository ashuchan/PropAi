"""Repli360 (rrac) PMS adapter — UNIT-LEVEL.

Research log (2026-05-17 Chrome-MCP + curl verification)
--------------------------------------------------------
The "rrac" / caf_v2 popup family (royce-like cluster, 158 properties
across the 5K, ~0 real units in production today — prod falls to
TIER_4_LLM floorplan-level) is a frontend over the Repli360 backend
(repli360 in turn fronts MRI ProspectConnect).

Mechanism (verified on royceattrumbull.com, site_id 1619):
  - The marketing site renders (JS-injected) per-floorplan "View
    Details" anchors whose onclick is literally:
        getUnitListByFloor(this,'A1AL' , 2 , 1619,``);
    i.e. getUnitListByFloor(this, <floorPlanID>, <template_type>,
    <site_id>, ...). site_id is constant per property; floorPlanID
    varies per plan. These attrs are absent from a static curl — they
    require the page to be rendered (the pipeline already renders
    JS-PMS sites).
  - POST https://app.repli360.com/admin/getUnitListByFloor (NO auth,
    NO bot wall — plain server-side POST works with Referer/Origin set
    to the property domain). Confirmed param set:
        floorPlanID, moveinDate ("%-d %b %Y" e.g. "17 May 2026"),
        site_id, template_type=2, mode=apt, type=2d,
        currentanuualterm="", AcademicTerm="", RentalLevel="",
        special=no, zpopUp=""
    (an empty moveinDate / mode returns the empty state — these
    values matter.)
  - Response JSON: {selected_units:[unitnum,...], str:<big HTML>}.
    The unit rows live in ``str`` as ``<tr class="unitlisting ...">``
    with ``data-available_date`` (ISO), ``<b class="unitNumber">``,
    a building ``<td>``, a deposit ``<td>``, and
    ``<span class="unit_price_value">$2,335</span>``.

Verified: royce fp A1AL → 7 real units (4114 Bldg4 $2,335 Available
Now, available_date 2026-05-17, …). Deterministic Tier-1.
"""

from __future__ import annotations

import datetime
import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ma_poc.pms.adapters._parsing import make_unit_dict
from ma_poc.pms.adapters._probe import probe_post
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.generic import _get_page_html

if TYPE_CHECKING:
    from playwright.async_api import Page

_TIER = "TIER_1_API_REPLI360"
_API = "https://app.repli360.com/admin/getUnitListByFloor"

# onclick="getUnitListByFloor(this,'A1AL' , 2 , 1619,``);"
_ONCLICK_RE = re.compile(
    r"getUnitListByFloor\(\s*this\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*"
    r"(\d+)\s*,\s*(\d+)",
    re.IGNORECASE,
)
_MARK_RE = re.compile(
    r"getUnitListByFloor\(|app\.repli360\.com|rrac_listAvailableUnit",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(r"[\d,]+")


def _origin_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


def _movein_today() -> str:
    """Repli360 expects the move-in date as ``"%-d %b %Y"`` (no zero pad).

    ``%-d`` is non-portable (fails on Windows); build it explicitly.
    """
    now = datetime.datetime.now(datetime.UTC)
    return f"{now.day} {now.strftime('%b %Y')}"


def find_repli360_floorplans(html: str) -> tuple[str, list[tuple[str, str]]]:
    """Return ``(site_id, [(floorPlanID, template_type), ...])``.

    Parsed from the JS-rendered ``getUnitListByFloor(this,'<fp>',<tt>,
    <sid>)`` onclick attributes. Empty when the page was not rendered
    (the attrs are JS-injected and absent from static HTML) — the
    caller degrades gracefully.
    """
    site_id = ""
    seen: set[str] = set()
    fps: list[tuple[str, str]] = []
    for m in _ONCLICK_RE.finditer(html or ""):
        fpid = m.group(1).strip()
        ttype = m.group(2).strip()
        sid = m.group(3).strip()
        if sid:
            site_id = sid
        if fpid and fpid not in seen:
            seen.add(fpid)
            fps.append((fpid, ttype))
    return site_id, fps


def parse_repli360_str(str_html: str, source_url: str) -> list[dict[str, Any]]:
    """Parse the ``str`` HTML from getUnitListByFloor → unit-level dicts.

    Each ``<tr class="unitlisting ...">`` is one available unit:
    building (1st td), unit number (``b.unitNumber``), deposit,
    ``span.unit_price_value`` rent, availability td, and the row's
    ``data-available_date`` (already ISO ``YYYY-MM-DD``).
    """
    if not str_html:
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(str_html, "lxml")
    out: list[dict[str, Any]] = []
    for tr in soup.select("tr.unitlisting"):
        b = tr.select_one("b.unitNumber")
        unit = b.get_text(strip=True) if b else ""
        if not unit:
            continue
        avail_date = str(tr.get("data-available_date") or "").strip()
        tds = tr.find_all("td")
        building = ""
        if tds:
            building = (
                tds[0].get_text(strip=True).replace("Building Number", "").strip()
            )
        rent: int | None = None
        price_el = tr.select_one("span.unit_price_value")
        if price_el is not None:
            mm = _MONEY_RE.search(price_el.get_text())
            if mm:
                try:
                    rent = int(mm.group(0).replace(",", ""))
                except (TypeError, ValueError):
                    rent = None
        avail_txt = "AVAILABLE"
        for td in tds:
            t = td.get_text(" ", strip=True)
            if "Availability" in t:
                cleaned = t.replace("Availability", "").strip()
                avail_txt = cleaned or "AVAILABLE"
                break
        out.append(
            make_unit_dict(
                unit_number=unit,
                building=building,
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE"
                if "available" in avail_txt.lower()
                else "UNKNOWN",
                availability_date=avail_date,
                source_api_url=source_url,
                extraction_tier=_TIER,
            )
        )
    return out


class Repli360Adapter:
    """Repli360 / rrac same-domain ``getUnitListByFloor`` extractor."""

    pms_name: str = "repli360"

    def static_fingerprints(self) -> list[str]:
        return ["app.repli360.com", "getUnitListByFloor", "rrac_listAvailableUnit"]

    def matches_response_body(self, body: Any) -> bool:
        if isinstance(body, str):
            return bool(_MARK_RE.search(body))
        return False

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_TIER)

        html = await _get_page_html(page, ctx)
        if not html:
            result.tier_used = f"{_TIER}_NO_HTML"
            result.errors.append("REPLI360: no page html")
            return result

        site_id, fps = find_repli360_floorplans(html)
        if not site_id or not fps:
            # onclick attrs are JS-injected; absent ⇒ page not rendered
            # or not actually a repli360 site. Degrade to fallback.
            result.tier_used = f"{_TIER}_NO_FLOORPLANS"
            result.errors.append(
                "REPLI360: site_id/floorPlanID not found in rendered HTML"
            )
            return result

        origin = _origin_of(
            str(getattr(getattr(ctx, "fetch_result", None), "final_url", "") or "")
        ) or _origin_of(getattr(ctx, "base_url", "") or "")
        movein = _movein_today()
        all_units: list[dict[str, Any]] = []
        seen_units: set[str] = set()
        for fpid, ttype in fps:
            data = {
                "floorPlanID": fpid,
                "moveinDate": movein,
                "site_id": site_id,
                "template_type": ttype or "2",
                "mode": "apt",
                "type": "2d",
                "currentanuualterm": "",
                "AcademicTerm": "",
                "RentalLevel": "",
                "special": "no",
                "zpopUp": "",
            }
            headers = {"Referer": origin + "/" if origin else "", "Origin": origin}
            try:
                resp = probe_post(_API, data=data, headers=headers, timeout=25)
            except Exception as exc:  # noqa: BLE001 — never raise from an adapter
                result.errors.append(
                    f"repli360-fetch-error[{fpid}]: "
                    f"{type(exc).__name__}: {str(exc)[:100]}"
                )
                continue
            if getattr(resp, "status_code", 0) != 200:
                continue
            try:
                j = json.loads(resp.text or "{}")
            except (json.JSONDecodeError, ValueError):
                continue
            for u in parse_repli360_str(str(j.get("str") or ""), _API):
                key = f"{u.get('unit_number')}|{u.get('building')}"
                if key in seen_units:
                    continue
                seen_units.add(key)
                all_units.append(u)

        result.units = all_units
        result.confidence = 1.0 if all_units else 0.0
        if not all_units:
            result.errors.append("REPLI360: no units parsed from any floorplan")
        return result
