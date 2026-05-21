"""iLoveLeasing JS-widget availability-table parser — UNIT-LEVEL (DOM).

The RentManager/iLoveLeasing marketing widget (NOT the server-side
``ua.rentmanager.com/Search_Result`` API — that is handled by
rentmanager.py) injects, client-side, a clean availability table into
the property detail page:

  <table class="unit-avail-table"> ... <tbody id="availabilitytable">
    <tr class="unit_avail_container" data-date="10/1/2025">
      <td class="unit-number">303</td><td class="unit-beds">2</td>
      <td class="unit-rent">$1,325.00</td><td class="unit-sqft">815</td>
      <td class="unit-date">Now</td>
      <td class="unit-info"><a data-featherlight="#15090-detail">…</a></td>
      …</tr> …

Verified 2026-05-18 against the real rendered DOM of
krcapartments.com/property-detail/wedgewood-place/ (5 units; e.g.
unit 303, 2bd, $1,325.00, 815 sqft, available Now). main's generic
extract_units_from_dom returns 0 on this structure (confirmed) — hence
this dedicated parser. Render-dependent: the table is JS-injected, so a
static fetch misses it; the caller must pass already-rendered HTML.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from ma_poc.pms.adapters._parsing import make_unit_dict

_TIER = "TIER_1_DOM_RENTMANAGER_ILOVELEASING"
_MONEY_RE = re.compile(r"[\d,]+(?:\.\d+)?")
_FEATHER_RE = re.compile(r"#?(\d+)-detail")


def _money(v: str) -> int | None:
    m = _MONEY_RE.search(str(v or ""))
    if not m:
        return None
    try:
        return int(round(float(m.group(0).replace(",", ""))))
    except (TypeError, ValueError):
        return None


def _iso(s: str) -> str:
    """``8/1/2026`` | ``2026-08-01`` → ``2026-08-01``. 'Now'/'' → ''."""
    s = str(s or "").strip()
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.match(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def _txt(row: Any, cls: str) -> str:
    el = row.find("td", class_=cls)
    return str(el.get_text(strip=True)) if el else ""


def parse_iloveleasing_table(html: str, source_url: str) -> list[dict[str, Any]]:
    """Rendered iLoveLeasing ``unit-avail-table`` HTML → unit-level dicts.

    One ``tr.unit_avail_container`` = one unit. Dedup by the stable
    ``data-featherlight`` detail id when present, else unit number.
    Returns [] when the table/rows are absent (caller falls through).
    """
    if not html or "unit_avail_container" not in html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr", class_="unit_avail_container")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        unum = _txt(row, "unit-number")
        rent = _money(_txt(row, "unit-rent"))
        if not unum or rent is None:
            continue
        # Stable id from the "View Details" featherlight target, if any.
        uid = ""
        info = row.find("td", class_="unit-info")
        if info:
            a = info.find("a")
            fl = a.get("data-featherlight", "") if a else ""
            fm = _FEATHER_RE.search(str(fl))
            if fm:
                uid = fm.group(1)
        key = uid or unum
        if key in seen:
            continue
        seen.add(key)

        date_txt = _txt(row, "unit-date")  # "Now" or "8/1/2026"
        avail_iso = _iso(date_txt) or _iso(str(row.get("data-date") or ""))

        out.append(
            make_unit_dict(
                unit_number=unum,
                bedrooms=_txt(row, "unit-beds"),
                sqft=_txt(row, "unit-sqft"),
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                availability_date=avail_iso,
                source_api_url=source_url,
                extraction_tier=_TIER,
            )
        )
    return out
