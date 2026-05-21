"""Nestio contact-widget DOM adapter — rendered-DOM unit extractor.

A subset of Funnel/Nestio customers (Dermot Company; likely some
non-Essex customers) DON'T have a server-side proxy at
``{site}.com/api/properties/{id}/availability`` (see ``_funnel.py``
for that path). Instead they embed the Nestio contact widget
(``integrations.nestio.com/contact-widget/v1/integration.js``)
which renders the unit-detail DOM client-side from a 503-prone
direct call to ``nestiolistings.com/api/v2/communities/...``.

curl_cffi against the API direct: 503. curl_cffi against the HTML page:
returns an empty React shell with no unit data. **A real browser
(Playwright/patchright) renders the widget into a deterministic DOM
template** — verified live against Dermot's 101 West End Avenue #11C:

  | DOM id                  | Value (live 2026-05-21)                       |
  |-------------------------|-----------------------------------------------|
  | ``apt-number-02``       | ``11C``                                       |
  | ``apt-title-02``        | ``101 West End Avenue``                       |
  | ``apt-address``         | ``101 West End Avenue, ..., NY, 10023``       |
  | ``apt-value-bedroom``   | ``-`` (= studio)                              |
  | ``apt-value-bathroom``  | ``1``                                         |
  | ``apt-value-sf``        | ``561``                                       |
  | ``apt-price``           | ``$ 4,212``                                   |
  | ``apt-fee``             | ``No Fee``                                    |
  | ``apt-date-available``  | ``05 / 05 / 2026``                            |
  | ``apt-description``     | ``* Listed rent is net effective...``         |

Because the renderer is a SHARED JS bundle (every Nestio-widget customer
loads the SAME ``integrations.nestio.com`` script), this DOM template is
universal across customers. Only the values differ per unit.

Detection markers (any of these in HTML):
  • ``integrations.nestio.com/contact-widget`` script src
  • ``window.NestioLeadCapture`` global
  • A combination of any ``apt-price`` + ``apt-value-bedroom`` IDs

This adapter takes the **rendered** page HTML — i.e. the DOM AFTER the
contact widget's JS has executed. The orchestrator must pass a
Playwright-rendered body, not the L1 curl_cffi shell.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

# ── Detector ────────────────────────────────────────────────────────────────


_RENDERED_MARKERS: tuple[str, ...] = (
    # The rendered-DOM markers — exclusive to the widget post-JS:
    'id="apt-price"',
    'id="apt-value-bedroom"',
    'id="apt-date-available"',
)
_WIDGET_LOAD_MARKERS: tuple[str, ...] = (
    # Markers that indicate the widget WILL render but may not have yet
    # (visible on the L1 shell too — used by ``detect_widget_will_render``):
    "integrations.nestio.com/contact-widget",
    "NestioLeadCapture",
    "nestiostatic.com",
    "funnelstatic.com",
)


def detect_widget_rendered(html: str) -> bool:
    """True if the HTML is the WIDGET-RENDERED DOM (post-Playwright).

    Requires at least 2 of the rendered-DOM ID markers. Single-marker
    matches are likely a non-Nestio page with one of these IDs by
    accident; pairing them shrinks false positives to ~zero.
    """
    if not html:
        return False
    hits = sum(1 for m in _RENDERED_MARKERS if m in html)
    return hits >= 2


def detect_widget_will_render(html: str) -> bool:
    """True if the HTML looks like a Nestio-widget shell (pre-render).

    Used by the orchestrator to decide whether to escalate the L1 curl
    shell to a Playwright render. Distinct from ``detect_widget_rendered``
    — the SHELL has the script + global but no rendered IDs yet.
    """
    if not html:
        return False
    return any(m in html for m in _WIDGET_LOAD_MARKERS)


# ── Parser ──────────────────────────────────────────────────────────────────


def _text_or_empty(soup: Any, css_id: str) -> str:
    el = soup.find(id=css_id)
    return el.get_text(strip=True) if el is not None else ""


_RENT_RE = re.compile(r"\$\s*([1-9]\d{0,3}(?:,\d{3})*|\d{3,5})")
_INT_RE = re.compile(r"(\d+(?:\.\d+)?)")
_ISO_DATE_PARTS_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4})")


def _parse_rent(s: str) -> int | None:
    if not s:
        return None
    m = _RENT_RE.search(s)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _parse_bedrooms(s: str) -> str:
    """Bedrooms in the widget render as the integer when ≥1 and as a
    dash ``-`` when studio. Normalise to ``"0"`` / ``"1"`` / ... so the
    downstream merger treats it like every other adapter's bedrooms."""
    if not s:
        return ""
    s = s.strip()
    if s in ("-", "—", "–", "Studio", "studio", "STUDIO"):
        return "0"
    m = _INT_RE.search(s)
    return m.group(1) if m else ""


def _parse_iso_date(s: str) -> str:
    """Widget renders dates as ``MM / DD / YYYY`` (with spaces). Convert
    to ``YYYY-MM-DD`` to match the rest of the adapter family."""
    if not s:
        return ""
    m = _ISO_DATE_PARTS_RE.search(s)
    if not m:
        return ""
    mm, dd, yyyy = m.groups()
    return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"


def parse_widget_dom(html: str, source_url: str = "") -> list[dict[str, Any]]:
    """Extract the unit record from a Nestio contact-widget rendered DOM.

    The widget renders ONE unit per page (the URL includes ``?id={unit_id}
    &team_id={building_team}``). So this returns either a 0- or 1-record
    list.

    Returns empty list when the detector doesn't pass — the rendered
    DOM markers must be present, otherwise we're looking at the L1
    shell (no data yet) or an unrelated page.
    """
    if not html:
        return []
    if not detect_widget_rendered(html):
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    unit_number = _text_or_empty(soup, "apt-number-02")
    building = _text_or_empty(soup, "apt-title-02")
    address = _text_or_empty(soup, "apt-address")
    bedrooms = _parse_bedrooms(_text_or_empty(soup, "apt-value-bedroom"))
    bathrooms = _text_or_empty(soup, "apt-value-bathroom")
    # bathrooms occasionally shows like "1.5" — preserve as-is (string).
    if bathrooms and not _INT_RE.match(bathrooms):
        bathrooms = ""
    sqft_raw = _text_or_empty(soup, "apt-value-sf")
    sqft_m = _INT_RE.search(sqft_raw)
    sqft = sqft_m.group(1) if sqft_m else ""

    rent = _parse_rent(_text_or_empty(soup, "apt-price"))
    rent_range = f"${rent:,}" if rent is not None else ""

    fee_text = _text_or_empty(soup, "apt-fee")  # "No Fee" / "$X Fee" / etc.

    avail_iso = _parse_iso_date(_text_or_empty(soup, "apt-date-available"))
    avail_status = "AVAILABLE" if (avail_iso or rent) else ""

    concession = _text_or_empty(soup, "apt-description")
    # The description line is the concession blurb on Nestio widgets —
    # "* Listed rent is net effective rent, based on a gross rent of
    # $4395.00 and 0.5 Months Free". Pass through verbatim; downstream
    # concession-parsing extracts the months-free + gross-rent.

    # Drop entirely-empty records (defensive — shouldn't happen given
    # the detector gate but keeps the contract clean).
    if not unit_number and not rent and not sqft:
        return []

    return [{
        "floor_plan_name": "",  # widget doesn't render a separate plan name
        "bed_label": "",
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "sqft": sqft,
        "unit_number": unit_number,
        "floor": "",
        "building": building,
        "rent_range": rent_range,
        "market_rent_low": rent,
        "market_rent_high": rent,
        "deposit": "",
        "concession": concession,
        "availability_status": avail_status,
        "available_units": "1" if avail_status else "",
        "availability_date": avail_iso,
        "lease_term": "",
        "move_in_date": "",
        "source_api_url": source_url,
        "source": "nestio_widget_dom",
        "fee_label": fee_text,
        "address": address,
    }]


__all__ = [
    "detect_widget_rendered",
    "detect_widget_will_render",
    "parse_widget_dom",
]
