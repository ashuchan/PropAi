"""Unit-level signal filters for the extraction pipeline.

Lives in signal_engine so every extraction tier (api_broad, api_narrow,
dom_scan, LLM adapters) can share the same predicate without duplicating
logic or importing from adapter-layer modules.

Design principles:
- Each predicate is a pure function (dict -> bool).
- `filter_by_*` helpers return a NEW list — never mutate the input.
- Adding a new filter dimension means adding a predicate + a new filter
  function; callers choose which filters to apply and in what order.
"""

from __future__ import annotations

from typing import Any


# Keys that carry a physical/structural dimension for an apartment unit.
# At least one must be present and numeric for a row to be considered a
# real unit record (vs. a CMS config object, neighbourhood record, or
# API noise row with only a name/description).
_NUMERIC_DIMENSION_KEYS: frozenset[str] = frozenset({
    "beds",
    "bedrooms",
    "numberOfBeds",
    "number_of_beds",
    "bed_count",
    "baths",
    "bathrooms",
    "numberOfBaths",
    "number_of_baths",
    "bath_count",
    "sqft",
    "squareFeet",
    "square_feet",
    "sq_ft",
    "area",
    "floorArea",
    "floor_area",
    "living_area",
})

# Keys that carry a rent price for an apartment unit.
_RENT_KEYS: frozenset[str] = frozenset({
    "rent",
    "asking_rent",
    "market_rent",
    "marketRent",
    "minRent",
    "maxRent",
    "min_rent",
    "max_rent",
    "rent_low",
    "rent_high",
    "market_rent_low",
    "market_rent_high",
    "price",
    "amount",
    "effectiveRent",
    "effective_rent",
})


def has_physical_dimension(unit: dict[str, Any]) -> bool:
    """Return True when the unit dict has at least one numeric dimension.

    A numeric dimension is a beds, baths, or sqft-family key whose value
    is a real number (int or float, not a string like "N/A" or None).
    CMS config objects, neighbourhood records, and API noise rows typically
    contain only string fields (name, description, url) — this predicate
    rejects them so the planner sees ``units_found=0`` accurately instead
    of escalating to expensive hop chains based on a false positive count.
    """
    for key in _NUMERIC_DIMENSION_KEYS:
        val = unit.get(key)
        if val is None:
            continue
        try:
            if float(val) >= 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def has_rent_signal(unit: dict[str, Any]) -> bool:
    """Return True when the unit dict carries any rent-like value."""
    for key in _RENT_KEYS:
        val = unit.get(key)
        if val is None:
            continue
        try:
            if float(val) > 0:
                return True
        except (TypeError, ValueError):
            if isinstance(val, dict):
                for sub in val.values():
                    try:
                        if float(sub) > 0:  # type: ignore[arg-type]
                            return True
                    except (TypeError, ValueError):
                        pass
    return False


def filter_by_physical_dimension(
    units: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only units that have at least one numeric dimension.

    Callers (generic:api_broad, etc.) should apply this BEFORE passing the
    unit list to the planner so the planner's ``pct`` calculation sees
    accurate data instead of counting dimensionless config rows as units.
    """
    return [u for u in units if has_physical_dimension(u)]


def filter_by_rent_signal(
    units: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only units that carry at least one rent-like value."""
    return [u for u in units if has_rent_signal(u)]


def filter_dimensionless_and_rentless(
    units: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return units that have either a physical dimension OR a rent signal."""
    return [u for u in units if has_physical_dimension(u) or has_rent_signal(u)]
