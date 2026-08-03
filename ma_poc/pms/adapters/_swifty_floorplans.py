"""Property-scoped Swifty WordPress unit-roster recovery.

Swifty multifamily sites publish their inventory through two same-origin
WordPress AJAX actions.  The entry page declares both action names and the
exact ``/wp-admin/admin-ajax.php`` endpoint; the first response lists floor
plans and the second returns native apartment rows for one floor plan.

The route was live-probed on three independent properties on 2026-08-01:
946 MLK, BroadVue, and The Kace.  Their unit tables use both four- and
five-column layouts, so availability is selected by semantic cell shape rather
than a fixed position.  No browser solver, LLM, or account-wide endpoint is
involved.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ma_poc.pms.adapters._parsing import bed_label_from, make_unit_dict, money_to_int
from ma_poc.pms.adapters._probe import body_html_from_ctx, probe_post

SWIFTY_TIER = "TIER_1_DOM_SWIFTY_UNIT_AJAX"
_LIST_ACTION = "swifty_floorplan_section_details_with_ajax"
_UNIT_ACTION = "swifty_load_available_units"
_AJAX_PATH = "/wp-admin/admin-ajax.php"
_PLAN_ID_RE = re.compile(r"^\d{1,12}$")
_TRAILING_VARIANT_RE = re.compile(r"\s+\([^)]+\)\s*$")
_DATE_CELL_RE = re.compile(
    r"^(?:"
    r"available(?:\s+now)?|now|immediate(?:ly)?"
    r"|\d{1,2}[-/]\d{1,2}(?:[-/]\d{2,4})?"
    r"|(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+\d{1,2}"
    r"(?:,?\s+\d{2,4})?"
    r")$",
    re.IGNORECASE,
)
_MAX_PLANS = 50
_MAX_UNITS = 1000


@dataclass(frozen=True)
class SwiftyFloorplan:
    plan_id: str
    name: str
    beds: int | None
    baths: str
    sqft: str


def has_swifty_unit_ajax(html: str) -> bool:
    """Require the exact provider plugin and both published action names."""
    low = str(html or "").lower()
    return (
        "swifty-frontend" in low
        and _LIST_ACTION.lower() in low
        and _UNIT_ACTION.lower() in low
        and "siteajaxurl" in low
    )


def _host_key(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def extract_swifty_ajax_url(html: str, base_url: str) -> str:
    """Return a same-property WordPress AJAX URL or ``""``.

    The page controls ``data-url``; same-host and exact-path checks prevent a
    captured marketing page from turning this helper into an arbitrary POST.
    ``www`` and apex forms are treated as the same origin family.
    """
    if not has_swifty_unit_ajax(html):
        return ""
    try:
        soup = BeautifulSoup(html, "lxml")
        node = soup.select_one(".siteajaxurl[data-url]")
        raw = str(node.get("data-url") or "").strip() if node else ""
        candidate = urljoin(base_url, raw)
        parts = urlsplit(candidate)
        if parts.scheme.lower() not in {"http", "https"}:
            return ""
        if not _host_key(base_url) or _host_key(candidate) != _host_key(base_url):
            return ""
        if parts.path.rstrip("/").lower() != _AJAX_PATH:
            return ""
        return f"{parts.scheme}://{parts.netloc}{_AJAX_PATH}"
    except Exception:
        return ""


def _optional_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_swifty_floorplans(html: str) -> list[SwiftyFloorplan]:
    """Parse the first AJAX response into bounded floor-plan descriptors."""
    try:
        soup = BeautifulSoup(html or "", "lxml")
        out: list[SwiftyFloorplan] = []
        seen: set[str] = set()
        for card in soup.select(".single-floorplan[data_id]"):
            plan_id = str(card.get("data_id") or "").strip()
            if not _PLAN_ID_RE.fullmatch(plan_id) or plan_id in seen:
                continue
            anchor = card.select_one("[data-name]")
            name = str(anchor.get("data-name") or "").strip() if anchor else ""
            if not name:
                title = card.select_one(".flp-title")
                name = title.get_text(" ", strip=True) if title else ""
            if not name:
                continue
            beds = _optional_int(anchor.get("data-bed")) if anchor else None
            baths = str(anchor.get("data-bath") or "").strip() if anchor else ""
            sqft = str(anchor.get("data-sqft") or "").strip() if anchor else ""
            seen.add(plan_id)
            out.append(SwiftyFloorplan(plan_id, name, beds, baths, sqft))
            if len(out) >= _MAX_PLANS:
                break
        return out
    except Exception:
        return []


def _availability_cell(cells: list[str]) -> str:
    for cell in cells[1:]:
        value = re.sub(r"\s+", " ", cell).strip()
        if _DATE_CELL_RE.fullmatch(value):
            return value
    return ""


def _floor_cell(cells: list[str], availability: str) -> str:
    for cell in cells[2:]:
        value = re.sub(r"\s+", " ", cell).strip()
        if not value or value == availability or "apply" in value.lower():
            continue
        if re.fullmatch(r"\d{1,3}", value) or "floor" in value.lower():
            return value
    return ""


def parse_swifty_unit_rows(
    html: str,
    plan: SwiftyFloorplan,
    source_url: str,
) -> list[dict[str, Any]]:
    """Parse four- or five-column Swifty unit-table rows."""
    try:
        soup = BeautifulSoup(html or "", "lxml")
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tr in soup.select("tr.single-flp-unit-row"):
            cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)).strip() for td in tr.select("td")]
            if len(cells) < 3:
                continue
            unit_raw = cells[0]
            unit_number = _TRAILING_VARIANT_RE.sub("", unit_raw).strip()
            if not unit_number or unit_number in seen:
                continue
            rent = money_to_int(cells[1])
            if rent is None or not 200 <= rent <= 50000:
                continue
            availability = _availability_cell(cells)
            floor = _floor_cell(cells, availability)
            out.append(
                make_unit_dict(
                    floor_plan_name=plan.name,
                    bed_label=bed_label_from(plan.beds, plan.name),
                    bedrooms=str(plan.beds) if plan.beds is not None else "",
                    bathrooms=plan.baths,
                    sqft=plan.sqft,
                    unit_number=unit_number,
                    unit_name=unit_raw if unit_raw != unit_number else "",
                    floor=floor,
                    rent_low=rent,
                    rent_high=rent,
                    availability_status="AVAILABLE",
                    availability_date=availability,
                    source_api_url=source_url,
                    extraction_tier=SWIFTY_TIER,
                )
            )
            seen.add(unit_number)
        return out
    except Exception:
        return []


def _fetch_swifty_units(ajax_url: str, referer: str) -> list[dict[str, Any]]:
    parts = urlsplit(referer)
    headers = {
        "Origin": f"{parts.scheme}://{parts.netloc}",
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
    }
    listing = probe_post(
        ajax_url,
        data={
            "action": _LIST_ACTION,
            "nocache": str(int(time.time() * 1000)),
            "pageType": "floorplans",
        },
        headers=headers,
        timeout=25,
    )
    if int(getattr(listing, "status_code", 0) or 0) != 200:
        return []
    plans = parse_swifty_floorplans(getattr(listing, "text", "") or "")
    out: list[dict[str, Any]] = []
    seen_units: set[str] = set()
    for plan in plans:
        response = probe_post(
            ajax_url,
            data={
                "action": _UNIT_ACTION,
                "flp_id": plan.plan_id,
                "nocache": str(int(time.time() * 1000)),
            },
            headers=headers,
            timeout=25,
        )
        if int(getattr(response, "status_code", 0) or 0) != 200:
            continue
        for row in parse_swifty_unit_rows(getattr(response, "text", "") or "", plan, ajax_url):
            unit_number = str(row.get("unit_number") or "").strip()
            if not unit_number or unit_number in seen_units:
                continue
            seen_units.add(unit_number)
            out.append(row)
            if len(out) >= _MAX_UNITS:
                return out
    return out


async def recover_swifty_floorplans(ctx: Any) -> list[dict[str, Any]]:
    """Recover native Swifty units only when the captured page opts in."""
    body = body_html_from_ctx(ctx)
    if not has_swifty_unit_ajax(body):
        return []
    fetch_result = getattr(ctx, "fetch_result", None)
    referer = str(getattr(fetch_result, "final_url", "") or "")
    referer = referer or str(getattr(ctx, "base_url", "") or "")
    ajax_url = extract_swifty_ajax_url(body, referer)
    if not ajax_url:
        return []
    try:
        return await asyncio.to_thread(_fetch_swifty_units, ajax_url, referer)
    except Exception:
        return []
