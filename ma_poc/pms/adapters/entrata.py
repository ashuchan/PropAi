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
    # widgets/ is the canonical Entrata endpoint confirmed by production traces —
    # it returns the full floor_plans + availability widget payload in one call.
    # The older module paths below fire on non-standard Entrata deployments.
    "/Apartments/module/widgets/",
    "/Apartments/module/floor_plans/",
    "/Apartments/module/availability_pricing/",
    "/Apartments/module/property_info/",
    "/api/floorplans",
    "/api/availability",
)

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


# 2026-05-13 port: aliases the producer may emit for the same move-in
# date field. Pre-port the adapter dropped this field entirely, leading
# to fleet-wide 0% available_date on TIER_1_API_ENTRATA properties
# (memory note on 2026-05-19 run). Empty when absent so adapters not
# yet emitting dates continue to produce identical output.
_ENTRATA_AVAIL_DATE_ALIASES: tuple[str, ...] = (
    "available_date",
    "availableDate",
    "availability_date",
    "move_in_date",
    "min_move_in_date",
    "date_available",
    "available_on",
    "first_available_date",
)


def _first_alias(item: dict[str, Any], aliases: tuple[str, ...]) -> str:
    """First non-empty string value among *aliases* in *item*; '' otherwise."""
    for k in aliases:
        v = item.get(k)
        if v:
            return str(v)
    return ""


def parse_entrata_floorplans(items: list[dict[str, Any]], url: str) -> list[dict[str, str]]:
    """Parse a flat list of Entrata floorplan dicts into standard unit dicts.

    2026-05-13 port: now extracts move-in date via the 7-alias lookup
    (``_ENTRATA_AVAIL_DATE_ALIASES``). schema_v2._format_date normalises
    the value downstream; ``make_unit_dict`` emits both ``availability_date``
    and ``available_date`` so the v2 reader picks it up regardless of
    which alias the producer used.
    """
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

        avail_dt = _first_alias(item, _ENTRATA_AVAIL_DATE_ALIASES)

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


# ────────────────────────────────────────────────────────────────────
# 2026-05-13 port: WordPress-embedded Entrata + ProspectPortal parsers.
# These are standalone functions callable from tests; orchestration
# wiring into ``EntrataAdapter.extract`` lands in a follow-up commit
# once the ``_probe.py`` curl_cffi helper is available
# (Commit 11 of MAY13_API_TIER_PORT_PLAN.md).
# ────────────────────────────────────────────────────────────────────


_AVAIL_UNITS_RE = re.compile(r'"available_units"\s*:\s*(\[)')
_FP_DETAIL_RE = re.compile(r"/floorplan/[a-z0-9][a-z0-9-]*/?", re.IGNORECASE)
_SLUG_BB_RE = re.compile(r"(\d+)\s*br[\s_-]*(\d+(?:\.\d+)?)\s*ba", re.IGNORECASE)


def _iso_date(s: Any) -> str:
    """Normalize ``MM/DD/YYYY`` -> ``YYYY-MM-DD``; passthrough/'' otherwise."""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", str(s or ""))
    if not m:
        return ""
    mo, d, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _bracket_json(text: str, start: int) -> str | None:
    """Return the balanced ``[...]`` JSON array starting at *start*.

    Returns ``None`` if the array is malformed (never raises). Used by
    ``parse_entrata_available_units`` to slice an HTML-embedded JSON
    payload without depending on a regex that has to encode bracket
    balancing.
    """
    depth = 0
    for j in range(start, len(text)):
        c = text[j]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[start : j + 1]
    return None


def parse_entrata_available_units(html: str, url: str) -> list[dict[str, str]]:
    """Parse Entrata-WP per-floorplan-detail embedded ``available_units``.

    Entrata marketing sites (WordPress + ProspectPortal apply links)
    server-render an HTML-entity-encoded JSON blob on
    ``/floorplan/<slug>/`` detail pages::

        "available_units":[{"id","name","available_on","price",
                            "deposit","apply_url"}, ...]

    Each entry is a real unit (stable ``id`` + unit ``name`` + date +
    rent) -> deterministic Tier-1, no render needed. Beds/baths come
    from the floorplan slug (``1br-1ba-pennsylvania``).

    Never raises. Returns ``[]`` if the page lacks ``available_units``
    or the JSON is malformed.
    """
    if not html or "available_units" not in html:
        return []
    # Lazy-import the stdlib `html` module to avoid shadowing the
    # ``html`` parameter above.
    import html as _htmlmod
    import json

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


def find_entrata_fp_detail_links(index_html: str, origin: str) -> list[str]:
    """Return absolute ``/floorplan/<slug>/`` URLs from an Entrata-WP
    index page. Used by the WP fallback to crawl per-plan detail pages.

    Pure HTML parser -- no network. Never raises.
    """
    if not index_html or not origin:
        return []
    out: list[str] = []
    base = origin.rstrip("/")
    for m in _FP_DETAIL_RE.finditer(index_html):
        path = m.group(0)
        if not path.endswith("/"):
            path += "/"
        u = base + path
        if u not in out:
            out.append(u)
    return out


# ProspectPortal `view_unit_spaces` HTML-fragment parser. The marketing
# shell links to <sub>.prospectportal.com; the real unit list is GET
# ?module=check_availability&action=view_unit_spaces&property[id]=<pid>&
# property_floorplan[id]=<fpid> returning an HTML fragment with
# <a class="unit-button" data-*> rows. Stateless GET, Cloudflare-fronted.
_PP_HOST_RE = re.compile(
    r"https?://([a-z0-9][a-z0-9-]*)\.prospectportal\.com", re.IGNORECASE
)
_PP_PROPID_RE = re.compile(r"property\[id\][^0-9]{0,6}(\d{3,9})", re.IGNORECASE)
_PP_FPID_RE = re.compile(
    r"""(?:property_floorplan\[id\]|data-floorplan)["'\]=\s/]{1,4}(\d{4,9})""",
    re.IGNORECASE,
)


def _pp_iso(s: Any) -> str:
    """``2026/05/17`` | ``2026-05-17`` -> ``2026-05-17``; '' otherwise."""
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", str(s or ""))
    if not m:
        return ""
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def parse_prospectportal_unit_spaces(html: str, url: str) -> list[dict[str, str]]:
    """Parse a ProspectPortal ``view_unit_spaces`` HTML fragment.

    One row per ``<a class="unit-button" data-*>``. The ``data-*`` attrs
    are authoritative:
      * data-unit                       -- unit_space id
      * data-rent                       -- numeric rent
      * data-bedroom / data-bathroom    -- bed/bath counts
      * data-unitavailabilitydate       -- ISO-ish move-in date

    Visible unit number is the sibling ``.unit-col.unit .unit-col-text``.
    Floorplan name/sqft from the fragment header. Live-verified
    2026-05-18: springriver.prospectportal.com A1 fp 712595 -> 3 units
    at $1,291, 642 sqft.

    Never raises; returns ``[]`` if BeautifulSoup parse fails or the
    fragment lacks ``unit-button`` rows.
    """
    if not html or "unit-button" not in html:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover -- bs4 is in main deps
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        # lxml not installed or input not parseable -- fall back silently.
        return []

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
        row = a.find_parent(class_="unit-row-wrapper") or a.find_parent(class_="unit-row")
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
                availability_date=_pp_iso(str(a.get("data-unitavailabilitydate") or "")),
                source_api_url=url,
                extraction_tier="TIER_1_DOM_ENTRATA_PROSPECTPORTAL",
            )
        )
    return out


class EntrataAdapter:
    """Entrata PMS adapter. Parses /Apartments/module/widgets/ API responses."""

    pms_name: str = "entrata"
    _fingerprints: list[str] = ["entrata.com", "/Apartments/module/"]

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
                # D16: strict unit-level / plan-level partition.
                result.units = list(_pp.units)
                result.plan_summaries = list(_pp.plan_summaries)
                result.post_process_meta = _pp.to_meta()
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
                    # D16: strict unit-level / plan-level partition.
                    result.units = list(_pp_probe.units)
                    result.plan_summaries = list(_pp_probe.plan_summaries)
                    result.post_process_meta = _pp_probe.to_meta()
                    result.tier_used = "TIER_1_API_ENTRATA_PROBE"
                    result.confidence = min(0.95, 0.7 + 0.05 * _pp_probe.n_admitted)
                    return result
                result.errors.append(
                    f"ENTRATA_PROBE_VALIDITY_REJECTED: {len(probe_units)} probed rows "
                    f"failed unit_validity (no numeric dimension)"
                )

        # 2026-05-22 (restore from 8b1bfa4, reverted in 4c9dbf8): the
        # ``view_unit_spaces`` ProspectPortal probe. Many Entrata-detected
        # properties embed a ``<iframe src="https://<sub>.prospectportal.com/
        # ?module=guest_card&...&property[id]=<pid>">`` on the marketing
        # page. The iframe alone gives us (host, property_id)
        # deterministically — bare host root + module=check_availability
        # then lists every floorplan id. Per-fp ``view_unit_spaces`` GETs
        # return the unit table the parser at line 360 already knows how
        # to read. Cloudflare-fronted → probe_get's WU escalation.
        #
        # Live-verified 2026-05-22 on 5 sample PIDs (19939, 243704, 30775,
        # 34777, 297737) — every page had exactly one PP iframe with
        # property[id] baked in. WITHOUT a residential proxy this returns
        # CF challenge bodies; the path still emits telemetry so
        # production runs without PROBE_PROXY_URL surface the gate
        # explicitly.
        from ma_poc.pms.adapters._adapter_telemetry import log_adapter_stage

        _pid = str(getattr(ctx, "property_id", "") or "unknown")
        try:
            pp_units = await self._probe_prospectportal(ctx)
        except Exception as _pp_exc:
            log_adapter_stage(
                "entrata",
                _pid,
                "prospectportal_probe",
                f"exception:{type(_pp_exc).__name__}",
                reason=str(_pp_exc)[:140],
            )
            pp_units = []
        if pp_units:
            from ma_poc.extraction.post_process import post_process

            _pp = post_process(pp_units, property_id=getattr(ctx, "property_id", None))
            log_adapter_stage(
                "entrata",
                _pid,
                "prospectportal_probe",
                "ok" if _pp.n_admitted > 0 else "validity_rejected",
                units=_pp.n_admitted,
                reason=f"parsed={len(pp_units)}",
            )
            if _pp.n_admitted > 0:
                result.units = list(_pp.units)
                result.plan_summaries = list(_pp.plan_summaries)
                result.post_process_meta = _pp.to_meta()
                result.tier_used = "TIER_1_API_ENTRATA_PROSPECTPORTAL"
                result.confidence = min(0.93, 0.7 + 0.04 * _pp.n_admitted)
                return result
        else:
            log_adapter_stage(
                "entrata",
                _pid,
                "prospectportal_probe",
                "ran_empty",
                reason="no prospectportal iframe or probe returned 0 rows",
            )

        result.confidence = 0.0
        result.errors.append("No Entrata floorplan data found in captured API responses")

        # 2026-05-18 (bonus / tier-label leak): when no Entrata-shaped API
        # response was actually captured, downgrade the tier label so the
        # failures.csv bucket doesn't mislead triage. Yardi-served properties
        # that load Entrata widgets via rcommoncf.entrata.com CDN previously
        # showed up as TIER_1_API_ENTRATA despite zero Entrata API capture
        # (~80% of the TIER_1_API_ENTRATA + FAILED_NO_DATA bucket on 2026-05-18).
        # result.api_responses is the filtered list of Entrata-shaped captures;
        # empty means the adapter never saw a real Entrata payload.
        if not result.api_responses:
            result.tier_used = "TIER_1_API"

        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    async def _probe_prospectportal(self, ctx: AdapterContext) -> list[dict[str, str]]:
        """Restore from 8b1bfa4 (reverted by 4c9dbf8). When the marketing
        page embeds an ``<iframe src="https://<sub>.prospectportal.com/
        ?module=guest_card&action=create_guest_card&property[id]=<pid>...">``,
        the iframe alone yields (host, property_id) deterministically.

        Flow:
          1. Extract ``<sub>.prospectportal.com`` host from the entry HTML
             or ``ctx.base_url`` via ``_PP_HOST_RE``.
          2. Fetch ``https://<host>/?module=check_availability&is_secure=1``
             via curl_cffi (probe_get). This lists every floorplan id.
          3. Harvest ``property[id]`` (``_PP_PROPID_RE``) and all
             ``property_floorplan[id]`` / ``data-floorplan`` ids
             (``_PP_FPID_RE``).
          4. For each fp_id (up to 30), GET
             ``view_unit_spaces`` and parse the unit table via the
             already-existing ``parse_prospectportal_unit_spaces``.

        Never raises — [] when not a PP site or every probe fails. Cloudflare
        is fronted; ``probe_get`` auto-escalates to BrightData Web Unlocker
        when ``WEB_UNLOCKER_KEY`` is set on the cloud env.
        """
        fr = getattr(ctx, "fetch_result", None)
        body = getattr(fr, "body", None) if fr is not None else None
        if isinstance(body, bytes):
            try:
                body = body.decode("utf-8", errors="replace")
            except Exception:
                body = ""
        seed = body if isinstance(body, str) else ""
        seed = (seed or "") + " " + (getattr(ctx, "base_url", "") or "")
        mh = _PP_HOST_RE.search(seed)
        if not mh:
            return []
        portal = f"https://{mh.group(1)}.prospectportal.com"

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
        # Cap at 30 floorplans per property to bound proxy bandwidth.
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

    @staticmethod
    def _origin_from_ctx(page: Page, ctx: AdapterContext) -> str:
        """Bug 9: build the origin (scheme://host) for direct probes.

        Prefer ``page.url`` since redirects may have already settled there;
        fall back to ``ctx.base_url`` when the page object isn't useful.
        """
        candidate = ""
        try:
            candidate = page.url or ""
        except Exception:
            candidate = ""
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

        Bug #5 fix (2026-05-16): also probe ``candidate_portal_urls``
        passed in by the orchestrator. ``_extract_portal_iframe_hints``
        harvests ``comms.entrata.com/widget?website_token=...`` URLs from
        entry-page iframes, but the link-hop scheduler can't fetch them
        because they require the CF clearance cookie set on the entry
        load. ``page.evaluate(fetch, url, {credentials: 'include'})``
        inherits that cookie automatically. Filtered to Entrata-shaped
        hosts so a SightMap or AppFolio candidate URL doesn't accidentally
        get probed by the Entrata adapter.
        """
        origin = self._origin_from_ctx(page, ctx)
        if not origin:
            return []

        # Bug #5 fix: candidate-derived URLs first. These are higher
        # confidence than the catalogue (the entry page explicitly
        # referenced them via iframe src / inline JS) so probe them
        # before the generic ``/Apartments/module/widgets/`` fallback.
        candidate_urls: list[str] = []
        for u in getattr(ctx, "candidate_portal_urls", None) or []:
            try:
                u_low = str(u).lower()
            except Exception:
                continue
            if not u_low.startswith("http"):
                continue
            # Entrata-shaped: comms.entrata.com widget endpoints, or
            # /Apartments/module/widgets/ on the property's own host.
            if "comms.entrata.com" in u_low or "/apartments/module/widgets" in u_low:
                if u not in candidate_urls:
                    candidate_urls.append(u)

        probe_urls = candidate_urls + [origin + path for path in _ENTRATA_PROBES]

        for url in probe_urls:
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
