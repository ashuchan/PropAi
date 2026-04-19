"""
Multi-property scraper.
========================
Reads a CSV of properties (config/properties.csv by default), scrapes each
property's website using entrata.scrape(), enriches with extracted metadata,
transforms units into the target schema, and emits a single JSON array.

Output schema (per property):

{
  "Property Name": ..., "Type": ..., "Unique ID": ..., "Property ID": ...,
  "Average Unit Size (SF)": ..., "Census Block Id": null, "City": ...,
  "Construction Finish Date": null, "Construction Start Date": null,
  "Development Company": null, "Latitude": ..., "Longitude": ...,
  "Management Company": ..., "Market Name": null, "Property Owner": null,
  "Property Address": ..., "Property Status": ..., "Property Type": ...,
  "Region": null, "Renovation Finish": null, "Renovation Start": null,
  "State": ..., "Stories": null, "Submarket Name": null, "Total Units": ...,
  "Tract Code": null, "Year Built": null, "ZIP Code": ..., "Lease Start Date": null,
  "First Move-In Date": ..., "Property Style": ..., "Update Date": ...,
  "Unit Mix": ..., "Asset Grade in Submarket": null, "Asset Grade in Market": null,
  "Phone": ..., "Website": ...,
  "units": [
    {
      "unit_id": "string",
      "market_rent_low": number | null,
      "market_rent_high": number | null,
      "available_date": "YYYY-MM-DD" | null,
      "lease_link": "string" | null,
      "concessions": "string" | null,
      "amenities": "string" | null
    }
  ]
}

Fields set to null are NOT obtainable from a property website — they require
external data sources (CoStar, county assessor, US Census API, RealPage, etc.).

Usage:
  python scripts/scrape_properties.py --csv config/properties.csv --out output/properties.json
  python scripts/scrape_properties.py --limit 5
  python scripts/scrape_properties.py --start-at 10 --limit 5
  python scripts/scrape_properties.py --proxy http://user:pass@host:port
"""

import argparse
import asyncio
import csv
import json
import re
import statistics
import sys
import urllib.parse
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

# Force UTF-8 stdout on Windows so emoji prints in entrata.py don't crash.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

# Make `entrata` importable when run from anywhere.
_HERE = Path(__file__).resolve().parent
_MA_POC_ROOT = _HERE.parent  # ma_poc/
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from entrata import scrape  # noqa: E402

# ── Unit transformation ────────────────────────────────────────────────────────

ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
US_DATE_RE  = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$")

def _to_iso_date(s: Any) -> str | None:
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    m = ISO_DATE_RE.match(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = US_DATE_RE.match(s)
    if m:
        mm, dd, yy = m.group(1), m.group(2), m.group(3)
        if len(yy) == 2:
            yy = "20" + yy
        return f"{int(yy):04d}-{int(mm):02d}-{int(dd):02d}"
    # "Available May 12th" / "May 12, 2026" — leave for caller
    try:
        # Last-ditch try: full ISO datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, TypeError):
        return None

def _money_to_int(s: Any) -> int | None:
    if s is None:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(s))
    if not cleaned or cleaned == ".":
        return None
    try:
        return int(float(cleaned))
    except ValueError:
        return None

def _sightmap_lease_link(unit: dict, fp: dict) -> str | None:
    """First non-null leasing URL we can find on the SightMap unit/floor plan."""
    for key in ("leasing_price_url", "leasing_start_dates_url"):
        v = unit.get(key)
        if isinstance(v, str) and v:
            return v
    for ol in (unit.get("outbound_links") or []):
        if isinstance(ol, dict) and ol.get("url"):
            return str(ol["url"])
    for ol in (fp.get("outbound_links") or []):
        if isinstance(ol, dict) and ol.get("url"):
            return str(ol["url"])
    return None

def _sightmap_units_from_body(body: dict, source_url: str) -> list[dict]:
    """Extract target-schema units directly from a SightMap API body."""
    out: list[dict] = []
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return out
    raw_units = data.get("units") or []
    raw_fps   = data.get("floor_plans") or []
    if not isinstance(raw_units, list) or not raw_units:
        return out
    fp_by_id: dict[str, dict] = {}
    for fp in raw_fps if isinstance(raw_fps, list) else []:
        if isinstance(fp, dict) and fp.get("id") is not None:
            fp_by_id[str(fp["id"])] = fp
    for u in raw_units:
        if not isinstance(u, dict):
            continue
        fp = fp_by_id.get(str(u.get("floor_plan_id") or ""), {})
        # SightMap "price" is a flat scalar; "total_price" is a [lo, hi] range
        # representing min/max with available specials.
        price = u.get("price")
        lo = int(price) if isinstance(price, (int, float)) and price > 0 else _money_to_int(u.get("display_price"))
        hi = lo
        tp = u.get("total_price")
        if isinstance(tp, list) and len(tp) == 2:
            try:
                tp_lo, tp_hi = int(tp[0]), int(tp[1])
                if tp_lo > 0:
                    lo = tp_lo
                if tp_hi > 0:
                    hi = tp_hi
            except (TypeError, ValueError):
                pass
        out.append({
            "unit_id":          str(u.get("unit_number") or u.get("label") or u.get("id") or ""),
            "market_rent_low":  lo,
            "market_rent_high": hi,
            "available_date":   _to_iso_date(u.get("available_on")),
            "lease_link":       _sightmap_lease_link(u, fp),
            "concessions":      (u.get("specials_description") or None),
            "amenities":        None,  # SightMap stores amenities as filter IDs only.
            # Floor plan diagram image (SightMap exposes directly on the floor_plan).
            "floorplan_image_url": fp.get("image") or fp.get("image_3d") or fp.get("image_secondary") or None,
            # Carry sqft for property-level Average Unit Size aggregate.
            "_sqft":            int(u["area"]) if isinstance(u.get("area"), (int, float)) and u["area"] > 0 else None,
            "_floor_plan":      fp.get("name") or fp.get("filter_label") or "",
            "_bedrooms":        fp.get("bedroom_count"),
        })
    return out

# Apartment rent sanity bounds (USD/month). Rejects garbage like "rent=14" that
# pops out of misidentified fields, while still allowing low-cost markets.
_RENT_MIN = 200
_RENT_MAX = 50000

# Extended to include generic keys like "id", "label", "name" — many PMS
# APIs (e.g. ResMan, Yardi) use plain "id" for unit identifiers.  The gate
# at line ~203 requires BOTH an id-key AND a rent-key to be present, so
# "id" alone won't cause false positives on non-unit lists.
_UNIT_ID_KEYS  = {"unit_number", "unitNumber", "unit_id", "unitId", "UnitNumber",
                  "id", "label", "name", "ID", "unit_name", "unitName"}
# Extended: some APIs nest rent inside an object (e.g. rent: {min, max})
# rather than flat keys.  The _extract_rent() helper handles both.
_RENT_KEYS     = {"price", "minRent", "askingRent", "rent", "monthlyRent",
                  "minPrice", "startingPrice", "base_rent", "baseRent",
                  "display_price", "displayPrice", "monthly_rent",
                  "rentTerms", "pricing", "market_rent"}

def _extract_rent(u: dict) -> tuple[int | None, int | None]:
    """Extract (rent_low, rent_high) from a unit/floorplan dict.

    Handles two patterns:
      1. Flat keys:   {"minRent": 1450, "maxRent": 1600}
      2. Nested dict: {"rent": {"min": 1351, "max": 1351}}
                      {"rentTerms": [{"rent": 1200, ...}]}
                      {"pricing": {"effectiveRent": 1500}}
    Returns (None, None) if no rent found.
    """
    _LO_KEYS = ("price", "minRent", "askingRent", "rent", "monthlyRent",
                 "minPrice", "startingPrice", "base_rent", "baseRent",
                 "display_price", "monthly_rent", "market_rent",
                 "rentTerms", "pricing")
    _HI_KEYS = ("maxRent", "price_max", "max_price", "maxPrice", "rent_max")

    lo: int | None = None
    hi: int | None = None

    # Try flat scalar keys first.
    for k in _LO_KEYS:
        v = u.get(k)
        if v is None:
            continue
        # Nested dict: rent: {min: X, max: Y}
        if isinstance(v, dict):
            lo = _money_to_int(v.get("min") or v.get("low") or v.get("amount")
                               or v.get("effectiveRent") or v.get("value"))
            hi = _money_to_int(v.get("max") or v.get("high"))
            if lo:
                break
            continue
        # Nested list: rentTerms: [{rent: 1200, term: 12}, ...]
        if isinstance(v, list) and v and isinstance(v[0], dict):
            best = None
            for term in v:
                r = _money_to_int(term.get("rent") or term.get("price") or term.get("amount"))
                if r and (best is None or r < best):
                    best = r
            if best:
                lo = best
                break
            continue
        lo = _money_to_int(v)
        if lo:
            break

    for k in _HI_KEYS:
        v = u.get(k)
        if v is not None:
            hi = _money_to_int(v)
            if hi:
                break
    if hi is None:
        hi = lo

    return (lo, hi)


def _generic_units_from_body(body, source_url: str) -> list[dict]:
    """
    Best-effort generic transform when no dedicated parser matches.
    Requires each candidate list to look like real per-unit data:
      - list has ≥3 dict entries
      - the first entry has BOTH a unit-id key AND a rent-like key
    Each emitted record must have a unit_id AND a rent in the sanity range.
    """
    out: list[dict] = []
    if not isinstance(body, (dict, list)):
        return out

    candidates: list[dict] = []
    lists_checked = 0
    lists_rejected_no_id = 0
    lists_rejected_no_rent = 0
    stack: list = [body]
    while stack:
        node = stack.pop()
        if isinstance(node, list) and len(node) >= 3 and isinstance(node[0], dict):
            lists_checked += 1
            sample = node[0]
            has_id   = any(k in sample for k in _UNIT_ID_KEYS)
            has_rent = any(k in sample for k in _RENT_KEYS)
            if has_id and has_rent:
                candidates.extend(node)
                continue
            if not has_id:
                lists_rejected_no_id += 1
            if not has_rent:
                lists_rejected_no_rent += 1
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)

    skipped_no_id = 0
    skipped_rent_bounds = 0
    for u in candidates:
        if not isinstance(u, dict):
            continue
        unit_id = ""
        for k in _UNIT_ID_KEYS:
            v = u.get(k)
            if v is not None and str(v).strip():
                unit_id = str(v).strip()
                break
        if not unit_id:
            skipped_no_id += 1
            continue

        lo, hi = _extract_rent(u)

        if lo is None or lo < _RENT_MIN or lo > _RENT_MAX:
            skipped_rent_bounds += 1
            continue

        sqft_v = u.get("area") or u.get("sqft") or u.get("square_feet") or u.get("squareFeet")
        # Floor plan / unit image — many PMS APIs expose one of these keys.
        fp_img = (u.get("floorplan_image") or u.get("floorplanImage") or u.get("floorPlanImage")
                  or u.get("floor_plan_image") or u.get("floorplan_image_url") or u.get("floorPlanImageUrl")
                  or u.get("image") or u.get("imageUrl") or u.get("image_url")
                  or u.get("thumbnail") or u.get("photoUrl") or None)
        if isinstance(fp_img, dict):
            fp_img = fp_img.get("url") or fp_img.get("src") or None
        out.append({
            "unit_id":          unit_id,
            "market_rent_low":  lo,
            "market_rent_high": hi,
            "available_date":   _to_iso_date(u.get("available_on") or u.get("availableDate")
                                             or u.get("available_date") or u.get("moveInDate")),
            "lease_link":       u.get("leasing_price_url") or u.get("applyUrl") or None,
            "concessions":      u.get("specials_description") or u.get("concession")
                                or u.get("special") or u.get("specials") or None,
            "amenities":        None,
            "floorplan_image_url": fp_img if isinstance(fp_img, str) and fp_img.startswith("http") else None,
            "_sqft":            int(sqft_v) if isinstance(sqft_v, (int, float)) and sqft_v > 0 else None,
            "_floor_plan":      u.get("floorPlanName") or u.get("floor_plan_name")
                                or u.get("model_id") or u.get("name") or "",
            "_bedrooms":        u.get("bedroom_count") or u.get("bedrooms") or u.get("beds"),
        })

    # Diagnostic summary for debugging extraction failures.
    if lists_checked or candidates or out:
        print(f"    generic_parser({source_url[:60]}): "
              f"{lists_checked} lists checked, "
              f"{len(candidates)} candidates "
              f"({lists_rejected_no_id} lists had no id-key, "
              f"{lists_rejected_no_rent} no rent-key), "
              f"{skipped_no_id} skipped no-id, "
              f"{skipped_rent_bounds} skipped rent-bounds "
              f"→ {len(out)} units emitted")

    return out

def _realpage_units_from_body(body, source_url: str) -> list[dict]:
    """Parse RealPage API responses (api.ws.realpage.com).

    RealPage splits data across two endpoints:
      /floorplans → {response: {floorplans: [...]}}  (beds, baths, sqft, deposit)
      /units      → {response: [{unitNumber, rent, availableDate, ...}]}

    The /units endpoint can return ``null`` when no units are available.
    In that case we still emit floorplan-level records (no rent, but beds/sqft).
    When /units IS present, we emit one record per unit with rent.
    """
    out: list[dict] = []
    if not isinstance(body, dict):
        return out
    resp = body.get("response")
    if resp is None:
        print(f"    realpage_parser({source_url[:60]}): response is null — "
              f"no available units at this time")
        return out

    # Case 1: response is a dict with "floorplans" key → /floorplans endpoint
    if isinstance(resp, dict) and "floorplans" in resp:
        fp_list = resp.get("floorplans") or []
        print(f"    realpage_parser({source_url[:60]}): "
              f"/floorplans endpoint, {len(fp_list)} floor plans")
        for fp in resp.get("floorplans") or []:
            if not isinstance(fp, dict):
                continue
            fp_id = str(fp.get("id") or fp.get("name") or "")
            beds = fp.get("bedRooms") or fp.get("bedrooms")
            fp.get("bathRooms") or fp.get("bathrooms")
            sqft = fp.get("sqft") or fp.get("squareFeet")
            sqft_v = None
            if isinstance(sqft, (int, float)) and sqft > 0:
                sqft_v = int(sqft)
            # Floorplans don't always have rent — emit anyway for bed/sqft data.
            lo = _money_to_int(fp.get("minRent") or fp.get("rentMin"))
            hi = _money_to_int(fp.get("maxRent") or fp.get("rentMax"))
            if hi is None:
                hi = lo
            out.append({
                "unit_id":          fp_id,
                "market_rent_low":  lo,
                "market_rent_high": hi,
                "available_date":   None,
                "lease_link":       None,
                "concessions":      None,
                "amenities":        None,
                "floorplan_image_url": fp.get("imageUrl") or fp.get("image") or fp.get("floorPlanImage") or None,
                "_sqft":            sqft_v,
                "_floor_plan":      fp.get("name") or fp_id,
                "_bedrooms":        beds,
            })
        return out

    # Case 2: response is a list → /units endpoint
    if isinstance(resp, list):
        print(f"    realpage_parser({source_url[:60]}): "
              f"/units endpoint, {len(resp)} raw units")
        for u in resp:
            if not isinstance(u, dict):
                continue
            uid = str(u.get("unitNumber") or u.get("unit_number") or u.get("unitId") or "")
            if not uid:
                continue
            lo = _money_to_int(u.get("rent") or u.get("minRent") or u.get("bestPrice"))
            hi = _money_to_int(u.get("maxRent"))
            if hi is None:
                hi = lo
            if lo is not None and (lo < _RENT_MIN or lo > _RENT_MAX):
                continue
            sqft_v = u.get("sqft") or u.get("squareFeet")
            out.append({
                "unit_id":          uid,
                "market_rent_low":  lo,
                "market_rent_high": hi,
                "available_date":   _to_iso_date(u.get("availableDate") or u.get("available_date")),
                "lease_link":       u.get("applyOnlineUrl") or None,
                "concessions":      u.get("concessions") or u.get("specials") or None,
                "amenities":        None,
                "floorplan_image_url": u.get("floorPlanImage") or u.get("floorplanImage") or u.get("imageUrl") or None,
                "_sqft":            int(sqft_v) if isinstance(sqft_v, (int, float)) and sqft_v > 0 else None,
                "_floor_plan":      u.get("floorPlanName") or u.get("floorplanName") or "",
                "_bedrooms":        u.get("bedrooms") or u.get("bedRooms"),
            })
        return out

    return out


def _avalon_units_from_body(body, source_url: str) -> list[dict]:
    """Parse AvalonBay community-units API responses.

    AvalonBay's ``/pf/api/v3/content/fetch/community-units`` endpoint returns
    a dict with ``units`` (list of individual apartment units) and optionally
    ``unitsSummary`` (promotional banners — ignored).

    Each unit has: ``unitId``, ``bedroomNumber``, ``bathroomNumber``,
    ``squareFeet``, ``floorPlanName``, ``pricing`` (nested dict with
    ``effectiveRent``, ``minRent``, ``maxRent``), ``availableDate``, etc.
    """
    out: list[dict] = []
    if not isinstance(body, dict):
        return out

    # The unit list lives directly at body["units"] — NOT inside unitsSummary.
    raw_units = body.get("units")
    if not isinstance(raw_units, list) or not raw_units:
        return out

    print(f"    avalon_parser({source_url[:60]}): "
          f"{len(raw_units)} raw units")

    for u in raw_units:
        if not isinstance(u, dict):
            continue
        uid = str(u.get("unitId") or u.get("unitNumber") or u.get("unitCode") or "")
        if not uid:
            continue

        # Avalon nests rent inside a pricing object or at top level.
        lo, hi = _extract_rent(u)
        if lo is None:
            pricing = u.get("pricing")
            if isinstance(pricing, dict):
                lo = _money_to_int(pricing.get("effectiveRent") or pricing.get("minRent")
                                   or pricing.get("rent"))
                hi = _money_to_int(pricing.get("maxRent")) or lo
        if hi is None:
            hi = lo
        if lo is not None and (lo < _RENT_MIN or lo > _RENT_MAX):
            continue

        sqft = u.get("squareFeet") or u.get("sqft")
        sqft_v = int(sqft) if isinstance(sqft, (int, float)) and sqft > 0 else None

        out.append({
            "unit_id":          uid,
            "market_rent_low":  lo,
            "market_rent_high": hi,
            "available_date":   _to_iso_date(u.get("availableDate") or u.get("moveInDate")
                                             or u.get("available_date")),
            "lease_link":       u.get("applyUrl") or u.get("applyOnlineUrl") or None,
            "concessions":      u.get("promotionTitle") or u.get("concession") or None,
            "amenities":        None,
            "floorplan_image_url": u.get("floorPlanImage") or u.get("imageUrl") or None,
            "_sqft":            sqft_v,
            "_floor_plan":      u.get("floorPlanName") or u.get("floorplanName") or "",
            "_bedrooms":        u.get("bedroomNumber") or u.get("bedrooms"),
        })
    return out


def transform_units_from_scrape(scrape_result: dict) -> list[dict]:
    """
    Walk every captured raw API body and the parser's normalised units to
    produce target-schema unit records, deduped by unit_id (or rent+sqft).
    """
    target: list[dict] = []
    seen: set[str] = set()

    def _add(rec: dict) -> None:
        key = rec.get("unit_id") or f"{rec.get('_floor_plan')}|{rec.get('_sqft')}|{rec.get('market_rent_low')}"
        if not key or key in seen:
            return
        seen.add(key)
        target.append(rec)

    raw_responses = scrape_result.get("_raw_api_responses") or []
    parser_used = "none"

    # Pass 1: dedicated parsers (host-specific). These are authoritative.
    dedicated_hit = False
    for resp in raw_responses:
        url = resp.get("url", "")
        body = resp.get("body")
        host = urllib.parse.urlparse(url).netloc.lower()
        if "sightmap.com" in host:
            before = len(target)
            for u in _sightmap_units_from_body(body, url):
                _add(u)
                dedicated_hit = True
            if len(target) > before:
                parser_used = "sightmap"
                print(f"  transform: SightMap parser → {len(target) - before} units from {url[:60]}")
        elif "realpage.com" in host:
            before = len(target)
            for u in _realpage_units_from_body(body, url):
                _add(u)
                dedicated_hit = True
            if len(target) > before:
                parser_used = "realpage"
                print(f"  transform: RealPage parser → {len(target) - before} units from {url[:60]}")
        elif ("avaloncommunities" in host or "avalonbay" in host
              or "community-units" in url.lower()):
            before = len(target)
            for u in _avalon_units_from_body(body, url):
                _add(u)
                dedicated_hit = True
            if len(target) > before:
                parser_used = "avalon"
                print(f"  transform: Avalon parser → {len(target) - before} units from {url[:60]}")

    # Pass 2: generic walker — only if no dedicated parser yielded anything,
    # because authoritative APIs make every other captured response noise.
    if not dedicated_hit:
        for resp in raw_responses:
            url = resp.get("url", "")
            body = resp.get("body")
            before = len(target)
            for u in _generic_units_from_body(body, url):
                _add(u)
            if len(target) > before:
                parser_used = "generic"

    # Fallback: if no API-level matches, use the parser's normalised units list.
    if not target and (scrape_result.get("units") or []):
        parser_used = "fallback_entrata_units"
        print(f"  transform: no API-level units — falling back to entrata.py "
              f"normalised units ({len(scrape_result.get('units', []))} raw)")
    if not target:
        for u in scrape_result.get("units") or []:
            rent_lo = _money_to_int(u.get("rent_range"))
            # rent_range can be "$1,000 - $2,000"
            rent_hi = rent_lo
            m = re.search(r"\$([\d,]+)\s*-\s*\$([\d,]+)", str(u.get("rent_range") or ""))
            if m:
                rent_lo = _money_to_int(m.group(1))
                rent_hi = _money_to_int(m.group(2))
            sqft_v = _money_to_int(u.get("sqft"))
            _add({
                "unit_id":          u.get("unit_number") or "",
                "market_rent_low":  rent_lo,
                "market_rent_high": rent_hi,
                "available_date":   _to_iso_date(u.get("availability_date")),
                "lease_link":       u.get("source_api_url") or None,
                "concessions":      u.get("concession") or None,
                "amenities":        None,
                "floorplan_image_url": u.get("floorplan_image_url") or u.get("floor_plan_image") or None,
                "_sqft":            sqft_v,
                "_floor_plan":      u.get("floor_plan_name") or "",
                "_bedrooms":        u.get("bedrooms") or None,
            })

    # Final summary — single line to diagnose "0 units" from logs.
    print(f"  transform_units: {len(raw_responses)} API responses, "
          f"parser={parser_used}, "
          f"{len(target)} units emitted "
          f"({len(seen) - len(target)} deduped)")

    return target

# ── Aggregates ────────────────────────────────────────────────────────────────

def aggregate_unit_stats(units: list[dict]) -> dict:
    """Compute Average Unit Size, Unit Mix, First Move-In Date, total count."""
    sqfts = [u["_sqft"] for u in units if u.get("_sqft")]
    avg_sqft = round(statistics.mean(sqfts)) if sqfts else None

    # Unit Mix grouped by bedroom count.
    mix_counter: Counter = Counter()
    for u in units:
        b = u.get("_bedrooms")
        try:
            b_int = int(float(b)) if b not in (None, "") else None
        except (TypeError, ValueError):
            b_int = None
        if b_int is None:
            label = "Unknown"
        elif b_int == 0:
            label = "Studio"
        else:
            label = f"{b_int}BR"
        mix_counter[label] += 1
    order = ["Studio", "1BR", "2BR", "3BR", "4BR", "5BR", "Unknown"]
    mix_parts = [f"{k}: {mix_counter[k]}" for k in order if mix_counter.get(k)]
    # Append any other label not in our predefined order.
    for k in mix_counter:
        if k not in order:
            mix_parts.append(f"{k}: {mix_counter[k]}")
    unit_mix = "; ".join(mix_parts) if mix_parts else None

    avail_dates = sorted([u["available_date"] for u in units if u.get("available_date")])
    first_move_in = avail_dates[0] if avail_dates else None

    return {
        "average_unit_size_sf": avg_sqft,
        "unit_mix":             unit_mix,
        "first_move_in_date":   first_move_in,
        "total_units_found":    len(units),
    }

# ── CSV row → output property record ──────────────────────────────────────────

def _csv_get(row: dict, *keys: str) -> str:
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""

def _clean(v: Any) -> Any:
    """Coerce literal 'null'/'none'/'' strings to real None."""
    if v is None:
        return None
    if isinstance(v, str) and v.strip().lower() in ("", "null", "none", "n/a"):
        return None
    return v

def _strip_internal(units: list[dict]) -> list[dict]:
    """Remove the underscore-prefixed helper fields before emitting."""
    return [{k: v for k, v in u.items() if not k.startswith("_")} for u in units]

def build_property_record(csv_row: dict, scrape_result: dict, target_units: list[dict]) -> dict:
    md = scrape_result.get("property_metadata") or {}
    stats = aggregate_unit_stats(target_units)

    csv_url = _csv_get(csv_row, "Property URL", "URL", "url")
    pid     = _csv_get(csv_row, "Property ID", "property_id", "id")

    # Prefer scraped name if it's a real property name; CSV name is reliable so default to it.
    property_name = _csv_get(csv_row, "Property Name", "name") or md.get("name") or ""

    # Prefer CSV address but fall back to JSON-LD.
    address = _csv_get(csv_row, "Address") or md.get("address") or ""
    city    = _csv_get(csv_row, "City") or md.get("city") or ""
    state   = _csv_get(csv_row, "State") or md.get("state") or ""
    zipc    = _csv_get(csv_row, "ZIP", "Zip", "Zip Code", "ZIP Code") or md.get("zip") or ""

    return {
        # Identity
        "Property Name":             property_name or None,
        "Type":                      _csv_get(csv_row, "Property Type") or "Multifamily",
        "Unique ID":                 pid or None,
        "Property ID":               pid or None,

        # Aggregates from scraped units
        "Average Unit Size (SF)":    stats["average_unit_size_sf"],
        "Total Units":               stats["total_units_found"] or _money_to_int(_csv_get(csv_row, "Total Units (Est.)")),
        "Unit Mix":                  stats["unit_mix"] or _csv_get(csv_row, "Unit Mix") or None,
        "First Move-In Date":        stats["first_move_in_date"],

        # Location (CSV first, scraped fallback)
        "City":                      city or None,
        "State":                     state or None,
        "ZIP Code":                  zipc or None,
        "Property Address":          address or None,
        "Latitude":                  md.get("latitude"),
        "Longitude":                 md.get("longitude"),

        # Classification
        "Property Type":             _csv_get(csv_row, "Building Type") or None,
        "Property Status":           _csv_get(csv_row, "Property Type") or None,
        "Property Style":            _csv_get(csv_row, "Building Type") or None,

        # Operations
        "Management Company":        _clean(_csv_get(csv_row, "Management Company")) or None,
        "Phone":                     _clean(md.get("telephone")),
        "Website":                   csv_url or scrape_result.get("base_url"),

        # Scraped from website (best effort)
        "Year Built":                md.get("year_built"),
        "Stories":                   md.get("stories"),

        # External-source-only fields — set to null
        "Census Block Id":           None,
        "Tract Code":                None,
        "Construction Start Date":   None,
        "Construction Finish Date":  None,
        "Renovation Start":          None,
        "Renovation Finish":         None,
        "Development Company":       None,
        "Property Owner":            None,
        "Region":                    None,
        "Market Name":               None,
        "Submarket Name":            None,
        "Asset Grade in Submarket":  None,
        "Asset Grade in Market":     None,
        "Lease Start Date":          None,

        "Property Image URL":        md.get("image_url") or None,
        "Property Gallery URLs":     md.get("gallery_urls") or [],

        "Update Date":               date.today().isoformat(),

        # Run diagnostics
        "_scrape_status":            scrape_result.get("extraction_tier_used"),
        "_scrape_errors":            scrape_result.get("errors") or [],

        "units":                     _strip_internal(target_units),
    }

# ── CSV reading ───────────────────────────────────────────────────────────────

def read_properties_csv(path: Path) -> list[dict]:
    """Read CSV with BOM-tolerant UTF-8. Returns list of dict rows."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)

# ── Orchestration ─────────────────────────────────────────────────────────────

async def run(csv_path: Path, out_path: Path, limit: int | None,
              start_at: int, proxy: str | None) -> None:
    rows = read_properties_csv(csv_path)
    print(f"\nLoaded {len(rows)} properties from {csv_path}")

    if start_at:
        rows = rows[start_at:]
    if limit:
        rows = rows[:limit]
    print(f"Processing {len(rows)} properties (start_at={start_at}, limit={limit})")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    properties_out: list[dict] = []

    for i, row in enumerate(rows, start=1):
        url = _csv_get(row, "Property URL", "URL", "url")
        pid = _csv_get(row, "Property ID", "property_id", "id")
        name = _csv_get(row, "Property Name", "name")
        print(f"\n{'#'*70}")
        print(f"# [{i}/{len(rows)}] {pid} — {name}")
        print(f"# {url}")
        print(f"{'#'*70}")

        if not url:
            print("  ⚠ Skipping: no URL in CSV row")
            properties_out.append(build_property_record(row, {"errors": ["no URL in CSV"]}, []))
            continue

        try:
            scrape_result = await scrape(url, proxy=proxy)
        except Exception as e:
            print(f"  ⚠ Scrape failed: {e}")
            scrape_result = {"errors": [f"scrape exception: {e}"], "base_url": url}

        target_units = transform_units_from_scrape(scrape_result)
        prop = build_property_record(row, scrape_result, target_units)
        properties_out.append(prop)
        print(f"  → {len(target_units)} units, avg sqft={prop['Average Unit Size (SF)']}, "
              f"mix={prop['Unit Mix']}")

        # Incremental write so a long run can be inspected mid-flight and an
        # interrupted run still leaves a usable file behind.
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(properties_out, f, indent=2, ensure_ascii=False, default=str)

    print(f"\n✅ Wrote {len(properties_out)} properties to {out_path}")
    total_units = sum(len(p.get("units") or []) for p in properties_out)
    print(f"   Total units across all properties: {total_units}")

def main():
    p = argparse.ArgumentParser(description="Multi-property scraper (CSV-driven)")
    p.add_argument("--csv",      default=str(_MA_POC_ROOT / "config" / "properties.csv"),
                   help="Path to properties CSV (default: ma_poc/config/properties.csv)")
    p.add_argument("--out",      default=str(_MA_POC_ROOT / "data" / "output" / "properties.json"),
                   help="Output JSON path (default: ma_poc/data/output/properties.json)")
    p.add_argument("--limit",    type=int, default=None,
                   help="Process at most N rows")
    p.add_argument("--start-at", type=int, default=0,
                   help="Skip first N rows")
    p.add_argument("--proxy",    default=None,
                   help="Proxy URL (e.g. http://user:pass@host:port)")
    args = p.parse_args()

    csv_path = Path(args.csv)
    out_path = Path(args.out)

    if not csv_path.exists():
        print(f"❌ CSV not found: {csv_path}")
        sys.exit(1)

    asyncio.run(run(csv_path, out_path, args.limit, args.start_at, args.proxy))

if __name__ == "__main__":
    main()
