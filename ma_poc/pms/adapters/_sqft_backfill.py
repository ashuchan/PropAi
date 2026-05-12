"""Floor-plan sqft context extraction and unit backfill.

Problem solved
--------------
Some properties expose two kinds of API responses during a single page load:

  1. A **unit-level API** — per-apartment records with unit_number, rent,
     beds, baths, availability. Sqft is absent or null because the PMS
     stores it at the floor-plan level, not the unit level.

  2. A **floor-plan-aggregate API** — one record per floor plan type, with
     beds, sqft range, and rent range, but *no* per-unit identity.
     Examples: repli360.com ``get_apartmentsync_data_for_floorplan_multi_template``,
     RentCafe ``getFloorplans``, generic ``/floor-plans`` endpoints.

The tier cascade picks the unit-level API as the winner (it has unit_ids and
rent). The floor-plan API is either skipped by ``has_unit_signals`` (too few
signal keys with real values) or tagged as noise by the LLM because it "does
not provide individual unit data." Result: all emitted units have area=-1.

This module recovers the sqft by:
  1. Scanning ALL captured API responses for floor-plan-aggregate shapes.
  2. Building a ``(beds_int, baths_int) → sqft_int`` lookup (the context).
  3. Filling ``sqft`` on units where it is absent and the context gives an
     *unambiguous* value for that (beds, baths) combination.

Design constraints
------------------
* **Never-raise** — every function is wrapped so a bug here can never crash
  the run. Failures are returned as an empty context / zero fill count.
* **Unambiguous only** — if two floor plans share the same (beds, baths) but
  have different sqft, the field is left blank. Silently assigning the wrong
  sqft to a unit is worse than leaving it absent.
* **Source-tagged** — backfilled units get ``_sqft_backfill_source`` so
  analytics can distinguish real vs inferred sqft.
* **Additive** — only fills units where sqft is absent. Never overwrites a
  real value, even if the context disagrees.
"""

from __future__ import annotations

import logging
import statistics
from typing import Any

log = logging.getLogger(__name__)

# ── Sqft field names (checked after lowercasing all keys) ────────────────────

_SQFT_SINGLE_KEYS: tuple[str, ...] = (
    "sqft", "squarefeet", "squarefootage", "square_feet", "square_footage",
    "sq_ft", "sqft_net", "netsqft", "floorsize", "floor_size",
    "unitsize", "unit_size", "size",
)
_SQFT_MIN_KEYS: tuple[str, ...] = (
    "minimumsqft", "minsqft", "minimum_sqft", "min_sqft",
    "minimumsquarefeet", "minsquarefeet", "minimumsize",
)
_SQFT_MAX_KEYS: tuple[str, ...] = (
    "maximumsqft", "maxsqft", "maximum_sqft", "max_sqft",
    "maximumsquarefeet", "maxsquarefeet", "maximumsize",
)

# Field names that indicate a real per-unit identity (vs floor-plan group)
_UNIT_ID_KEYS: tuple[str, ...] = (
    "unitnumber", "unit_number", "unitid", "unit_id",
    "apartmentname", "apartmentid", "apartment_id",
    "displayunitnumber", "display_unit_number",
)

# Beds field names (checked after lowercasing)
_BEDS_KEYS: tuple[str, ...] = (
    "beds", "bedrooms", "bedroom", "bed",
    "numbedrooms", "numofbedrooms", "numberofbedrooms",
    "bedcount", "bed_count", "bedroomcount", "bedroom_count",
)

# Baths field names (checked after lowercasing)
_BATHS_KEYS: tuple[str, ...] = (
    "baths", "bathrooms", "bathroom", "bath",
    "numbathrooms", "numofbathrooms", "numberofbathrooms",
    "bathcount", "bath_count", "bathroomcount", "bathroom_count",
    "bathtotal",
)

# Labels that map to 0-bed (studio) in bed-label strings
_STUDIO_LABELS: frozenset[str] = frozenset({"studio", "stu", "s", "efficiency"})

# Minimum realistic apartment sqft — mirrors _format_area / sanity_bound
_SQFT_FLOOR = 150
# Floor-plan catalog heuristic: if > this fraction of items have a real
# unit_id/unit_number, it's probably unit-level data, not a catalog
_UNIT_ID_FRACTION_THRESHOLD = 0.35
# Maximum items in a response we're willing to treat as a floor-plan catalog.
# Real unit lists often have 50-500 items; floor plan catalogs rarely exceed 25.
_CATALOG_MAX_ITEMS = 50


# ── Internal helpers ──────────────────────────────────────────────────────────


def _lc(item: dict[str, Any]) -> dict[str, Any]:
    """Return a lowercase-key copy of *item*."""
    return {k.lower(): v for k, v in item.items()}


def _first_val(item_lc: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first non-None, non-empty value found under *keys*."""
    for k in keys:
        v = item_lc.get(k)
        if v not in (None, "", "null", "None"):
            return v
    return None


def _parse_int(v: Any) -> int | None:
    """Coerce to int; return None on failure."""
    if v is None:
        return None
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


def _parse_beds_from_label(label: str) -> int | None:
    """Parse beds from a human-readable label like '1 Bed', '2BR', 'Studio'."""
    s = label.strip().lower()
    if s in _STUDIO_LABELS:
        return 0
    import re
    m = re.search(r"(\d+)\s*(?:bed|br\b|bedroom)", s)
    if m:
        return int(m.group(1))
    return None


def _extract_sqft_from_item(item_lc: dict[str, Any]) -> int | None:
    """Extract a single representative sqft from a floor-plan item.

    Priority order:
      1. A single-value key (``sqft``, ``squareFeet``, etc.)
      2. Range midpoint from min/max keys
      3. Min only (when max absent)

    Returns None if no value >= _SQFT_FLOOR found.
    """
    # 1. Single value
    single_raw = _first_val(item_lc, _SQFT_SINGLE_KEYS)
    if single_raw is not None:
        # May be a range string like "700-800"
        s = str(single_raw).strip()
        if "-" in s:
            parts = s.split("-", 1)
            lo = _parse_int(parts[0])
            hi = _parse_int(parts[1])
            if lo and hi and lo >= _SQFT_FLOOR and hi >= _SQFT_FLOOR:
                return (lo + hi) // 2
        val = _parse_int(single_raw)
        if val is not None and val >= _SQFT_FLOOR:
            return val

    # 2. Min/max range
    lo_raw = _first_val(item_lc, _SQFT_MIN_KEYS)
    hi_raw = _first_val(item_lc, _SQFT_MAX_KEYS)
    lo = _parse_int(lo_raw)
    hi = _parse_int(hi_raw)
    if lo and hi and lo >= _SQFT_FLOOR and hi >= _SQFT_FLOOR:
        return (lo + hi) // 2
    if lo and lo >= _SQFT_FLOOR:
        return lo
    if hi and hi >= _SQFT_FLOOR:
        return hi

    # 3. Check nested floorSize dict (JSON-LD pattern)
    fs = item_lc.get("floorsize")
    if isinstance(fs, dict):
        val = _parse_int(fs.get("value") or fs.get("minvalue") or fs.get("maxvalue"))
        if val and val >= _SQFT_FLOOR:
            return val

    return None


def _extract_beds_from_item(item_lc: dict[str, Any]) -> int | None:
    """Extract bed count from a floor-plan item."""
    # Try numeric beds keys first
    for k in _BEDS_KEYS:
        v = item_lc.get(k)
        if v is not None and v not in ("", "null", "None"):
            n = _parse_int(v)
            if n is not None and 0 <= n <= 10:
                return n

    # Try label-based bed extraction from floor_plan_name / name / bedLabel
    for label_key in ("bedlabel", "bed_label", "floorplanname", "floorplan_name",
                       "name", "planname", "plan_name", "typename", "type_name"):
        label = item_lc.get(label_key)
        if isinstance(label, str):
            beds = _parse_beds_from_label(label)
            if beds is not None:
                return beds

    return None


def _extract_baths_from_item(item_lc: dict[str, Any]) -> int | None:
    """Extract bath count from a floor-plan item (integer part only)."""
    for k in _BATHS_KEYS:
        v = item_lc.get(k)
        if v is not None and v not in ("", "null", "None"):
            n = _parse_int(v)
            if n is not None and 0 <= n <= 10:
                return n
    return None


def _has_real_unit_id(item_lc: dict[str, Any]) -> bool:
    """Return True when the item carries a real per-unit identifier."""
    for k in _UNIT_ID_KEYS:
        v = item_lc.get(k)
        if v not in (None, "", "null", "None", "0", 0):
            return True
    return False


def _is_floorplan_catalog(items: list[dict[str, Any]]) -> bool:
    """Heuristic: True when *items* looks like a floor-plan catalog.

    Criteria (all must hold):
      1. Item count <= _CATALOG_MAX_ITEMS (floor plan lists are short)
      2. At least 1 item has sqft >= _SQFT_FLOOR
      3. Fewer than _UNIT_ID_FRACTION_THRESHOLD of items have real unit IDs

    The third criterion distinguishes per-unit APIs (most items have unit_number)
    from floor-plan catalogs (items represent plan types, not individual units).
    """
    if not items or len(items) > _CATALOG_MAX_ITEMS:
        return False

    item_lcs = [_lc(item) for item in items if isinstance(item, dict)]
    if not item_lcs:
        return False

    has_sqft = any(_extract_sqft_from_item(lc) is not None for lc in item_lcs)
    if not has_sqft:
        return False

    unit_id_count = sum(1 for lc in item_lcs if _has_real_unit_id(lc))
    unit_id_fraction = unit_id_count / len(item_lcs)
    if unit_id_fraction >= _UNIT_ID_FRACTION_THRESHOLD:
        return False

    return True


def _find_item_list(body: Any) -> list[dict[str, Any]] | None:
    """Extract the first list of dicts from *body*, handling common envelope shapes."""
    if isinstance(body, list):
        dicts = [x for x in body if isinstance(x, dict)]
        return dicts if dicts else None
    if isinstance(body, dict):
        # Direct list value
        for v in body.values():
            if isinstance(v, list):
                dicts = [x for x in v if isinstance(x, dict)]
                if dicts:
                    return dicts
        # One level of nesting
        for v in body.values():
            if isinstance(v, dict):
                for v2 in v.values():
                    if isinstance(v2, list):
                        dicts = [x for x in v2 if isinstance(x, dict)]
                        if dicts:
                            return dicts
    return None


# ── Public API ────────────────────────────────────────────────────────────────

# Type alias: (beds, baths) → sqft.  baths may be None when absent.
SqftContext = dict[tuple[int | None, int | None], int]


def build_floorplan_sqft_context(
    api_responses: list[dict[str, Any]],
) -> SqftContext:
    """Scan all captured API responses and build a (beds, baths) → sqft lookup.

    Inspects every captured API response body. For each one that looks like
    a floor-plan catalog (short list, has sqft, lacks per-unit IDs), extracts
    a (beds, baths) → [sqft...] mapping.

    Final context: only (beds, baths) pairs with a **single distinct sqft
    value** across all catalog APIs are included. Ambiguous pairs (multiple
    distinct sqft values for the same bed/bath combo) are dropped so the
    backfill never silently assigns the wrong sqft.

    Args:
        api_responses: List of captured API response dicts, each with ``url``
            and ``body``. Does not have to be filtered — non-catalog responses
            are skipped silently.

    Returns:
        Dict mapping ``(beds_int, baths_int_or_None)`` to ``sqft_int``.
        Empty dict when no catalog APIs were found or all combos were ambiguous.
    """
    # Accumulate all sqft values seen per (beds, baths) key across all APIs
    raw: dict[tuple[int | None, int | None], list[int]] = {}

    for resp in api_responses:
        body = resp.get("body")
        if body is None:
            continue
        try:
            items = _find_item_list(body)
            if not items:
                continue
            if not _is_floorplan_catalog(items):
                continue
            # Extract sqft context from this catalog
            for item in items:
                if not isinstance(item, dict):
                    continue
                lc = _lc(item)
                sqft = _extract_sqft_from_item(lc)
                if sqft is None:
                    continue
                beds = _extract_beds_from_item(lc)
                baths = _extract_baths_from_item(lc)
                key = (beds, baths)
                raw.setdefault(key, []).append(sqft)
        except Exception:
            # Never crash the run on a parse error
            continue

    # Keep only unambiguous mappings (one distinct sqft per key)
    context: SqftContext = {}
    for key, sqft_list in raw.items():
        distinct = set(sqft_list)
        if len(distinct) == 1:
            context[key] = distinct.pop()
        # Ambiguous: try median as a best-effort fallback only when values
        # are close (within 10% of each other) — avoids large misassignments
        elif len(distinct) > 1:
            vals = sorted(distinct)
            lo, hi = vals[0], vals[-1]
            if lo > 0 and (hi - lo) / lo <= 0.10:
                context[key] = round(statistics.median(sqft_list))

    return context


def _unit_needs_sqft(unit: dict[str, Any]) -> bool:
    """True when the unit is missing a real sqft value."""
    sqft = unit.get("sqft") or unit.get("area") or unit.get("_sqft")
    if sqft in (None, "", "0", "0.0", "-1", -1, 0, 0.0, "null", "None"):
        return True
    if isinstance(sqft, str):
        # Catch range strings that _format_area can't parse (e.g., "")
        s = sqft.strip()
        return not s or s in ("0", "-1")
    if isinstance(sqft, (int, float)):
        return sqft <= 0
    return True


def _unit_beds_baths(unit: dict[str, Any]) -> tuple[int | None, int | None]:
    """Extract (beds, baths) from a raw adapter unit dict."""
    beds_raw = (
        unit.get("beds") or unit.get("bedrooms") or unit.get("_bedrooms")
    )
    baths_raw = (
        unit.get("baths") or unit.get("bathrooms") or unit.get("_bathrooms")
    )
    beds = _parse_int(beds_raw)
    baths = _parse_int(baths_raw)
    # Infer studio from floor_plan_name if beds still unknown
    if beds is None:
        fp = str(unit.get("floor_plan_name") or unit.get("bed_label") or "").lower()
        if "studio" in fp:
            beds = 0
    return beds, baths


def backfill_unit_sqft(
    units: list[dict[str, Any]],
    sqft_context: SqftContext,
) -> int:
    """Fill sqft on units where it is absent, using the floor-plan context.

    Lookup strategy (tried in order):
      1. Exact (beds, baths) key
      2. (beds, None) key — when baths are absent from the context
      3. (None, None) key — only when the unit also has no beds (rare fallback)

    Only fills when the context gives exactly one sqft value for the key
    (guaranteed by ``build_floorplan_sqft_context``). Marks filled units
    with ``_sqft_backfill_source="floorplan_context"`` for observability.

    Args:
        units: Raw adapter unit dicts (in-place mutation).
        sqft_context: From ``build_floorplan_sqft_context``.

    Returns:
        Count of units that were filled.
    """
    if not sqft_context:
        return 0

    filled = 0
    for unit in units:
        if not isinstance(unit, dict):
            continue
        if not _unit_needs_sqft(unit):
            continue

        beds, baths = _unit_beds_baths(unit)

        # Try most-specific key first, then progressively looser
        sqft: int | None = None
        for key in ((beds, baths), (beds, None), (None, None)):
            if key in sqft_context:
                sqft = sqft_context[key]
                break

        if sqft is not None:
            unit["sqft"] = str(sqft)
            unit["_sqft_backfill_source"] = "floorplan_context"
            filled += 1

    return filled


def run_sqft_backfill(
    units: list[dict[str, Any]],
    api_responses: list[dict[str, Any]],
) -> tuple[int, int]:
    """Build sqft context from all captured APIs and backfill units.

    Convenience wrapper that runs both steps and returns diagnostic counts.

    Returns:
        (context_keys, units_filled) — number of (beds, baths) pairs in the
        context and number of units whose sqft was filled.
    """
    try:
        context = build_floorplan_sqft_context(api_responses)
        filled = backfill_unit_sqft(units, context) if context else 0
        return len(context), filled
    except Exception as exc:
        log.warning("sqft_backfill: unexpected error — %s", exc, exc_info=True)
        return 0, 0
