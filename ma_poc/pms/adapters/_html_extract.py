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


def _extract_standalone_offers(data: Any, source_url: str) -> list[dict[str, Any]]:
    """Bug 4 (2026-05-09 deep-dive): emit units from Offer arrays not nested in
    a known container.

    Some marketing CMSes (gscapts.com / Jonah Systems / Knock-driven sites)
    emit JSON-LD with multiple top-level ``Offer`` nodes that are siblings of
    ``Brand``/``PostalAddress``/etc., or nested inside non-listed container
    types like ``Brand``. Pass 1 (``_walk_jsonld``) explicitly skips bare
    Offers; Pass 2 (``_extract_offers_as_units``) only descends into
    ``_OFFER_CONTAINER_TYPES``. This pass closes the gap.

    Distinguishing-fields guard mirrors pass 2: require ≥2 distinct prices
    OR ≥2 distinct names so a single Offer replicated across the doc
    doesn't masquerade as multiple units.
    """
    offers: list[dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            t = node.get("@type")
            t_list: list[str] = []
            if isinstance(t, str):
                t_list = [t]
            elif isinstance(t, list):
                t_list = [x for x in t if isinstance(x, str)]
            # Bare Offer nodes (single-typed) — collect, don't recurse into
            # their fields (Offer's children are scalar attributes, not
            # nested unit data).
            if t_list == ["Offer"]:
                offers.append(node)
                return
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(data)
    if len(offers) < 2:
        return []

    prices: set[str] = set()
    names: set[str] = set()
    for o in offers:
        for pk in ("price", "lowPrice", "highPrice"):
            v = o.get(pk)
            if v not in (None, ""):
                prices.add(str(v))
        n = o.get("name")
        if n:
            names.add(str(n))
    if len(prices) < 2 and len(names) < 2:
        return []

    units: list[dict[str, Any]] = []
    for o in offers:
        u = _build_unit_from_offer(o, "", source_url)
        if u:
            units.append(u)
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
            has_rooms = bool(item.get("numberOfRooms") or item.get("numberOfBedrooms"))
            if not (has_price or has_name):
                # Skip items that only have physical attributes (size/rooms)
                # but no name and no price — these are itemOffered sub-nodes
                # nested inside Offer elements and are handled by pass 2
                # (_extract_offers_as_units) instead.
                continue
            # 2026-05-13 teammate analysis (C8 JSON-LD false-positive): nodes
            # with @type=Apartment but only a ``name`` and no quantitative
            # data (no price, no size, no bedroom count) still slip through.
            # Examples observed:
            #   {"@type": "Apartment", "name": "Pet Friendly", ...}
            #   {"@type": "ApartmentComplex", "name": "Amenities"}
            # post_process eventually rejects them, but the planner sees
            # ``ran_units=1`` first and escalates to LLM rescue, burning
            # token budget. Require at least one quantitative dimension to
            # emit a unit; name-only nodes get skipped.
            if not (has_price or has_size or has_rooms):
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

            # Schema.org Apartment uses numberOfBedrooms / numberOfBathroomsTotal
            # as the canonical bedroom/bathroom fields. numberOfRooms is the
            # total room count (may be absent on floor-plan-only records).
            # Read all three so AMLI-style JSON-LD (which uses the canonical
            # fields) surfaces bed/bath data rather than emitting empty strings.
            def _rooms_val(v: Any) -> str:
                if v is None:
                    return ""
                if isinstance(v, dict):
                    v = v.get("value", "")
                return str(v) if v not in (None, "") else ""

            num_bedrooms = (
                _rooms_val(item.get("numberOfBedrooms"))
                or _rooms_val(item.get("numberOfRooms"))
            )
            num_bathrooms = _rooms_val(
                item.get("numberOfBathroomsTotal")
                or item.get("numberOfFullBathrooms")
                or item.get("numberOfRooms")
                if not num_bedrooms  # avoid double-counting rooms as baths
                else item.get("numberOfBathroomsTotal")
                or item.get("numberOfFullBathrooms")
            )

            units.append(
                {
                    "floor_plan_name": name,
                    "bed_label": "",
                    "bedrooms": num_bedrooms,
                    "bathrooms": num_bathrooms,
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

    # Pass 3 (Bug 4, 2026-05-09): standalone Offer arrays — bare ``Offer``
    # nodes that are siblings of ``Brand``/``PostalAddress``/etc., or nested
    # inside non-listed containers like ``Brand``. Closes the gscapts.com /
    # Jonah Systems / Knock CMS gap where Offers are present but neither
    # Pass 1 nor Pass 2 emits them. Same distinguishing-fields guard as
    # Pass 2 prevents single-offer-replicated arrays from inflating counts.
    if not units:
        for data in parsed_blocks:
            units.extend(_extract_standalone_offers(data, source_url))

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


def detect_floorplan_subpage_urls(
    blobs: list[dict[str, Any]],
    page_html: str,
    base_url: str,
) -> list[tuple[str, str]]:
    """Detect floor-plan sub-page URLs from a floor-plan index page.

    Jonah Digital (and similar custom CMSes) structure availability as:
      /floorplans/           ← index page (limited unit data per plan)
      /floorplans/{slug}/    ← per-plan page with FULL unit list in
                               an embedded <script type="application/json">
                               block (not behind any XHR).

    This helper recognises the index page by finding a Jonah-style widget
    config blob (``renderable_endpoint: "_fp-renderable"`` + ``base_uri``)
    in the embedded JSON, then scans the page HTML for ``<a href>`` tags
    that are one path-segment deeper than ``base_uri``.  Returns those URLs
    as ``(url, "floorplan_subpage")`` tuples so link-hop can fetch each
    and extract units from the sub-page's embedded JSON.

    Args:
        blobs:     Output of ``extract_embedded_blobs_from_html`` — parsed blobs.
        page_html: Raw HTML of the current page (used for anchor scanning).
        base_url:  Absolute URL of the current page (for resolving relative hrefs).

    Returns:
        Deduplicated list of ``(absolute_url, "floorplan_subpage")`` tuples.
        Empty when the page is not a recognised floor-plan index.
    """
    if not blobs or not page_html:
        return []

    # Step 1 — confirm we're on a floor-plan index by looking for a Jonah
    # config blob with both ``base_uri`` and ``renderable_endpoint``.
    base_uri: str | None = None
    for blob in blobs:
        body = blob.get("body") if isinstance(blob, dict) else None
        if not isinstance(body, dict):
            continue
        if body.get("renderable_endpoint") and body.get("base_uri"):
            candidate_base = str(body["base_uri"]).strip()
            if candidate_base and candidate_base.startswith("/"):
                base_uri = candidate_base.rstrip("/") + "/"
                break

    if base_uri is None:
        return []

    # Step 2 — find <a href> tags whose path is exactly one segment deeper
    # than base_uri (e.g. /floorplans/the-edgefield/ when base_uri=/floorplans/).
    import urllib.parse as _urlparse

    try:
        soup = BeautifulSoup(page_html, "lxml")
    except Exception:
        soup = BeautifulSoup(page_html, "html.parser")

    try:
        base_parsed = _urlparse.urlparse(base_url)
        base_host = base_parsed.scheme + "://" + (base_parsed.netloc or "")
    except Exception:
        return []

    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    for a in soup.find_all("a", href=True):
        raw = a.get("href") or ""
        href = (raw if isinstance(raw, str) else " ".join(raw)).strip()
        if not href or href.startswith(("#", "tel:", "mailto:", "javascript:")):
            continue
        try:
            resolved = _urlparse.urljoin(base_url, href)
        except Exception:
            continue
        try:
            parsed = _urlparse.urlparse(resolved)
        except Exception:
            continue
        # Must be same host
        if (base_parsed.hostname or "").lower() != (parsed.hostname or "").lower():
            continue
        path = parsed.path.rstrip("/") + "/"
        # Must start with base_uri and have exactly one more path segment
        if not path.startswith(base_uri):
            continue
        # Strip the base_uri prefix and see if the remainder is a single slug
        remainder = path[len(base_uri):]
        if "/" in remainder.rstrip("/"):
            continue  # more than one additional segment — skip
        if not remainder.rstrip("/"):
            continue  # same page
        abs_url = base_host + path
        if abs_url not in seen:
            seen.add(abs_url)
            out.append((abs_url, "floorplan_subpage"))

    return out


# Leasing-portal host patterns. Each tuple is (substring, portal_name);
# matches against any URL string found inside an embedded JSON blob.
# Specific-first so e.g. ``onlineleasing.realpage.com`` outranks the
# generic ``realpage.com`` host check.
_PORTAL_URL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("sightmap.com/embed/", "sightmap"),
    ("embed.engrain.com", "sightmap"),
    ("onlineleasing.realpage.com", "realpage_oll"),
    ("myleasingoffice.com", "realpage_oll"),
    ("rentcafe.com/apartments/", "rentcafe"),
    ("rentcafe.com/onlineleasing", "rentcafe"),
    ("funnelleasing.com/embed", "funnel"),
    ("apartments.appfolio.com", "appfolio"),
    ("widgets.appfolio.com", "appfolio"),
)

# Cap the recursion depth so a self-referential JSON blob can't blow
# the stack. 12 is deep enough for every realistic portal-config shape
# we've seen (SightMap's deepest path is ``engrain.disabled_ui[0]`` —
# 3 levels).
_PORTAL_DETECT_MAX_DEPTH = 12


def detect_embedded_portal_urls(
    blobs: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Scan parsed embedded-JSON blobs for leasing-portal embed URLs.

    Some property sites (Jonah Digital, headless WordPress, custom
    React/Vue marketing shells) don't host their own unit data — they
    inline a configuration blob that points at a third-party leasing
    portal (SightMap, RealPage OLL, RentCafe, FunnelLeasing, AppFolio)
    where the actual units live. The portal URL is *physically present*
    in the HTML we already fetched, just nested inside a JSON config
    that our unit-signal heuristic correctly rejects (it has no
    rent/sqft keys, only `embed_url`, `integration`, `engrain.asset_id`,
    etc.).

    This helper walks every parsed blob recursively, looking at every
    string value for one of the known portal host patterns. Returns a
    list of ``(url, portal_name)`` tuples — caller emits them as fetch
    candidates for link-hop, which then dispatches each portal URL to
    the matching PMS adapter (the URL host triggers the SightMap /
    RentCafe / etc. fingerprint).

    Args:
        blobs: Output of ``extract_embedded_blobs_from_html`` —
               ``[{"url": ..., "body": parsed_json}]``.

    Returns:
        Deduplicated list of ``(url, portal_name)`` tuples in first-seen
        order. Empty when no portal pointer is found.
    """
    if not blobs:
        return []

    seen_urls: set[str] = set()
    out: list[tuple[str, str]] = []

    def _walk(node: Any, depth: int) -> None:
        if depth > _PORTAL_DETECT_MAX_DEPTH:
            return
        if isinstance(node, str):
            # URL strings inside JSON often have escaped slashes ("\/").
            # The L1 fetcher's json.loads already unescapes those, but
            # being defensive in case a future caller hands in raw
            # unparsed text.
            candidate = node.replace("\\/", "/")
            if "://" not in candidate and not candidate.startswith("//"):
                return
            lowered = candidate.lower()
            for needle, portal in _PORTAL_URL_PATTERNS:
                if needle in lowered:
                    # Normalise to absolute URL — accept protocol-relative
                    # too ("//sightmap.com/embed/...").
                    if candidate.startswith("//"):
                        candidate = "https:" + candidate
                    if candidate in seen_urls:
                        return
                    seen_urls.add(candidate)
                    out.append((candidate, portal))
                    return
            return
        if isinstance(node, dict):
            for v in node.values():
                _walk(v, depth + 1)
        elif isinstance(node, list):
            for item in node:
                _walk(item, depth + 1)

    for blob in blobs:
        body = blob.get("body") if isinstance(blob, dict) else None
        if body is None:
            continue
        _walk(body, 0)

    return out


# ── SecureCafe portal URL detection ─────────────────────────────────────────

# Properties that use SecureCafe online leasing often have one of two shapes:
#
#  Shape A — URL directly in HTML (href to subdomain.securecafe.com):
#    The Playwright-rendered page contains a direct href to the securecafe
#    subdomain. detect_embedded_portal_urls doesn't catch this because it
#    only walks JSON blobs.  The DEFAULT_HOST_KEYWORDS score of 120 for
#    securecafe.com means the ranker will already surface these links at
#    10000+ — no extra work needed.
#
#  Shape B — local /onlineleasing/{slug} route (JS SPA redirect):
#    The property site defines a local client-side route `/onlineleasing/{slug}`
#    that JavaScript redirects to `{subdomain}.securecafe.com/onlineleasing/
#    {slug}/`.  The slug in the href MATCHES the slug in the securecafe path.
#    The subdomain is derived from the property domain by stripping TLD.
#    Since the redirect is pure JS, we can construct the canonical URL
#    deterministically without following the redirect.

_ONLINELEASING_RE = re.compile(
    r'href=["\'](?:https?://[^"\']*)?/onlineleasing/([a-z0-9][a-z0-9-]{2,80})',
    re.IGNORECASE,
)
_TLD_RE = re.compile(
    r'\.(com|net|org|apartments|info|biz|co|us|homes|live|apts)$',
    re.IGNORECASE,
)


def detect_securecafe_portal_url(html: str, base_url: str) -> str | None:
    """Construct a SecureCafe floor-plans URL from /onlineleasing/ href + domain.

    Matches Shape-B SecureCafe sites where the property domain has a JS-only
    route ``/onlineleasing/{slug}`` that client-side-redirects to the securecafe
    subdomain.  Because the JS redirect is never followed in HTTP-fetch mode,
    we synthesise the canonical URL instead:

        https://{domain_slug}.securecafe.com/onlineleasing/{path_slug}/floorplans.aspx

    The domain slug is the property hostname with ``www.`` and TLD stripped.
    The path slug comes directly from the ``/onlineleasing/`` href — it always
    matches the SecureCafe path because the property developer sets both.

    Args:
        html: Full Playwright-rendered page HTML.
        base_url: The property entry URL (used to derive the subdomain).

    Returns:
        Absolute floorplans.aspx URL, or None when the pattern is absent.
    """
    path_m = _ONLINELEASING_RE.search(html)
    if not path_m:
        return None

    path_slug = path_m.group(1).lower()

    try:
        from urllib.parse import urlparse as _up
        host = _up(base_url).netloc or base_url.split("/")[0]
    except Exception:
        host = base_url.split("/")[0]

    host = re.sub(r"^www\.", "", host, flags=re.IGNORECASE)
    domain_slug = _TLD_RE.sub("", host)

    if not domain_slug or "." in domain_slug:
        return None

    return (
        f"https://{domain_slug}.securecafe.com"
        f"/onlineleasing/{path_slug}/floorplans.aspx"
    )


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


# Context words that precede a sqft match but indicate it's NOT the unit's
# floor area — balconies, storage, patios, fees, etc. If any of these appear
# within 40 characters before the sqft number, suppress the match.
_SQFT_NOISE_CONTEXT_RE = re.compile(
    r"balcon|patio|terrace|storage|closet|garage|amenity|amenities|locker",
    re.IGNORECASE,
)

# Minimum realistic apartment sqft. Matches _format_area's [150, 10000] clamp.
# Values below this are amenity descriptions, not unit sizes.
_SQFT_MIN = 150


def _sqft_match_is_valid(text: str, m: re.Match) -> bool:
    """Return False when the sqft regex match is context-contaminated.

    Two rejection criteria (both must be absent for the match to be valid):
      1. The captured value is below the 150 sqft floor.
      2. A noise context word appears in the 40 chars before the match start.
    """
    try:
        val = int(m.group(1).replace(",", ""))
    except (ValueError, AttributeError):
        return False
    if val < _SQFT_MIN:
        return False
    prefix = text[max(0, m.start() - 40) : m.start()]
    if _SQFT_NOISE_CONTEXT_RE.search(prefix):
        return False
    return True


def _container_yields_unit(text: str) -> dict[str, Any] | None:
    """Return a unit dict if ``text`` has at least rent + (sqft or beds).

    2026-05-12 sqft fix: the regex matches any 2-5 digit number + sqft suffix,
    including "100 sq ft storage" or "50 sq ft balcony". These values are
    below 150 sqft (the realistic apartment floor), so _format_area previously
    stored them as the -1 absent sentinel even though beds/baths were present.
    _sqft_match_is_valid now rejects sub-150 matches and context-contaminated
    matches (where a noise word like "balcony" or "storage" precedes the number).
    """
    rents = _RENT_PATTERN.findall(text)
    if not rents:
        return None
    rent_ints = [r for r in (_rent_to_int(x) for x in rents) if r is not None]
    if not rent_ints:
        return None
    rent_lo = min(rent_ints)
    rent_hi = max(rent_ints)

    # Find the best (largest) valid sqft match in the container text.
    # Using the largest value avoids picking up sub-unit amenity areas that
    # appear in the same container as the real unit sqft.
    valid_sqft_matches = [
        m for m in _SQFT_PATTERN.finditer(text) if _sqft_match_is_valid(text, m)
    ]
    m_sqft = max(valid_sqft_matches, key=lambda m: int(m.group(1).replace(",", "")), default=None)

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


def _select_one_text(node: Any, selector: str | None) -> str:
    """Pick the first matching child element's text under ``node``.

    Returns "" on missing selector / no match / parse error so callers
    can ``or``-chain a regex fallback.
    """
    if not selector:
        return ""
    try:
        match = node.select_one(selector)
    except Exception:
        return ""
    if match is None:
        return ""
    try:
        text = match.get_text(" ", strip=True)
    except Exception:
        return ""
    return text or ""


def _select_all_text(node: Any, selector: str | None) -> list[str]:
    """Return text of every match under ``node``. Used for
    multi-element selectors (amenities lists, concession callouts)."""
    if not selector:
        return []
    try:
        matches = node.select(selector)
    except Exception:
        return []
    out: list[str] = []
    for m in matches:
        try:
            t = m.get_text(" ", strip=True)
        except Exception:
            continue
        if t:
            out.append(t)
    return out


def extract_with_hints(
    html: str,
    source_url: str,
    hints: Any,
) -> list[dict[str, Any]]:
    """Phase 8 — hint-driven DOM extraction.

    Honors ``hints.container`` (required) plus per-field selectors when
    set. For every field whose selector is present AND matches, the
    selector's value wins; otherwise the regex extraction over the
    container's full text fills the gap. This means hints that only set
    ``container`` still work (back-compat — preserves the
    ``test_dom_hints_wiring.py`` contract), while richer hints that
    nominate per-field selectors get deterministic per-field routing.

    Returns ``[]`` when container doesn't match. Does NOT fall back to
    the default cascade — callers can chain.
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

    # Optional per-field selectors. Anything absent or unmatched falls
    # through to the regex extraction over the container's text.
    sel_unit_id = getattr(hints, "unit_id", None)
    sel_floor_plan = getattr(hints, "floor_plan_name", None)
    sel_rent = getattr(hints, "rent", None)
    sel_sqft = getattr(hints, "sqft", None)
    sel_beds = getattr(hints, "bedrooms", None)
    sel_baths = getattr(hints, "bathrooms", None)
    sel_avail_date = getattr(hints, "availability_date", None)
    sel_avail_status = getattr(hints, "availability_status", None)
    sel_amenities = getattr(hints, "amenities", None)
    sel_concession = getattr(hints, "concession", None)

    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in nodes:
        text = node.get_text(" ", strip=True)
        if len(text) < 10 or len(text) > 3000:
            continue
        # Regex-derived baseline (every existing field)
        unit = _container_yields_unit(text)
        if unit is None:
            continue

        # Per-field selector overrides — only override when the
        # selector matched. An empty selector match leaves the regex
        # value alone so a partially-broken hint set degrades
        # gracefully rather than wiping out otherwise-valid units.
        if (v := _select_one_text(node, sel_unit_id)):
            unit["unit_number"] = v
        if (v := _select_one_text(node, sel_floor_plan)):
            unit["floor_plan_name"] = v
        if (v := _select_one_text(node, sel_rent)):
            # Re-parse the matched text for rent integers — the LLM may
            # nominate a wrapper that contains "$1,500/mo" rather than
            # the bare number, and we want consistent rent_low/high.
            rent_ints = [r for r in (_rent_to_int(x) for x in _RENT_PATTERN.findall(v)) if r is not None]
            if rent_ints:
                lo, hi = min(rent_ints), max(rent_ints)
                unit["rent_range"] = f"{lo}-{hi}" if hi > lo else str(lo)
                unit["market_rent_low"] = lo
                unit["market_rent_high"] = hi
        if (v := _select_one_text(node, sel_sqft)):
            m = _SQFT_PATTERN.search(v)
            if m:
                unit["sqft"] = m.group(1)
        if (v := _select_one_text(node, sel_beds)):
            m = _BEDS_PATTERN.search(v)
            if m:
                unit["bedrooms"] = m.group(1)
        if (v := _select_one_text(node, sel_baths)):
            m = _BATHS_PATTERN.search(v)
            if m:
                unit["bathrooms"] = m.group(1)
        if (v := _select_one_text(node, sel_avail_date)):
            unit["availability_date"] = v
        if (v := _select_one_text(node, sel_avail_status)):
            # Normalise to the existing enum surface used by schema_v2.
            up = v.strip().upper()
            if up in ("AVAILABLE", "UNAVAILABLE", "WAITLIST", "UNKNOWN"):
                unit["availability_status"] = up

        # Amenities: list selector → list of strings. Falls through
        # to None when no selector / no matches; the existing regex
        # path doesn't surface amenities at all so this is purely
        # additive.
        amen_items = _select_all_text(node, sel_amenities)
        if amen_items:
            # If a single match contained a comma-separated list,
            # split it. If multiple elements matched, take them as-is.
            if len(amen_items) == 1 and "," in amen_items[0]:
                amen_items = [x.strip() for x in amen_items[0].split(",") if x.strip()]
            unit["amenities"] = amen_items

        # Concession: the strikethrough rent or banner text. Stamp
        # ``concession_source="strikethrough_dom"`` since that matches
        # the V2 enum the prompt and schema agree on for DOM-derived
        # concessions.
        conc_text = _select_one_text(node, sel_concession)
        if conc_text:
            unit["concession_text"] = conc_text
            unit["concession_source"] = "strikethrough_dom"

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
