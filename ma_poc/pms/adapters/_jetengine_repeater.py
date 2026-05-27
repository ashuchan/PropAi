"""JetEngine repeater extractor — WordPress + RealPage-OLL cohort.

Background — the 2026-05-23 Chrome MCP probe of copperpointeapts.com
(``/property-floor-plans/c1-3-bedroom/``) confirmed a widely-used
WordPress pattern: JetEngine's ``jet-listing-dynamic-repeater__item``
table rows render the per-floor-plan unit list with full rent, sqft,
unit number, and availability date inline in the HTML — no XHR call,
no JS hydration required:

    <tr class="jet-listing-dynamic-repeater__item">
      <td>
        <a class="rr_unit_terms_popup"
           data-unit-application-url=
             "https://8875465.onlineleasing.realpage.com/?MoveInDate=Now&UnitId=220
              &SearchUrl=...">
          10108
        </a>
      </td>
      <td>$1539</td>
      <td>1185</td>
      <td><p>Now</p></td>
    </tr>

The four columns are fixed across the cohort (the JetEngine repeater
config is shared as a template across the operator's WordPress sites):
[unit_number, rent, sqft, availability_date]. The
``data-unit-application-url`` always points to
``{N}.onlineleasing.realpage.com`` — that's the OneSite / RealPage OLL
backend ID, useful as a stable source identifier.

The floor plan's bedroom count is derived from the page URL slug
(``c1-3-bedroom`` → ``3``) or the H2 heading (``<h2>3 Bedroom</h2>``).
Sqft is per-unit from the row (it's the same across all units of one
plan but the row carries it; we trust the row).

Detection markers (all required for a confident match):
  • ``jet-listing-dynamic-repeater__item`` (the JetEngine class)
  • ``rr_unit_terms_popup`` (RealPage's resident-rent terms popup)
  • The page URL OR HTML body contains
    ``onlineleasing.realpage.com``

Validated 2026-05-23 on Copper Pointe Apartments 3 BR page —
4 units, rent + sqft + unit number + availability date on every row.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any
from urllib.parse import urlparse

from ma_poc.pms.adapters._parsing import make_unit_dict

# ── Detection ───────────────────────────────────────────────────────────────

_JET_ROW_MARKER = "jet-listing-dynamic-repeater__item"
_RR_POPUP_MARKER = "rr_unit_terms_popup"
_REALPAGE_BACKEND_MARKER = "onlineleasing.realpage.com"


def detect_jetengine_repeater(html: str, url: str = "") -> bool:
    """True when the HTML carries the JetEngine + RealPage-OLL shape.

    Three-signal gate so we don't mis-route plain JetEngine sites (the
    class is used widely by WordPress operators for non-rental
    content). All three must be present:
      1. ``jet-listing-dynamic-repeater__item`` class
      2. ``rr_unit_terms_popup`` class
      3. ``onlineleasing.realpage.com`` URL anywhere in the body

    The ``url`` parameter is currently unused — kept in the signature
    for future host-based gating if needed.
    """
    if not html:
        return False
    if _JET_ROW_MARKER not in html:
        return False
    if _RR_POPUP_MARKER not in html:
        return False
    return _REALPAGE_BACKEND_MARKER in html


# ── Page-level metadata extraction ──────────────────────────────────────────

_URL_BED_RE = re.compile(
    r"/property-floor-plans/[a-z0-9-]*?(\d+)-bedroom",
    re.IGNORECASE,
)
_URL_BED_STUDIO_RE = re.compile(
    r"/property-floor-plans/[a-z0-9-]*?(?:studio|efficiency)",
    re.IGNORECASE,
)
_H2_BED_RE = re.compile(
    r"<h[12345][^>]*>\s*(\d+)\s*Bedroom",
    re.IGNORECASE,
)
_H2_STUDIO_RE = re.compile(
    r"<h[12345][^>]*>\s*Studio",
    re.IGNORECASE,
)
_URL_PLAN_SLUG_RE = re.compile(
    r"/property-floor-plans/([a-z0-9-]+?)(?:/|$)",
    re.IGNORECASE,
)


def extract_bedroom_count(html: str, url: str) -> int | None:
    """Derive the floor plan's bedroom count from the URL slug or the
    page's H2 heading. Returns ``None`` when neither yields a result.

    URL pattern: ``/property-floor-plans/<plan-id>-<N>-bedroom/`` or
    ``/property-floor-plans/<plan-id>-studio/``.
    H2 pattern: ``<h2>3 Bedroom</h2>`` or ``<h2>Studio</h2>``.

    Studios return ``0``.
    """
    if url:
        m = _URL_BED_RE.search(url)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        if _URL_BED_STUDIO_RE.search(url):
            return 0
    if html:
        m = _H2_BED_RE.search(html)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        if _H2_STUDIO_RE.search(html):
            return 0
    return None


def extract_plan_slug(url: str) -> str:
    """Pull the floor-plan slug (e.g. ``c1-3-bedroom``) out of the URL.

    Returns ``""`` when the URL doesn't match the expected
    ``/property-floor-plans/<slug>/`` pattern.
    """
    if not url:
        return ""
    m = _URL_PLAN_SLUG_RE.search(url)
    return m.group(1) if m else ""


def extract_plan_h2_label(html: str) -> str:
    """Pull the human-readable plan heading from the first H2 element.

    Returns ``""`` when no H2 matches. Used as the floor_plan_name so
    downstream "3 Bedroom" / "Studio" / "1 Bedroom Den" labels stay
    consistent with the operator's display.
    """
    if not html:
        return ""
    m = re.search(
        r"<h[12345][^>]*>\s*((?:\d+\s*Bedroom|Studio|Efficiency)[A-Za-z0-9\s\-]*?)\s*</h[12345]>",
        html, re.IGNORECASE,
    )
    return m.group(1).strip() if m else ""


# ── Row extraction ──────────────────────────────────────────────────────────

# Capture every <tr class="jet-listing-dynamic-repeater__item">…</tr>.
# Tolerant of attribute order and extra classes after the marker.
_ROW_RE = re.compile(
    r'<tr\s+class="[^"]*jet-listing-dynamic-repeater__item[^"]*"[^>]*>(.*?)</tr>',
    re.IGNORECASE | re.DOTALL,
)

# Inside each row, extract the four cells. We don't require exactly 4
# <td>s — operators sometimes hide extra columns via CSS but still
# render the cells. The first <td> with the rr_unit_terms_popup anchor
# is the unit number; rent is the first $-prefixed numeric; sqft is
# the first 3-4 digit standalone number; available_date is "Now" or
# YYYY-MM-DD.
_UNIT_ANCHOR_RE = re.compile(
    r'<a[^>]*class="[^"]*rr_unit_terms_popup[^"]*"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_UNIT_APP_URL_RE = re.compile(
    r'data-unit-application-url=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_REALPAGE_PROPERTY_ID_RE = re.compile(
    r"https?://(\d+)\.onlineleasing\.realpage\.com",
    re.IGNORECASE,
)
_UNIT_ID_RE = re.compile(r"UnitId=(\d+)", re.IGNORECASE)
_ROW_RENT_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
_ROW_SQFT_RE = re.compile(r">\s*(\d{3,5})\s*<")  # standalone numeric cell
_ROW_DATE_RE = re.compile(
    r">\s*(Now|\d{4}-\d{2}-\d{2}|[A-Z][a-z]+\s+\d{1,2},?\s*\d{4})\s*<",
    re.IGNORECASE,
)


def _tag_strip(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def _money_to_int(s: str) -> int | None:
    if not s:
        return None
    cleaned = re.sub(r"[^\d.]", "", s)
    if not cleaned:
        return None
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def parse_jetengine_rows(
    html: str, source_url: str = ""
) -> list[dict[str, Any]]:
    """Walk the HTML, extract every JetEngine repeater row, emit one
    unit dict per row. Returns ``[]`` when no rows match.

    Per-page metadata (bedroom count, plan name) is inherited by every
    row from the URL slug / H2 heading. The RealPage propertyId is
    surfaced into ``source_ids`` so the profile updater can persist it.
    """
    if not html:
        return []

    rows = _ROW_RE.findall(html)
    if not rows:
        return []

    bed_count = extract_bedroom_count(html, source_url)
    bed_str = str(bed_count) if bed_count is not None else ""
    bed_label = ""
    if bed_count == 0:
        bed_label = "Studio"
    elif bed_count is not None:
        bed_label = f"{bed_count} Bed"

    plan_slug = extract_plan_slug(source_url)
    plan_name = extract_plan_h2_label(html) or plan_slug

    host = ""
    if source_url:
        try:
            host = urlparse(source_url).netloc.lower()
        except Exception:
            host = ""

    out: list[dict[str, Any]] = []
    for row_html in rows:
        anchor_m = _UNIT_ANCHOR_RE.search(row_html)
        if not anchor_m:
            # No unit-number anchor — JetEngine row without our marker
            # column. Skip rather than guess.
            continue
        unit_number = _tag_strip(html_lib.unescape(anchor_m.group(1)))
        if not unit_number:
            continue

        rent_m = _ROW_RENT_RE.search(row_html)
        rent_int = _money_to_int(rent_m.group(1)) if rent_m else None
        if rent_int is None:
            continue

        # Sqft: first standalone 3-5 digit cell content AFTER the
        # unit anchor and AFTER the rent cell. We strip both regions
        # before searching so:
        #   • the unit number inside ``<a>10108</a>`` isn't mistaken
        #     for sqft
        #   • the rent dollar amount isn't mistaken for sqft
        sqft_search_html = row_html
        # Strip rent first (later position is left intact by anchor strip)
        if rent_m:
            sqft_search_html = (
                sqft_search_html[: rent_m.start()]
                + sqft_search_html[rent_m.end() :]
            )
        # Re-find the anchor in the stripped string and strip its inner
        # text so the unit number doesn't false-positive.
        anchor_strip_m = _UNIT_ANCHOR_RE.search(sqft_search_html)
        if anchor_strip_m:
            sqft_search_html = (
                sqft_search_html[: anchor_strip_m.start()]
                + sqft_search_html[anchor_strip_m.end() :]
            )
        sqft_m = _ROW_SQFT_RE.search(sqft_search_html)
        sqft_str = sqft_m.group(1) if sqft_m else ""
        if not sqft_str:
            continue

        date_m = _ROW_DATE_RE.search(row_html)
        avail_date = date_m.group(1) if date_m else ""
        # Normalize "Now" to empty string (downstream treats empty as
        # immediate); preserves YYYY-MM-DD as-is.
        if avail_date.lower() == "now":
            avail_date = ""

        app_url_m = _UNIT_APP_URL_RE.search(row_html)
        app_url = html_lib.unescape(app_url_m.group(1)) if app_url_m else ""
        rp_property_id = ""
        rp_unit_id = ""
        if app_url:
            rp_m = _REALPAGE_PROPERTY_ID_RE.search(app_url)
            if rp_m:
                rp_property_id = rp_m.group(1)
            uid_m = _UNIT_ID_RE.search(app_url)
            if uid_m:
                rp_unit_id = uid_m.group(1)

        out.append(
            make_unit_dict(
                floor_plan_name=plan_name,
                bed_label=bed_label,
                bedrooms=bed_str,
                sqft=sqft_str,
                unit_number=unit_number,
                rent_low=rent_int,
                rent_range=f"${rent_int:,}",
                availability_status="AVAILABLE",
                availability_date=avail_date,
                source_api_url=source_url,
                extraction_tier="TIER_1_DOM_JETENGINE_REALPAGE_OLL",
                source_ids={
                    "realpage_oll_property_id": rp_property_id,
                    "realpage_oll_unit_id": rp_unit_id,
                    "wp_plan_slug": plan_slug,
                    "host": host,
                },
            )
        )
    return out
