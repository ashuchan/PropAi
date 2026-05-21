"""AIR Communities (Apartment Income REIT) adapter.

AIR Communities is the operator behind 76 apartment communities + 27,010
apartment homes across the US. All properties share a common Adobe
Experience Manager (AEM) stack — observed across laurelcrossing,
adara, arcadia, 20thstreetstation, 21fitzsimons, and 15fifty5 (probed
2026-05-21).

URL family (universal across the portfolio):
  • ``{domain}/residences.html`` — floor-plan list page
  • ``{domain}/floor-plan/{bed-class}/{slug}.html`` — per-plan page with
    unit-level data inline
  • ``{domain}/content/air-properties/{aem-slug}/us/en/floor-plan/
    {bed-class}/{slug}/jcr:content/.../availableunits.json`` —
    JSON form of the unit list (redundant; HTML already has it)

Two-step extraction:
  1. ``parse_residences_html`` — list of floor plans (name, sqft,
     starting rent, bedroom count, per-plan deep-link URL).
  2. ``parse_per_plan_html`` — list of units (unit_number, rent,
     availability_date, propertyUnitID), one call per floor plan.

Detection marker: ``apartmentIncomeReit/clientlibs`` substring in HTML
body. Unique to AIR — Adobe ships AEM clientlib paths under each
brand's legal name, and "Apartment Income REIT" is AIR Communities'
SEC-registered name.

Phase 6.x positioning: this is a NEW dedicated adapter, not an extension
of an existing tier. Sits above Tier 4 LLM in the cascade.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

# ── Detector ────────────────────────────────────────────────────────────────

_AIR_MARKER = "apartmentIncomeReit/clientlibs"


def detect_air_communities(html: str) -> bool:
    """True if the HTML carries the AIR Communities AEM clientlib marker.

    Cheap substring check — no parsing. Used by the adapter dispatch to
    route the request before any expensive extraction work runs.
    """
    if not html:
        return False
    return _AIR_MARKER in html


# ── Plan-list parser (operates on /residences.html) ─────────────────────────

# Bedroom-container id → canonical integer bedroom count. Two AIR-observed
# id forms in production:
#   • Bare form  ``studio`` / ``one`` / ``two`` / ``three``  (laurelcrossing HAR)
#   • Compound   ``one-bedroom`` / ``two-bedroom`` / ``three-bedroom``  (adara,
#     arcadia, 21fitzsimons, 20thstreetstation, 15fifty5 live probes 2026-05-21)
# Map both — same canonical integer.
_BEDROOM_ID_TO_INT: dict[str, str] = {
    "studio": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "one-bedroom": "1",
    "two-bedroom": "2",
    "three-bedroom": "3",
    "four-bedroom": "4",
    "five-bedroom": "5",
}

_RENT_FROM_RE = re.compile(
    r"(?:Starting\s+at|From|Starts\s+at)?\s*\$\s*([1-9]\d{0,3}(?:,\d{3})*|\d{3,5})",
    re.IGNORECASE,
)
_SQFT_RE = re.compile(r"([1-9]\d{1,4}(?:,\d{3})*)\s*Sq\.?\s*Ft\.?", re.IGNORECASE)


def _money_to_int(s: str) -> int | None:
    try:
        return int(s.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def parse_residences_html(html: str, base_url: str = "") -> list[dict[str, Any]]:
    """Extract the plan-level list from ``{property}/residences.html``.

    Returns one dict per floor plan with:
      ``floor_plan_name``, ``bedrooms`` (str: "0"/"1"/"2"/...),
      ``sqft``, ``market_rent_low``, ``rent_range``,
      ``propertyfloorplanid``, ``details_url`` (absolute URL to the
      per-plan page), ``source``.

    Returns empty list if the HTML doesn't carry the AIR marker — the
    caller (adapter dispatch) is expected to detect first.
    """
    if not detect_air_communities(html):
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    out: list[dict[str, Any]] = []
    for container in soup.find_all("div", class_="bedroomContainer"):
        cid = (container.get("id") or "").strip().lower()
        bedrooms = _BEDROOM_ID_TO_INT.get(cid, "")
        for card in container.find_all("div", class_="floor-plan-item"):
            fp_id = (card.get("data-propertyfloorplanid") or "").strip()
            # Plan name
            name_el = card.find("div", class_=re.compile(r"plan-name\s+name"))
            name = name_el.get_text(strip=True) if name_el else ""
            # Starting rent
            rent_el = card.find("div", class_=re.compile(r"plan-name\s+price"))
            rent_text = rent_el.get_text(" ", strip=True) if rent_el else ""
            rent_match = _RENT_FROM_RE.search(rent_text)
            rent_low = _money_to_int(rent_match.group(1)) if rent_match else None
            rent_range = f"${rent_low:,}" if rent_low is not None else ""
            # Sqft
            feet_el = card.find("div", class_=re.compile(r"plan-name\s+feet"))
            feet_text = feet_el.get_text(" ", strip=True) if feet_el else ""
            sqft_match = _SQFT_RE.search(feet_text)
            sqft = sqft_match.group(1).replace(",", "") if sqft_match else ""
            # Details URL
            details_a = card.find("a", class_="details-btn")
            details_href = (details_a.get("href") if details_a else "") or ""
            details_url = urljoin(base_url, details_href) if base_url else details_href

            # Drop entries with no usable signal
            if not name and not fp_id:
                continue

            out.append({
                "floor_plan_name": name,
                "bed_label": "",
                "bedrooms": bedrooms,
                "bathrooms": "",
                "sqft": sqft,
                "unit_number": "",
                "floor": "",
                "building": "",
                "rent_range": rent_range,
                "market_rent_low": rent_low,
                "market_rent_high": rent_low,
                "deposit": "",
                "concession": "",
                "availability_status": "",
                "available_units": "",
                "availability_date": "",
                "lease_term": "",
                "move_in_date": "",
                "source_api_url": base_url,
                "source": "air_communities_plan",
                "propertyfloorplanid": fp_id,
                "details_url": details_url,
            })
    return out


# ── Unit-list parser (operates on /floor-plan/{bed}/{slug}.html) ────────────


def derive_plan_context_from_url(url: str) -> dict[str, Any]:
    """Pull bedrooms + plan-slug out of a per-plan URL.

    Works on the canonical AIR shape ``/floor-plan/{bed-class}/{slug}.html``
    where ``bed-class`` is ``studio``, ``1-bedroom``, ``2-bedroom``, etc.
    Returns ``{}`` if the URL doesn't match the AIR pattern — caller
    falls back to empty plan_context.

    Used when ``parse_per_plan_html`` is called standalone (the
    orchestrator follows a per-plan sub-page link without first parsing
    the parent ``/residences.html``).
    """
    m = re.search(
        r"/floor-plan/([a-z0-9][a-z0-9\-]*?)/([a-z0-9\-]+?)(?:\.html)?(?:[?#]|$)",
        url,
        re.IGNORECASE,
    )
    if not m:
        return {}
    bed_class = m.group(1).lower()
    plan_slug = m.group(2)
    # Bed class → integer. Handles ``studio``, ``1-bedroom``, ``2-bedroom``,
    # also tolerates bare ``1`` if a property uses that form.
    bedrooms = ""
    if bed_class == "studio":
        bedrooms = "0"
    else:
        bed_m = re.match(r"(\d+)", bed_class)
        if bed_m:
            bedrooms = bed_m.group(1)
    return {
        "bedrooms": bedrooms,
        "plan_slug": plan_slug,
    }


_UNIT_NUMBER_RE = re.compile(r"Unit\s*#?\s*([A-Z0-9][A-Z0-9\-]{0,12})", re.IGNORECASE)
_AVAIL_DATE_RE = re.compile(
    r"Available\s+("
    r"Now|Today|Soon|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2}"
    r"(?:,?\s+\d{4})?"
    r")",
    re.IGNORECASE,
)
_UNIT_FEE_RE = re.compile(r"\$\s*([1-9]\d{0,3}(?:,\d{3})*|\d{3,5})")


def parse_per_plan_html(
    html: str,
    plan_context: dict[str, Any] | None = None,
    base_url: str = "",
) -> list[dict[str, Any]]:
    """Extract unit-level records from a per-plan ``/floor-plan/{bed}/{slug}.html``.

    Each plan page lists 0–N available units inline. We anchor on
    ``data-property-unit-id`` attributes (one per unit, observed across
    all probed properties) — the surrounding wrapper carries unit number
    + availability date in visible text and rent in ``.available-unit__fee``.

    The ``plan_context`` argument carries plan-level metadata from
    ``parse_residences_html`` so the emitted unit records inherit
    bedrooms / bathrooms / sqft / floor_plan_name. Passing ``None`` is
    accepted but emits records with empty plan-context fields.
    """
    if not html:
        return []
    if plan_context is None:
        plan_context = {}
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    out: list[dict[str, Any]] = []
    seen_unit_ids: set[str] = set()

    # Each unit anchors on an element carrying data-property-unit-id.
    # The unit's wrapper is typically the parent or a nearby container
    # with class="available-unit-wrapper".
    for unit_el in soup.find_all(attrs={"data-property-unit-id": True}):
        unit_id = (unit_el.get("data-property-unit-id") or "").strip()
        if not unit_id or unit_id in seen_unit_ids:
            continue
        seen_unit_ids.add(unit_id)

        # Find the surrounding wrapper to bound our text scan. Walk up
        # to the nearest ancestor that has class="available-unit-wrapper"
        # or stop at <body>.
        wrapper = unit_el
        for _ in range(6):  # bounded walk
            parent = wrapper.parent
            if not parent or parent.name == "body":
                break
            classes = parent.get("class") or []
            if any(c == "available-unit-wrapper" or c.startswith("available-unit") for c in classes):
                wrapper = parent
                break
            wrapper = parent

        wrapper_text = wrapper.get_text(" ", strip=True)

        # Unit number — first "Unit #X" mention in the wrapper text.
        unum_match = _UNIT_NUMBER_RE.search(wrapper_text)
        unit_number = unum_match.group(1) if unum_match else ""

        # Availability — "Available {Date}" or "Available Now/Today/Soon"
        avail_match = _AVAIL_DATE_RE.search(wrapper_text)
        avail_text = avail_match.group(1).strip() if avail_match else ""
        if avail_text.lower() in ("now", "today"):
            avail_status = "AVAILABLE"
            avail_date = ""
        elif avail_text.lower() == "soon":
            avail_status = "AVAILABLE_SOON"
            avail_date = ""
        elif avail_text:
            avail_status = "AVAILABLE"
            avail_date = avail_text
        else:
            avail_status = ""
            avail_date = ""

        # Rent — prefer .available-unit__fee text if present, else any
        # $-amount inside the wrapper.
        rent_low: int | None = None
        fee_el = wrapper.find(class_=re.compile(r"available-unit__fee|unit-fee"))
        if fee_el:
            fee_text = fee_el.get_text(" ", strip=True)
            fee_match = _UNIT_FEE_RE.search(fee_text)
            if fee_match:
                rent_low = _money_to_int(fee_match.group(1))
        if rent_low is None:
            fee_match = _UNIT_FEE_RE.search(wrapper_text)
            if fee_match:
                rent_low = _money_to_int(fee_match.group(1))
        rent_range = f"${rent_low:,}" if rent_low is not None else ""

        # Inherit plan-context fields
        out.append({
            "floor_plan_name": plan_context.get("floor_plan_name", ""),
            "bed_label": "",
            "bedrooms": plan_context.get("bedrooms", ""),
            "bathrooms": plan_context.get("bathrooms", ""),
            "sqft": plan_context.get("sqft", ""),
            "unit_number": unit_number,
            "floor": "",
            "building": "",
            "rent_range": rent_range,
            "market_rent_low": rent_low,
            "market_rent_high": rent_low,
            "deposit": "",
            "concession": "",
            "availability_status": avail_status,
            "available_units": "",
            "availability_date": avail_date,
            "lease_term": "",
            "move_in_date": "",
            "source_api_url": base_url,
            "source": "air_communities_unit",
            "property_unit_id": unit_id,
            "propertyfloorplanid": plan_context.get("propertyfloorplanid", ""),
        })
    return out


__all__ = [
    "derive_plan_context_from_url",
    "detect_air_communities",
    "parse_per_plan_html",
    "parse_residences_html",
]
