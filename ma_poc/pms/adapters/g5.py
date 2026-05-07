"""G5 Marketing Cloud GraphQL adapter.

Patch #11 (2026-05-06 rent-recovery audit). Phase A measurement showed
`inventory.g5marketingcloud.com/graphql` is captured on 166 properties in
the production run; 72 of those have zero rent extracted today (the largest
single unaddressed host gap). G5 is a major multifamily marketing platform
whose embedded inventory widget on tenant property sites issues GraphQL
queries to the inventory endpoint to populate the floor-plan grid.

Schema validation
-----------------
Schema introspected live on 2026-05-06 against the production endpoint.
The two response types this adapter targets:

* ``Floorplan`` (per-floor-plan record)
  Fields used: ``name``, ``beds`` (Int), ``baths`` (String),
  ``sqft`` (numeric), ``sqftDisplay`` (String), ``startingRate`` (Int),
  ``endingRate`` (Int), ``rateDisplay`` (String),
  ``totalRentStarting`` / ``totalRentEnding`` (Int),
  ``totalAvailableUnits`` (Int).

* ``Apartment`` (per-unit record — used by some queries instead of Floorplan)
  Fields used: ``name``, ``displayName``, ``building``, ``availabilityDate``,
  ``sqftDisplay``, ``prices``, ``floorplan``.

The adapter accepts response envelopes of the form
``{"data": {"<root>": [...]}}`` where ``<root>`` is one of
``floorplans``, ``apartmentComplex.floorplans``, ``apartments``, etc.
GraphQL responses without a ``data`` key (errors-only) are silently
ignored, never raised.

Tier label: ``TIER_1_API_G5_GRAPHQL``.
"""

from __future__ import annotations

from typing import Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)


__all__ = [
    "is_g5_graphql_url",
    "is_g5_graphql_body",
    "extract_floorplan_lists_from_g5_body",
    "parse_g5_floorplan",
    "parse_g5_apartment",
    "parse_g5_response",
]


def is_g5_graphql_url(url: str) -> bool:
    """Return True iff the URL is the G5 Marketing Cloud GraphQL endpoint."""
    if not url:
        return False
    u = url.lower()
    return "inventory.g5marketingcloud.com" in u and "/graphql" in u


def is_g5_graphql_body(body: Any) -> bool:
    """Return True iff the body looks like a G5 GraphQL response carrying
    a list of floorplans or apartments. Pure / never raises.
    """
    if not isinstance(body, dict):
        return False
    data = body.get("data")
    if not isinstance(data, dict):
        return False
    return bool(_walk_for_floorplan_list(data) or _walk_for_apartment_list(data))


_FLOORPLAN_LIST_KEYS = (
    "floorplans",
    "floorplanList",
    "apartmentComplexFloorplans",
)
_APARTMENT_LIST_KEYS = (
    "apartments",
    "apartmentList",
    "units",
)
# Floor-plan-shaped: at least 2 of these keys on the first item.
_FP_SIGNAL_KEYS = {
    "name",
    "beds",
    "baths",
    "sqft",
    "sqftDisplay",
    "startingRate",
    "endingRate",
    "rateDisplay",
    "totalAvailableUnits",
    "totalRentStarting",
}
_APT_SIGNAL_KEYS = {
    "name",
    "displayName",
    "building",
    "availabilityDate",
    "sqftDisplay",
    "prices",
    "floorplan",
}


def _looks_like_floorplan_list(items: list[Any]) -> bool:
    if not items or not isinstance(items[0], dict):
        return False
    return len(_FP_SIGNAL_KEYS & set(items[0].keys())) >= 2


def _looks_like_apartment_list(items: list[Any]) -> bool:
    if not items or not isinstance(items[0], dict):
        return False
    keys = set(items[0].keys())
    # Apartment must have at least 2 signal keys overall AND at least 1 key
    # that is NOT shared with the Floorplan type, otherwise a floorplan list
    # would match as both (name+sqftDisplay are in both).
    apt_only = {"displayName", "building", "availabilityDate", "prices", "floorplan"}
    return (
        len(_APT_SIGNAL_KEYS & keys) >= 2
        and bool(apt_only & keys)
    )


def _walk_for_floorplan_list(node: Any, depth: int = 0) -> list[dict] | None:
    """Recursively scan a GraphQL `data` block for the first list whose
    items look like Floorplan records. Bounded depth.
    """
    if depth > 6:
        return None
    if isinstance(node, list):
        if _looks_like_floorplan_list(node):
            return list(node)
        return None
    if isinstance(node, dict):
        # Prefer named floorplan keys first.
        for key in _FLOORPLAN_LIST_KEYS:
            v = node.get(key)
            if isinstance(v, list) and _looks_like_floorplan_list(v):
                return list(v)
        # Then any list child whose items look like floorplans.
        for v in node.values():
            res = _walk_for_floorplan_list(v, depth + 1)
            if res:
                return res
    return None


def _walk_for_apartment_list(node: Any, depth: int = 0) -> list[dict] | None:
    if depth > 6:
        return None
    if isinstance(node, list):
        if _looks_like_apartment_list(node):
            return list(node)
        return None
    if isinstance(node, dict):
        for key in _APARTMENT_LIST_KEYS:
            v = node.get(key)
            if isinstance(v, list) and _looks_like_apartment_list(v):
                return list(v)
        for v in node.values():
            res = _walk_for_apartment_list(v, depth + 1)
            if res:
                return res
    return None


def extract_floorplan_lists_from_g5_body(body: Any) -> tuple[list[dict], list[dict]]:
    """Return (floorplans, apartments) lists extracted from a G5 body.
    Either may be empty. Pure / never raises.
    """
    if not isinstance(body, dict):
        return [], []
    data = body.get("data")
    if not isinstance(data, dict):
        return [], []
    fps = _walk_for_floorplan_list(data) or []
    apts = _walk_for_apartment_list(data) or []
    return fps, apts


def _parse_baths(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def parse_g5_floorplan(item: dict, url: str) -> dict[str, str] | None:
    """Convert a G5 Floorplan record into a unit dict. Returns None if the
    record carries no usable signal.
    """
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    beds = item.get("beds")
    baths = _parse_baths(item.get("baths"))
    sqft_n = item.get("sqft")
    sqft_disp = str(item.get("sqftDisplay") or "").strip()
    if isinstance(sqft_n, (int, float)) and sqft_n > 0:
        sqft = str(int(sqft_n))
    else:
        sqft = sqft_disp

    # Rent: prefer startingRate/endingRate (per-unit floor) over totalRent*.
    # G5 returns these as Int — money_to_int expects a string, so coerce.
    rent_lo_n = item.get("startingRate") or item.get("totalRentStarting")
    rent_hi_n = item.get("endingRate") or item.get("totalRentEnding")
    rent_lo = money_to_int(str(rent_lo_n)) if rent_lo_n else None
    rent_hi = money_to_int(str(rent_hi_n)) if rent_hi_n else None

    # Skip records with no usable signal at all
    if not (name or beds is not None or sqft or rent_lo):
        return None

    avail_n = item.get("totalAvailableUnits")
    avail = (
        str(avail_n)
        if isinstance(avail_n, (int, float)) and avail_n > 0
        else ""
    )

    beds_str = str(int(beds)) if isinstance(beds, (int, float)) else ""
    baths_str = str(baths) if baths is not None else ""

    return make_unit_dict(
        floor_plan_name=name,
        bed_label=bed_label_from(beds, name) if isinstance(beds, (int, float)) else "",
        bedrooms=beds_str,
        bathrooms=baths_str,
        sqft=sqft,
        unit_number="",  # Floorplan record — no unit number
        rent_range=format_rent_range(rent_lo, rent_hi),
        availability_status="AVAILABLE" if rent_lo or avail else "",
        available_units=avail,
        availability_date="",
        source_api_url=url,
        extraction_tier="TIER_1_API_G5_GRAPHQL",
    )


def parse_g5_apartment(item: dict, url: str) -> dict[str, str] | None:
    """Convert a G5 Apartment record into a unit dict. Returns None if no
    usable signal.
    """
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or item.get("displayName") or "").strip()
    building = str(item.get("building") or "").strip()
    avail_dt = str(item.get("availabilityDate") or "").strip()
    sqft = str(item.get("sqftDisplay") or "").strip()

    # `prices` may be a list of {min, max, leaseTermMonths} objects.
    prices = item.get("prices")
    rent_lo: int | None = None
    rent_hi: int | None = None
    if isinstance(prices, list):
        candidates: list[int] = []
        for p in prices:
            if not isinstance(p, dict):
                continue
            for k in ("min", "minRent", "starting", "amount", "value"):
                v = p.get(k)
                vi = money_to_int(str(v)) if v is not None else None
                if vi:
                    candidates.append(vi)
                    break
        if candidates:
            rent_lo = min(candidates)
            rent_hi = max(candidates)

    # Floorplan link
    fp = item.get("floorplan")
    fp_name = ""
    beds = None
    baths = None
    if isinstance(fp, dict):
        fp_name = str(fp.get("name") or "").strip()
        beds = fp.get("beds")
        baths = _parse_baths(fp.get("baths"))

    if not (name or fp_name or rent_lo or sqft):
        return None

    return make_unit_dict(
        floor_plan_name=fp_name or name,
        bed_label=bed_label_from(beds, fp_name or name) if isinstance(beds, (int, float)) else "",
        bedrooms=str(int(beds)) if isinstance(beds, (int, float)) else "",
        bathrooms=str(baths) if baths is not None else "",
        sqft=sqft,
        unit_number=name or "",
        building=building,
        rent_range=format_rent_range(rent_lo, rent_hi),
        availability_status="AVAILABLE" if rent_lo or avail_dt else "",
        available_units="1" if rent_lo else "",
        availability_date=avail_dt,
        source_api_url=url,
        extraction_tier="TIER_1_API_G5_GRAPHQL",
    )


def parse_g5_response(body: Any, url: str) -> list[dict[str, str]]:
    """Top-level parser. Extract Floorplan AND/OR Apartment records from a
    G5 GraphQL response and return a unified unit dict list.

    Strategy: prefer Apartment-level records when present (richer per-unit
    data); fall back to Floorplan-level records (one per plan).
    """
    if not isinstance(body, dict):
        return []
    fps, apts = extract_floorplan_lists_from_g5_body(body)
    out: list[dict[str, str]] = []
    if apts:
        for it in apts:
            u = parse_g5_apartment(it, url)
            if u:
                out.append(u)
    if not out and fps:
        for it in fps:
            u = parse_g5_floorplan(it, url)
            if u:
                out.append(u)
    return out
