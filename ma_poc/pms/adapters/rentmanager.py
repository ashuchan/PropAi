"""
Rent Manager adapter.

Research log
------------
Web sources consulted:
  - https://rentmanager.com/websites — Rent Manager's vanity-website product
    (footer link is the canonical fingerprint observed on KRC Apartments and
    other Rent-Manager-hosted properties).
Real payloads inspected (curl 2026-05-06):
  - https://krcapartments.com/property-detail/norfolk-place/ — 122KB; static
    HTML contains ``<tr class='unit_avail_container'>`` rows inside a
    ``jQuery('#availabilitytable').append(`...`)`` template. After
    Playwright/curl renders the JS, the rows become real DOM elements.
  - https://krcapartments.com/property-detail/virginia-flats/ — same shape.
Key findings:
  - Strategy is dom_first. Unit data is always inside an ``#availabilitytable``
    table whose rows carry ``class="unit_avail_container"`` and a ``data-date``
    attribute. Each row has cells:
      td.unit-number, td.unit-beds, td.unit-rent, td.unit-sqft, td.unit-date,
      td.unit-info, td.unit-tour, td.unit-action
  - There is also a hidden ``<div id="{id}-detail">`` block per unit with
    Beds / Baths / Sq.Ft. / Rent labels — useful for joining bath count
    (``td.unit-beds`` only carries bed count, baths come from the detail block).
  - No XHR is used; the data is in static HTML inside a JS template literal,
    rendered by jQuery on DOMContentLoaded. Both ``page.content()`` (post-
    render) and the raw fetch body (pre-render, in a script block) carry it.
    This adapter handles the post-render shape — the static-template variant
    falls through to the GenericAdapter HTML cascade.

Detection
---------
Detector adds an HTML marker for ``rentmanager.com/websites`` (footer "Powered
by Rent Manager" link). When matched, dispatch routes here; this adapter then
parses the unit-avail-container rows out of the HTML.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


_TIER_BASE = "TIER_3_DOM_RENTMANAGER"
_TIER_NO_HTML = f"{_TIER_BASE}_NO_HTML"
_TIER_NO_ROWS = f"{_TIER_BASE}_NO_ROWS"

# Match either a real DOM row (post-render) or the same row inside a JS
# template literal (pre-render — KRC ships the rows as ``jQuery(...).append(`
# <tr class='unit_avail_container' ...>``...`)``). Both hit the same regex
# because the structure is identical; only the surrounding ``\``/`<script>``
# differs and is ignored by the row-level scan.
_ROW_RE = re.compile(
    r"<tr[^>]*class=['\"][^'\"]*\bunit_avail_container\b[^'\"]*['\"][^>]*?(?:data-date=['\"]([^'\"]*)['\"])?[^>]*>(.*?)</tr>",
    re.IGNORECASE | re.DOTALL,
)
# Cell extractors. Class names confirmed on KRC samples; the values inside
# ``td.unit-rent`` carry the full "$1,275.00" string — we strip via money_to_int.
_CELL_PATTERNS: dict[str, re.Pattern[str]] = {
    "unit_number": re.compile(
        r"<td[^>]*class=['\"][^'\"]*\bunit-number\b[^'\"]*['\"][^>]*>(.*?)</td>",
        re.IGNORECASE | re.DOTALL,
    ),
    "unit_beds": re.compile(
        r"<td[^>]*class=['\"][^'\"]*\bunit-beds\b[^'\"]*['\"][^>]*>(.*?)</td>",
        re.IGNORECASE | re.DOTALL,
    ),
    "unit_rent": re.compile(
        r"<td[^>]*class=['\"][^'\"]*\bunit-rent\b[^'\"]*['\"][^>]*>(.*?)</td>",
        re.IGNORECASE | re.DOTALL,
    ),
    "unit_sqft": re.compile(
        r"<td[^>]*class=['\"][^'\"]*\bunit-sqft\b[^'\"]*['\"][^>]*>(.*?)</td>",
        re.IGNORECASE | re.DOTALL,
    ),
    "unit_date": re.compile(
        r"<td[^>]*class=['\"][^'\"]*\bunit-date\b[^'\"]*['\"][^>]*>(.*?)</td>",
        re.IGNORECASE | re.DOTALL,
    ),
}
# Bath count lives in a hidden detail block, not the row cells. Pattern from
# KRC sample: ``<strong>Baths:</strong> 1.0<br />``.
_BATH_RE = re.compile(
    r"<strong>\s*Baths\s*:?\s*</strong>\s*([0-9.]+)",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _text(html_fragment: str) -> str:
    """Strip tags + collapse whitespace from an HTML fragment."""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html_fragment or "")).strip()


def parse_rentmanager_html(html: str) -> list[dict[str, str]]:
    """Extract unit records from a Rent Manager page HTML.

    Walks every ``<tr class='unit_avail_container'>`` row, pulls the cell
    contents, and joins each row to the property-level bath count (when
    present in any hidden detail block on the page).

    Returns an empty list when no rows match — the caller surfaces this as
    ``TIER_3_DOM_RENTMANAGER_NO_ROWS`` so the cascade can fall through to
    the LLM tiers without ambiguity.
    """
    if not html:
        return []

    units: list[dict[str, str]] = []
    # Bath count is property-wide on KRC samples; if multiple detail blocks
    # exist with different baths, prefer the first match (we lack a stable
    # unit→detail link without parsing the unit-info anchor).
    bath_match = _BATH_RE.search(html)
    bath_str = bath_match.group(1) if bath_match else ""

    for row_match in _ROW_RE.finditer(html):
        date_attr = row_match.group(1) or ""
        row_inner = row_match.group(2) or ""

        unit_number = _text(_first_cell(_CELL_PATTERNS["unit_number"], row_inner))
        bed_text = _text(_first_cell(_CELL_PATTERNS["unit_beds"], row_inner))
        rent_text = _text(_first_cell(_CELL_PATTERNS["unit_rent"], row_inner))
        sqft_text = _text(_first_cell(_CELL_PATTERNS["unit_sqft"], row_inner))
        date_text = _text(_first_cell(_CELL_PATTERNS["unit_date"], row_inner)) or date_attr

        # Quality gate: must have rent + (unit_number OR sqft). Drops rows
        # that are headers, JS placeholders ("${UnitDate}"), or empty.
        rent_int = money_to_int(rent_text)
        if rent_int is None:
            continue
        if not unit_number and not sqft_text:
            continue
        if "${" in unit_number or "${" in rent_text:
            # Unrendered template literal — skip.
            continue

        beds_clean = re.sub(r"[^0-9]", "", bed_text) or ""
        sqft_clean = re.sub(r"[^0-9]", "", sqft_text) or ""

        availability_date = (
            date_text if date_text and date_text.lower() not in {"now", "available"} else ""
        )
        availability_status = (
            "AVAILABLE"
            if not date_text or date_text.lower() in {"now", "available"} or rent_int > 0
            else "UNKNOWN"
        )

        units.append(
            make_unit_dict(
                floor_plan_name="",
                bed_label=bed_label_from(int(beds_clean) if beds_clean.isdigit() else None),
                bedrooms=beds_clean,
                bathrooms=bath_str,
                sqft=sqft_clean,
                unit_number=unit_number,
                rent_range=f"${rent_int:,}",
                rent_low=rent_int,
                rent_high=rent_int,
                availability_status=availability_status,
                available_units="1",
                availability_date=availability_date,
                extraction_tier=_TIER_BASE,
            )
        )

    return units


def _first_cell(pattern: re.Pattern[str], html: str) -> str:
    m = pattern.search(html)
    return m.group(1) if m else ""


def _ctx_html(ctx: AdapterContext) -> str | None:
    """Pull raw HTML out of ``ctx.fetch_result.body`` (bytes or str)."""
    fr = getattr(ctx, "fetch_result", None)
    if fr is None:
        return None
    body = getattr(fr, "body", None)
    if body is None:
        return None
    if isinstance(body, bytes):
        try:
            return body.decode("utf-8", errors="replace")
        except Exception:
            return None
    if isinstance(body, str):
        return body
    return None


class RentManagerAdapter:
    """Rent Manager PMS adapter. Parses #availabilitytable DOM rows."""

    pms_name: str = "rentmanager"
    _fingerprints: list[str] = ["rentmanager.com/websites", "unit_avail_container"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from Rent Manager `<tr class='unit_avail_container'>` rows.

        Two HTML sources are tried in order: the live Playwright page (post-
        render — the rows are real DOM elements) and the raw fetch body
        (pre-render — the rows are inside a ``jQuery(...).append(\\`...\\`)``
        template). The same regex matches both because the row markup is
        identical; only the wrapping context differs.
        """
        result = AdapterResult(tier_used=_TIER_BASE)

        html: str | None = None
        if page is not None and hasattr(page, "content"):
            try:
                content: Any = await page.content()
                if isinstance(content, str) and content:
                    html = content
            except Exception:
                pass
        if html is None:
            html = _ctx_html(ctx)

        if not html:
            result.tier_used = _TIER_NO_HTML
            result.errors.append(
                "RENTMANAGER_NO_HTML: neither page.content() nor "
                "ctx.fetch_result.body produced usable HTML"
            )
            return result

        try:
            units = parse_rentmanager_html(html)
        except Exception as exc:
            result.errors.append(f"RENTMANAGER_PARSE_EXC: {type(exc).__name__}: {exc}")
            return result

        if not units:
            result.tier_used = _TIER_NO_ROWS
            result.errors.append(
                "RENTMANAGER_NO_ROWS: no <tr class='unit_avail_container'> rows "
                "matched in the rendered HTML — page may not be a Rent Manager "
                "property-detail page, or the data hasn't rendered yet"
            )
            return result

        result.units = units
        result.winning_url = ctx.base_url
        result.confidence = min(0.85, 0.6 + 0.04 * len(units))
        result.tier_used = _TIER_BASE
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
