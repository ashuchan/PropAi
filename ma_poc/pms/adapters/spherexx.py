"""
Spherexx Presentation Software ("Convert") adapter.

Research log
------------
Web sources consulted:
  - https://spherexx.com — Spherexx multifamily marketing platform
  - https://presentation.spherexx.app — the iframe-hosted SPA backend
Live probe (2026-05-13, henryonthepark.com/interactive-site-map/):
  - Embed pattern: <script>window.sspcfg={key:'<base64>',opts:{...}}</script>
    + <script src="https://presentation.spherexx.app/js/ssploader.js" defer>
  - Loader creates an iframe → presentation.spherexx.app/#/ssp/availability
  - Iframe makes POST /api/authenticate → returns JWT
  - Subsequent calls use Bearer <JWT>:
      GET /api/community       (property metadata)
      GET /api/configuration   (site-plan UI config)
      GET /api/unit            (UNIT LIST — list of unit objects)
      GET /api/floorplan       (floor-plan list)
      GET /api/amenity
      GET /api/fees
Key findings:
  - /api/unit is a JSON ARRAY of unit objects with the shape:
      {ID, Name, Building, Number, Sqft, Bed, Bath, Floor, Price,
       PriceMin, PriceMax, AvailableDate, FloorplanID, FloorplanName,
       ...}
  - /api/floorplan is a JSON ARRAY of floor-plan metadata:
      {ID, Name, Bed, Bath, MinSqFt, MaxSqFt, ...}
  - Units join to floor-plans via FloorplanID — but units already carry
    FloorplanName so we don't actually need the join for emission.
  - The canary's page.on("response") captures all iframe XHRs since
    Playwright fires response events for child frames, so the adapter
    doesn't need to authenticate / re-fetch — the captured responses
    are in ctx._api_responses.
"""

from __future__ import annotations

import asyncio
import base64
import html as html_lib
import json
import re
from datetime import date
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urljoin, urlparse

from ma_poc.pms.adapters._parsing import (
    BATH_RE,
    bed_label_from,
    make_unit_dict,
    money_to_int,
    parse_area,
    rent_in_sanity_range,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


_TIER_BASE = "TIER_1_API_SPHEREXX"
_TIER_NO_RESPONSE = f"{_TIER_BASE}_NO_RESPONSE"
_TIER_SHAPE_REJECTED = f"{_TIER_BASE}_SHAPE_REJECTED"
_TIER_PARSE_FAILED = f"{_TIER_BASE}_PARSE_FAILED"
_TIER_PRESENTATION_DIRECT = f"{_TIER_BASE}_PRESENTATION_DIRECT"
# ZRS/spherexx server-rendered floorplan-detail path (no API iframe).
# chathamsquare/mirabella-class: units are server-rendered in an HTML
# table on /floorplans/<bed>/<plan>/ detail pages, NOT via the
# presentation.spherexx.app /api/unit iframe. Deterministic Tier-1.
_TIER_ZRS = "TIER_1_DOM_SPHEREXX_ZRS"
# Current Spherexx/ZRS template.  Unlike the older hidden-input table
# above, this template renders explicit ``tr.unit-list__unit`` rows with a
# public unit-detail link, base rent, and a per-unit apply URL.  All of the
# data is present in the HTML returned by the ordinary page fetch.
_TIER_ZRS_UNIT_LIST = "TIER_1_DOM_SPHEREXX_ZRS_UNIT_LIST"
# Current Spherexx/ZRS marketing sites hydrate their floor-plan cards from two
# public same-origin JSON routes.  ``plansandpricing`` is the plan catalogue;
# ``availabilityv2`` is the physical-apartment roster.  The latter exposes a
# stable UnitID + ApartmentNumber pair, rent, area, and explicit move-in date.
_TIER_ZRS_AVAILABILITY_V2 = "TIER_1_API_SPHEREXX_ZRS_AVAILABILITY_V2"
# Razz/myrazz embedded portal: "Happily Made by Razz" Vue SPA renders a
# per-unit list at /models with a labeled "Available <date>" column.
# Distinct from the presentation.spherexx.app /api iframe — no API XHR
# fires; units live only in the post-hydration DOM. Anchor on the stable
# ``wrap-model-item model-list`` container + label TEXT (the date leaf is
# an unclassed <div>, so class selectors are unsafe — same lesson as the
# AppFolio js-listing-* scare). Raw "May 19"/"Now" is passed through;
# schema_v2._format_date normalizes it (no-year→run year, Now→run date).
_TIER_RAZZ = "TIER_1_DOM_SPHEREXX_RAZZ"
# Older Spherexx/Kamson/Adkast availability embeds are ordinary server-
# rendered tables under ``clients.spherexx.com``.  They predate the
# presentation.spherexx.app API but still publish a PMS-native apartment id
# per row (``data-unitid`` or ``href=#unit<id>``), a physical unit number and
# the current rent.  Keep this path distinct from both the SPA and ZRS tiers
# so a canary can measure it independently.
_TIER_LEGACY = "TIER_1_DOM_SPHEREXX_LEGACY_AVAILABILITY"

_RAZZ_ITEM_RE = re.compile(r"wrap-model-item[\s\"']*model-list", re.IGNORECASE)
_RAZZ_UNIT_RE = re.compile(
    r"Unit\s+([A-Za-z0-9.\-]+)\s*-\s*(Studio|\d+)\s*Bed\s*\|\s*"
    r"([\d.]+)\s*Bath",
    re.IGNORECASE,
)
_RAZZ_RENT_RE = re.compile(r"Base\s*Rent\s*\$?\s*([\d,]+)", re.IGNORECASE)
_RAZZ_SQFT_RE = re.compile(r"Sq\.?\s*Ft\.?\s*([\d,]+)", re.IGNORECASE)
_RAZZ_AVAIL_RE = re.compile(
    r"Available\s+(Now|Today|[A-Za-z]{3,9}\.?\s+\d{1,2}"
    r"|\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)


def _legacy_cell_text(row: Any, *labels: str) -> str:
    """Return text from the first legacy table cell with a matching label."""
    wanted = {
        re.sub(r"[^a-z0-9]+", "", label.casefold())
        for label in labels
        if label
    }
    for cell in row.find_all(["td", "th"]):
        observed = re.sub(
            r"[^a-z0-9]+",
            "",
            str(cell.get("data-label") or "").casefold(),
        )
        if observed in wanted:
            return re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()
    return ""


def _legacy_plan_name(row: Any) -> str:
    """Find the plan/bedroom heading immediately preceding a legacy table."""
    table = row.find_parent("table")
    if table is None:
        return ""
    heading = table.find_previous(
        lambda tag: (
            getattr(tag, "name", None) in {"h2", "h3"}
            or (
                getattr(tag, "name", None) == "p"
                and "ntabletitle"
                in {str(c).casefold() for c in (tag.get("class") or [])}
            )
        )
    )
    if heading is None:
        return ""
    return re.sub(r"\s+", " ", heading.get_text(" ", strip=True)).strip()


def parse_spherexx_legacy_availability(
    html: str,
    source_url: str,
) -> list[dict[str, Any]]:
    """Parse legacy ``clients.spherexx.com/*availability*`` tables.

    Two currently published templates are supported:

    * Kamson: ``tr[data-unitid][data-floorplanid]``.
    * Adkast: ``table.availability tr`` with ``href="#unit480051"``.

    A row is admitted only when all three strict unit signals are present:
    a PMS-native id, a physical unit label, and a positive current rent.  This
    deliberately excludes headings, wait-list plan ids, and empty roster rows.
    """
    if not html or not source_url or "availability" not in html.casefold():
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

    candidate_rows: list[Any] = []
    seen_nodes: set[int] = set()
    for row in [
        *soup.select("tr[data-unitid]"),
        *soup.select("table.availability tr"),
    ]:
        node_key = id(row)
        if node_key in seen_nodes:
            continue
        seen_nodes.add(node_key)
        candidate_rows.append(row)

    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row in candidate_rows:
        native_id = str(row.get("data-unitid") or "").strip()
        if not native_id:
            native_link = row.find("a", href=re.compile(r"^#unit\d+$", re.I))
            native_match = re.search(
                r"#unit(\d+)",
                str(native_link.get("href") or "") if native_link else "",
                re.I,
            )
            native_id = native_match.group(1) if native_match else ""

        unit_number = _legacy_cell_text(row, "Unit", "Apartment")
        rent = money_to_int(_legacy_cell_text(row, "Rent", "Price"))
        if not native_id or native_id in seen_ids or not unit_number or not rent:
            continue

        beds_text = _legacy_cell_text(row, "Bedroom", "Bedrooms", "Beds")
        baths_text = _legacy_cell_text(row, "Bathroom", "Bathrooms", "Baths")
        beds_match = re.search(r"\d+(?:\.\d+)?", beds_text)
        baths_match = re.search(r"\d+(?:\.\d+)?", baths_text)
        beds = beds_match.group(0) if beds_match else ""
        baths = baths_match.group(0) if baths_match else ""
        if beds.endswith(".0"):
            beds = beds[:-2]
        if baths.endswith(".0"):
            baths = baths[:-2]

        sqft_text = _legacy_cell_text(
            row,
            "Sq.Ft.",
            "Sq Ft",
            "Sqft",
            "Square Feet",
        )
        sqft_match = re.search(r"\d[\d,]*", sqft_text)
        sqft = sqft_match.group(0).replace(",", "") if sqft_match else ""
        availability = _legacy_cell_text(
            row,
            "Availability",
            "Available",
            "Date Available",
        )
        lease_term = _legacy_cell_text(row, "LeaseTerm", "Lease Term")
        building = _legacy_cell_text(row, "Building")
        floorplan_id = str(row.get("data-floorplanid") or "").strip()
        source_ids: dict[str, str] = {"spherexx_unit_id": native_id}
        if floorplan_id:
            source_ids["spherexx_floorplan_id"] = floorplan_id

        unit = make_unit_dict(
            floor_plan_name=_legacy_plan_name(row),
            bedrooms=beds,
            bathrooms=baths,
            sqft=sqft,
            unit_number=unit_number,
            building=building,
            rent_low=rent,
            rent_high=rent,
            availability_status="AVAILABLE",
            available_units="1",
            availability_date=availability,
            lease_term=lease_term,
            source_api_url=source_url,
            extraction_tier=_TIER_LEGACY,
            source_ids=source_ids,
        )

        label_node = row.find(attrs={"data-tlabel": True})
        observed_label = (
            re.sub(
                r"\s+",
                " ",
                str(label_node.get("data-tlabel") or ""),
            ).strip()
            if label_node
            else ""
        )
        if observed_label:
            observed_name = re.sub(
                rf"\s+-\s+{re.escape(unit_number)}\s*$",
                "",
                observed_label,
            ).strip()
            if observed_name:
                unit["source_property_name"] = observed_name

        seen_ids.add(native_id)
        out.append(unit)
    return out



# Detail-page paths across ZRS template variants:
#   /floorplans/4bedroom/d1/            (chathamsquare)
#   /floorplans-and-pricing/1-bed/11649 (mirabella)
#   /floor-plans/2-bed/a2/
_ZRS_DETAIL_RE = re.compile(
    r"/floor-?plans(?:-and-pricing)?/[a-z0-9-]+/[a-z0-9-]+/?",
    re.IGNORECASE,
)
# Per-unit hidden-input block + adjacent price cell.
_ZRS_UID_RE = re.compile(
    r'data-type="uid"\s+value="(\d+)"', re.IGNORECASE
)
_ZRS_UNITNO_RE = re.compile(
    r'data-type="unitNumber"\s+value="([^"]*)"', re.IGNORECASE
)
_ZRS_BID_RE = re.compile(r'data-type="bid"\s+value="([^"]*)"', re.IGNORECASE)
_ZRS_BASEPRICE_RE = re.compile(
    r'data-base-unit-price="([\d.]+)"', re.IGNORECASE
)
_ZRS_OVERVIEW_INFO_RE = re.compile(
    r"(?:(Studio)|([\d.]+)\s*Bed)\s+([\d.]+)\s*Bath\s+([\d,]+)\s*SF\b",
    re.IGNORECASE,
)

_PRESENTATION_ORIGIN = "https://presentation.spherexx.app"
_PRESENTATION_MAX_AUTH_BYTES = 64 * 1024
_PRESENTATION_MAX_JSON_BYTES = 2 * 1024 * 1024
_PRESENTATION_MAX_MARKETING_BYTES = 1024 * 1024
_PRESENTATION_MAX_UNITS = 1_000
_PRESENTATION_HTTP_TIMEOUT_SECONDS = 20.0

_PRESENTATION_KEY_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_PRESENTATION_DECODED_KEY_RE = re.compile(rb"fpaw:[A-Za-z0-9]+")
_PRESENTATION_LITERAL_CONFIG_RE = re.compile(
    r"\bwindow\.sspcfg\s*=\s*\{\s*['\"]key['\"]\s*:\s*"
    r"['\"](?P<key>[A-Za-z0-9+/]+={0,2})['\"]\s*,\s*"
    r"['\"]opts['\"]\s*:\s*\{",
    re.IGNORECASE | re.DOTALL,
)
_PRESENTATION_BTOA_CONFIG_RE = re.compile(
    r"\b(?:var|let|const)\s+(?P<variable>[A-Za-z_$][\w$]*)\s*=\s*"
    r"window\.btoa\(\s*['\"]fpaw:['\"]\s*\+\s*"
    r"['\"](?P<feed>[A-Za-z0-9]+)['\"]\s*\)\s*;?"
    r".{0,256}?\bwindow\.sspcfg\s*=\s*\{\s*['\"]key['\"]\s*:\s*"
    r"(?P=variable)\b",
    re.IGNORECASE | re.DOTALL,
)
_PRESENTATION_IFRAME_SRC_RE = re.compile(
    r"<iframe\b[^>]*\bsrc\s*=\s*['\"](?P<src>[^'\"]+)['\"]",
    re.IGNORECASE | re.DOTALL,
)
_PRESENTATION_OUTER_CONFIG_RE = re.compile(
    r"^\s*\{\s*['\"]key['\"]\s*:\s*"
    r"['\"](?P<key>[A-Za-z0-9+/]+={0,2})['\"]\s*,\s*"
    r"['\"]opts['\"]\s*:\s*\{.{0,256}\}\s*\}\s*$",
    re.IGNORECASE | re.DOTALL,
)
_PRESENTATION_LINK_RE = re.compile(
    r"<a\b[^>]*\bhref\s*=\s*['\"](?P<href>[^'\"]+)['\"]",
    re.IGNORECASE | re.DOTALL,
)

_GENERIC_PROPERTY_NAME_TOKENS = frozenset(
    {
        "apartment",
        "apartments",
        "community",
        "east",
        "homes",
        "i",
        "ii",
        "north",
        "of",
        "on",
        "residence",
        "residences",
        "south",
        "the",
        "west",
    }
)
_GENERIC_STREET_TOKENS = frozenset(
    {
        "apt",
        "avenue",
        "ave",
        "boulevard",
        "blvd",
        "circle",
        "court",
        "ct",
        "drive",
        "dr",
        "east",
        "highway",
        "hwy",
        "lane",
        "ln",
        "north",
        "parkway",
        "pkwy",
        "road",
        "rd",
        "south",
        "street",
        "st",
        "suite",
        "unit",
        "way",
        "west",
    }
)


def _beds_from_url_seg(seg: str) -> str:
    """``4bedroom`` → ``4``; ``studio`` → ``0``; else ''."""
    s = (seg or "").lower()
    if "studio" in s:
        return "0"
    m = re.search(r"\d+", s)
    return m.group(0) if m else ""


def _zrs_explicit_date(value: Any) -> str:
    """Return a source date as ISO without applying a timezone conversion."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:T|\s|$)", raw)
    if iso:
        try:
            return date(*(int(part) for part in iso.groups())).isoformat()
        except ValueError:
            return ""
    us = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})(?:\s|$)", raw)
    if us:
        month, day, year = (int(part) for part in us.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return ""
    return ""


def _zrs_plan_catalog(
    plans_body: Any,
    index_html: str,
) -> dict[str, dict[str, str]]:
    """Build a floor-plan-ID catalogue from public JSON plus visible cards.

    The JSON route is authoritative for ID/name.  The server-rendered cards
    enrich that pair with bedrooms, bathrooms, advertised area, and lease
    term.  A card can also supply the ID/name when the plan route is briefly
    unavailable, but conflicting names are never guessed through.
    """
    catalog: dict[str, dict[str, str]] = {}
    conflicts: set[str] = set()

    if isinstance(plans_body, list):
        for plan in plans_body:
            if not isinstance(plan, dict):
                continue
            plan_id = str(
                plan.get("FloorplanID") or plan.get("FloorPlanID") or ""
            ).strip()
            name = re.sub(
                r"\s+", " ", str(plan.get("FloorplanName") or "")
            ).strip()
            if not plan_id.isdigit() or not name or len(name) > 120:
                continue
            prior = catalog.get(plan_id)
            if prior is not None and prior["name"].casefold() != name.casefold():
                conflicts.add(plan_id)
                continue
            catalog[plan_id] = {"name": name}

    if index_html:
        try:
            from bs4 import BeautifulSoup

            try:
                soup = BeautifulSoup(index_html, "lxml")
            except Exception:
                soup = BeautifulSoup(index_html, "html.parser")
        except Exception:
            soup = None
        if soup is not None:
            for card in soup.select("[data-fpid]"):
                plan_id = str(card.get("data-fpid") or "").strip()
                name_el = card.select_one(".floorplans__fpname")
                visible_name = re.sub(
                    r"\s+",
                    " ",
                    str(card.get("data-name") or "")
                    or (name_el.get_text(" ", strip=True) if name_el else ""),
                ).strip()
                if not plan_id.isdigit() or not visible_name or len(visible_name) > 120:
                    continue
                prior = catalog.get(plan_id)
                if prior is not None and prior["name"].casefold() != visible_name.casefold():
                    conflicts.add(plan_id)
                    continue
                item = prior or {"name": visible_name}
                beds = str(card.get("data-bed") or "").strip()
                if re.fullmatch(r"\d+(?:\.0+)?", beds):
                    item["beds"] = str(int(float(beds)))
                baths = str(card.get("data-bath") or "").strip()
                if not baths:
                    bath_match = BATH_RE.search(card.get_text(" ", strip=True))
                    baths = bath_match.group(1) if bath_match else ""
                if re.fullmatch(r"\d+(?:\.\d+)?", baths):
                    item["baths"] = baths.rstrip("0").rstrip(".")
                sqft_attr = money_to_int(str(card.get("data-sqft") or ""))
                sqft = (
                    sqft_attr
                    if sqft_attr is not None and 150 <= sqft_attr <= 10_000
                    else parse_area(
                        card.get_text(" ", strip=True), amenity_guard=False
                    )
                )
                if sqft is not None:
                    item["sqft"] = str(sqft)
                lease_term = str(card.get("data-lease-term") or "").strip()
                if lease_term.isdigit() and 1 <= int(lease_term) <= 60:
                    item["lease_term"] = f"{int(lease_term)} months"
                catalog[plan_id] = item

    for plan_id in conflicts:
        catalog.pop(plan_id, None)
    return catalog


def parse_zrs_availability_v2(
    units_body: Any,
    plans_body: Any,
    index_html: str,
    url: str,
) -> list[dict[str, Any]]:
    """Parse Spherexx ``/ajax/availabilityv2/`` physical-unit JSON.

    Admission deliberately requires a numeric vendor UnitID, a visible
    apartment number containing a digit, a positive displayed rent, and an
    exact floor-plan-ID join.  Plan-only ``{}`` responses and orphan rows are
    therefore retained as no-data rather than promoted with inferred IDs.
    """
    if not isinstance(units_body, list) or not units_body:
        return []
    catalog = _zrs_plan_catalog(plans_body, index_html)
    if not catalog:
        return []

    candidates: list[dict[str, Any]] = []
    for raw in units_body:
        if not isinstance(raw, dict):
            continue
        unit_id = str(raw.get("UnitID") or "").strip()
        unit_no = re.sub(
            r"\s+", " ", str(raw.get("ApartmentNumber") or "")
        ).strip()
        plan_id = str(
            raw.get("FloorplanID") or raw.get("FloorPlanID") or ""
        ).strip()
        plan = catalog.get(plan_id)
        if (
            not unit_id.isdigit()
            or not unit_no
            or not re.search(r"\d", unit_no)
            or len(unit_no) > 40
            or re.search(r"\$|\b(?:bed|bath|sq\.?\s*ft|rent|price)\b", unit_no, re.I)
            or plan is None
        ):
            continue

        rent = money_to_int(
            str(raw.get("MinPrice") or raw.get("DisplayPrice") or "")
        )
        if rent is None or rent <= 0 or not rent_in_sanity_range(rent):
            continue
        raw_sqft = money_to_int(str(raw.get("SqFt") or ""))
        sqft = plan.get("sqft") or (
            str(raw_sqft) if raw_sqft is not None and 150 <= raw_sqft <= 10_000 else ""
        )
        lease_term = plan.get("lease_term", "")
        raw_lease_term = str(raw.get("LeaseTerm") or "").strip()
        if raw_lease_term.isdigit() and 1 <= int(raw_lease_term) <= 60:
            lease_term = f"{int(raw_lease_term)} months"
        candidates.append(
            {
                "unit_id": unit_id,
                "unit_no": unit_no,
                "plan_id": plan_id,
                "plan": plan,
                "rent": rent,
                "sqft": sqft,
                "lease_term": lease_term,
                "availability_date": _zrs_explicit_date(raw.get("DateAvailable")),
            }
        )

    id_to_numbers: dict[str, set[str]] = {}
    number_to_ids: dict[str, set[str]] = {}
    for candidate in candidates:
        id_to_numbers.setdefault(candidate["unit_id"], set()).add(
            candidate["unit_no"]
        )
        number_to_ids.setdefault(candidate["unit_no"], set()).add(
            candidate["unit_id"]
        )

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        pair = (candidate["unit_id"], candidate["unit_no"])
        if pair in seen:
            continue
        seen.add(pair)
        if (
            len(id_to_numbers[candidate["unit_id"]]) != 1
            or len(number_to_ids[candidate["unit_no"]]) != 1
        ):
            continue
        plan = candidate["plan"]
        beds = plan.get("beds", "")
        rent = candidate["rent"]
        out.append(
            make_unit_dict(
                floor_plan_name=plan["name"],
                bed_label=(
                    bed_label_from(int(beds), plan["name"]) if beds else ""
                ),
                bedrooms=beds,
                bathrooms=plan.get("baths", ""),
                sqft=candidate["sqft"],
                unit_number=candidate["unit_no"],
                rent_range=f"${rent:,}",
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                available_units="1",
                availability_date=candidate["availability_date"],
                lease_term=candidate["lease_term"],
                source_ids={
                    "spherexx_unit_id": candidate["unit_id"],
                    "spherexx_floorplan_id": candidate["plan_id"],
                },
                source_api_url=url,
                extraction_tier=_TIER_ZRS_AVAILABILITY_V2,
            )
        )
    return out


def parse_zrs_floorplan_detail(html: str, url: str) -> list[dict[str, str]]:
    """Parse a ZRS/spherexx ``/floorplans/<bed>/<plan>/`` detail page.

    Each available unit is a row carrying hidden inputs
    ``data-type="uid|unitNumber|bid"`` and a price cell with
    ``data-base-unit-price``. Emits one unit-level row per unit with a
    real ``bid-unitNumber`` identity (never ``inferred_``).
    """
    if not html or "floorplan-detail__units" not in html:
        return []
    # Derive beds + plan name from the URL: /floorplans/<bed>/<plan>/
    beds = plan = ""
    mu = re.search(
        r"/floor-?plans(?:-and-pricing)?/([a-z0-9-]+)/([a-z0-9-]+)",
        url,
        re.IGNORECASE,
    )
    if mu:
        beds = _beds_from_url_seg(mu.group(1))
        plan = mu.group(2).upper()
    units: list[dict[str, str]] = []
    for m in _ZRS_UID_RE.finditer(html):
        uid = m.group(1)
        win = html[m.start() : m.start() + 1600]
        un = _ZRS_UNITNO_RE.search(win)
        bd = _ZRS_BID_RE.search(win)
        pr = _ZRS_BASEPRICE_RE.search(win)
        unit_no = (un.group(1) if un else "").strip()
        bid = (bd.group(1) if bd else "").strip()
        ident = "-".join(p for p in (bid, unit_no) if p) or f"sxx-{uid}"
        rent_i: int | None = None
        if pr:
            try:
                rent_i = int(round(float(pr.group(1))))
            except (TypeError, ValueError):
                rent_i = None
        units.append(
            make_unit_dict(
                floor_plan_name=plan,
                bedrooms=beds,
                unit_number=ident,
                rent_low=rent_i,
                rent_high=rent_i,
                availability_status="AVAILABLE",
                source_api_url=url,
                extraction_tier=_TIER_ZRS,
            )
        )
    return units


def parse_zrs_unit_list(html: str, url: str) -> list[dict[str, Any]]:
    """Parse the current server-rendered Spherexx/ZRS unit-list template.

    Admission is intentionally strict.  A row must provide all three of:

    * an explicit, visible apartment number containing a digit;
    * a positive base rent in ``data-og-display-price``; and
    * a per-unit apply link carrying numeric ``unitID`` and ``siteid``.

    The exact combination binds the marketing row to a physical apartment
    and prevents fee tables or floor-plan price cards from being promoted to
    unit inventory.  Conflicting duplicate IDs/numbers are rejected rather
    than guessed.
    """
    if not html or "unit-list__unit" not in html.lower():
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

    # A detail page has one overview shared by all rows.  Refuse to borrow
    # metadata when multiple distinct overviews are present; the row remains
    # usable because its unit identity + rent are independently explicit.
    names = {
        re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        for el in soup.select(".floorplan-overview__name")
        if el.get_text(" ", strip=True)
    }
    plan_name = next(iter(names)) if len(names) == 1 else ""
    info_values = {
        re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        for el in soup.select(".floorplan-overview__info")
        if el.get_text(" ", strip=True)
    }
    overview_info = next(iter(info_values)) if len(info_values) == 1 else ""
    beds = baths = sqft = ""
    info_match = _ZRS_OVERVIEW_INFO_RE.search(overview_info)
    if info_match:
        beds = "0" if info_match.group(1) else info_match.group(2)
        baths = info_match.group(3)
        sqft = info_match.group(4).replace(",", "")

    # URL fallback is useful on variants whose overview classes changed.
    url_match = re.search(
        r"/floor-?plans(?:-and-pricing)?/([a-z0-9-]+)/([a-z0-9-]+)",
        url,
        re.IGNORECASE,
    )
    if url_match:
        beds = beds or _beds_from_url_seg(url_match.group(1))
        plan_name = plan_name or url_match.group(2).replace("-", " ").title()

    from urllib.parse import parse_qs, urljoin, urlparse

    candidates: list[dict[str, Any]] = []
    for row in soup.select("tr.unit-list__unit"):
        unit_cell = row.find(
            ["th", "td"], attrs={"data-label": re.compile(r"^Apt\s*#$", re.I)}
        )
        price_cell = row.find(
            ["th", "td"], attrs={"data-label": re.compile(r"^Price$", re.I)}
        )
        if unit_cell is None or price_cell is None:
            continue

        unit_anchor = unit_cell.find("a", href=True)
        unit_no = re.sub(
            r"\s+", " ", unit_anchor.get_text(" ", strip=True) if unit_anchor else ""
        ).strip()
        if (
            not unit_no
            or not re.search(r"\d", unit_no)
            or len(unit_no) > 40
            or re.search(r"\$|\b(?:bed|bath|sq\.?\s*ft)\b", unit_no, re.I)
        ):
            continue
        unit_title = str(
            (unit_anchor or {}).get("title")
            or (unit_anchor or {}).get("aria-label")
            or ""
        )
        if not re.search(r"\bunit\b", unit_title, re.I) or unit_no not in unit_title:
            continue

        rent = money_to_int(str(price_cell.get("data-og-display-price") or ""))
        if rent is None or rent <= 0:
            continue

        apply_anchor = None
        query: dict[str, list[str]] = {}
        for anchor in row.find_all("a", href=True):
            href = str(anchor.get("href") or "")
            parsed = urlparse(urljoin(url, href))
            current_query = {
                key.lower(): value for key, value in parse_qs(parsed.query).items()
            }
            if "unitid" in current_query and "siteid" in current_query:
                apply_anchor = anchor
                query = current_query
                break
        if apply_anchor is None:
            continue
        unit_id = (query.get("unitid") or [""])[0].strip()
        site_id = (query.get("siteid") or [""])[0].strip()
        if not unit_id.isdigit() or not site_id.isdigit():
            continue

        move_in = (query.get("moveindate") or [""])[0].strip()
        lease_term = ""
        lease_match = re.search(
            r"\|\s*(\d{1,2})\s*months?\b",
            price_cell.get_text(" ", strip=True),
            re.IGNORECASE,
        )
        if lease_match:
            lease_term = f"{lease_match.group(1)} months"
        candidates.append(
            {
                "unit_no": unit_no,
                "unit_id": unit_id,
                "rent": rent,
                "move_in": move_in,
                "lease_term": lease_term,
            }
        )

    # Exact duplicates are harmless (some responsive templates duplicate a
    # table); conflicting identity mappings are not.  Drop every conflicted
    # candidate so no physical identity is guessed.
    id_to_numbers: dict[str, set[str]] = {}
    number_to_ids: dict[str, set[str]] = {}
    for candidate in candidates:
        id_to_numbers.setdefault(candidate["unit_id"], set()).add(candidate["unit_no"])
        number_to_ids.setdefault(candidate["unit_no"], set()).add(candidate["unit_id"])

    out: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for candidate in candidates:
        pair = (candidate["unit_id"], candidate["unit_no"])
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        if len(id_to_numbers[candidate["unit_id"]]) != 1:
            continue
        if len(number_to_ids[candidate["unit_no"]]) != 1:
            continue
        rent = candidate["rent"]
        out.append(
            make_unit_dict(
                floor_plan_name=plan_name,
                bed_label=bed_label_from(int(float(beds)), plan_name) if beds else "",
                bedrooms=beds,
                bathrooms=baths,
                sqft=sqft,
                unit_number=candidate["unit_no"],
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                available_units="1",
                availability_date=candidate["move_in"],
                lease_term=candidate["lease_term"],
                source_ids={"spherexx_unit_id": candidate["unit_id"]},
                source_api_url=url,
                extraction_tier=_TIER_ZRS_UNIT_LIST,
            )
        )
    return out


def find_zrs_detail_links(index_html: str, origin: str) -> list[str]:
    """Absolute ``/floorplans/<bed>/<plan>/`` detail URLs from the index."""
    if not index_html:
        return []
    seen: list[str] = []
    for m in _ZRS_DETAIL_RE.finditer(index_html):
        path = m.group(0)
        if not path.endswith("/"):
            path += "/"
        u = origin.rstrip("/") + path
        if u not in seen:
            seen.append(u)
    return seen


async def _zrs_fetch(url: str) -> str:
    from ma_poc.pms.adapters._probe import probe_get

    # Plain direct GET only.  This secondary floor-plan crawl must never spend
    # Web Unlocker budget or inherit a configured residential proxy, and the
    # blocking curl call must not stall the async scraper loop.
    r = await asyncio.to_thread(
        probe_get,
        url,
        timeout=20,
        unlocker=False,
        proxies={},
        retries=1,
    )
    return r.text or "" if r.status_code == 200 else ""


def _decode_presentation_key(value: str) -> bytes | None:
    """Decode one page-published Spherexx key without accepting variants."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or _PRESENTATION_KEY_RE.fullmatch(value) is None
    ):
        return None
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return None
    if len(decoded) > 96 or _PRESENTATION_DECODED_KEY_RE.fullmatch(decoded) is None:
        return None
    return decoded


def _extract_spherexx_embed_key(source_html: str) -> str:
    """Return one unambiguous public Presentation key from operator HTML.

    Only the three live-observed loader forms are accepted: a literal
    ``window.sspcfg`` key, a narrowly back-referenced ``window.btoa`` form,
    or an exact Presentation ``convert.asp`` iframe whose sole query value
    decodes to the same config shape.  JavaScript is never evaluated.
    """
    if not isinstance(source_html, str) or not source_html:
        return ""

    candidates: set[str] = set()
    lower_html = source_html.lower()
    has_exact_loader = (
        "https://presentation.spherexx.app/js/ssploader.js" in lower_html
    )
    if has_exact_loader:
        for match in _PRESENTATION_LITERAL_CONFIG_RE.finditer(source_html):
            candidate = match.group("key")
            if _decode_presentation_key(candidate) is not None:
                candidates.add(candidate)
        for match in _PRESENTATION_BTOA_CONFIG_RE.finditer(source_html):
            decoded = f"fpaw:{match.group('feed')}".encode("ascii")
            candidate = base64.b64encode(decoded).decode("ascii")
            if _decode_presentation_key(candidate) is not None:
                candidates.add(candidate)

    for match in _PRESENTATION_IFRAME_SRC_RE.finditer(source_html):
        raw_src = html_lib.unescape(match.group("src")).strip()
        try:
            parsed = urlparse(raw_src)
            port = parsed.port
        except ValueError:
            continue
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower().rstrip(".")
            != "presentation.spherexx.app"
            or port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != "/convert.asp"
            or parsed.fragment
            or not parsed.query.startswith("key=")
            or "&" in parsed.query
        ):
            continue
        outer_key = unquote(parsed.query[4:])
        if len(outer_key) > 1_024 or _PRESENTATION_KEY_RE.fullmatch(outer_key) is None:
            continue
        try:
            outer = base64.b64decode(outer_key, validate=True)
            if len(outer) > 512:
                continue
            outer_text = outer.decode("ascii")
        except (UnicodeDecodeError, ValueError, TypeError):
            continue
        outer_match = _PRESENTATION_OUTER_CONFIG_RE.fullmatch(outer_text)
        if outer_match is None:
            continue
        candidate = outer_match.group("key")
        if _decode_presentation_key(candidate) is not None:
            candidates.add(candidate)

    return next(iter(candidates)) if len(candidates) == 1 else ""


def _spherexx_operator_inventory_routes(
    source_html: str,
    page_url: str,
) -> list[str]:
    """Find the one exact same-origin Presentation route seen in cohort."""
    try:
        base = urlparse(page_url)
        expected_host = (base.hostname or "").lower().rstrip(".")
    except ValueError:
        return []
    if base.scheme not in {"http", "https"} or not expected_host:
        return []

    found: list[str] = []
    for match in _PRESENTATION_LINK_RE.finditer(source_html or ""):
        href = html_lib.unescape(match.group("href")).strip()
        try:
            resolved = urljoin(page_url, href)
            parsed = urlparse(resolved)
            port = parsed.port
        except ValueError:
            continue
        if (
            parsed.scheme not in {"http", "https"}
            or (parsed.hostname or "").lower().rstrip(".") != expected_host
            or port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path.rstrip("/").lower() != "/interactive-site-map"
            or parsed.query
            or parsed.fragment
        ):
            continue
        canonical = resolved.split("#", 1)[0]
        if canonical not in found:
            found.append(canonical)
        if len(found) == 1:
            break
    return found


async def _spherexx_fetch_operator_page(url: str, expected_host: str) -> str:
    """Plain direct GET for one exact operator-linked inventory document."""
    import httpx

    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or (parsed.hostname or "").lower().rstrip(".") != expected_host
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.rstrip("/").lower() != "/interactive-site-map"
        or parsed.query
        or parsed.fragment
    ):
        return ""

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_PRESENTATION_HTTP_TIMEOUT_SECONDS),
            follow_redirects=False,
            trust_env=False,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "PropAi/1.0",
            },
        ) as client:
            async with client.stream("GET", url) as response:
                if int(response.status_code) != 200 or str(response.url) != url:
                    return ""
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > _PRESENTATION_MAX_MARKETING_BYTES:
                            return ""
                    except ValueError:
                        pass
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > _PRESENTATION_MAX_MARKETING_BYTES:
                        return ""
                    chunks.append(chunk)
                body = b"".join(chunks)
                encoding = response.encoding or "utf-8"
                try:
                    return body.decode(encoding, errors="replace")
                except LookupError:
                    return body.decode("utf-8", errors="replace")
    except (httpx.HTTPError, ValueError):
        return ""


async def _spherexx_api_request(
    method: str,
    path: str,
    authorization: str,
) -> tuple[int, Any]:
    """Call one fixed Presentation endpoint over bounded direct HTTP."""
    import httpx

    allowed = {
        ("POST", "/api/authenticate"),
        ("GET", "/api/community"),
        ("GET", "/api/unit"),
    }
    method = method.upper()
    if (method, path) not in allowed:
        return 0, None
    if method == "POST":
        if not authorization.startswith("Basic "):
            return 0, None
        max_bytes = _PRESENTATION_MAX_AUTH_BYTES
    else:
        if not authorization.startswith("Bearer "):
            return 0, None
        max_bytes = _PRESENTATION_MAX_JSON_BYTES

    url = _PRESENTATION_ORIGIN + path
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_PRESENTATION_HTTP_TIMEOUT_SECONDS),
            follow_redirects=False,
            trust_env=False,
            headers={
                "Accept": "application/json",
                "Authorization": authorization,
                "User-Agent": "PropAi/1.0",
            },
        ) as client:
            async with client.stream(method, url) as response:
                status = int(response.status_code)
                if status != 200 or str(response.url) != url:
                    return status, None
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > max_bytes:
                            return status, None
                    except ValueError:
                        pass
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        return status, None
                    chunks.append(chunk)
                try:
                    return status, json.loads(b"".join(chunks))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return status, None
    except (httpx.HTTPError, ValueError):
        return 0, None


def _normalized_property_tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", html_lib.unescape(value or "").casefold())


def _property_name_signature(value: str) -> set[str]:
    return {
        token
        for token in _normalized_property_tokens(value)
        if token not in _GENERIC_PROPERTY_NAME_TOKENS
    }


def _property_names_match(configured: str, published: str) -> bool:
    configured_signature = _property_name_signature(configured)
    published_signature = _property_name_signature(published)
    if not configured_signature or not published_signature:
        return False
    return configured_signature.issubset(published_signature) or (
        published_signature.issubset(configured_signature)
    )


def _spherexx_marketing_boundary_matches(
    ctx: AdapterContext,
    source_html: str,
) -> bool:
    """Require configured name plus ZIP and local address/city evidence."""
    property_name = str(getattr(ctx, "property_name", "") or "").strip()
    zip_code = str(getattr(ctx, "zip_code", "") or "").strip()
    zip_match = re.match(r"^(\d{5})", zip_code)
    name_signature = _property_name_signature(property_name)
    if not name_signature or zip_match is None:
        return False

    page_text = html_lib.unescape(source_html or "")
    page_tokens = set(_normalized_property_tokens(page_text))
    if not name_signature.issubset(page_tokens):
        return False

    zip5 = zip_match.group(1)
    has_zip = re.search(rf"(?<!\d){re.escape(zip5)}(?:-\d{{4}})?(?!\d)", page_text)
    if has_zip is None:
        return False

    city_tokens = set(
        _normalized_property_tokens(str(getattr(ctx, "city", "") or ""))
    )
    has_city = bool(city_tokens and city_tokens.issubset(page_tokens))

    address_tokens = _normalized_property_tokens(
        str(getattr(ctx, "address", "") or "")
    )
    street_number = next((t for t in address_tokens if t.isdigit()), "")
    street_tokens = {
        token
        for token in address_tokens
        if not token.isdigit() and token not in _GENERIC_STREET_TOKENS
    }
    has_address = bool(
        street_number
        and street_number in page_tokens
        and street_tokens
        and street_tokens.intersection(page_tokens)
    )
    return has_city or has_address


def _strict_spherexx_presentation_units(
    body: Any,
    url: str,
) -> list[dict[str, Any]]:
    """Admit only complete, unique physical rows from direct Presentation."""
    if (
        not isinstance(body, list)
        or not body
        or len(body) > _PRESENTATION_MAX_UNITS
        or not _is_spherexx_unit_response(body)
    ):
        return []

    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for raw in body:
        if not isinstance(raw, dict):
            return []
        unit_id = str(raw.get("ID") or "").strip()
        unit_name = re.sub(r"\s+", " ", str(raw.get("Name") or "")).strip()
        number = re.sub(r"\s+", " ", str(raw.get("Number") or "")).strip()
        plan_id = str(raw.get("FloorplanID") or "").strip()
        plan_name = re.sub(
            r"\s+", " ", str(raw.get("FloorplanName") or "")
        ).strip()
        if (
            not unit_id.isdigit()
            or int(unit_id) <= 0
            or not unit_name
            or len(unit_name) > 40
            or re.search(r"\d", unit_name) is None
            or re.search(r"\$|\b(?:bed|bath|rent|price|sq\.?\s*ft)\b", unit_name, re.I)
            or not number
            or len(number) > 40
            or re.search(r"\d", number) is None
            or not plan_id.isdigit()
            or int(plan_id) <= 0
            or not plan_name
            or len(plan_name) > 120
        ):
            return []

        sqft_raw = raw.get("Sqft")
        bed_raw = raw.get("Bed")
        bath_raw = raw.get("Bath")
        if (
            isinstance(sqft_raw, bool)
            or not isinstance(sqft_raw, (int, float))
            or not 150 <= float(sqft_raw) <= 10_000
            or isinstance(bed_raw, bool)
            or not isinstance(bed_raw, (int, float))
            or not 0 <= float(bed_raw) <= 20
            or isinstance(bath_raw, bool)
            or not isinstance(bath_raw, (int, float))
            or not 0 < float(bath_raw) <= 20
        ):
            return []

        rent = money_to_int(str(raw.get("PriceMin") or raw.get("Price") or ""))
        available_date = _zrs_explicit_date(raw.get("AvailableDate"))
        if (
            rent is None
            or rent <= 0
            or not rent_in_sanity_range(rent)
            or not available_date
            or unit_id in seen_ids
            or unit_name.casefold() in seen_names
        ):
            return []
        seen_ids.add(unit_id)
        seen_names.add(unit_name.casefold())

        parsed = _parse_spherexx_unit(raw, url)
        if parsed is None:
            return []
        parsed["availability_date"] = available_date
        parsed["extraction_tier"] = _TIER_PRESENTATION_DIRECT
        out.append(parsed)
    return out


async def _recover_spherexx_presentation_units(
    embed_key: str,
    marketing_html: str,
    ctx: AdapterContext,
) -> tuple[list[dict[str, Any]], str]:
    """Replay the public Presentation API after strict property scoping."""
    if _decode_presentation_key(embed_key) is None:
        return [], "invalid_embed_key"
    if not _spherexx_marketing_boundary_matches(ctx, marketing_html):
        return [], "marketing_property_boundary_mismatch"

    status, auth_body = await _spherexx_api_request(
        "POST",
        "/api/authenticate",
        f"Basic {embed_key}",
    )
    if status != 200 or not isinstance(auth_body, list) or len(auth_body) != 1:
        return [], "authentication_shape_rejected"
    token = auth_body[0]
    if (
        not isinstance(token, str)
        or not 32 <= len(token) <= 8_192
        or re.fullmatch(
            r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
            token,
        )
        is None
    ):
        return [], "authentication_token_rejected"

    bearer = f"Bearer {token}"
    status, community_body = await _spherexx_api_request(
        "GET",
        "/api/community",
        bearer,
    )
    if (
        status != 200
        or not isinstance(community_body, list)
        or len(community_body) != 1
        or not isinstance(community_body[0], dict)
    ):
        return [], "community_shape_rejected"
    community_name = str(community_body[0].get("Name") or "").strip()
    if not _property_names_match(
        str(getattr(ctx, "property_name", "") or ""),
        community_name,
    ):
        return [], "community_property_boundary_mismatch"

    status, unit_body = await _spherexx_api_request(
        "GET",
        "/api/unit",
        bearer,
    )
    if status != 200:
        return [], "unit_request_failed"
    units = _strict_spherexx_presentation_units(
        unit_body,
        _PRESENTATION_ORIGIN + "/api/unit",
    )
    if not units:
        return [], "unit_shape_rejected"
    return units, ""


# Field names that uniquely identify a Spherexx /api/unit response. The
# array elements carry these mixed-case keys (different from any other
# adapter's API shape, so the body-shape check is unambiguous).
_SPHEREXX_UNIT_KEYS = frozenset({
    "ID", "Name", "Building", "Sqft", "Bed", "Bath", "Price",
    "FloorplanID", "FloorplanName", "AvailableDate",
})


def _is_spherexx_unit_response(body: Any) -> bool:
    """True when *body* is a Spherexx /api/unit array.

    Shape: list[ dict with at least {ID, Name, Sqft, Bed, Bath, Price,
    FloorplanID, FloorplanName} ]. Empty arrays are NOT a match — they
    don't have enough signal to commit to this adapter.
    """
    if not isinstance(body, list):
        return False
    if not body:
        return False
    first = body[0]
    if not isinstance(first, dict):
        return False
    # Require ≥ 6 of the 9 signature keys present. The full set rarely all
    # appear (some properties don't have Building), so a partial match is
    # the right gate.
    matched = sum(1 for k in _SPHEREXX_UNIT_KEYS if k in first)
    return matched >= 6


def _is_spherexx_floorplan_response(body: Any) -> bool:
    """True when *body* is a Spherexx /api/floorplan array."""
    if not isinstance(body, list) or not body:
        return False
    first = body[0]
    if not isinstance(first, dict):
        return False
    # Floor-plan signature: {ID, Name, Bed, Bath, MinSqFt or MaxSqFt}.
    # Distinct from /api/unit by the MinSqFt/MaxSqFt keys (units use Sqft).
    return (
        "ID" in first
        and "Name" in first
        and ("MinSqFt" in first or "MaxSqFt" in first)
    )


def _parse_spherexx_unit(u: dict[str, Any], url: str) -> dict[str, str] | None:
    """Parse one Spherexx unit dict → our standard unit-dict shape.

    Returns None when the unit lacks both Price and Sqft (truly empty
    placeholder — Spherexx sometimes returns these for unbuilt buildings).
    """
    # Price — prefer PriceMin (lowest avail rent for the unit); falls
    # back to Price (single value) when range fields are absent.
    price_min_raw = u.get("PriceMin") or u.get("Price")
    price_max_raw = u.get("PriceMax") or u.get("Price")
    price_min: int | None = None
    price_max: int | None = None
    if isinstance(price_min_raw, (int, float)) and price_min_raw > 0:
        price_min = int(price_min_raw)
    if isinstance(price_max_raw, (int, float)) and price_max_raw > 0:
        price_max = int(price_max_raw)
    if price_min is None and price_max is None:
        # Try string forms ("$1,592" etc.) — Spherexx normally emits
        # numbers but be defensive.
        price_min = money_to_int(str(u.get("PriceMin") or u.get("Price") or ""))
        price_max = money_to_int(str(u.get("PriceMax") or u.get("Price") or ""))

    # Sqft
    sqft_raw = u.get("Sqft")
    sqft = str(int(sqft_raw)) if isinstance(sqft_raw, (int, float)) and sqft_raw > 0 else ""

    # Bed / Bath — Spherexx emits floats (1.0, 2.5).
    bed_raw = u.get("Bed")
    bath_raw = u.get("Bath")
    beds = int(bed_raw) if isinstance(bed_raw, (int, float)) and bed_raw >= 0 else None
    baths_str = ""
    if isinstance(bath_raw, (int, float)) and bath_raw > 0:
        # 1.0 → "1"; 2.5 → "2.5"
        baths_str = f"{bath_raw:.1f}".rstrip("0").rstrip(".")

    # Quality gate — skip rows with no rent AND no sqft. These are usually
    # placeholders for buildings that aren't on the market yet.
    if price_min is None and price_max is None and not sqft:
        return None

    fp_name = str(u.get("FloorplanName") or "").strip()
    name = str(u.get("Name") or "").strip()
    building = str(u.get("Building") or "").strip()
    floor = str(u.get("Floor") or "").strip()

    avail_raw = u.get("AvailableDate") or u.get("availableDate") or ""
    avail = str(avail_raw).split("T")[0] if avail_raw else ""

    if price_min and price_max and price_min != price_max:
        rent_range = f"${price_min:,} - ${price_max:,}"
    elif price_min:
        rent_range = f"${price_min:,}"
    elif price_max:
        rent_range = f"${price_max:,}"
    else:
        rent_range = ""

    return make_unit_dict(
        floor_plan_name=fp_name,
        bed_label=bed_label_from(beds, fp_name),
        bedrooms=str(beds) if beds is not None else "",
        bathrooms=baths_str,
        sqft=sqft,
        unit_number=name,
        floor=floor,
        building=building,
        rent_range=rent_range,
        rent_low=price_min,
        rent_high=price_max or price_min,
        availability_status="AVAILABLE",
        available_units="1",
        availability_date=avail,
        source_ids={
            k: v
            for k, v in {
                "spherexx_unit_id": u.get("ID"),
                "spherexx_floorplan_id": u.get("FloorplanID"),
            }.items()
            if v
        },
        source_api_url=url,
        extraction_tier=_TIER_BASE,
    )


def parse_razz_models_dom(html: str, url: str) -> list[dict[str, str]]:
    """Parse a Razz/myrazz ``/models`` rendered DOM → standard unit dicts.

    The Razz Vue SPA renders each unit inside a ``wrap-model-item
    model-list`` block whose visible text follows a stable labeled
    layout, e.g.::

        1X1  Unit 627 - 1 Bed | 1 Bath  Base Rent $925
        Sq. Ft. 700  Available May 19  Term 12 Months  Deposit -

    We anchor on that label text (not the generated Vue ``data-v-*`` /
    unclassed date <div>) so markup churn doesn't silently break it.
    ``available`` ("May 19" / "Now") is emitted RAW — schema_v2.
    _format_date does the canonical normalization downstream.
    """
    try:
        from bs4 import BeautifulSoup  # lazy: avoid import cost off-path
    except ImportError:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return []

    items = soup.find_all(class_=re.compile(r"wrap-model-item", re.IGNORECASE))
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for it in items:
        txt = re.sub(r"\s+", " ", it.get_text(" ", strip=True))
        m = _RAZZ_UNIT_RE.search(txt)
        if not m:
            continue
        unit_no = m.group(1).strip()
        if unit_no in seen:
            continue
        seen.add(unit_no)
        beds = 0 if m.group(2).lower() == "studio" else int(m.group(2))
        bath_f = float(m.group(3))
        baths_str = f"{bath_f:.1f}".rstrip("0").rstrip(".")

        rm = _RAZZ_RENT_RE.search(txt)
        rent = int(rm.group(1).replace(",", "")) if rm else None
        sm = _RAZZ_SQFT_RE.search(txt)
        sqft = sm.group(1).replace(",", "") if sm else ""
        am = _RAZZ_AVAIL_RE.search(txt)
        avail = am.group(1).strip() if am else ""

        rent_range = f"${rent:,}" if rent else ""
        out.append(
            make_unit_dict(
                floor_plan_name="",
                bed_label=bed_label_from(beds, ""),
                bedrooms=str(beds),
                bathrooms=baths_str,
                sqft=sqft,
                unit_number=unit_no,
                rent_range=rent_range,
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                available_units="1",
                availability_date=avail,
                source_api_url=url,
                extraction_tier=_TIER_RAZZ,
            )
        )
    return out


def parse_spherexx_units(body: list[dict[str, Any]], url: str) -> list[dict[str, str]]:
    """Parse a Spherexx /api/unit array → list of standard unit dicts."""
    out: list[dict[str, str]] = []
    for u in body:
        if not isinstance(u, dict):
            continue
        rec = _parse_spherexx_unit(u, url)
        if rec is not None:
            out.append(rec)
    return out


class SpherexxAdapter:
    """Spherexx Presentation Software adapter.

    Detection: site embeds ``presentation.spherexx.app`` or has
    ``window.sspcfg`` (the loader config global). Adapter parses
    ``/api/unit`` JSON arrays captured during page load.
    """

    pms_name: str = "spherexx"
    _fingerprints: list[str] = [
        "presentation.spherexx.app",
        "spherexx.app",
        "spherexx.com",
        "sspcfg",
        "ssploader.js",
        "myrazz.com",
        "images.myrazz.com",
        "wrap-models-list",
        "sxxweb",
        "spherexx.com/copyright",
        "/Content/js/core/floorplans.js",
        "/Content/js/fci/",
        "/Content/js/fci/floorplans.js",
        "/Content/js/zrscustom/floorplans.js",
        "/ajax/availabilityv2/",
    ]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from Spherexx /api/unit responses captured at fetch time."""
        result = AdapterResult(tier_used=_TIER_BASE)
        all_units: list[dict[str, str]] = []

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        spherexx_unit_resps: list[dict[str, Any]] = []
        spherexx_any_resps: list[dict[str, Any]] = []

        for resp in api_responses:
            url = resp.get("url", "")
            body = resp.get("body")
            url_lower = url.lower()
            if "spherexx" not in url_lower:
                continue
            spherexx_any_resps.append(resp)
            if not _is_spherexx_unit_response(body):
                continue
            # _is_spherexx_unit_response returned True → body is a non-empty
            # list[dict[str, Any]]. Cast for the type checker.
            assert isinstance(body, list)
            spherexx_unit_resps.append(resp)
            try:
                units = parse_spherexx_units(body, url)
            except Exception as exc:
                result.errors.append(f"spherexx-parse-error: {exc}")
                continue
            if units:
                all_units.extend(units)
                result.api_responses.append(resp)

        if all_units:
            result.units = all_units
            result.winning_url = (
                result.api_responses[0].get("url")
                if result.api_responses else None
            )
            result.confidence = min(0.95, 0.7 + 0.04 * len(all_units))
            result.tier_used = _TIER_BASE
            return result

        # Razz/myrazz /models DOM fallback. The "Happily Made by Razz" Vue
        # SPA fires no spherexx API XHR — units exist only in the post-
        # hydration DOM. Pull rendered HTML the same way appfolio's SSR
        # path does (fetch_result.body first to avoid a re-fetch, then
        # live page.content()). Guarded by Razz markers so spherexx-API /
        # ZRS sites are never touched (cannot regress them).
        page_html: str | None = None
        _fr = getattr(ctx, "fetch_result", None)
        if _fr is not None:
            _body = getattr(_fr, "body", None)
            if isinstance(_body, bytes):
                try:
                    page_html = _body.decode("utf-8", errors="replace")
                except Exception:
                    page_html = None
            elif isinstance(_body, str):
                page_html = _body
        if page_html is None and page is not None:
            try:
                page_html = await page.content()
            except Exception:
                page_html = None

        # Current Spherexx/ZRS template: the fetched detail page itself has a
        # strict per-apartment roster.  Parse it before any secondary crawl.
        page_url = getattr(ctx, "base_url", "") or ""
        if _fr is not None:
            page_url = str(getattr(_fr, "final_url", "") or page_url)

        # Presentation direct replay. Hash-mode and inline Spherexx widgets can
        # expose the public config on the marketing page without hydrating the
        # iframe before the response capture closes. Recover only from the
        # three observed, tightly parsed config forms and require both the
        # operator page and the singleton /api/community row to match the
        # configured property before requesting or emitting unit inventory.
        presentation_html = page_html or ""
        embed_key = _extract_spherexx_embed_key(presentation_html)
        if not embed_key and presentation_html and page_url:
            try:
                expected_host = (urlparse(page_url).hostname or "").lower().rstrip(".")
            except ValueError:
                expected_host = ""
            if expected_host:
                for route in _spherexx_operator_inventory_routes(
                    presentation_html,
                    page_url,
                )[:1]:
                    route_html = await _spherexx_fetch_operator_page(
                        route,
                        expected_host,
                    )
                    route_key = _extract_spherexx_embed_key(route_html)
                    if route_key:
                        embed_key = route_key
                        presentation_html = presentation_html + "\n" + route_html
                        break
        if embed_key:
            try:
                presentation_units, presentation_error = (
                    await _recover_spherexx_presentation_units(
                        embed_key,
                        presentation_html,
                        ctx,
                    )
                )
            except Exception as exc:
                presentation_units = []
                presentation_error = f"unexpected_{type(exc).__name__}"
            if presentation_units:
                from ma_poc.extraction.post_process import post_process

                pp = post_process(
                    presentation_units,
                    property_id=getattr(ctx, "property_id", None),
                )
                if pp.n_unit_level > 0:
                    result.units = pp.units
                    result.plan_summaries = pp.plan_summaries
                    result.winning_url = _PRESENTATION_ORIGIN + "/api/unit"
                    result.confidence = min(
                        0.97,
                        0.78 + 0.03 * pp.n_unit_level,
                    )
                    result.tier_used = _TIER_PRESENTATION_DIRECT
                    result.api_responses.append(
                        {
                            "url": result.winning_url,
                            "status": 200,
                            "body": "<spherexx-presentation-unit-json>",
                            "via": "spherexx_presentation_direct",
                        }
                    )
                    return result
            if presentation_error:
                result.errors.append(
                    "SPHEREXX_PRESENTATION_DIRECT_SKIPPED: "
                    + presentation_error
                )

        if page_html:
            try:
                zrs_inline_units = parse_zrs_unit_list(page_html, page_url)
                if not zrs_inline_units:
                    zrs_inline_units = parse_zrs_floorplan_detail(page_html, page_url)
            except Exception as exc:
                zrs_inline_units = []
                result.errors.append(f"zrs-unit-list-parse-error: {exc}")
            if zrs_inline_units:
                from ma_poc.extraction.post_process import post_process

                pp = post_process(
                    zrs_inline_units,
                    property_id=getattr(ctx, "property_id", None),
                )
                if pp.n_unit_level > 0:
                    result.units = pp.units
                    result.plan_summaries = pp.plan_summaries
                    result.winning_url = page_url or None
                    result.confidence = min(0.94, 0.74 + 0.04 * pp.n_unit_level)
                    result.tier_used = str(
                        pp.units[0].get("extraction_tier") or _TIER_ZRS
                    )
                    result.api_responses.append(
                        {
                            "url": page_url,
                            "status": 200,
                            "body": "<zrs-unit-roster-dom>",
                            "via": "spherexx_zrs_unit_roster_dom",
                        }
                    )
                    return result

        # Current ZRS templates expose their physical-unit roster at a public
        # same-origin JSON route.  The ordinary fetch often lands on the home
        # page, so the browser never executes the floor-plan-page XHR and the
        # response is absent from ``ctx._api_responses``.  Replay the two exact
        # routes used by Spherexx's own ``floorplans.js`` and apply the strict
        # UnitID/ApartmentNumber/plan-ID admission gate above.
        parsed_page_url = urlparse(page_url)
        base = (
            f"{parsed_page_url.scheme}://{parsed_page_url.netloc}"
            if parsed_page_url.scheme and parsed_page_url.netloc
            else ""
        )
        zrs_index_html = ""
        page_html_lower = (page_html or "").lower()
        has_zrs_availability_template = any(
            marker in page_html_lower
            for marker in (
                "/content/js/core/floorplans.js",
                "/content/js/fci/",
                "/content/js/zrscustom/floorplans.js",
                "/ajax/availabilityv2/",
            )
        )
        if base and has_zrs_availability_template:
            availability_url = base + "/ajax/availabilityv2/"
            try:
                availability_text = await _zrs_fetch(availability_url)
                availability_body = (
                    json.loads(availability_text) if availability_text else None
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                availability_body = None
            except Exception as exc:
                availability_body = None
                result.errors.append(f"zrs-availability-v2-fetch-error: {exc}")
            if isinstance(availability_body, list) and availability_body:
                plans_url = base + "/ajax/api/plansandpricing/"
                try:
                    plans_text = await _zrs_fetch(plans_url)
                    plans_body = json.loads(plans_text) if plans_text else None
                except (json.JSONDecodeError, TypeError, ValueError):
                    plans_body = None
                except Exception as exc:
                    plans_body = None
                    result.errors.append(f"zrs-plan-catalog-fetch-error: {exc}")

                catalog_html = page_html or ""
                if "data-fpid" not in catalog_html.lower():
                    try:
                        zrs_index_html = await _zrs_fetch(base + "/floorplans/")
                    except Exception:
                        zrs_index_html = ""
                    catalog_html = zrs_index_html or catalog_html
                try:
                    availability_units = parse_zrs_availability_v2(
                        availability_body,
                        plans_body,
                        catalog_html,
                        availability_url,
                    )
                except Exception as exc:
                    availability_units = []
                    result.errors.append(f"zrs-availability-v2-parse-error: {exc}")
                if availability_units:
                    from ma_poc.extraction.post_process import post_process

                    pp = post_process(
                        availability_units,
                        property_id=getattr(ctx, "property_id", None),
                    )
                    if pp.n_unit_level > 0:
                        result.units = pp.units
                        result.plan_summaries = pp.plan_summaries
                        result.winning_url = availability_url
                        result.confidence = min(
                            0.95, 0.76 + 0.03 * pp.n_unit_level
                        )
                        result.tier_used = _TIER_ZRS_AVAILABILITY_V2
                        result.api_responses.append(
                            {
                                "url": availability_url,
                                "status": 200,
                                "body": "<zrs-availability-v2-json>",
                                "via": "spherexx_zrs_availability_v2",
                            }
                        )
                        return result
        if page_html and (
            "wrap-model-item" in page_html
            or "myrazz" in page_html.lower()
            or "happily made by razz" in page_html.lower()
        ):
            try:
                razz_units = parse_razz_models_dom(
                    page_html, getattr(ctx, "base_url", "") or ""
                )
            except Exception as exc:
                razz_units = []
                result.errors.append(f"razz-parse-error: {exc}")
            if razz_units:
                result.units = razz_units
                result.winning_url = getattr(ctx, "base_url", "") or None
                result.confidence = min(0.92, 0.7 + 0.04 * len(razz_units))
                result.tier_used = _TIER_RAZZ
                result.api_responses.append(
                    {
                        "url": (getattr(ctx, "base_url", "") or "") + "#/models",
                        "status": 200,
                        "body": "<razz-models-dom>",
                        "via": "razz_models_dom",
                    }
                )
                return result

        # ZRS server-rendered fallback: chathamsquare/mirabella-class
        # spherexx sites render units in an HTML table on
        # /floorplans/<bed>/<plan>/ detail pages (no API iframe). The
        # API path above captured nothing — crawl the detail pages.
        if base:
            try:
                idx = zrs_index_html or await _zrs_fetch(base + "/floorplans/")
            except Exception:
                idx = ""
            links = find_zrs_detail_links(idx, base)[:30]
            zrs_units: list[dict[str, str]] = []
            for du in links:
                try:
                    dh = await _zrs_fetch(du)
                except Exception:
                    continue
                current_units = parse_zrs_unit_list(dh, du)
                if current_units:
                    zrs_units.extend(current_units)
                else:
                    zrs_units.extend(parse_zrs_floorplan_detail(dh, du))
            if zrs_units:
                from ma_poc.extraction.post_process import post_process

                pp = post_process(
                    zrs_units,
                    property_id=getattr(ctx, "property_id", None),
                )
                if pp.n_unit_level > 0:
                    result.units = pp.units
                    result.plan_summaries = pp.plan_summaries
                    result.winning_url = base + "/floorplans/"
                    result.confidence = min(0.92, 0.7 + 0.04 * pp.n_unit_level)
                    result.tier_used = (
                        _TIER_ZRS_UNIT_LIST
                        if any(
                            str(unit.get("extraction_tier") or "")
                            == _TIER_ZRS_UNIT_LIST
                            for unit in pp.units
                        )
                        else _TIER_ZRS
                    )
                    result.api_responses.append(
                        {
                            "url": base + "/floorplans/",
                            "status": 200,
                            "body": "<zrs-floorplan-detail>",
                            "via": "spherexx_zrs_probe",
                        }
                    )
                    return result

        # Failure-mode classification.
        result.confidence = 0.0
        if not spherexx_any_resps:
            result.tier_used = _TIER_NO_RESPONSE
            result.errors.append(
                "SPHEREXX_NO_RESPONSE: no responses from presentation.spherexx.app "
                "captured during page load — the iframe may not have hydrated "
                "before Playwright captured (try increasing settle time) or the "
                "site doesn't actually embed a Spherexx widget"
            )
        elif not spherexx_unit_resps:
            result.tier_used = _TIER_SHAPE_REJECTED
            seen_paths = sorted({
                r.get("url", "").split("?")[0].rsplit("/", 1)[-1]
                for r in spherexx_any_resps
            })
            result.errors.append(
                f"SPHEREXX_SHAPE_REJECTED: {len(spherexx_any_resps)} responses "
                f"captured from spherexx.app (endpoints: {seen_paths}), but none "
                f"matched the /api/unit array shape. Expected list of dicts "
                f"with keys ID/Name/Sqft/Bed/Bath/Price/FloorplanID/FloorplanName"
            )
        else:
            result.tier_used = _TIER_PARSE_FAILED
            result.errors.append(
                f"SPHEREXX_PARSE_FAILED: {len(spherexx_unit_resps)} unit "
                "responses matched the shape but parsing produced 0 records — "
                "field-name drift on Spherexx side"
            )

        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    def matches_response_body(self, body: Any) -> bool:
        """Body-shape check used by ``detector.confirm_detection``."""
        return _is_spherexx_unit_response(body)
