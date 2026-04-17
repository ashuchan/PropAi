"""
San Artes Apartments Scottsdale — Production Playwright Scraper
================================================================
Target: https://sanartesapartmentsscottsdale.com  (Entrata/Mark-Taylor hosted)

Extraction strategy (in priority order):
  Tier 1 — API Interception  : capture XHR/fetch JSON during page load
  Tier 2 — JSON-LD           : parse <script type="application/ld+json"> blocks
  Tier 3 — DOM Parsing       : CSS selectors on rendered Entrata DOM
  Tier 4 — LLM fallback      : (stub — plug in Azure OpenAI if needed)

Pipeline:
  1. Launch browser with network interception active
  2. Load homepage → collect all internal links
  3. Identify property/floor-plan links (heuristic + known paths)
  4. Crawl each link, capturing API calls & clicking interaction buttons
  5. Try extraction tiers in order
  6. Deduplicate units by floor plan name + bed/sqft fingerprint
  7. Output: JSON (full detail) + CSV (tabular)

Usage:
    pip install playwright
    playwright install chromium
    python san_artes_scraper.py

    # With explicit URL (default: San Artes):
    python san_artes_scraper.py --url https://sanartesapartmentsscottsdale.com

    # With proxy (recommended for production):
    python san_artes_scraper.py --proxy http://user:pass@proxy.brightdata.com:22225
"""

import argparse
import asyncio
import csv
import json
import re
import sys
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Load .env early so API keys are available before any LLM provider is
# instantiated (needed when Tier 6/7 LLM calls are triggered).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from playwright.async_api import BrowserContext, Page, async_playwright

# Make ma_poc/ root importable so lazy imports like `from services.X` and
# `from llm.factory` work regardless of which directory the script is invoked from.
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent  # ma_poc/
for _p in (_SCRIPT_DIR, _PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Windows console defaults to cp1252 and will crash on emoji prints.
# Force UTF-8 on stdout/stderr if the stream supports reconfigure().
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

# Hard cap so sub-link discovery cannot crawl forever on large sites.
# Reduced from 40 → 10: most unit data arrives via Tier 1 API interception
# during the homepage load. Crawling dozens of sub-pages rarely adds units
# but easily causes 180s timeouts on slow sites.
MAX_CRAWL_PAGES = 10

# ── Configuration ─────────────────────────────────────────────────────────────

DEFAULT_URL = "https://sanartesapartmentsscottsdale.com"

# Entrata known API patterns (San Artes / Mark-Taylor)
ENTRATA_API_PATTERNS = [
    "/api/",
    "/availabilities",
    "/floor-plans",
    "floorplan",
    "availability",
    "/pricing",
    "/units",
    "/apartments",
    "getFloorPlans",
    "getAvailabilities",
    "propertyInfo",
]

# Pages to always crawl on Entrata sites
ENTRATA_PRIORITY_PATHS = [
    "/floor-plans",
    "/floorplans",
    "/apartments",
    "/availability",
    "/vacancies",            # vacancy listing pages (e.g. projectmanagementinc.net)
    "/rent",
    "/conventional",        # Entrata "conventional" lease page
]

# Buttons to click to reveal unit data
EXPAND_BUTTON_PATTERNS = [
    r"available\s+unit",
    r"view\s+unit",
    r"see\s+unit",
    r"check\s+avail",
    r"floor\s+plan",
    r"show\s+more",
    r"view\s+all",
    r"view\s+availability",
    r"see\s+availability",
    r"check\s+availability",
    r"view\s+floor",
    r"see\s+floor",
    r"view\s+pricing",
    r"see\s+pricing",
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# ── URL helpers ────────────────────────────────────────────────────────────────

def _norm_host(host: str) -> str:
    return host.lower().lstrip(".").removeprefix("www.")

def normalise_url(base: str, href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
        return None
    parsed = urllib.parse.urlparse(href)
    if parsed.scheme in ("http", "https"):
        base_host = _norm_host(urllib.parse.urlparse(base).netloc)
        link_host = _norm_host(parsed.netloc)
        # Same host or subdomain — strict suffix match, not substring.
        if link_host != base_host and not link_host.endswith("." + base_host):
            return None
        return href
    if parsed.scheme:
        # Non-http scheme we don't know how to crawl.
        return None
    return urllib.parse.urljoin(base, href)

def is_property_link(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    keywords = [
        "floor-plan", "floorplan", "apartment", "unit", "bedroom",
        "studio", "availability", "lease", "rent", "plan",
        "conventional", "pricing", "available",
    ]
    return any(k in path for k in keywords)


# Links whose anchor text suggests they lead to unit/availability data.
# Used for anchor-text-based discovery (complements URL-path matching).
_AVAILABILITY_ANCHOR_RE = re.compile(
    r"view\s+availab|see\s+availab|check\s+availab"
    r"|view\s+floor|see\s+floor|floor\s*plan"
    r"|view\s+pricing|see\s+pricing"
    r"|view\s+unit|see\s+unit|view\s+apartment"
    r"|availab\w*\s+unit|available\s+apartment"
    r"|view\s+all\s+unit|see\s+all\s+unit",
    re.IGNORECASE,
)


# Links to exclude from the fallback exploratory crawl.
# These pages never contain unit/availability data.
_NON_PROPERTY_PATH_RE = re.compile(
    r"/(?:"
    r"contact|help|support|faq|about|career|job|press|news|blog|media"
    r"|privacy|terms|legal|policy|cookie|accessibility|sitemap"
    r"|login|sign-?in|register|account|profile|password|reset"
    r"|gallery|photo|video|virtual-tour|tour|3d"
    r"|amenit|neighborhood|area|location|direction|map|nearby"
    r"|resident|portal|pay|maintenance|apply|application"
    r"|review|testimonial|team|staff|management"
    r"|pet|parking|storage|gym|pool|fitness"
    r"|schedule|appointment|call|request"
    r"|thank|confirm|success|error|404|403"
    r"|wp-content|wp-admin|wp-json|assets|static|cdn"
    r"|\.pdf|\.jpg|\.png|\.svg|\.gif|\.css|\.js"
    r")(?:/|$|\?|#)",
    re.IGNORECASE,
)


def is_exploratory_candidate(url: str, base_url: str) -> bool:
    """Return True if a link is worth visiting during fallback exploratory crawl.

    Accepts same-host links that are NOT obviously non-property pages.
    This is intentionally permissive — the fallback only runs when all
    targeted crawling has failed, so casting a wider net is appropriate.
    """
    parsed = urllib.parse.urlparse(url)
    base_parsed = urllib.parse.urlparse(base_url)
    # Must be same host.
    if _norm_host(parsed.netloc) != _norm_host(base_parsed.netloc):
        return False
    path = parsed.path.lower()
    # Skip homepage (already visited).
    if path in ("", "/", base_parsed.path.lower()):
        return False
    # Skip links already matched by is_property_link (already in the queue).
    if is_property_link(url):
        return False
    # Reject known non-property pages.
    if _NON_PROPERTY_PATH_RE.search(path):
        return False
    return True

# Hosts whose API responses are never apartment data — these get captured
# because their URLs happen to match broad patterns like "/api/" or "/units".
_FALSE_POSITIVE_HOSTS = {
    "googleapis.com", "maps.googleapis.com",
    "go-mpulse.net", "c.go-mpulse.net",
    "visitor-analytics.io", "visits.visitor-analytics.io",
    "google-analytics.com", "www.google-analytics.com",
    "googletagmanager.com", "www.googletagmanager.com",
    "doubleclick.net",
    "facebook.com", "connect.facebook.net",
    "hotjar.com",
    "sentry.io",
    # Chatbot / leasing assistant widgets — config & tour scheduling only
    "meetelise.com", "app.meetelise.com",
    "sierra.chat",
    "theconversioncloud.com", "api.theconversioncloud.com",
    # Lead-gen / referral / review widgets — no unit data
    "nestiolistings.com",
    "rentgrata.com", "api.rentgrata.com",
    "g5marketingcloud.com", "client-leads.g5marketingcloud.com",
    "g5-api-proxy.g5marketingcloud.com",
    # Accessibility widgets
    "userway.org", "api.userway.org",
    # Chat widgets
    "omni.cafe", "webchat.omni.cafe",
    # Entrata communications/chat widget API
    "comms.entrata.com",
}

# Subdomains of blocked hosts that actually serve unit/floor-plan data.
# Checked BEFORE _FALSE_POSITIVE_HOSTS — an allowlisted host is never blocked.
_ALLOWLIST_OVERRIDES = {
    "api.ws.realpage.com",          # /v2/property/{id}/floorplans, /v2/units
    "onlineleasing.realpage.com",   # leasing portal with unit data
}

# URL path fragments that are never unit/availability data.
_FALSE_POSITIVE_PATH_FRAGMENTS = {
    "/tag-manager/",
    "/mapsjs/",
    "/gen_204",
    "/analytics/",
    "/gtag/",
    "/pixel",
    "/beacon",
    # NOTE: /apartments/module/widgets/ is NOT blocked — some widget responses
    # (floorPlanWidget, availabilityWidget) contain real floor plan data.
    # Non-property widgets are filtered in _filter_entrata_widget_response().
    # Entrata chat/messaging widget endpoints
    "/widget/inbox_members",
    "/widget/contact",
    "/widget/messages",
    "/widget/conversations",
    "/widget/campaigns",
    # Tour scheduling (no unit data)
    "/tour/availabilities",
    # G5 lead forms and review widgets
    "/html_forms/",
    "/yext_reviews/",
    # RealPage blurb/marketing text endpoints (no unit data)
    "/blurb/v1/",
}


def looks_like_availability_api(url: str) -> bool:
    """Check if a URL looks like a property availability/units API call.

    Returns False for known false-positive hosts (Google Maps, analytics
    pixels, tag managers) that match broad patterns like '/api/' but never
    contain apartment data.
    """
    url_lower = url.lower()

    # Reject known false-positive hosts (unless allowlisted).
    try:
        host = urllib.parse.urlparse(url_lower).netloc
        if host not in _ALLOWLIST_OVERRIDES:
            for fp_host in _FALSE_POSITIVE_HOSTS:
                if host == fp_host or host.endswith("." + fp_host):
                    return False
    except Exception:
        pass

    # Reject known false-positive path fragments.
    for frag in _FALSE_POSITIVE_PATH_FRAGMENTS:
        if frag in url_lower:
            return False

    return any(p.lower() in url_lower for p in ENTRATA_API_PATTERNS)


# Entrata widget types that contain real floor plan / availability data.
_ENTRATA_PROPERTY_WIDGET_TYPES = {"floor_plans", "availability"}

# Entrata widget types that are known to NOT contain unit data.
_ENTRATA_NOISE_WIDGET_TYPES = {
    "custom", "directions", "events", "specials", "resident_login",
    "gallery", "contact", "reviews", "social", "blog", "amenities",
}


def _filter_entrata_widget_response(body: dict) -> dict | None:
    """Filter Entrata /Apartments/module/widgets/ responses.

    Returns the body unchanged if it contains floor plan / availability data,
    or None if it's a non-property widget (directions, gallery, etc.).
    """
    widget_name = body.get("widget_name", "")
    if widget_name in _ENTRATA_NOISE_WIDGET_TYPES:
        return None
    # Check if widget_data.content actually contains floor plan data
    # (not just layout config). The floor_plans widget has a nested
    # floor_plans list; the availability widget usually only has UI config.
    widget_data = body.get("widget_data", {})
    content = widget_data.get("content", {})
    if isinstance(content, dict):
        fp_section = content.get("floor_plans", {})
        if isinstance(fp_section, dict):
            fp_list = fp_section.get("floor_plans", [])
            if isinstance(fp_list, list) and fp_list:
                return body
        # Check for availability section with actual unit data
        avail_section = content.get("availability", {})
        if isinstance(avail_section, dict):
            avail_units = avail_section.get("units", [])
            if isinstance(avail_units, list) and avail_units:
                return body
    # Unknown widget with no recognisable floor plan data.
    if widget_name not in _ENTRATA_PROPERTY_WIDGET_TYPES:
        return None
    # Known property widget type but no data — still keep it for
    # the parser to attempt (it might have a different structure).
    return body


def _response_looks_like_units(body) -> bool:
    """Quick heuristic: does this API response body contain unit/floorplan data?

    Used for the early-exit decision after homepage load — if any captured
    API response has a list of 1+ dicts with rent-like or unit-like keys,
    we skip sub-page crawling.
    """
    _SIGNAL_KEYS = {
        "rent", "minRent", "maxRent", "min_rent", "max_rent",
        "price", "askingRent", "monthlyRent",
        "baseRent", "base_rent", "display_price", "startingPrice",
        "bedrooms", "beds", "bedRooms", "sqft", "squareFeet", "square_feet",
        "square_footage", "no_of_bedroom", "no_of_bathroom",
        "unitNumber", "unit_number", "unitId", "unit_id",
        "floorPlanName", "floor_plan_name", "floorplan_name", "floorplan-name",
        "availableDate", "available_date", "availableCount",
    }

    def _has_signals(lst: list) -> bool:
        if not lst or not isinstance(lst[0], dict):
            return False
        sample_keys = set(lst[0].keys())
        return len(sample_keys & _SIGNAL_KEYS) >= 2

    if isinstance(body, list):
        return _has_signals(body)
    if isinstance(body, dict):
        for v in body.values():
            if isinstance(v, list) and _has_signals(v):
                return True
            # One level deeper (e.g. {data: {units: [...]}}, {response: {floorplans: [...]}})
            if isinstance(v, dict):
                for vv in v.values():
                    if isinstance(vv, list) and _has_signals(vv):
                        return True
    return False

# ── Entrata API response parser ────────────────────────────────────────────────

def _get(d: dict, *keys) -> str:
    """Try multiple key names, return first non-empty string found.

    Handles nested rent/sqft objects like ``{rent: {min: 1351, max: 1351}}``
    by extracting the first numeric value from the nested dict.  Lists are
    skipped (caller should handle those separately).
    """
    for k in keys:
        v = d.get(k)
        if v is None or v == "" or v == [] or v == {}:
            continue
        if isinstance(v, list):
            continue
        # Nested dict: try to extract a scalar (e.g. rent: {min: X, max: Y}).
        if isinstance(v, dict):
            for sub_k in ("min", "low", "amount", "value", "effectiveRent",
                          "max", "high"):
                sv = v.get(sub_k)
                if sv is not None and sv != "":
                    return str(sv)
            continue
        return str(v)
    return ""

def _money_to_int(s: str) -> int | None:
    """Parse '$1,450', '1450.00', '1,450 USD' → 1450. Returns None on failure."""
    if not s:
        return None
    cleaned = re.sub(r"[^\d.]", "", s)
    if not cleaned or cleaned == ".":
        return None
    try:
        return int(float(cleaned))
    except ValueError:
        return None

def _find_list(obj, keys: tuple[str, ...]) -> list:
    """Return the first non-empty list found at any of the given keys in obj (dict only)."""
    if not isinstance(obj, dict):
        return []
    for k in keys:
        v = obj.get(k)
        if isinstance(v, list) and v:
            return v
    return []

def _parse_sightmap_payload(body, url: str) -> list[dict]:
    """
    SightMap (sightmap.com) dedicated parser.
    Joins data.units[] to data.floor_plans[] by floor_plan_id so each unit gets
    name/beds/baths from its floor plan plus price/sqft/availability from itself.
    """
    units_out: list[dict] = []
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return units_out

    raw_units = data.get("units") or []
    raw_fps   = data.get("floor_plans") or []
    if not isinstance(raw_units, list) or not raw_units:
        return units_out

    fp_by_id: dict[str, dict] = {}
    for fp in raw_fps if isinstance(raw_fps, list) else []:
        if isinstance(fp, dict) and fp.get("id") is not None:
            fp_by_id[str(fp["id"])] = fp

    for u in raw_units:
        if not isinstance(u, dict):
            continue
        fp = fp_by_id.get(str(u.get("floor_plan_id") or ""), {})

        price = u.get("price")
        price_i: int | None = None
        if isinstance(price, (int, float)) and price > 0:
            price_i = int(price)
        else:
            price_i = _money_to_int(str(u.get("display_price") or ""))

        area = u.get("area")
        if isinstance(area, (int, float)) and area > 0:
            sqft = str(int(area))
        else:
            sqft = str(u.get("display_area") or "").strip()

        beds = fp.get("bedroom_count")
        baths = fp.get("bathroom_count")
        name = fp.get("name") or fp.get("filter_label") or ""

        if beds == 0 or (isinstance(name, str) and "studio" in name.lower()):
            bed_label = "Studio"
        elif beds is not None:
            bed_label = f"{beds} Bedroom"
        else:
            bed_label = ""

        units_out.append({
            "floor_plan_name":    str(name),
            "bed_label":          bed_label,
            "bedrooms":           str(beds) if beds is not None else "",
            "bathrooms":          str(baths) if baths is not None else "",
            "sqft":               sqft,
            "unit_number":        str(u.get("unit_number") or u.get("label") or ""),
            "floor":              str(u.get("floor_id") or ""),
            "building":           str(u.get("building") or ""),
            "rent_range":         f"${price_i:,}" if price_i else (str(u.get("display_price") or "")),
            "deposit":            "",
            "concession":         str(u.get("specials_description") or ""),
            "availability_status":"AVAILABLE",  # SightMap only lists leasable inventory
            "available_units":    "1",
            "availability_date":  str(u.get("available_on") or u.get("display_available_on") or ""),
            "source_api_url":     url,
            "extraction_tier":    "TIER_1_API_SIGHTMAP",
        })
    return units_out

def parse_api_responses(api_responses: list[dict]) -> list[dict]:
    """
    Parse captured API JSON into normalised unit/floor-plan records.
    Handles SightMap (dedicated), Entrata, custom REST, and GraphQL-style responses.
    """
    units = []
    seen: set[str] = set()

    skipped_no_fields = 0  # candidates rejected by "no name/beds/sqft/rent" gate

    for resp in api_responses:
        url  = resp["url"]
        data = resp["body"]

        # ── Dedicated parsers by host ─────────────────────────────────────
        host = urllib.parse.urlparse(url).netloc.lower()
        if "sightmap.com" in host:
            sm_units = _parse_sightmap_payload(data, url)
            for u in sm_units:
                key = u["unit_number"] or f"{u['floor_plan_name']}|{u['sqft']}|{u['rent_range']}"
                if key and key not in seen:
                    seen.add(key)
                    units.append(u)
            if sm_units:
                print(f"    SightMap parser: {len(sm_units)} units from {url[:80]}")
                continue  # This payload fully handled.
            else:
                print(f"    SightMap parser: 0 units from {url[:80]}")

        # ── Generic envelope unwrap ───────────────────────────────────────
        candidates: list[dict] = []
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            LIST_KEYS = (
                "floorPlans", "floor_plans", "FloorPlans",
                "units", "Units",
                "apartments", "Apartments",
                "availabilities", "Availabilities",
                "results", "items",
            )
            candidates = _find_list(data, LIST_KEYS)
            if not candidates:
                inner = data.get("data") if isinstance(data.get("data"), dict) else None
                if not inner and isinstance(data.get("response"), dict):
                    inner = data.get("response")
                if isinstance(inner, dict):
                    candidates = _find_list(inner, LIST_KEYS)
                    # Two-level: {data: {results: {units: [...]}}}
                    if not candidates:
                        for v in inner.values():
                            if isinstance(v, dict):
                                candidates = _find_list(v, LIST_KEYS)
                                if candidates:
                                    break

        for item in candidates:
            if not isinstance(item, dict):
                continue

            name      = _get(item, "floorPlanName","floor_plan_name","name","planName",
                              "unitType","unit_type","title","FloorPlanName","floorplan_name",
                              "floorplan-name")
            rent_lo   = _get(item, "minRent","rent_min","min_rent","startingFrom","starting_rent",
                              "askingRent","rent","minPrice","startingPrice","MinRent",
                              "price","base_rent","baseRent","display_price","displayPrice",
                              "monthlyRent","monthly_rent")
            rent_hi   = _get(item, "maxRent","rent_max","max_rent","maxAskingRent","endingAt","MaxRent",
                              "max_price","maxPrice","price_max")
            beds      = _get(item, "bedrooms","beds","bedroom_count","numBedrooms",
                              "bd","Bedrooms","BedroomCount","bedroomCount","num_bedrooms",
                              "no_of_bedroom","no_of_bedrooms","bedroomNumber","BedroomNumber")
            baths     = _get(item, "bathrooms","baths","bathroom_count","numBathrooms",
                              "ba","Bathrooms","BathroomCount","bathroomCount","num_bathrooms",
                              "no_of_bathroom","no_of_bathrooms","bathroomNumber","BathroomNumber")
            sqft      = _get(item, "sqft","squareFeet","square_feet","minSqft",
                              "size","SquareFeet","Sqft","sqftMin","area","square_footage",
                              "squareFootage","display_area","displayArea")
            sqft_max  = _get(item, "maxSqft","sqftMax","squareFeetMax","SquareFeetMax","max_area")
            avail     = _get(item, "availableCount","available_count","numAvailable",
                              "unitsAvailable","AvailableCount","units_available")
            avail_dt  = _get(item, "availableDate","available_date","moveInDate",
                              "moveInReady","availableFrom","AvailableDate","NextAvailDate",
                              "available_on","availableOn","display_available_on","readyDate")
            status    = _get(item, "status","availability_status","leaseStatus","Status","unit_status")
            unit_num  = _get(item, "unitNumber","unit_number","unitId","unit_id","UnitNumber",
                              "label","display_unit_number","unitCode","unit_code")
            floor_num = _get(item, "floor","floorNumber","FloorNumber","floor_id","floorId")
            building  = _get(item, "building","buildingName","BuildingName","building_name")
            plan_type = _get(item, "floorPlanType","type","bedBath","BedBath")
            deposit   = _get(item, "deposit","securityDeposit","SecurityDeposit","security_deposit")
            concession= _get(item, "concession","special","promotion","Concession","Special",
                              "specials_description","specialsDescription")

            # Skip if we can't identify the record at all
            if not any([name, beds, sqft, rent_lo]):
                skipped_no_fields += 1
                continue

            # Deduplicate (unit_number takes priority, else floor plan fingerprint)
            dedup_key = unit_num or f"{name}|{beds}|{sqft}|{rent_lo}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            # Format rent — strip $/commas/currency codes before numeric parse.
            lo_i = _money_to_int(rent_lo)
            hi_i = _money_to_int(rent_hi)
            if lo_i is not None and hi_i is not None and lo_i != hi_i:
                rent_display = f"${lo_i:,} - ${hi_i:,}"
            elif lo_i is not None:
                rent_display = f"${lo_i:,}"
            else:
                rent_display = ""

            # Format sqft
            sqft_display = sqft
            if sqft and sqft_max and sqft != sqft_max:
                sqft_display = f"{sqft} - {sqft_max}"

            # Bed label
            if beds == "0" or (not beds and "studio" in (name or "").lower()):
                bed_label = "Studio"
            elif beds:
                bed_label = f"{beds} Bedroom"
            else:
                bed_label = plan_type or "?"

            units.append({
                "floor_plan_name":    name,
                "bed_label":          bed_label,
                "bedrooms":           beds,
                "bathrooms":          baths,
                "sqft":               sqft_display,
                "unit_number":        unit_num,
                "floor":              floor_num,
                "building":           building,
                "rent_range":         rent_display,
                "deposit":            deposit,
                "concession":         concession,
                "availability_status": status or ("AVAILABLE" if (avail and avail != "0") else ""),
                "available_units":    avail,
                "availability_date":  avail_dt,
                "source_api_url":     url,
                "extraction_tier":    "TIER_1_API",
            })

    # Drop floor-plan stub records (no rent, no unit#) if any real unit records exist.
    has_real = any(u.get("unit_number") or u.get("rent_range") for u in units)
    stub_count = 0
    if has_real:
        before = len(units)
        units = [u for u in units if u.get("unit_number") or u.get("rent_range")]
        stub_count = before - len(units)

    # Summary log — diagnose extraction failures from this line alone.
    print(f"    parse_api_responses: {len(api_responses)} APIs → "
          f"{len(units)} units extracted "
          f"(skipped: {skipped_no_fields} no-fields, {stub_count} stubs, "
          f"{len(seen) - len(units)} deduped)")

    return units

# ── JSON-LD parser ─────────────────────────────────────────────────────────────

TARGET_JSONLD_TYPES = {"Apartment", "ApartmentComplex", "Offer", "FloorPlan", "Residence", "SingleFamilyResidence"}


def _is_low_signal_units(units: list[dict]) -> bool:
    """True if every unit is missing both a rent and a unit_number/unit_id.

    Phase 3 tiers (JSON-LD, DOM, generic API parser) can return placeholder
    rows that pass a container/selector check but carry no rent and no unit
    identifier. Accepting such a result makes the pipeline stop before
    Phase 4 link exploration, which is where real unit data usually lives.
    """
    if not units:
        return True
    for u in units:
        has_rent = bool(
            u.get("rent_range") or u.get("market_rent_low")
            or u.get("market_rent_high") or u.get("rent")
        )
        has_id = bool(
            (u.get("unit_number") or "").strip()
            or (u.get("unit_id") or "").strip()
        )
        if has_rent or has_id:
            return False
    return True


def _units_below_expected(units: list[dict], expected: int | None,
                          floor_ratio: float = 0.2) -> bool:
    """True if ``units`` is materially smaller than the known expected count.

    Used to reject premature Phase 3 success when the prior run or the CSV
    says the property has N units and today's extractor returned <20% of N.
    Applies only when every unit in the result is also rent-less — a small
    but correctly-priced result may just reflect low availability.
    """
    if not expected or expected <= 0 or not units:
        return False
    if len(units) >= max(1, int(expected * floor_ratio)):
        return False
    # Only veto if rents are also missing — a 2-unit result with real rents
    # may legitimately be the only availability.
    return all(
        not (u.get("rent_range") or u.get("market_rent_low") or u.get("rent"))
        for u in units
    )

def _jsonld_type_matches(item: dict) -> bool:
    t = item.get("@type")
    if isinstance(t, list):
        return any(isinstance(x, str) and x in TARGET_JSONLD_TYPES for x in t)
    return isinstance(t, str) and t in TARGET_JSONLD_TYPES

def _jsonld_floor_size(item: dict) -> str:
    # schema.org floorSize may be a QuantitativeValue dict OR a plain number/string.
    fs = item.get("floorSize")
    if isinstance(fs, dict):
        v = fs.get("value", "")
        return str(v) if v not in (None, "") else ""
    if fs in (None, ""):
        return ""
    return str(fs)

def _walk_jsonld(node, out: list[dict]) -> None:
    """JSON-LD may nest matching items inside @graph, itemListElement, etc."""
    if isinstance(node, dict):
        if _jsonld_type_matches(node):
            out.append(node)
        for v in node.values():
            _walk_jsonld(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_jsonld(v, out)

async def extract_property_metadata(page: Page) -> dict:
    """
    Pull property-level metadata from the loaded page: OpenGraph tags, JSON-LD
    (ApartmentComplex / PostalAddress / GeoCoordinates / Organization), <title>,
    and a footer-text phone heuristic. All fields are best-effort and may be empty.
    """
    md: dict = {
        "title":       "",
        "h1":          "",
        "description": "",
        "site_name":   "",
        "name":        "",
        "address":     "",
        "city":        "",
        "state":       "",
        "zip":         "",
        "country":     "",
        "telephone":   "",
        "latitude":    None,
        "longitude":   None,
        "year_built":  None,
        "stories":     None,
        "total_units": None,
        "image_url":   None,
        "gallery_urls": [],
    }

    try:
        raw = await page.evaluate(
            """() => {
                const out = {ogs: {}, json_ld: [], title: document.title || '', h1: '', body_text: ''};
                const h1 = document.querySelector('h1');
                if (h1) out.h1 = (h1.innerText || '').trim();
                document.querySelectorAll('meta').forEach(m => {
                    const key = m.getAttribute('property') || m.getAttribute('name');
                    const val = m.getAttribute('content');
                    if (key && val) out.ogs[key] = val;
                });
                document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
                    const txt = (s.textContent || '').trim();
                    if (txt) out.json_ld.push(txt);
                });
                // Snapshot footer-ish text for phone-number scraping.
                const footer = document.querySelector('footer') || document.body;
                if (footer) out.body_text = (footer.innerText || '').slice(0, 8000);
                return out;
            }"""
        )
    except Exception:
        return md

    md["title"]       = (raw.get("title") or "").strip()
    md["h1"]          = (raw.get("h1") or "").strip()
    ogs               = raw.get("ogs") or {}
    md["description"] = (ogs.get("og:description") or ogs.get("description") or "").strip()
    md["site_name"]   = (ogs.get("og:site_name") or "").strip()
    if not md["name"]:
        md["name"] = md["site_name"] or (ogs.get("og:title") or "").strip() or md["title"]

    # Property hero image — prefer og:image, fall back to twitter:image.
    _hero = (ogs.get("og:image") or ogs.get("og:image:secure_url")
             or ogs.get("twitter:image") or ogs.get("twitter:image:src") or "").strip()
    if _hero:
        md["image_url"] = _hero
        md["gallery_urls"].append(_hero)

    # Lat/lng from common meta-tag conventions.
    for k in ("place:location:latitude", "geo.position", "ICBM", "og:latitude"):
        v = ogs.get(k)
        if not v:
            continue
        # geo.position / ICBM are typically "lat;lng" or "lat, lng".
        m = re.search(r"(-?\d+\.\d+)[\s,;]+(-?\d+\.\d+)", v)
        if m:
            md["latitude"]  = float(m.group(1))
            md["longitude"] = float(m.group(2))
            break
        try:
            md["latitude"] = float(v)
            lng = ogs.get("place:location:longitude") or ogs.get("og:longitude")
            if lng:
                md["longitude"] = float(lng)
            break
        except ValueError:
            continue

    # Walk JSON-LD for ApartmentComplex / Place / PostalAddress / GeoCoordinates.
    def _walk(node):
        if isinstance(node, dict):
            t = node.get("@type")
            types = t if isinstance(t, list) else ([t] if t else [])
            if any(isinstance(x, str) and x in (
                "ApartmentComplex", "Apartment", "Residence", "RealEstateListing",
                "Place", "LocalBusiness", "Organization", "Hotel", "LodgingBusiness"
            ) for x in types):
                if not md["name"] and isinstance(node.get("name"), str):
                    md["name"] = node["name"].strip()
                if not md["telephone"] and isinstance(node.get("telephone"), str):
                    md["telephone"] = re.sub(r"\s+", " ", node["telephone"]).strip()
                addr = node.get("address")
                if isinstance(addr, dict):
                    md["address"] = (addr.get("streetAddress") or md["address"] or "")
                    md["city"]    = (addr.get("addressLocality") or md["city"] or "")
                    md["state"]   = (addr.get("addressRegion") or md["state"] or "")
                    md["zip"]     = (addr.get("postalCode") or md["zip"] or "")
                    md["country"] = (addr.get("addressCountry") or md["country"] or "")
                geo = node.get("geo")
                if isinstance(geo, dict):
                    try:
                        if md["latitude"] is None and geo.get("latitude") is not None:
                            md["latitude"] = float(geo["latitude"])
                        if md["longitude"] is None and geo.get("longitude") is not None:
                            md["longitude"] = float(geo["longitude"])
                    except (TypeError, ValueError):
                        pass
                if not md["total_units"] and isinstance(node.get("numberOfRooms"), (int, float)):
                    md["total_units"] = int(node["numberOfRooms"])
                # JSON-LD image: string | {url: ...} | list of either.
                img = node.get("image") or node.get("photo")
                if img:
                    _urls: list[str] = []
                    for item in (img if isinstance(img, list) else [img]):
                        if isinstance(item, str) and item.strip():
                            _urls.append(item.strip())
                        elif isinstance(item, dict):
                            u = item.get("url") or item.get("contentUrl")
                            if isinstance(u, str) and u.strip():
                                _urls.append(u.strip())
                    if _urls and not md["image_url"]:
                        md["image_url"] = _urls[0]
                    for u in _urls:
                        if u not in md["gallery_urls"]:
                            md["gallery_urls"].append(u)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    for blob in raw.get("json_ld") or []:
        try:
            _walk(json.loads(blob))
        except (json.JSONDecodeError, ValueError):
            continue

    # Footer phone fallback (XXX-XXX-XXXX, (XXX) XXX-XXXX, etc.)
    if not md["telephone"]:
        m = re.search(r"(\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}", raw.get("body_text") or "")
        if m:
            md["telephone"] = m.group(0).strip()

    return md

def _jsonld_item_has_unit_signal(item: dict) -> bool:
    """True if a JSON-LD node has offers/pricing/rooms data — i.e. unit-level.

    ApartmentComplex blocks describing the *property* as a whole (just name,
    address, phone) routinely pass the @type filter but have no offers or
    numberOfRooms. Emitting them as units produces a garbage single-unit
    record and makes Phase 3 declare premature success.
    """
    offers = item.get("offers")
    if isinstance(offers, dict) and (
        offers.get("price") or offers.get("lowPrice") or offers.get("highPrice")
    ):
        return True
    if isinstance(offers, list) and offers:
        return True
    num_rooms = item.get("numberOfRooms")
    if isinstance(num_rooms, dict):
        num_rooms = num_rooms.get("value")
    try:
        if num_rooms not in (None, "") and float(num_rooms) > 0:
            return True
    except (TypeError, ValueError):
        pass
    if item.get("floorSize"):
        return True
    t = item.get("@type")
    t_list = t if isinstance(t, list) else [t]
    return any(x in ("Apartment", "FloorPlan", "Residence", "Offer",
                     "SingleFamilyResidence") for x in t_list if isinstance(x, str))


async def parse_jsonld(page: Page) -> list[dict]:
    """Tier 2: parse Schema.org JSON-LD blocks."""
    units: list[dict] = []
    try:
        blocks = await page.eval_on_selector_all(
            'script[type="application/ld+json"]',
            "els => els.map(e => e.textContent)"
        )
    except Exception as e:
        print(f"  JSON-LD selector error: {e}")
        return units

    for block in blocks:
        if not block or not block.strip():
            continue
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue

        matched: list[dict] = []
        _walk_jsonld(data, matched)

        for item in matched:
            # Quality gate: skip property-level shells (ApartmentComplex with
            # no offers/rooms/floorSize) — they're not units.
            if not _jsonld_item_has_unit_signal(item):
                continue

            name = item.get("name") or ""
            if not isinstance(name, str):
                name = str(name)

            offers = item.get("offers", {})
            lo_raw, hi_raw = "", ""
            if isinstance(offers, dict):
                lo_raw = str(offers.get("lowPrice") or offers.get("price") or "")
                hi_raw = str(offers.get("highPrice") or "")
            elif isinstance(offers, list) and offers:
                # Array of Offer objects — take min/max.
                prices = []
                for o in offers:
                    if isinstance(o, dict):
                        p = o.get("price") or o.get("lowPrice")
                        pi = _money_to_int(str(p) if p is not None else "")
                        if pi is not None:
                            prices.append(pi)
                if prices:
                    lo_raw = str(min(prices))
                    hi_raw = str(max(prices)) if max(prices) != min(prices) else ""

            lo_i = _money_to_int(lo_raw)
            hi_i = _money_to_int(hi_raw)
            if lo_i is not None and hi_i is not None and lo_i != hi_i:
                rent_range = f"${lo_i:,} - ${hi_i:,}"
            elif lo_i is not None:
                rent_range = f"${lo_i:,}"
            else:
                rent_range = ""

            num_rooms = item.get("numberOfRooms", "")
            if isinstance(num_rooms, dict):
                num_rooms = num_rooms.get("value", "")

            units.append({
                "floor_plan_name":    name,
                "bed_label":          "",
                "bedrooms":           str(num_rooms) if num_rooms not in (None, "") else "",
                "bathrooms":          "",
                "sqft":               _jsonld_floor_size(item),
                "unit_number":        "",
                "floor":              "",
                "building":           "",
                "rent_range":         rent_range,
                "deposit":            "",
                "concession":         "",
                "availability_status":"",
                "available_units":    "",
                "availability_date":  "",
                "source_api_url":     page.url,
                "extraction_tier":    "TIER_2_JSONLD",
            })
    return units

# ── DOM parser ────────────────────────────────────────────────────────────────

async def parse_dom(page: Page, base_url: str) -> list[dict]:
    """
    Tier 3: CSS-selector based DOM parsing.
    Handles Entrata, AppFolio, custom CMSes.
    """
    units = []

    # Selector cascade — try most specific to most generic
    CONTAINER_SELECTORS = [
        # Entrata standard
        ".fp-group",
        ".floorplan-item",
        ".floor-plan-card",
        ".fp-item",
        # Mark-Taylor / custom
        "[class*='FloorPlan']",
        "[class*='floorPlan']",
        "[class*='floor-plan']",
        "[class*='floorplan']",
        # Generic apartment
        ".apartment-item",
        ".unit-card",
        ".plan-card",
        "[data-floor-plan]",
        "[data-unit]",
        # Very generic — any article with a price in it
        "article",
        ".card",
        "li",
    ]

    cards = []
    matched_sel = None
    for sel in CONTAINER_SELECTORS:
        els = await page.query_selector_all(sel)
        # Must have multiple matching elements AND at least one with rent-like text
        if len(els) >= 2:
            for el in els[:5]:
                try:
                    txt = await el.inner_text()
                except Exception:
                    try:
                        txt = await page.evaluate("el => el.textContent || ''", el)
                    except Exception:
                        continue
                if re.search(r"\$\d{3,}", txt):
                    cards = els
                    matched_sel = sel
                    break
        if cards:
            break

    if not cards:
        print("  ⚠ DOM: no card containers found")
        return units

    print(f"  ✓ DOM: {len(cards)} cards matched with selector '{matched_sel}'")

    seen: set[str] = set()
    for card in cards:
        try:
            try:
                text = (await card.inner_text()).strip()
            except Exception:
                text = (await page.evaluate("el => el.textContent || ''", card)).strip()
            if not text or len(text) < 20:
                continue

            unit: dict = {
                "floor_plan_name":    "",
                "bed_label":          "",
                "bedrooms":           "",
                "bathrooms":          "",
                "sqft":               "",
                "unit_number":        "",
                "floor":              "",
                "building":           "",
                "rent_range":         "",
                "deposit":            "",
                "concession":         "",
                "availability_status":"",
                "available_units":    "",
                "availability_date":  "",
                "source_api_url":     page.url,
                "extraction_tier":    "TIER_3_DOM",
            }

            # Name: first heading-like element
            for name_sel in ["h1","h2","h3","h4",
                              "[class*='name']","[class*='title']","[class*='plan']"]:
                el = await card.query_selector(name_sel)
                if el:
                    t = (await el.inner_text()).strip()
                    if t and len(t) < 80:
                        unit["floor_plan_name"] = t
                        break

            # Bed/bath/sqft from text
            bed_m   = re.search(r"(\d+(?:\.\d)?)\s*(?:bed|bd|bedroom)s?", text, re.I)
            bath_m  = re.search(r"(\d+(?:\.\d)?)\s*(?:bath|ba)s?", text, re.I)
            sqft_m  = re.search(r"([\d,]+)\s*(?:sq\.?\s*ft|sqft|sf)\b", text, re.I)
            sqft2_m = re.search(r"([\d,]+)\s*[-–]\s*([\d,]+)\s*(?:sq\.?\s*ft|sqft|sf)", text, re.I)
            studio  = bool(re.search(r"\bstudio\b", text, re.I))

            unit["bedrooms"]  = bed_m.group(1) if bed_m else ("0" if studio else "")
            unit["bathrooms"] = bath_m.group(1) if bath_m else ""
            if sqft2_m:
                unit["sqft"] = f"{sqft2_m.group(1)} - {sqft2_m.group(2)}"
            elif sqft_m:
                unit["sqft"] = sqft_m.group(1).replace(",","")

            if studio:
                unit["bed_label"] = "Studio"
            elif unit["bedrooms"]:
                unit["bed_label"] = f"{unit['bedrooms']} Bedroom"

            # Rent
            rent_m = re.search(
                r"\$([\d,]+)(?:/mo)?(?:\s*[-–]\s*\$([\d,]+))?", text
            )
            if rent_m:
                lo = "$" + rent_m.group(1)
                hi = ("$" + rent_m.group(2)) if rent_m.group(2) else ""
                unit["rent_range"] = f"{lo} - {hi}" if hi else lo

            # Available count
            av_m = re.search(r"(\d+)\s+(?:available|units?\s+avail)", text, re.I)
            unit["available_units"] = av_m.group(1) if av_m else ""

            # Availability date
            date_m = re.search(
                r"(?:available|avail\.?|move.in)\s*(?:date|now|:)?\s*"
                r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s*\d{0,4})",
                text, re.I
            )
            if not date_m:
                date_m = re.search(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b", text)
            unit["availability_date"] = date_m.group(1).strip() if date_m else ""

            # Status
            if re.search(r"\bavailable\b", text, re.I):
                unit["availability_status"] = "AVAILABLE"
            elif re.search(r"\bwait.?list\b|\bcoming soon\b", text, re.I):
                unit["availability_status"] = "WAITLIST"
            elif re.search(r"\bunavailable\b|\bleased\b|\bnot available\b", text, re.I):
                unit["availability_status"] = "UNAVAILABLE"

            # Concession / special
            con_m = re.search(r"(\d+)\s+(?:week|month)s?\s+free", text, re.I)
            if con_m:
                unit["concession"] = con_m.group(0)

            # Unit number (if this is a unit-level card, not floor-plan-level)
            unum_m = re.search(r"(?:unit|apt\.?)\s*#?\s*([A-Z]?\d{2,4}[A-Z]?)", text, re.I)
            unit["unit_number"] = unum_m.group(1) if unum_m else ""

            # DOM-tier minimum: require a rent OR a unit number. Floor-plan
            # cards with just name/bed/sqft are not unit records and produce
            # misleading zero-rent rows if accepted. Fall through to Phase 4
            # so the real availability page / API can be discovered.
            if not (unit["rent_range"] or unit["unit_number"]):
                continue

            # Dedup
            key = unit["unit_number"] or f"{unit['floor_plan_name']}|{unit['bedrooms']}|{unit['sqft']}|{unit['rent_range']}"
            if key in seen:
                continue
            seen.add(key)

            units.append(unit)
        except Exception as e:
            print(f"  ⚠ Card error: {e}")

    return units

# ── Click expanders ────────────────────────────────────────────────────────────

async def click_expanders(page: Page, deep: bool = False):
    """Click 'View Available Units', 'See All', etc. — restricted to actually-clickable elements.

    When ``deep=True``, also clicks floor-plan cards/tabs to trigger per-plan
    availability APIs (required for sites like Elio at Lake Lena where each
    floor plan has a separate "Check Availability" action).
    """
    clickable = page.locator("button, a, [role='button'], input[type='button'], input[type='submit']")
    for pattern in EXPAND_BUTTON_PATTERNS:
        try:
            btns = await clickable.filter(has_text=re.compile(pattern, re.I)).all()
        except Exception:
            continue
        for btn in btns[:5]:
            try:
                if not await btn.is_visible():
                    continue
                label = (await btn.inner_text()).strip()[:60]
                print(f"    Clicking: {label!r}")
                await btn.click(timeout=3000, no_wait_after=True)
                await asyncio.sleep(1.5)
            except Exception:
                # Element may have detached, navigated, or been obscured — skip and continue.
                continue

    # Deep interaction: click individual floor-plan cards/tabs to trigger
    # their per-plan availability APIs or reveal per-plan unit tables.
    if deep:
        _FP_SELECTORS = (
            '[class*="floor-plan"] a, [class*="floor-plan"] button, '
            '[class*="floorplan"] a, [class*="floorplan"] button, '
            '.plan-card, .fp-card, [data-floor-plan], '
            '[class*="FloorPlan"] a, [class*="FloorPlan"] button, '
            '.floor-plan-card a, .floor-plan-card button'
        )
        try:
            fp_cards = await page.query_selector_all(_FP_SELECTORS)
            for card in fp_cards[:8]:
                try:
                    if not await card.is_visible():
                        continue
                    label = (await page.evaluate("el => el.textContent || ''", card)).strip()[:40]
                    if not label:
                        continue
                    print(f"    Deep-click (floor plan): {label!r}")
                    await card.click(timeout=3000, no_wait_after=True)
                    await asyncio.sleep(2.0)
                except Exception:
                    continue
        except Exception:
            pass

# ── Embedded JSON extraction (Tier 1.5) ──────────────────────────────────────

# Known JS globals where SSR frameworks embed page data.
_EMBEDDED_JS_GLOBALS = [
    "__NEXT_DATA__",           # Next.js
    "__INITIAL_STATE__",       # Redux SSR / generic SSR
    "__NUXT__",                # Nuxt.js
    "__remixContext",          # Remix
    "__APP_DATA__",            # Various
    "pageData",               # Custom CMS
    "__data__",                # Custom CMS
    "initialState",           # Generic
    "serverData",             # Generic
]

# Domains that host leasing portals inside iframes or via redirects.
_LEASING_PORTAL_DOMAINS = frozenset({
    "sightmap.com",
    "realpage.com",
    "loftliving.com",
    "on-site.com",
    "rentcafe.com",
    "entrata.com",
    "yardi.com",
    "smartrent.com",
    "onlineleasing.realpage.com",
})


async def extract_embedded_json(page: Page) -> list[dict]:
    """Tier 1.5: Extract unit/floor plan data from inline ``<script>`` tags and
    JavaScript global variables.

    Many SSR sites (Next.js, Nuxt.js, Remix, custom CMSes) embed structured
    data in the page as JS globals or ``<script type="application/json">``
    blocks rather than fetching it via XHR.  This function searches for those
    blobs and returns them in the same ``{url, body}`` shape as captured API
    responses so the downstream ``parse_api_responses`` pipeline can handle
    them transparently.

    Returns:
        List of dicts with ``url`` (synthetic, prefixed ``embedded:``) and
        ``body`` (parsed JSON object/array).
    """
    found: list[dict] = []

    # ── Strategy 1: Known JS global variables ─────────────────────────────
    for var in _EMBEDDED_JS_GLOBALS:
        try:
            raw = await page.evaluate(
                f"typeof window['{var}'] !== 'undefined'"
                f" ? JSON.stringify(window['{var}'])"
                f" : null"
            )
            if raw and len(raw) > 200:
                data = json.loads(raw)
                found.append({"url": f"embedded:js:{var}", "body": data})
                print(f"  📦 Embedded: window.{var} ({len(raw):,} chars)")
        except Exception:
            continue

    # ── Strategy 2: <script type="application/json"> blocks ───────────────
    # (Excluding ld+json — that's handled by Tier 2 parse_jsonld.)
    try:
        json_blocks = await page.evaluate("""() => {
            const scripts = document.querySelectorAll(
                'script[type="application/json"]'
            );
            return Array.from(scripts)
                .map(s => ({id: s.id || s.getAttribute('data-id') || '', text: s.textContent}))
                .filter(s => s.text && s.text.length > 200 && s.text.length < 1000000);
        }""")
        for block in (json_blocks or []):
            try:
                data = json.loads(block["text"])
                found.append({
                    "url": f"embedded:json-block:{block['id'] or 'anon'}",
                    "body": data,
                })
                print(f"  📦 Embedded: <script type=application/json> "
                      f"id={block['id']!r} ({len(block['text']):,} chars)")
            except (json.JSONDecodeError, ValueError):
                continue
    except Exception:
        pass

    # ── Strategy 3: Inline <script> containing JSON with unit keywords ────
    # Catches patterns like:  var floorPlans = [{...}, ...];
    #                         window.propertyData = {...};
    try:
        script_texts = await page.evaluate("""() => {
            const scripts = document.querySelectorAll('script:not([src]):not([type])');
            return Array.from(scripts)
                .map(s => s.textContent)
                .filter(t => t && t.length > 300 && t.length < 500000)
                .filter(t => /(?:floor.?plan|floorPlan|units|avail|rent|bedroom|sqft|pricing)/i.test(t));
        }""")
        for script_text in (script_texts or [])[:5]:
            # Try to extract JSON objects assigned to a variable.
            # Pattern: var/let/const X = {...}; or window.X = {...};
            for m in re.finditer(
                r"""(?:var|let|const|window\.)\s*(\w+)\s*=\s*"""
                r"""(\[\s*\{[\s\S]*?\}\s*\]|\{[\s\S]*?\})"""
                r"""\s*;""",
                script_text,
            ):
                var_name = m.group(1)
                json_str = m.group(2)
                if len(json_str) < 200:
                    continue
                try:
                    data = json.loads(json_str)
                    found.append({
                        "url": f"embedded:script-var:{var_name}",
                        "body": data,
                    })
                    print(f"  📦 Embedded: var {var_name} ({len(json_str):,} chars)")
                except (json.JSONDecodeError, ValueError):
                    # Regex-extracted snippet may not be valid JSON — expected.
                    continue
            if found:
                break
    except Exception:
        pass

    # ── Strategy 4: Evaluate common property-data variable names ──────────
    # Some sites assign data to variables that our regex in Strategy 3 can't
    # reliably extract (minified, multi-line, template literals).  Evaluate
    # them directly in the browser context.
    if not found:
        _PROPERTY_VARS = [
            "floorPlans", "floorplans", "floor_plans",
            "unitData", "units", "propertyData", "propertyInfo",
            "availableUnits", "apartmentData", "pricingData",
            "communityData", "buildingData",
        ]
        for var in _PROPERTY_VARS:
            try:
                raw = await page.evaluate(
                    f"typeof window['{var}'] !== 'undefined' && window['{var}'] !== null"
                    f" ? JSON.stringify(window['{var}'])"
                    f" : null"
                )
                if raw and len(raw) > 200:
                    data = json.loads(raw)
                    # Quick sanity check: should be a list of 2+ dicts or a dict with a list.
                    looks_useful = False
                    if isinstance(data, list) and len(data) >= 2 and isinstance(data[0], dict):
                        looks_useful = True
                    elif isinstance(data, dict):
                        for v in data.values():
                            if isinstance(v, list) and len(v) >= 2 and isinstance(v[0], dict):
                                looks_useful = True
                                break
                    if looks_useful:
                        found.append({"url": f"embedded:js:{var}", "body": data})
                        print(f"  📦 Embedded: window.{var} ({len(raw):,} chars)")
            except Exception:
                continue

    if found:
        print(f"  📦 Total embedded JSON blobs: {len(found)}")

    return found


async def detect_leasing_portals(page: Page) -> list[str]:
    """Detect leasing portal iframes or JS redirects on the current page.

    Returns a list of portal URLs that should be navigated to for API capture.
    Checks:
      1. ``<iframe src="...">`` pointing to known leasing portal domains
      2. Hidden ``<a>`` links to leasing portals (e.g. "Apply Now" buttons)
      3. Meta-refresh or JS-redirect targets captured during page load
    """
    portal_urls: list[str] = []

    # ── Check iframes ─────────────────────────────────────────────────────
    try:
        iframe_srcs = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('iframe[src]'))
                .map(f => f.src)
                .filter(s => s && s.startsWith('http'));
        }""")
        for src in (iframe_srcs or []):
            host = urllib.parse.urlparse(src).netloc.lower()
            for domain in _LEASING_PORTAL_DOMAINS:
                if domain in host:
                    portal_urls.append(src)
                    break
    except Exception:
        pass

    # ── Check "Apply Now" / "View Floor Plans" links to leasing portals ───
    try:
        leasing_links = await page.evaluate("""() => {
            const links = document.querySelectorAll(
                'a[href*="sightmap"], a[href*="realpage"], a[href*="rentcafe"],'
                + 'a[href*="loftliving"], a[href*="on-site.com"], a[href*="onlineleasing"]'
            );
            return Array.from(links).map(a => a.href).filter(h => h.startsWith('http'));
        }""")
        for href in (leasing_links or []):
            if href not in portal_urls:
                portal_urls.append(href)
    except Exception:
        pass

    # ── Check if current page.url is itself a leasing portal ──────────────
    # (can happen after a JS redirect that Playwright followed)
    current_host = urllib.parse.urlparse(page.url).netloc.lower()
    for domain in _LEASING_PORTAL_DOMAINS:
        if domain in current_host:
            # We're already on the portal — no need to navigate
            print(f"  🔍 Current page is a leasing portal: {page.url[:80]}")
            return []

    # ── Check for external property-domain links ─────────────────────────
    # Management company sites (e.g. willowbridgepc.com) often link to the
    # property's own domain (e.g. eliolakelena.com).  Detect these by
    # checking if any external link's domain contains a word from the page
    # title or property name (4+ chars, excluding common stop words).
    if not portal_urls:
        try:
            page_title = await page.title()
            _stop = {"the", "apartments", "apartment", "community", "communities",
                     "home", "homes", "welcome", "living", "residences",
                     "properties", "property", "www", "com", "net", "org"}
            name_words = [w.lower() for w in re.findall(r'[a-zA-Z]{4,}', page_title or '')
                         if w.lower() not in _stop]
            if name_words:
                all_links = await page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('a[href]'))
                        .map(a => ({href: a.href, text: (a.innerText || '').trim().substring(0, 80)}))
                        .filter(l => l.href.startsWith('http'));
                }""")
                current_domain = urllib.parse.urlparse(page.url).netloc.lower()
                for link_info in (all_links or []):
                    link_domain = urllib.parse.urlparse(link_info['href']).netloc.lower()
                    if link_domain == current_domain:
                        continue
                    for word in name_words:
                        if word in link_domain:
                            portal_urls.append(link_info['href'])
                            print(f"  External property domain detected: {link_info['href'][:100]}")
                            break
                    if portal_urls:
                        break  # Take only the first match
        except Exception:
            pass

    if portal_urls:
        print(f"  Found {len(portal_urls)} leasing portal / external property link(s):")
        for u in portal_urls[:5]:
            print(f"     {u[:120]}")

    return portal_urls


async def probe_entrata_api(page: Page, base_url: str) -> list[dict]:
    """For Entrata-hosted sites, try to fetch floor plan data via known API
    endpoints using the browser's session cookies.

    Entrata sites use POST-based APIs at ``/api/v1/propertyunits/`` with a
    ``method`` + ``params`` body.  The property ID is extracted from the page's
    HTML/JS globals.

    Returns API-response-shaped dicts (same format as captured responses).
    """
    found: list[dict] = []

    # ── Detect Entrata site ───────────────────────────────────────────────
    # Check if we're on an Entrata-hosted property site.
    is_entrata = False
    try:
        has_entrata_marker = await page.evaluate("""() => {
            // Entrata sites typically have these markers in the DOM
            const markers = [
                document.querySelector('meta[name*="entrata"]'),
                document.querySelector('link[href*="entrata"]'),
                document.querySelector('script[src*="entrata"]'),
                document.querySelector('[class*="entrata"]'),
                document.querySelector('#entrata-widget-container'),
            ];
            return markers.some(m => m !== null);
        }""")
        if has_entrata_marker:
            is_entrata = True
    except Exception:
        pass

    # Also detect by URL pattern: /Apartments/ is Entrata's standard URL prefix.
    if not is_entrata:
        try:
            page_html = await page.content()
            if "/Apartments/module/" in page_html or "entrata.com" in page_html.lower():
                is_entrata = True
        except Exception:
            pass

    if not is_entrata:
        return found

    print("  🏢 Entrata site detected — probing floor plan API")

    # ── Extract property/site ID from the page ────────────────────────────
    property_id = None
    try:
        property_id = await page.evaluate("""() => {
            // Check common Entrata ID locations
            const meta = document.querySelector('meta[name="entrata:property_id"]');
            if (meta) return meta.content;

            // Check URL patterns like /Apartments/module/application_NNN/
            const m = window.location.pathname.match(/\\/(\\d{3,8})\\b/);
            if (m) return m[1];

            // Check for Entrata config in global JS
            if (window.entrataConfig && window.entrataConfig.propertyId)
                return String(window.entrataConfig.propertyId);
            if (window.propertyId)
                return String(window.propertyId);

            // Search hidden inputs
            const input = document.querySelector(
                'input[name*="property_id"], input[name*="propertyId"], '
                + 'input[name*="PropertyId"]'
            );
            if (input) return input.value;

            // Search data attributes
            const el = document.querySelector('[data-property-id]');
            if (el) return el.getAttribute('data-property-id');

            return null;
        }""")
    except Exception:
        pass

    if not property_id:
        # Try extracting from any captured widget URLs (they often contain property IDs)
        try:
            page_url = page.url
            m = re.search(r"/(\d{4,8})(?:/|$|\?)", page_url)
            if m:
                property_id = m.group(1)
        except Exception:
            pass

    if not property_id:
        print("  ↳ Entrata: could not extract property ID — skipping API probe")
        return found

    print(f"  🏢 Entrata property ID: {property_id}")

    # ── Try known Entrata API endpoints ───────────────────────────────────
    # Use page.evaluate(fetch(...)) so the request carries session cookies.
    entrata_api_paths = [
        f"/api/v1/floorplans/{property_id}",
        f"/api/v1/propertyunits/{property_id}",
        "/api/v1/floorplans",
        "/api/v1/units",
    ]

    origin = urllib.parse.urlparse(base_url)
    api_base = f"{origin.scheme}://{origin.netloc}"

    for api_path in entrata_api_paths:
        api_url = api_base + api_path
        try:
            raw = await page.evaluate(f"""async () => {{
                try {{
                    const resp = await fetch('{api_url}', {{
                        headers: {{'Accept': 'application/json'}},
                        credentials: 'same-origin',
                    }});
                    if (!resp.ok) return null;
                    const ct = resp.headers.get('content-type') || '';
                    if (!ct.includes('json')) return null;
                    const body = await resp.json();
                    return JSON.stringify(body);
                }} catch(e) {{
                    return null;
                }}
            }}""")
            if raw and len(raw) > 100:
                data = json.loads(raw)
                found.append({"url": f"entrata-api:{api_path}", "body": data})
                print(f"  🏢 Entrata API hit: {api_path} ({len(raw):,} chars)")
        except Exception:
            continue

    # ── Try Entrata's POST-based widget API for floor plans ───────────────
    try:
        raw = await page.evaluate(f"""async () => {{
            try {{
                const resp = await fetch('{api_base}/api/v1/propertyunits/', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                    }},
                    credentials: 'same-origin',
                    body: JSON.stringify({{
                        method: {{
                            name: "getUnits",
                            version: "r1",
                            params: {{propertyId: "{property_id}"}}
                        }}
                    }})
                }});
                if (!resp.ok) return null;
                const ct = resp.headers.get('content-type') || '';
                if (!ct.includes('json')) return null;
                return await resp.text();
            }} catch(e) {{ return null; }}
        }}""")
        if raw and len(raw) > 100:
            data = json.loads(raw)
            found.append({"url": "entrata-api:POST /api/v1/propertyunits/", "body": data})
            print(f"  🏢 Entrata POST API hit: /api/v1/propertyunits/ ({len(raw):,} chars)")
    except Exception:
        pass

    if found:
        print(f"  🏢 Entrata API probe: {len(found)} response(s) captured")

    return found


# ── Main scraper ───────────────────────────────────────────────────────────────

def _proxy_config(proxy: str | None) -> dict | None:
    if not proxy:
        return None
    # Allow bare "host:port" by defaulting to http scheme.
    if "://" not in proxy:
        proxy = "http://" + proxy
    parsed = urllib.parse.urlparse(proxy)
    if not parsed.hostname or not parsed.port:
        print(f"  ⚠ Ignoring malformed proxy URL: {proxy}")
        return None
    cfg: dict = {"server": f"{parsed.scheme or 'http'}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        cfg["username"] = urllib.parse.unquote(parsed.username)
        cfg["password"] = urllib.parse.unquote(parsed.password or "")
    return cfg

async def _goto_robust(page: Page, url: str, timeout_ms: int = 45000) -> None:
    """
    `networkidle` hangs on sites with analytics polling or chat widgets — use
    `domcontentloaded` as the primary wait and then settle briefly.
    """
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        await page.wait_for_load_state("load", timeout=5000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    await asyncio.sleep(1.0)

# ── Helper functions for the 7-phase pipeline ────────────────────────────────


def filter_network_noise(
    api_responses: list[dict],
    profile: Any | None = None,
) -> list[dict]:
    """Filter API responses through three layers: global blocklist, profile
    blocklist, and unit-signal scoring. Returns filtered + sorted responses.
    """
    # Layer 1+3 are already handled by looks_like_availability_api() in the
    # response handler. Here we apply the profile-specific blocklist.
    if profile is None:
        return api_responses

    blocked_patterns: set[str] = set()
    api_hints = getattr(profile, "api_hints", None)
    if api_hints:
        for ep in getattr(api_hints, "blocked_endpoints", []):
            blocked_patterns.add(ep.url_pattern)

    if not blocked_patterns:
        return api_responses

    filtered = []
    for resp in api_responses:
        url = resp.get("url", "")
        if url in blocked_patterns:
            print(f"  -- Profile blocklist: skipping {url[:80]}")
            continue
        # Also check if the URL matches any blocked pattern as a substring
        blocked = False
        for pattern in blocked_patterns:
            if pattern in url:
                print(f"  -- Profile blocklist (substring): skipping {url[:80]}")
                blocked = True
                break
        if not blocked:
            filtered.append(resp)

    return filtered


def prioritize_links(
    internal_links: set[str],
    anchor_text_links: list[str],
    profile: Any | None = None,
    base_url: str = "",
    property_city: str | None = None,
) -> list[str]:
    """Sort and deduplicate links by priority for exploration.

    Priority order:
    1. Profile winning_page_url and availability_links
    2. Anchor text matches ("View Availability", etc.)
    3. URL keyword matches (is_property_link())
    4. Exploratory candidates
    Excludes profile.navigation.explored_links that previously had no data.
    """
    # Gather links to skip (previously explored with no data)
    skip_links: set[str] = set()
    if profile is not None:
        nav = getattr(profile, "navigation", None)
        if nav:
            skip_links.update(getattr(nav, "explored_links", []))

    result: list[str] = []
    seen: set[str] = set()

    def _add(url: str) -> None:
        if url and url not in seen and url not in skip_links:
            seen.add(url)
            result.append(url)

    # Priority 1: Profile-learned URLs
    if profile is not None:
        nav = getattr(profile, "navigation", None)
        if nav:
            winning = getattr(nav, "winning_page_url", None)
            if winning:
                _add(winning)
            for link in getattr(nav, "availability_links", []):
                _add(link)
            avail_path = getattr(nav, "availability_page_path", None)
            if avail_path and base_url:
                _add(base_url.rstrip("/") + avail_path)

    # Priority 2: Anchor text matches
    for link in anchor_text_links:
        _add(link)

    # Priority 3: Always-crawl priority paths
    for path in ENTRATA_PRIORITY_PATHS:
        url = base_url.rstrip("/") + path
        _add(url)
        # For vacancy-type pages, also try with city filter appended.
        if property_city and path == "/vacancies":
            _add(url + "?city=" + urllib.parse.quote(property_city))

    # Also check if the base domain (not the property sub-path) has a vacancies page.
    if property_city:
        parsed = urllib.parse.urlparse(base_url)
        domain_root = f"{parsed.scheme}://{parsed.netloc}"
        _add(domain_root + "/vacancies?city=" + urllib.parse.quote(property_city))

    # Priority 4: URL keyword matches from internal links
    for link in sorted(internal_links):
        if is_property_link(link):
            _add(link)

    # Priority 5: Exploratory candidates (cast wider net)
    for link in sorted(internal_links):
        if is_exploratory_candidate(link, base_url):
            _add(link)

    return result


def try_known_patterns(
    api_responses: list[dict],
    profile: Any | None = None,
) -> list[dict]:
    """Try to extract units using profile-learned patterns before global patterns.

    Checks:
    1. Profile known_endpoints with json_paths
    2. Profile llm_field_mappings (deterministic replay)
    3. Falls back to global parse_api_responses()

    Returns units list or [].
    """
    if not api_responses:
        return []

    captured_urls = {r.get("url", "") for r in api_responses}
    captured_by_url = {r.get("url", ""): r for r in api_responses}

    if profile is not None:
        api_hints = getattr(profile, "api_hints", None)
        if api_hints:
            # Check LLM field mappings first (most specific, learned from past LLM calls)
            for mapping in getattr(api_hints, "llm_field_mappings", []):
                pattern = mapping.api_url_pattern
                # Check if any captured URL matches this pattern
                for url in captured_urls:
                    if pattern in url or url in pattern:
                        resp = captured_by_url.get(url)
                        if resp and resp.get("body"):
                            try:
                                from services.llm_extractor import apply_saved_mapping
                                units = apply_saved_mapping(
                                    resp["body"],
                                    {
                                        "response_envelope": mapping.response_envelope,
                                        "json_paths": mapping.json_paths,
                                    },
                                )
                                if units:
                                    print(f"  ✅ Profile LLM mapping matched: {url[:80]} → {len(units)} units")
                                    return units
                            except Exception as e:
                                print(f"  ⚠ Profile mapping replay failed: {e}")

            # Check known endpoints
            for endpoint in getattr(api_hints, "known_endpoints", []):
                pattern = endpoint.url_pattern
                for url in captured_urls:
                    if pattern in url or url in pattern:
                        # Known endpoint found — try global parser on just this response
                        resp = captured_by_url.get(url)
                        if resp:
                            units = parse_api_responses([resp])
                            if units:
                                print(f"  ✅ Profile known endpoint matched: {url[:80]} → {len(units)} units")
                                return units

    # Fall back to global pattern matching
    units = parse_api_responses(api_responses)
    return units


def apply_availability_defaults(units: list[dict]) -> list[dict]:
    """Apply default availability status and date when missing.

    If units/floor plans are found but have no availability data:
    - Set availability_status to "AVAILABLE"
    - Set availability_date to today's date

    All US multifamily property sites have an availability section,
    so if we found the unit listed, it's available.
    """
    today_str = datetime.now(UTC).strftime("%Y-%m-%d")
    for unit in units:
        status = unit.get("availability_status")
        if not status or status == "UNKNOWN":
            unit["availability_status"] = "AVAILABLE"
        date_val = unit.get("available_date") or unit.get("availability_date")
        if not date_val:
            unit["available_date"] = today_str
            unit["availability_date"] = today_str
    return units


async def explore_link_with_observation(
    page: Page,
    link: str,
    api_responses: list[dict],
    base_url: str,
    deep_click: bool = False,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Navigate to a link, observe new network calls, try extraction.

    Returns:
        (units_found, new_api_responses, llm_candidates)
        - units_found: extracted units if any deterministic tier succeeded
        - new_api_responses: API responses captured during this navigation
        - llm_candidates: promising APIs that couldn't be parsed deterministically
    """
    api_count_before = len(api_responses)

    try:
        await _goto_robust(page, link, timeout_ms=20000)
    except Exception as e:
        err_str = str(e)
        if "Timeout" in err_str or "timeout" in err_str:
            print(f"  ⏱ TIMEOUT: {link[:70]}")
        else:
            print(f"  ⚠ Load error: {err_str[:120]}")
        return [], [], []

    await click_expanders(page, deep=deep_click)
    await asyncio.sleep(1.5)

    # Identify new API responses captured during this navigation
    new_responses = api_responses[api_count_before:]
    if new_responses:
        print(f"    📡 {len(new_responses)} new API responses on this page")

    llm_candidates: list[dict] = []

    # Try deterministic extraction on new responses
    if new_responses:
        units = parse_api_responses(new_responses)
        if units:
            return units, new_responses, []
        # Check for promising but unparsed responses
        for resp in new_responses:
            if _response_looks_like_units(resp.get("body")):
                llm_candidates.append(resp)

    # Try embedded JSON
    embedded_blobs = await extract_embedded_json(page)
    if embedded_blobs:
        api_responses.extend(embedded_blobs)
        units = parse_api_responses(embedded_blobs)
        if units:
            return units, new_responses + embedded_blobs, []

    # Try JSON-LD
    units = await parse_jsonld(page)
    if units:
        return units, new_responses, []

    # Try DOM parsing
    units = await parse_dom(page, base_url)
    if units:
        return units, new_responses, []

    return [], new_responses, llm_candidates


async def _extract_dom_sections_with_rent_signals(page: Page) -> list[str]:
    """Find DOM sections that contain rent/unit signals for targeted LLM analysis.

    Instead of sending the full page HTML, this identifies specific container
    sections that have price patterns, then extracts just those sections.
    Returns a list of HTML strings for the matching sections.
    """
    try:
        sections = await page.evaluate("""() => {
            const containers = document.querySelectorAll(
                'section, [class*="floor"], [class*="plan"], [class*="unit"], ' +
                '[class*="avail"], [class*="pricing"], [class*="rent"], ' +
                'main, [role="main"], .content, #content'
            );
            const results = [];
            const priceRe = /\\$\\d{3,}/;
            for (const el of containers) {
                const text = el.innerText || '';
                if (priceRe.test(text) && text.length > 100 && text.length < 50000) {
                    // Check it's not just a header with a single price
                    const priceCount = (text.match(/\\$\\d{3,}/g) || []).length;
                    if (priceCount >= 2) {
                        results.push(el.outerHTML.substring(0, 20000));
                    }
                }
                if (results.length >= 3) break;
            }
            return results;
        }""")
        return sections if isinstance(sections, list) else []
    except Exception as e:
        print(f"  ⚠ DOM section extraction error: {e}")
        return []


async def scrape(base_url: str, proxy: str | None = None,
                 profile: Any | None = None,
                 expected_total_units: int | None = None,
                 property_city: str | None = None) -> dict:
    # Normalize http → https.  Nearly all property sites support HTTPS;
    # plain HTTP causes redirect stalls (3-5s wasted per page) or hangs
    # entirely when the server forces HSTS but the redirect chain is slow.
    if base_url.startswith("http://"):
        base_url = "https://" + base_url[len("http://"):]

    # ── Profile-guided routing ───────────────────────────────────────────
    # Determines which tiers to skip based on past extraction success.
    # HOT profiles jump directly to the known-good tier.
    # WARM profiles try preferred tier first but cascade on failure.
    # COLD / no profile → full cascade, no skipping.
    skip_to_tier: int | None = None
    run_full_cascade: bool = True
    if profile is not None:
        try:
            from services.profile_router import route
            decision = route(profile)
            skip_to_tier = decision.skip_to_tier
            run_full_cascade = decision.run_full_cascade
            maturity = getattr(profile, "confidence", None)
            mat_label = getattr(maturity, "maturity", "COLD") if maturity else "COLD"
            if skip_to_tier:
                print(f"  📋 Profile: maturity={mat_label}, "
                      f"preferred_tier={skip_to_tier}, "
                      f"cascade={'yes' if run_full_cascade else 'no'}")
        except Exception as e:
            print(f"  ⚠ Profile routing failed: {e}")

    results = {
        "scraped_at":             datetime.now(UTC).isoformat(),
        "property_name":          urllib.parse.urlparse(base_url).netloc or base_url,
        "base_url":               base_url,
        "links_found":            [],
        "property_links_crawled": [],
        "api_calls_intercepted":  [],
        "units":                  [],
        "extraction_tier_used":   None,
        "_winning_page_url":      None,   # URL/endpoint that actually produced units
        "errors":                 [],
        # Populated by caller (daily_runner) so LLM interaction records can
        # carry the canonical property ID for cost accounting.
        "_property_id":           "unknown",
        # Interaction records from Tier 6 (LLM) and Tier 7 (Vision) calls.
        # Each element is a dict produced by llm.interaction_logger.make_interaction().
        "_llm_interactions":      [],
        # Profile routing metadata for debugging.
        "_profile_skip_to_tier":  skip_to_tier,
        "_profile_cascade":       run_full_cascade,
    }

    launch_args: dict = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-dev-shm-usage"],
    }
    context_args = {
        "user_agent": USER_AGENT,
        "viewport": {"width": 1280, "height": 900},
        "locale": "en-US",
    }
    proxy_cfg = _proxy_config(proxy)
    if proxy_cfg:
        launch_args["proxy"] = proxy_cfg

    api_responses: list[dict] = []
    seen_api_urls: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_args)
        context: BrowserContext = await browser.new_context(**context_args)

        try:
            # ── Network interception ───────────────────────────────────────
            async def handle_response(response):
                try:
                    url = response.url
                    if not looks_like_availability_api(url):
                        return
                    # Entrata widget endpoints return multiple distinct responses
                    # (one per widget type) on the same URL — don't deduplicate
                    # those, otherwise we'd miss the floor_plans widget.
                    is_widget_url = "/apartments/module/widgets/" in url.lower()
                    if url in seen_api_urls and not is_widget_url:
                        return
                    ct = (response.headers or {}).get("content-type", "").lower()
                    if "json" not in ct and not url.lower().endswith(".json"):
                        print(f"  -- API skipped (non-JSON content-type: {ct[:40]}): {url[:80]}")
                        return
                    if not (200 <= response.status < 300):
                        print(f"  -- API skipped (HTTP {response.status}): {url[:80]}")
                        return
                    body = await response.json()
                    seen_api_urls.add(url)

                    # Filter Entrata widget responses: keep floor_plans /
                    # availability widgets, discard noise (directions, gallery, etc.)
                    if "/apartments/module/widgets/" in url.lower() and isinstance(body, dict):
                        filtered = _filter_entrata_widget_response(body)
                        if filtered is None:
                            wn = body.get("widget_name", "?")
                            print(f"  -- Widget skipped ({wn}): {url[:80]}")
                            return
                        # Extract the floor_plans list from the widget wrapper so the
                        # generic parser can process it like a normal API response.
                        widget_data = body.get("widget_data", {})
                        content = widget_data.get("content", {})
                        fp_list = content.get("floor_plans", {})
                        if isinstance(fp_list, dict):
                            fp_list = fp_list.get("floor_plans", [])
                        if isinstance(fp_list, list) and fp_list:
                            body = fp_list
                            print(f"  📡 Widget floor_plans extracted: "
                                  f"{len(fp_list)} floor plans from {url[:80]}")

                    api_responses.append({"url": url, "body": body})
                    # Log body shape so failed extractions can be diagnosed.
                    body_hint = ""
                    if isinstance(body, dict):
                        body_hint = f"dict keys={list(body.keys())[:6]}"
                    elif isinstance(body, list):
                        body_hint = f"list[{len(body)}]"
                    print(f"  📡 API captured [{response.status}]: {url[:100]}  ({body_hint})")
                except Exception as exc:
                    # Body already consumed, non-JSON, navigation cancel, etc.
                    exc_str = str(exc)
                    if "was not received" not in exc_str and "Target closed" not in exc_str:
                        print(f"  -- API response error: {exc_str[:80]}  url={response.url[:60]}")

            page = await context.new_page()
            page.on("response", handle_response)

            # ── 1. Homepage — collect all links ───────────────────────────
            print(f"\n{'='*65}")
            print(f"  STEP 1: Load homepage — {base_url}")
            print(f"{'='*65}")
            _page_unreachable = False
            try:
                await _goto_robust(page, base_url, timeout_ms=60000)
            except Exception as e:
                msg = f"Homepage load error: {e}"
                print(f"  ⚠ {msg}")
                results["errors"].append(msg)
                # Mark page as unreachable so LLM/Vision phases are skipped
                # (no point analyzing a browser error page).
                err_str = str(e).lower()
                if any(sig in err_str for sig in (
                    "err_ssl", "err_name_not_resolved", "err_connection_refused",
                    "err_connection_timed_out", "err_too_many_redirects",
                    "err_cert", "net::err_", "timeout",
                )):
                    _page_unreachable = True
                    print(f"  ⛔ Page unreachable — will skip LLM/Vision phases")

            # Extract property-level metadata from the loaded homepage.
            try:
                results["property_metadata"] = await extract_property_metadata(page)
                pname = (results["property_metadata"].get("name")
                         or results["property_metadata"].get("title") or "").strip()
                if pname:
                    results["property_name"] = pname
            except Exception as e:
                print(f"  ⚠ Property metadata extraction error: {e}")
                results["property_metadata"] = {}

            all_hrefs: list[str | None] = []
            # Collect both href and visible text for each link so we can
            # use anchor text to identify availability links whose URL
            # paths don't contain recognisable keywords.
            all_links_with_text: list[dict] = []
            try:
                all_links_with_text = await page.eval_on_selector_all(
                    "a[href]",
                    """els => els.map(e => ({
                        href: e.getAttribute('href'),
                        text: (e.innerText || e.textContent || '').trim().substring(0, 120)
                    }))""",
                )
                all_hrefs = [l["href"] for l in all_links_with_text]
            except Exception:
                pass

            internal_links: set[str] = set()
            # Links whose anchor text matches availability-related phrases —
            # these are high-priority crawl targets regardless of URL path.
            anchor_text_links: list[str] = []
            for link_info in all_links_with_text:
                url = normalise_url(base_url, link_info.get("href"))
                if url:
                    internal_links.add(url)
                    text = link_info.get("text", "")
                    if text and _AVAILABILITY_ANCHOR_RE.search(text):
                        anchor_text_links.append(url)
            # Also normalise any plain hrefs not yet covered.
            for href in all_hrefs:
                url = normalise_url(base_url, href)
                if url:
                    internal_links.add(url)

            results["links_found"] = sorted(internal_links)
            print(f"\n  🔗 Total internal links: {len(internal_links)}")
            for link in sorted(internal_links):
                print(f"     {link}")

            # Wait briefly for any in-flight XHR/fetch responses to arrive.
            # SPA sites (SightMap, Entrata widgets) fire API calls AFTER
            # domcontentloaded — a 2s settle catches most of them.
            if not api_responses:
                await asyncio.sleep(2.0)

            # ════════════════════════════════════════════════════════════
            # PHASE 2: Noise Filtering
            # ════════════════════════════════════════════════════════════
            # Apply profile-specific blocklist on top of global filters
            filtered_responses = filter_network_noise(api_responses, profile)
            if len(filtered_responses) < len(api_responses):
                print(f"\n  🔇 Noise filter: {len(api_responses)} → {len(filtered_responses)} "
                      f"API responses (profile blocklist removed {len(api_responses) - len(filtered_responses)})")

            # ════════════════════════════════════════════════════════════
            # PHASE 3: Known Pattern Extraction
            # ════════════════════════════════════════════════════════════
            # Try profile-learned patterns, then global patterns, then
            # embedded JSON, JSON-LD, DOM — all on homepage data.
            print(f"\n{'='*65}")
            print(f"  PHASE 3: Known Pattern Extraction — {len(filtered_responses)} API responses")
            print(f"{'='*65}")

            # Effective expected unit count — CSV's "Total Units" wins if
            # provided, otherwise fall back to last successful scrape.
            _expected_units = expected_total_units
            if not _expected_units and profile is not None:
                conf = getattr(profile, "confidence", None)
                _expected_units = getattr(conf, "last_unit_count", None) if conf else None

            def _phase3_accept(candidate: list[dict], tier_name: str) -> bool:
                """Reject premature Phase 3 success when the result looks bogus.

                Three rejection conditions:
                  1. Every unit lacks both rent and an id (placeholder row).
                  2. Unit count is <20% of the known expected count AND rents
                     are missing (likely a featured-units teaser).
                  3. CSV Total Units is known and result is <20% of it.
                """
                if _is_low_signal_units(candidate):
                    print(f"  ✗ {tier_name}: rejected — all units have no rent "
                          f"and no unit_id (continuing cascade)")
                    return False
                if _units_below_expected(candidate, _expected_units):
                    print(f"  ✗ {tier_name}: rejected — {len(candidate)} units "
                          f"<< expected {_expected_units} (continuing cascade)")
                    return False
                return True

            units = try_known_patterns(filtered_responses, profile)
            if units and not _phase3_accept(units, "TIER_1_API"):
                units = []
            if units:
                tier_label = "TIER_1_API"
                # Check if it came from a profile mapping
                if profile is not None:
                    api_hints = getattr(profile, "api_hints", None)
                    if api_hints and getattr(api_hints, "llm_field_mappings", []):
                        tier_label = "TIER_1_PROFILE_MAPPING"
                print(f"  ✅ {tier_label}: {len(units)} units/floor plans")
                results["extraction_tier_used"] = tier_label
                unit_api_srcs = [r["url"] for r in filtered_responses
                                 if _response_looks_like_units(r["body"])]
                results["_winning_page_url"] = unit_api_srcs[0] if unit_api_srcs else base_url

            # Tier 1.5 — Embedded JSON on homepage
            if not units:
                embedded_blobs = await extract_embedded_json(page)
                if embedded_blobs:
                    api_responses.extend(embedded_blobs)
                    candidate = parse_api_responses(embedded_blobs)
                    if candidate and _phase3_accept(candidate, "TIER_1_5_EMBEDDED"):
                        units = candidate
                        print(f"  ✅ TIER 1.5 (Embedded JSON — homepage): "
                              f"{len(units)} units/floor plans")
                        results["extraction_tier_used"] = "TIER_1_5_EMBEDDED"

            # Tier 2 — JSON-LD on homepage
            if not units:
                candidate = await parse_jsonld(page)
                if candidate and _phase3_accept(candidate, "TIER_2_JSONLD"):
                    units = candidate
                    print(f"  ✅ TIER 2 (JSON-LD): {len(units)} items")
                    results["extraction_tier_used"] = "TIER_2_JSONLD"

            # Tier 3 — DOM on homepage
            if not units:
                candidate = await parse_dom(page, base_url)
                if candidate and _phase3_accept(candidate, "TIER_3_DOM"):
                    units = candidate
                    print(f"  ✅ TIER 3 (DOM — homepage): {len(units)} units/floor plans")
                    results["extraction_tier_used"] = "TIER_3_DOM"

            if units:
                print(f"\n  >> Phase 3 success: {len(units)} units from homepage — skipping exploration")

            # ════════════════════════════════════════════════════════════
            # PHASE 4: Link-by-Link Exploration with Network Observation
            # ════════════════════════════════════════════════════════════
            # Navigate links one by one, observe NEW network calls per page,
            # collect LLM candidates for Phase 5.
            all_llm_candidates: list[dict] = []
            explored_links_status: dict[str, bool] = {}  # link -> had_data
            visited: set[str] = set()

            # Profile skip: if profile says only LLM/Vision works, skip crawl
            _profile_skip_crawl = (
                skip_to_tier is not None
                and skip_to_tier >= 4
                and not run_full_cascade
            )

            if not units and not _profile_skip_crawl:
                crawl_queue = prioritize_links(
                    internal_links, anchor_text_links, profile, base_url,
                    property_city=property_city,
                )
                print(f"\n{'='*65}")
                print(f"  PHASE 4: Link Exploration — {len(crawl_queue)} links to visit")
                print(f"{'='*65}")
                for link in crawl_queue[:5]:
                    print(f"     {link}")
                if len(crawl_queue) > 5:
                    print(f"     ... and {len(crawl_queue) - 5} more")

                link_idx = 0
                while link_idx < len(crawl_queue) and len(visited) < MAX_CRAWL_PAGES:
                    target = crawl_queue[link_idx]
                    link_idx += 1
                    if target in visited:
                        continue
                    visited.add(target)

                    print(f"\n{'─'*65}")
                    print(f"  PHASE 4 [{len(visited)}/{MAX_CRAWL_PAGES}]: {target}")

                    # Use deep clicking for COLD profiles to discover
                    # floor-plan tabs/accordions that trigger per-plan APIs.
                    _is_cold = True
                    if profile is not None:
                        _conf = getattr(profile, "confidence", None)
                        _mat = getattr(_conf, "maturity", "COLD") if _conf else "COLD"
                        _is_cold = (_mat == "COLD")
                    link_units, new_resps, llm_cands = await explore_link_with_observation(
                        page, target, api_responses, base_url,
                        deep_click=_is_cold,
                    )

                    # Filter new responses through profile blocklist
                    if new_resps:
                        new_resps = filter_network_noise(new_resps, profile)

                    if link_units:
                        units = link_units
                        explored_links_status[target] = True
                        print(f"  ✅ PHASE 4 (Exploration): {len(units)} units from {target[:80]}")
                        results["extraction_tier_used"] = "TIER_5_5_EXPLORATORY"
                        results["_winning_page_url"] = target
                        break
                    else:
                        explored_links_status[target] = False
                        all_llm_candidates.extend(llm_cands)
                        if llm_cands:
                            print(f"    + {len(llm_cands)} promising APIs for LLM analysis")

                    # Also try Entrata API probe on this page
                    if not units:
                        entrata_blobs = await probe_entrata_api(page, base_url)
                        if entrata_blobs:
                            api_responses.extend(entrata_blobs)
                            units = parse_api_responses(entrata_blobs)
                            if units:
                                print(f"  ✅ TIER 4 (Entrata API probe): "
                                      f"{len(units)} units/floor plans")
                                results["extraction_tier_used"] = "TIER_4_ENTRATA_API"
                                explored_links_status[target] = True
                                break

                    # Check for leasing portal iframes on this page
                    if not units:
                        portal_urls = await detect_leasing_portals(page)
                        for portal_url in portal_urls[:1]:
                            print(f"  🔍 Portal detected: {portal_url[:100]}")
                            try:
                                await _goto_robust(page, portal_url, timeout_ms=30000)
                                await asyncio.sleep(2.0)
                                portal_units = parse_api_responses(api_responses)
                                if portal_units:
                                    units = portal_units
                                    print(f"  ✅ TIER 5 (Portal): {len(units)} units")
                                    results["extraction_tier_used"] = "TIER_5_PORTAL"
                                    explored_links_status[target] = True
                                    break
                                # Try embedded JSON + JSON-LD + DOM on portal
                                portal_blobs = await extract_embedded_json(page)
                                if portal_blobs:
                                    api_responses.extend(portal_blobs)
                                    units = parse_api_responses(portal_blobs)
                                    if units:
                                        print(f"  ✅ TIER 5 (Portal embedded): {len(units)} units")
                                        results["extraction_tier_used"] = "TIER_5_PORTAL"
                                        explored_links_status[target] = True
                                        break
                                units = await parse_jsonld(page)
                                if units:
                                    print(f"  ✅ TIER 5 (Portal JSON-LD): {len(units)} items")
                                    results["extraction_tier_used"] = "TIER_5_PORTAL"
                                    explored_links_status[target] = True
                                    break
                                units = await parse_dom(page, portal_url)
                                if units:
                                    print(f"  ✅ TIER 5 (Portal DOM): {len(units)} units")
                                    results["extraction_tier_used"] = "TIER_5_PORTAL"
                                    explored_links_status[target] = True
                                    break
                            except Exception as e:
                                print(f"  ⚠ Portal error: {e}")
                    if units:
                        break

                if not units:
                    print(f"\n  ↳ Phase 4: explored {len(visited)} pages, "
                          f"collected {len(all_llm_candidates)} LLM candidates")

            results["property_links_crawled"] = list(visited)
            results["api_calls_intercepted"] = [r["url"] for r in api_responses]

            # ════════════════════════════════════════════════════════════
            # PHASE 4.5: LLM-Guided Navigation Discovery
            # ════════════════════════════════════════════════════════════
            # For COLD profiles with 0 units and no LLM candidates after
            # Phase 4, use vision LLM to suggest navigation actions.
            if (not units and not all_llm_candidates and not _page_unreachable
                    and len(visited) < MAX_CRAWL_PAGES):
                _is_cold_4_5 = True
                if profile is not None:
                    _c45 = getattr(profile, "confidence", None)
                    _m45 = getattr(_c45, "maturity", "COLD") if _c45 else "COLD"
                    _is_cold_4_5 = (_m45 == "COLD")

                if _is_cold_4_5:
                    print(f"\n{'='*65}")
                    print(f"  PHASE 4.5: LLM Navigation Discovery")
                    print(f"{'='*65}")
                    try:
                        screenshot = await page.screenshot(full_page=True)
                        from services.vision_extractor import suggest_navigation

                        nav_suggestions = await suggest_navigation(
                            screenshot=screenshot,
                            property_context={
                                "property_name": results.get("property_name", ""),
                                "website": base_url,
                            },
                            property_id=results.get("_property_id", "unknown"),
                        )

                        if nav_suggestions:
                            print(f"  LLM suggested {len(nav_suggestions)} navigation actions")
                            for suggestion in nav_suggestions[:3]:
                                action = suggestion.get("action", "")
                                if action == "navigate":
                                    target_url = suggestion.get("url", "")
                                    if target_url and target_url not in visited:
                                        print(f"    Navigate → {target_url[:80]}")
                                        visited.add(target_url)
                                        link_units, new_resps, llm_cands = await explore_link_with_observation(
                                            page, target_url, api_responses, base_url,
                                            deep_click=True,
                                        )
                                        if new_resps:
                                            new_resps = filter_network_noise(new_resps, profile)
                                        if link_units:
                                            units = link_units
                                            results["extraction_tier_used"] = "TIER_4_5_LLM_NAV"
                                            results["_winning_page_url"] = target_url
                                            print(f"  PHASE 4.5: {len(units)} units from {target_url[:80]}")
                                            break
                                        all_llm_candidates.extend(llm_cands)
                                elif action == "click":
                                    selector = suggestion.get("selector", "")
                                    text = suggestion.get("text", "")
                                    if selector:
                                        try:
                                            print(f"    Click → {text!r} ({selector})")
                                            api_before = len(api_responses)
                                            await page.click(selector, timeout=5000)
                                            await asyncio.sleep(2.0)
                                            new_apis = api_responses[api_before:]
                                            if new_apis:
                                                new_apis = filter_network_noise(new_apis, profile)
                                                click_units = parse_api_responses(new_apis)
                                                if click_units:
                                                    units = click_units
                                                    results["extraction_tier_used"] = "TIER_4_5_LLM_NAV"
                                                    print(f"  PHASE 4.5: {len(units)} units from click")
                                                    break
                                                all_llm_candidates.extend(
                                                    [r for r in new_apis
                                                     if _response_looks_like_units(r.get("body"))]
                                                )
                                        except Exception as e:
                                            print(f"    Click failed: {e}")
                                            continue
                        else:
                            print("  No navigation suggestions from LLM")
                    except ImportError:
                        print("  (vision_extractor.suggest_navigation not available)")
                    except Exception as e:
                        print(f"  Phase 4.5 error: {e}")

            # ════════════════════════════════════════════════════════════
            # PHASE 5: LLM-Assisted API Analysis
            # ════════════════════════════════════════════════════════════
            # Analyze promising but unparsed API responses one at a time.
            # Max 3 LLM calls per property.
            llm_analysis_results: dict[str, Any] = {}

            # HOT profiles with run_full_cascade=False skip LLM phases —
            # the profile says only the known-good tier works for this property.
            _skip_llm = (
                not run_full_cascade
                and skip_to_tier is not None
                and skip_to_tier <= 3
            )
            if _skip_llm:
                print(f"  📋 Profile: HOT with preferred_tier={skip_to_tier}, "
                      "skipping LLM/Vision (no cascade)")

            if not units and all_llm_candidates and not _page_unreachable and not _skip_llm:
                print(f"\n{'='*65}")
                print(f"  PHASE 5: LLM API Analysis — {len(all_llm_candidates)} candidates (max 3)")
                print(f"{'='*65}")

                _property_ctx = {
                    "property_name": results.get("property_name", ""),
                    "website": base_url,
                }
                _prop_id = results.get("_property_id", "unknown")

                try:
                    from services.llm_extractor import analyze_api_with_llm as _analyze_api

                    for i, candidate in enumerate(all_llm_candidates[:3]):
                        api_url = candidate.get("url", "unknown")
                        print(f"\n  LLM [{i+1}/3]: Analyzing {api_url[:100]}")

                        llm_units, mapping_dict, is_noise, interaction = await _analyze_api(
                            candidate, _property_ctx, property_id=_prop_id,
                        )
                        if interaction is not None:
                            results.setdefault("_llm_interactions", []).append(interaction)

                        if is_noise:
                            print(f"    → Noise (will blocklist)")
                            llm_analysis_results[api_url] = f"noise: {candidate.get('url', '')}"
                            continue

                        if llm_units:
                            units = llm_units
                            print(f"  ✅ PHASE 5 (LLM API Analysis): {len(units)} units from {api_url[:80]}")
                            results["extraction_tier_used"] = "TIER_4_LLM"
                            if mapping_dict:
                                llm_analysis_results[api_url] = mapping_dict
                                results["_llm_hints"] = {"json_paths": mapping_dict.get("json_paths", {})}
                            break
                        else:
                            print(f"    → No units extracted")
                            llm_analysis_results[api_url] = "noise: llm_returned_empty"

                except Exception as e:
                    print(f"  ⚠ Phase 5 (LLM API Analysis) error: {e}")

            # ════════════════════════════════════════════════════════════
            # PHASE 6: DOM Fallback with Targeted LLM
            # ════════════════════════════════════════════════════════════
            # If no APIs worked, find DOM sections with rent signals and
            # analyze them with LLM. Falls back to Vision LLM last.
            if not units and not _page_unreachable and not _skip_llm:
                print(f"\n{'='*65}")
                print(f"  PHASE 6: DOM Fallback + LLM")
                print(f"{'='*65}")

                # 6a: Try to find DOM sections with rent signals
                dom_sections = await _extract_dom_sections_with_rent_signals(page)
                if dom_sections:
                    print(f"  Found {len(dom_sections)} DOM sections with rent signals")
                    try:
                        from services.llm_extractor import analyze_dom_with_llm as _analyze_dom

                        _property_ctx = {
                            "property_name": results.get("property_name", ""),
                            "website": base_url,
                        }
                        _prop_id = results.get("_property_id", "unknown")
                        current_url = page.url

                        # Analyze the first section (budget: 1 DOM LLM call)
                        dom_units, css_selectors, dom_interaction = await _analyze_dom(
                            dom_sections[0], current_url, _property_ctx,
                            property_id=_prop_id,
                        )
                        if dom_interaction is not None:
                            results.setdefault("_llm_interactions", []).append(dom_interaction)

                        if dom_units:
                            units = dom_units
                            print(f"  ✅ PHASE 6a (DOM LLM): {len(units)} units")
                            results["extraction_tier_used"] = "TIER_3_DOM_LLM"
                            if css_selectors:
                                results["_llm_hints"] = {"css_selectors": css_selectors}

                    except Exception as e:
                        print(f"  ⚠ Phase 6a (DOM LLM) error: {e}")
                else:
                    print(f"  ↳ No DOM sections with rent signals found")

                # 6b: Fallback to legacy LLM (full page + APIs) if targeted approach failed
                if not units:
                    try:
                        from services.llm_extractor import extract_with_llm, prepare_llm_input

                        llm_input = prepare_llm_input(
                            page_html=await page.content(),
                            api_responses=api_responses,
                            property_context={
                                "property_name": results.get("property_name", ""),
                                "website": base_url,
                            },
                        )
                        llm_units, llm_hints, _, llm_interaction = await extract_with_llm(
                            llm_input,
                            property_id=results.get("_property_id", "unknown"),
                        )
                        if llm_interaction is not None:
                            results.setdefault("_llm_interactions", []).append(llm_interaction)

                        if llm_units:
                            units = llm_units
                            print(f"  ✅ PHASE 6b (Legacy LLM): {len(units)} units/floor plans")
                            results["extraction_tier_used"] = "TIER_4_LLM"
                            results["_llm_hints"] = llm_hints
                    except Exception as e:
                        print(f"  ⚠ Phase 6b (Legacy LLM) error: {e}")

                # 6c: Vision LLM as absolute last resort
                if not units:
                    try:
                        from services.vision_extractor import extract_with_vision

                        screenshot = await page.screenshot(full_page=True)
                        vision_units, vision_hints, _, vision_interaction = await extract_with_vision(
                            screenshot=screenshot,
                            property_context={
                                "property_name": results.get("property_name", ""),
                                "website": base_url,
                            },
                            property_id=results.get("_property_id", "unknown"),
                        )
                        if vision_interaction is not None:
                            results.setdefault("_llm_interactions", []).append(vision_interaction)

                        if vision_units:
                            units = vision_units
                            print(f"  ✅ PHASE 6c (Vision): {len(units)} units")
                            results["extraction_tier_used"] = "TIER_5_VISION"
                            results["_llm_hints"] = vision_hints
                    except Exception as e:
                        print(f"  ⚠ Phase 6c (Vision) error: {e}")

                if not units:
                    print("  ⚠ ALL PHASES (3-6) FAILED — no units extracted.")
                    results["extraction_tier_used"] = "FAILED"

            if not units and _page_unreachable:
                print("  ⛔ UNREACHABLE — skipped LLM/Vision phases to avoid wasting budget.")
                results["extraction_tier_used"] = "FAILED_UNREACHABLE"

            # ════════════════════════════════════════════════════════════
            # PHASE 7: Finalization + Profile Learning
            # ════════════════════════════════════════════════════════════
            # Apply availability defaults and record learning data.
            if units:
                units = apply_availability_defaults(units)

            # Record which page/URL produced the winning data
            if units and not results.get("_winning_page_url"):
                try:
                    results["_winning_page_url"] = page.url
                except Exception:
                    results["_winning_page_url"] = base_url

            # Attach profile learning data for the updater
            results["_llm_analysis_results"] = llm_analysis_results
            results["_explored_links"] = explored_links_status
            results["_raw_api_responses"] = api_responses
            results["api_calls_intercepted"] = [r["url"] for r in api_responses]
            results["units"] = units
        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

    return results

# ── Output ─────────────────────────────────────────────────────────────────────

CSV_FIELDS = [
    "floor_plan_name", "bed_label", "bedrooms", "bathrooms",
    "sqft", "unit_number", "floor", "building",
    "rent_range", "deposit", "concession",
    "availability_status", "available_units", "availability_date",
    "extraction_tier", "source_api_url",
]

def _slugify(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return s[:60] or "site"

def save_results(results: dict, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    host = urllib.parse.urlparse(results.get("base_url") or "").netloc
    slug = _slugify(host or str(results.get("property_name") or "site"))

    json_path    = output_dir / f"{slug}_units_{ts}.json"
    csv_path     = output_dir / f"{slug}_units_{ts}.csv"
    raw_api_path = output_dir / f"{slug}_raw_api_{ts}.json"

    # Split raw API bodies into their own file so the main result stays readable.
    raw_bodies = results.pop("_raw_api_responses", None)
    if raw_bodies:
        try:
            with open(raw_api_path, "w", encoding="utf-8") as f:
                json.dump(raw_bodies, f, indent=2, ensure_ascii=False, default=str)
            print(f"  📦 Raw API bodies: {raw_api_path}")
        except Exception as e:
            print(f"  ⚠ Could not save raw API bodies: {e}")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    units = results.get("units", [])
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(units)

    print(f"\n{'='*65}")
    print(f"  📄 JSON: {json_path}")
    print(f"  📊 CSV:  {csv_path}")
    print("\n  ── SUMMARY ──────────────────────────────────────────────")
    print(f"  Property:    {results['property_name']}")
    print(f"  Scraped at:  {results['scraped_at']}")
    print(f"  Tier used:   {results['extraction_tier_used']}")
    print(f"  Links found: {len(results['links_found'])}")
    print(f"  APIs hit:    {len(results['api_calls_intercepted'])}")
    print(f"  Units:       {len(units)}")

    if results["errors"]:
        print(f"\n  ⚠ Errors: {len(results['errors'])}")
        for e in results["errors"]:
            print(f"    - {e}")

    if units:
        print("\n  ── UNITS / FLOOR PLANS ──────────────────────────────────")
        print(f"  {'Plan Name':30s} {'Type':12s} {'Sqft':12s} {'Rent':22s} {'Avail':6s} {'Date'}")
        print(f"  {'─'*30} {'─'*12} {'─'*12} {'─'*22} {'─'*6} {'─'*12}")
        for u in units:
            name  = (u.get("floor_plan_name") or "")[:29]
            label = (u.get("bed_label") or "")[:11]
            sqft  = (u.get("sqft") or "")[:11]
            rent  = (u.get("rent_range") or "N/A")[:21]
            avail = str(u.get("available_units") or "")[:5]
            dt    = (u.get("availability_date") or "")[:12]
            print(f"  {name:30s} {label:12s} {sqft:12s} {rent:22s} {avail:6s} {dt}")

    return json_path, csv_path

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Entrata / multifamily property scraper")
    parser.add_argument("--url",   default=DEFAULT_URL, help="Property website URL")
    parser.add_argument("--proxy", default=None,        help="Proxy URL (e.g. http://user:pass@host:port)")
    parser.add_argument("--out",   default="./output",  help="Output directory")
    args = parser.parse_args()

    print(f"\n{'='*65}")
    print("  Property Scraper")
    print(f"  Target: {args.url}")
    print(f"  Proxy:  {args.proxy or 'None (direct)'}")
    print(f"{'='*65}")

    results = asyncio.run(scrape(args.url, args.proxy))
    save_results(results, Path(args.out))

if __name__ == "__main__":
    main()
