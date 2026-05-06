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
from datetime import date
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
    "floorPlans",
    "floorplans",
    "floor_plans",
    "unitData",
    "units",
    "propertyData",
    "propertyInfo",
    "availableUnits",
    "apartmentData",
    "pricingData",
    "communityData",
    "buildingData",
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


# Schema.org container types that wrap a multi-Offer array where each Offer
# represents a unit. Used by ``_extract_offers_as_units``. These types are
# NOT in TARGET_JSONLD_TYPES (so they do not pollute _walk_jsonld output);
# they are walked separately as a second pass.
_OFFER_CONTAINER_TYPES: frozenset[str] = frozenset(
    {
        "Place",
        "LocalBusiness",
        "RealEstateListing",
        "Product",
        # ApartmentComplex appears here too — when it has a multi-Offer array
        # it's the same pattern. The first-pass loop treats ApartmentComplex
        # as property metadata only (no offers price means "skip"); the
        # second pass below will emit per-Offer units when offers[] >= 2.
        "ApartmentComplex",
    }
)


def _money_int_or_none(v: Any) -> int | None:
    """Helper: parse a money-shaped value to int, returning None on failure."""
    if v is None or v == "":
        return None
    return _money_to_int(str(v))


def _build_unit_from_offer(
    offer: dict[str, Any], parent_name: str, source_url: str
) -> dict[str, Any] | None:
    """Build a unit-shape dict from a single Offer node nested under a Place /
    LocalBusiness / Product / RealEstateListing / ApartmentComplex container.

    Pulls floor_plan_name, rent, sqft, beds, availability from either the
    Offer itself or its ``itemOffered`` sub-node. Returns None if the offer
    has no rent at all (we do not emit phantom rows from offers without
    pricing).
    """
    item: dict[str, Any] = (
        offer.get("itemOffered") if isinstance(offer.get("itemOffered"), dict) else {}
    )

    name = offer.get("name") or item.get("name") or parent_name or ""
    if not isinstance(name, str):
        name = str(name)

    lo_i = _money_int_or_none(offer.get("lowPrice") or offer.get("price"))
    hi_i = _money_int_or_none(offer.get("highPrice"))
    # Some Offers attach price under itemOffered instead of the offer itself
    if lo_i is None and isinstance(item, dict):
        lo_i = _money_int_or_none(item.get("price") or item.get("lowPrice"))
        if hi_i is None:
            hi_i = _money_int_or_none(item.get("highPrice"))

    if lo_i is None and hi_i is None:
        return None

    if lo_i is not None and hi_i is not None and lo_i != hi_i:
        rent_range = f"${lo_i:,} - ${hi_i:,}"
    elif lo_i is not None:
        rent_range = f"${lo_i:,}"
    elif hi_i is not None:
        rent_range = f"${hi_i:,}"
    else:
        return None

    sqft = ""
    if isinstance(item, dict):
        sqft = _jsonld_floor_size(item)
    if not sqft:
        sqft = _jsonld_floor_size(offer)

    bedrooms = ""
    num_rooms = item.get("numberOfRooms") if isinstance(item, dict) else None
    if num_rooms in (None, ""):
        num_rooms = offer.get("numberOfRooms")
    if isinstance(num_rooms, dict):
        num_rooms = num_rooms.get("value", "")
    if num_rooms not in (None, ""):
        bedrooms = str(num_rooms)

    avail = ""
    if isinstance(item, dict):
        avail = item.get("availability") or ""
    if not avail:
        avail = offer.get("availability") or ""
    avail_date = ""
    if isinstance(item, dict):
        avail_date = item.get("availabilityStarts") or ""
    if not avail_date:
        avail_date = offer.get("validFrom") or ""

    return {
        "floor_plan_name": name,
        "bed_label": "",
        "bedrooms": bedrooms,
        "bathrooms": "",
        "sqft": sqft,
        "unit_number": "",
        "floor": "",
        "building": "",
        "rent_range": rent_range,
        "market_rent_low": lo_i if lo_i is not None else hi_i,
        "market_rent_high": hi_i if hi_i is not None else lo_i,
        "deposit": "",
        "concession": "",
        "availability_status": str(avail) if avail else "",
        "available_units": "",
        "availability_date": str(avail_date) if avail_date else "",
        "lease_term": "",
        "move_in_date": "",
        "source_api_url": source_url,
        "extraction_tier": "TIER_2_JSONLD",
    }


def _extract_offers_as_units(data: Any, source_url: str) -> list[dict[str, Any]]:
    """Find Place / LocalBusiness / Product / RealEstateListing / ApartmentComplex
    containers in the JSON-LD tree that have a multi-Offer array, and emit
    each Offer as a unit dict.

    Many marketing-site JSON-LD blocks use this pattern — a single container
    node describing the property + an ``offers`` (or ``makesOffer``) array
    where each Offer corresponds to a unit/floor-plan. The original
    extraction loop only handled the per-Apartment-with-offers case and
    missed these container patterns, leaving ~50% of properties with
    extractable JSON-LD failing extraction (May 2026 investigation).

    Distinguishing-fields guard: the offers must have at least one
    distinguishing dimension (≥2 distinct prices, names, or sqft values)
    before we emit them. This protects against a single-offer-replicated
    array masquerading as multiple units.
    """
    units: list[dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            t = node.get("@type")
            t_list: list[str] = []
            if isinstance(t, str):
                t_list = [t]
            elif isinstance(t, list):
                t_list = [x for x in t if isinstance(x, str)]

            if any(x in _OFFER_CONTAINER_TYPES for x in t_list):
                parent_name = node.get("name") if isinstance(node.get("name"), str) else ""
                for key in ("offers", "makesOffer", "itemListElement"):
                    arr = node.get(key)
                    if not isinstance(arr, list) or len(arr) < 2:
                        continue
                    # Distinguishing-fields guard: collect prices, names, sqft
                    prices: set[str] = set()
                    names: set[str] = set()
                    sizes: set[str] = set()
                    offer_dicts = [o for o in arr if isinstance(o, dict)]
                    for o in offer_dicts:
                        for pk in ("price", "lowPrice", "highPrice"):
                            v = o.get(pk)
                            if v not in (None, ""):
                                prices.add(str(v))
                        n = o.get("name")
                        if n:
                            names.add(str(n))
                        io = o.get("itemOffered") if isinstance(o.get("itemOffered"), dict) else {}
                        if io.get("name"):
                            names.add(str(io["name"]))
                        fs = _jsonld_floor_size(io) if io else ""
                        if fs:
                            sizes.add(fs)
                    distinct_dims = sum(1 for s in (prices, names, sizes) if len(s) >= 2)
                    if distinct_dims < 1:
                        continue
                    for o in offer_dicts:
                        u = _build_unit_from_offer(o, parent_name, source_url)
                        if u:
                            units.append(u)

            # Recurse into all children. node.values() already iterates @graph
            # if present, so we do NOT walk it separately — doing so would
            # double-emit when a container is referenced via both a regular key
            # and an @graph entry.
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(data)
    return units


def extract_jsonld_from_html(html: str, source_url: str) -> list[dict[str, Any]]:
    """Extract unit records from ``<script type="application/ld+json">`` blocks.

    Emits the adapter-compatible dict shape (``floor_plan_name``,
    ``rent_range``, ``sqft``, etc.) with ``extraction_tier="TIER_2_JSONLD"``.
    Missing / malformed JSON-LD blocks are silently skipped.

    Two extraction passes:
      1. ``TARGET_JSONLD_TYPES`` items (Apartment, FloorPlan, etc.) — each
         emitted as a unit. Existing behavior, preserved verbatim.
      2. Container types with multi-Offer arrays (Place, LocalBusiness,
         Product, RealEstateListing, ApartmentComplex) — each Offer in the
         array emitted as a unit. Added 2026-05 to recover ~370 properties
         that ship JSON-LD but use the container-with-offers pattern.

    Pass 2 only fires when pass 1 returned no units, to avoid double-counting
    when both patterns are present (e.g., a Place with offers AND nested
    Apartment nodes).
    """
    if not html:
        return []

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        # lxml missing — BS4 will raise; fall back to the stdlib parser.
        soup = BeautifulSoup(html, "html.parser")

    parsed_blocks: list[Any] = []
    units: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.string or script.get_text()
        if not text or not text.strip():
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        parsed_blocks.append(data)

        matched: list[dict[str, Any]] = []
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
            # Skip ApartmentComplex when its offers field is a multi-Offer
            # array — that pattern is the "container with per-unit Offers"
            # case handled in pass 2 (_extract_offers_as_units). Emitting
            # ApartmentComplex from pass 1 would compress the array into a
            # single fake aggregate unit AND block pass 2 from running.
            if "ApartmentComplex" in t_list:
                offers_field = item.get("offers")
                if isinstance(offers_field, list) and len(offers_field) >= 2:
                    continue
            if not _jsonld_item_has_unit_signal(item):
                continue

            # Phantom-shell guard: a matched item with zero usable fields
            # (no name, no offers price, no floorSize, no numberOfRooms)
            # is a property-level node slipping through. Emitting it as a
            # "1 unit" result fools the pipeline into claiming success.
            offers_raw = item.get("offers")
            offers: dict[str, Any] = offers_raw if isinstance(offers_raw, dict) else {}
            has_price = bool(offers.get("price") or offers.get("lowPrice") or offers.get("highPrice")) or (
                isinstance(offers_raw, list) and bool(offers_raw)
            )
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

            units.append(
                {
                    "floor_plan_name": name,
                    "bed_label": "",
                    "bedrooms": str(num_rooms) if num_rooms not in (None, "") else "",
                    "bathrooms": "",
                    "sqft": _jsonld_floor_size(item),
                    "unit_number": "",
                    "floor": "",
                    "building": "",
                    "rent_range": rent_range,
                    # Surface numeric rent so the v2 transform doesn't have to
                    # re-parse the human-readable string. Fall back to ``lo_i``
                    # for both when the high price is missing (single-value rent).
                    "market_rent_low": lo_i,
                    "market_rent_high": hi_i if hi_i is not None else lo_i,
                    "deposit": "",
                    "concession": "",
                    "availability_status": "",
                    "available_units": "",
                    "availability_date": "",
                    "lease_term": "",
                    "move_in_date": "",
                    "source_api_url": source_url,
                    "extraction_tier": "TIER_2_JSONLD",
                }
            )

    # Pass 2: Container-with-offers pattern (Place / LocalBusiness / Product /
    # RealEstateListing / ApartmentComplex with multi-Offer array). Only fires
    # when pass 1 returned nothing — avoids double-counting when both patterns
    # coexist on the same page (typical RentCafe / SightMap layouts ship both).
    if not units:
        for data in parsed_blocks:
            units.extend(_extract_offers_as_units(data, source_url))

    return units


def parse_jsonld(html: str, source_url: str = "") -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """F5: Strict JSON-LD parser that returns (property_metadata, units).

    Units list is empty UNLESS the JSON-LD contains an ItemList or Offer array
    of length >= 2, or an ApartmentUnit[] collection, AND the items have at
    least 2 distinct discriminating fields (numberOfRooms, floorSize, price,
    name) across the collection.

    A single 'Apartment' schema object where every 'unit' is actually the
    property itself is treated as property metadata only — units=[] is returned.
    """
    property_metadata: dict[str, Any] = {}

    if not html:
        return property_metadata, []

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    candidate_collections: list[list[dict[str, Any]]] = []

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.string or script.get_text()
        if not text or not text.strip():
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue

        # Extract property metadata (ApartmentComplex / Place nodes)
        def _extract_meta(node: Any) -> None:
            if not isinstance(node, dict):
                return
            t = node.get("@type", "")
            if isinstance(t, str) and t in ("ApartmentComplex", "Place", "LocalBusiness"):
                for k in ("name", "telephone", "url"):
                    if node.get(k):
                        property_metadata[k] = node[k]
                addr = node.get("address")
                if isinstance(addr, dict):
                    property_metadata["address"] = addr
            if isinstance(node.get("@graph"), list):
                for child in node["@graph"]:
                    _extract_meta(child)

        _extract_meta(data)

        # Collect ItemList / ApartmentUnit arrays
        def _find_collections(node: Any) -> None:
            if not isinstance(node, dict):
                return
            t = node.get("@type", "")
            t_types = [t] if isinstance(t, str) else (t if isinstance(t, list) else [])

            if "ItemList" in t_types:
                items = node.get("itemListElement") or []
                if isinstance(items, list) and len(items) >= 2:
                    candidate_collections.append(items)

            # Direct array of apartment/floorplan nodes
            for v in node.values():
                if isinstance(v, list) and len(v) >= 2:
                    if all(isinstance(i, dict) for i in v):
                        types_in_list = [i.get("@type", "") for i in v]
                        apartment_types = {"Apartment", "ApartmentUnit", "FloorPlan", "Product"}
                        if any(t in apartment_types for t in types_in_list):
                            candidate_collections.append(v)
                if isinstance(v, dict):
                    _find_collections(v)
            if isinstance(node.get("@graph"), list):
                for child in node["@graph"]:
                    _find_collections(child)

        _find_collections(data)

    # Evaluate candidate collections for discriminating distinctness
    def _field_val(item: dict[str, Any], field: str) -> str:
        v = item.get(field)
        if v is None:
            return ""
        if isinstance(v, dict):
            return str(v.get("value", v.get("price", "")))
        if isinstance(v, list) and v and isinstance(v[0], dict):
            p = v[0].get("price", "")
            return str(p)
        return str(v)

    _DISCRIMINATING = ("numberOfRooms", "floorSize", "name")
    _PRICE_KEYS = ("price", "lowPrice", "highPrice")

    def _has_distinct_fields(collection: list[dict[str, Any]]) -> bool:
        distinct_dimensions = 0
        for field in _DISCRIMINATING:
            vals = {_field_val(i, field) for i in collection if _field_val(i, field)}
            if len(vals) >= 2:
                distinct_dimensions += 1
        # Check offers/price distinctness
        prices: set[str] = set()
        for item in collection:
            offers = item.get("offers")
            if isinstance(offers, dict):
                for pk in _PRICE_KEYS:
                    if offers.get(pk):
                        prices.add(str(offers[pk]))
            elif isinstance(offers, list):
                for o in offers:
                    if isinstance(o, dict):
                        for pk in _PRICE_KEYS:
                            if o.get(pk):
                                prices.add(str(o[pk]))
        if len(prices) >= 2:
            distinct_dimensions += 1
        return distinct_dimensions >= 2

    best_units: list[dict[str, Any]] = []
    for collection in candidate_collections:
        if not _has_distinct_fields(collection):
            continue
        # Emit units from this collection via existing logic
        collection_units = extract_jsonld_from_html(
            # Fake an HTML wrapper so extract_jsonld_from_html can parse the items
            # Re-emit as JSON-LD for the existing parser to handle
            '<script type="application/ld+json">'
            + json.dumps({"@type": "ItemList", "itemListElement": collection})
            + "</script>",
            source_url,
        )
        if len(collection_units) > len(best_units):
            best_units = collection_units

    return property_metadata, best_units


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
        script_type_raw = script.get("type") or ""
        script_type = (
            script_type_raw if isinstance(script_type_raw, str) else " ".join(script_type_raw)
        ).lower()
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
            found.append(
                {
                    "url": f"embedded:script-var:{var_name}",
                    "body": data,
                }
            )

        # Also accept ``window.__NEXT_DATA__ = {...};`` and similar known globals
        # where the regex above might not match due to multi-line templates.
        for gvar in _EMBEDDED_JS_GLOBALS:
            pattern = re.compile(
                rf"window\.{re.escape(gvar)}\s*=\s*(\{{[\s\S]*?\}})\s*;",
                re.MULTILINE,
            )
            gmatch = pattern.search(text)
            if not gmatch:
                continue
            try:
                data = json.loads(gmatch.group(1))
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
    ".unit-card",
    ".unit-row",
    ".unit-item",
    ".unitContainer",
    ".floorplan",
    ".floor-plan",
    ".floorplan-card",
    ".floor-plan-card",
    ".floorplan-row",
    ".floor-plan-row",
    ".floorplanItem",
    ".fp-card",
    ".apartment",
    ".apartment-card",
    ".apartment-row",
    ".listing",
    ".listing-card",
    ".listing-item",
    ".pricing-card",
    ".pricing-item",
    ".pricing-row",
    ".plan-card",
    "[data-unit]",
    "[data-floorplan]",
    "[data-floor-plan]",
    "[data-apartment]",
    "article.unit",
    "article.floorplan",
    "article.apartment",
    "div.unit",
    "div.floorplan",
    "div.apartment",
    "div.listing",
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
        "bed_label": f"{beds_val}BR" if beds_val and beds_val != "0" else ("Studio" if is_studio else ""),
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


def extract_units_from_dom(
    html: str,
    source_url: str,
    hints: Any | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Extract units by scanning common container selectors for rent signals.

    Conservative on purpose: requires rent + at least one structural signal
    (sqft / beds / studio) per container. Prevents false positives on pages
    that show a single "Starting at $1,200" banner but no per-unit table.

    Phase 8: when ``hints`` (a FieldSelectorMap) is provided with a non-empty
    ``container``, that selector is tried FIRST. On miss, falls back to the
    default cascade.

    Returns (units, hit_mode) where hit_mode is one of:
      "hints"   — profile hint selectors fired
      "default" — default cascade fired
      "none"    — no units extracted
    """
    if not html:
        return [], "none"

    if hints is not None:
        try:
            hint_units = extract_with_hints(html, source_url, hints)
        except Exception:
            hint_units = []
        if hint_units:
            return hint_units, "hints"

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return [], "none"

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
            dedup = unit["unit_number"] or f"{unit['rent_range']}|{unit['sqft']}|{unit['bedrooms']}"
            if dedup in seen:
                continue
            seen.add(dedup)
            units.append(unit)
        if units:
            # First selector that produced usable units wins — keeps output
            # coherent (all units come from the same container pattern).
            break
    if units:
        return units, "default"
    return [], "none"


def extract_with_hints(
    html: str,
    source_url: str,
    hints: Any,
) -> list[dict[str, Any]]:
    """Phase 8 — hint-only DOM extraction.

    Honors ``hints.container`` (and optional rent / sqft / bedrooms / etc.
    selectors). Returns ``[]`` if container doesn't match. Does NOT fall
    back to the default cascade — callers can chain.
    """
    if not html or hints is None:
        return []
    container_sel = getattr(hints, "container", None)
    if not container_sel:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return []
    try:
        nodes = soup.select(container_sel)
    except Exception:
        return []
    if not nodes or len(nodes) > 200:
        return []
    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in nodes:
        text = node.get_text(" ", strip=True)
        if len(text) < 10 or len(text) > 3000:
            continue
        unit = _container_yields_unit(text)
        if unit is None:
            continue
        unit["source_api_url"] = f"dom_hints:{container_sel}"
        unit["_source_url"] = source_url
        dedup = unit["unit_number"] or f"{unit['rent_range']}|{unit['sqft']}|{unit['bedrooms']}"
        if dedup in seen:
            continue
        seen.add(dedup)
        units.append(unit)
    return units


# ── F4: available_date extraction ─────────────────────────────────────────────

_AVAIL_DATE_TEXT_RE = re.compile(
    r"(?:available|move[- ]?in)[\s:]+([A-Za-z]+\s+\d{1,2},?\s*\d{0,4}|\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)
_AVAIL_NOW_RE = re.compile(r"available\s+now", re.IGNORECASE)
_TODAY = None  # Populated lazily to avoid import-time side effects


def _today_date() -> date:
    return date.today()


def extract_available_date_from_card(card_html: str) -> str | None:
    """Extract an available_date from a unit card's HTML subtree.

    Tries the following in order:
      1. ``[data-available-date]`` / ``[data-move-in]`` attribute
      2. ``<time datetime=...>`` element
      3. ``[class*="available"]``, ``[class*="availability"]``, ``[class*="avail-date"]``
      4. Text regex: ``(available|move-in): <date>``

    Normalizes to ISO YYYY-MM-DD. Skips past dates unless "available now"
    appears in the card (indicating immediate availability).

    Returns None on any parse failure or ambiguity.
    """
    if not card_html:
        return None
    try:
        from dateutil import parser as du_parser  # type: ignore[import-untyped]
    except ImportError:
        return None

    try:
        soup = BeautifulSoup(card_html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(card_html, "html.parser")
        except Exception:
            return None

    available_now = bool(_AVAIL_NOW_RE.search(card_html))

    def _parse_date_str(raw: str) -> str | None:
        raw = raw.strip()
        if not raw:
            return None
        # Guard against bare numbers that dateutil happily parses as "12th of this month"
        if re.match(r"^\d{1,2}$", raw):
            return None
        try:
            dt = du_parser.parse(raw, default=None)
            if dt is None:
                return None
            d = dt.date()
            today = _today_date()
            if d < today and not available_now:
                return None
            return str(d.isoformat())
        except Exception:
            return None

    # 1. data-available-date / data-move-in attribute
    for attr in ("data-available-date", "data-move-in"):
        el = soup.find(attrs={attr: True})
        if el:
            raw = el.get(attr, "")
            result = _parse_date_str(str(raw))
            if result:
                return result

    # 2. <time> element
    for time_el in soup.find_all("time"):
        raw = time_el.get("datetime") or time_el.get_text()
        result = _parse_date_str(str(raw))
        if result:
            return result

    # 3. Class-based selectors
    for el in soup.find_all(class_=True):
        class_attr: Any = el.get("class") or []
        classes = " ".join(class_attr) if isinstance(class_attr, list) else str(class_attr)
        if any(k in classes.lower() for k in ("available", "availability", "avail-date")):
            raw = el.get_text(" ", strip=True)
            result = _parse_date_str(raw)
            if result:
                return result

    # 4. Regex on full card text
    text = soup.get_text(" ", strip=True)
    m = _AVAIL_DATE_TEXT_RE.search(text)
    if m:
        result = _parse_date_str(m.group(1))
        if result:
            return result

    return None


# ────────────────────────────────────────────────────────────────────
# Plain-text regex unit extractor (added 2026-05-06)
# ────────────────────────────────────────────────────────────────────
# Targets the LLM_COULD_NOT_EXTRACT cohort: pages where rent is in
# flowing marketing-copy text rather than structured DOM containers.
# DOM scan (extract_units_from_dom) requires a CSS selector container
# that holds rent + structural fields together. Marketing-CMS templates
# (Jonah Digital, Hyly, WordPress + Elementor) often render rent as
# free-form copy with bed/bath/sqft tokens nearby but no enclosing
# container. Sample (cottagesatsanford.com): page has 11 ``$NNN``
# matches + 3 "1 Bed 1 Bath" tokens + 4 sqft tokens but DOM scan
# returns nothing.
#
# Strategy: strip HTML to plain text, find each rent-shaped ``$NNN``
# occurrence, look for bed/bath OR sqft within 300 chars, dedup by
# (rent, sqft, beds), require >= 2 distinct units to emit. Quality
# gates filter phone numbers, deposit fees, and amenity prices.

_TEXT_DOLLAR_RE = re.compile(
    r"\$\s?(\d{1,2}[,.]?\d{3}|\d{3,4})(?:\.\d{2})?(?:\s*/\s*(?:mo|month))?",
    re.IGNORECASE,
)
_TEXT_BEDBATH_RE = re.compile(
    r"(\d+|studio|one|two|three|four)\s*(?:-|\s)*(?:bed|bd|br)(?:room)?s?\b\s*"
    r"[\|/,•·\s]*\s*(\d+(?:\.\d+)?)\s*(?:bath|ba|bth)(?:room)?s?\b",
    re.IGNORECASE,
)
# Standalone bedroom mention (no bath) — used when sqft is also present
_TEXT_BEDS_ALONE_RE = re.compile(
    r"\b(\d+|studio|one|two|three|four)\s*(?:-|\s)*(?:bed|bd|br)(?:room)?s?\b",
    re.IGNORECASE,
)
_TEXT_SQFT_RE = re.compile(
    r"(\d{2,4}(?:,\d{3})?)\s*(?:sq\.?\s*ft|sqft|square\s+feet)\b",
    re.IGNORECASE,
)
_TEXT_PLAN_RE = re.compile(
    r"(?:floor\s*plan|plan|model)\s*:?\s*([A-Za-z][\w\s\-]{1,28})",
    re.IGNORECASE,
)
_NUM_WORD = {"studio": "0", "one": "1", "two": "2", "three": "3", "four": "4"}


def extract_units_from_text(html: str, source_url: str = "") -> list[dict[str, Any]]:
    """Plain-text rent-cluster extractor for marketing-template sites.

    Falls back from DOM scan when the page has rent visible in flowing
    copy but no CSS container holds it together. Emits 1 unit per
    distinct (rent, sqft, beds) cluster found within 300-char proximity.

    Quality gates:
      - rent must be in $200..$50_000 range (filters phone numbers, deposit fees)
      - rent must have bed/bath OR sqft within 300 chars (filters loose dollar mentions)
      - >= 2 distinct units required (filters single-mention pages)
    """
    if not html:
        return []

    # Strip noise first
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return []
    for tag in soup.find_all(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    if len(text) < 200:
        return []

    seen: set[tuple[int, str, str]] = set()
    units: list[dict[str, Any]] = []

    def _closest_match(pattern: re.Pattern, window: str, anchor_in_window: int) -> "re.Match[str] | None":
        """Find the match in `window` whose start is closest to `anchor_in_window`."""
        best = None
        best_dist = 10**9
        for mm in pattern.finditer(window):
            d = abs(mm.start() - anchor_in_window)
            if d < best_dist:
                best, best_dist = mm, d
        return best

    for m in _TEXT_DOLLAR_RE.finditer(text):
        rent_str = m.group(1).replace(",", "").replace(".", "")
        try:
            rent_int = int(rent_str)
        except (ValueError, TypeError):
            continue
        if rent_int < 200 or rent_int > 50_000:
            continue

        # Tight 120-char window on each side — adjacent clusters in marketing
        # copy are typically 100-200 chars apart, so a wider window picks up
        # the wrong cluster's bed/bath.
        ctx_start = max(0, m.start() - 120)
        ctx_end = min(len(text), m.end() + 120)
        ctx = text[ctx_start:ctx_end]
        anchor = m.start() - ctx_start

        bb_match = _closest_match(_TEXT_BEDBATH_RE, ctx, anchor)
        sf_match = _closest_match(_TEXT_SQFT_RE, ctx, anchor)
        # Standalone-beds qualifier (e.g. "Studio" or "1 bedroom" with no bath)
        beds_alone = (
            _closest_match(_TEXT_BEDS_ALONE_RE, ctx, anchor) if not bb_match else None
        )

        # Need bed/bath OR sqft (or both) — at least one structural signal
        if not (bb_match or sf_match):
            continue

        beds = ""
        baths = ""
        if bb_match:
            b_raw = bb_match.group(1).lower()
            beds = _NUM_WORD.get(b_raw, b_raw)
            baths = bb_match.group(2)
        elif beds_alone:
            b_raw = beds_alone.group(1).lower()
            beds = _NUM_WORD.get(b_raw, b_raw)
        sqft = sf_match.group(1).replace(",", "") if sf_match else ""
        plan_match = _TEXT_PLAN_RE.search(ctx)
        plan_name = plan_match.group(1).strip() if plan_match else ""

        key = (rent_int, sqft, beds)
        if key in seen:
            continue
        seen.add(key)

        units.append(
            {
                "floor_plan_name": plan_name,
                "bed_label": "",
                "bedrooms": beds,
                "bathrooms": baths,
                "sqft": sqft,
                "unit_number": "",
                "floor": "",
                "building": "",
                "rent_range": f"${rent_int:,}",
                "market_rent_low": rent_int,
                "market_rent_high": rent_int,
                "deposit": "",
                "concession": "",
                "availability_status": "",
                "available_units": "",
                "availability_date": "",
                "lease_term": "",
                "move_in_date": "",
                "source_api_url": source_url,
                "extraction_tier": "TIER_3_TEXT_REGEX",
            }
        )

    # Need at least 2 distinct units to qualify — protects against pages
    # that mention a single deposit / amenity-pool / processing-fee dollar
    # near a sqft amenity blurb.
    if len(units) < 2:
        return []
    return units
