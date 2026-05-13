"""
Spherexx Presentation Software ("Convert") adapter.

Research log
------------
Web sources consulted:
  - https://spherexx.com — Spherexx multifamily marketing platform
  - https://presentation.spherexx.app — the iframe-hosted SPA backend
Live probe (2026-05-13, henryonthepark.com/interactive-site-map/):
  - Embed pattern: <script>window.sspcfg={key:'<base64>',opts:{...}}</script>
    + <script src="https://presentation.spherexx.app/js/ssploader.js" defer>
  - Loader creates an iframe → presentation.spherexx.app/#/ssp/availability
  - Iframe makes POST /api/authenticate → returns JWT
  - Subsequent calls use Bearer <JWT>:
      GET /api/community       (property metadata)
      GET /api/configuration   (site-plan UI config)
      GET /api/unit            (UNIT LIST — list of unit objects)
      GET /api/floorplan       (floor-plan list)
      GET /api/amenity
      GET /api/fees
Key findings:
  - /api/unit is a JSON ARRAY of unit objects with the shape:
      {ID, Name, Building, Number, Sqft, Bed, Bath, Floor, Price,
       PriceMin, PriceMax, AvailableDate, FloorplanID, FloorplanName,
       ...}
  - /api/floorplan is a JSON ARRAY of floor-plan metadata:
      {ID, Name, Bed, Bath, MinSqFt, MaxSqFt, ...}
  - Units join to floor-plans via FloorplanID — but units already carry
    FloorplanName so we don't actually need the join for emission.
  - The canary's page.on("response") captures all iframe XHRs since
    Playwright fires response events for child frames, so the adapter
    doesn't need to authenticate / re-fetch — the captured responses
    are in ctx._api_responses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


_TIER_BASE = "TIER_1_API_SPHEREXX"
_TIER_NO_RESPONSE = f"{_TIER_BASE}_NO_RESPONSE"
_TIER_SHAPE_REJECTED = f"{_TIER_BASE}_SHAPE_REJECTED"
_TIER_PARSE_FAILED = f"{_TIER_BASE}_PARSE_FAILED"


# Field names that uniquely identify a Spherexx /api/unit response. The
# array elements carry these mixed-case keys (different from any other
# adapter's API shape, so the body-shape check is unambiguous).
_SPHEREXX_UNIT_KEYS = frozenset({
    "ID", "Name", "Building", "Sqft", "Bed", "Bath", "Price",
    "FloorplanID", "FloorplanName", "AvailableDate",
})


def _is_spherexx_unit_response(body: Any) -> bool:
    """True when *body* is a Spherexx /api/unit array.

    Shape: list[ dict with at least {ID, Name, Sqft, Bed, Bath, Price,
    FloorplanID, FloorplanName} ]. Empty arrays are NOT a match — they
    don't have enough signal to commit to this adapter.
    """
    if not isinstance(body, list):
        return False
    if not body:
        return False
    first = body[0]
    if not isinstance(first, dict):
        return False
    # Require ≥ 6 of the 9 signature keys present. The full set rarely all
    # appear (some properties don't have Building), so a partial match is
    # the right gate.
    matched = sum(1 for k in _SPHEREXX_UNIT_KEYS if k in first)
    return matched >= 6


def _is_spherexx_floorplan_response(body: Any) -> bool:
    """True when *body* is a Spherexx /api/floorplan array."""
    if not isinstance(body, list) or not body:
        return False
    first = body[0]
    if not isinstance(first, dict):
        return False
    # Floor-plan signature: {ID, Name, Bed, Bath, MinSqFt or MaxSqFt}.
    # Distinct from /api/unit by the MinSqFt/MaxSqFt keys (units use Sqft).
    return (
        "ID" in first
        and "Name" in first
        and ("MinSqFt" in first or "MaxSqFt" in first)
    )


def _parse_spherexx_unit(u: dict[str, Any], url: str) -> dict[str, str] | None:
    """Parse one Spherexx unit dict → our standard unit-dict shape.

    Returns None when the unit lacks both Price and Sqft (truly empty
    placeholder — Spherexx sometimes returns these for unbuilt buildings).
    """
    # Price — prefer PriceMin (lowest avail rent for the unit); falls
    # back to Price (single value) when range fields are absent.
    price_min_raw = u.get("PriceMin") or u.get("Price")
    price_max_raw = u.get("PriceMax") or u.get("Price")
    price_min: int | None = None
    price_max: int | None = None
    if isinstance(price_min_raw, (int, float)) and price_min_raw > 0:
        price_min = int(price_min_raw)
    if isinstance(price_max_raw, (int, float)) and price_max_raw > 0:
        price_max = int(price_max_raw)
    if price_min is None and price_max is None:
        # Try string forms ("$1,592" etc.) — Spherexx normally emits
        # numbers but be defensive.
        price_min = money_to_int(str(u.get("PriceMin") or u.get("Price") or ""))
        price_max = money_to_int(str(u.get("PriceMax") or u.get("Price") or ""))

    # Sqft
    sqft_raw = u.get("Sqft")
    sqft = str(int(sqft_raw)) if isinstance(sqft_raw, (int, float)) and sqft_raw > 0 else ""

    # Bed / Bath — Spherexx emits floats (1.0, 2.5).
    bed_raw = u.get("Bed")
    bath_raw = u.get("Bath")
    beds = int(bed_raw) if isinstance(bed_raw, (int, float)) and bed_raw >= 0 else None
    baths_str = ""
    if isinstance(bath_raw, (int, float)) and bath_raw > 0:
        # 1.0 → "1"; 2.5 → "2.5"
        baths_str = f"{bath_raw:.1f}".rstrip("0").rstrip(".")

    # Quality gate — skip rows with no rent AND no sqft. These are usually
    # placeholders for buildings that aren't on the market yet.
    if price_min is None and price_max is None and not sqft:
        return None

    fp_name = str(u.get("FloorplanName") or "").strip()
    name = str(u.get("Name") or "").strip()
    building = str(u.get("Building") or "").strip()
    floor = str(u.get("Floor") or "").strip()

    avail_raw = u.get("AvailableDate") or u.get("availableDate") or ""
    avail = str(avail_raw).split("T")[0] if avail_raw else ""

    if price_min and price_max and price_min != price_max:
        rent_range = f"${price_min:,} - ${price_max:,}"
    elif price_min:
        rent_range = f"${price_min:,}"
    elif price_max:
        rent_range = f"${price_max:,}"
    else:
        rent_range = ""

    return make_unit_dict(
        floor_plan_name=fp_name,
        bed_label=bed_label_from(beds, fp_name),
        bedrooms=str(beds) if beds is not None else "",
        bathrooms=baths_str,
        sqft=sqft,
        unit_number=name,
        floor=floor,
        building=building,
        rent_range=rent_range,
        rent_low=price_min,
        rent_high=price_max or price_min,
        availability_status="AVAILABLE",
        available_units="1",
        availability_date=avail,
        source_api_url=url,
        extraction_tier=_TIER_BASE,
    )


def parse_spherexx_units(body: list[dict[str, Any]], url: str) -> list[dict[str, str]]:
    """Parse a Spherexx /api/unit array → list of standard unit dicts."""
    out: list[dict[str, str]] = []
    for u in body:
        if not isinstance(u, dict):
            continue
        rec = _parse_spherexx_unit(u, url)
        if rec is not None:
            out.append(rec)
    return out


class SpherexxAdapter:
    """Spherexx Presentation Software adapter.

    Detection: site embeds ``presentation.spherexx.app`` or has
    ``window.sspcfg`` (the loader config global). Adapter parses
    ``/api/unit`` JSON arrays captured during page load.
    """

    pms_name: str = "spherexx"
    _fingerprints: list[str] = [
        "presentation.spherexx.app",
        "spherexx.app",
        "spherexx.com",
        "sspcfg",
        "ssploader.js",
    ]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from Spherexx /api/unit responses captured at fetch time."""
        result = AdapterResult(tier_used=_TIER_BASE)
        all_units: list[dict[str, str]] = []

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        spherexx_unit_resps: list[dict[str, Any]] = []
        spherexx_any_resps: list[dict[str, Any]] = []

        for resp in api_responses:
            url = resp.get("url", "")
            body = resp.get("body")
            url_lower = url.lower()
            if "spherexx" not in url_lower:
                continue
            spherexx_any_resps.append(resp)
            if not _is_spherexx_unit_response(body):
                continue
            # _is_spherexx_unit_response returned True → body is a non-empty
            # list[dict[str, Any]]. Cast for the type checker.
            assert isinstance(body, list)
            spherexx_unit_resps.append(resp)
            try:
                units = parse_spherexx_units(body, url)
            except Exception as exc:
                result.errors.append(f"spherexx-parse-error: {exc}")
                continue
            if units:
                all_units.extend(units)
                result.api_responses.append(resp)

        if all_units:
            result.units = all_units
            result.winning_url = (
                result.api_responses[0].get("url")
                if result.api_responses else None
            )
            result.confidence = min(0.95, 0.7 + 0.04 * len(all_units))
            result.tier_used = _TIER_BASE
            return result

        # Failure-mode classification.
        result.confidence = 0.0
        if not spherexx_any_resps:
            result.tier_used = _TIER_NO_RESPONSE
            result.errors.append(
                "SPHEREXX_NO_RESPONSE: no responses from presentation.spherexx.app "
                "captured during page load — the iframe may not have hydrated "
                "before Playwright captured (try increasing settle time) or the "
                "site doesn't actually embed a Spherexx widget"
            )
        elif not spherexx_unit_resps:
            result.tier_used = _TIER_SHAPE_REJECTED
            seen_paths = sorted({
                r.get("url", "").split("?")[0].rsplit("/", 1)[-1]
                for r in spherexx_any_resps
            })
            result.errors.append(
                f"SPHEREXX_SHAPE_REJECTED: {len(spherexx_any_resps)} responses "
                f"captured from spherexx.app (endpoints: {seen_paths}), but none "
                f"matched the /api/unit array shape. Expected list of dicts "
                f"with keys ID/Name/Sqft/Bed/Bath/Price/FloorplanID/FloorplanName"
            )
        else:
            result.tier_used = _TIER_PARSE_FAILED
            result.errors.append(
                f"SPHEREXX_PARSE_FAILED: {len(spherexx_unit_resps)} unit "
                "responses matched the shape but parsing produced 0 records — "
                "field-name drift on Spherexx side"
            )

        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    def matches_response_body(self, body: Any) -> bool:
        """Body-shape check used by ``detector.confirm_detection``."""
        return _is_spherexx_unit_response(body)
