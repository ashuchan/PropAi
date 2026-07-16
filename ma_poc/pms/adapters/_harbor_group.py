"""Harbor Group Management adapter.

Harbor Group Management operates ~12 properties on a Drupal CMS
("perq_stable" theme) with a deterministic three-level URL family:

  ``{prop-url}/floor-plans``           — index of all floor-plan slugs
  ``{prop-url}/{plan-slug}/listing``   — plan description (NOT unit-level)
  ``{prop-url}/{plan-slug}/units``     — unit-level availability (SSR HTML)

Properties in our 4982-prop CSV:
  • Aurella Cary          harborgroupmanagement.com/apartments/nc/cary/aurella-cary
  • Waterford Village     harborgroupmanagement.com/apartments/MA/Bridgewater/Waterford-Village/
  • The Canterbury        harborgroupmanagement.com/apartments/oh/columbus/the-canterbury/

Detection: URL contains ``harborgroupmanagement.com/apartments/``.

Two-step extraction:
  1. ``parse_harbor_floor_plans`` — fetch ``{prop-url}/floor-plans`` and
     extract all plan slugs from href attributes.
  2. ``parse_harbor_units_page`` — fetch ``{prop-url}/{plan-slug}/units``
     and parse unit cards from the SSR HTML.

Unit card structure (confirmed on Aurella Cary + Rockbrook Creek, May 2026):

  <div class="listing-card">
    <p  class="listing-card-price">$1155 </p>
    <h4 class="listing-card-title">Apartment #H-201A4</h4>
    <p  class="listing-card-info listing-card-type">2 Bed</p>
    <p  class="listing-card-info listing-card-bath">1 Bath</p>
    <p  class="listing-card-info listing-card-sqft">850 Sq Ft</p>
    <p  class="listing-card-amenity-sub-text">Available Now</p>
    <!-- or: "Available May 26, 2026" -->
    <a  class="listing-card-apply apply-url"
        href="https://api.findigs.com/...?unit_id=AUR-H-201A4&move_in_date=2026-05-19">
    </a>
  </div>

``data-total-pages`` on the listing-container is a client-side display
pagination marker only — all unit records are present in the SSR HTML
on the first (and only) server response. Do NOT try ``?page=N``.

The adapter is wired into ``generic.py`` sub-tier detection for the
``harborgroupmanagement.com/apartments/`` URL pattern.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# ── Detector ────────────────────────────────────────────────────────────────

# Detection URL patterns. ``hgliving.com`` is a Harbor Group mirror domain that
# 301-redirects to ``harborgroupmanagement.com``; both must detect because the
# request URL keeps its original host, so hgliving properties otherwise miss the
# adapter and fall to the LLM/generic fallback (11 props in the 07-12 run).
_HGM_MARKERS: tuple[str, ...] = (
    "harborgroupmanagement.com/apartments/",
    "hgliving.com/apartments/",
)


def detect_harbor_group(url: str) -> bool:
    """True if the *request URL* is a Harbor Group Management property.

    Detection is URL-based (not HTML-based) because the landing HTML
    is generic Drupal — there is no unique clientlib or meta marker.
    Adapter fetches follow the ``hgliving.com`` → ``harborgroupmanagement.com``
    301 redirect, so a mirror-domain URL resolves the same as a native one.
    """
    u = (url or "").lower()
    return any(m in u for m in _HGM_MARKERS)


# ── Plan-slug discovery (operates on {prop-url}/floor-plans) ────────────────

# href pattern: /<state>/<city>/<slug>/<plan-slug>/listing
# We extract the *plan-slug* segment (one before /listing).
_LISTING_HREF_RE = re.compile(
    r"/apartments/[^/]+/[^/]+/[^/]+/([^/?#]+)/listing",
    re.IGNORECASE,
)


def parse_harbor_floor_plans(html: str, base_url: str = "") -> list[str]:
    """Extract plan slugs from the /floor-plans index page.

    Args:
        html:     Raw HTML of the ``{prop-url}/floor-plans`` page.
        base_url: Canonical property URL (used for error context only).

    Returns:
        Ordered list of plan slugs (strings), deduplicated, preserving
        first-seen order.  Returns ``[]`` if no slugs are found.
    """
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    seen: set[str] = set()
    slugs: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        m = _LISTING_HREF_RE.search(href)
        if m:
            slug = m.group(1).lower()
            if slug not in seen:
                seen.add(slug)
                slugs.append(slug)
    return slugs


# ── Unit-card parser (operates on {prop-url}/{plan-slug}/units) ──────────────

_RENT_CLEAN_RE = re.compile(r"[\$,\s]")
_BEDS_RE = re.compile(r"(\d+)\s*(?:bed|br)", re.IGNORECASE)
_STUDIO_RE = re.compile(r"\bstudio\b", re.IGNORECASE)
_BATHS_RE = re.compile(r"([\d.]+)\s*(?:bath|ba)\b", re.IGNORECASE)
_SQFT_RE = re.compile(r"([\d,]+)\s*(?:sq\.?\s*ft|sqft)", re.IGNORECASE)
_UNIT_NUM_RE = re.compile(r"#([A-Za-z0-9\-]+)")
_AVAIL_DATE_RE = re.compile(
    r"Available\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)
_MOVE_IN_RE = re.compile(r"move_in_date=(\d{4}-\d{2}-\d{2})")
_UNIT_ID_PARAM_RE = re.compile(r"[?&]unit_id=([^&]+)")


def _parse_avail(text: str) -> tuple[str, str]:
    """Return (availability_status, availability_date_str).

    Status is ``"AVAILABLE"`` or ``"UNAVAILABLE"``.
    Date string is ISO-8601 ``YYYY-MM-DD`` or ``""`` if not parseable.
    """
    t = (text or "").strip()
    if not t:
        return "UNKNOWN", ""
    if re.search(r"not available|unavailable|waitlist", t, re.IGNORECASE):
        return "UNAVAILABLE", ""
    if re.search(r"available now", t, re.IGNORECASE):
        return "AVAILABLE", ""
    dm = _AVAIL_DATE_RE.search(t)
    if dm:
        raw_date = dm.group(1).replace(",", "").strip()
        try:
            from datetime import datetime
            d = datetime.strptime(raw_date, "%B %d %Y").date()
            return "AVAILABLE", str(d)
        except ValueError:
            pass
    # Default: treat any "Available" text as available
    if re.search(r"available", t, re.IGNORECASE):
        return "AVAILABLE", ""
    return "UNKNOWN", ""


def parse_harbor_units_page(
    html: str,
    plan_slug: str = "",
    plan_name: str = "",
    base_url: str = "",
) -> list[dict[str, Any]]:
    """Parse unit records from a ``/{plan-slug}/units`` SSR HTML page.

    Args:
        html:       Raw HTML of the units page.
        plan_slug:  The plan slug from the URL (e.g. ``"the-azalea"``).
        plan_name:  Human-readable plan name, if known from /floor-plans
                    index.  Falls back to slug title-case if not provided.
        base_url:   The full URL of the units page (for source tracking).

    Returns:
        List of unit-record dicts in the jugnu canonical schema shape.
        Returns ``[]`` if no unit cards are found in the HTML.
    """
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    fp_name = plan_name or plan_slug.replace("-", " ").title()

    units: list[dict[str, Any]] = []
    for card in soup.find_all("div", class_="listing-card"):
        # ── rent ──────────────────────────────────────────────────────
        rent_el = card.find("p", class_="listing-card-price")
        rent_text = rent_el.get_text(strip=True) if rent_el else ""
        rent_raw = _RENT_CLEAN_RE.sub("", rent_text)
        try:
            rent = int(rent_raw) if rent_raw else None
        except ValueError:
            rent = None

        # ── unit number ───────────────────────────────────────────────
        title_el = card.find("h4", class_="listing-card-title")
        title_text = title_el.get_text(strip=True) if title_el else ""
        # "Apartment #H-201A4" → "H-201A4"
        num_m = _UNIT_NUM_RE.search(title_text)
        unit_number = num_m.group(1) if num_m else title_text.strip()

        # ── beds ──────────────────────────────────────────────────────
        beds_el = card.find("p", class_="listing-card-type")
        beds_text = beds_el.get_text(strip=True) if beds_el else ""
        beds: int | None = None
        if _STUDIO_RE.search(beds_text):
            beds = 0
        else:
            bm = _BEDS_RE.search(beds_text)
            if bm:
                beds = int(bm.group(1))

        # ── baths ─────────────────────────────────────────────────────
        baths_el = card.find("p", class_="listing-card-bath")
        baths_text = baths_el.get_text(strip=True) if baths_el else ""
        bath_m = _BATHS_RE.search(baths_text)
        baths: float | None = None
        if bath_m:
            try:
                baths = float(bath_m.group(1))
            except ValueError:
                pass

        # ── sqft ──────────────────────────────────────────────────────
        sqft_el = card.find("p", class_="listing-card-sqft")
        sqft_text = sqft_el.get_text(strip=True) if sqft_el else ""
        sqft_m = _SQFT_RE.search(sqft_text)
        sqft: int | None = None
        if sqft_m:
            try:
                sqft = int(sqft_m.group(1).replace(",", ""))
            except ValueError:
                pass

        # ── availability ──────────────────────────────────────────────
        avail_el = card.find("p", class_="listing-card-amenity-sub-text")
        avail_text = avail_el.get_text(strip=True) if avail_el else ""
        avail_status, avail_date = _parse_avail(avail_text)

        # ── unit_id from apply link ───────────────────────────────────
        apply_a = card.find("a", class_="apply-url")
        apply_href = (apply_a.get("href") or "") if apply_a else ""
        uid_m = _UNIT_ID_PARAM_RE.search(apply_href)
        unit_id_param = uid_m.group(1) if uid_m else ""

        # Prefer apply-link move_in_date as a high-quality availability date
        if not avail_date and apply_href:
            mi_m = _MOVE_IN_RE.search(apply_href)
            if mi_m and avail_status == "AVAILABLE":
                avail_date = mi_m.group(1)

        # ── floor-plan label ──────────────────────────────────────────
        # Derive bed/bath label from plan_slug if no inline data
        # (e.g. "the-azalea-renovated" → plan name carries the context).
        bed_label = ""
        if beds is not None and baths is not None:
            bed_label = f"{beds}B/{baths:.0f}Ba"
        elif beds is not None:
            bed_label = f"{beds}B"

        rec: dict[str, Any] = {
            "unit_number": unit_number,
            "floor_plan_name": fp_name,
            "floor_plan_id": plan_slug,
            "bed_label": bed_label,
            "bedrooms": beds,
            "bathrooms": baths,
            "sqft": sqft,
            "floor": None,
            "building": None,
            "market_rent_low": rent,
            "market_rent_high": rent,
            "availability_status": avail_status,
            "available_date_raw": avail_text,
            "available_date_post_fix": avail_date,
            "source": "harbor_group_units_page",
            "source_api_url": base_url,
            # Internal: let orchestrator store the Findigs/MRI unit ID
            "_unit_id_param": unit_id_param,
        }
        # Only include units that have at minimum a unit number
        if unit_number:
            units.append(rec)

    return units


# ── Property-URL helpers ────────────────────────────────────────────────────


def harbor_prop_base(url: str) -> str:
    """Return the canonical property base URL (no trailing slash).

    Strips any trailing path segments beyond the property slug.
    For example:
      ``https://www.harborgroupmanagement.com/apartments/nc/cary/aurella-cary/listing``
      → ``https://www.harborgroupmanagement.com/apartments/nc/cary/aurella-cary``
    """
    parsed = urlparse(url)
    # Path: /apartments/{state}/{city}/{slug}[/extra...]
    parts = parsed.path.rstrip("/").split("/")
    # Find the index of "apartments" and take 4 segments after it
    try:
        apt_idx = parts.index("apartments")
    except ValueError:
        # Not a standard HGM URL — return as-is
        return url.rstrip("/")
    base_parts = parts[: apt_idx + 4]  # apartments + state + city + slug = 4 segments
    base_path = "/".join(base_parts)
    return f"{parsed.scheme}://{parsed.netloc}{base_path}"
