"""RentCafe-HOSTED portal unit-table parser — UNIT-LEVEL (DOM).

The ``rentcafe.com/apartments/<state>/<city>/<slug>/default.aspx`` hosted
ILS portal is server-fetchable (HTTP 200, NO bot-wall, NO login —
unlike the vanity domains which 403) and SSRs every unit as a clean
data-attributed row:

  <table id="floorplanUnits<fpId>">
    <tr class="fp-unit" data-unit-id="6423427"
        data-unit-name="6869-1A " data-unit-beds="1 Bed"
        data-unit-baths="1 Bath" data-unit-size="716 Sqft"
        data-unit-rent="$1,357 - $1,656">
      <th>6869-1A </th><td>$1,357 - $1,656</td>
      <td><span aria-label="Available on Aug 3">Aug 3</span></td> …

Verified 2026-05-18 against the real rendered DOM of
rentcafe.com/.../tall-oaks-apartment-homes/default.aspx (fixture
rentcafe_hosted_units.html): unit 6869-1A 1Bed 716sqft $1,357-$1,656
avail Aug 3; 6714-2B $1,392-$1,717 Sep 7. main's generic
extract_units_from_dom returns 0 on this (doesn't look for
tr.fp-unit[data-*]) — hence this dedicated parser. Render-dependent
(units inject client-side / behind "Show more units"); the caller must
pass already-rendered HTML.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from bs4 import BeautifulSoup

from ma_poc.pms.adapters._parsing import make_unit_dict

_TIER = "TIER_1_DOM_RENTCAFE_HOSTED"
_MONEY_RE = re.compile(r"[\d,]+(?:\.\d+)?")
# Live markup serializes the query separator either as ``&`` or the literal
# JavaScript escape ``\u0026``.  The latter ends in a word character, so a
# leading ``\b`` before ``myOlePropertyId`` incorrectly drops provenance.
_PROPERTY_ID_RE = re.compile(r"myOlePropertyId\s*=\s*(\d+)", re.IGNORECASE)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _money(v: str) -> int | None:
    m = _MONEY_RE.search(str(v or ""))
    if not m:
        return None
    try:
        return int(round(float(m.group(0).replace(",", ""))))
    except (TypeError, ValueError):
        return None


def _rent_range(v: str) -> tuple[int | None, int | None]:
    """``$1,357 - $1,656`` → (1357, 1656); ``$1,357`` → (1357, 1357)."""
    nums = [
        int(round(float(x.replace(",", ""))))
        for x in re.findall(r"[\d,]+(?:\.\d+)?", str(v or ""))
    ]
    if not nums:
        return None, None
    return min(nums), max(nums)


def _iso(s: str, today: date | None = None) -> str:
    """``Aug 3`` | ``Available on Sep 7`` | ``8/3/2026`` → ISO.

    Month-name forms carry no year — infer the next occurrence on/after
    today (RentCafe only lists current/future availability). 'Now' → ''.
    """
    s = str(s or "").strip()
    td = today or date.today()
    m = re.search(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})\b", s)
    if m and m.group(1).lower() in _MONTHS:
        mon = _MONTHS[m.group(1).lower()]
        day = int(m.group(2))
        yr = td.year
        try:
            if date(yr, mon, day) < td:
                yr += 1
            return f"{yr:04d}-{mon:02d}-{day:02d}"
        except ValueError:
            return ""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.match(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def _digits(v: str) -> str:
    m = re.search(r"\d[\d,]*", str(v or ""))
    return m.group(0).replace(",", "") if m else ""


def _is_waitlist_sentinel(v: str) -> bool:
    """Reject RentCafe's priced waitlist placeholders as non-physical units.

    Live Coopers Landing evidence (property 480033, 2026-08-01) exposes
    labels such as ``WAIT1BD`` and ``WAITAPP`` as ``tr.fp-unit`` rows with
    stable-looking IDs and rents.  They are application/waitlist inventory,
    not apartment numbers, so admitting them would manufacture unit-level
    success.  RentCafe removes punctuation inconsistently; compare a compact
    form and fail closed for every WAIT-prefixed label.
    """
    compact = re.sub(r"[\s_-]+", "", str(v or "")).casefold()
    return compact.startswith("wait")


def parse_rentcafe_hosted_table(html: str, source_url: str) -> list[dict[str, Any]]:
    """Rendered RentCafe-hosted ``tr.fp-unit`` rows → unit-level dicts.

    All fields come from the row's ``data-unit-*`` attributes (robust);
    availability from the ``aria-label="Available on …"`` span or the
    cell text. Dedup by ``data-unit-id`` (stable) else unit name.
    Returns [] when no fp-unit rows are present (caller falls through).
    """
    if not html or "fp-unit" not in html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr", class_="fp-unit")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        unum = str(row.get("data-unit-name") or "").strip()
        rlo, rhi = _rent_range(str(row.get("data-unit-rent") or ""))
        if not unum or _is_waitlist_sentinel(unum) or rlo is None:
            continue
        uid = str(row.get("data-unit-id") or "").strip()
        key = uid or unum
        if key in seen:
            continue
        seen.add(key)

        avail = ""
        sp = row.select_one("span[aria-label]")
        if sp and sp.get("aria-label"):
            avail = _iso(str(sp.get("aria-label")))
        if not avail:
            tds = row.find_all("td")
            if len(tds) > 1:
                avail = _iso(tds[1].get_text(" ", strip=True))

        floor_plan = row.find_parent(attrs={"data-floorplan-id": True})
        floor_plan_id = ""
        floor_plan_name = ""
        if floor_plan is not None:
            floor_plan_id = str(floor_plan.get("data-floorplan-id") or "").strip()
            floor_plan_name = str(floor_plan.get("data-name") or "").strip()
            if not floor_plan_name:
                name_node = floor_plan.select_one(".fp-name")
                if name_node is not None:
                    floor_plan_name = name_node.get_text(" ", strip=True)

        source_ids: dict[str, str] = {}
        if uid:
            # RentCafe/SecureCafe uses this native apartment identifier in
            # the application URL; it is already registered UNIT_STABLE.
            source_ids["securecafe_apartment_id"] = uid
        if floor_plan_id:
            source_ids["rentcafe_floorplan_id"] = floor_plan_id

        unit = make_unit_dict(
            floor_plan_name=floor_plan_name,
            unit_number=unum,
            bedrooms=_digits(str(row.get("data-unit-beds") or "")),
            bathrooms=_digits(str(row.get("data-unit-baths") or "")),
            sqft=_digits(str(row.get("data-unit-size") or "")),
            rent_low=rlo,
            rent_high=rhi,
            availability_status="AVAILABLE",
            availability_date=avail,
            source_api_url=source_url,
            extraction_tier=_TIER,
            source_ids=source_ids,
        )
        property_id = _PROPERTY_ID_RE.search(str(row))
        if property_id:
            unit["source_property_id"] = property_id.group(1)
        out.append(unit)
    return out
