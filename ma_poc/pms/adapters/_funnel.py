"""Funnel Leasing (formerly Nestio) adapter.

Funnel is the multifamily CRM / leasing platform that Nestio rebranded to
in August 2020. Their customer list includes Essex Property Trust
(~247 properties via essexapartmenthomes.com sitemap), Cortland, UDR,
RedPeak, Monument Real Estate, Avanti Residential, Dermot Company —
combined ~50,000+ units.

Integration shape (universal across Funnel-proxied customers):
  • Property page URL: ``{site}.com/apartments/{city}/{slug}``
  • Property carries ``<div id="CommunityId" data-communityid="{prop_id}">``
    in HTML (also ``data-property-id="{prop_id}"`` on other elements).
  • API endpoint: ``{site}.com/api/properties/{prop_id}/availability?
    start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&format=spa``
  • JSON response: ``{result: {floorplans: [{units: [...]}]}}``
  • Each unit: ``{unit_id, name, floorplan_name, beds, baths, sqft,
    minimum_rent, maximum_rent, availability_date, specials, amenities}``

The endpoint is Vercel-fronted — plain ``curl`` gets a 429 bot challenge.
``curl_cffi`` with ``impersonate="chrome120"`` (production's default
network layer) passes cleanly. See
``project_nestio_funnel_discovery_2026-05-21`` memo.

Validated 2026-05-21 against 3 distinct Essex properties (Belcarra,
Connolly Station, Allure at Scripps Ranch) — pattern is universal.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

# ── Detector ────────────────────────────────────────────────────────────────

# Primary detector: <div id="CommunityId" data-communityid="...">  observed
# on every Funnel-proxied property page. Captures the property_id we need
# for the API call. Multiple variants — Essex puts it in a <div>, some
# customers use other tag names; allow any tag with both attributes.
_COMMUNITY_ID_RE = re.compile(
    r'\bdata-communityid="(\d+)"',
    re.IGNORECASE,
)

# Secondary detector: ``data-property-id="..."`` is also present on a
# different DOM element (the favorites button); used as a fallback if
# the CommunityId div is missing.
_PROPERTY_ID_RE = re.compile(
    r'\bdata-property-id="(\d+)"',
    re.IGNORECASE,
)


def detect_funnel(html: str) -> bool:
    """True if the HTML carries any Funnel-property-page marker.

    Cheap substring + regex check, no full parse. Returns True when we
    have enough signal to attempt extraction. Reject when only generic
    Nestio asset references appear (those can leak onto a non-Funnel
    page via partial integrations).
    """
    if not html:
        return False
    if "data-communityid=" not in html.lower():
        # Allow secondary marker too, but only when paired with a Funnel
        # asset host reference to reduce false positives on unrelated
        # data-property-id attributes from other CMSes.
        if "data-property-id=" in html.lower() and (
            "nestiostatic.com" in html or "funnelstatic.com" in html
        ):
            return True
        return False
    return True


def find_property_id(html: str) -> str | None:
    """Extract the Funnel ``property_id`` from a customer property page.

    Returns the first ``data-communityid`` value if present, else falls
    back to ``data-property-id`` (matched only when a Funnel asset host
    is also referenced, to avoid bare-data-attr false positives).
    """
    if not html:
        return None
    m = _COMMUNITY_ID_RE.search(html)
    if m:
        return m.group(1)
    if "nestiostatic.com" in html or "funnelstatic.com" in html:
        m2 = _PROPERTY_ID_RE.search(html)
        if m2:
            return m2.group(1)
    return None


# ── API URL builder ────────────────────────────────────────────────────────


def build_availability_api_url(
    base_url: str,
    property_id: str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> str:
    """Build the Funnel ``/api/properties/{id}/availability`` URL.

    Defaults to start=today, end=today+60d. Date range affects which
    units are returned (units available outside the window are
    suppressed); 60d is wide enough for most leasing-cycle visibility
    without inflating response size. Always uses ``format=spa`` —
    that's the only format the Funnel server accepts.
    """
    if start_date is None:
        start_date = date.today()
    if end_date is None:
        end_date = start_date + timedelta(days=60)
    p = urlparse(base_url)
    origin = f"{p.scheme}://{p.netloc}" if p.netloc else base_url
    return (
        f"{origin}/api/properties/{property_id}/availability"
        f"?start_date={start_date.isoformat()}"
        f"&end_date={end_date.isoformat()}"
        f"&format=spa"
    )


# ── API response parser ────────────────────────────────────────────────────


def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        try:
            return int(str(v).replace(",", "").replace("$", "").strip())
        except (TypeError, ValueError):
            return None


def _str_or_empty(v: Any) -> str:
    return "" if v is None else str(v)


def parse_funnel_api_response(
    data: dict[str, Any], source_url: str = ""
) -> list[dict[str, Any]]:
    """Walk a Funnel ``/api/properties/{id}/availability`` JSON body
    into unit records.

    The response shape is ``{result: {floorplans: [{units: [...]}]}}``.
    Each unit carries enough to populate the standard adapter unit dict.
    Per-floor-plan metadata (``floorplan_name``, ``floorplan_id``) is
    inherited by every unit in that floor plan.

    Defensive: returns empty list on malformed input rather than raising.
    """
    if not isinstance(data, dict):
        return []
    result = data.get("result")
    if not isinstance(result, dict):
        return []
    floorplans = result.get("floorplans")
    if not isinstance(floorplans, list):
        return []

    out: list[dict[str, Any]] = []
    seen_unit_ids: set[str] = set()
    for fp in floorplans:
        if not isinstance(fp, dict):
            continue
        units = fp.get("units")
        if not isinstance(units, list):
            continue
        fp_id = _str_or_empty(fp.get("floorplan_id") or fp.get("id"))
        # The floorplan level CAN carry its own name + structural metadata
        # — but the per-unit payload usually carries everything needed,
        # so we read floorplan name only as a fallback for units that
        # don't have ``floorplan_name`` themselves (older API versions).
        fp_name_fallback = _str_or_empty(fp.get("name"))

        for u in units:
            if not isinstance(u, dict):
                continue
            uid = _str_or_empty(u.get("unit_id"))
            if uid and uid in seen_unit_ids:
                continue
            if uid:
                seen_unit_ids.add(uid)

            rent_lo = _to_int(u.get("minimum_rent"))
            rent_hi = _to_int(u.get("maximum_rent"))
            # Some Funnel responses store rent under different keys —
            # the schema permits ``rent`` / ``displayPrice`` fallbacks.
            if rent_lo is None and rent_hi is None:
                rent_lo = _to_int(u.get("rent") or u.get("displayPrice"))
                rent_hi = rent_lo

            if rent_lo is not None and rent_hi is not None and rent_lo != rent_hi:
                rent_range = f"${rent_lo:,} - ${rent_hi:,}"
            elif rent_lo is not None:
                rent_range = f"${rent_lo:,}"
            elif rent_hi is not None:
                rent_range = f"${rent_hi:,}"
            else:
                rent_range = ""

            avail = _str_or_empty(u.get("availability_date"))
            if "T" in avail:
                avail = avail.split("T", 1)[0]
            avail_status = "AVAILABLE" if (avail or rent_lo) else ""

            # specials -> concession string (Funnel ships array of objects
            # with descriptions; take the first description as the
            # canonical concession label).
            concession = ""
            specials = u.get("specials")
            if isinstance(specials, list) and specials:
                first = specials[0]
                if isinstance(first, dict):
                    concession = _str_or_empty(
                        first.get("description")
                        or first.get("name")
                        or first.get("text")
                    )
                elif isinstance(first, str):
                    concession = first

            out.append({
                "floor_plan_name": _str_or_empty(
                    u.get("floorplan_name") or fp_name_fallback
                ),
                "bed_label": "",
                "bedrooms": _str_or_empty(u.get("beds")),
                "bathrooms": _str_or_empty(u.get("baths")),
                "sqft": _str_or_empty(u.get("sqft")),
                "unit_number": _str_or_empty(u.get("name")),
                "floor": _str_or_empty(
                    u.get("floor")
                    or (u.get("floorplate") or {}).get("floor")
                ),
                "building": _str_or_empty(
                    (u.get("floorplate") or {}).get("building_name")
                ),
                "rent_range": rent_range,
                "market_rent_low": rent_lo,
                "market_rent_high": rent_hi,
                "deposit": "",
                "concession": concession,
                "availability_status": avail_status,
                "available_units": "",
                "availability_date": avail,
                "lease_term": "",
                "move_in_date": "",
                "source_api_url": source_url,
                "source": "funnel_api",
                "property_unit_id": uid,
                "propertyfloorplanid": _str_or_empty(u.get("floorplan_id") or fp_id),
            })
    return out


__all__ = [
    "build_availability_api_url",
    "detect_funnel",
    "find_property_id",
    "parse_funnel_api_response",
]
