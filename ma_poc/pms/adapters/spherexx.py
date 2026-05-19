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


_TIER_BASE = "TIER_1_API_SPHEREXX"
_TIER_NO_RESPONSE = f"{_TIER_BASE}_NO_RESPONSE"
_TIER_SHAPE_REJECTED = f"{_TIER_BASE}_SHAPE_REJECTED"
_TIER_PARSE_FAILED = f"{_TIER_BASE}_PARSE_FAILED"
# ZRS/spherexx server-rendered floorplan-detail path (no API iframe).
# chathamsquare/mirabella-class: units are server-rendered in an HTML
# table on /floorplans/<bed>/<plan>/ detail pages, NOT via the
# presentation.spherexx.app /api/unit iframe. Deterministic Tier-1.
_TIER_ZRS = "TIER_1_DOM_SPHEREXX_ZRS"
# Razz/myrazz embedded portal: "Happily Made by Razz" Vue SPA renders a
# per-unit list at /models with a labeled "Available <date>" column.
# Distinct from the presentation.spherexx.app /api iframe — no API XHR
# fires; units live only in the post-hydration DOM. Anchor on the stable
# ``wrap-model-item model-list`` container + label TEXT (the date leaf is
# an unclassed <div>, so class selectors are unsafe — same lesson as the
# AppFolio js-listing-* scare). Raw "May 19"/"Now" is passed through;
# schema_v2._format_date normalizes it (no-year→run year, Now→run date).
_TIER_RAZZ = "TIER_1_DOM_SPHEREXX_RAZZ"

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


def _beds_from_url_seg(seg: str) -> str:
    """``4bedroom`` → ``4``; ``studio`` → ``0``; else ''."""
    s = (seg or "").lower()
    if "studio" in s:
        return "0"
    m = re.search(r"\d+", s)
    return m.group(0) if m else ""


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

    r = probe_get(url, timeout=20)
    return r.text or "" if r.status_code == 200 else ""


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
        origin = ""
        fr = getattr(ctx, "fetch_result", None)
        if fr is not None:
            origin = str(getattr(fr, "final_url", "") or "")
        origin = origin or getattr(ctx, "base_url", "") or ""
        if origin:
            from urllib.parse import urlparse

            p = urlparse(origin)
            if p.scheme and p.netloc:
                base = f"{p.scheme}://{p.netloc}"
                try:
                    idx = await _zrs_fetch(base + "/floorplans/")
                except Exception:
                    idx = ""
                links = find_zrs_detail_links(idx, base)[:30]
                zrs_units: list[dict[str, str]] = []
                for du in links:
                    try:
                        dh = await _zrs_fetch(du)
                    except Exception:
                        continue
                    zrs_units.extend(parse_zrs_floorplan_detail(dh, du))
                if zrs_units:
                    from ma_poc.extraction.post_process import post_process

                    pp = post_process(
                        zrs_units,
                        property_id=getattr(ctx, "property_id", None),
                    )
                    if pp.n_admitted > 0:
                        result.units = pp.admitted
                        result.plan_summaries = pp.plan_summaries
                        result.winning_url = base + "/floorplans/"
                        result.confidence = min(0.92, 0.7 + 0.04 * pp.n_admitted)
                        result.tier_used = _TIER_ZRS
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
