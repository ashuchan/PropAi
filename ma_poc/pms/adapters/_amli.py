"""AMLI Residential adapter — tRPC-nested __NEXT_DATA__ extractor.

AMLI runs a Next.js + tRPC frontend over a RealPage/Entrata backend.
Every AMLI property page embeds the full unit list in
``<script id="__NEXT_DATA__" type="application/json">``, but the data
sits behind a deeply-nested tRPC envelope:

    props.pageProps.trpcState.json.queries[N].state.data[M]
      └── floorplanName, bedroomMax, bathroomMax, sqftMin, sqftMax,
          priceMin, priceMinWithFees, units: [
            { unitNumber, floor, buildingNumber, rent, baseRent,
              totalRent, squareFeet, rpAvailableDate,
              realPageAvailabilityDate, unitTypeCode, ... }
          ]

The generic ``parse_api_responses`` walks ``floorPlans`` / ``units``
keys at top level only, so it sees a handful of units per page (the
ones whose path happens to surface) and drops the rest plus most of
their metadata. This adapter walks the tRPC nesting directly and
emits one record per unit with full floor-plan context inherited.

Confirmed against the AMLI South Shore HAR (Austin, TX) — 36 units
across 25 floor plans in a single 527 KB HTML page.

Phase 6.x positioning: feeds the generic embedded-JSON tier — the
existing extractor already finds the ``__NEXT_DATA__`` blob; this
module parses its tRPC-specific shape.
"""

from __future__ import annotations

from typing import Any

# ── Detector ────────────────────────────────────────────────────────────────


def detect_amli_trpc_blob(blob_body: Any) -> bool:
    """True if a parsed embedded-JSON blob carries AMLI's tRPC envelope.

    Cheap walk on already-parsed JSON. Two anchor signals:
      • ``buildId`` at top level (Next.js convention) AND
      • ``props.pageProps.trpcState.json.queries`` as a list AND
      • at least one query has ``state.data`` as a list of dicts with
        a ``units`` array (the unit-list shape).

    Returning False on any envelope mismatch keeps non-AMLI Next.js
    blobs from being mis-routed to this parser.
    """
    if not isinstance(blob_body, dict):
        return False
    if "buildId" not in blob_body:
        return False
    props = blob_body.get("props")
    if not isinstance(props, dict):
        return False
    page_props = props.get("pageProps")
    if not isinstance(page_props, dict):
        return False
    trpc = page_props.get("trpcState")
    if not isinstance(trpc, dict):
        return False
    json_state = trpc.get("json")
    if not isinstance(json_state, dict):
        return False
    queries = json_state.get("queries")
    if not isinstance(queries, list):
        return False
    # Require at least one query whose state.data has units
    for q in queries:
        if not isinstance(q, dict):
            continue
        state = q.get("state")
        if not isinstance(state, dict):
            continue
        data = state.get("data")
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("units"), list):
                    return True
    return False


# ── Parser ──────────────────────────────────────────────────────────────────


def _money_to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        try:
            return int(str(v).replace(",", "").replace("$", ""))
        except (TypeError, ValueError):
            return None


def _str_or_empty(v: Any) -> str:
    if v is None:
        return ""
    return str(v)


def parse_amli_trpc_blob(
    blob_body: Any, source_url: str = ""
) -> list[dict[str, Any]]:
    """Walk an AMLI tRPC __NEXT_DATA__ blob and return unit records.

    Returns empty list if ``blob_body`` doesn't pass ``detect_amli_trpc_blob``
    — callers should detect first or simply chain (the detector is cheap).

    Per-floor-plan metadata inherited by every unit it contains:
      • ``floor_plan_name``    ← ``floorplanName`` (e.g. "E2")
      • ``bedrooms``           ← ``bedroomMax`` (str)
      • ``bathrooms``          ← ``bathroomMax`` (str)
      • ``sqft``               ← per-unit ``squareFeet`` if present,
                                  else falls back to ``sqftMin``

    Per-unit fields read from the units[] array:
      • ``unit_number``        ← ``unitNumber``
      • ``floor``              ← ``floor``
      • ``building``           ← ``buildingNumber``
      • ``sqft``               ← ``squareFeet`` (overrides floor-plan)
      • ``market_rent_low/high`` + ``rent_range`` ← ``rent``/``totalRent``
      • ``availability_date``  ← ``rpAvailableDate``
      • ``availability_status``← derived ("AVAILABLE" if any rent set)
      • ``property_unit_id``   ← ``unitId`` (str)
      • ``realpage_unit_id``   ← ``realPageUnitId``
      • ``entrata_unit_id``    ← ``entrataUnitId``
    """
    if not detect_amli_trpc_blob(blob_body):
        return []

    queries = blob_body["props"]["pageProps"]["trpcState"]["json"]["queries"]
    out: list[dict[str, Any]] = []
    seen_unit_ids: set[str] = set()

    for q in queries:
        if not isinstance(q, dict):
            continue
        data = (q.get("state") or {}).get("data")
        if not isinstance(data, list):
            continue
        for fp in data:
            if not isinstance(fp, dict):
                continue
            units = fp.get("units")
            if not isinstance(units, list):
                continue
            # Floor-plan-level metadata to inherit
            fp_name = _str_or_empty(fp.get("floorplanName"))
            bedrooms = _str_or_empty(fp.get("bedroomMax"))
            bathrooms = _str_or_empty(fp.get("bathroomMax"))
            fp_sqft_min = fp.get("sqftMin")
            fp_id = _str_or_empty(fp.get("id"))
            for u in units:
                if not isinstance(u, dict):
                    continue
                # Dedup by unitId across queries — the same unit can
                # appear in multiple tRPC queries (the homepage + the
                # floorplans query both materialize it).
                uid = u.get("unitId")
                uid_str = _str_or_empty(uid) if uid is not None else ""
                if uid_str and uid_str in seen_unit_ids:
                    continue
                if uid_str:
                    seen_unit_ids.add(uid_str)

                rent_low = _money_to_int(u.get("rent") or u.get("baseRent"))
                rent_high = _money_to_int(u.get("totalRent")) or rent_low
                if rent_low is None and rent_high is None:
                    rent_range = ""
                elif rent_low is not None and rent_high is not None and rent_low != rent_high:
                    rent_range = f"${rent_low:,} - ${rent_high:,}"
                elif rent_low is not None:
                    rent_range = f"${rent_low:,}"
                else:
                    rent_range = f"${rent_high:,}"

                unit_sqft = u.get("squareFeet")
                if unit_sqft in (None, 0, ""):
                    unit_sqft = fp_sqft_min
                sqft_str = _str_or_empty(unit_sqft) if unit_sqft not in (None, 0, "") else ""

                # Availability — AMLI ships ISO date; if present, the unit
                # is implicitly available. No date + zero rent ⇒ leave
                # status empty (the downstream merger handles unknowns).
                avail_date = _str_or_empty(u.get("rpAvailableDate"))
                if not avail_date:
                    avail_date = _str_or_empty(u.get("realPageAvailabilityDate"))
                if "T" in avail_date:
                    avail_date = avail_date.split("T", 1)[0]
                avail_status = "AVAILABLE" if (avail_date or rent_low) else ""

                out.append({
                    "floor_plan_name": fp_name or _str_or_empty(u.get("unitTypeCode")),
                    "bed_label": "",
                    "bedrooms": bedrooms,
                    "bathrooms": bathrooms,
                    "sqft": sqft_str,
                    "unit_number": _str_or_empty(u.get("unitNumber")),
                    "floor": _str_or_empty(u.get("floor")),
                    "building": _str_or_empty(u.get("buildingNumber")),
                    "rent_range": rent_range,
                    "market_rent_low": rent_low,
                    "market_rent_high": rent_high,
                    "deposit": "",
                    "concession": "",
                    "availability_status": avail_status,
                    "available_units": "",
                    "availability_date": avail_date,
                    "lease_term": "",
                    "move_in_date": "",
                    "source_api_url": source_url,
                    "source": "amli_trpc",
                    "property_unit_id": uid_str,
                    "propertyfloorplanid": fp_id,
                    "realpage_unit_id": _str_or_empty(u.get("realPageUnitId")),
                    "entrata_unit_id": _str_or_empty(u.get("entrataUnitId")),
                })
    return out


__all__ = [
    "detect_amli_trpc_blob",
    "parse_amli_trpc_blob",
]
