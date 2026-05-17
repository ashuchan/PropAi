"""Canonical accessors for unit dicts.

A unit dict in this codebase travels through ~12 producers (PMS adapters,
LLM extractors, JSON-LD parsers, DOM scanners, the cross-source merger),
each emitting its own field-name convention:

  * ``make_unit_dict`` shape:   bedrooms, bathrooms, sqft, unit_number, rent_range
  * RealPage / merger transit:  _bedrooms, _bathrooms, _sqft, _floor_plan,
                                _unit_number
  * V2 canonical shape:         beds, baths, area, rent_low, rent_high
  * ProvenancedUnit (merger):   FieldValue-wrapped values (need .value unwrap)
  * Windsor RentCafe PascalCase: BedRooms, BathRooms, FloorplanName, MinimumSqft
  * MAA Worthington camelCase:   floorplanName, apartmentName, minimumRent

Every consumer downstream of extraction has to read the same logical field
out of any of these shapes. This module is the single place that knows the
alias tables — every other module reads via these accessors.

**Case-insensitive lookup.** All alias matching is case-insensitive
(``BedRooms``, ``bedRooms``, ``BEDROOMS`` all match the alias ``bedrooms``).
This mirrors the per-adapter ``_normalise_item`` pattern (e.g. rentcafe.py
line 56) so canonical is robust to producer-side casing variability without
forcing each adapter to pre-lowercase. Underscores ARE significant —
``floor_plan_name`` and ``floorplanname`` are distinct aliases because
producers genuinely use both forms.

Public surface:
    * Alias tuples — the exhaustive list per logical field
    * ``get_raw`` / ``get_numeric`` / ``get_str`` — value extractors
    * ``is_present`` — presence predicate
    * ``ABSENT`` — the -1 sentinel for "we looked and the source said nothing"

This module is **pure**: deterministic, no side effects, never raises on
unexpected input. All coercion failures are caught at the access boundary
and surfaced as ``None``.

See docs/2026_05_11_regressions_fix_design.md for the design.
"""

from __future__ import annotations

import math
from typing import Any, Final

from ma_poc.models.source import FieldValue

# ── Sentinel ──────────────────────────────────────────────────────────────────

#: "We tried and the source had nothing." Distinct from ``None``
#: (= "not provided"). Preserved through the V2 schema so analytics can
#: distinguish "missing because we didn't look" from "missing because we
#: looked and the source didn't have it."
ABSENT: Final[int] = -1


# ── Field-name alias tables ───────────────────────────────────────────────────
#
# Entries are listed in their natural form. Lookup is case-insensitive, so
# each entry covers every case variation of its string (``bedrooms`` covers
# ``Bedrooms``, ``BEDROOMS``, ``bedRooms``, ``BedRooms``). Underscores and
# hyphens ARE significant — different word-boundary conventions need
# separate entries.
#
# Order within each tuple matters: ``get_raw`` / ``get_numeric`` / ``get_str``
# return the FIRST present alias. Convention:
#   [0] V2 canonical name (what _format_v2_unit_record writes)
#   [1-N] make_unit_dict shape → producer-specific aliases → merger transit
#
# Adding a new alias is a deliberate append here. Never inline the lookup
# in a call site.

BEDS_KEYS: Final[tuple[str, ...]] = (
    "beds",                          # V2 canonical
    "bedrooms",                      # also covers BedRooms / bedRooms (case-insens)
    "_bedrooms",                     # merger transit
    "bedroom_count", "bedroomcount",
    "numbedrooms", "num_bedrooms",
    "no_of_bedroom",
    "bed",
    # Schema.org Apartment/FloorPlan canonical key (after lowercase normalisation).
    # PID 61950 250high.com 2026-05-15 — Squarespace /pricing JSON-LD emitted
    # `numberOfBedrooms` and validity dropped 6 units because the lookup tuple
    # didn't include this Schema.org name.
    "numberofbedrooms",
)

BATHS_KEYS: Final[tuple[str, ...]] = (
    "baths",                         # V2 canonical
    "bathrooms",                     # also covers BathRooms / bathRooms
    "_bathrooms",                    # merger transit
    "bathroom_count", "bathroomcount",
    "numbathrooms", "num_bathrooms",
    "no_of_bathroom",
    # Schema.org Apartment/FloorPlan canonical key (after lowercase normalisation).
    "numberofbathroomstotal",
    "numberofbathrooms",
)

SQFT_KEYS: Final[tuple[str, ...]] = (
    "area",                          # V2 canonical
    "sqft",                          # MAA Worthington raw uses plain "sqft"
    "_sqft",                         # merger transit
    "squarefeet", "square_feet",
    "minimumsquarefeet", "minimum_square_feet",
    "minimumsqft", "minimum_sqft",   # Windsor PascalCase MinimumSqft → minimumsqft
    "size",
    "square_footage", "squarefootage",
    "sq_ft",
    # Schema.org Apartment.floorSize (after lowercase normalisation).
    "floorsize",
)
# ``area`` is admitted as a sqft alias ONLY because ``get_numeric`` rejects
# non-numeric values. Some neighborhoods / locator APIs (e.g. nestiolistings
# on the 2026-05-11 Skyline at Kessler regression) emit ``area`` as the
# string "Hudson County" — that fails coercion in ``get_numeric`` and is
# never surfaced as a sqft.

FP_NAME_KEYS: Final[tuple[str, ...]] = (
    "floor_plan_name",               # V2 canonical
    "_floor_plan",                   # merger transit
    "floorplan_name",                # adapter variant (one underscore)
    "floorplanname",                 # MAA floorplanName / Windsor FloorplanName / post-lower
    "floorplan-name",                # CMS hyphenated variant (observed in _merge_fns)
    "planname", "plan_name",
    "unittype", "unit_type",
)
# ``name`` is intentionally NOT here. Generic key used by many API shapes for
# property/community/neighborhood name. Letting it float through as
# floor_plan_name was the root cause of the Skyline at Kessler 258-row
# regression (``{"name": "Hoboken"}``).

UID_KEYS: Final[tuple[str, ...]] = (
    "unit_id",                       # V2 canonical
    "unit_number",
    "_unit_number",                  # merger transit
    "unitnumber",                    # camelCase unitNumber post-lower
    "unitid",                        # camelCase unitId post-lower
    "apartmentname", "apartment_name",  # MAA Worthington apartmentName
    "label",
    "display_unit_number", "displayunitnumber",
)
# ``id`` is intentionally excluded — too generic. Many bodies emit ``id`` for
# property-level or floorplan-level identifiers. Producers wanting to map a
# unit-level identifier (e.g. MAA Worthington's ULID per-unit ``id``) should
# do so explicitly inside their adapter.

RENT_LO_KEYS: Final[tuple[str, ...]] = (
    "rent_low",                      # V2 canonical
    "market_rent_low",
    "asking_rent",
    "minrent", "min_rent",           # MinRent / minRent / min_rent
    "minimumrent", "minimum_rent",   # Windsor MinimumRent / camelCase
    "minimummarketrent", "minimum_market_rent",
    "min_price", "minprice",
    "baserent", "base_rent",
    "monthlyrent", "monthly_rent",
    "display_price", "displayprice",
    "startingfrom", "starting_from",
    "startingprice", "starting_price",
)

RENT_HI_KEYS: Final[tuple[str, ...]] = (
    "rent_high",                     # V2 canonical
    "market_rent_high",
    "maxrent", "max_rent",
    "maximumrent", "maximum_rent",
    "maximummarketrent", "maximum_market_rent",
    "max_price", "maxprice",
)

AVAIL_DATE_KEYS: Final[tuple[str, ...]] = (
    "available_date",                # V2 canonical
    "availability_date",
    "availabledate",                 # MAA availableDate / availableDate post-lower
    "movein_date", "moveindate",
    "moveinready", "movein_ready",
    "available_on", "availableon",
    "readydate", "ready_date",
)

#: Availability-status field aliases — vendor variants of the per-row
#: status string ("AVAILABLE" / "UNAVAILABLE" / "WAITLIST" / etc.). Used by
#: :func:`classify._is_available_with_rent` (2026-05-17) to promote rent-
#: bearing AVAILABLE rows to unit-level even when they lack an
#: ``available_date`` move-in date. Centralised here so future producers'
#: spellings land in one place.
AVAIL_STATUS_KEYS: Final[tuple[str, ...]] = (
    "availability_status",           # V2 canonical
    "availabilitystatus",            # camelCase availabilityStatus post-lower
    "availability",
    "available",
    "status",
    "is_available", "isavailable",
    "availability_state", "availabilitystate",
    "availability_code", "availabilitycode",
)

RENT_RANGE_KEYS: Final[tuple[str, ...]] = (
    "rent_range",
    "rentrange",                     # post-lower of rentRange
)

#: Building name / identifier — used by the cross-page dedup pass to keep
#: townhome complexes with re-used apartment numbers across buildings
#: ("Bldg A #1" vs "Bldg B #1") distinct in the dedup bucket key.
BUILDING_KEYS: Final[tuple[str, ...]] = (
    "building",                      # V2 canonical
    "building_name", "buildingname",
    "bldg", "bldg_name", "bldgname",
)

#: Floor-plan identifier / code — the per-property unique key that
#: distinguishes one plan from another. Used by the inverse-B4 guard to
#: detect unit rows whose ``unit_number`` is actually a plan code that
#: leaked through. Distinct from ``FP_NAME_KEYS`` (display name).
FP_ID_KEYS: Final[tuple[str, ...]] = (
    "floor_plan_id",                 # V2 canonical
    "floorplan_id", "floorplanid",
    "floor_plan_code", "floorplancode",
    "plan_code", "plancode",
    "plan_id", "planid",
)


# ── Internal helpers ──────────────────────────────────────────────────────────


#: Sub-keys tried in order when ``get_numeric`` encounters a nested-dict
#: value. Mirrors the precedent in ``_parsing.get_field`` so APIs that wrap
#: rent / sqft as ``{min: 1500, max: 2000}`` (ResMan, Yardi nested) are
#: still readable. ``min`` / ``low`` first → the typical "from" / "starting
#: at" semantic. ``max`` / ``high`` last → fallback when only the upper
#: bound is set.
_NESTED_SUB_KEYS: Final[tuple[str, ...]] = (
    "min", "low", "amount", "value", "effectiveRent", "max", "high",
)


def _unwrap_field_value(v: Any) -> Any:
    """If *v* is a ``FieldValue`` (provenance wrapper from the merger), return
    its ``.value`` attribute; otherwise return *v* unchanged.

    Uses ``isinstance`` (not duck typing) so unrelated objects that happen to
    expose ``.value`` and ``.source`` attributes don't collide.
    """
    if isinstance(v, FieldValue):
        return v.value
    return v


def _build_lower_view(unit: dict[str, Any]) -> dict[str, Any]:
    """Build a ``{lowercased_key: value}`` view of *unit*.

    Case-insensitive matching is critical for vendor-variability: PascalCase
    Windsor payloads (``BedRooms``, ``FloorplanName``), camelCase MAA payloads
    (``floorplanName``, ``minimumRent``), and post-normalisation lowercased
    forms all collapse to the same lowercased key, matched against the
    lowercased alias.

    Non-string keys are silently dropped — dict-of-int or dict-of-tuple keys
    don't exist in our unit shape and shouldn't make this raise.

    On case-conflict (e.g. a dict containing both ``beds`` and ``BEDS``),
    last-write wins per Python's dict iteration order. That's a malformed
    input; we don't try to merge.
    """
    return {k.lower(): v for k, v in unit.items() if isinstance(k, str)}


def _coerce_scalar_to_float(v: Any) -> float | None:
    """Coerce a scalar (int / float / string) to ``float``, or ``None``.

    Centralises the coercion rules so ``get_numeric``'s direct-value branch
    and its nested-dict branch share one implementation:

      * ``None``                  → None
      * ``bool``                  → None (subclass of int but never real data)
      * ``int`` / ``float``       → ``float()``; NaN / inf → None
      * ``str``                   → strip whitespace + leading ``$`` +
                                     commas, then ``float()``; non-numeric → None
      * anything else (list, set, etc.)  → None (caught by the str() / float())
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    try:
        if isinstance(v, (int, float)):
            f = float(v)
        else:
            s = str(v).strip().replace("$", "").replace(",", "")
            if not s:
                return None
            f = float(s)
    except (ValueError, TypeError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


# ── Public presence predicate ─────────────────────────────────────────────────


def is_present(value: Any) -> bool:
    """A field carries a real (non-empty, non-sentinel) value.

    Returns ``False`` for:
      * ``None``
      * the ``ABSENT`` sentinel (-1) and its string form "-1"
      * empty / whitespace-only strings
      * the string tokens "null", "none", "n/a" (case-insensitive)
      * empty collections (``{}``, ``[]``, ``()``) — caller would have
        nothing to extract from them
      * NaN floats

    Returns ``True`` for:
      * 0 / 0.0 / "0" — zero is a real measurement (studio = 0 beds)
      * any non-empty string
      * any non-empty collection
      * any finite or infinite number except ``ABSENT``

    This is the absence-detection used by ``is_valid_unit`` and every gate
    that needs to ask "did the source say something here?"
    """
    if value is None:
        return False
    if value == ABSENT or value == str(ABSENT):
        return False
    # Empty collections are absent — without this guard ``get_str`` would
    # stringify ``{}`` as ``"{}"`` and surface that as a "real" field value.
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        return len(value) > 0
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return False
        if s.lower() in ("null", "none", "n/a"):
            return False
        return True
    if isinstance(value, float) and math.isnan(value):
        return False
    return True


# ── Public value extractors ───────────────────────────────────────────────────


def get_raw(unit: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """First present alias value across the key tuple, preserved as-is.

    Returns the raw unwrapped value (``FieldValue.value`` or scalar). Returns
    ``None`` if no alias is present. Unlike ``get_str`` / ``get_numeric``,
    this does not coerce the type.

    Lookup is case-insensitive: each alias in *keys* is matched against the
    lowercased key set of *unit*.
    """
    unit_lc = _build_lower_view(unit)
    for k in keys:
        v = _unwrap_field_value(unit_lc.get(k.lower()))
        if is_present(v):
            return v
    return None


def get_numeric(unit: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """First numeric-coercible value across alias keys, returned as ``float``.

    Coercion rules (per ``_coerce_scalar_to_float``):
      * ``int`` / ``float``       → cast to ``float`` (NaN / inf → ``None``)
      * ``str``                   → strip whitespace + leading ``$`` + commas,
                                     then ``float()``; non-numeric strings →
                                     skip and try next alias
      * ``bool``                  → never numeric (bool is an int subclass but
                                     ``True``-as-bedrooms is always a coercion
                                     bug, not real data)
      * ``ABSENT`` sentinel       → skip (treated as not-present)

    Nested-dict unwrap: some APIs wrap rent/sqft as ``{min: 1500, max: 2000}``
    (ResMan, Yardi nested). When the alias value is itself a dict,
    ``get_numeric`` tries the sub-keys in ``_NESTED_SUB_KEYS`` order and
    returns the first numerically-coercible one. Mirrors the long-standing
    contract of ``_parsing.get_field``.

    List values are explicitly skipped: ``[1500, 2000]`` is ambiguous for a
    scalar field and the caller should pre-flatten if it has list-shaped rent.

    Returns ``None`` when no alias is present OR no present alias coerces to a
    finite number.

    Geographic strings under SQFT_KEYS (the 2026-05-11 nestiolistings
    ``area="Hudson County"`` regression) are dropped here.
    """
    unit_lc = _build_lower_view(unit)
    for k in keys:
        v = _unwrap_field_value(unit_lc.get(k.lower()))
        if not is_present(v):
            continue
        # Nested-dict unwrap: try a known set of inner sub-keys.
        if isinstance(v, dict):
            for sub_k in _NESTED_SUB_KEYS:
                f = _coerce_scalar_to_float(v.get(sub_k))
                if f is not None:
                    return f
            continue  # no sub-key produced a number — try next alias
        # Lists are ambiguous for a scalar field — skip them entirely.
        if isinstance(v, list):
            continue
        f = _coerce_scalar_to_float(v)
        if f is not None:
            return f
    return None


def get_str(unit: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """First non-empty string value across alias keys.

    Returns the value as ``str`` (calling ``str(v)`` on non-string inputs).
    Returns ``None`` when no alias is present OR all present aliases
    stringify to empty.
    """
    unit_lc = _build_lower_view(unit)
    for k in keys:
        v = _unwrap_field_value(unit_lc.get(k.lower()))
        if not is_present(v):
            continue
        s = str(v).strip()
        if s:
            return s
    return None
