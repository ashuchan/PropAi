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
        money = _MONEY_RE.findall(fee)
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


def _pp_iso(s: str) -> str:
    """``2026/05/17`` | ``2026-05-17`` → ``2026-05-17``; else ''."""
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", str(s or ""))
    if not m:
        return ""
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


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


async def _entrata_static_fetch(url: str) -> str:
    from ma_poc.pms.adapters._probe import probe_get

    r = probe_get(url, timeout=20)
    return (r.text or "") if r.status_code == 200 else ""


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

        result.confidence = 0.0
        result.errors.append("No Entrata floorplan data found in captured API responses")

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
        for fid in fp_ids[:30]:
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
