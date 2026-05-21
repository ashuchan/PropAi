"""Phase 6.7 (2026-05-21) — RealPage Leasing iframe widget adapter.

The 12-property ``needs_chrome_probe`` cluster from the HAR-replay
worklist all share a common shape:

  • Property page embeds ``<div class="realpage widget">{"realpageId":NNNN}</div>``
  • Widget JS renders a SAME-ORIGIN iframe at
    ``<property>.com/.../#!/oll/search-floorplan``
  • The iframe DOM contains floor-plan cards with this text layout:
        Floor Plan Name (heading)
        N Bed | N Bath | NNN sq ft
        $NNNN*  (or "Call for Pricing")
        (N) Available

curl_cffi / HAR-replay can't reach the data because the widget
authenticates against ``leasing.realpage.com`` with per-property
tokens minted at widget-load time. We do not attempt to re-mint
those tokens — replaying them would require browser execution
anyway, and the rendered iframe DOM is already deterministic.

Architecture:
  • ``detect_realpage_leasing_widget(html)`` — synchronous, HTML-only:
    returns the ``realpageId`` string if the widget marker is present.
  • ``parse_realpage_leasing_iframe_dom(iframe_html, source_url)`` —
    synchronous, HTML-only: parses captured iframe DOM into unit dicts.
  • The Playwright orchestration that fetches the iframe DOM lives in
    ``generic.py`` so this module stays browser-free and testable.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

# Detector — matches ``<div class="realpage widget">{"realpageId":NNNN}</div>``
# and variations where the JSON appears as element text content. The class
# token order varies (``widget realpage`` is the most common). We anchor on
# the literal ``realpage`` + ``widget`` class tokens being present together.

_REALPAGE_ID_RE = re.compile(r'"realpageId"\s*:\s*"?(\d+)"?')


def detect_realpage_leasing_widget(html: str) -> str | None:
    """Return the RealPage Leasing widget ``realpageId`` if found, else None.

    Looks for a ``<div>`` carrying both ``realpage`` and ``widget`` as class
    tokens and a JSON payload containing ``realpageId``. The ID is the
    sole input required to identify which property's iframe to load.
    """
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    for div in soup.find_all("div"):
        classes = div.get("class") or []
        if not classes:
            continue
        # ``class`` may come back as list or as a single string
        if isinstance(classes, str):
            class_set = set(classes.split())
        else:
            class_set = set(classes)
        if not ({"realpage", "widget"} <= class_set):
            continue
        # 1) Inline JSON payload — the canonical shape.
        text = div.string or div.get_text() or ""
        if not text:
            # Some sites stash the config in a child element
            text = " ".join(c.get_text() for c in div.find_all(True))
        if text:
            m = _REALPAGE_ID_RE.search(text)
            if m:
                return m.group(1)
        # 2) Data-attribute carrier — runs regardless of text presence.
        for attr in ("data-realpage-id", "data-realpageid"):
            v = div.get(attr)
            if v and re.fullmatch(r"\d+", str(v).strip()):
                return str(v).strip()
    return None


# Iframe-DOM parser — operates on already-rendered iframe HTML

_CARD_BED_BATH_SQFT_RE = re.compile(
    r"(\d+|studio)\s*bed[s]?\s*[|•/·]\s*"
    r"(\d+(?:\.\d+)?)\s*bath[s]?\s*[|•/·]\s*"
    # sqft accepts 2-5 digit raw OR comma-grouped (``1,240``)
    r"((?:\d{1,3}(?:,\d{3})+)|\d{2,5})\s*sq\.?\s*ft\.?",
    re.IGNORECASE,
)
_CARD_RENT_RE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})*|\d{3,5})(?:\.\d{2})?\*?", re.IGNORECASE)
_CARD_AVAILABLE_RE = re.compile(r"\(\s*(\d+)\s*\)\s*Available", re.IGNORECASE)


def _parse_one_card(card_text: str, source_url: str) -> dict[str, Any] | None:
    """Parse the text contents of a single floor-plan card.

    Expected line shape:
        <name>
        N Bed | N Bath | NNN sq ft
        $NNNN*
        (N) Available

    Tolerates whitespace normalisation, missing rent (renders as "Call
    for Pricing" — sets availability_status and leaves rent blank), and
    cards that omit the available-count line.
    """
    # Normalise: drop blank lines, strip each line
    lines = [ln.strip() for ln in card_text.splitlines() if ln.strip()]
    if not lines:
        return None
    name = lines[0]
    # Find the bed|bath|sqft line — may not be line 1, e.g. some cards
    # have a price tag above it.
    bbs = None
    for ln in lines:
        m = _CARD_BED_BATH_SQFT_RE.search(ln)
        if m:
            bbs = m
            break
    if not bbs:
        return None
    beds_raw = bbs.group(1)
    bedrooms = "0" if beds_raw.lower() == "studio" else beds_raw
    bathrooms = bbs.group(2)
    sqft = bbs.group(3).replace(",", "")

    # Rent — may not be present
    rent_lo: int | None = None
    rent_range = ""
    avail_status = ""
    for ln in lines:
        rm = _CARD_RENT_RE.search(ln)
        if rm:
            rent_lo = int(rm.group(1).replace(",", ""))
            rent_range = f"${rent_lo:,}"
            break
    if rent_lo is None and any(
        "call for pricing" in ln.lower() for ln in lines
    ):
        avail_status = "call for pricing"

    # Availability count
    avail_count = ""
    for ln in lines:
        am = _CARD_AVAILABLE_RE.search(ln)
        if am:
            avail_count = am.group(1)
            break

    return {
        "floor_plan_name": name,
        "bed_label": "",
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "sqft": sqft,
        "unit_number": "",
        "floor": "",
        "building": "",
        "rent_range": rent_range,
        "market_rent_low": rent_lo,
        "market_rent_high": rent_lo,
        "deposit": "",
        "concession": "",
        "availability_status": avail_status,
        "available_units": avail_count,
        "availability_date": "",
        "lease_term": "",
        "move_in_date": "",
        "source_api_url": source_url,
        "source": "realpage_leasing_widget",
    }


# Card-container selectors observed during Chrome probing. Listed in order
# of confidence — the orchestrator tries each in turn. Centralising here
# keeps the selector list discoverable and easy to extend as more
# properties are probed.
_CARD_SELECTORS: tuple[str, ...] = (
    '[role="article"]',
    ".floorplan-card",
    ".rp-floorplan-card",
    ".floor-plan-card",
    ".oll-card",
)


def parse_realpage_leasing_iframe_dom(
    iframe_html: str, source_url: str = ""
) -> list[dict[str, Any]]:
    """Parse captured iframe inner-HTML into unit dicts.

    Tries each known card selector in turn. The first selector that
    matches ≥1 card wins — the orchestrator never falls back across
    selectors because that's how we'd get duplicated units.
    """
    if not iframe_html:
        return []
    try:
        soup = BeautifulSoup(iframe_html, "lxml")
    except Exception:
        soup = BeautifulSoup(iframe_html, "html.parser")

    cards: list[Any] = []
    for sel in _CARD_SELECTORS:
        try:
            found = soup.select(sel)
        except Exception:
            continue
        if found:
            cards = found
            break

    if not cards:
        return []

    out: list[dict[str, Any]] = []
    for card in cards:
        text = card.get_text("\n", strip=True)
        unit = _parse_one_card(text, source_url)
        if unit:
            out.append(unit)
    return out


def iframe_url_for_realpage_id(base_url: str, realpage_id: str) -> str:
    """Construct the same-origin iframe URL.

    The widget JS routes ``/#!/oll/search-floorplan`` (hash-based SPA
    route inside the iframe). The orchestrator navigates the iframe via
    Playwright; this helper centralises the URL shape so adapters and
    tests agree on it.
    """
    if not base_url.endswith("/"):
        base_url = base_url + "/"
    return f"{base_url}#!/oll/search-floorplan?id={realpage_id}"


__all__ = [
    "detect_realpage_leasing_widget",
    "iframe_url_for_realpage_id",
    "parse_realpage_leasing_iframe_dom",
]
