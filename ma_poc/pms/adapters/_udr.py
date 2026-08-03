"""UDR (udr.com) Schema.org JSON-LD ItemList adapter.

UDR is a top-50 multifamily REIT (~16 properties in our CSV). Their
``apartments-pricing`` floor-plans page embeds availability as a
Schema.org ``ItemList`` of ``Apartment`` items in a ``<script
type="application/ld+json">`` block. Verified live against UDR
Cambridge Woods, Tampa on 2026-05-24:

    {
      "@context": "https://schema.org",
      "@type": "ItemList",
      "itemListElement": [
        {
          "@type": "ListItem",
          "position": 8,
          "item": {
            "@type": ["Apartment", "Product"],
            "name": "Apartment #8 - 4020",          ← unit_number lives here
            "description": "2 Beds | 1.5 Baths | 1309 Sq. Ft",
            "url": "...?unitid=13664212",            ← INTERNAL id (don't use)
            "offers": {
              "@type": "Offer",
              "url": "...?unitid=13664212",
              "price": 1833,
              "priceCurrency": "USD",
              "availability": "https://schema.org/InStock"
            },
            "floorSize": {"@type": "QuantitativeValue", "value": 1309},
            "numberOfBathroomsTotal": 1,
            "numberOfBedrooms": 2
          }
        },
        ...
      ]
    }

The bug this fixes (2026-05-23 audit row #41 — Cambridge Woods unit
``13664212``): the generic DOM tier was extracting ``unitid`` from
the URL param instead of parsing the human-friendly Schema.org
``name`` ("Apartment #8 - 4020" → ``4020``). The visible unit
number on udr.com is the part after the dash; the leading "Apartment
#N" is just a sequential page-position label.

This parser:
  * Walks the ItemList, extracting per-unit name + offer + floorSize +
    beds/baths from the canonical Schema.org structure
  * Parses ``"Apartment #<seq> - <unit_number>"`` into just the
    unit_number (the bit displayed to renters)
  * Preserves the internal ``unitid`` URL param in ``source_ids`` for
    cross-reference
  * Returns extraction_tier ``TIER_1_JSONLD_UDR``

Wired into ``generic.py`` for the udr.com domain only — Schema.org
``Apartment`` ItemList is too generic to enable globally without
risking pollution from other operators that ship it differently.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from html import unescape
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)

# UDR's ``Apartment #<seq> - <unit>`` shape. The leading "#<seq>" is
# the position label (sequential per-page); the part after " - " is
# the displayed unit number. Both segments are required for a clean
# parse — if the dash is missing we fall through to the raw name.
_UDR_NAME_RE = re.compile(
    r"^Apartment\s*#?\s*[A-Z0-9]+\s*[-–—]\s*([A-Z0-9][A-Z0-9\-]*)\s*$",
    re.IGNORECASE,
)
_UDR_NAME_PARTS_RE = re.compile(
    r"^Apartment\s*#?\s*([A-Z0-9]+)\s*[-–—]\s*"
    r"([A-Z0-9][A-Z0-9\-]*)\s*$",
    re.IGNORECASE,
)

# Extract unitid URL param (preserved as source_id for provenance).
_UDR_UNITID_RE = re.compile(r"[?&]unitid=(\d+)", re.IGNORECASE)

_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE | re.DOTALL)
_HTML_ATTR_RE = re.compile(
    r"([:\w-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'=<>`]+))",
    re.IGNORECASE | re.DOTALL,
)
_UDR_HOSTS = frozenset({"udr.com", "www.udr.com"})


def is_udr_url(url: str) -> bool:
    """Return true only for an HTTPS URL on UDR's exact public hosts."""
    try:
        parsed = urlsplit(str(url or "").strip())
        return (
            parsed.scheme.lower() == "https"
            and (parsed.hostname or "").lower().rstrip(".") in _UDR_HOSTS
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
        )
    except (TypeError, ValueError):
        return False


def canonical_udr_url_from_html(html: str) -> str:
    """Extract an official UDR canonical URL from a vanity-site homepage.

    UDR serves some communities on a vanity domain while publishing a strict
    ``rel=canonical`` link to the matching ``www.udr.com`` community page.
    Only an HTTPS canonical on UDR's exact public hosts is accepted; lookalike
    hosts, credentials, ports, relative URLs, query strings, and fragments are
    rejected or stripped before this value can drive a cross-host probe.
    """
    if not html:
        return ""

    for tag in _LINK_TAG_RE.findall(html):
        attrs: dict[str, str] = {}
        for match in _HTML_ATTR_RE.finditer(tag):
            value = next(
                (part for part in match.groups()[1:] if part is not None),
                "",
            )
            attrs[match.group(1).lower()] = unescape(value).strip()

        rel_tokens = {token.lower() for token in attrs.get("rel", "").split()}
        if "canonical" not in rel_tokens:
            continue
        href = attrs.get("href", "")
        if not is_udr_url(href):
            continue

        parsed = urlsplit(href)
        path = parsed.path or "/"
        return urlunsplit(
            (
                "https",
                (parsed.hostname or "").lower().rstrip("."),
                path,
                "",
                "",
            )
        )
    return ""


def udr_pricing_urls(base_url: str) -> list[str]:
    """Build tightly scoped pricing-page candidates for one UDR community."""
    if not is_udr_url(base_url):
        return []

    parsed = urlsplit(base_url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if "apartments-pricing" in {segment.lower() for segment in segments}:
        return []

    origin = f"https://{(parsed.hostname or '').lower().rstrip('.')}"
    clean_path = "/" + "/".join(segments) if segments else ""
    candidates = [f"{origin}{clean_path}/apartments-pricing/"]

    # UDR community URLs have /{market}/{area}/{community}/. Some catalog
    # records point at a leaf such as /contact-us/; the community root is the
    # first three path segments in that case.
    if len(segments) > 3:
        candidates.append(
            f"{origin}/{'/'.join(segments[:3])}/apartments-pricing/"
        )
    return list(dict.fromkeys(candidates))
# The same UDR pricing page carries a richer first-party view model beside
# its Schema.org ItemList.  JSON-LD has identity/rent but no move-in date;
# ``jsonObjPropertyViewModel`` has the exact unit-keyed ``AvailableDateLabel``
# and rent-matrix MoveInDate.  All seven July-31 UDR date-gap properties use
# this stable assignment (106/106 affected native units, live 2026-08-01).
_UDR_VIEW_MODEL_MARKER = "window.udr.jsonObjPropertyViewModel = "


def _udr_view_model_dates(html: str) -> dict[str, str]:
    """Return namespaced native-ID and unambiguous-label date keys.

    The object is a JavaScript assignment whose value is strict JSON.  Decode
    only the first JSON value after the marker so adjacent script statements
    cannot contaminate parsing.  Visible ``AvailableDateLabel`` wins; the
    first rent-matrix ``MoveInDate`` is an exact fallback.  Malformed or absent
    data degrades to an empty mapping.
    """
    if not html:
        return {}
    start = html.find(_UDR_VIEW_MODEL_MARKER)
    if start < 0:
        return {}
    start += len(_UDR_VIEW_MODEL_MARKER)
    try:
        payload, _ = json.JSONDecoder().raw_decode(html[start:].lstrip())
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}

    out: dict[str, str] = {}
    label_dates: dict[str, str] = {}
    label_counts: dict[str, int] = {}
    for floor_plan in payload.get("floorPlans") or []:
        if not isinstance(floor_plan, dict):
            continue
        for unit in floor_plan.get("units") or []:
            if not isinstance(unit, dict):
                continue
            unit_number = str(
                unit.get("marketingName") or unit.get("lookUpName") or ""
            ).strip()
            raw_date = str(unit.get("AvailableDateLabel") or "").strip()
            if not raw_date:
                for rent_option in unit.get("rentsMatrix") or []:
                    if not isinstance(rent_option, dict):
                        continue
                    raw_date = str(rent_option.get("MoveInDate") or "").strip()
                    if raw_date:
                        break
            if not raw_date:
                continue

            # The JSON-LD offer URL carries this same immutable apartment ID.
            # UDR's marketing label can repeat across buildings (for example
            # 7-105 and 19-105 both appear as ``105`` in the view model), so
            # native identity is the authoritative join.
            for native_key in (
                "apartmentId",
                "realpageunitid",
                "realPageUnitId",
                "unitId",
            ):
                native_value = unit.get(native_key)
                if isinstance(native_value, bool) or native_value in (None, ""):
                    continue
                if isinstance(native_value, float) and native_value.is_integer():
                    native_text = str(int(native_value))
                else:
                    native_text = str(native_value).strip()
                if native_text:
                    out[f"id:{native_text}"] = raw_date

            if unit_number:
                label_key = unit_number.upper()
                label_counts[label_key] = label_counts.get(label_key, 0) + 1
                label_dates[label_key] = raw_date

    # A display-label fallback is retained for older UDR shapes that do not
    # expose a native ID, but only when exactly one view-model row owns it.
    for label_key, raw_date in label_dates.items():
        if label_counts.get(label_key) == 1:
            out[f"label:{label_key}"] = raw_date
    return out


def _format_udr_plan_code(raw_code: str) -> str:
    """Reformat a UDR image-filename plan code (lowercase, no dots)
    into the displayed plan name (uppercase, with decimal point).

    UDR's marketing page shows "Plan B1.5T" but the floor-plan image
    URL uses "cambridgewoods_b15t_combined_3d.gif" — the decimal point
    is stripped from the filename. This rule restores it:

      Insert a "." between TWO consecutive digits when at least one
      letter precedes the digit pair AND at least one letter follows
      the digit pair OR the digit pair is at the end.

    Examples (verified live across 13 Cambridge Woods plans 2026-05-24):
      "a1a"   → "A1A"        (no two-digit run)
      "a1d"   → "A1D"
      "b15t"  → "B1.5T"      (1 and 5 → 1.5, followed by T)
      "b25at" → "B2.5AT"     (2 and 5 → 2.5, followed by AT)
      "c25"   → "C2.5"       (2 and 5 at end → 2.5)

    Falls back to ``raw_code.upper()`` when no two-digit run is found.
    """
    if not raw_code:
        return ""
    up = raw_code.upper()
    # Insert a "." between the first two consecutive digits when they
    # follow a leading letter cluster. We deliberately only handle the
    # first occurrence — UDR plans like "A1B5T" don't exist in any
    # community we've seen; restrict to the simple case to avoid
    # over-aggressive periodization.
    m = re.match(r"^([A-Z]+)(\d)(\d)([A-Z]*)$", up)
    if m:
        return f"{m.group(1)}{m.group(2)}.{m.group(3)}{m.group(4)}"
    return up


def _extract_unit_from_udr_name(name: str) -> str:
    """Parse ``"Apartment #8 - 4020"`` → ``"4020"``.

    Falls back to the input string when the expected shape isn't
    present (lets the caller decide whether to keep the raw name or
    bail).
    """
    if not name:
        return ""
    s = name.strip()
    m = _UDR_NAME_RE.match(s)
    if m:
        return m.group(1).strip()
    # 2026-07-11: UDR dropped the "#<seq> - " infix — names now read
    # "Apartment 404" with the unit number directly after the word.
    m2 = re.match(r"^Apartments?\s*#?\s*([A-Z0-9][A-Z0-9\-]*)\s*$", s, re.IGNORECASE)
    if m2:
        return m2.group(1).strip()
    return s


def _udr_name_parts(name: str) -> tuple[str, str] | None:
    """Return UDR's ambiguous ``(#prefix, unit)`` name components."""
    match = _UDR_NAME_PARTS_RE.match(str(name or "").strip())
    if not match:
        return None
    return match.group(1).strip(), match.group(2).strip()


def _udr_prefix_is_building(items: list[Any]) -> bool:
    """Distinguish a physical building prefix from UDR's sequence label.

    UDR uses the same JSON-LD name slot for two incompatible shapes:

    * Cambridge Woods: ``Apartment #8 - 4020`` where ``8`` is only a page
      sequence and the public unit is ``4020``.
    * Arbor Park: ``Apartment #03R - 0202`` where ``03R`` is the building and
      the public unit is ``03R-0202``.
    * Vitruvian West: ``Apartment #1 - 124`` where numeric building prefixes
      repeat across many apartments and the public unit is ``1-124``.

    Alphanumeric prefixes are physical. Numeric prefixes are physical only
    when repeated within the property ItemList; unique numeric prefixes remain
    sequence labels. This property-level inference avoids inventing a prefix
    from one ambiguous row while preserving the exact public IDs on the two
    live repeated-building shapes above.
    """
    prefixes: list[str] = []
    for list_item in items:
        if not isinstance(list_item, dict):
            continue
        item = list_item.get("item")
        if not isinstance(item, dict):
            continue
        parts = _udr_name_parts(str(item.get("name") or ""))
        if parts:
            prefixes.append(parts[0])
    if any(not prefix.isdigit() for prefix in prefixes):
        return True
    return any(count > 1 for count in Counter(prefixes).values())


def _is_udr_apartment_item(item: Any) -> bool:
    """Schema.org Apartment / Apartment+Product gate. UDR uses both
    spellings (``"@type": "Apartment"`` and ``"@type": ["Apartment",
    "Product"]``); accept either."""
    if not isinstance(item, dict):
        return False
    t = item.get("@type")
    if isinstance(t, str):
        return t.lower() == "apartment"
    if isinstance(t, list):
        return any(isinstance(s, str) and s.lower() == "apartment" for s in t)
    return False


def _walk_jsonld_blocks(html: str) -> list[Any]:
    """Pull every ``<script type='application/ld+json'>`` block, parse
    each as JSON, return the parsed payloads. Malformed blocks are
    silently skipped — UDR pages often carry 2 JSON-LD blocks
    (BreadcrumbList + ItemList) and only one is the ItemList we want.
    """
    blocks: list[Any] = []
    for m in re.finditer(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return blocks


def parse_udr_jsonld(html: str, source_url: str = "") -> list[dict[str, Any]]:
    """Extract unit records from a UDR ``apartments-pricing`` page.

    Returns an empty list when no UDR-shape ItemList is present (so
    callers can chain to the next tier without a confidence drop).
    """
    if not html:
        return []
    units: list[dict[str, Any]] = []
    seen_unitids: set[str] = set()
    view_model_dates = _udr_view_model_dates(html)

    for block in _walk_jsonld_blocks(html):
        # We want ItemList with itemListElement containing Apartment
        if not isinstance(block, dict):
            continue
        if block.get("@type") != "ItemList":
            continue
        items = block.get("itemListElement") or []
        if isinstance(items, dict):
            # 2026-07-11 audit: single-unit communities serialize
            # itemListElement as ONE ListItem dict, not a list
            # (Edgewater — 1 available unit). Wrap it.
            items = [items]
        if not isinstance(items, list):
            continue

        prefix_is_building = _udr_prefix_is_building(items)

        for li in items:
            if not isinstance(li, dict):
                continue
            item = li.get("item")
            if not _is_udr_apartment_item(item):
                continue
            assert isinstance(item, dict)  # narrowed by _is_udr_apartment_item

            raw_name = str(item.get("name") or "").strip()
            name_parts = _udr_name_parts(raw_name)
            building = name_parts[0] if prefix_is_building and name_parts else ""
            unit_number = (
                f"{name_parts[0]}-{name_parts[1]}"
                if prefix_is_building and name_parts
                else _extract_unit_from_udr_name(raw_name)
            )
            if not unit_number:
                continue

            # Pull rent + sqft + beds + baths from the offer / floorSize
            # / numberOf* attributes — all standard Schema.org keys.
            offer = item.get("offers") or {}
            if isinstance(offer, list) and offer:
                # Some pages wrap a single offer in a list; take first.
                offer = offer[0]
            price = offer.get("price") if isinstance(offer, dict) else None
            rent_val = money_to_int(str(price)) if price is not None else None

            url_for_unitid = ""
            if isinstance(offer, dict):
                url_for_unitid = str(offer.get("url") or item.get("url") or "")
            else:
                url_for_unitid = str(item.get("url") or "")
            unitid_m = _UDR_UNITID_RE.search(url_for_unitid)
            internal_unitid = unitid_m.group(1) if unitid_m else ""

            # Dedup: a unit can show up once per query/listing — the
            # internal unitid is the stable cross-reference.
            if internal_unitid and internal_unitid in seen_unitids:
                continue
            if internal_unitid:
                seen_unitids.add(internal_unitid)

            floor_size = item.get("floorSize") or {}
            sqft_val: int | None = None
            if isinstance(floor_size, dict):
                fs_v = floor_size.get("value")
                if isinstance(fs_v, (int, float)):
                    sqft_val = int(fs_v)
                elif isinstance(fs_v, str) and fs_v.strip().isdigit():
                    sqft_val = int(fs_v)

            beds = item.get("numberOfBedrooms")
            baths = item.get("numberOfBathroomsTotal") or item.get("numberOfBathrooms")

            # Availability: Schema.org "InStock" → AVAILABLE, anything
            # else (OutOfStock / PreOrder) → UNAVAILABLE.
            avail_raw = (
                offer.get("availability") if isinstance(offer, dict) else ""
            ) or ""
            status = "AVAILABLE" if "instock" in str(avail_raw).lower() else "UNAVAILABLE"

            # Floor plan name — UDR ships description like
            # "2 Beds | 1.5 Baths | 1309 Sq. Ft" which isn't really
            # a plan name. Try to derive from the image filename in
            # ``image`` URL (e.g. cambridgewoods_b15t_combined_3d.gif
            # → b15t → "B1.5T"). Fall back to "" — schema_v2 can join
            # plan-name from elsewhere.
            #
            # 2026-05-24 (user Q): the website displays "Plan B1.5T"
            # (with period). Image filename strips the period; we
            # restore it by inserting a "." between two consecutive
            # digits after the leading letter(s). Verified live across
            # all 13 Cambridge Woods plans: a1a, a1b, a1c, a1d, a1e,
            # b15t→B1.5T, b25at→B2.5AT, b25bt→B2.5BT, etc.
            floor_plan_name = ""
            image_url = item.get("image") or ""
            if isinstance(image_url, str) and image_url:
                # extract the segment between '_' chars that's the plan
                # code — UDR's convention is community_planCode_*.gif
                plan_m = re.search(
                    r"/floor-plans/[^/]*?_([a-z0-9]+)(?:_[a-z0-9]+)*\.(?:gif|png|jpe?g|webp)",
                    image_url,
                    re.IGNORECASE,
                )
                if plan_m:
                    raw_code = plan_m.group(1)
                    floor_plan_name = _format_udr_plan_code(raw_code)

            beds_str = str(int(beds)) if isinstance(beds, (int, float)) else (
                str(beds) if beds else ""
            )
            baths_str = (
                f"{float(baths):.1f}".rstrip("0").rstrip(".")
                if isinstance(baths, (int, float))
                else (str(baths) if baths else "")
            )

            units.append(
                make_unit_dict(
                    floor_plan_name=floor_plan_name,
                    bed_label=bed_label_from(
                        int(beds) if isinstance(beds, (int, float)) else None,
                        floor_plan_name,
                    ),
                    bedrooms=beds_str,
                    bathrooms=baths_str,
                    sqft=str(sqft_val) if sqft_val else "",
                    unit_number=unit_number,
                    building=building,
                    rent_range=format_rent_range(rent_val, rent_val),
                    availability_status=status,
                    # JSON-LD does not carry the move-in date, but UDR's
                    # adjacent first-party view model does, keyed by the same
                    # displayed marketing unit number.
                    availability_date=(
                        view_model_dates.get(f"id:{internal_unitid}", "")
                        if internal_unitid
                        else ""
                    )
                    or view_model_dates.get(
                        "label:"
                        + (
                            name_parts[1]
                            if name_parts
                            else unit_number
                        ).upper(),
                        "",
                    ),
                    source_ids=(
                        {"udr_unitid": internal_unitid} if internal_unitid else {}
                    ),
                    source_api_url=source_url,
                    extraction_tier="TIER_1_JSONLD_UDR",
                )
            )
    return units


__all__ = [
    "canonical_udr_url_from_html",
    "is_udr_url",
    "parse_udr_jsonld",
    "udr_pricing_urls",
    "_udr_view_model_dates",
    "_extract_unit_from_udr_name",
]
