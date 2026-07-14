"""Per-plan unit-row parser for the "encoreskyline-template" family.

2026-05-19 deep-probe finding: a family of marketing-template sites
(visual signature ``Skip to main content`` / ``Book a Tour`` / per-plan
URLs at ``/floorplans/{slug}/``) renders **real apartment-level rows
inline** on each per-plan page — but only after a per-plan
``Check Availability`` JavaScript click. The underlying data widget is
`Jonah Digital / MeetElise` (``JonahWidget.meetelise({organization, building})``
initialiser in the page); the click triggers the widget to fetch unit
inventory from the Jonah data layer and insert it as DOM rows.

Verified live (2026-05-19) on encoreskyline.com (#B-302 Floor 3 …),
geneseepointe.com (#308 / #209 / #410 / #310 Floor 1 …), and
highlineaustin.com (#2206 / #1306 / #7108 …). jugnu's TIER_3_DOM cascade
scraped the floorplan-card list and never fired the per-plan toggle, so
these properties returned 0% real ``unit_id`` despite the data being one
JS click deep.

Inline row shape (rendered text, after the click) is uniform across the
verified sites — modulo optional ``Floor <N>``, optional ``$<deposit>
Deposit``, optional ``Lease Now`` button text, and optional ``Starting
at`` rent prefix::

    #<unit>  [Floor <N>]  <sqft> sq. ft.  [Starting at] $<rent>
       [$<deposit> Deposit]  Available <date>   [Lease Now]

This module **only** parses that rendered text into unit dicts. It does
NOT navigate per-plan URLs or fire the click — that is the recovery
caller's job (a separate wiring change in the cascade / a marketing-template
adapter that owns the page-interaction lifecycle). Splitting the
mechanical parser from the page-interaction recovery keeps this file
trivially unit-testable (no Playwright dependency).

NOTE — RealPage-Online-Leasing variant: the same visual template
sometimes wraps a RealPage onlineleasing portal (see
``rent-portofino.com``) instead of the Jonah widget; in that case the
``_pms_portal_hop`` recovery is the correct path, not this one. The
detection signal that selects this parser is the **Jonah Digital marker**
(``JonahWidget|meetelise|jonahdigital`` token in the page HTML).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ma_poc.pms.adapters._parsing import make_unit_dict

log = logging.getLogger(__name__)

_TIER = "TIER_1_DOM_ENCORESKYLINE_TEMPLATE"

# Detection markers — set on every encoreskyline-family page by the
# Jonah Digital data layer at load. Stable across the verified sites.
_JONAH_MARKERS = (
    "jonahwidget",
    "jonahdigital",
    "meetelise",
)


def is_encoreskyline_template_page(html: str) -> bool:
    """Cheap detector — does this page embed the Jonah Digital widget?

    Used by the recovery caller to decide whether to fire the
    per-plan ``Check Availability`` click loop. Strict by design: a stray
    "meetelise" mention in unrelated copy is acceptable (it almost always
    co-occurs with the actual widget on the verified template family).
    """
    if not html:
        return False
    low = html.lower()
    return any(m in low for m in _JONAH_MARKERS)


# Per-unit row regex. Designed for the rendered ``document.body.innerText``
# (after the click). Mostly whitespace-tolerant; uses non-capturing
# alternations for the optional segments so the named groups are stable.
# Examples it matches (all from live 2026-05-19 captures)::
#
#     "#B-302 Floor 3 703 sq. ft. $1,750 $400 Deposit Available Jul 3"
#     "#308 Floor 1 820 sq. ft. Starting at $2,025 Available Now Lease Now"
#     "#2206 668 sq. ft. Starting at $1,284 $150 DepositAvailable May 20"
#     "#7108 668 sq. ft. Starting at $1,364 $150 DepositAvailable Jul 21"
#
# NOT matched (by design): bare floorplan-level "Starting at $X" rows
# without a leading ``#<unit>`` token.
_UNIT_ROW_RE = re.compile(
    r"""
    \#                              # unit-number sigil
    (?P<unit>                       # unit number — eg. B-302, 2206, 308
        [A-Za-z]?\-?\d{1,4}[A-Za-z]?
    )
    \s+
    (?:Floor\s+(?P<floor>\d{1,3})\s+)?       # optional "Floor N"
    (?P<sqft>\d{2,5})\s*sq\.?\s*ft\.?\s*     # "<sqft> sq. ft."
    (?:Starting\s+at\s+)?                    # optional "Starting at"
    \$(?P<rent>[\d,]+)                       # "$<rent>"
    (?:\s*\$(?P<deposit>[\d,]+)\s*Deposit)?  # optional "$<dep> Deposit"
    \s*Available\s+
    (?P<date>                                # "Available <date>"
        Now
        |
        [A-Z][a-z]{2,8}\s+\d{1,2}            # "Jul 3" / "May 20" / "June 16"
        |
        \d{1,2}/\d{1,2}(?:/\d{2,4})?         # "5/20" or "5/20/26"
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _int_or_none(s: str | None) -> int | None:
    if not s:
        return None
    s = s.replace(",", "").strip()
    if not s.isdigit():
        return None
    try:
        return int(s)
    except ValueError:
        return None


_RENTPRESS_ATTR_RE = re.compile(
    r"""data-floorplans\s*=\s*(["'])(?P<json>.*?)\1""", re.DOTALL
)


def _rp_iso(s: str) -> str:
    """``09/10/2026`` (MM/DD/YYYY) | ``2026-09-10`` → ``2026-09-10``; else ''."""
    s = str(s or "").strip()
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def parse_rentpress_data_floorplans(
    html: str, source_url: str
) -> list[dict[str, Any]]:
    """Parse the RentPress WordPress plugin's ``data-floorplans`` attribute.

    2026-07-12: RentPress (RentCafe-synced) sites embed the FULL unit
    inventory as an entity-escaped JSON array in
    ``<div id="rentpress-app" ... data-floorplans='[{...}]'>`` — statically,
    in the initial HTML. Each floorplan carries a ``units`` array of real
    per-unit objects (unit_name, unit_rent_best, unit_bedrooms, unit_sqft,
    unit_available_on). The encoreskyline adapter previously only drove the
    Jonah per-plan click flow and never read this attribute, so these
    RentPress-backed sites fell through to the LLM/failed tier despite
    carrying clean unit-level data in the static page.

    Emits one unit-level row per unit. Returns ``[]`` on absent/unparseable
    markup — never raises. Verified live (curl_cffi, $0): themobilelofts.com
    → 2 units (OSL-D1 $1,589 653sqft 2026-09-10; OSL-... $1,759).
    """
    import html as _html
    import json

    if not html or "data-floorplans" not in html:
        return []
    m = _RENTPRESS_ATTR_RE.search(html)
    if not m:
        return []
    try:
        plans = json.loads(_html.unescape(m.group("json")))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(plans, list):
        return []

    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fp in plans:
        if not isinstance(fp, dict):
            continue
        fp_name = str(fp.get("floorplan_name") or fp.get("floorplan_post_title") or "")
        for u in fp.get("units") or []:
            if not isinstance(u, dict):
                continue
            unit_no = str(
                u.get("unit_name") or u.get("unit_code") or u.get("unit_space_id") or ""
            ).strip()
            if not unit_no or unit_no in seen:
                continue
            seen.add(unit_no)
            rent = _int_or_none(
                str(
                    u.get("unit_rent_best") or u.get("unit_rent_min")
                    or u.get("unit_rent_base") or ""
                )
            )
            _sqft = _int_or_none(
                str(u.get("unit_sqft") or fp.get("floorplan_sqft_min") or "")
            )
            source_ids: dict[str, Any] = {}
            if u.get("unit_code"):
                source_ids["rentpress_unit_code"] = str(u["unit_code"])
            units.append(
                make_unit_dict(
                    floor_plan_name=fp_name,
                    bedrooms=str(u.get("unit_bedrooms") or fp.get("floorplan_bedrooms") or ""),
                    bathrooms=str(u.get("unit_bathrooms") or fp.get("floorplan_bathrooms") or ""),
                    sqft=str(_sqft or ""),
                    unit_number=unit_no,
                    rent_low=rent,
                    rent_high=rent,
                    availability_status=(
                        "AVAILABLE" if str(u.get("unit_available")) in ("1", "True", "true")
                        else "UNAVAILABLE"
                    ),
                    availability_date=_rp_iso(
                        str(u.get("unit_available_on") or u.get("unit_ready_date") or "")
                    ),
                    source_api_url=source_url,
                    extraction_tier="TIER_1_DOM_RENTPRESS",
                    source_ids=source_ids,
                )
            )
    return units


def parse_encoreskyline_units(
    rendered_text: str, source_url: str
) -> list[dict[str, Any]]:
    """Parse rendered ``body.innerText`` into unit-level dicts.

    *rendered_text* MUST come from a per-plan page **after** the
    ``Check Availability`` click has fired and the Jonah widget has
    inserted its rows. Passing the pre-click text yields ``[]``.

    Returns one dict per ``#unit`` row matched. Deduplicates on
    ``unit_number`` (first occurrence wins). Never raises.
    """
    if not rendered_text:
        return []

    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in _UNIT_ROW_RE.finditer(rendered_text):
        unit = (m.group("unit") or "").strip()
        if not unit or unit in seen:
            continue
        seen.add(unit)
        rent = _int_or_none(m.group("rent"))
        units.append(
            make_unit_dict(
                floor_plan_name="",  # filled by caller from per-plan URL/header
                bedrooms="",
                bathrooms="",
                sqft=str(_int_or_none(m.group("sqft")) or ""),
                unit_number=unit,
                floor=(m.group("floor") or "").strip(),
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                availability_date=(m.group("date") or "").strip(),
                source_api_url=source_url,
                extraction_tier=_TIER,
            )
        )
    return units
