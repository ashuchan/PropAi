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
from typing import Any

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

# Extract unitid URL param (preserved as source_id for provenance).
_UDR_UNITID_RE = re.compile(r"[?&]unitid=(\d+)", re.IGNORECASE)


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
    m = _UDR_NAME_RE.match(name.strip())
    if m:
        return m.group(1).strip()
    return name.strip()


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

    for block in _walk_jsonld_blocks(html):
        # We want ItemList with itemListElement containing Apartment
        if not isinstance(block, dict):
            continue
        if block.get("@type") != "ItemList":
            continue
        items = block.get("itemListElement") or []
        if not isinstance(items, list):
            continue

        for li in items:
            if not isinstance(li, dict):
                continue
            item = li.get("item")
            if not _is_udr_apartment_item(item):
                continue
            assert isinstance(item, dict)  # narrowed by _is_udr_apartment_item

            raw_name = str(item.get("name") or "").strip()
            unit_number = _extract_unit_from_udr_name(raw_name)
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
                    rent_range=format_rent_range(rent_val, rent_val),
                    availability_status=status,
                    availability_date="",  # UDR JSON-LD doesn't carry move-in date
                    source_ids=(
                        {"udr_unitid": internal_unitid} if internal_unitid else {}
                    ),
                    source_api_url=source_url,
                    extraction_tier="TIER_1_JSONLD_UDR",
                )
            )
    return units


__all__ = [
    "parse_udr_jsonld",
    "_extract_unit_from_udr_name",
]
