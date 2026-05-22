"""Brookfield Properties REIT — WP-middleware floorplans API parser.

The Brookfield REIT serves all its properties' inventory from a single
shared WordPress middleware at::

    https://rent.brookfieldproperties.com/wp-json/middleware/v1/getFloorplans/
        ?propertyId[]=<n>&has_availability=true&order=DESC&orderby=hasSpecials

Every Brookfield property (Turtle Cove, The Eugene, Atelier, Dawson,
Briggs & Union, Miramar Lakes, …) loads its marketing site on a vanity
domain (e.g. ``turtlecoveapartments.com``, ``thedawsontampa.com``,
``atelierdtla.com``) and cross-origin-XHRs into this middleware. The
middleware host is therefore the only stable detection signal.

Response shape (live-verified 2026-05-22 against propertyId=1807803):
top-level JSON array of floor-plan dicts, each with the following keys
that the parser reads:

    floorplanName            human-readable plan name ("1A Renovation 3")
    beds / baths             bed/bath counts (strings like "1", "2")
    minimumSQFT / maximumSQFT sqft bounds (strings)
    minimumRent / maximumRent currently advertised rent range (strings)
    originalMinRent          baseline ('non-special') low — ignored, see note
    availableUnitsCount      live availability count (string)
    availableDate            earliest availability date ("YYYY-MM-DD")
    hasSpecials              "1" if a promo is active on this plan, else "0"
    propertyName             marketing name of the parent property

Why we explicitly do NOT fold ``originalMinRent`` into ``concession``:
inspection of the live response shows ``originalMinRent`` < ``minimumRent``
on plans with ``hasSpecials="1"``, which is the OPPOSITE of a discount
(a real concession would make the asking rent lower than the baseline,
not higher). The semantic of this field is undocumented and a previous
LLM-learned mapping (cached in profile_replay) wrongly emitted it as
``concession_value`` for 8 properties on 2026-05-22. We surface
``hasSpecials="1"`` as a free-text concession marker only — no fabricated
dollar amount.
"""

from __future__ import annotations

from typing import Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)

TIER = "TIER_1_API_BROOKFIELD"

# Cross-origin XHR endpoint that every Brookfield property's marketing site
# hits during page load. Match on this substring; the middleware lives on
# rent.brookfieldproperties.com regardless of which property is being viewed.
_URL_MARKER = "rent.brookfieldproperties.com/wp-json"


def is_brookfield_url(url: str) -> bool:
    """True if *url* is the Brookfield WP middleware endpoint."""
    return bool(url) and _URL_MARKER in url.lower()


def _is_brookfield_body(body: Any) -> bool:
    """Return True when *body* looks like a Brookfield floorplans payload.

    Body is a flat JSON list of dicts; an item is recognised by carrying
    ``floorplanName`` together with at least one of the rent / sqft /
    availability fields that the middleware emits. Defensive — we don't
    want a different list-at-root payload from the same host to mis-route
    here.
    """
    if not isinstance(body, list) or not body:
        return False
    first = body[0]
    if not isinstance(first, dict):
        return False
    if "floorplanName" not in first:
        return False
    return any(
        k in first
        for k in (
            "minimumRent", "maximumRent",
            "minimumSQFT", "maximumSQFT",
            "availableUnitsCount", "availableDate",
            "beds", "baths",
        )
    )


def _to_int(v: Any) -> int | None:
    """Coerce a string/number to int, treating empty/zero strings as None."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in ("0", "0.0"):
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def parse_brookfield_units(
    items: list[dict[str, Any]],
    url: str,
) -> list[dict[str, str]]:
    """Project Brookfield floor-plan rows into the canonical unit-dict shape.

    Each Brookfield row is a floor-plan summary (no per-apartment rows;
    ``availableUnitsCount`` is the only per-unit signal). We emit one row
    per floor plan with the count in ``available_units`` — the downstream
    ``post_process`` step routes rows without a unit_number into
    ``plan_summaries`` automatically.
    """
    units: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue

        fp_name = str(item.get("floorplanName") or "").strip()
        beds = _to_int(item.get("beds"))
        # Brookfield emits baths as a flat integer string; treat 1.5 as a
        # possible value even though the live sample shows whole numbers.
        baths_raw = item.get("baths")
        baths_val: float | None
        if baths_raw in (None, ""):
            baths_val = None
        else:
            try:
                baths_val = float(baths_raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                baths_val = None
        sqft_min = _to_int(item.get("minimumSQFT"))
        sqft_max = _to_int(item.get("maximumSQFT"))
        sqft_str = ""
        if sqft_min and sqft_max and sqft_min != sqft_max:
            sqft_str = f"{sqft_min}-{sqft_max}"
        elif sqft_min:
            sqft_str = str(sqft_min)
        elif sqft_max:
            sqft_str = str(sqft_max)

        rent_lo = money_to_int(str(item.get("minimumRent") or ""))
        rent_hi = money_to_int(str(item.get("maximumRent") or ""))
        if rent_hi is None:
            rent_hi = rent_lo

        avail_count = _to_int(item.get("availableUnitsCount"))
        avail_date = str(item.get("availableDate") or "").strip()
        # Pre-1970 / sentinel guard — Brookfield occasionally ships
        # "0000-00-00" on plans with no live availability.
        if avail_date.startswith(("0000", "1900", "1970")):
            avail_date = ""

        has_specials = str(item.get("hasSpecials") or "").strip()
        concession = "Has specials" if has_specials == "1" else ""

        building = str(item.get("propertyName") or "").strip()

        units.append(
            make_unit_dict(
                floor_plan_name=fp_name,
                bed_label=bed_label_from(beds, fp_name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=(
                    str(int(baths_val))
                    if baths_val is not None and baths_val == int(baths_val)
                    else (str(baths_val) if baths_val is not None else "")
                ),
                sqft=sqft_str,
                # Brookfield is plan-level only — no per-apartment unit numbers
                # in the middleware response. post_process routes rows with
                # empty unit_number into plan_summaries.
                unit_number="",
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi,
                availability_status="AVAILABLE" if avail_count else "UNKNOWN",
                available_units=str(avail_count) if avail_count else "",
                availability_date=avail_date,
                building=building,
                concession=concession,
                source_api_url=url,
                extraction_tier=TIER,
            )
        )
    return units


def try_parse_brookfield(
    resp: dict[str, Any],
) -> tuple[list[dict[str, str]], bool]:
    """Best-effort Brookfield parse for a single intercepted response.

    Returns ``(units, matched)``. ``matched`` is True when the URL+body pair
    is recognised as Brookfield (even if the projection emitted zero rows —
    e.g. an empty middleware list when no plans are currently available).
    Callers use ``matched`` to suppress fall-through to less precise
    branches (the generic broad parser would otherwise emit junk).
    """
    url = str(resp.get("url") or "")
    body = resp.get("body")
    if not is_brookfield_url(url):
        return [], False
    if not _is_brookfield_body(body):
        return [], False
    assert isinstance(body, list)
    return parse_brookfield_units(body, url), True
