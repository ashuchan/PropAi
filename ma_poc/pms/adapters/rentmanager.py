"""RentManager (iLoveLeasing) PMS adapter — UNIT-LEVEL.

Research log (2026-05-18, server-side curl verified)
-----------------------------------------------------
RentManager marketing sites embed the iLoveLeasing widget
(``www.iloveleasing.com/pub/widget/js/luv.js``) but the real unit feed
is the RentManager **Unit-Availability** ``Search_Result`` endpoint —
clean, NO auth, NO bot wall, server-side curl 200 (repli360/securecafe
class, NOT the JS-only iLoveLeasing widget):

  GET https://<eid>.ua.rentmanager.com/Search_Result?command=
      Search_Result&template=<tmpl>&locations=<loc>&maxperpage=<n>

The full URL is typically present verbatim in the property's STATIC
HTML (e.g. highlandapts.com →
``https://high.ua.rentmanager.com/Search_Result?command=Search_Result&
template=highlandUnit&locations=default&maxperpage=99``). ``<eid>`` is
the RentManager corp id; also derivable from ``<eid>.twa.rentmanager
.com`` / ``<eid>.owa.rentmanager.com``.

Response: a sequence of ``document.write("{ ... }")`` chunks, each a
backtick-delimited pseudo-JSON record:
  { `ppropertyname`:`…`, `pid`:`49`, `unitid`:`18302`, `unit`:`2600`,
    `availabilitydateresult`:`5/31/2023`, `marketrent`:`$4,971.00`, … }
(no beds/baths/sqft in this template). 99/page, ``Page X of N``
pagination — we request a large ``maxperpage`` to pull all in one GET.

Verified: high.ua.rentmanager.com → unit 2600 @ $4,971.00.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ma_poc.pms.adapters._iloveleasing_table import parse_iloveleasing_table
from ma_poc.pms.adapters._parsing import make_unit_dict
from ma_poc.pms.adapters._probe import probe_get, probe_post
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.generic import _get_page_html
from ma_poc.pms.detector import has_rentmanager_condor_inventory_signature

if TYPE_CHECKING:
    from playwright.async_api import Page

_TIER = "TIER_1_API_RENTMANAGER"
_CONDOR_TIER = "TIER_1_DOM_RENTMANAGER_CONDOR_CARDS"
_INLINE_UNITCOUNT_TIER = "TIER_1_DOM_RENTMANAGER_INLINE_UNITCOUNT"
_LEASELEADS_CARD_TIER = "TIER_1_DOM_RENTMANAGER_LEASELEADS_CARDS"
_UNITLIST_CARD_TIER = "TIER_1_DOM_RENTMANAGER_UNITLIST_CARDS"
_CONDOR_CARD_SELECTOR = (
    "#availabilityListing "
    ".unit-details[data-unitid][data-name][data-unitfloorplan]"
    "[data-propid][data-bed][data-bath][data-price][data-availability]"
)

# Full Search_Result URL as it appears verbatim in static HTML.
_RM_SEARCH_URL_RE = re.compile(
    r"https?://[a-z0-9-]+\.ua\.rentmanager\.com/Search_Result\?[^\"'<>]+",
    re.IGNORECASE,
)
# Fallback: derive the corp id (eid) from any rentmanager subdomain.
_RM_EID_RE = re.compile(
    r"https?://([a-z0-9-]+)\.(?:ua|twa|owa)\.rentmanager\.com",
    re.IGNORECASE,
)
# 2026-07-11 audit: management landing pages expose only the PropertyListing
# widget URL (e.g. high.ua.rentmanager.com/PropertyListing?template=highlandProp)
# — no verbatim Search_Result. The Unit template is the PropertyListing
# template with the trailing 'Prop' swapped for 'Unit' (highlandProp →
# highlandUnit). This beats the eid-synthesis fallback, which wrongly builds
# '<eid>Unit' (highUnit) when the corp id (high) ≠ template prefix (highland).
_RM_PROPLIST_RE = re.compile(
    r"https?://([a-z0-9-]+\.(?:ua|twa|owa)\.rentmanager\.com)/PropertyListing\?"
    r"[^\s\"'<>]*?template=([A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_RM_MARK_RE = re.compile(
    r"\.ua\.rentmanager\.com|\.twa\.rentmanager\.com|cdn\.rentmanager\.com"
    r"|iloveleasing\.com",
    re.IGNORECASE,
)
# One unit record: a { ... } block containing a backtick `unitid` key.
_RM_REC_RE = re.compile(r"\{[^{}]*?`unitid`[^{}]*?\}")
_RM_FIELD_RE = re.compile(r"`([A-Za-z0-9_]+)`\s*:\s*`([^`]*)`")
_MONEY_RE = re.compile(r"[\d,]+")
_JS_PUSH_RE_TEMPLATE = r"\b{array}\.push\(\s*\{{(?P<body>.*?)\}}\s*\)\s*;"
_JS_FIELD_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_]*)\s*:\s*(['\"])(.*?)\2",
    re.DOTALL,
)
_ILOVELEASING_API = "https://www.iloveleasing.com/pub/wapi/api/"
_ILOVELEASING_SETTINGS_RE = re.compile(
    r"window\.luv_settings\s*=\s*\[(?P<settings>[^]]+)\]",
    re.IGNORECASE | re.DOTALL,
)
_ILOVELEASING_SETTING_VALUE_RE = re.compile(r"['\"]([^'\"]*)['\"]")
_ILOVELEASING_GENERIC_NAME_TOKENS = frozenset(
    {
        "apartment",
        "apartments",
        "community",
        "home",
        "homes",
        "townhome",
        "townhomes",
    }
)
_ILOVELEASING_ADDRESS_NOISE = frozenset(
    {
        "apartment",
        "apartments",
        "avenue",
        "ave",
        "boulevard",
        "blvd",
        "circle",
        "court",
        "drive",
        "east",
        "highway",
        "lane",
        "north",
        "parkway",
        "place",
        "road",
        "south",
        "street",
        "terrace",
        "trail",
        "west",
    }
)


def _rm_money(v: str) -> int | None:
    m = _MONEY_RE.search(str(v or ""))
    if not m:
        return None
    try:
        return int(round(float(m.group(0).replace(",", ""))))
    except (TypeError, ValueError):
        return None


def _rm_iso(s: str) -> str:
    """Normalize common RentManager date forms to ``YYYY-MM-DD``."""
    s = str(s or "").strip()
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.match(r"\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s*$", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def _derive_search_url_from_property_listing(html: str) -> str:
    """Derive the Unit-Availability Search_Result URL from a PropertyListing
    widget URL: same host (forced to the ``ua.*`` API host), Unit template =
    PropertyListing template with a trailing 'Prop' swapped for 'Unit'.
    Returns '' when no PropertyListing template is present."""
    clean = (html or "").replace("&#038;", "&").replace("&amp;", "&")
    m = _RM_PROPLIST_RE.search(clean)
    if not m:
        return ""
    host, tmpl = m.group(1), m.group(2)
    unit_tmpl = (
        re.sub(r"Prop$", "Unit", tmpl)
        if tmpl.lower().endswith("prop")
        else tmpl + "Unit"
    )
    host = re.sub(r"\.(?:twa|owa)\.", ".ua.", host, flags=re.IGNORECASE)
    return (
        f"https://{host}/Search_Result?command=Search_Result"
        f"&template={unit_tmpl}&locations=default&maxperpage=9999"
    )


def find_rentmanager_search_url(html: str) -> str:
    """Return a ready-to-call ``ua.rentmanager.com/Search_Result`` URL.

    Prefer the verbatim URL in the static HTML (carries the right
    ``template``/``locations``). Bump ``maxperpage`` to pull every unit
    in one GET (avoids the 99/page pagination). Falls back to deriving the
    Unit template from a PropertyListing widget URL (management landing
    pages). '' when not RentManager.
    """
    m = _RM_SEARCH_URL_RE.search(html or "")
    if not m:
        # 2026-07-11 audit: PropertyListing-only landing pages carry no
        # verbatim Search_Result URL — derive it from the PropertyListing
        # template (highlandProp → highlandUnit) before giving up.
        return _derive_search_url_from_property_listing(html)
    url = (
        m.group(0)
        .replace("&#038;", "&")
        .replace("&amp;", "&")
        .rstrip("\\\"' ")
    )
    if re.search(r"[?&]maxperpage=\d+", url, re.IGNORECASE):
        url = re.sub(r"([?&]maxperpage=)\d+", r"\g<1>9999", url, flags=re.IGNORECASE)
    else:
        url += ("&" if "?" in url else "?") + "maxperpage=9999"
    return url


def parse_rentmanager_search(text: str, url: str) -> list[dict[str, Any]]:
    """Parse the RentManager Search_Result body → unit-level dicts.

    Each ``{ `key`:`val`, … }`` record with a ``unitid`` is one unit:
    ``unit`` = unit number, ``marketrent`` = rent, ``availabilitydate
    result`` = availability date. Deduped by unitid.
    """
    if not text or "`unitid`" not in text:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rm in _RM_REC_RE.finditer(text):
        rec = {
            k.lower(): v.strip()
            for k, v in _RM_FIELD_RE.findall(rm.group(0))
        }
        uid = rec.get("unitid", "").strip()
        unum = rec.get("unit", "").strip() or uid
        key = uid or unum
        if not key or key in seen:
            continue
        seen.add(key)
        rent = _rm_money(rec.get("marketrent", ""))
        out.append(
            make_unit_dict(
                unit_number=unum,
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                availability_date=_rm_iso(rec.get("availabilitydateresult", "")),
                source_api_url=url,
                extraction_tier=_TIER,
            )
        )
    return out


def _rentmanager_scope_key(value: Any) -> str:
    """Punctuation-insensitive but otherwise exact public property label."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _rentmanager_street_number(value: Any) -> str:
    match = re.match(r"\s*(\d+[a-z]?)\b", str(value or ""), re.IGNORECASE)
    return match.group(1).casefold() if match else ""


def parse_rentmanager_unitlist_cards(
    html: str,
    source_url: str,
    *,
    property_name: str,
    address: str,
    zip_code: str,
) -> list[dict[str, Any]]:
    """Parse RentManager ``unitList`` cards with a whole-response boundary.

    The Lewiston-family public template renders one ``.list-item.unit`` per
    apartment.  Its two title nodes are the property label and unit number;
    rent and dimensions are row-local.  The Findigs application URL is *not*
    identity: live probes on three sibling properties showed the same UUID on
    every card, so it is deliberately ignored.

    A response is admitted only when every card agrees with the CSV property
    name, street number, and ZIP.  This is intentionally stricter than the
    server's ``propertynameeq`` query because public vendor filters can leak a
    sibling property's rows.
    """
    if (
        not html
        or "list-item unit" not in html
        or ".ua.rentmanager.com/Search_Result" not in html
    ):
        return []
    expected_name = _rentmanager_scope_key(property_name)
    expected_street = _rentmanager_street_number(address)
    expected_zip = re.sub(r"\D", "", str(zip_code or ""))[:5]
    if not expected_name or not expected_street or len(expected_zip) != 5:
        return []

    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return []
    cards = soup.select(".list-item.unit")
    if not cards:
        return []

    output: list[dict[str, Any]] = []
    seen_units: set[str] = set()
    for card in cards:
        titles = card.select(".list-details.unit h2.interior-item-title")
        if len(titles) < 2:
            return []
        card_property = titles[0].get_text(" ", strip=True)
        unit_number = titles[1].get_text(" ", strip=True)
        address_text = " ".join(
            node.get_text(" ", strip=True)
            for node in card.select(".address-wrapper .list-csz")
        )
        if (
            _rentmanager_scope_key(card_property) != expected_name
            or _rentmanager_street_number(address_text) != expected_street
            or expected_zip not in re.sub(r"\D", "", address_text)
        ):
            return []

        unit_key = unit_number.casefold()
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .#/-]{0,30}", unit_number)
            or not any(char.isdigit() for char in unit_number)
            or unit_key in seen_units
        ):
            return []
        card_text = card.get_text(" ", strip=True)
        card_text_lower = card_text.casefold()
        if "not available" in card_text_lower or "unavailable" in card_text_lower:
            continue
        rent_match = re.search(r"\bRent:\s*\$?\s*([\d,.]+)", card_text, re.I)
        beds_match = re.search(r"\bBeds:\s*(\d+(?:\.\d+)?)", card_text, re.I)
        baths_match = re.search(r"\bBaths:\s*(\d+(?:\.\d+)?)", card_text, re.I)
        if baths_match is None:
            # Direct ``Search_Result`` responses carry the bath in the exact
            # operator script and write its visible <p> at render time.  The
            # browser body has the <p>; a deterministic raw-response replay
            # still has this scalar literal.
            baths_match = re.search(
                r"\bvar\s+bath\s*=\s*['\"](\d+(?:\.\d+)?)['\"]",
                str(card),
                re.IGNORECASE,
            )
        rent = _rm_money(rent_match.group(1) if rent_match else "")
        if not rent or not beds_match or not baths_match:
            return []

        date_match = re.search(
            r"\bnew\s+Date\(\s*['\"](\d{1,2}/\d{1,2}/\d{4})['\"]\s*\)",
            str(card),
            re.IGNORECASE,
        )
        seen_units.add(unit_key)
        output.append(
            make_unit_dict(
                bedrooms=beds_match.group(1),
                bathrooms=baths_match.group(1),
                unit_number=unit_number,
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                availability_date=_rm_iso(date_match.group(1)) if date_match else "",
                source_api_url=source_url,
                extraction_tier=_UNITLIST_CARD_TIER,
                data_gaps=["floor_plan_name", "area"],
            )
        )
    return output


def parse_rentmanager_condor_cards(
    html: str,
    source_url: str,
) -> list[dict[str, Any]]:
    """Parse Condor-family RentManager availability cards into apartments.

    The public roster uses ``data-name`` for the apartment number and
    ``data-unitfloorplan`` for its plan.  That is intentionally handled here,
    rather than changing the generic ``data-name`` mapping, because other
    websites use ``data-name`` for a plan.  The document must first pass the
    composite card + RentManager-attribution guard in the detector.

    A stable ``data-unitid`` and apartment number are sufficient identity, so
    a card with a blank floor-plan name is retained. Duplicate rendered cards
    are collapsed by ``data-unitid``.
    """
    if not has_rentmanager_condor_inventory_signature(html):
        return []

    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return []

    units: list[dict[str, Any]] = []
    seen_uids: set[str] = set()
    for card in soup.select(_CONDOR_CARD_SELECTOR):
        uid = str(card.get("data-unitid") or "").strip()
        unit_number = str(card.get("data-name") or "").strip()
        if not uid or not unit_number or uid in seen_uids:
            continue
        seen_uids.add(uid)

        rent = _rm_money(str(card.get("data-price") or ""))
        date_node = card.select_one(".availability-date [data-available]")
        date_raw = (
            str(date_node.get("data-available") or "")
            if date_node is not None
            else ""
        )
        units.append(
            make_unit_dict(
                floor_plan_name=str(card.get("data-unitfloorplan") or "").strip(),
                bedrooms=str(card.get("data-bed") or "").strip(),
                bathrooms=str(card.get("data-bath") or "").strip(),
                unit_number=unit_number,
                floor=str(card.get("data-floor") or "").strip(),
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                availability_date=_rm_iso(date_raw),
                source_api_url=source_url,
                extraction_tier=_CONDOR_TIER,
                source_ids={"rentmanager_uid": uid},
            )
        )
    return units


def _rentmanager_js_push_objects(html: str, array: str) -> list[dict[str, str]]:
    """Parse bounded quoted fields from ``array.push({…})`` literals.

    This is intentionally not a JavaScript evaluator.  Hidden-Valley-family
    pages serialize simple, quoted scalar records; anything outside that shape
    is ignored.
    """
    if not html or not array.isidentifier():
        return []
    pattern = re.compile(
        _JS_PUSH_RE_TEMPLATE.format(array=re.escape(array)),
        re.IGNORECASE | re.DOTALL,
    )
    objects: list[dict[str, str]] = []
    for match in pattern.finditer(html):
        fields = {
            key.casefold(): value.strip()
            for key, _quote, value in _JS_FIELD_RE.findall(match.group("body"))
        }
        if fields:
            objects.append(fields)
    return objects


def parse_rentmanager_inline_unitcount(
    html: str,
    source_url: str,
) -> list[dict[str, Any]]:
    """Join RentManager's inline floor-plan and available-unit arrays.

    ``units.push`` carries plan dimensions/pricing while ``unitcount.push``
    carries the physical apartment and availability date.  A unit is emitted
    only when its ``matchtype`` resolves to exactly one dimensioned plan.
    """
    plans_raw = _rentmanager_js_push_objects(html, "units")
    units_raw = _rentmanager_js_push_objects(html, "unitcount")
    if not plans_raw or not units_raw:
        return []

    plans: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for raw in plans_raw:
        name = str(raw.get("fpname") or raw.get("matchfpname") or "").strip()
        key = name.casefold()
        if not key:
            continue
        try:
            bedrooms = int(float(raw.get("bedrooms") or ""))
            bathrooms = float(raw.get("bathrooms") or "")
            sqft = int(float(raw.get("sqft") or ""))
        except (TypeError, ValueError):
            continue
        if not (0 <= bedrooms <= 10 and 0 <= bathrooms <= 10 and 150 <= sqft <= 10_000):
            continue
        prices = [
            value
            for value in (
                _rm_money(raw.get("rentoverride", "")),
                _rm_money(raw.get("rent", "")),
            )
            if value and 200 <= value <= 50_000
        ]
        plan = {
            "name": name,
            "bedrooms": bedrooms,
            "bathrooms": bathrooms,
            "sqft": sqft,
            "rent_low": min(prices) if prices else None,
            "rent_high": max(prices) if prices else None,
        }
        if key in plans and plans[key] != plan:
            ambiguous.add(key)
        else:
            plans[key] = plan
    for key in ambiguous:
        plans.pop(key, None)

    output: list[dict[str, Any]] = []
    seen_units: set[str] = set()
    for raw in units_raw:
        plan = plans.get(str(raw.get("matchtype") or "").strip().casefold())
        unit_number = str(raw.get("unit") or "").strip()
        unit_key = unit_number.casefold()
        if (
            plan is None
            or not unit_number
            or not any(char.isdigit() for char in unit_number)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .#/-]{0,30}", unit_number)
            or unit_key in seen_units
        ):
            continue
        seen_units.add(unit_key)
        output.append(
            make_unit_dict(
                floor_plan_name=plan["name"],
                bedrooms=str(plan["bedrooms"]),
                bathrooms=str(plan["bathrooms"]),
                sqft=str(plan["sqft"]),
                unit_number=unit_number,
                rent_low=plan["rent_low"],
                rent_high=plan["rent_high"],
                availability_status="AVAILABLE",
                availability_date=_rm_iso(raw.get("availdate", "")),
                source_api_url=source_url,
                extraction_tier=_INLINE_UNITCOUNT_TIER,
            )
        )
    return output


def parse_rentmanager_leaseleads_cards(
    html: str,
    source_url: str,
) -> list[dict[str, Any]]:
    """Parse strongly attributed LeaseLeads/RentManager unit cards.

    The composite requires both RentManager attribution and the exact
    LeaseLeads floor-plan/card attributes.  Unit identity and rent come from
    the individual card; dimensions come from its nearest plan container.
    """
    marker = "data-ll-floor-plan-unit-carousel-item"
    if not html or marker not in html or _RM_MARK_RE.search(html) is None:
        return []

    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return []

    output: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for card in soup.select("[data-ll-floor-plan-unit-carousel-item]"):
        plan = card.find_parent(attrs={"data-ll-floor-plan": True})
        if plan is None:
            continue
        id_node = card.select_one("[data-ll-floor-plan-unit-carousel-item-id]")
        uid = (
            str(id_node.get("data-ll-floor-plan-unit-carousel-item-id") or "").strip()
            if id_node is not None
            else ""
        )
        name_node = card.select_one("[data-ll-floor-plan-unit-carousel-item-name]")
        name_text = name_node.get_text(" ", strip=True) if name_node is not None else ""
        unit_match = re.search(
            r"\bUnit\s+#?\s*([A-Za-z0-9][A-Za-z0-9 .#/-]{0,30})\b",
            name_text,
            re.IGNORECASE,
        )
        unit_number = unit_match.group(1).strip() if unit_match else ""
        price_node = card.select_one("[data-ll-floor-plan-unit-carousel-item-price]")
        rent = _rm_money(price_node.get_text(" ", strip=True) if price_node else "")
        availability_node = card.select_one(
            "[data-ll-floor-plan-unit-carousel-item-available-on]"
        )
        availability_text = (
            availability_node.get_text(" ", strip=True)
            if availability_node is not None
            else ""
        )
        plan_text = plan.get_text(" ", strip=True)
        bedrooms_match = re.search(r"\b(\d+)\s+Bedroom\b", plan_text, re.IGNORECASE)
        bathrooms_match = re.search(r"\b(\d+(?:\.\d+)?)\s+Bathroom\b", plan_text, re.IGNORECASE)
        sqft_match = re.search(r"\b([\d,]+)\s+Sq\.?\s*Ft\.?\b", plan_text, re.IGNORECASE)
        try:
            sqft = int(sqft_match.group(1).replace(",", "")) if sqft_match else 0
        except ValueError:
            sqft = 0
        availability_lower = availability_text.casefold()
        if (
            not uid.isdigit()
            or uid in seen_ids
            or not unit_number
            or not any(char.isdigit() for char in unit_number)
            or not rent
            or not bedrooms_match
            or not bathrooms_match
            or not 150 <= sqft <= 10_000
            or not availability_text
            or "not available" in availability_lower
            or "unavailable" in availability_lower
        ):
            continue
        seen_ids.add(uid)
        date_match = re.search(r"\b\d{1,2}/\d{1,2}/\d{4}\b", availability_text)
        output.append(
            make_unit_dict(
                floor_plan_name=str(plan.get("data-ll-event-label") or "").strip(),
                bedrooms=bedrooms_match.group(1),
                bathrooms=bathrooms_match.group(1),
                sqft=str(sqft),
                unit_number=unit_number,
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                availability_date=_rm_iso(date_match.group(0)) if date_match else "",
                source_api_url=source_url,
                extraction_tier=_LEASELEADS_CARD_TIER,
                source_ids={"rentmanager_uid": uid},
            )
        )
    return output


def parse_rentmanager_wp_cards(html: str, source_url: str) -> list[dict[str, Any]]:
    """Parse the WordPress RentManager plugin's availability cards.

    2026-07-12: the KRC/WP RentManager theme renders each available unit as
    ``<a class="individual-item" data-bed data-rent data-date>`` cards inside
    ``.rm-ua-container`` — NOT the ``tr.unit_avail_container`` table that
    ``parse_iloveleasing_table`` targets. So finelivingapts-class properties
    (20 real SSR unit cards, verified) parsed to 0 units and fell through to
    the LLM tier. Fields: data-rent (per-unit rent), data-bed (beds), the
    ``.availableDate`` div (real move-in date), the ``h2`` (floor-plan name +
    ``#unit`` in its span), and ``uid=`` in the detail href (stable id).

    Returns ``[]`` on absent markup / unparseable HTML — never raises.
    """
    if not html or not any(
        marker in html
        for marker in (
            "individual-item",
            "rmwb_unit_listing-wrapper",
            "rmwb_listing-wrapper",
            'class="floorplan-item"',
            "class='floorplan-item'",
        )
    ):
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    items = soup.select("a.individual-item")
    units: list[dict[str, Any]] = []
    for it in items:
        href = str(it.get("href") or "")
        rent = _rm_money(it.get("data-rent"))
        beds = it.get("data-bed")

        fp_name = ""
        unit_no = ""
        h2 = it.find("h2")
        if h2 is not None:
            span = h2.find("span")
            if span is not None:
                m = re.search(r"#\s*([A-Za-z0-9-]+)", span.get_text(" ", strip=True))
                if m:
                    unit_no = m.group(1)
                fp_name = h2.get_text(" ", strip=True).replace(
                    span.get_text(" ", strip=True), ""
                ).strip()
            else:
                fp_name = h2.get_text(" ", strip=True)

        avail = ""
        ad = it.find(class_="availableDate")
        if ad is not None:
            dm = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", ad.get_text(" ", strip=True))
            if dm:
                avail = _rm_iso(dm.group(1))
        if not avail and it.get("data-date"):
            dm = re.match(r"(\d{4})/(\d{1,2})", str(it.get("data-date")))
            if dm:
                avail = f"{dm.group(1)}-{int(dm.group(2)):02d}-01"

        bath_m = re.search(r"Bath\s+([\d.]+)", it.get_text(" ", strip=True), re.I)
        baths = bath_m.group(1) if bath_m else ""

        source_ids: dict[str, Any] = {}
        uid_m = re.search(r"uid=(\d+)", href)
        if uid_m:
            source_ids["rentmanager_uid"] = uid_m.group(1)
            if not unit_no:
                unit_no = uid_m.group(1)
        if not unit_no:
            continue

        units.append(
            make_unit_dict(
                floor_plan_name=fp_name,
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths),
                sqft="",
                unit_number=str(unit_no),
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                availability_date=avail,
                source_api_url=source_url,
                extraction_tier="TIER_1_DOM_RENTMANAGER_WP_CARDS",
                source_ids=source_ids,
            )
        )

    # GMC/Legacy-Oaks variant: one ``rmwb_listing-wrapper`` per apartment.
    # Unlike ``rmwb_unit_listing-wrapper`` above, the visible values are
    # label/value ``<li>`` pairs and the unit number has its own heading.  The
    # same-origin details link carries the native RentManager ``uid``.
    for item in soup.select(".rmwb_listing-wrapper:not(.rmwb_unit_listing-wrapper)"):
        unit_node = item.select_one(".rmwb_unit-number")
        unit_match = re.search(
            r"#\s*([A-Za-z0-9-]+)",
            unit_node.get_text(" ", strip=True) if unit_node else "",
        )
        uid_link = item.find("a", href=re.compile(r"[?&]uid=\d+", re.I))
        href = str(uid_link.get("href") or "") if uid_link else ""
        uid_match = re.search(r"[?&]uid=(\d+)", href, re.I)
        unit_number = unit_match.group(1) if unit_match else ""
        native_uid = uid_match.group(1) if uid_match else ""
        if not unit_number or not native_uid:
            continue

        values: dict[str, str] = {}
        for row in item.select(".rmwb_info-list li"):
            label_node = row.select_one(".rmwb_info-title")
            value_node = row.select_one(".rmwb_info-detail")
            if label_node is None or value_node is None:
                continue
            label = re.sub(
                r"[^a-z0-9]+",
                "",
                label_node.get_text(" ", strip=True).casefold(),
            )
            values[label] = value_node.get_text(" ", strip=True)

        rent = _rm_money(values.get("rent", ""))
        beds = values.get("bedrooms", "") or str(item.get("data-bedrooms") or "")
        baths = values.get("bathrooms", "") or str(item.get("data-bathrooms") or "")
        sqft_match = re.search(r"\d[\d,]*", values.get("squarefootage", ""))
        sqft = sqft_match.group(0).replace(",", "") if sqft_match else ""
        plan_name = str(item.get("data-unittype") or "").strip()
        units.append(
            make_unit_dict(
                floor_plan_name=plan_name,
                bedrooms=beds,
                bathrooms=baths,
                sqft=sqft,
                unit_number=unit_number,
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                available_units="1",
                availability_date=_rm_iso(values.get("moveindate", "")),
                source_api_url=source_url,
                extraction_tier="TIER_1_DOM_RENTMANAGER_WP_CARDS",
                source_ids={"rentmanager_uid": native_uid},
            )
        )

    # Newer versions of the same public WordPress plugin render one
    # ``rmwb_unit_listing-wrapper`` per physical apartment.  The stable
    # RentManager uid is in the same-origin detail link; the visible heading
    # ends in the apartment number (``2575 Ivy Ave E. #104``).
    for item in soup.select(".rmwb_unit_listing-wrapper"):
        item_text = item.get_text(" ", strip=True)
        header = item.select_one(".rmwb_listing_header")
        header_text = header.get_text(" ", strip=True) if header else ""
        unit_m = re.search(r"#\s*([A-Za-z0-9-]+)\s*$", header_text)
        uid_link = item.find("a", href=re.compile(r"[?&]uid=\d+", re.I))
        href = str(uid_link.get("href") or "") if uid_link else ""
        uid_m = re.search(r"[?&]uid=(\d+)", href, re.I)
        unit_no = unit_m.group(1) if unit_m else ""
        uid = uid_m.group(1) if uid_m else ""
        if not unit_no:
            unit_no = uid
        if not unit_no:
            continue

        dimensions = re.search(
            r"([\d.]+)\s*Beds?\s*,\s*([\d.]+)\s*Baths?"
            r"(?:\s*\|\s*([\d,]+)\s*Square\s*Feet)?",
            item_text,
            re.I,
        )
        beds = dimensions.group(1) if dimensions else ""
        baths = dimensions.group(2) if dimensions else ""
        sqft = dimensions.group(3).replace(",", "") if dimensions and dimensions.group(3) else ""
        rent_m = re.search(r"Base\s+Rent\*?\s*-?\s*\$\s*([\d,]+)", item_text, re.I)
        rent = _rm_money(rent_m.group(1)) if rent_m else None
        available_m = re.search(
            r"\bAvailable\s+(\d{1,2}/\d{1,2}/\d{4}|Now|Immediate)\b",
            item_text,
            re.I,
        )
        available_raw = available_m.group(1) if available_m else ""
        source_ids = {"rentmanager_uid": uid} if uid else {}
        units.append(
            make_unit_dict(
                floor_plan_name=(
                    f"{beds} Beds, {baths} Bath" if beds and baths else ""
                ),
                bedrooms=beds,
                bathrooms=baths,
                sqft=sqft,
                unit_number=unit_no,
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                availability_date=_rm_iso(available_raw),
                source_api_url=source_url,
                extraction_tier="TIER_1_DOM_RENTMANAGER_WP_CARDS",
                source_ids=source_ids,
            )
        )

    # A third plugin template exposes a compact ``floorplan-item`` table.
    # It omits a display unit number, but every row links to one distinct
    # native ``uid``.  Preserve that operator-issued identifier as both the
    # anchor and unit number so the shared unit gate can retain the row.
    for row in soup.select("tr.floorplan-item"):
        cells = row.find_all("td")
        if len(cells) < 6:
            continue
        href_node = row.find("a", href=re.compile(r"[?&]uid=\d+", re.I))
        href = str(href_node.get("href") or "") if href_node else ""
        uid_m = re.search(r"[?&]uid=(\d+)", href, re.I)
        if not uid_m:
            continue
        uid = uid_m.group(1)
        bed_bath = cells[0].get_text(" ", strip=True)
        dimensions = re.search(r"([\d.]+)\s*/\s*([\d.]+)", bed_bath)
        beds = dimensions.group(1) if dimensions else ""
        baths = dimensions.group(2) if dimensions else ""
        sqft = re.sub(r"[^0-9]", "", cells[1].get_text(" ", strip=True))
        rent = _rm_money(str(row.get("data-rent") or cells[2].get_text(" ", strip=True)))
        available_raw = str(
            row.get("data-availability") or cells[4].get_text(" ", strip=True)
        ).strip()
        units.append(
            make_unit_dict(
                floor_plan_name=bed_bath,
                bedrooms=beds,
                bathrooms=baths,
                sqft=sqft,
                unit_number=uid,
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                availability_date=_rm_iso(available_raw),
                source_api_url=source_url,
                extraction_tier="TIER_1_DOM_RENTMANAGER_WP_CARDS",
                source_ids={"rentmanager_uid": uid},
            )
        )

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for unit in units:
        source_ids = unit.get("source_ids") or {}
        key = str(source_ids.get("rentmanager_uid") or unit.get("unit_number") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(unit)
    return deduped


def parse_iloveleasing_settings(html: str) -> tuple[str, str, str, str] | None:
    """Return the four public ``window.luv_settings`` values.

    The iLoveLeasing launcher publishes these values in the exact property's
    HTML and uses them client-side for its public widget API.  Values are
    configuration identifiers, not credentials; malformed or partial embeds
    are ignored conservatively.
    """
    match = _ILOVELEASING_SETTINGS_RE.search(html or "")
    if not match:
        return None
    values = _ILOVELEASING_SETTING_VALUE_RE.findall(match.group("settings"))
    if len(values) < 3 or not all(values[:3]):
        return None
    padded = [*values[:4], "", "", "", ""]
    return padded[0], padded[1], padded[2], padded[3]


def _iloveleasing_name_key(value: Any) -> str:
    tokens = re.findall(r"[a-z0-9]+", str(value or "").casefold().replace("'", ""))
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    return "".join(
        token for token in tokens if token not in _ILOVELEASING_GENERIC_NAME_TOKENS
    )


def _iloveleasing_address_tokens(value: Any) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").casefold())
        if not token.isdigit()
        and len(token) >= 3
        and token not in _ILOVELEASING_ADDRESS_NOISE
    }


def _iloveleasing_widget_matches_context(
    property_payload: dict[str, Any],
    ctx: AdapterContext,
) -> bool:
    """Conservatively bind a cross-host widget to the target property.

    Some management sites embed a portfolio-wide widget.  Exact property-name
    equality (after housing-type suffixes) rejects those siblings; matching
    street number/name and any published city/state/ZIP fields closes the
    remaining cross-host contamination boundary.
    """
    data = property_payload.get("data")
    if not isinstance(data, dict):
        return False
    location = data.get("location")
    if not isinstance(location, dict):
        return False
    address = location.get("address")
    if not isinstance(address, dict):
        return False

    expected_name = _iloveleasing_name_key(getattr(ctx, "property_name", ""))
    observed_name = _iloveleasing_name_key(location.get("name"))
    if not expected_name or observed_name != expected_name:
        return False

    expected_address = str(getattr(ctx, "address", "") or "").strip()
    observed_address = str(address.get("street1") or "").strip()
    if not expected_address or not observed_address:
        return False
    expected_number = re.search(r"\b\d+[a-z]?\b", expected_address.casefold())
    observed_number = re.search(r"\b\d+[a-z]?\b", observed_address.casefold())
    if (
        expected_number is None
        or observed_number is None
        or expected_number.group(0) != observed_number.group(0)
    ):
        return False
    if not (
        _iloveleasing_address_tokens(expected_address)
        & _iloveleasing_address_tokens(observed_address)
    ):
        return False

    location_pairs = (
        (getattr(ctx, "city", ""), address.get("city")),
        (getattr(ctx, "state", ""), address.get("state")),
        (getattr(ctx, "zip_code", ""), address.get("zip")),
    )
    observed_location = False
    for expected, observed in location_pairs:
        expected_key = re.sub(r"[^a-z0-9]+", "", str(expected or "").casefold())
        if not expected_key:
            continue
        observed_location = True
        observed_key = re.sub(r"[^a-z0-9]+", "", str(observed or "").casefold())
        if expected_key.lstrip("0") != observed_key.lstrip("0"):
            return False
    return observed_location


def parse_iloveleasing_availability(
    payload: Any,
    source_url: str,
    property_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Parse the public iLoveLeasing availability response into units."""
    if not isinstance(payload, dict) or str(payload.get("valid")).casefold() != "true":
        return []
    property_data = property_payload.get("data")
    location = property_data.get("location") if isinstance(property_data, dict) else None
    address = location.get("address") if isinstance(location, dict) else None
    if not isinstance(address, dict):
        address = {}
    full_address = ", ".join(
        part
        for part in (
            str(address.get("street1") or "").strip(),
            str(address.get("city") or "").strip(),
            " ".join(
                part
                for part in (
                    str(address.get("state") or "").strip(),
                    str(address.get("zip") or "").strip(),
                )
                if part
            ),
        )
        if part
    )
    property_name = str(location.get("name") or "").strip() if isinstance(location, dict) else ""

    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload.get("units") or []:
        if not isinstance(item, dict):
            continue
        native_id = str(item.get("unitid") or "").strip()
        unit_number = str(item.get("unitname") or "").strip()
        dedupe_key = native_id or unit_number
        if not dedupe_key or dedupe_key in seen or not unit_number:
            continue
        term_rents = [
            _rm_money(str(term.get("price") or ""))
            for term in (item.get("termprices") or [])
            if isinstance(term, dict)
        ]
        positive_rents = [rent for rent in term_rents if rent is not None and rent > 0]
        if not positive_rents:
            continue
        seen.add(dedupe_key)
        source_ids = {"iloveleasing_unit_id": native_id} if native_id else {}
        unit = make_unit_dict(
            floor_plan_name=str(item.get("planname") or ""),
            bedrooms=str(item.get("beds") or ""),
            bathrooms=str(item.get("baths") or ""),
            sqft=str(item.get("sqft") or "").replace(",", ""),
            unit_number=unit_number,
            rent_low=min(positive_rents),
            rent_high=max(positive_rents),
            availability_status="AVAILABLE",
            availability_date=_rm_iso(str(item.get("dateavailable") or "")),
            source_api_url=source_url,
            extraction_tier="TIER_1_API_RENTMANAGER_ILOVELEASING_PUBLIC",
            source_ids=source_ids,
        )
        unit.update(
            {
                "address": full_address,
                "source_property_name": property_name,
                "source_property_provenance": "published_iloveleasing_widget",
            }
        )
        units.append(unit)
    return units


def _fetch_iloveleasing_public_units(
    html: str,
    page_url: str,
    ctx: AdapterContext,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Resolve the public iLoveLeasing widget API synchronously.

    The adapter calls this helper through ``asyncio.to_thread``.  It performs
    at most three ordinary direct POSTs (init, widget metadata, availability),
    never a browser-unlock or CAPTCHA-solving tier.
    """
    settings = parse_iloveleasing_settings(html)
    if settings is None:
        return [], [], ""
    api_key, property_id, source_id, phase_id = settings
    parsed_url = urlparse(page_url if "://" in page_url else f"https://{page_url}")
    if not parsed_url.hostname:
        return [], [], ""
    origin = f"{parsed_url.scheme or 'https'}://{parsed_url.netloc}"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": origin,
        "Referer": page_url,
    }
    common = {
        "guid": api_key,
        "propid": property_id,
        "advertiserid": "",
        "psid": source_id,
        "userguid": "",
        "url": page_url,
        "phaseid": phase_id,
    }
    responses: list[dict[str, Any]] = []

    try:
        init_url = f"{_ILOVELEASING_API}init/"
        init_response = probe_post(
            init_url,
            data={
                **common,
                "advertiserurl": "",
                "browser": "Mozilla/5.0",
                "domain": parsed_url.hostname,
                "utmsource": "",
                "utmmedium": "",
                "utmcampaign": "",
            },
            headers=headers,
            timeout=25,
        )
        if getattr(init_response, "status_code", 0) != 200:
            return [], responses, ""
        init_payload = json.loads(init_response.text or "{}")
        if str(init_payload.get("valid")).casefold() != "true":
            return [], responses, ""
        common["userguid"] = str(init_payload.get("user_token") or "")
        advertiser = init_payload.get("advertiser")
        if isinstance(advertiser, dict):
            common["advertiserid"] = str(advertiser.get("id") or "")
        responses.append({"url": init_url, "status": 200, "via": "iloveleasing_public"})

        widget_url = f"{_ILOVELEASING_API}widget/"
        widget_response = probe_post(
            widget_url,
            data=common,
            headers=headers,
            timeout=25,
        )
        if getattr(widget_response, "status_code", 0) != 200:
            return [], responses, ""
        widget_payload = json.loads(widget_response.text or "{}")
        property_payload = widget_payload.get("property")
        if not isinstance(property_payload, dict):
            return [], responses, ""
        responses.append({"url": widget_url, "status": 200, "via": "iloveleasing_public"})
        if not _iloveleasing_widget_matches_context(property_payload, ctx):
            return [], responses, "property_boundary_mismatch"
        modules = property_payload.get("modules") or []
        if "availability" not in modules:
            return [], responses, "availability_module_absent"

        availability_url = f"{_ILOVELEASING_API}availability/"
        today = date.today()
        availability_response = probe_post(
            availability_url,
            data={
                **common,
                "movedate": f"{today.month}/{today.day}/{today.year}",
                "beds": "",
                "baths": "",
                "minsqft": "",
                "maxsqft": "",
                "minprice": "",
                "maxprice": "",
            },
            headers=headers,
            timeout=25,
        )
        if getattr(availability_response, "status_code", 0) != 200:
            return [], responses, ""
        availability_payload = json.loads(availability_response.text or "{}")
        responses.append(
            {"url": availability_url, "status": 200, "via": "iloveleasing_public"}
        )
        return (
            parse_iloveleasing_availability(
                availability_payload,
                availability_url,
                property_payload,
            ),
            responses,
            "",
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return [], responses, ""
    except Exception:
        return [], responses, ""


class RentManagerAdapter:
    """RentManager API and attributed public-roster extractor.

    Both paths are public and render-independent: the server-side
    ``ua.rentmanager.com/Search_Result`` feed and SSR availability cards.
    """

    pms_name: str = "rentmanager"

    def static_fingerprints(self) -> list[str]:
        return [
            "ua.rentmanager.com",
            "twa.rentmanager.com",
            "iloveleasing.com",
            "filereader.rentmanager.com",
            "website created by rent manager",
        ]

    def matches_response_body(self, body: Any) -> bool:
        if isinstance(body, str):
            return (
                "`unitid`" in body and "rentmanager" in body.lower()
            ) or has_rentmanager_condor_inventory_signature(body)
        return False

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_TIER)
        html = await _get_page_html(page, ctx)
        if not html:
            result.tier_used = f"{_TIER}_NO_HTML"
            result.errors.append("RENTMANAGER: no page html")
            return result

        from ma_poc.extraction.post_process import post_process

        # ── Path 0: exact SSR apartment rosters already in the page body ────
        # Prefer these before any network probe. Lewiston-family ``unitList``
        # cards, Hidden-Valley ``units.push``/``unitcount.push`` joins, and
        # LeaseLeads data-ll cards carry physical identity and pricing in one
        # attributed body.
        page_url = str(getattr(ctx, "base_url", "") or "")
        published_search_url = find_rentmanager_search_url(html)
        inline_units = parse_rentmanager_unitlist_cards(
            html,
            published_search_url or page_url,
            property_name=str(getattr(ctx, "property_name", "") or ""),
            address=str(getattr(ctx, "address", "") or ""),
            zip_code=str(getattr(ctx, "zip_code", "") or ""),
        ) or parse_rentmanager_inline_unitcount(
            html, str(getattr(ctx, "base_url", "") or "")
        ) or parse_rentmanager_leaseleads_cards(
            html, str(getattr(ctx, "base_url", "") or "")
        )
        if inline_units:
            inline_pp = post_process(
                inline_units, property_id=getattr(ctx, "property_id", None)
            )
            if inline_pp.n_admitted > 0:
                result.units = inline_pp.admitted
                result.plan_summaries = inline_pp.plan_summaries
                result.winning_url = str(getattr(ctx, "base_url", "") or "") or None
                result.tier_used = str(
                    inline_units[0].get("extraction_tier") or _TIER
                )
                result.confidence = min(0.94, 0.75 + 0.02 * inline_pp.n_admitted)
                return result

        # ── Path A: server-side ua.rentmanager.com/Search_Result API ──────────
        search_url = find_rentmanager_search_url(html)
        if not search_url:
            # No verbatim Search_Result URL — try to synthesise from eid
            # (template defaults to the common "<eid>Unit").
            me = _RM_EID_RE.search(html)
            if me:
                eid = me.group(1)
                search_url = (
                    f"https://{eid}.ua.rentmanager.com/Search_Result?"
                    f"command=Search_Result&template={eid}Unit"
                    f"&locations=default&maxperpage=9999"
                )
        if search_url:
            try:
                r = probe_get(search_url, timeout=30)
            except Exception as exc:  # noqa: BLE001 — never raise from an adapter
                result.errors.append(
                    f"rentmanager-fetch-error: {type(exc).__name__}: {str(exc)[:100]}"
                )
                r = None
            if r is not None and getattr(r, "status_code", 0) == 200:
                units = parse_rentmanager_search(r.text or "", search_url)
                if units:
                    _pp = post_process(
                        units, property_id=getattr(ctx, "property_id", None)
                    )
                    if _pp.n_admitted > 0:
                        result.units = _pp.admitted
                        result.plan_summaries = _pp.plan_summaries
                        result.winning_url = search_url
                        result.confidence = min(0.92, 0.7 + 0.04 * _pp.n_admitted)
                        result.api_responses.append(
                            {"url": search_url, "status": 200,
                             "via": "rentmanager_ua_probe"}
                        )
                        return result
                    # 2026-07-11 audit: rent-bearing admission exemption.
                    # Many operators' RentManager Unit templates expose
                    # unit# + market_rent + availability but NO beds/baths/
                    # sqft, so the shared dimension gate rejects every row
                    # (Highland: 1094 parsed / 0 admitted). A Search_Result
                    # record is authoritative Tier-1 operator data — real
                    # unit number + real market rent, deduped by unitid — so
                    # admit rows carrying a unit_number AND a rent even
                    # without dimensions. Scoped to THIS adapter; the shared
                    # validity gate is untouched. Tier is suffixed
                    # _RENT_ONLY and confidence capped so a dimensioned tier,
                    # if one ever exists for the property, still wins.
                    rent_bearing = [
                        u for u in units
                        if str(u.get("unit_number") or "").strip()
                        and (u.get("market_rent_low") or 0)
                    ]
                    if rent_bearing:
                        result.units = rent_bearing
                        result.winning_url = search_url
                        result.tier_used = f"{_TIER}_RENT_ONLY"
                        result.confidence = min(
                            0.85, 0.65 + 0.02 * len(rent_bearing)
                        )
                        result.api_responses.append(
                            {"url": search_url, "status": 200,
                             "via": "rentmanager_ua_probe_rent_only"}
                        )
                        return result
                    result.errors.append(
                        f"RENTMANAGER_VALIDITY_REJECTED: {len(units)} rows "
                        "failed unit_validity"
                    )
                else:
                    result.errors.append(
                        "RENTMANAGER: no units parsed from Search_Result"
                    )
            elif r is not None:
                result.errors.append(
                    f"RENTMANAGER: Search_Result HTTP "
                    f"{getattr(r, 'status_code', 0)}"
                )

        # ── Path B fallback: JS-injected iLoveLeasing availability table ──────
        # (krcapartments-class — render-only; server-side API absent.)
        page_url = str(
            getattr(getattr(ctx, "fetch_result", None), "final_url", "")
            or getattr(ctx, "base_url", "")
            or ""
        )
        il_units = parse_iloveleasing_table(html, page_url)
        if il_units:
            _pp = post_process(
                il_units, property_id=getattr(ctx, "property_id", None)
            )
            if _pp.n_admitted > 0:
                result.units = _pp.admitted
                result.plan_summaries = _pp.plan_summaries
                result.winning_url = page_url or None
                result.tier_used = "TIER_1_DOM_RENTMANAGER_ILOVELEASING"
                result.confidence = min(0.90, 0.7 + 0.04 * _pp.n_admitted)
                return result
            result.errors.append(
                f"RENTMANAGER_ILOVELEASING_VALIDITY_REJECTED: "
                f"{len(il_units)} rows failed unit_validity"
            )

        # ── Path C: Condor-family SSR availability cards ──────────────────────
        # These strongly attributed pages expose the complete apartment roster
        # in data-* fields. ``data-name`` means unit number here (not plan).
        condor_units = parse_rentmanager_condor_cards(html, page_url)
        if condor_units:
            _pp = post_process(
                condor_units, property_id=getattr(ctx, "property_id", None)
            )
            if _pp.n_admitted > 0:
                result.units = _pp.admitted
                result.plan_summaries = _pp.plan_summaries
                result.winning_url = page_url or None
                result.tier_used = _CONDOR_TIER
                result.confidence = min(0.92, 0.72 + 0.04 * _pp.n_admitted)
                return result
            result.errors.append(
                f"RENTMANAGER_CONDOR_VALIDITY_REJECTED: "
                f"{len(condor_units)} cards failed unit_validity"
            )

        # ── Path D: WordPress RentManager plugin availability cards ───────────
        # (finelivingapts-class — <a class="individual-item" data-rent …>
        # cards inside .rm-ua-container that the iLoveLeasing table parser
        # can't see. 19 SSR unit cards verified live w/ rent+beds+date+uid.)
        wp_units = parse_rentmanager_wp_cards(html, page_url)
        if wp_units:
            _pp = post_process(
                wp_units, property_id=getattr(ctx, "property_id", None)
            )
            if _pp.n_admitted > 0:
                result.units = _pp.admitted
                result.plan_summaries = _pp.plan_summaries
                result.winning_url = page_url or None
                result.tier_used = "TIER_1_DOM_RENTMANAGER_WP_CARDS"
                result.confidence = min(0.90, 0.7 + 0.04 * _pp.n_admitted)
                return result
            result.errors.append(
                f"RENTMANAGER_WP_CARDS_VALIDITY_REJECTED: "
                f"{len(wp_units)} cards failed unit_validity"
            )

        # ── Path D: public iLoveLeasing widget API ───────────────────────────
        # Some RentManager-family properties expose no ua.rentmanager.com
        # Search_Result and render no availability table. Their exact page
        # instead publishes ``window.luv_settings``; the first-party launcher
        # uses those public values to POST to init/widget/availability. Resolve
        # the same documented client flow, then require exact widget name +
        # address metadata before admitting any cross-host inventory.
        if parse_iloveleasing_settings(html) is not None:
            il_public_units, il_public_responses, il_public_reason = (
                await asyncio.to_thread(
                    _fetch_iloveleasing_public_units,
                    html,
                    page_url,
                    ctx,
                )
            )
            result.api_responses.extend(il_public_responses)
            if il_public_reason:
                result.errors.append(
                    f"RENTMANAGER_ILOVELEASING_PUBLIC: {il_public_reason}"
                )
            if il_public_units:
                _pp = post_process(
                    il_public_units, property_id=getattr(ctx, "property_id", None)
                )
                if _pp.n_admitted > 0:
                    result.units = _pp.admitted
                    result.plan_summaries = _pp.plan_summaries
                    result.winning_url = f"{_ILOVELEASING_API}availability/"
                    result.tier_used = (
                        "TIER_1_API_RENTMANAGER_ILOVELEASING_PUBLIC"
                    )
                    result.confidence = min(0.92, 0.72 + 0.04 * _pp.n_admitted)
                    return result
                result.errors.append(
                    "RENTMANAGER_ILOVELEASING_PUBLIC_VALIDITY_REJECTED: "
                    f"{len(il_public_units)} rows failed unit_validity"
                )

        if not result.errors:
            result.tier_used = f"{_TIER}_NO_ENDPOINT"
            result.errors.append(
                "RENTMANAGER: no ua.rentmanager.com Search_Result and "
                "no supported public availability cards"
            )
        result.confidence = 0.0
        return result

    def _origin(self, url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""
