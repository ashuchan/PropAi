"""HTML-based extractors for Jugnu adapters.

The L1 fetcher produces a FetchResult with a ``body`` (raw HTML bytes) but no
live Playwright page. Daily_runner's JSON-LD and embedded-JSON extractors
are Playwright-coupled (``page.evaluate``, ``page.eval_on_selector_all``).
This module ports the same *logic* to operate on a raw HTML string so
adapters can still recover units from SSR / statically-rendered sites when
there is no page, and also to run as an extra deterministic tier when there
is a page but XHR capture yielded nothing.

Three public functions:
  - ``extract_jsonld_from_html(html, source_url)`` — emits adapter-shape
    unit dicts from ``<script type="application/ld+json">`` blocks.
  - ``extract_embedded_blobs_from_html(html)`` — emits ``[{url, body}]``
    synthetic API-response records that can be fed into
    ``parse_api_responses()`` exactly like captured XHR bodies.
  - ``extract_units_from_dom(html, source_url)`` — scans container elements
    (``.unit``, ``.floor-plan``, ``.pricing-card``, etc.) for visible rent
    signals and extracts unit records. This catches properties that ship
    unit data as static HTML with no JSON envelope.

Uses BeautifulSoup4 (already a project dependency) for robust HTML parsing.
The JSON-LD walking / type-matching / unit-signal logic is reused from
daily_runner via the ``_daily_runner_parsers`` bridge so both pipelines
agree on what a unit-shaped JSON-LD block looks like.
"""
from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup

from ma_poc.pms.adapters._daily_runner_parsers import (
    _jsonld_floor_size,
    _jsonld_item_has_unit_signal,
    _money_to_int,
    _walk_jsonld,
)

# Mirrors scripts/entrata.py::_EMBEDDED_JS_GLOBALS so both pipelines search
# the same set of SSR framework globals.
_EMBEDDED_JS_GLOBALS: tuple[str, ...] = (
    "__NEXT_DATA__",
    "__INITIAL_STATE__",
    "__NUXT__",
    "__remixContext",
    "__APP_DATA__",
    "pageData",
    "__data__",
    "initialState",
    "serverData",
)

# Variable-name fallbacks — the same list daily_runner evaluates when
# Strategy 1-3 yield nothing. For HTML-only parsing we only match by regex
# against inline script bodies, so the list is used as a priority filter.
_PROPERTY_VARS: tuple[str, ...] = (
    "floorPlans", "floorplans", "floor_plans",
    "unitData", "units", "propertyData", "propertyInfo",
    "availableUnits", "apartmentData", "pricingData",
    "communityData", "buildingData",
)

# Signal keywords that make an inline script worth JSON-extracting.
_UNIT_KEYWORD_RE = re.compile(
    r"floor.?plan|floorPlan|units|avail|rent|bedroom|sqft|pricing",
    re.IGNORECASE,
)

# var/let/const/window.X = {...};  or  = [...];
_ASSIGNMENT_RE = re.compile(
    r"(?:var|let|const|window\.)\s*(\w+)\s*=\s*"
    r"(\[\s*\{[\s\S]*?\}\s*\]|\{[\s\S]*?\})"
    r"\s*;",
    re.MULTILINE,
)


def extract_jsonld_from_html(html: str, source_url: str) -> list[dict[str, Any]]:
    """Extract unit records from ``<script type="application/ld+json">`` blocks.

    Emits the adapter-compatible dict shape (``floor_plan_name``,
    ``rent_range``, ``sqft``, etc.) with ``extraction_tier="TIER_2_JSONLD"``.
    Missing / malformed JSON-LD blocks are silently skipped.
    """
    if not html:
        return []

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        # lxml missing — BS4 will raise; fall back to the stdlib parser.
        soup = BeautifulSoup(html, "html.parser")

    units: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.string or script.get_text()
        if not text or not text.strip():
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue

        matched: list[dict] = []
        _walk_jsonld(data, matched)

        for item in matched:
            # Skip bare Offer nodes — they're metadata of an enclosing
            # Apartment/FloorPlan, already consumed when we emit that parent's
            # rent_range. _walk_jsonld matches Offer because it's in
            # TARGET_JSONLD_TYPES, but emitting them as units produces dupes.
            #
            # Also skip AggregateOffer (property-level summary with lowPrice
            # /highPrice across ALL units) — emitting it as a single unit
            # yields a degenerate row with no unit_number / sqft / beds,
            # which passes validation as "1 unit extracted" but isn't real
            # rental inventory. Observed on Embarc at West Jordan (5119) —
            # its Product > offers:AggregateOffer block was surfacing as a
            # phantom unit.
            t = item.get("@type")
            t_list: list[str] = []
            if isinstance(t, str):
                t_list = [t]
            elif isinstance(t, list):
                t_list = [x for x in t if isinstance(x, str)]
            if "AggregateOffer" in t_list:
                continue
            if t == "Offer" or (isinstance(t, list) and "Offer" in t and len(t) == 1):
                continue
            if not _jsonld_item_has_unit_signal(item):
                continue

            # Phantom-shell guard: a matched item with zero usable fields
            # (no name, no offers price, no floorSize, no numberOfRooms)
            # is a property-level node slipping through. Emitting it as a
            # "1 unit" result fools the pipeline into claiming success.
            offers = item.get("offers") if isinstance(item.get("offers"), dict) else {}
            has_price = bool(
                offers.get("price") or offers.get("lowPrice") or offers.get("highPrice")
            ) or (isinstance(item.get("offers"), list) and item.get("offers"))
            has_name = bool(item.get("name"))
            has_size = bool(item.get("floorSize"))
            has_rooms = bool(item.get("numberOfRooms"))
            if not (has_price or has_name or has_size or has_rooms):
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
                prices: list[int] = []
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
                "availability_status": "",
                "available_units":    "",
                "availability_date":  "",
                "source_api_url":     source_url,
                "extraction_tier":    "TIER_2_JSONLD",
            })

    return units


def extract_embedded_blobs_from_html(html: str) -> list[dict[str, Any]]:
    """Extract embedded JSON blobs as synthetic API responses.

    Searches for:
      1. ``<script type="application/json">`` blocks
      2. ``<script id="__NEXT_DATA__">`` (special-case Next.js pattern)
      3. Inline ``var X = {...};`` assignments where ``X`` is a known
         property-data variable name OR the body contains unit keywords

    Returns a list of ``{url, body}`` dicts using synthetic ``embedded:*``
    URL prefixes so downstream logs can distinguish these from real XHR
    captures. The bodies are parsed JSON — ready to hand to
    ``parse_api_responses()``.
    """
    if not html:
        return []

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    found: list[dict[str, Any]] = []

    # ── Strategy A: <script type="application/json"> (incl. __NEXT_DATA__) ──
    for script in soup.find_all("script", attrs={"type": "application/json"}):
        text = script.string or script.get_text()
        if not text or len(text) < 200 or len(text) > 1_000_000:
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        block_id = script.get("id") or script.get("data-id") or "anon"
        found.append({"url": f"embedded:json-block:{block_id}", "body": data})

    # ── Strategy B: Inline <script> assignments ────────────────────────────
    # Only look at scripts without src AND without a type (or with type
    # text/javascript). Gate on unit-keyword presence to keep noise low.
    for script in soup.find_all("script"):
        if script.get("src"):
            continue
        script_type = (script.get("type") or "").lower()
        if script_type and script_type not in ("", "text/javascript", "application/javascript"):
            continue
        text = script.string or script.get_text()
        if not text or len(text) < 300 or len(text) > 500_000:
            continue
        if not _UNIT_KEYWORD_RE.search(text):
            continue

        # Try: var/let/const/window.X = <JSON>;
        for m in _ASSIGNMENT_RE.finditer(text):
            var_name = m.group(1)
            json_str = m.group(2)
            if len(json_str) < 200:
                continue
            try:
                data = json.loads(json_str)
            except (json.JSONDecodeError, ValueError):
                # Regex-extracted fragment may not be valid JSON — expected.
                continue
            found.append({
                "url": f"embedded:script-var:{var_name}",
                "body": data,
            })

        # Also accept ``window.__NEXT_DATA__ = {...};`` and similar known globals
        # where the regex above might not match due to multi-line templates.
        for gvar in _EMBEDDED_JS_GLOBALS:
            pattern = re.compile(
                rf"window\.{re.escape(gvar)}\s*=\s*(\{{[\s\S]*?\}})\s*;",
                re.MULTILINE,
            )
            m = pattern.search(text)
            if not m:
                continue
            try:
                data = json.loads(m.group(1))
            except (json.JSONDecodeError, ValueError):
                continue
            found.append({"url": f"embedded:js:{gvar}", "body": data})

    return found


# ── DOM selector cascade ────────────────────────────────────────────────────
# Runs when neither XHR capture nor JSON-LD nor embedded-JSON produced units
# but the raw HTML has visible rent signals ($NNN text). Looks for container
# elements that plausibly wrap a single unit/floor-plan, pulls the visible
# rent / sqft / beds / baths from the container's text, and emits an
# adapter-shape unit dict.

_DOM_CONTAINER_SELECTORS: tuple[str, ...] = (
    # Common PMS / CMS container patterns. Specific-first, generic-last so a
    # site with both `.unit-card` and `.card` prefers the specific one.
    ".unit-card", ".unit-row", ".unit-item", ".unitContainer",
    ".floorplan", ".floor-plan", ".floorplan-card", ".floor-plan-card",
    ".floorplan-row", ".floor-plan-row", ".floorplanItem", ".fp-card",
    ".apartment", ".apartment-card", ".apartment-row",
    ".listing", ".listing-card", ".listing-item",
    ".pricing-card", ".pricing-item", ".pricing-row", ".plan-card",
    "[data-unit]", "[data-floorplan]", "[data-floor-plan]", "[data-apartment]",
    "article.unit", "article.floorplan", "article.apartment",
    "div.unit", "div.floorplan", "div.apartment", "div.listing",
)

_RENT_PATTERN = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})*|\d{3,5})(?:\.\d{2})?",
)
_SQFT_PATTERN = re.compile(
    r"(\d{2,5})\s*(?:sq\.?\s*ft\.?|sqft|square\s*feet)",
    re.IGNORECASE,
)
_BEDS_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:bed|br\b|bedroom)",
    re.IGNORECASE,
)
_BATHS_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:bath|ba\b|bathroom)",
    re.IGNORECASE,
)
_STUDIO_RE = re.compile(r"\bstudio\b", re.IGNORECASE)
_UNIT_NUM_PATTERN = re.compile(
    r"(?:unit|apt|apartment|#)\s*#?\s*([A-Za-z0-9][A-Za-z0-9\-]{0,10})",
    re.IGNORECASE,
)
_FP_NAME_PATTERN = re.compile(
    r"(?:the\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
)

# Rent bounds copy of _parsing.rent_in_sanity_range without importing it
# (avoids the circular concern when this file is imported by generic.py).
_RENT_LO_BOUND = 200
_RENT_HI_BOUND = 50_000


def _rent_to_int(s: str) -> int | None:
    try:
        n = int(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None
    if not (_RENT_LO_BOUND <= n <= _RENT_HI_BOUND):
        return None
    return n


def _container_yields_unit(text: str) -> dict[str, Any] | None:
    """Return a unit dict if ``text`` has at least rent + (sqft or beds)."""
    rents = _RENT_PATTERN.findall(text)
    if not rents:
        return None
    rent_ints = [r for r in (_rent_to_int(x) for x in rents) if r is not None]
    if not rent_ints:
        return None
    rent_lo = min(rent_ints)
    rent_hi = max(rent_ints)

    m_sqft = _SQFT_PATTERN.search(text)
    m_beds = _BEDS_PATTERN.search(text)
    m_baths = _BATHS_PATTERN.search(text)
    m_unit = _UNIT_NUM_PATTERN.search(text)
    is_studio = bool(_STUDIO_RE.search(text))

    # Require at least one structural signal beyond rent so we don't pick up
    # "hero price" banners or aggregate summaries.
    if not (m_sqft or m_beds or is_studio):
        return None

    beds_val = m_beds.group(1) if m_beds else ("0" if is_studio else "")
    baths_val = m_baths.group(1) if m_baths else ""
    sqft_val = m_sqft.group(1) if m_sqft else ""
    unit_num = m_unit.group(1) if m_unit else ""

    rent_range = f"{rent_lo}-{rent_hi}" if rent_hi > rent_lo else str(rent_lo)

    return {
        "floor_plan_name": "",
        "bed_label": f"{beds_val}BR" if beds_val and beds_val != "0" else (
            "Studio" if is_studio else ""
        ),
        "bedrooms": beds_val,
        "bathrooms": baths_val,
        "sqft": sqft_val,
        "unit_number": unit_num,
        "floor": "",
        "building": "",
        "rent_range": rent_range,
        "market_rent_low": rent_lo,
        "market_rent_high": rent_hi,
        "deposit": "",
        "concession": "",
        "availability_status": "AVAILABLE",
        "available_units": "",
        "availability_date": "",
        "extraction_tier": "TIER_3_DOM",
    }


def extract_units_from_dom(html: str, source_url: str) -> list[dict[str, Any]]:
    """Extract units by scanning common container selectors for rent signals.

    Conservative on purpose: requires rent + at least one structural signal
    (sqft / beds / studio) per container. Prevents false positives on pages
    that show a single "Starting at $1,200" banner but no per-unit table.
    """
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return []

    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for selector in _DOM_CONTAINER_SELECTORS:
        try:
            nodes = soup.select(selector)
        except Exception:
            continue
        if not nodes:
            continue
        # If we found >80 of the same selector, it probably matched
        # something too generic (like every `.apartment` article on a blog).
        if len(nodes) > 80:
            continue
        for node in nodes:
            text = node.get_text(" ", strip=True)
            if len(text) < 10 or len(text) > 3000:
                continue
            unit = _container_yields_unit(text)
            if unit is None:
                continue
            unit["source_api_url"] = f"dom:{selector}"
            unit["_source_url"] = source_url
            dedup = (
                unit["unit_number"]
                or f"{unit['rent_range']}|{unit['sqft']}|{unit['bedrooms']}"
            )
            if dedup in seen:
                continue
            seen.add(dedup)
            units.append(unit)
        if units:
            # First selector that produced usable units wins — keeps output
            # coherent (all units come from the same container pattern).
            break
    return units
