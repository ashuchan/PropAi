"""
Entrata adapter.

Research log
------------
Web sources consulted:
  - https://www.entrata.com/ — Entrata prospect portal documentation (accessed 2026-04-17)
  - https://www.entrata.com/resources — Platform overview confirming widget-based architecture
Real payloads inspected (from data/runs/*/raw_api/):
  - 257356 (Hackney House) — /Apartments/module/widgets/ returning flat list of floorplan dicts
    with keys: id, floorplan-name, no_of_bedroom, no_of_bathroom, square_footage, min_rent,
    max_rent, rent, floorplan_url, fee_calculator, floorplan_image
  - 252511 (Intro Cleveland) — /Apartments/module/widgets/ returning widget_data envelope
    with availability widget (min_move_in_date, max_move_in_date) and ppConfig with property_id
Key findings:
  - API endpoint: /Apartments/module/widgets/ — returns either flat floorplan list or
    widget_data envelope depending on which widget is loaded
  - Response envelope: direct list[] for floorplans, or widget_data.content.floor_plans.floor_plans[]
  - Unit ID field: 'id' (floorplan ID, not unit-level)
  - Rent field(s): min_rent/max_rent as formatted strings ("$1,565"), rent as display string
  - Known gotchas: availability widget has UI config only (no units); noise widgets (directions,
    gallery, amenities, contact, reviews) must be filtered; ppConfig contains property_id but no
    unit data; fee_calculator URL contains property[id] and floorplan[id] params
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

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

# Bug 9 (2026-05-09 deep-dive): when the entry page fired no Entrata XHRs
# (typical of marketing-redirect homepages — observed on 1701arch.com /
# livethearch.com), probe these well-known Entrata paths directly through
# the live browser session. ``page.evaluate`` runs the fetch under the
# same origin so cookies/session/CSRF are preserved without us re-creating
# the auth flow.
_ENTRATA_PROBES: tuple[str, ...] = (
    "/Apartments/module/floor_plans/",
    "/Apartments/module/availability_pricing/",
    "/Apartments/module/property_info/",
    "/api/floorplans",
    "/api/availability",
)

# 2026-05-19: Entrata "Prospect Portal" *website* recovery. These are
# Entrata-hosted marketing sites (host ``*.prospectportal.com`` or a vanity
# domain that loads ``commoncf.entrata.com/.../prospect_portal/*`` scripts).
# Their floor-plan grid is server-side rendered into the DOM at
# ``/{city}/{slug}/conventional/`` — no unit XHR fires, so the captured-API
# and probe paths above both come back empty and the adapter used to dead-end
# at ``no_units``. This was the single largest recoverable failure cluster in
# the 2026-05-19 deep probe (~43% of failed cases, 100% recoverable). The
# selectors below were verified against live Prospect Portal pages on both a
# canonical ``*.prospectportal.com`` subdomain and a vanity domain.
_PROSPECT_PORTAL_DOM_JS = r"""
() => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const cards = Array.from(document.querySelectorAll('.fp-card'));
  return cards.map((c) => {
    const liDeposit = Array.from(c.querySelectorAll('.fp-details li'))
      .map((e) => T(e))
      .find((t) => /deposit/i.test(t)) || '';
    const special =
      T(c.querySelector('.fp-special-main-text')) ||
      T(c.querySelector('.fp-special-text')) ||
      '';
    return {
      name: T(c.querySelector('.fp-title')),
      bedbath: T(c.querySelector('.dynamic-text-before')),
      sqft: T(c.querySelector('.dynamic-text-after')),
      fee: T(c.querySelector('.fee-transparency-text')),
      lease: T(c.querySelector('.lease-term-name')),
      deposit: liDeposit,
      availability: T(c.querySelector('.availability')),
      special: special,
    };
  });
}
"""

# "1 Bed / 1 Bath", "Studio / 1 Bath", "2 Bed / 2.5 Bath"
_BED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*bed", re.IGNORECASE)
_BATH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*bath", re.IGNORECASE)
_SQFT_RE = re.compile(r"([\d,]+)\+?\s*sq", re.IGNORECASE)
_MONEY_RE = re.compile(r"\$[\s]?[\d,]+(?:\.\d{2})?")
# "2 Units Available", "Only 1 Unit Available!", "1 Unit Available"
_UNIT_COUNT_RE = re.compile(r"(\d+)\s*units?\s*available", re.IGNORECASE)


def parse_prospect_portal_cards(
    cards: list[dict[str, str]], url: str
) -> list[dict[str, str]]:
    """Parse SSR Prospect Portal ``.fp-card`` rows into standard unit dicts.

    Plan-level (one row per floor plan, no per-apartment unit number) — these
    pages render a plan grid, not a unit roster. ``available_units`` carries
    the per-plan count when the page states one ("N Units Available").
    """
    units: list[dict[str, str]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        name = (card.get("name") or "").strip()
        bedbath = card.get("bedbath") or ""
        if not name and not bedbath:
            continue

        bed_m = _BED_RE.search(bedbath)
        bath_m = _BATH_RE.search(bedbath)
        if bed_m:
            beds: int | None = int(float(bed_m.group(1)))
        elif re.search(r"studio", bedbath, re.IGNORECASE):
            beds = 0
        else:
            beds = None
        baths = bath_m.group(1) if bath_m else ""

        sqft_m = _SQFT_RE.search(card.get("sqft") or "")
        sqft = sqft_m.group(1).replace(",", "") if sqft_m else ""

        fee = card.get("fee") or ""
        # Fee-transparency guard — see _pp_base_rent_scope. This entry point
        # is handed an ALREADY-FLATTENED cell string by a Playwright
        # page.evaluate(), so only the text rule can apply here; there is no
        # element to anchor on. No-op unless the "Base Rent:" label is present.
        money = _MONEY_RE.findall(_pp_base_rent_scope(fee) or fee)
        rent_lo = money_to_int(money[0]) if money else None
        rent_hi = money_to_int(money[-1]) if money else None
        rent_range = format_rent_range(rent_lo, rent_hi)

        avail = card.get("availability") or ""
        count_m = _UNIT_COUNT_RE.search(avail)
        available_units = count_m.group(1) if count_m else ""
        availability_date = ""
        status = "AVAILABLE"
        if re.search(r"waitlist", avail, re.IGNORECASE):
            status = "UNAVAILABLE"
            available_units = available_units or "0"
        else:
            date_m = re.search(r"available\s+(.+)$", avail, re.IGNORECASE)
            if date_m and not count_m:
                availability_date = date_m.group(1).strip()

        units.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths),
                sqft=sqft,
                unit_number="",
                rent_range=rent_range,
                rent_low=rent_lo,
                rent_high=rent_hi,
                deposit=(card.get("deposit") or "").strip(),
                concession=(card.get("special") or "").strip(),
                availability_status=status,
                available_units=available_units,
                availability_date=availability_date,
                lease_term=(card.get("lease") or "").strip(),
                source_api_url=url,
                extraction_tier="TIER_3_DOM_ENTRATA_PP",
            )
        )
    return units

# Entrata widget types that contain real floor plan / availability data.
_PROPERTY_WIDGET_TYPES = {"floor_plans", "availability"}

# Entrata widget types that are known to NOT contain unit data.
_NOISE_WIDGET_TYPES = {
    "custom",
    "directions",
    "events",
    "specials",
    "resident_login",
    "gallery",
    "contact",
    "reviews",
    "social",
    "blog",
    "amenities",
}

# Regex to extract property_id from fee_calculator URLs.
_PROPERTY_ID_RE = re.compile(r"property\[id\]=(\d+)")


def _filter_widget_response(body: dict[str, Any]) -> dict[str, Any] | None:
    """Filter Entrata widget responses. Returns body if it has unit data, else None."""
    widget_name = body.get("widget_name", "")
    if widget_name in _NOISE_WIDGET_TYPES:
        return None
    widget_data = body.get("widget_data", {})
    content = widget_data.get("content", {})
    if isinstance(content, dict):
        fp_section = content.get("floor_plans", {})
        if isinstance(fp_section, dict):
            fp_list = fp_section.get("floor_plans", [])
            if isinstance(fp_list, list) and fp_list:
                return body
        avail_section = content.get("availability", {})
        if isinstance(avail_section, dict):
            avail_units = avail_section.get("units", [])
            if isinstance(avail_units, list) and avail_units:
                return body
    if widget_name not in _PROPERTY_WIDGET_TYPES:
        return None
    return body


def parse_entrata_floorplans(items: list[dict[str, Any]], url: str) -> list[dict[str, str]]:
    """Parse a flat list of Entrata floorplan dicts into standard unit dicts."""
    units: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("floorplan-name") or item.get("floorplan_name") or "")
        beds_raw = item.get("no_of_bedroom")
        baths_raw = item.get("no_of_bathroom")
        beds = int(beds_raw) if beds_raw is not None else None
        baths = int(baths_raw) if baths_raw is not None else None
        sqft = str(item.get("square_footage") or "")

        rent_lo = money_to_int(str(item.get("min_rent") or ""))
        rent_hi = money_to_int(str(item.get("max_rent") or ""))
        rent_range = format_rent_range(rent_lo, rent_hi)
        # 2026-05-19: Entrata floorplan/availability items carry a move-in
        # date the adapter previously dropped (fleet-wide 0% available_date
        # on TIER_1_API_ENTRATA). Alias-tolerant + additive: empty when
        # absent, so no existing output changes. schema_v2._format_date
        # normalizes the value downstream.
        avail_dt = next(
            (
                str(item[k])
                for k in (
                    "available_date",
                    "availableDate",
                    "availability_date",
                    "move_in_date",
                    "min_move_in_date",
                    "date_available",
                    "available_on",
                    "first_available_date",
                )
                if item.get(k)
            ),
            "",
        )

        units.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths) if baths is not None else "",
                sqft=sqft,
                unit_number=str(item.get("id") or ""),
                rent_range=rent_range,
                availability_status="AVAILABLE",
                availability_date=avail_dt,
                available_units="1",
                source_api_url=url,
                extraction_tier="TIER_1_API_ENTRATA",
            )
        )
    return units


def parse_entrata_widget_envelope(
    body: dict[str, Any],
    url: str,
) -> list[dict[str, str]]:
    """Extract units from the widget_data.content.floor_plans envelope."""
    widget_data = body.get("widget_data", {})
    content = widget_data.get("content", {})
    fp_section = content.get("floor_plans", {})
    if isinstance(fp_section, dict):
        fp_list = fp_section.get("floor_plans", [])
        if isinstance(fp_list, list) and fp_list:
            return parse_entrata_floorplans(fp_list, url)
    return []


_AVAIL_UNITS_RE = re.compile(r'"available_units"\s*:\s*(\[)')
_FP_DETAIL_RE = re.compile(r"/floorplan/[a-z0-9][a-z0-9-]*/?", re.IGNORECASE)
_SLUG_BB_RE = re.compile(r"(\d+)\s*br[\s_-]*(\d+(?:\.\d+)?)\s*ba", re.IGNORECASE)


def _iso_date(s: str) -> str:
    """``MM/DD/YYYY`` → ``YYYY-MM-DD``; passthrough/'' otherwise."""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", str(s or ""))
    if not m:
        return ""
    mo, d, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _bracket_json(html: str, start: int) -> str | None:
    """Return the balanced ``[...]`` JSON array starting at *start*."""
    depth = 0
    for j in range(start, len(html)):
        c = html[j]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return html[start : j + 1]
    return None


def parse_entrata_available_units(
    html: str, url: str
) -> list[dict[str, str]]:
    """Parse Entrata-WP per-floorplan-detail embedded ``available_units``.

    Entrata marketing sites (WordPress + prospectportal apply links)
    server-render an HTML-entity-encoded JSON blob on
    ``/floorplan/<slug>/`` detail pages:
      "available_units":[{"id","name","available_on","price",
                          "deposit","apply_url"}, ...]
    Each entry is a real unit (stable ``id`` + unit ``name`` + date +
    rent) → deterministic Tier-1, no render needed. Beds/baths come from
    the floorplan slug (``1br-1ba-pennsylvania``).
    """
    if not html or "available_units" not in html:
        return []
    import html as _htmlmod

    decoded = _htmlmod.unescape(html).replace("\\/", "/")
    beds = baths = plan = ""
    ms = re.search(r"/floorplan/([a-z0-9][a-z0-9-]*)", url, re.IGNORECASE)
    slug = ms.group(1) if ms else ""
    mb = _SLUG_BB_RE.search(slug)
    if mb:
        beds, baths = mb.group(1), mb.group(2)
    plan = re.sub(r"^\d+br[-_]?\d+(?:\.\d+)?ba[-_]?", "", slug, flags=re.IGNORECASE)
    plan = plan.replace("-", " ").strip().title() or slug

    units: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in _AVAIL_UNITS_RE.finditer(decoded):
        arr = _bracket_json(decoded, m.start(1))
        if not arr:
            continue
        try:
            data = json.loads(arr)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, list):
            continue
        for u in data:
            if not isinstance(u, dict):
                continue
            uid = str(u.get("id") or "").strip()
            name = str(u.get("name") or "").strip()
            key = uid or name
            if not key or key in seen:
                continue
            seen.add(key)
            rent_i: int | None = None
            pr = re.search(r"[\d,]+", str(u.get("price") or ""))
            if pr:
                try:
                    rent_i = int(round(float(pr.group(0).replace(",", ""))))
                except (TypeError, ValueError):
                    rent_i = None
            units.append(
                make_unit_dict(
                    floor_plan_name=plan,
                    bedrooms=beds,
                    bathrooms=baths,
                    unit_number=name or f"ent-{uid}",
                    rent_low=rent_i,
                    rent_high=rent_i,
                    availability_status="AVAILABLE",
                    availability_date=_iso_date(u.get("available_on")),
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_ENTRATA_WP",
                )
            )
    return units


# --- Entrata modern "prospect portal" theme — `var unitsData` roster (2026-07-30) ---
# The modern Entrata theme (classes pricing-card / fee-transparency-wrapper /
# unit-cell) does NOT use the .fp-card / .unit-card / jd-fp DOM the parsers above
# recognise. Instead the ``/{city}/{property-slug}/conventional/`` LISTING page
# embeds the ENTIRE property roster as a single JS string:
#     <script>var unitsData = '{ "<floorplan_id>": [ {unit...}, ... ], ... }';</script>
# It is an escaped-JSON object keyed by floorplan_id → array of available units.
# ONE fetch yields every floorplan and every available unit (no per-plan drill
# needed for identity/rent/availability). Live-verified 2026-07-30 on Manor Homes
# (12 units), Rise Bedford Lake (7), Steeplechase (14) — all 200 OK direct, no proxy.
#
# RENT — use the per-unit ADVERTISED BASE (min/max_advertised_base_rent, ints).
# ``min_rent``/``max_rent`` is the plan-level "from" range; ``min_rent_unit`` is
# the FEE-INCLUSIVE total on fee-transparency properties (Steeplechase base 1150
# vs unit 1350). Taking the base dodges the #65 gross-as-rent inversion.
# IDENTITY — ``unit_id_engrain`` equals the visible unit number as a string
# (incl. alphanumerics like "1818PS"); ``unit_id`` is Entrata's stable int.
_ENTRATA_UNITS_DATA_RE = re.compile(r"var\s+unitsData\s*=\s*'(.*?)'\s*;", re.DOTALL)


def _units_data_int(value: Any) -> int | None:
    """Coerce a unitsData rent field (int or decimal string) to int, or None."""
    if value in (None, "", 0, "0"):
        return None
    try:
        return int(round(float(str(value).replace(",", ""))))
    except (TypeError, ValueError):
        return None


def parse_entrata_modern_units_data(html: str, url: str) -> list[dict[str, Any]]:
    """Parse the modern Entrata theme's embedded ``var unitsData`` roster.

    Returns one unit-level row per available unit across every floorplan, or
    ``[]`` when the blob is absent or unparseable. Never raises.
    """
    if not html or "unitsData" not in html:
        return []
    m = _ENTRATA_UNITS_DATA_RE.search(html)
    if not m:
        return []
    # The blob is a single-quoted JS string holding escaped JSON: unescape the
    # slash and quote escapes, then json.loads. Empirically sufficient on the
    # live corpus; guarded so a shape change degrades to zero rows, never a raise.
    blob = m.group(1).replace("\\/", "/").replace('\\"', '"').replace("\\'", "'")
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []

    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _fpid, arr in data.items():
        if not isinstance(arr, list):
            continue
        for u in arr:
            if not isinstance(u, dict):
                continue
            unit_no = str(
                u.get("unit_number") or u.get("marketing_unit_number") or ""
            ).strip()
            engrain = str(u.get("unit_id_engrain") or "").strip()
            key = unit_no or engrain or str(u.get("unit_id") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            # BASE rent (per-unit); fall back to the plan-level "from" range
            # (min_rent/max_rent) only when the base fields are absent.
            rent_low = _units_data_int(u.get("min_advertised_base_rent"))
            rent_high = _units_data_int(u.get("max_advertised_base_rent"))
            if rent_low is None and rent_high is None:
                rent_low = _units_data_int(u.get("min_rent"))
                rent_high = _units_data_int(u.get("max_rent"))
            # sqft: ``sqft`` is often null on the listing but ``sqft_unit`` carries it.
            sqft_val = u.get("sqft")
            if sqft_val in (None, "", 0, "0"):
                sqft_val = u.get("sqft_unit")
            sqft = str(sqft_val) if sqft_val not in (None, "") else ""
            source_ids: dict[str, Any] = {}
            if u.get("unit_id") not in (None, ""):
                source_ids["entrata_uid"] = str(u.get("unit_id"))
            if engrain:
                source_ids["unit_id_engrain"] = engrain
            if _fpid not in (None, ""):
                source_ids["entrata_fpid"] = str(_fpid)
            bedroom = u.get("bedroom")
            bathroom = u.get("bathroom")
            units.append(
                make_unit_dict(
                    floor_plan_name=str(u.get("floorplan_name") or "").strip(),
                    bedrooms=str(bedroom) if bedroom not in (None, "") else "",
                    bathrooms=str(bathroom) if bathroom not in (None, "") else "",
                    sqft=sqft,
                    unit_number=unit_no or engrain,
                    rent_low=rent_low,
                    rent_high=rent_high if rent_high is not None else rent_low,
                    availability_status="AVAILABLE",
                    availability_date=_iso_date(str(u.get("available_on") or "")),
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_ENTRATA_MODERN",
                    source_ids=source_ids or None,
                )
            )
    return units


_JSONLD_BLOCK_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_STARTING_AT_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")


def _jsonld_nodes(html: str) -> list[dict[str, Any]]:
    """Every JSON-LD node in *html*, flattening ``@graph``. Never raises."""
    out: list[dict[str, Any]] = []
    for block in _JSONLD_BLOCK_RE.findall(html or ""):
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            out.extend(x for x in data["@graph"] if isinstance(x, dict))
        elif isinstance(data, dict):
            out.append(data)
        elif isinstance(data, list):
            out.extend(x for x in data if isinstance(x, dict))
    return out


def _ld_type_is(node: dict[str, Any], wanted: str) -> bool:
    ty = node.get("@type")
    if isinstance(ty, str):
        return ty == wanted
    if isinstance(ty, list):
        return wanted in ty
    return False


def _ld_num(value: Any) -> str:
    """Format a JSON-LD number string ("0.0", "1.0", "472.0") as "0"/"1"/"472"."""
    if value in (None, ""):
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    return str(int(f)) if f == int(f) else str(f)


def parse_entrata_floorplan_html_jsonld(html: str, url: str) -> list[dict[str, str]]:
    """Parse a ProspectPortal ``/floor-plan/<type>/<design>.html`` page's JSON-LD.

    These per-plan pages (Broadway / Chase / Ravel; #91 Lever A template 2)
    embed a schema.org ``FloorPlan`` node (beds/baths/sqft) plus an ``ItemList``
    of ``Apartment`` items — one per available unit, each carrying its unit
    number (``name``) and rent inside ``floorSize.description`` ("Unit #X,
    Starting at $Y"). Returns one unit-level row per Apartment, or ``[]`` when
    the page carries no such JSON-LD. Never raises.
    """
    if not html or "FloorPlan" not in html:
        return []
    nodes = _jsonld_nodes(html)
    if not nodes:
        return []
    fp = next((n for n in nodes if _ld_type_is(n, "FloorPlan")), {})
    beds = _ld_num(fp.get("numberOfBedrooms"))
    baths = _ld_num(fp.get("numberOfBathroomsTotal"))
    _fp_size = fp.get("floorSize") if isinstance(fp.get("floorSize"), dict) else {}
    plan_sqft = _ld_num(_fp_size.get("value"))
    plan_name = str(fp.get("name") or "").strip()

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for n in nodes:
        if not _ld_type_is(n, "ItemList"):
            continue
        for el in n.get("itemListElement") or []:
            item = el.get("item") if isinstance(el, dict) else None
            if not isinstance(item, dict) or not _ld_type_is(item, "Apartment"):
                continue
            unit_no = str(item.get("name") or "").strip()
            if not unit_no or unit_no in seen:
                continue
            seen.add(unit_no)
            fs = item.get("floorSize") if isinstance(item.get("floorSize"), dict) else {}
            m = _STARTING_AT_RE.search(str(fs.get("description") or ""))
            rent: int | None = None
            if m:
                try:
                    rent = int(round(float(m.group(1).replace(",", ""))))
                except (TypeError, ValueError):
                    rent = None
            rows.append(
                make_unit_dict(
                    floor_plan_name=plan_name,
                    bedrooms=beds,
                    bathrooms=baths,
                    sqft=_ld_num(fs.get("value")) or plan_sqft,
                    unit_number=unit_no,
                    rent_low=rent,
                    rent_high=rent,
                    availability_status="AVAILABLE",
                    availability_date="",
                    source_api_url=url,
                    extraction_tier="TIER_2_JSONLD_ENTRATA_FP",
                )
            )
    return rows


# --- Entrata ProspectPortal `check_availability` surface (2026-05-18) ---
# Validated via DevTools on springriver.prospectportal.com. The
# marketing shell links to <sub>.prospectportal.com; the real unit
# list is GET ?module=check_availability&action=view_unit_spaces&
# property[id]=<pid>&property_floorplan[id]=<fpid>&move_in_date=...&
# occupancy_type=conventional → an HTML fragment whose unit rows are
# <a class="unit-button" data-unit data-rent data-bedroom data-bathroom
# data-unitavailabilitydate ...>. Cloudflare-fronted (cf_clearance) →
# probe_get's cost-gated Web-Unlocker escalation clears it. Stateless
# GET, repli360/securecafe-class (no browser, no OLL stateful wall).
_PP_HOST_RE = re.compile(
    r"https?://([a-z0-9][a-z0-9-]*)\.prospectportal\.com", re.IGNORECASE
)
_PP_PROPID_RE = re.compile(r"property\[id\][^0-9]{0,6}(\d{3,9})", re.IGNORECASE)
_PP_FPID_RE = re.compile(
    r"""(?:property_floorplan\[id\]|data-floorplan)["'\]=\s/]{1,4}(\d{4,9})""",
    re.IGNORECASE,
)

# 2026-05-27 — LeaseLeads-fallback HTML signal. We accept either the
# rendered iframe ``embed.leaseleads.co/<uuid>`` (post-JS DOM) or the
# static ``LeaseLeadsEmbed('<uuid>')`` init call (pre-JS markup, the
# only signal in curl-fetched HTML on the live Lumina site). Either
# form is enough to justify a single ``recover_leaseleads_embed``
# attempt — the helper itself re-validates by sniffing the live DOM.
_LEASELEADS_HTML_SIGNAL_RE = re.compile(
    r"""(?:embed\.leaseleads\.co/[0-9a-f-]{36}"""
    r"""|LeaseLeadsEmbed\(\s*['"][0-9a-f-]{36})""",
    re.IGNORECASE,
)


def _pp_iso(s: str) -> str:
    """``2026/05/17`` | ``2026-05-17`` | ``05/17/2026`` → ``2026-05-17``; else ''.

    2026-07-12: added the US month-first form. The view_unit_spaces
    fragment publishes ``data-unitavailabilitydate="09/17/2026"`` (MM/DD/
    YYYY), which the year-first-only regex dropped — so availability_date
    came back empty on every replayed unit row.
    """
    txt = str(s or "")
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", txt)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", txt)  # MM/DD/YYYY
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return ""


def parse_prospectportal_unit_spaces(
    html: str, url: str
) -> list[dict[str, str]]:
    """Parse a ProspectPortal ``view_unit_spaces`` HTML fragment.

    One row per ``<a class="unit-button" data-*>``. The ``data-*`` attrs
    are authoritative (data-unit = unit_space id, data-rent numeric,
    data-bedroom/-bathroom, data-unitavailabilitydate); the visible unit
    number is the sibling ``.unit-col.unit .unit-col-text``. Floorplan
    name/sqft from the fragment header. Verified: springriver A1 fp
    712595 → units 1306/1406/1410 @ $1,291, 642 sqft, avail 2026-05-17.
    """
    if not html or "unit-button" not in html:
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    fp_name = ""
    h = soup.select_one("h6.availability-fp-name")
    if h:
        fp_name = h.get_text(strip=True)
    fp_sqft = ""
    for li in soup.select("li.fp-stats-item.modal-sq-feet .stat-value"):
        fp_sqft = li.get_text(strip=True)
        break

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in soup.select("a.unit-button"):
        uid = str(a.get("data-unit") or a.get("rel") or "").strip()
        row = a.find_parent(class_="unit-row-wrapper") or a.find_parent(
            class_="unit-row"
        )
        unum = ""
        if row is not None:
            uc = row.select_one(".unit-col.unit .unit-col-text")
            if uc:
                unum = uc.get_text(strip=True)
        unum = unum or uid
        if not unum or unum in seen:
            continue
        seen.add(unum)
        rent_i: int | None = None
        rraw = str(
            a.get("data-rent")
            or a.get("data-min-advertised-base-rent")
            or ""
        )
        rm = re.search(r"[\d,]+", rraw)
        if rm:
            try:
                rent_i = int(round(float(rm.group(0).replace(",", ""))))
            except (TypeError, ValueError):
                rent_i = None
        sqft = ""
        if row is not None:
            sc = row.select_one(".unit-col.sqft .unit-col-text") or row.select_one(
                ".unit-col.sq-ft .unit-col-text"
            )
            if sc:
                sqft = sc.get_text(strip=True)
        out.append(
            make_unit_dict(
                floor_plan_name=fp_name,
                bedrooms=str(a.get("data-bedroom") or ""),
                bathrooms=str(a.get("data-bathroom") or ""),
                sqft=sqft or fp_sqft,
                unit_number=unum,
                rent_low=rent_i,
                rent_high=rent_i,
                availability_status="AVAILABLE",
                availability_date=_pp_iso(
                    str(a.get("data-unitavailabilitydate") or "")
                ),
                source_api_url=url,
                extraction_tier="TIER_1_DOM_ENTRATA_PROSPECTPORTAL",
            )
        )
    return out


# ----------------------------------------------------------------------
# Prospect Portal SSR HTML extraction (2026-05-24)
#
# Background
# ----------
# Entrata Prospect Portal vanity sites (e.g. www.triomke.com,
# www.thebellemeade.com) server-side-render the floor-plan grid into HTML
# at the canonical ``/{city}/{slug}/conventional/`` path. No XHRs fire
# for unit data — the page is static. Pre-this-fix the only consumers of
# this body looked for ``available_units`` (HTML-entity JSON) or
# ``<unit-button>`` markers (the ProspectPortal ``view_unit_spaces``
# fragment); neither matches the SSR card/group templates below, so the
# adapter dead-ended in ``TIER_1_API_ENTRATA_EMPTY`` despite the link-hop
# correctly fetching the right URL.
#
# Two SSR templates exist in production (live-verified 2026-05-24):
#
# Template A — ``.fp-card`` (older PP):
#   <div class="fp-card">
#     <div class="fp-title">The Chilton</div>
#     <div class="dynamic-text-before">1 Bed / 1 Bath</div>
#     <div class="dynamic-text-after">1,006 sq. ft</div>
#     <span class="fee-transparency-text">From $2,664 per month</span>
#     <span class="lease-term-name">15mo lease</span>
#     <span class="availability">1 Units Available</span>
#   </div>
# Live sample: thebellemeade.com (pid 38509) → 4 plans, all rent+sqft.
#
# Template B — ``li.fp-group-item`` with ``.fp-col`` blocks (newer PP):
#   <li class="fp-group-item">
#     <span class="fp-name">Bruce</span>
#     <div class="fp-col bed-bath">
#       <span class="fp-col-title">Beds / Baths</span>
#       <span class="fp-col-text">Studio / 1 ba</span></div>
#     <div class="fp-col rent">
#       <span class="fp-col-title">Rent</span>
#       <div class="fp-col-text fee-transparency-wrapper">
#         <span class="fee-transparency-text">$1,298 per month</span></div></div>
#     <div class="fp-col sq-feet">
#       <span class="fp-col-title">Sq. Ft</span>
#       <span class="fp-col-text">540</span></div>
#     <div class="fp-col action">Only One Left! Details</div>
#   </li>
# Live sample: triomke.com (pid 65287) → 10 plans, 2/10 rent+sqft (rest
# show "—" rent placeholder; sqft still extracts cleanly).
# ----------------------------------------------------------------------

_PP_BED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b(?:ed|d)\b", re.IGNORECASE)
_PP_BATH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b(?:ath|a)\b", re.IGNORECASE)
_PP_SQFT_NUM_RE = re.compile(r"([\d,]+)")
_PP_AVAIL_COUNT_RE = re.compile(r"(\d+)\s*units?\s*available", re.IGNORECASE)
_PP_AVAIL_DATE_RE = re.compile(
    r"available\s+([a-z]{3,9}\s+\d{1,2},?\s*\d{4})", re.IGNORECASE
)


# ── Fee-transparency dual-price display (2026-07-28) ────────────────────
# Entrata's fee-transparency template renders TWO DIFFERENT QUANTITIES in
# a single rent cell — not a range:
#
#   <span class="fee-transparency-text">
#     Total Monthly Leasing Price <span class="...-emphasized">$1,583.98</span>
#     <span class="pp-base-rent pp-base-rent-line">
#       <span class="pp-base-rent-label">Base Rent:</span>
#       <span class="pp-base-rent-amount">$1,391/month</span>
#     </span>
#   </span>
#
# marlowepeoriaplace.com publishes the definitions on its own /floorplans
# page: "Base Rent: The monthly rent for the rental home." and "Total
# Monthly Leasing Price: Base Rent plus fixed, mandatory monthly fees."
# The gross figure is therefore ALWAYS the larger of the two and ALWAYS
# rendered first, so the legacy ``(first, last)`` read produced an
# inverted range: run-2026-07-27-full-0d54ca7 shipped 107 rows with
# rent_low > rent_high, 100% of them Entrata (96 Marlowe Peoria Place I
# unit-level rows + 11 Cyan on Peachtree plan-level rows).
#
# ``rent_low``/``rent_high`` mean ADVERTISED ASKING RENT everywhere else
# in the corpus, and Entrata's own units-table column header for the
# advertised price is literally "Base Rent" — so the range is taken from
# the labelled base-rent portion and the gross figure is dropped. It is a
# different quantity, not a range endpoint; writing it into rent_high (or
# swapping the two to satisfy low<=high) would silently launder it into a
# rent number.
#
# TWO renderings of the base-rent label occur in production. Both were
# enumerated by scanning ALL 4,097 captured bodies of
# run-2026-07-27-full-0d54ca7 for the rent-cell strings each PP parse
# site actually reads (not guessed):
#
#   (1) PREFIX + colon — Prospect-Portal fee-transparency themes
#       "Total Monthly Leasing Price From $1,583.98 Base Rent: $1,391+/month"
#       "Total Monthly Leasing Price Starting from $2,562 Base Rent: $2,417+/month"
#       "Total Monthly Leasing Price $2,251.65 + Base Rent: $2,170+/per installment"
#       "Total Monthly Leasing Price $2,172.98 Base Rent: $1,980/month"
#       DOM: <span class="pp-base-rent-amount">$1,391+/month</span>
#
#   (2) SUFFIX, no colon — the newer ``jd-fp-unit-card`` theme
#       "... $1,731 /mo* 15 months $1,615 Base Rent"
#       DOM: <span class="jd-fp-card-info-term-and-base--base"
#                  data-jd-fp-adp="base_display">$1,615 Base Rent</span>
#
# Rendering (2) is why the first shot at this fix left 156 inverted rows
# on 9 properties: a PREFIX-and-colon text regex cannot see a suffix
# label. It is NOT fixed by loosening that regex to a bare "Base Rent"
# — "Base Rent $1,650 – $2,084" (a column header above a real range) and
# "$305 Off Base Rent" (a concession) would both be captured, converting
# an inversion bug into a truncation bug.
#
# So the PRIMARY rule is DOM-anchored on Entrata's own per-quantity
# markup (``pp-base-rent-amount`` / ``data-jd-fp-adp="base_display"``),
# which is unambiguous and covers both renderings; the text regex stays
# as a fallback for flattened strings, and keeps its colon requirement
# so it stays strictly narrower than the DOM rule. See the table test in
# tests/pms/adapters/test_entrata_pp_fee_transparency_rent.py for the
# must-match / must-NOT-match set.
_PP_MONEY_TOKEN = r"\$\s*[\d,]+(?:\.\d{2})?"
_PP_MONEY_RE = re.compile(r"\$\s*([\d,]+)(?:\.\d{2})?")
# Deliberately no em-dash in the range separator: "—"/"--" is Entrata's
# hidden-rent placeholder and is rejected before this pattern is applied.
_PP_BASE_RENT_LABEL_RE = re.compile(
    rf"base\s+rent\s*:\s*"
    rf"(?P<amounts>{_PP_MONEY_TOKEN}"
    rf"(?:\s*(?:-|–|to)\s*{_PP_MONEY_TOKEN})?)",
    re.IGNORECASE,
)

# Entrata's own markup for "this element holds the base rent, not the
# gross". Tried in order, one ``select_one`` per selector — a single
# comma-joined selector returns the first match in DOCUMENT order, not
# in selector order (the trap already documented at _parse_pp_option_rows).
_PP_BASE_RENT_NODE_SELECTORS = (
    ".pp-base-rent-amount",
    '[data-jd-fp-adp="base_display"]',
    ".jd-fp-card-info-term-and-base--base",
)


def _pp_base_rent_node_text(el: Any) -> str | None:
    """Return the text of Entrata's own base-rent node inside ``el``, if any.

    ``el`` is a BeautifulSoup element (a PP rent cell or the unit card that
    contains it). Returns ``None`` when the element carries no base-rent
    node or the node holds no money token — callers then keep their
    existing whole-cell behaviour.
    """
    if el is None or not hasattr(el, "select_one"):
        return None
    for sel in _PP_BASE_RENT_NODE_SELECTORS:
        node = el.select_one(sel)
        if node is None:
            continue
        text = str(node.get_text(" ", strip=True))
        if _PP_MONEY_RE.search(text):
            return text
    return None


def _pp_rent_cell_text(el: Any) -> str:
    """Flatten a PP rent cell to the string a rent range may be read from.

    When Entrata's fee-transparency dual price is present the cell holds
    the gross "Total Monthly Leasing Price" AND the advertised base rent;
    only the base rent is an asking-rent range endpoint, so the cell is
    narrowed to Entrata's own base-rent node. Otherwise this is exactly
    ``el.get_text(" ", strip=True)`` — a no-op on every other PP theme.
    """
    if el is None:
        return ""
    return _pp_base_rent_node_text(el) or str(el.get_text(" ", strip=True))


def _pp_base_rent_scope(rent_text: str) -> str | None:
    """Narrow a PP rent-cell string to Entrata's labelled Base-Rent amount(s).

    Text-level fallback for call sites that only have a flattened string
    (and defence in depth behind ``_pp_rent_cell_text``). Returns the
    substring holding ONLY the money token(s) that follow the ``Base
    Rent:`` label, else ``None`` (caller keeps its existing whole-string
    behaviour). See the block comment above for why the gross "Total
    Monthly Leasing Price" must never become a rent-range endpoint, and
    why this pattern keeps its colon requirement.
    """
    m = _PP_BASE_RENT_LABEL_RE.search(rent_text or "")
    return m.group("amounts") if m else None


def _pp_money_low_high(rent_text: str) -> tuple[int | None, int | None]:
    """Extract (low, high) ints from a rent display string.

    Returns (None, None) for the em-dash / blank placeholder used when an
    operator hides the published rent (~80% of plans on some triomke-style
    sites). Without this guard the dash was being captured as ``$``,
    yielding a 0 rent and tripping the validity gate.

    When the string carries Entrata's fee-transparency dual price
    ("Total Monthly Leasing Price $X ... Base Rent: $Y"), the range is
    read from the labelled Base-Rent portion ONLY — see the block comment
    above. Otherwise the first/last money tokens are the range endpoints,
    unchanged.
    """
    if not rent_text:
        return (None, None)
    if "—" in rent_text or "--" in rent_text:
        return (None, None)
    base = _pp_base_rent_scope(rent_text)
    if base:
        scoped = _PP_MONEY_RE.findall(base)
        if scoped:
            return (money_to_int(scoped[0]), money_to_int(scoped[-1]))
    matches = _PP_MONEY_RE.findall(rent_text)
    if not matches:
        return (None, None)
    lo = money_to_int(matches[0])
    hi = money_to_int(matches[-1])
    return (lo, hi)


def _pp_beds_baths(bedbath_text: str) -> tuple[int | None, str]:
    """Parse 'Studio / 1 ba', '1 bd / 1 ba', '2 Bed / 2.5 Bath' → (beds, baths)."""
    if not bedbath_text:
        return (None, "")
    beds: int | None = None
    bed_m = _PP_BED_RE.search(bedbath_text)
    if bed_m:
        try:
            beds = int(float(bed_m.group(1)))
        except (TypeError, ValueError):
            beds = None
    elif re.search(r"\bstudio\b", bedbath_text, re.IGNORECASE):
        beds = 0
    bath_m = _PP_BATH_RE.search(bedbath_text)
    baths = bath_m.group(1) if bath_m else ""
    return (beds, baths)


def _pp_txt(el: Any) -> str:
    return el.get_text(" ", strip=True) if el else ""


def parse_entrata_prospectportal_html(
    html: str, url: str
) -> list[dict[str, str]]:
    """Parse Entrata Prospect Portal SSR HTML — supports THREE templates:

    Template A — ``.fp-card`` with ``dynamic-text-before/after`` siblings
    Template B — ``li.fp-group-item`` with ``.fp-col`` title-labelled blocks
    Template C — ``.unit-item`` with ``.unit-title`` / ``.unit-bed-bath``
                 (packed "N Bed, N Bath, NNN SqFt") / ``.unit-price``
                 (the "unit-roster" layout)

    Plan-level output (one row per floorplan name) — these pages render
    a plan grid, not a per-apartment unit roster. Returns ``[]`` when
    none of the three match; the caller (see ``EntrataAdapter.extract``)
    falls through to the next recovery rung.

    Live-verified 2026-05-24:
      * pid 38509 thebellemeade.com — Template A, 4/4 strict-pass
      * pid 65287 triomke.com       — Template B, 2/10 strict-pass
      * pid 41388 greenwoodsapts.com — Template C, 6/6 strict-pass (HAR)

    Critical for the ``TIER_1_API_ENTRATA_EMPTY`` cluster — pre-fix the
    static fallback only looked for ``available_units`` HTML-entity JSON
    and the ``view_unit_spaces`` unit-button fragment, neither of which
    matches the vanity-host SSR grid. HAR-driven validation 2026-05-24
    against the 47 ENTRATA_EMPTY HARs showed 35/47 (74%) lifted by
    Templates A+B alone; adding Template C lifts another ~5 in the
    "untemplated" residue.
    """
    if not html:
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")

    cards = soup.select(".fp-card")
    if cards:
        return _parse_pp_fp_card(cards, url)

    items = soup.select("li.fp-group-item")
    if items:
        return _parse_pp_fp_group_item(items, url)

    # Template C: .unit-item rows (unit-roster layout)
    unit_items = soup.select("li.unit-item, .unit-item")
    if unit_items:
        return _parse_pp_unit_item(unit_items, url)

    return []


def _parse_pp_fp_card(
    cards: list[Any], url: str
) -> list[dict[str, str]]:
    """Template A: ``.fp-card`` with ``dynamic-text-before/after`` siblings."""
    units: list[dict[str, str]] = []
    for card in cards:
        name = _pp_txt(card.select_one(".fp-title")) or _pp_txt(
            card.select_one(".fp-name")
        )
        bedbath = _pp_txt(card.select_one(".dynamic-text-before"))
        sqft_raw = _pp_txt(card.select_one(".dynamic-text-after"))
        # Fee-transparency guard — see _pp_rent_cell_text. No-op on themes
        # that carry no base-rent node (identical to the old _pp_txt call).
        rent_raw = _pp_rent_cell_text(card.select_one(".fee-transparency-text"))
        avail_raw = _pp_txt(card.select_one(".availability"))
        lease_raw = _pp_txt(card.select_one(".lease-term-name"))
        special_raw = _pp_txt(
            card.select_one(".fp-special-main-text")
        ) or _pp_txt(card.select_one(".fp-special-text"))

        beds, baths = _pp_beds_baths(bedbath)
        sqft_m = _PP_SQFT_NUM_RE.search(sqft_raw)
        sqft = sqft_m.group(1).replace(",", "") if sqft_m else ""
        rent_lo, rent_hi = _pp_money_low_high(rent_raw)

        count_m = _PP_AVAIL_COUNT_RE.search(avail_raw)
        avail_units = count_m.group(1) if count_m else ""
        status = "AVAILABLE"
        if re.search(r"waitlist", avail_raw, re.IGNORECASE):
            status = "UNAVAILABLE"
            avail_units = avail_units or "0"
        avail_date = ""
        if not count_m:
            date_m = _PP_AVAIL_DATE_RE.search(avail_raw)
            if date_m:
                avail_date = date_m.group(1)

        # Skip empty rows — name and at least one numeric dimension needed
        if not (name or bedbath) or not (rent_lo or rent_hi or sqft):
            continue

        units.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths),
                sqft=sqft,
                unit_number="",
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi,
                concession=special_raw,
                availability_status=status,
                available_units=avail_units,
                availability_date=avail_date,
                lease_term=lease_raw,
                source_api_url=url,
                extraction_tier="TIER_1_DOM_ENTRATA_PP_FPCARD",
            )
        )
    return units


def _parse_pp_fp_group_item(
    items: list[Any], url: str
) -> list[dict[str, str]]:
    """Template B: ``li.fp-group-item`` with title-labelled ``.fp-col`` blocks."""
    units: list[dict[str, str]] = []
    for item in items:
        name = _pp_txt(
            item.select_one(".fp-name-link, .fp-name, .fp-title")
        )

        # Walk every .fp-col block; index by .fp-col-title text + by
        # class hint (bed-bath / rent / sq-feet / deposit / action).
        # Class-hint lookup keeps us robust to title text changes ("Sq.
        # Ft" vs "Sq Ft" vs "Sqft") and missing-title columns.
        cols: dict[str, str] = {}
        for col in item.select(".fp-col"):
            # Fee-transparency guard — see _pp_rent_cell_text. Applied to
            # every .fp-col, not just the rent one: a bed-bath / sqft /
            # deposit column has no base-rent node, so it is a no-op there.
            value = (
                _pp_rent_cell_text(col.select_one(".fee-transparency-text"))
                or _pp_rent_cell_text(col.select_one(".fp-col-text"))
                or _pp_rent_cell_text(col)
            )
            classes = " ".join(col.get("class") or [])
            for hint in (
                "bed-bath",
                "rent",
                "sq-feet",
                "sqft",
                "deposit",
                "action",
            ):
                if hint in classes:
                    cols.setdefault(hint, value)
                    break
            title = _pp_txt(col.select_one(".fp-col-title"))
            if title:
                cols.setdefault(
                    title.lower().strip().rstrip(":"), value
                )

        bedbath = cols.get("bed-bath") or cols.get("beds / baths") or ""
        rent_raw = cols.get("rent", "")
        sqft_raw = (
            cols.get("sq-feet")
            or cols.get("sqft")
            or cols.get("sq. ft")
            or cols.get("sq ft")
            or ""
        )
        action_raw = cols.get("action", "")
        deposit_raw = cols.get("deposit", "")

        beds, baths = _pp_beds_baths(bedbath)
        sqft_m = _PP_SQFT_NUM_RE.search(sqft_raw)
        sqft = sqft_m.group(1).replace(",", "") if sqft_m else ""
        rent_lo, rent_hi = _pp_money_low_high(rent_raw)

        avail_units = ""
        avail_date = ""
        count_m = _PP_AVAIL_COUNT_RE.search(action_raw)
        if count_m:
            avail_units = count_m.group(1)
        elif re.search(r"only\s+one\b", action_raw, re.IGNORECASE):
            avail_units = "1"
        date_m = _PP_AVAIL_DATE_RE.search(action_raw)
        if date_m:
            avail_date = date_m.group(1)
        status = "AVAILABLE"
        if re.search(r"waitlist", action_raw, re.IGNORECASE):
            status = "UNAVAILABLE"

        # Skip empty rows — need a name and at least one numeric dimension
        if not (name or bedbath) or not (rent_lo or rent_hi or sqft):
            continue

        units.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths),
                sqft=sqft,
                unit_number="",
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi,
                deposit=deposit_raw if deposit_raw and deposit_raw != "—" else "",
                availability_status=status,
                available_units=avail_units,
                availability_date=avail_date,
                source_api_url=url,
                extraction_tier="TIER_1_DOM_ENTRATA_PP_FPGROUP",
            )
        )
    return units


# Template C: .unit-bed-bath text packs beds + baths + sqft into one cell:
#   "1 Bed, 1 Bath, 620 SqFt" — needs a single combined parse pass.
#
# Sqft tolerance for ``&nbsp`` separator: greenwoodsapts.com (HAR 2026-
# 05-24) ships the value as literal ``620\n&nbspSqFt`` because the HTML
# was emitted without the entity's trailing ``;`` so BS4 leaves it as
# raw text. The ``[\s&\w]{0,8}?`` window between the number and the
# ``sqft`` token absorbs ``&nbsp``, ``&#160;``, ``&#xA0;``, plain
# whitespace, and zero-separator variants without dragging in nearby
# digits.
_PP_C_BB_SQ_RE = re.compile(
    r"(?:(?P<beds>\d+|studio)\s*bed[s]?\b[\s,]*)?"
    r"(?:(?P<baths>\d+(?:\.\d+)?)\s*bath[s]?\b[\s,]*)?"
    r"(?:(?P<sqft>[\d,]+)[\s&\w]{0,8}?(?:sq\s*ft|sqft))?",
    re.IGNORECASE,
)
# Available-count from .unit-floor-plan: "4 Available", "Only 1 Left"
_PP_C_AVAIL_RE = re.compile(
    r"(\d+)\s*(?:available|left|unit)|only\s+(\w+)",
    re.IGNORECASE,
)


def _parse_pp_unit_item(items: list[Any], url: str) -> list[dict[str, str]]:
    """Template C: ``.unit-item`` "unit-roster" layout with packed
    ``.unit-bed-bath`` and ``.unit-price`` siblings."""
    units: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for item in items:
        name = _pp_txt(item.select_one(".unit-title")) or _pp_txt(
            item.select_one(".floorplan-title")
        )
        if not name:
            continue
        # Some sites repeat the same title across multiple variations of
        # the same floorplan — dedupe on (name, bedbath) below.

        bedbath = _pp_txt(item.select_one(".unit-bed-bath"))
        # Fee-transparency guard — see _pp_rent_cell_text.
        rent_raw = _pp_rent_cell_text(item.select_one(".unit-price"))
        avail_raw = _pp_txt(item.select_one(".unit-floor-plan")) or _pp_txt(
            item.select_one(".unit-availability")
        )

        # Parse "1 Bed, 1 Bath, 620 SqFt" in a single sweep
        beds: int | None = None
        baths = ""
        sqft = ""
        m = _PP_C_BB_SQ_RE.search(bedbath)
        if m:
            if m.group("beds"):
                bs = m.group("beds")
                if bs.lower() == "studio":
                    beds = 0
                else:
                    try:
                        beds = int(bs)
                    except ValueError:
                        beds = None
            if m.group("baths"):
                baths = m.group("baths")
            if m.group("sqft"):
                sqft = m.group("sqft").replace(",", "")
        # Fallback to discrete parser if the packed regex didn't pick it up
        if beds is None:
            beds, baths_alt = _pp_beds_baths(bedbath)
            baths = baths or baths_alt
        if not sqft:
            # Same &nbsp-tolerant window as the packed regex above
            sm = re.search(
                r"([\d,]+)[\s&\w]{0,8}?(?:sq\s*ft|sqft)",
                bedbath,
                re.IGNORECASE,
            )
            if sm:
                sqft = sm.group(1).replace(",", "")

        rent_lo, rent_hi = _pp_money_low_high(rent_raw)

        avail_units = ""
        status = "AVAILABLE"
        if avail_raw:
            am = _PP_C_AVAIL_RE.search(avail_raw)
            if am:
                avail_units = am.group(1) or ("1" if am.group(2) else "")
        if re.search(r"waitlist|no\s+availability", avail_raw, re.IGNORECASE):
            status = "UNAVAILABLE"

        # Skip empty rows — need name + at least one numeric dimension
        if not (rent_lo or rent_hi or sqft):
            continue

        # Dedupe on (name, bedbath) — Template C often emits the same
        # floorplan once per carousel image
        dedupe_key = f"{name}|{bedbath}"
        if dedupe_key in seen_names:
            continue
        seen_names.add(dedupe_key)

        units.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths),
                sqft=sqft,
                unit_number="",
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi,
                availability_status=status,
                available_units=avail_units,
                source_api_url=url,
                extraction_tier="TIER_1_DOM_ENTRATA_PP_UNITITEM",
            )
        )
    return units


# ----------------------------------------------------------------------
# Prospect Portal PER-PLAN unit-card drill (2026-05-25, canary 1ef1060
# regr#9 — 212 properties / ~1,759 units flipped from inferred_* to
# real unit_numbers).
#
# Background: parse_entrata_prospectportal_html (Templates A/B/C above)
# parses the floorplan-grid INDEX page — one row per plan, with
# ``unit_number=""``. Downstream, core/identity.compute_fallback_unit_id
# hashes name+beds+baths+sqft into ``inferred_<hex>`` (per scraper.py:
# 1044 note). The 212-prop cohort emits one inferred_* row per plan, so
# verdict.py and unit-matching see synthetic ids instead of real ones.
#
# Drill mechanic: each PP-SSR plan link points to a per-plan SSR page
# ``/floorplans/<state>/<property>/<plan-slug>-<fpid>-<phase>/`` that
# server-renders the per-apartment roster as ``.unit-card`` blocks:
#
#   <div class="unit-card container-shape-dynamic unit-item-details-267"
#        role="article">
#     <div class="unit-header">
#       <h3 class="unit-number"> 8700 </h3>
#       ...
#       <a data-fpid="1440" data-uid="267" data-uspid="267" data-fid="8"
#          ...>Show on map</a>
#     </div>
#     ... "2 Bed • 2 Bath • 954 SqFt • 1 • Building 15" ...
#     ... "Available Now" / "Available 06/15/2026" ...
#     ... "from $1,595 per month" ...
#   </div>
#
# Authoritative fields:
#   * unit_number   ← ``<h3 class="unit-number">`` text (e.g. "8700")
#   * data-unit-id  ← ``data-unit-id`` attr OR ``data-uid``/``data-uspid``
#                     on the Show-on-map link OR the ``unit-item-details-
#                     <NNN>`` class suffix — used as source_ids.entrata_uid
#                     so the stable PP id survives merge.
#   * data-fpid     ← floorplan id (when present on the map link)
#
# Live-verified 2026-05-25:
#   * www.risewestarlington.com (.../a1-silver-1212885-1/) — 1 unit
#   * foxlake.prospectportal.com (.../abbington-1440-1/) — 5 units
# Both via curl_cffi chrome120; status 200, no CF challenge.

# "1 Bed • 1 Bath • 480&nbsp;SqFt • 2 • 3605 • Available 07/21/2026"
# splits cleanly on the dot-separator character (``•``) the PP template
# uses to delineate stat cells. We also tolerate ``,`` and ``|`` because
# some legacy PP themes use them in the same position.
_PP_UNIT_CARD_SEP_RE = re.compile(r"[•|,]")
_PP_UNIT_CARD_AVAIL_NOW_RE = re.compile(r"available\s+now", re.IGNORECASE)
_PP_UNIT_CARD_AVAIL_MDY_RE = re.compile(
    r"available\s+(\d{1,2})/(\d{1,2})/(\d{4})", re.IGNORECASE
)
# "from $1,595 per month" / "$1,595" / "$1,495 - $1,695"
_PP_UNIT_CARD_RENT_RE = re.compile(
    r"\$\s*([\d,]+)(?:\.\d{2})?", re.IGNORECASE
)
# Class suffix encoding the unit id, e.g. "unit-item-details-267".
_PP_UNIT_CARD_ID_CLASS_RE = re.compile(r"unit-item-details-(\d+)")
# Bed/bath/sqft tuples that may appear once in the same line, e.g.
# "2 Bed • 2 Bath • 954 SqFt" or "Studio • 1 Bath • 480 SqFt".
_PP_UNIT_CARD_BED_RE = re.compile(
    r"(\d+)\s*bed[s]?\b|\bstudio\b", re.IGNORECASE
)
_PP_UNIT_CARD_BATH_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*bath[s]?\b", re.IGNORECASE
)
# Sqft tolerant to NBSP / "&nbsp" sans-semicolon / plain whitespace.
_PP_UNIT_CARD_SQFT_RE = re.compile(
    r"([\d,]+)[\s\xa0&\w]{0,8}?(?:sq\s*ft|sqft)", re.IGNORECASE
)
# Plan-slug-fpid extraction from the per-plan URL:
# .../floorplans/<location>/<property>/<plan-slug>-<fpid>-<phase>/
# fpid is \d{2,9}: some Entrata properties number floor plans with 2-digit ids
# (2026-07-30, Brownstone brownstonetx.com: a1-53-1, b1-52-1, a2-54-1, c1-55-1 —
# all 4 plans have 2-digit fpids, so \d{3,9} rejected the WHOLE property). The
# {slug}-{fpid}-{phase} structure across two path segments stays specific enough
# that a 2-digit middle token doesn't false-match apply/portal/grid hrefs (see
# test_find_pp_plan_links_ignores_non_plan_hrefs).
_PP_PLAN_URL_RE = re.compile(
    r"/floorplans/[^/]+/[^/]+/([a-z0-9][a-z0-9-]*?)-(\d{2,9})-(\d+)/?",
    re.IGNORECASE,
)
# 2026-05-25 chip #98 follow-up — Aria at Ella URL variant:
# .../<state>/<property>/floorplans/<plan-slug>-<fpid>/fp_name/
# occupancy_type/<type>/
# Anchored to ``/fp_name/`` so it does not false-match on the V1
# pattern (which has 3 path components after /floorplans/ and a
# trailing ``-<phase>`` digit token, not a ``/fp_name/`` literal).
# Live-verified 2026-05-25 across all 12 plans of ariaatella.com
# (e.g. ``a1-730162``, ``a1eup-730171``, ``a1g-730166``).
_PP_PLAN_URL_RE_V2 = re.compile(
    r"/floorplans/([a-z0-9][a-z0-9-]*?)-(\d{3,9})/fp_name/",
    re.IGNORECASE,
)


def _pp_plan_url_match(url: str) -> tuple[str, str] | None:
    """Try both PP per-plan URL templates. Returns (slug, fpid) on hit.

    Two known production templates as of 2026-05-25:
      * V1 — risewestarlington / foxlake / 14fifty / etc.:
        ``/floorplans/<state>/<property>/<slug>-<fpid>-<phase>/``
      * V2 — Aria at Ella (chip #98 follow-up cohort):
        ``/floorplans/<slug>-<fpid>/fp_name/occupancy_type/<type>/``

    V1 wins if both match (the V1 pattern is stricter and includes a
    phase token, so a V1 hit is unambiguous; V2 has only ``/fp_name/``
    as its disambiguating anchor).
    """
    m = _PP_PLAN_URL_RE.search(url or "")
    if m:
        return (m.group(1), m.group(2))
    m = _PP_PLAN_URL_RE_V2.search(url or "")
    if m:
        return (m.group(1), m.group(2))
    return None


#: Markers that mean "this ProspectPortal plan-detail page carries a
#: per-apartment roster". MUST stay in sync with the templates
#: ``parse_entrata_pp_unit_cards`` can actually parse:
#:   U1  ``.unit-card``   — the card template
#:   U2  ``.option-row`` inside ``.fp-units-table`` — the Aria/Grand Oaks
#:       tabular template, handled by ``_parse_pp_option_rows``
#: Matched on a CLASS BOUNDARY, not as a bare substring. "unit-card" as a
#: substring also matches Jonah Digital's ``jd-fp-unit-card`` (live:
#: livethewatts.com/floorplans/ has 46 of them and ZERO Entrata elements),
#: which would hand a foreign vendor's page to the Entrata parser for a
#: guaranteed-empty parse.
_PP_PLAN_UNIT_MARKERS_RE = re.compile(
    r"(?<![-\w])(?:unit-card|fp-units-table|option-row)(?![-\w])"
)


#: ProspectPortal plan-index href: ``/{city-slug}/{property-slug}/conventional/``.
#: Absolute or root-relative; the trailing slash is optional in the wild.
_PP_CONVENTIONAL_RE = re.compile(
    r"""href=["'']((?:https?://[^"'\s]+)?/[a-z0-9][a-z0-9\-]*/[a-z0-9][a-z0-9\-]*/conventional/?)["'']""",
    re.IGNORECASE,
)


def _find_pp_conventional_index(html: str, base: str) -> list[str]:
    """Absolute ``/{city}/{slug}/conventional/`` URLs found in *html*.

    The slug shape cannot be derived from CSV fields — live examples are
    ``/rockville/fenestra-at-the-square/``,
    ``/oklahoma-city-oklahoma-city/garden-gate/`` and
    ``/corvallis-corvallis/grand-oaks-grand-oaks/``, where city and property
    are each sometimes doubled. So it is read off the page's own anchors.

    Same-host only: a ProspectPortal vanity site can link a sibling property,
    and drilling someone else's roster would attribute their apartments here.
    """
    if not html:
        return []
    from urllib.parse import urljoin, urlparse

    base_host = urlparse(base).netloc.lower() if base else ""
    out: list[str] = []
    seen: set[str] = set()
    for m in _PP_CONVENTIONAL_RE.finditer(html):
        url = urljoin(base or "", m.group(1))
        host = urlparse(url).netloc.lower()
        # Allow the vanity host itself and its prospectportal twin.
        if base_host and host != base_host and not host.endswith("prospectportal.com"):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _pp_plan_page_has_units(plan_html: str) -> bool:
    """True when a plan-detail page looks like it carries a unit roster.

    2026-07-25: the per-plan drill used to gate on ``"unit-card" in
    plan_html`` alone, which is only template U1. Template U2 pages — no
    ``.unit-card`` anywhere, roster rendered as ``.option-row`` inside
    ``.fp-units-table`` — were SKIPPED BEFORE the parser ran, even though
    ``parse_entrata_pp_unit_cards`` already falls back to
    ``_parse_pp_option_rows`` for exactly that shape.

    So the U2 branch was unreachable from the drill: the parser was correct
    and simply never handed the page. Live example — grandoakscommunity.com
    plan 2X2A has unit-card=0, option-row=4, fp-units-table=1 and holds 3
    real apartments (D103 $1,939 / F302 $2,049 / G202 $1,939).
    """
    if not plan_html:
        return False
    return bool(_PP_PLAN_UNIT_MARKERS_RE.search(plan_html))


def _pp_extract_card_uid(card: Any) -> str:
    """Return the stable PP unit id (``data-unit-id`` / ``data-uid`` /
    ``data-uspid`` / ``unit-item-details-<n>`` class suffix). Empty
    string when no id found — caller will fall back to the visible
    ``unit-number`` text.

    Order of preference matches PP's own equivalence: ``data-unit-id``
    on the card root is the canonical attr, ``data-uid``/``data-uspid``
    on the map-link is the same numeric id, and the class suffix
    duplicates it on the root. Live capture from foxlake confirms all
    three resolve to ``267`` for the same unit.
    """
    raw = str(card.get("data-unit-id") or "").strip()
    if raw:
        return raw
    for el in card.select("[data-uid], [data-uspid], [data-unit-id]"):
        for attr in ("data-unit-id", "data-uid", "data-uspid"):
            v = str(el.get(attr) or "").strip()
            if v:
                return v
    for cls in card.get("class") or []:
        m = _PP_UNIT_CARD_ID_CLASS_RE.match(str(cls))
        if m:
            return m.group(1)
    return ""


def _pp_extract_card_fpid(card: Any) -> str:
    """Return the floorplan id (``data-fpid``) if any descendant carries
    it. Used to anchor the per-unit row to its parent plan when the
    caller doesn't already know the fpid."""
    for el in card.select("[data-fpid]"):
        v = str(el.get("data-fpid") or "").strip()
        if v:
            return v
    return ""


_PP_OPT_ROW_BED_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*bed[s]?\b|\bstudio\b", re.IGNORECASE
)
_PP_OPT_ROW_BATH_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*bath[s]?\b", re.IGNORECASE
)
# Aria emits ``data-date="06/06/2026"`` (MM/DD/YYYY) on the See-Details
# button. The visible text "Available Jun 06, 2026" is the human form;
# we prefer the numeric attr because it's locale-stable.
_PP_OPT_ROW_DATA_DATE_RE = re.compile(
    r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$"
)
# Fallback for the visible "Jun 06, 2026" / "June 6, 2026" form when
# the See-Details button is missing or stripped.
_PP_OPT_ROW_TEXT_DATE_RE = re.compile(
    r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})"
)
_PP_OPT_ROW_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4,
    "june": 6, "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}


def _pp_opt_row_iso_date(s: str) -> str:
    """Normalise PP availability strings to ``YYYY-MM-DD``.

    Accepts the canonical ``data-date`` attribute (``MM/DD/YYYY``)
    or the visible text (``"Jun 06, 2026"``). Empty string for
    "Available Now" / blank / unparseable.
    """
    if not s:
        return ""
    s = s.strip()
    m = _PP_OPT_ROW_DATA_DATE_RE.match(s)
    if m:
        mo, d, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    m2 = _PP_OPT_ROW_TEXT_DATE_RE.search(s)
    if m2:
        month_name, d, y = m2.groups()
        mo_num = _PP_OPT_ROW_MONTH_NAMES.get(month_name.lower())
        if mo_num:
            return f"{y}-{mo_num:02d}-{int(d):02d}"
    return ""


def _parse_pp_option_rows(
    soup: Any, url: str, plan: str, derived_fpid: str
) -> list[dict[str, str]]:
    """Template U2 — Aria-style per-plan ``.option-row`` roster.

    See ``parse_entrata_pp_unit_cards`` docstring for the markup
    layout and rationale. This is the inner helper that does the
    actual extraction once the caller has decided this is the right
    template (no ``.unit-card`` markers but the page has option-row
    data rows).

    Returns ``[]`` when only the header row exists (``.option-row.
    title`` is the column-headers row PP renders to label the table
    on mobile — never a unit).
    """
    data_rows = [
        r for r in soup.select(".option-row")
        if "title" not in (r.get("class") or [])
    ]
    if not data_rows:
        return []

    # Beds/baths come from the page-level header — every row on a
    # per-plan page shares the same bed/bath/sqft signature. PP's
    # ``.fp-details-container`` carries "1 Bed / 1 Bath" text.
    header_text = ""
    header = soup.select_one(".fp-details-container")
    if header:
        header_text = header.get_text(" ", strip=True)

    beds_val: int | None = None
    bm = _PP_OPT_ROW_BED_RE.search(header_text)
    if bm:
        if bm.group(0).lower().startswith("studio"):
            beds_val = 0
        elif bm.group(1):
            try:
                beds_val = int(float(bm.group(1)))
            except ValueError:
                beds_val = None
    baths_val = ""
    bath_m = _PP_OPT_ROW_BATH_RE.search(header_text)
    if bath_m:
        baths_val = bath_m.group(1)

    # If header didn't carry a usable plan name (rare; the chip #98
    # caller usually passes one in), grab the H1 / h2 plan heading.
    if not plan and header:
        h1 = header.select_one("h1, h2, .fp-name, .fp-title")
        if h1:
            plan = h1.get_text(" ", strip=True)

    units: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    for row in data_rows:
        first = row.select_one(".detail.first")
        if not first:
            continue
        unit_num = first.get_text(" ", strip=True)
        # Strip the ``"Unit "`` mobile-label prefix PP injects.
        unit_num = re.sub(r"^unit\s+", "", unit_num, flags=re.IGNORECASE).strip()
        if not unit_num:
            continue

        # Rent + lease term live in .detail.second. Use the
        # ``.unit-rent`` / ``.fee-transparency-text`` selectors when
        # present so we never grab a deposit / concession dollar
        # token by accident.
        # 2026-07-25 — MUST be tried in priority order, one select_one per
        # selector. A single comma-separated selector returns the first match
        # in DOCUMENT ORDER, not in selector order, and PP renders the
        # Building cell as a bare ``.detail.second`` BEFORE the rent cell — so
        # the broad third alternative won, rent_text came back "Building D",
        # and every unit on the page shipped with a null rent while still
        # carrying a real unit_number.
        #
        # Live-verified on grandoakscommunity.com plan 2X2A: units D103 /
        # F302 / G202 all extracted correctly but rent-less before this fix;
        # $1,939 / $2,049 / $1,939 after.
        rent_text = ""
        for sel in (
            ".detail.second .unit-rent",
            ".detail.second .fee-transparency-text",
            ".detail.second",
        ):
            el = row.select_one(sel)
            if el is None:
                continue
            # Fee-transparency guard — see _pp_rent_cell_text.
            candidate = _pp_rent_cell_text(el)
            if _pp_money_low_high(candidate) != (None, None):
                rent_text = candidate
                break
            # Keep the broadest match as a last resort so a template that
            # renders rent directly in .detail.second still parses.
            rent_text = rent_text or candidate
        rent_lo, rent_hi = _pp_money_low_high(rent_text)

        lease_el = row.select_one(".lease-term-name")
        lease_term = lease_el.get_text(" ", strip=True) if lease_el else ""

        # Sqft + availability — both rendered as ``.detail.block`` in
        # document order. PP labels them with a mobile-text label that
        # we strip.
        blocks = row.select(".detail.block")
        sqft_val = ""
        if blocks:
            txt = blocks[0].get_text(" ", strip=True)
            txt = re.sub(
                r"^sq\.?\s*ft\.?\s*", "", txt, flags=re.IGNORECASE
            ).strip()
            sm = re.search(r"([\d,]+)", txt)
            if sm:
                sqft_val = sm.group(1).replace(",", "")
        visible_avail = ""
        if len(blocks) >= 2:
            txt = blocks[1].get_text(" ", strip=True)
            visible_avail = re.sub(
                r"^available\s*", "", txt, flags=re.IGNORECASE
            ).strip()

        # data-* on the See-Details button is the authoritative source.
        btn = row.select_one(
            ".js-show-details, .detail.action button, .detail.action a"
        )
        data_unit = str(btn.get("data-unit") or "").strip() if btn else ""
        data_fp = str(btn.get("data-floorplan") or "").strip() if btn else ""
        data_date = str(btn.get("data-date") or "").strip() if btn else ""

        avail_date = _pp_opt_row_iso_date(data_date) or _pp_opt_row_iso_date(
            visible_avail
        )
        status = "AVAILABLE"

        # Dedupe on (data-unit | unit_num). PP occasionally renders
        # the same row twice when the plan has variant lease terms.
        dedupe_key = data_unit or unit_num
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        source_ids: dict[str, Any] = {}
        if data_unit:
            source_ids["entrata_uid"] = data_unit
        fpid = derived_fpid or data_fp
        if fpid:
            source_ids["entrata_fpid"] = fpid

        units.append(
            make_unit_dict(
                floor_plan_name=plan,
                bed_label=bed_label_from(beds_val, plan),
                bedrooms=str(beds_val) if beds_val is not None else "",
                bathrooms=baths_val,
                sqft=sqft_val,
                unit_number=unit_num,
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi,
                availability_status=status,
                availability_date=avail_date,
                lease_term=lease_term,
                source_api_url=url,
                extraction_tier="TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL",
                source_ids=source_ids or None,
            )
        )
    return units


def parse_entrata_pp_unit_cards(
    html: str, url: str, floor_plan_name: str = ""
) -> list[dict[str, str]]:
    """Parse a Prospect Portal **per-plan** page's per-apartment roster.

    Two DOM templates as of 2026-05-25:

    Template U1 — ``.unit-card`` blocks (risewestarlington / foxlake /
    most "newer" PP themes). Each ``.unit-card`` is one real unit. The
    visible PP number lives in ``<h3 class="unit-number">`` and the
    stable PP id in ``data-unit-id`` / ``data-uid`` / class suffix.

    Template U2 — ``.option-row`` rows (Aria at Ella style, chip #98
    follow-up). Same "per-plan page lists real units" semantics, but
    different markup:

      <div class="option-row">
        <div class="detail first">Unit 2205</div>
        <div class="detail second">
          ...<span class="stat-value unit-rent">$1,344 /month</span>...
          <span class="lease-term-name">18mo lease</span>
        </div>
        <div class="detail block">Sq.ft. 682</div>
        <div class="detail block">Available Jun 06, 2026</div>
        <div class="detail action">
          <button class="js-show-details"
                  data-floorplan="730162" data-unit="4632678"
                  data-date="06/06/2026">See Details</button>
        </div>
      </div>

    Each row emits unit-level data; ``data-unit`` becomes
    ``source_ids.entrata_uid`` and ``data-floorplan`` becomes
    ``source_ids.entrata_fpid``. ``data-date`` (MM/DD/YYYY) is the
    canonical availability date — strictly preferred over the visible
    ``"Available Jun 06, 2026"`` text because PP renders the data-date
    in ISO-friendly numeric form.

    ``floor_plan_name`` is the parent plan label (e.g. ``"A1 Silver"``,
    ``"Abbington"``). When empty, the function derives it from the URL
    slug (``a1-silver-1212885-1`` → ``"A1 Silver"``;
    ``a1-730162`` → ``"A1"``).

    Tier label: ``TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL``.

    Live-verified 2026-05-25:
      * risewestarlington.com pid 1212885 → 1 unit (#184, uid 5256171)
      * foxlake.prospectportal.com fpid 1440 → 5 units (uids 267/338/
        306/...)
      * ariaatella.com fpid 730162 → 3 units (#2205/1104/2104, uids
        4632678/4632621/4632666)
    """
    if not html or ("unit-card" not in html and "option-row" not in html):
        return []
    from bs4 import BeautifulSoup

    # Derive the plan name from the URL slug when caller didn't supply it.
    plan = floor_plan_name
    slug_fpid = _pp_plan_url_match(url or "")
    derived_fpid = slug_fpid[1] if slug_fpid else ""
    if not plan and slug_fpid:
        slug = slug_fpid[0].replace("-", " ").strip()
        plan = slug.title() if slug else ""

    soup = BeautifulSoup(html, "lxml")
    cards = soup.select(".unit-card")
    if not cards:
        # Template U2 (Aria at Ella style) — .option-row roster.
        return _parse_pp_option_rows(
            soup, url, plan or "", derived_fpid
        )

    units: list[dict[str, str]] = []
    seen_uids: set[str] = set()
    for card in cards:
        unit_num = ""
        h3 = card.select_one(".unit-number")
        if h3:
            unit_num = h3.get_text(" ", strip=True)
        uid = _pp_extract_card_uid(card)

        # Need at least one of (unit_number, stable uid) — without either
        # we'd just be emitting a placeholder row.
        if not unit_num and not uid:
            continue

        # Dedupe on uid (preferred) or unit_num — PP sometimes repeats a
        # card inside a "compare units" modal on the same page.
        dedupe_key = uid or unit_num
        if dedupe_key in seen_uids:
            continue
        seen_uids.add(dedupe_key)

        # Flatten the card's visible text into one searchable blob; the
        # individual stat tokens (bed / bath / sqft / building / floor /
        # availability) live inside many small <span>s and the labels
        # are inconsistent across PP themes, so a single regex sweep is
        # more reliable than selector-per-field.
        text = card.get_text(" ", strip=True)

        beds_val: int | None = None
        bm = _PP_UNIT_CARD_BED_RE.search(text)
        if bm:
            if bm.group(0).lower().startswith("studio"):
                beds_val = 0
            elif bm.group(1):
                try:
                    beds_val = int(bm.group(1))
                except ValueError:
                    beds_val = None
        baths_val = ""
        bath_m = _PP_UNIT_CARD_BATH_RE.search(text)
        if bath_m:
            baths_val = bath_m.group(1)
        sqft_val = ""
        sqft_m = _PP_UNIT_CARD_SQFT_RE.search(text)
        if sqft_m:
            sqft_val = sqft_m.group(1).replace(",", "")

        # Rent: PP cards nest the canonical rent in ``.unit-pricing
        # .price-value`` (verified rise/foxlake 2026-05-25). Older PP
        # themes use a flat ``.unit-price`` element. The text-fallback
        # MUST exclude ``.unit-deposit`` ("Deposit: $500") and the
        # concession banner ("$305 Off Monthly Rent") — those leak
        # dollar tokens that would otherwise win the first-match.
        rent_text = ""
        for sel in (
            ".unit-pricing .price-value",
            ".unit-pricing",
            ".unit-price",
        ):
            el = card.select_one(sel)
            if el:
                # Fee-transparency guard — see _pp_rent_cell_text.
                rent_text = _pp_rent_cell_text(el)
                if rent_text:
                    break
        # Text-level fallback for a theme that renders the "Base Rent:"
        # label with no base-rent node to anchor on. No-op on every other
        # PP theme (no "Base Rent:" label -> whole string, as before).
        rent_matches = _PP_UNIT_CARD_RENT_RE.findall(
            _pp_base_rent_scope(rent_text) or rent_text
        )
        rent_lo: int | None = None
        rent_hi: int | None = None
        if rent_matches:
            rent_lo = money_to_int(rent_matches[0])
            rent_hi = money_to_int(rent_matches[-1])

        # Availability: "Available Now" → today (left empty, downstream
        # fills the scrape date) | "Available MM/DD/YYYY" → ISO date.
        avail_date = ""
        status = "AVAILABLE"
        date_m = _PP_UNIT_CARD_AVAIL_MDY_RE.search(text)
        if date_m:
            mo, d, y = date_m.groups()
            avail_date = f"{y}-{int(mo):02d}-{int(d):02d}"
        # "Available Now" / no date → leave avail_date blank; status
        # stays AVAILABLE so the downstream gate doesn't reject.

        # Building / floor — best-effort, useful for downstream merge
        # but not required for validity.
        building = ""
        bld_m = re.search(r"\bbuilding\s+([A-Za-z0-9-]+)", text, re.IGNORECASE)
        if bld_m:
            building = bld_m.group(1)

        source_ids: dict[str, Any] = {}
        if uid:
            source_ids["entrata_uid"] = uid
        fpid = derived_fpid or _pp_extract_card_fpid(card)
        if fpid:
            source_ids["entrata_fpid"] = fpid

        # Final unit_number: visible PP number wins, then stable uid as
        # the fallback ("ent-<uid>" prefix flags the synthesised case so
        # consumers can tell the two apart in QA).
        final_unit_num = unit_num or f"ent-{uid}"

        units.append(
            make_unit_dict(
                floor_plan_name=plan,
                bed_label=bed_label_from(beds_val, plan),
                bedrooms=str(beds_val) if beds_val is not None else "",
                bathrooms=baths_val,
                sqft=sqft_val,
                unit_number=final_unit_num,
                building=building,
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi,
                availability_status=status,
                availability_date=avail_date,
                source_api_url=url,
                extraction_tier="TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL",
                source_ids=source_ids or None,
            )
        )
    return units


# ── Prospect Portal "jd-fp-unit-card" widget (2026-07-16) ────────────────────
# Newer Entrata vanity-domain template. The /floorplans page ships skeleton
# ``jd-fp-unit-card--preload`` cards, then JS populates the real per-apartment
# roster as ``<a data-jd-fp-selector="unit-card" title="#3100"
# data-unit="<hash>" class="jd-fp-unit-card jd-fp-unit-card--row">`` — the
# marketing unit number in ``title``, a stable per-unit id in ``data-unit``,
# and beds/baths/sqft/rent/availability in the card text. The static
# (unrendered) grid carries only ``--preload`` skeletons with placeholder
# numbers (#00000), so this parser is meaningful ONLY on a RENDERED body and
# filters the preload skeletons out. Live-verified 2026-07-16 on a rendered
# anthemeverett.com/floorplans capture: 25/25 real distinct unit numbers.
_JDFP_TITLE_RE = re.compile(r"#\s*([0-9A-Za-z][0-9A-Za-z-]*)")
_JDFP_FP_RE = re.compile(
    r"Floorplan layout:\s*([^\n$]+?)(?:\s+\$|\s+\d+\s*months?|$)", re.IGNORECASE
)
# jd-fp cards write sqft as "464 sq. ft." (with periods) — the older
# _PP_UNIT_CARD_SQFT_RE doesn't tolerate the dotted form.
_JDFP_SQFT_RE = re.compile(r"([\d,]+)\s*sq\.?\s*ft", re.IGNORECASE)


def parse_entrata_pp_jd_fp_cards(html: str, url: str) -> list[dict[str, str]]:
    """Parse Entrata ``jd-fp-unit-card`` widget rows from a RENDERED /floorplans
    page. Skips ``--preload`` skeletons; the real ``unit_number`` is the
    ``title`` attribute (``#3100`` → ``3100``)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    units: list[dict[str, str]] = []
    seen: set[str] = set()
    for card in soup.select('a[data-jd-fp-selector="unit-card"]'):
        classes = " ".join(card.get("class") or [])
        if "preload" in classes:
            continue  # skeleton placeholder, not real data
        m = _JDFP_TITLE_RE.match(str(card.get("title") or "").strip())
        unum = m.group(1) if m else ""
        if not unum or unum in seen:
            continue
        seen.add(unum)
        text = re.sub(r"\s+", " ", card.get_text(" ", strip=True))
        plan = ""
        fpm = _JDFP_FP_RE.search(text)
        if fpm:
            plan = fpm.group(1).strip()
        beds_val: int | None = None
        if re.search(r"\bstudio\b", text, re.IGNORECASE):
            beds_val = 0
        else:
            bm = _PP_UNIT_CARD_BED_RE.search(text)
            if bm and bm.group(1):
                beds_val = int(bm.group(1))
        bam = _PP_UNIT_CARD_BATH_RE.search(text)
        baths_val = bam.group(1) if bam else ""
        sm = _JDFP_SQFT_RE.search(text)
        sqft_val = sm.group(1).replace(",", "") if sm else ""
        # Fee-transparency guard. This theme renders the label as a SUFFIX
        # with no colon ("$1,731 /mo* 15 months $1,615 Base Rent"), so the
        # text rule cannot see it — the DOM node is the anchor here.
        # ``text`` is the WHOLE card (beds/baths/sqft/availability all live
        # in it), so the rent string is narrowed separately.
        rent_text = _pp_base_rent_node_text(card) or text
        rents = _PP_UNIT_CARD_RENT_RE.findall(
            _pp_base_rent_scope(rent_text) or rent_text
        )
        rent_lo = int(rents[0].replace(",", "")) if rents else None
        rent_hi = int(rents[-1].replace(",", "")) if rents else None
        status = "AVAILABLE" if re.search(r"available", text, re.IGNORECASE) else "UNKNOWN"
        adm = _PP_UNIT_CARD_AVAIL_MDY_RE.search(text)
        avail_date = (
            f"{adm.group(3)}-{adm.group(1).zfill(2)}-{adm.group(2).zfill(2)}"
            if adm
            else ""
        )
        src = str(card.get("data-unit") or "").strip()
        source_ids: dict[str, Any] = {"entrata_uid": src} if src else {}
        units.append(
            make_unit_dict(
                floor_plan_name=plan,
                bed_label=bed_label_from(beds_val, plan),
                bedrooms=str(beds_val) if beds_val is not None else "",
                bathrooms=baths_val,
                sqft=sqft_val,
                unit_number=unum,
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi,
                availability_status=status,
                availability_date=avail_date,
                source_api_url=url,
                extraction_tier="TIER_1_DOM_ENTRATA_PP_JD_FP",
                source_ids=source_ids or None,
            )
        )
    return units


# Anchor selectors PP uses to link from the plan grid to a per-plan
# detail page. The href matches ``/floorplans/<state>/<property>/<slug>
# -<fpid>-<phase>/`` — _PP_PLAN_URL_RE pins the format. The ".fp-name-
# link" is the most reliable selector on Template B index pages
# (foxlake live capture: 7 fp-group-items × 1 fp-name-link each, all
# matching). We also accept .fp-cta-link, .fp-link, and the
# href-pattern fallback for themes that don't class the link.
_PP_PLAN_LINK_SELECTORS = (
    ".fp-name-link",
    ".fp-cta-link",
    "a.fp-link",
    ".fp-card a[href*='/floorplans/']",
    "li.fp-group-item a[href*='/floorplans/']",
    "li.unit-item a[href*='/floorplans/']",
)


def find_entrata_pp_plan_links(index_html: str, origin: str) -> list[str]:
    """Absolute per-plan SSR URLs discovered on a PP index page.

    The PP plan grid (Templates A/B/C above) renders one anchor per plan
    pointing to ``/floorplans/<state>/<property>/<slug>-<fpid>-<phase>/``
    — that's the unit-card page parse_entrata_pp_unit_cards expects.
    Returns absolute URLs deduped in document order; same-origin
    candidates are preserved as-is, cross-origin hrefs are rewritten
    onto ``origin`` so the drill stays on the property we're crawling.

    Returns ``[]`` when the page has no PP plan grid (an Entrata-WP
    detail page, a non-PP CMS, an Entrata API-only site).
    """
    if not index_html:
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(index_html, "lxml")
    candidates: list[str] = []
    for sel in _PP_PLAN_LINK_SELECTORS:
        for a in soup.select(sel):
            href = str(a.get("href") or "").strip()
            if not href:
                continue
            candidates.append(href)
    # Fallback: scan every <a> for the URL pattern in case the theme
    # didn't class the link. _PP_PLAN_URL_RE filters non-PP hrefs.
    if not candidates:
        for a in soup.select("a[href*='/floorplans/']"):
            href = str(a.get("href") or "").strip()
            if href:
                candidates.append(href)

    origin_clean = (origin or "").rstrip("/")
    out: list[str] = []
    for href in candidates:
        # Accept either PP per-plan URL template (V1 phased or V2 Aria-
        # style). _pp_plan_url_match returns None on non-matching hrefs
        # (apply links, residents-portal links, the grid index itself).
        if _pp_plan_url_match(href) is None:
            continue
        if href.startswith("http://") or href.startswith("https://"):
            absolute = href
        elif href.startswith("/"):
            absolute = origin_clean + href
        else:
            absolute = origin_clean + "/" + href
        # Strip query / fragment to canonicalise.
        absolute = absolute.split("?")[0].split("#")[0]
        if not absolute.endswith("/"):
            absolute += "/"
        if absolute not in out:
            out.append(absolute)
    return out


def find_entrata_fp_detail_links(index_html: str, origin: str) -> list[str]:
    """Absolute ``/floorplan/<slug>/`` detail URLs from an index page."""
    if not index_html:
        return []
    out: list[str] = []
    for m in _FP_DETAIL_RE.finditer(index_html):
        path = m.group(0)
        if not path.endswith("/"):
            path += "/"
        u = origin.rstrip("/") + path
        if u not in out:
            out.append(u)
    return out


async def _entrata_static_fetch(
    url: str, *, unlocker: bool = True, headers: dict[str, str] | None = None
) -> str:
    from ma_poc.pms.adapters._probe import probe_get

    kw: dict[str, Any] = {"timeout": 20, "unlocker": unlocker}
    if headers:
        kw["headers"] = headers
    r = probe_get(url, **kw)
    return (r.text or "") if r.status_code == 200 else ""


# Verbatim ``view_unit_spaces`` XHR URL embedded in a PP grid's primary-action
# buttons (data-url / href). Anchored on the endpoint token; the surrounding
# non-delimiter run captures the full (possibly &amp;- or \/-escaped) URL.
_VUS_URL_RE = re.compile(
    r"""((?:https?:)?[^\s"'<>\\]*view_unit_spaces[^\s"'<>\\]*)""",
    re.IGNORECASE,
)


def _extract_vus_urls(index_bodies: list[tuple[str, str]], base: str) -> list[tuple[str, str]]:
    """Return ``(grid_url, view_unit_spaces_url)`` pairs harvested from the
    grid bodies. ``is_availability_alert`` (waitlist) plans are skipped —
    they are legitimately 0-unit and keep their plan-level row."""
    from urllib.parse import urljoin

    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for idx_url, html in index_bodies:
        if not html:
            continue
        for m in _VUS_URL_RE.finditer(html):
            raw = m.group(1).replace("&amp;", "&").replace("\\/", "/")
            if "is_availability_alert" in raw:
                continue
            if raw.startswith("//"):
                raw = "https:" + raw
            elif raw.startswith("/"):
                raw = urljoin(idx_url or base, raw)
            if raw in seen:
                continue
            seen.add(raw)
            out.append((idx_url or base, raw))
    return out


def _replay_vus_sync(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Blocking replay of view_unit_spaces URLs on a cookie-bearing session.

    The endpoint 400s unless the request carries (a) the grid page's session
    cookies AND (b) an ``X-Requested-With: XMLHttpRequest`` header AND (c) a
    ``Referer`` of the grid URL — it is an in-page XHR. So we open ONE
    curl_cffi session per grid, GET the grid to seat cookies, then replay
    each unit URL on that session. Runs in a worker thread (see caller).
    Validated locally (residential IP): villagewestside → unit rows w/ rent.
    """
    try:
        from curl_cffi import requests as _cc
    except Exception:
        return []
    rows: list[dict[str, str]] = []
    sessions: dict[str, Any] = {}
    for grid_url, vus_url in pairs[:8]:
        try:
            sess = sessions.get(grid_url)
            if sess is None:
                sess = _cc.Session(impersonate="chrome120")
                sess.get(grid_url, timeout=25)  # seat cookies
                sessions[grid_url] = sess
            r = sess.get(
                vus_url,
                timeout=25,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "text/html, */*; q=0.01",
                    "Referer": grid_url,
                },
            )
            if getattr(r, "status_code", 0) == 200 and r.text:
                rows.extend(parse_prospectportal_unit_spaces(r.text, vus_url))
        except Exception:
            continue
    for sess in sessions.values():
        try:
            sess.close()
        except Exception:
            pass
    return rows


async def _harvest_view_unit_spaces(
    index_bodies: list[tuple[str, str]], base: str
) -> list[dict[str, str]]:
    """Harvest the verbatim ``view_unit_spaces`` XHR URLs from PP grid bodies
    and replay them (cookie-bearing session + XHR + Referer) into unit rows.

    2026-07-12: the complete per-plan availability URLs are embedded verbatim
    in the /conventional grid's primary-action buttons; the old
    ``_probe_prospectportal`` RECONSTRUCTS a stripped URL that 400s. Replaying
    the verbatim URL as an in-page XHR yields unit-level rows the existing
    ``parse_prospectportal_unit_spaces`` handles. Validated locally from a
    residential IP. Self-gating + never-raises: vanity grids carry no such
    buttons (0 URLs → 0 rows → plan-level stands), and from a walled network
    the replay 403s and returns []. Runs the blocking session work in a
    worker thread so the event loop is never blocked.
    """
    import asyncio

    pairs = _extract_vus_urls(index_bodies, base)
    if not pairs:
        return []
    try:
        return await asyncio.to_thread(_replay_vus_sync, pairs)
    except Exception:
        return []


# 2026-05-20: structured empty-exit labels for Path B retry. Same pattern
# as ``_classify_rentcafe_failure`` (rentcafe.py:311). Previously the
# adapter exited with the bare ``TIER_1_API_ENTRATA`` success label even
# on 0-unit outcomes; ``is_empty_exit()`` returned False so the
# orchestrator never re-dispatched. The 193-property ENTRATA failed-
# strict cohort from sw9p4 (mostly false-positive Entrata detection
# triggered by ``/Apartments/module/application_authentication/`` link)
# is unblocked by this — Path B can now route them to Knock / SightMap /
# RentCafe / etc. on the retry.
_TIER_BASE = "TIER_1_API_ENTRATA"
_TIER_NO_RESPONSE = f"{_TIER_BASE}_NO_RESPONSE"
#: 2026-07-25: the page never LOADED — a bot-management interstitial was
#: served instead of the site. Distinct from _NO_RESPONSE ("page loaded, no
#: Entrata XHR seen"), which is an extraction signal. Conflating the two cost
#: a whole diagnosis cycle: 55 of 60 properties in the ENTRATA_NO_RESPONSE
#: cohort returned HTTP 403 + a Cloudflare challenge on static fetch, so the
#: roster was never reachable — yet they read as 60 adapter bugs.
_TIER_FETCH_BLOCKED = f"{_TIER_BASE}_FETCH_BLOCKED"

#: Cloudflare / bot-management interstitial markers. A challenge page is small
#: and carries none of the site's own content, so size + marker together are a
#: reliable discriminator against a genuinely thin marketing shell.
_CHALLENGE_MARKERS: tuple[str, ...] = (
    "just a moment",
    "cdn-cgi/challenge-platform",
    "cf-browser-verification",
    "_cf_chl_opt",
    "checking your browser",
    "enable javascript and cookies to continue",
)
_CHALLENGE_MAX_BYTES = 10_000


def _looks_like_challenge_page(html: str | None) -> bool:
    """True when *html* is a bot-management interstitial, not the site.

    Requires BOTH a small body and a known marker: a real marketing shell can
    mention "just a moment" in copy, and a large page that embeds the phrase is
    not a challenge. Never raises.
    """
    if not html:
        return False
    try:
        if len(html) > _CHALLENGE_MAX_BYTES:
            return False
        low = html.lower()
        return any(m in low for m in _CHALLENGE_MARKERS)
    except Exception:  # pragma: no cover - defensive
        return False
_TIER_SHAPE_REJECTED = f"{_TIER_BASE}_SHAPE_REJECTED"
_TIER_EMPTY = f"{_TIER_BASE}_EMPTY"


def _is_entrata_response(body: Any) -> bool:
    """Loose body-shape check matching the two known Entrata response
    formats (flat list of floorplan dicts, or widget envelope).

    Returns True when ``body`` carries Entrata-shaped data — used in the
    failure classifier to distinguish ``_SHAPE_REJECTED`` (no body
    matched) from ``_EMPTY`` (bodies matched, parser found 0 units).
    """
    if isinstance(body, list) and body and isinstance(body[0], dict):
        first = body[0]
        if any(k in first for k in ("floorplan-name", "no_of_bedroom", "square_footage")):
            return True
    if isinstance(body, dict):
        # Widget envelope — gated by _filter_widget_response in the main
        # extract path. Cheap match here: presence of common Entrata
        # widget keys.
        for k in ("widget_data", "floorplans", "available_units"):
            if k in body:
                return True
    return False


def _classify_entrata_failure(
    api_responses: list[dict[str, Any]],
    page_html: str | None = None,
) -> tuple[str, str]:
    """Return (tier_code, machine-readable error message) for a 0-unit
    Entrata extraction.

    Tier resolution:
      * Page body is a bot-management interstitial → ``_FETCH_BLOCKED``
      * No API responses captured at all → ``_NO_RESPONSE``
      * Responses captured but none Entrata-shaped → ``_SHAPE_REJECTED``
      * Otherwise (responses matched but parser produced 0 records) →
        ``_EMPTY``

    The ``_FETCH_BLOCKED`` arm exists because the other three all describe
    EXTRACTION outcomes, and lumping a failed FETCH in with them is actively
    misleading. Measured 2026-07-25: 55 of the 60 properties sitting at
    ``ENTRATA_NO_RESPONSE`` returned HTTP 403 with a Cloudflare challenge on
    static fetch — the roster was never on the wire — yet the tier name says
    "no Entrata response", which reads as an adapter bug and sent a whole
    diagnosis cycle looking for parser gaps that did not exist.
    """
    if _looks_like_challenge_page(page_html):
        return (
            _TIER_FETCH_BLOCKED,
            "ENTRATA_FETCH_BLOCKED: bot-management interstitial served instead "
            "of the page — nothing was fetched to extract from. Not an adapter "
            "failure; retry via the render/CF-clearing path.",
        )
    if not api_responses:
        return (
            _TIER_NO_RESPONSE,
            "ENTRATA_NO_RESPONSE: no Entrata-shaped network responses captured "
            "during page load (false-positive detector signal or marketing-only "
            "shell)",
        )
    shape_matches = [r for r in api_responses if _is_entrata_response(r.get("body"))]
    if not shape_matches:
        return (
            _TIER_SHAPE_REJECTED,
            f"ENTRATA_SHAPE_REJECTED: {len(api_responses)} responses captured, "
            "none matched Entrata envelope/key signature",
        )
    return (
        _TIER_EMPTY,
        f"ENTRATA_EMPTY: {len(shape_matches)} shape-matched response(s), "
        "but parser emitted 0 admitted units",
    )


class EntrataAdapter:
    """Entrata PMS adapter. Parses /Apartments/module/widgets/ API responses."""

    pms_name: str = "entrata"
    _fingerprints: list[str] = [
        "entrata.com",
        "/Apartments/module/",
        "prospectportal.com",
        "prospect_portal",
        "floorplan_overview",
    ]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from Entrata widget API responses captured during page load.

        Entrata sites load floorplan data via /Apartments/module/widgets/ endpoints.
        The response is either a flat list of floorplan objects or a widget_data
        envelope wrapping the list.
        """
        result = AdapterResult(tier_used="TIER_1_API_ENTRATA")
        all_units: list[dict[str, str]] = []

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        for resp in api_responses:
            url = resp.get("url", "")
            body = resp.get("body")

            # Flat list of floorplan dicts (most common Entrata shape)
            if isinstance(body, list) and body and isinstance(body[0], dict):
                first = body[0]
                if any(k in first for k in ("floorplan-name", "no_of_bedroom", "square_footage")):
                    units = parse_entrata_floorplans(body, url)
                    all_units.extend(units)
                    result.api_responses.append(resp)
                    continue

            # Widget envelope
            if isinstance(body, dict):
                filtered = _filter_widget_response(body)
                if filtered is None:
                    continue
                units = parse_entrata_widget_envelope(body, url)
                if units:
                    all_units.extend(units)
                    result.api_responses.append(resp)

        if all_units:
            # Stage 1 validity gate — drops dim-less rows.
            from ma_poc.extraction.post_process import post_process

            _pp_parsed = len(all_units)
            _pp = post_process(all_units, property_id=getattr(ctx, "property_id", None))
            if _pp.n_admitted > 0:
                result.units = _pp.admitted
                result.plan_summaries = _pp.plan_summaries
                result.winning_url = (
                    result.api_responses[0].get("url") if result.api_responses else None
                )
                result.confidence = min(0.95, 0.7 + 0.05 * _pp.n_admitted)
                return result
            result.errors.append(
                f"ENTRATA_VALIDITY_REJECTED: {_pp_parsed} parsed rows "
                f"failed unit_validity (no numeric dimension)"
            )

        # Bug 9 (2026-05-09 deep-dive): direct probe of known Entrata paths
        # when the captured-API path produced nothing AND we have a live
        # Playwright page. Entry pages that are pure marketing redirects
        # (e.g. 1701arch.com → livethearch.com) load zero unit XHRs, so the
        # capture-driven path can't fire. Probing with page.evaluate lets us
        # exercise the same origin/session the page already established.
        if page is not None:
            probe_units = await self._probe_known_endpoints(page, ctx)
            if probe_units:
                # Stage 1 validity gate also applies to probed units.
                from ma_poc.extraction.post_process import post_process

                _pp_probe = post_process(
                    probe_units, property_id=getattr(ctx, "property_id", None)
                )
                if _pp_probe.n_admitted > 0:
                    result.units = _pp_probe.admitted
                    result.plan_summaries = _pp_probe.plan_summaries
                    result.tier_used = "TIER_1_API_ENTRATA_PROBE"
                    result.confidence = min(0.95, 0.7 + 0.05 * _pp_probe.n_admitted)
                    return result
                result.errors.append(
                    f"ENTRATA_PROBE_VALIDITY_REJECTED: {len(probe_units)} probed rows "
                    f"failed unit_validity (no numeric dimension)"
                )

        # Entrata-WP static fallback: marketing sites (WordPress +
        # prospectportal apply links) embed an HTML-entity-encoded
        # "available_units" JSON on /floorplan/<slug>/ detail pages.
        # The captured-API and probe paths above see nothing (data is
        # server-rendered, not an XHR), so crawl the detail pages with
        # a static probe (no render needed).
        origin = ""
        fr = getattr(ctx, "fetch_result", None)
        if fr is not None:
            origin = str(getattr(fr, "final_url", "") or "")
        origin = origin or getattr(ctx, "base_url", "") or ""
        try:
            p = urlparse(origin)
            base = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""
        except Exception:
            base = ""

        # ProspectPortal SSR-grid fallback (2026-05-24). Vanity-host
        # Entrata sites render the floor-plan grid into HTML at
        # ``/{city}/{slug}/conventional/`` with no XHR backing it.
        # Pre-this-fix the cascade had no parser for that grid — the
        # adapter dead-ended in ``ENTRATA_EMPTY`` despite the link-hop
        # correctly fetching the right URL. Live-verified on pid 38509
        # (4 units) and 65287 (10 units) — see
        # ``parse_entrata_prospectportal_html`` doctring for template
        # details.
        if base:
            pp_ssr_units: list[dict[str, str]] = []
            # Tracks every index HTML body we successfully matched a PP
            # template against — keyed by its absolute URL. Used by the
            # per-plan unit-card drill (canary 1ef1060 regr#9) to find
            # plan links without re-fetching the index.
            pp_ssr_index_bodies: list[tuple[str, str]] = []
            fr_body_check = (
                getattr(fr, "body", None) if fr is not None else None
            )
            if isinstance(fr_body_check, bytes):
                fr_body_check = fr_body_check.decode("utf-8", "replace")

            # Step 1: parse the captured body in case the homepage
            # already IS the SSR grid (some PP vanity hosts redirect
            # ``/`` → ``/{city}/{slug}/conventional/``).
            #
            # 2026-05-25 (wave-2 cluster #3 CF+Entrata+SightMap):
            # ``fp-name-link`` is the most reliable PP plan-link
            # selector and survives PP theme variants that omit the
            # legacy ``fp-card`` / ``fp-group-item`` wrappers (e.g. the
            # newer ``beans-floorplans-map-tab`` theme — live-verified
            # on 14fiftyapartments.com pid 258254). Without it those
            # bodies were silently dropped before the per-plan unit-
            # card drill could see their plan links.
            if isinstance(fr_body_check, str) and fr_body_check and (
                "fp-card" in fr_body_check
                or "fp-group-item" in fr_body_check
                or "fp-name-link" in fr_body_check
            ):
                _cap_url = str(getattr(fr, "final_url", "") or base)
                pp_ssr_units.extend(
                    parse_entrata_prospectportal_html(
                        fr_body_check, _cap_url
                    )
                )
                pp_ssr_index_bodies.append((_cap_url, fr_body_check))

            # Step 2: discover deep ``/{city}/{slug}/(conventional|affordable)/``
            # candidates from the captured body, plus shallow guesses that
            # PP servers commonly 302 to the canonical deep path.
            #
            # 2026-05-27 (failure-grind chip): the prior regex required
            # ``https?://`` absolute hrefs only; Entrata sites that emit
            # the same nav as a root-relative path (``href="/city/prop/
            # conventional/"``) were dropped. Both forms now feed the
            # candidate list, and candidates whose slug matches the host
            # are ranked first so the cap of 3 reaches the right page on
            # multi-property portals. Reference impl:
            # investigations/2026-05-27-failure-grind/artifacts/probe/
            # entrata_deep_probe.py (14 rescues, ~30 estimated remaining).
            deep_candidates: list[str] = []
            if isinstance(fr_body_check, str) and fr_body_check:
                _base_host = urlparse(base).netloc
                # Host slug (e.g. "princetonbradford" from
                # "www.princetonbradford.com") — used to rank anchors
                # whose path segment overlaps with the property name.
                _host_slug = re.sub(
                    r"^www\.|\..*$", "", _base_host
                ).lower()
                _re_deep_abs = re.compile(
                    r'href=["\']'
                    r'(https?://[^"\']+/(?:[^/"\']+/){2,}'
                    r'(?:conventional|affordable)/?[^"\'?#]*?)["\']',
                    re.IGNORECASE,
                )
                _re_deep_rel = re.compile(
                    r'href=["\']'
                    r'(/(?:[^/"\']+/){2,}(?:conventional|affordable)/?[^"\'?#]*?)["\']',
                    re.IGNORECASE,
                )
                _raw: list[str] = []
                for _m in _re_deep_abs.finditer(fr_body_check):
                    cand = _m.group(1).split("?")[0].split("#")[0]
                    if cand and urlparse(cand).netloc.endswith(_base_host):
                        _raw.append(cand)
                for _m in _re_deep_rel.finditer(fr_body_check):
                    rel = _m.group(1).split("?")[0].split("#")[0]
                    if rel:
                        _raw.append(base + rel)

                def _slug_score(u: str) -> int:
                    path = urlparse(u).path.lower()
                    return 1 if _host_slug and _host_slug in path else 0

                # Rank slug-matching first, then preserve discovery
                # order; dedupe; cap at 3 to prevent runaway crawls.
                _seen: set[str] = set()
                _ranked = sorted(
                    _raw, key=lambda u: -_slug_score(u)
                )
                for cand in _ranked:
                    if cand in _seen:
                        continue
                    _seen.add(cand)
                    deep_candidates.append(cand)
                    if len(deep_candidates) >= 3:
                        break

            for path in (
                "/conventional/",
                "/floorplans/",
                "/floor-plans/",
            ):
                deep_candidates.append(base + path)

            # 2026-07-11 adapter audit: hosted Prospect Portal SPAs
            # (villagewestside.prospectportal.com) serve the SAME shell
            # for every path — /conventional/ and /floorplans/ never
            # carry fp-card markers, so Steps 2-3 come up empty and the
            # property falls to plan-text junk. But the shell embeds
            # Entrata's internal id ("property_id":"540130"), and the
            # server-rendered module endpoint
            # /Apartments/module/property_info/property_id/{pid}
            # returns the full fp-card index (7 plans + fp_name plan
            # links on the reference portal). Append it so Step 3's
            # existing parser + Step 4's unit-card drill just work.
            #
            # The pid must come from the SSR config script. The captured
            # body is often the Playwright-RENDERED DOM, where JS may have
            # stripped that config block — so if the token isn't in the
            # captured body, re-fetch the origin ROOT statically (the
            # curl shell reliably carries "property_id":"<pid>").
            _pid_re = re.compile(r'"property_id"\s*:\s*"?(\d{3,12})"?')
            if "prospectportal.com" in urlparse(base).netloc:
                _pid = ""
                if isinstance(fr_body_check, str) and fr_body_check:
                    _m = _pid_re.search(fr_body_check)
                    if _m:
                        _pid = _m.group(1)
                if not _pid:
                    try:
                        _root_html = await _entrata_static_fetch(base + "/")
                    except Exception:
                        _root_html = ""
                    if _root_html:
                        _m = _pid_re.search(_root_html)
                        if _m:
                            _pid = _m.group(1)
                if _pid:
                    deep_candidates.append(
                        base
                        + "/Apartments/module/property_info/property_id/"
                        + _pid
                    )
            elif isinstance(fr_body_check, str) and fr_body_check:
                # 2026-07-11 (DOM-tier debug): CROSS-DOMAIN Prospect Portal.
                # Plan-card marketing sites (WordPress etc.) link to
                # ``{slug}.prospectportal.com`` for availability — the
                # portal carries the full fp-card unit index but neither
                # discovery path above can reach it: the deep-candidate
                # regex is same-host-only (``netloc.endswith(_base_host)``)
                # and the pid-mining block requires the ENTRY base to be
                # prospectportal.com. Net effect: the adapter dies
                # SHAPE_REJECTED, Path-B retries promote generic_plan_text,
                # and the property ships plan-level rows despite unit-level
                # data one hop away (liveatthemirage.com → themirage.
                # prospectportal.com pid 100052396, 16 fp-cards — verified
                # live; ~15% of the 344-prop plan-shaped GENERIC_PLAN_TEXT
                # cohort carries a prospectportal href). Mine the portal
                # origin from the marketing body, statically fetch its
                # shell for the internal property_id, and feed the module
                # endpoint to Step 3 — same machinery as the same-host
                # path above.
                _pp_m = _PP_HOST_RE.search(fr_body_check)
                if _pp_m:
                    _pp_origin = (
                        f"https://{_pp_m.group(1)}.prospectportal.com"
                    )
                    try:
                        _pp_shell = await _entrata_static_fetch(
                            _pp_origin + "/"
                        )
                    except Exception:
                        _pp_shell = ""
                    if _pp_shell:
                        _m = _pid_re.search(_pp_shell)
                        if _m:
                            deep_candidates.append(
                                _pp_origin
                                + "/Apartments/module/property_info/property_id/"
                                + _m.group(1)
                            )

            # Step 3: fetch and try the PP SSR parser. Stop on first
            # body that admits at least one validity-gated row.
            for cand_url in dict.fromkeys(deep_candidates):
                if pp_ssr_units:
                    break  # already got the homepage body
                try:
                    cand_html = await _entrata_static_fetch(cand_url)
                except Exception:
                    cand_html = ""
                if not cand_html:
                    continue
                if (
                    "fp-card" not in cand_html
                    and "fp-group-item" not in cand_html
                    and "fp-name-link" not in cand_html
                ):
                    continue
                pp_ssr_units.extend(
                    parse_entrata_prospectportal_html(cand_html, cand_url)
                )
                pp_ssr_index_bodies.append((cand_url, cand_html))

            # 2026-07-26 — HARVEST THE PLAN INDEX FROM THE PAGE.
            #
            # ProspectPortal mounts its plan grid at
            # ``/{city-slug}/{property-slug}/conventional/`` — NOT at
            # ``/floorplans``, which on many of these hosts 302s to the
            # homepage. The candidate-path probes above therefore find no
            # index, the per-plan drill has nothing to walk, and the property
            # ships plan-level.
            #
            # The slug shape is NOT derivable: live examples are
            # ``/rockville/fenestra-at-the-square/``,
            # ``/oklahoma-city-oklahoma-city/garden-gate/`` and
            # ``/corvallis-corvallis/grand-oaks-grand-oaks/`` — city and
            # property are each sometimes doubled. So it is harvested from
            # the page's own anchors rather than constructed.
            #
            # Measured on the 2026-07-26 canary: 20 of the 53 unconverted
            # Entrata-surface properties (38%) expose this href on their
            # landing page. Live-verified on gardengateokc.com — the
            # conventional index yields 19 plan links whose detail pages
            # carry real apartments (4032 Renovated $1,863).
            if not pp_ssr_index_bodies:
                for _conv_url in _find_pp_conventional_index(
                    fr_body_check if isinstance(fr_body_check, str) else "", base
                )[:2]:
                    try:
                        _conv_html = await _entrata_static_fetch(_conv_url)
                    except Exception:
                        continue
                    if not _conv_html:
                        continue
                    if (
                        "fp-card" not in _conv_html
                        and "fp-group-item" not in _conv_html
                        and "fp-name-link" not in _conv_html
                    ):
                        continue
                    pp_ssr_units.extend(
                        parse_entrata_prospectportal_html(_conv_html, _conv_url)
                    )
                    pp_ssr_index_bodies.append((_conv_url, _conv_html))
                    break

            # Step 3b (2026-07-30, #91 Lever A): the per-plan
            # ``/floorplans/<state>/<property>/<slug>-<fpid>-<phase>/`` links are
            # often ONLY in the RENDERED landing DOM — the static body is a SPA
            # shell, and the anchors are plain ``<a href="/floorplans/...">`` with
            # no fp-card / fp-name-link class, so Steps 1-3 never added them to
            # ``pp_ssr_index_bodies``. When a live page is available AND no static
            # body already exposed plan links, harvest the rendered content so the
            # drill below can discover them. Additive: no-op when the page has no
            # such links. Verified: brownstonetx.com (4 plans, 2-digit fpids →
            # unit 423, 1/1, 610sf) and drexelridge.com (unit 204, 1/1, 906sf).
            if not any(
                find_entrata_pp_plan_links(_b, base) for _, _b in pp_ssr_index_bodies
            ):
                _dom_sources: list[str] = []
                if page is not None:
                    try:
                        _pc = await page.content()
                    except Exception:
                        _pc = ""
                    if _pc:
                        _dom_sources.append(_pc)
                # The #42/#45 render lever (jugnu.py) re-runs extraction with the
                # RENDERED DOM in fetch_result.body and page=None (ENABLE_ENTRATA_
                # PLAN_RENDER / ENABLE_PLAN_UNIT_RENDER), so the rendered
                # /floorplans/ links arrive via fr_body_check, NOT page.content().
                # Static bodies carry no such links (0 for Brownstone/Drexel), so
                # this only fires post-render — additive.
                if isinstance(fr_body_check, str) and fr_body_check:
                    _dom_sources.append(fr_body_check)
                for _dom in _dom_sources:
                    if find_entrata_pp_plan_links(_dom, base):
                        pp_ssr_index_bodies.append(
                            (str(getattr(fr, "final_url", "") or base), _dom)
                        )
                        break

            # Step 4 (canary 1ef1060 regr#9, 2026-05-25): unit-card drill.
            # Templates A/B/C above produce plan-level rows with
            # ``unit_number=""``; downstream the runner synthesises
            # ``inferred_<sha16>`` ids from name+beds+baths+sqft. The
            # per-plan SSR page at ``/floorplans/<state>/<property>/<slug>
            # -<fpid>-<phase>/`` server-renders the real per-apartment
            # roster as ``.unit-card`` blocks — parsing those gives every
            # row a natural ``unit_number`` (e.g. "184", "8700") plus a
            # stable ``source_ids.entrata_uid``, closing the regression.
            #
            # When the drill produces at least one unit-card row,
            # ``pp_ssr_units`` is REPLACED — unit-level rows fully
            # supersede plan-level rows from the same property, so
            # keeping both would double-count. The drill is best-effort:
            # any per-plan fetch that errors / 404s is skipped, and an
            # empty drill leaves the plan-level rows intact.
            pp_unit_card_rows: list[dict[str, str]] = []
            seen_plan_urls: set[str] = set()
            for _idx_url, _idx_html in pp_ssr_index_bodies:
                plan_links = find_entrata_pp_plan_links(_idx_html, base)
                for plan_url in plan_links:
                    if plan_url in seen_plan_urls:
                        continue
                    seen_plan_urls.add(plan_url)
                    # Cap drill fan-out — the largest PP properties have
                    # ~30 plans; beyond that we trust the plan-level
                    # rows rather than incur the per-plan fetch cost.
                    if len(seen_plan_urls) > 30:
                        break
                    try:
                        plan_html = await _entrata_static_fetch(plan_url)
                    except Exception:
                        plan_html = ""
                    if not plan_html or not _pp_plan_page_has_units(plan_html):
                        continue
                    pp_unit_card_rows.extend(
                        parse_entrata_pp_unit_cards(plan_html, plan_url)
                    )

            # Also check the captured body itself in case link-hop
            # landed us directly on a per-plan page (the user-flagged
            # cohort — risewestarlington / foxlake — does exactly this).
            if (
                isinstance(fr_body_check, str)
                and fr_body_check
                and "unit-card" in fr_body_check
            ):
                _cap_url = str(getattr(fr, "final_url", "") or base)
                pp_unit_card_rows.extend(
                    parse_entrata_pp_unit_cards(fr_body_check, _cap_url)
                )

            # 2026-07-12: when the per-plan unit-card drill found nothing,
            # harvest the verbatim view_unit_spaces XHR URLs from the grid
            # bodies and replay them with the XHR header. This upgrades the
            # prospectportal-hosted plan grids to unit-level (the vanity-domain
            # grids carry no such buttons and harvest nothing — they keep
            # their plan-level row). See _harvest_view_unit_spaces.
            if not pp_unit_card_rows and pp_ssr_index_bodies:
                _vus_rows = await _harvest_view_unit_spaces(
                    pp_ssr_index_bodies, base
                )
                if _vus_rows:
                    pp_unit_card_rows.extend(_vus_rows)

            # 2026-07-16: newer Entrata vanity-domain "jd-fp-unit-card" widget.
            # A RENDERED /floorplans body carries real per-unit rows the older
            # .unit-card / view_unit_spaces paths don't recognise. Additive and
            # safe: the static (unrendered) grid has only --preload skeletons,
            # which parse_entrata_pp_jd_fp_cards filters out → no-op unless the
            # captured body was rendered (render tier / unlocker) and populated.
            if not pp_unit_card_rows:
                _jdfp_bodies = [b for _, b in pp_ssr_index_bodies]
                if isinstance(fr_body_check, str) and fr_body_check:
                    _jdfp_bodies.append(fr_body_check)
                _jdfp_url = str(getattr(fr, "final_url", "") or base)
                for _jb in _jdfp_bodies:
                    if "jd-fp-unit-card" not in _jb:
                        continue
                    pp_unit_card_rows.extend(
                        parse_entrata_pp_jd_fp_cards(_jb, _jdfp_url)
                    )

            # 2026-07-30 — MODERN prospect-portal theme (`var unitsData`).
            # Classes pricing-card / fee-transparency-wrapper / unit-cell; the
            # ``/{city}/{slug}/conventional/`` listing embeds the whole roster as
            # an escaped-JSON ``var unitsData = '{...}'`` object that none of the
            # DOM paths above recognise, so these properties ship plan-level.
            # Additive: fires only when every DOM drill found nothing. ONE fetch
            # covers identity + base rent + availability for every unit (no
            # per-plan fan-out). See parse_entrata_modern_units_data.
            if not pp_unit_card_rows:
                _mud_bodies: list[tuple[str, str]] = list(pp_ssr_index_bodies)
                if (
                    isinstance(fr_body_check, str)
                    and fr_body_check
                    and "unitsData" in fr_body_check
                ):
                    _mud_bodies.append(
                        (str(getattr(fr, "final_url", "") or base), fr_body_check)
                    )
                # No collected body carries the blob → harvest the /conventional/
                # index href from the landing page and fetch it once. The modern
                # listing lacks fp-card/fp-name-link, so Steps 1/3 discarded it.
                if not any("unitsData" in _b for _, _b in _mud_bodies):
                    for _mu_conv in _find_pp_conventional_index(
                        fr_body_check if isinstance(fr_body_check, str) else "",
                        base,
                    )[:2]:
                        try:
                            _mu_html = await _entrata_static_fetch(_mu_conv)
                        except Exception:
                            continue
                        if _mu_html and "unitsData" in _mu_html:
                            _mud_bodies.append((_mu_conv, _mu_html))
                            break
                from ma_poc.validation.unit_validity import has_dimension

                for _mu_url, _mb in _mud_bodies:
                    if "unitsData" not in _mb:
                        continue
                    # #90 guard (2026-07-30): admit only modern rows that carry
                    # a physical dimension. unitsData units always carry explicit
                    # beds/baths/sqft, so this is a no-op on real rosters — but a
                    # pathological dimensionless blob would otherwise REPLACE
                    # pp_ssr_units below and, failing post_process, orphan the
                    # plan-level baseline the property already had. Unlike the
                    # per-plan-drill sources, a modern (/conventional/) property's
                    # downstream WP (/floorplans/) + prospectportal paths target a
                    # different URL shape, so nothing re-derives the plan rows —
                    # the loss would be plan->FAILED, not plan->plan.
                    pp_unit_card_rows.extend(
                        _r
                        for _r in parse_entrata_modern_units_data(_mb, _mu_url)
                        if has_dimension(_r)
                    )
                    if pp_unit_card_rows:
                        break

            # Step 6 (2026-07-30, #91 Lever A template 2): ProspectPortal per-plan
            # ``/floor-plan/<type>/<design>.html`` pages embed schema.org JSON-LD
            # (a FloorPlan node + an ItemList of Apartment items) — unit-level
            # rent+sqft, one page per design. Unlike the SPA /floorplans/ grid,
            # these links live in the STATIC landing HTML, so discovery is
            # code-only. Fires only when every path above found nothing.
            # Verified: Ravel (3 units), Chase (1), Broadway. Sequential + capped
            # to respect the shared-host rate limiter.
            if not pp_unit_card_rows:
                from ma_poc.validation.unit_validity import has_dimension

                _fp_html_srcs = [_b for _, _b in pp_ssr_index_bodies]
                if isinstance(fr_body_check, str) and fr_body_check:
                    _fp_html_srcs.append(fr_body_check)
                _fp_html_links: list[str] = []
                _seen_fp: set[str] = set()
                for _src in _fp_html_srcs:
                    for _m in re.findall(
                        r'href="([^"]*?/floor-plan/[^"]*?\.html)"', _src
                    ):
                        _u = (
                            _m
                            if _m.startswith("http")
                            else base.rstrip("/") + "/" + _m.lstrip("/")
                        )
                        if _u not in _seen_fp:
                            _seen_fp.add(_u)
                            _fp_html_links.append(_u)
                for _fp_url in _fp_html_links[:25]:
                    try:
                        _fp_h = await _entrata_static_fetch(_fp_url)
                    except Exception:
                        continue
                    pp_unit_card_rows.extend(
                        _r
                        for _r in parse_entrata_floorplan_html_jsonld(_fp_h, _fp_url)
                        if has_dimension(_r)
                    )

            if pp_unit_card_rows:
                pp_ssr_units = pp_unit_card_rows

            if pp_ssr_units:
                from ma_poc.extraction.post_process import post_process

                _pps = post_process(
                    pp_ssr_units,
                    property_id=getattr(ctx, "property_id", None),
                )
                if _pps.n_admitted > 0:
                    result.units = _pps.admitted
                    result.plan_summaries = _pps.plan_summaries
                    # When the drill fired, attribute the winning URL to
                    # the first per-plan page we hit so downstream tier
                    # attribution / debug traces point at the right page.
                    if pp_unit_card_rows:
                        # Any pp_unit_card_rows source (per-plan drill,
                        # view_unit_spaces, jd-fp, or the modern unitsData
                        # roster) produces unit-level rows — credit the
                        # unit-level tier, not just the drill. winning_url
                        # points at the per-plan page when the drill fired,
                        # else the conventional/candidate index we parsed.
                        result.winning_url = (
                            next(iter(seen_plan_urls))
                            if seen_plan_urls
                            else (deep_candidates[0] if deep_candidates else base)
                        )
                        result.tier_used = (
                            "TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL"
                        )
                    else:
                        result.winning_url = (
                            deep_candidates[0] if deep_candidates else base
                        )
                        result.tier_used = "TIER_1_DOM_ENTRATA_PP_SSR"
                    result.confidence = min(
                        0.92, 0.7 + 0.04 * _pps.n_admitted
                    )
                    result.api_responses.append(
                        {
                            "url": result.winning_url or base,
                            "status": 200,
                            "body": (
                                "<entrata-pp-unit-cards>"
                                if pp_unit_card_rows
                                else "<entrata-pp-ssr-grid>"
                            ),
                            "via": (
                                "entrata_pp_unit_card_drill"
                                if pp_unit_card_rows
                                else "entrata_pp_ssr"
                            ),
                        }
                    )
                    return result
                result.errors.append(
                    f"ENTRATA_PP_SSR_VALIDITY_REJECTED: "
                    f"{len(pp_ssr_units)} parsed rows failed unit_validity"
                )

        if base:
            wp_units: list[dict[str, str]] = []
            # The captured fetch body may itself be a /floorplan/ detail
            # page; parse it first, then crawl siblings from the index.
            fr_body = getattr(fr, "body", None) if fr is not None else None
            if isinstance(fr_body, bytes):
                fr_body = fr_body.decode("utf-8", "replace")
            if isinstance(fr_body, str) and "available_units" in fr_body:
                wp_units.extend(
                    parse_entrata_available_units(
                        fr_body, str(getattr(fr, "final_url", "") or base)
                    )
                )
            links: list[str] = []
            for idx_path in ("/floorplans/", "/floor-plans/", "/"):
                try:
                    idx = await _entrata_static_fetch(base + idx_path)
                except Exception:
                    idx = ""
                links = find_entrata_fp_detail_links(idx, base)
                if links:
                    break
            for du in links[:30]:
                try:
                    dh = await _entrata_static_fetch(du)
                except Exception:
                    continue
                wp_units.extend(parse_entrata_available_units(dh, du))
            if wp_units:
                from ma_poc.extraction.post_process import post_process

                _ppw = post_process(
                    wp_units, property_id=getattr(ctx, "property_id", None)
                )
                if _ppw.n_admitted > 0:
                    result.units = _ppw.admitted
                    result.plan_summaries = _ppw.plan_summaries
                    result.winning_url = base + "/floorplans/"
                    result.tier_used = "TIER_1_DOM_ENTRATA_WP"
                    result.confidence = min(0.92, 0.7 + 0.04 * _ppw.n_admitted)
                    result.api_responses.append(
                        {
                            "url": base + "/floorplans/",
                            "status": 200,
                            "body": "<entrata-wp-available-units>",
                            "via": "entrata_wp_probe",
                        }
                    )
                    return result

        # ProspectPortal `check_availability` fallback (2026-05-18).
        # Marketing shell links to <sub>.prospectportal.com; the real
        # unit list is the CF-fronted view_unit_spaces GET. Stateless,
        # server-side via probe_get (cost-gated Web-Unlocker clears the
        # CF challenge). Guarded/additive — only runs after every other
        # path produced nothing; returns silently if not a PP site.
        try:
            pp_units = await self._probe_prospectportal(ctx)
        except Exception as exc:  # noqa: BLE001 — never raise from an adapter
            pp_units = []
            result.errors.append(
                f"prospectportal-probe-error: {type(exc).__name__}: {str(exc)[:90]}"
            )
        if pp_units:
            from ma_poc.extraction.post_process import post_process

            _ppp = post_process(
                pp_units, property_id=getattr(ctx, "property_id", None)
            )
            if _ppp.n_admitted > 0:
                result.units = _ppp.admitted
                result.plan_summaries = _ppp.plan_summaries
                result.tier_used = "TIER_1_DOM_ENTRATA_PROSPECTPORTAL"
                result.confidence = min(0.92, 0.7 + 0.04 * _ppp.n_admitted)
                return result

        # 2026-05-27 — LeaseLeads-embed fallback (additive). When an upstream
        # detector misroutes a LeaseLeads-shell property to Entrata (the
        # marketing site loads a ``LeaseLeads`` virtual-leasing-agent JS
        # bundle that emits Entrata-ish fingerprints), the primary Entrata
        # path produces zero units. The site itself embeds a public
        # LeaseLeads iframe of the form
        # ``https://embed.leaseleads.co/<uuid>/floor-plans`` whose backing
        # JSON API (``api.leaseleads.co/api/v2/property/<uuid>/floor-plans``)
        # is auth-free. Wired via the shared ``recover_leaseleads_embed``
        # helper that powers the Squarespace/Wix universal-recovery chain.
        # Live-verified against Lumina (pid 72391, liveatlumina.com).
        # Runs BEFORE the empty-exit label below so a successful
        # LeaseLeads recovery returns SUCCESS instead of TIER_*_EMPTY.
        # Bound unconditionally: the failure classifier below reads this to
        # tell a blocked FETCH from a missing XHR, and `page` is None on the
        # body-only paths (and in tests), which previously left it unassigned.
        html = ""
        if not result.units and page is not None:
            try:
                html = await page.content()
            except Exception:
                html = ""
            if html and _LEASELEADS_HTML_SIGNAL_RE.search(html):
                from ma_poc.pms.adapters._leaseleads_embed import (
                    recover_leaseleads_embed,
                )

                try:
                    ll_units = await recover_leaseleads_embed(page, ctx)
                except Exception:
                    ll_units = []
                if ll_units:
                    # may13: post_process classifies leaseleads units as
                    # plan-level (different promotion logic than the chip
                    # base 442c559). Skip post_process — assign units
                    # directly. The helper's output is already validated.
                    result.units = list(ll_units)
                    result.tier_used = "TIER_1_API_LEASELEADS"
                    result.confidence = min(0.93, 0.7 + 0.04 * len(ll_units))
                    return result

        # 2026-05-20: emit a structured empty-exit label so Path B retry
        # can route this property to its next-best PMS candidate. The
        # default ``TIER_1_API_ENTRATA`` initial value on line 424 was
        # the SUCCESS label — leaving it on a 0-unit exit meant
        # ``is_empty_exit()`` returned False and the orchestrator never
        # retried with a different adapter. Same bug-shape as the OneSite
        # cluster #6 fix (`5a7a676`) and the Equity sub-cluster fix
        # (`bffb24e`).
        #
        # Verified live 2026-05-20 against 4 of the 193 ENTRATA failed-
        # strict properties from canary sw9p4 (Centennial Place,
        # Foxchase, Hills at North Mesa, Grove Luxury) — all have
        # ``commoncf.entrata.com`` or ``application_authentication`` URL
        # markers but no real Entrata inventory backing; the detector
        # picks Entrata at 0.85 based on the marker, the adapter runs
        # empty, and Path B should re-dispatch to the next candidate.
        tier_code, err_msg = _classify_entrata_failure(api_responses, html)
        result.tier_used = tier_code
        result.confidence = 0.0
        result.errors.append(err_msg)

        return result

    async def _probe_prospectportal(
        self, ctx: AdapterContext
    ) -> list[dict[str, str]]:
        """Discover <sub>.prospectportal.com + property/floorplan ids,
        then per-floorplan ``view_unit_spaces`` via probe_get (+WU for
        the Cloudflare challenge). Never raises; [] when not a PP site.
        """
        fr = getattr(ctx, "fetch_result", None)
        body = getattr(fr, "body", None) if fr is not None else None
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        seed = body if isinstance(body, str) else ""
        seed = (seed or "") + " " + (getattr(ctx, "base_url", "") or "")
        mh = _PP_HOST_RE.search(seed)
        if not mh:
            return []
        portal = f"https://{mh.group(1)}.prospectportal.com"

        # The PP check_availability landing lists every floorplan
        # (data-floorplan / property_floorplan[id]) and carries
        # property[id]. probe_get auto-escalates to Web-Unlocker on the
        # CF challenge shell.
        #
        # 2026-05-26 cost audit: PP adapter was consuming ~30 WU calls/property
        # (1 landing + 29 per-plan). Cap floor-plan loop at _PP_FP_WU_CAP (8)
        # and allow WU for all calls on the same CF-protected domain.  This
        # keeps the cost at 1+8=9 WU calls/PP property vs the old 30.
        _PP_FP_WU_CAP = 8
        landing = ""
        try:
            r = await _entrata_static_fetch(
                portal + "/?module=check_availability&is_secure=1"
            )
            landing = r or ""
        except Exception:
            landing = ""
        hay = landing or seed
        mp = _PP_PROPID_RE.search(hay)
        if not mp:
            return []
        prop_id = mp.group(1)
        fp_ids: list[str] = []
        for m in _PP_FPID_RE.finditer(hay):
            fid = m.group(1)
            if fid and fid not in fp_ids:
                fp_ids.append(fid)
        if not fp_ids:
            return []

        from datetime import date

        movein = date.today().isoformat()
        out: list[dict[str, str]] = []
        for fid in fp_ids[:_PP_FP_WU_CAP]:
            u = (
                f"{portal}/?module=check_availability&is_secure=1"
                f"&property[id]={prop_id}&action=view_unit_spaces"
                f"&property_floorplan[id]={fid}"
                f"&move_in_date={movein}&occupancy_type=conventional"
            )
            try:
                h = await _entrata_static_fetch(u)
            except Exception:
                continue
            out.extend(parse_prospectportal_unit_spaces(h, u))
        return out

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    @staticmethod
    def _origin_from_ctx(page: Page, ctx: AdapterContext) -> str:
        """Build the origin (scheme://host) for direct probes.

        Resolution order:
          1. ``ctx.fetch_result.final_url`` — post-redirect URL, the most
             reliable signal of where the page actually lives.
          2. ``page.url`` — Playwright session URL.
          3. ``ctx.base_url`` — initial CSV URL (may be pre-redirect).

        2026-05-13 (C2 Entrata teammate analysis): ~60% of TIER_1_API_ENTRATA
        failures (~133 properties) had the probe fire against the pre-redirect
        origin. Entry URL was e.g. ``elevatetosequoia.com``; the page actually
        landed on ``elevatetoriveroaks.com`` after a redirect. The probe hit
        the original host with the wrong content, returning empty. Preferring
        ``fetch_result.final_url`` fixes this.
        """
        candidate = ""
        # 1. fetch_result.final_url — definitive post-redirect URL.
        fr = getattr(ctx, "fetch_result", None)
        if fr is not None:
            candidate = str(getattr(fr, "final_url", "") or "")
        # 2. page.url — Playwright session URL.
        if not candidate:
            try:
                candidate = page.url or ""
            except Exception:
                candidate = ""
        # 3. ctx.base_url — CSV entry URL (last resort, may be pre-redirect).
        if not candidate:
            candidate = getattr(ctx, "base_url", "") or ""
        try:
            parsed = urlparse(candidate)
        except Exception:
            return ""
        if not parsed.scheme or not parsed.netloc:
            return ""
        return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))

    async def _probe_known_endpoints(
        self,
        page: Page,
        ctx: AdapterContext,
    ) -> list[dict[str, str]]:
        """Bug 9: hit the Entrata endpoint catalogue directly via fetch.

        Returns the first probe's parsed units. Probes never raise — a
        404/CORS/error simply moves to the next path.
        """
        origin = self._origin_from_ctx(page, ctx)
        if not origin:
            return []

        for path in _ENTRATA_PROBES:
            url = origin + path
            # Defensive: if the SDK doesn't expose evaluate (test stubs),
            # bail entire probe loop rather than misbehave.
            evaluate = getattr(page, "evaluate", None)
            if not callable(evaluate):
                return []
            try:
                payload = await evaluate(
                    "(u) => fetch(u, {credentials: 'include'}).then(r => r.ok ? r.json() : null).catch(() => null)",
                    url,
                )
            except Exception as exc:
                log.debug("Entrata probe failed url=%s err=%s", url, exc)
                continue
            if not payload:
                continue
            try:
                if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                    if any(
                        k in payload[0]
                        for k in ("floorplan-name", "no_of_bedroom", "square_footage")
                    ):
                        units = parse_entrata_floorplans(payload, url)
                        if units:
                            return units
                elif isinstance(payload, dict):
                    units = parse_entrata_widget_envelope(payload, url)
                    if units:
                        return units
                    fps = payload.get("floor_plans") if isinstance(payload, dict) else None
                    if isinstance(fps, list) and fps:
                        units = parse_entrata_floorplans(fps, url)
                        if units:
                            return units
            except Exception as exc:
                log.debug("Entrata probe parse failed url=%s err=%s", url, exc)
                continue
        return []
