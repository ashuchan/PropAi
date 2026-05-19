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
from ma_poc.pms.adapters._probe import probe_get, probe_post
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.generic import _get_page_html

if TYPE_CHECKING:
    from playwright.async_api import Page

_TIER = "TIER_1_API_REPLI360"
_API = "https://app.repli360.com/admin/getUnitListByFloor"
_TPL_RENDER = "https://app.repli360.com/admin/template-render"

# The per-property repli360 embed script — present in the property's
# STATIC HTML (no render needed): src=".../admin/rrac-website-script/
# <encrypted-token>". Fetching it yields ``var site_id = '<id>'``.
_SCRIPT_RE = re.compile(
    r"https?://app\.repli360\.com/admin/rrac-website-script/[A-Za-z0-9=_./+-]+",
    re.IGNORECASE,
)
_SITEID_RE = re.compile(
    r"""var\s+site_id\s*=\s*['"](\d{2,9})['"]""", re.IGNORECASE
)

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


def find_repli360_script_url(html: str) -> str:
    """Return the ``rrac-website-script/<token>`` embed URL, or ''.

    This is in the property's STATIC HTML — it does NOT require the page
    to be rendered (unlike the JS-injected onclick attrs). It is the
    render-independent entry point.
    """
    m = _SCRIPT_RE.search(html or "")
    return m.group(0) if m else ""


def fetch_repli360_site_id(script_url: str, referer: str = "") -> str:
    """GET the embed script, return its ``var site_id = '<id>'``, or ''.

    No auth, no bot wall (verified). Best-effort: any failure → ''.
    """
    if not script_url:
        return ""
    try:
        hdrs = {"Referer": referer} if referer else {}
        r = probe_get(script_url, headers=hdrs, timeout=20)
    except Exception:
        return ""
    if getattr(r, "status_code", 0) != 200:
        return ""
    m = _SITEID_RE.search(r.text or "")
    return m.group(1) if m else ""


def fetch_repli360_floorplans(
    site_id: str, referer: str = ""
) -> list[tuple[str, str]]:
    """POST ``/admin/template-render`` → the floorplan list.

    The bootstrap widget HTML it returns embeds every
    ``getUnitListByFloor(this,'<fp>',<tt>,<sid>)`` onclick — i.e. the
    full floorplan list — render-free. Reuses :func:`find_repli360_
    floorplans` to parse those attrs. No auth / no bot wall (verified).
    """
    if not site_id:
        return []
    try:
        origin = _origin_of(referer)
        hdrs = {"Referer": referer or "", "Origin": origin}
        r = probe_post(
            _TPL_RENDER,
            data={
                "site_id": site_id,
                "template_type": "2",
                "action": "",
                "ready_script": "",
                "source": "",
                "property_id": "",
                "zpopUp": "",
            },
            headers=hdrs,
            timeout=25,
        )
    except Exception:
        return []
    if getattr(r, "status_code", 0) != 200:
        return []
    _sid, fps = find_repli360_floorplans(r.text or "")
    return fps


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

        origin = _origin_of(
            str(getattr(getattr(ctx, "fetch_result", None), "final_url", "") or "")
        ) or _origin_of(getattr(ctx, "base_url", "") or "")
        referer = origin + "/" if origin else ""

        # PRIMARY (render-independent): the embed-script URL is in the
        # STATIC HTML → fetch it → site_id → /admin/template-render →
        # floorplan list. All server-side, no auth, no bot wall, no
        # browser. This is what lifts repli360 past the render cap.
        site_id = ""
        fps: list[tuple[str, str]] = []
        script_url = find_repli360_script_url(html)
        if script_url:
            site_id = fetch_repli360_site_id(script_url, referer)
            if site_id:
                fps = fetch_repli360_floorplans(site_id, referer)

        # FALLBACK: if the static chain didn't resolve (no script tag, or
        # template-render empty), use JS-rendered onclick attrs if the
        # page happened to be rendered. Keeps the prior behaviour.
        if not site_id or not fps:
            r_sid, r_fps = find_repli360_floorplans(html)
            site_id = site_id or r_sid
            fps = fps or r_fps

        if not site_id or not fps:
            result.tier_used = f"{_TIER}_NO_FLOORPLANS"
            result.errors.append(
                "REPLI360: site_id/floorPlanID not resolvable "
                "(static script chain + rendered onclick both empty)"
            )
            return result
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
