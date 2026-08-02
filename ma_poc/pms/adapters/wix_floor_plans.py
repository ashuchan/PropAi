"""Property-bound Wix plan cards and explicitly linked 3DPlans rosters.

Wix component ids are site-specific, so this adapter reads bounded semantic
records instead of depending on ``comp-*`` selectors.  Three source shapes are
currently accepted:

* the original pipe-delimited ``Starting at`` card;
* a labeled Wix plan card (``Bed:``, ``Bathroom:``, ``Rent Starting at:``);
* a Wix repeater item with an authored plan/category rent range.

The first two shapes may omit area.  They remain plan rows and inquiry text is
preserved as ``UNKNOWN`` -- a starting rent does not prove a current apartment.

One external unit route is supported deliberately: an exact Wix property page
may label an ``apps.3dplans.com/InteractivePropertyMap/PropertyMap`` URL as its
available-unit map.  The returned roster is accepted only after the map GUID,
provider property metadata, configured property identity, and Wix-authored
plan catalogue all agree.  This is not a generic outbound-link crawler.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import (
    VERIFIED_PLAN_ONLY_SURFACE_KEY,
    AdapterContext,
    AdapterResult,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)


_PLAN_TIER = "TIER_1_DOM_WIX_FLOOR_PLANS"
_MAP_TIER = "TIER_1_API_WIX_3DPLANS"
_MAX_INTERNAL_PLAN_PAGES = 4
_MAX_MAP_RESPONSES = 24
_MAP_HOST = "apps.3dplans.com"
_MAP_PATH = "/interactivepropertymap/propertymap"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_PLAN_LINK_LABEL_RE = re.compile(
    r"^(?:floor\s*plans?|pricing|apartments)$", re.IGNORECASE
)
_BEDROOM_PLAN_LINK_RE = re.compile(
    r"^(?:studio|one|two|three|four|1|2|3|4)[ -]?(?:bedroom|bed)(?:s)?$",
    re.IGNORECASE,
)
_AVAILABLE_MAP_LABEL_RE = re.compile(
    r"(?:\bview\s+map\s+of\s+available\s+units\b|"
    r"\bmap\s+for\s+available\s+units\b|\bavailability\s+map\b)",
    re.IGNORECASE,
)
_PLAN_CODE_RE = re.compile(r"^[A-Z]{1,3}\d{1,3}$", re.IGNORECASE)


# Kept as a compatibility fallback for browser stubs and older tests.  The
# production path prefers page HTML because it also proves record boundaries,
# authored internal links, and the absence/presence of a unit-bearing map.
_WIX_DOM_JS = r"""
async () => {
  const T = (el) => (el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : '');
  const seen = new Set();
  const cards = [];
  for (const el of Array.from(document.querySelectorAll('div, section, article, li'))) {
    const text = T(el);
    if (text.length < 18 || text.length > 700) continue;
    const hasRent = /(?:Starting\s+at|Rent\s+Starting\s+at)\s*:?\s*\$\s*[\d,]+/i.test(text)
      || /\$\s*[\d,]+\s*[-–]\s*\$?\s*[\d,]+/.test(text);
    const hasPlan = /(?:studio|\d+\s*(?:bed|bedroom)|\bBed\s*:)/i.test(text);
    if (!hasRent || !hasPlan || seen.has(text)) continue;
    seen.add(text);
    cards.push({tag: el.tagName, id: el.id || '', text});
  }
  return {ok: cards.length > 0, cards};
}
"""


_SPECS_RE = re.compile(
    r"(?P<beds>studio|\d+)(?:\s*(?:bed|bedroom)s?)?\s*\|\s*"
    r"(?P<baths>\d+(?:\.\d+)?)\s*bath\w*\s*\|\s*"
    r"(?P<sqft>\d[\d,]*)\s*(?:sq\.?\s*ft|square\s*feet)",
    re.IGNORECASE,
)
_STARTING_AT_RE = re.compile(
    r"Starting\s+at\s*:?\s*\$\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE
)
_DEPOSIT_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s+Starting\s+at", re.IGNORECASE
)
_LABELED_CARD_RE = re.compile(
    r"^(?P<title>[A-Za-z0-9][A-Za-z0-9 &'’/().+-]{1,100}?)\s+"
    r"(?P<inquiry>Contact\s+for\s+Availability)\s+"
    r"Bed\s*:\s*(?P<beds>\d+)\s+"
    r"Bathrooms?\s*:\s*(?P<baths>\d+(?:\.\d+)?)"
    r"(?:\s+with\s+[^$]{1,80})?\s+"
    r"Rent\s+Starting\s+at\s*:\s*\$\s*(?P<low>[\d,]+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)
_RANGE_CARD_RE = re.compile(
    r"^(?P<title>Studio|\d+\s+Bedroom(?:\s*&\s*\d+(?:\.\d+)?\s+Bath)?"
    r"(?:\s+Penthouse)?)\s+"
    r"\$\s*(?P<low>[\d,]+(?:\.\d+)?)\s*[-–]\s*"
    r"\$?\s*(?P<high>[\d,]+(?:\.\d+)?)"
    r"(?:\s+Reserve\s+Now!*)*\s*$",
    re.IGNORECASE,
)
_TITLE_BED_RE = re.compile(r"\b(\d+)\s+Bedroom\b", re.IGNORECASE)
_TITLE_BATH_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s+Bath\b", re.IGNORECASE)
_MONEY_VALUE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")
_AREA_VALUE_RE = re.compile(
    r"\b(\d[\d,]*)[\s-]*(?:sq\.?\s*ft\.?|square[\s-]*(?:feet|foot))\b",
    re.IGNORECASE,
)
_BED_VALUE_RE = re.compile(
    r"\b(studio)(?:\s*(?:bed|bedroom)s?)?\b|\b(\d+)\s*(?:bed|bedroom)s?\b",
    re.IGNORECASE,
)
_BATH_VALUE_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*bath(?:room)?s?\b", re.IGNORECASE)
_CONSTELLATION_CARD_RE = re.compile(
    r"^Floor\s+Plan\s*:\s*(?P<title>.+?)\s+"
    r"(?P<beds>\d+)\s+Bed\s*\|\s*"
    r"(?P<baths>\d+(?:\.\d+)?)\s+Bath\s*\|\s*"
    r"(?P<sqft>\d[\d,]*)\s+Sq\.?\s*Ft\.?\s*"
    r"\$\s*(?P<rent>[\d,]+(?:\.\d+)?)$",
    re.IGNORECASE,
)
_GENTRY_CARD_RE = re.compile(
    r"^(?P<title>(?:STUDIO|\d+\s+BEDROOM)\b.+?)\s*"
    r"(?:\(\s*(?P<sqft1>\d[\d,]*)\s+SQ\.?\s*FT\.?\s*\)|"
    r"(?P<sqft2>\d[\d,]*)\s+SQ\.?\s*FT\.?)\s+"
    r"Unfurnished\s+\$\s*(?P<low>[\d,]+)\s*[-–]\s*\$?\s*(?P<high>[\d,]+)\s+"
    r"Furnished\s+\$\s*(?P<furnished_low>[\d,]+)"
    r"(?:\s*[-–]\s*\$?\s*(?P<furnished_high>[\d,]+))?$",
    re.IGNORECASE,
)
_WESTGATE_CARD_RE = re.compile(
    r"\bFloor\s+Plans\s+Bed\s*:\s*(?P<beds>\d+)\s+"
    r"Bath\s*:\s*(?P<baths>\d+(?:\.\d+)?)\s+"
    r"SQ\.?\s*FT\.?\s*:\s*(?P<sqft>\d[\d,]*)\s+"
    r"Rent\s*:\s*\$\s*(?P<low>[\d,]+)\s*[-–]\s*\$?\s*(?P<high>[\d,]+)\s+"
    r"Deposit\s*:\s*Varies\b",
    re.IGNORECASE,
)
_ALLEN_RANCH_RE = re.compile(
    r"\bNow\s+Available!.*?\bDetails\s*:\s*\$\s*(?P<rent>[\d,]+(?:\.\d+)?)\s+"
    r"Per\s+Month\s+(?P<beds>\d+)\s+Bedroom\s*/\s*"
    r"(?P<baths>\d+(?:\.\d+)?)\s+Bathroom\s+"
    r"(?P<sqft>\d[\d,]*)\s+Sq\.?\s*Ft\b.*?"
    r"\bLease\s+Terms\s*:\s*(?P<term>Month\s+to\s+Month)\s+"
    r"\$\s*(?P<deposit>[\d,]+(?:\.\d+)?)\s+Refundable\s+Deposit\b",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class _HtmlSource:
    url: str
    html: str
    status: int = 200
    semantic_priority: int = 100
    role: str = "current_page"


@dataclass(frozen=True)
class _PlanPageLink:
    url: str
    semantic_priority: int
    label: str


@dataclass(frozen=True)
class _MapLink:
    url: str
    guid: str
    label: str
    marketing_url: str


@dataclass
class _MapCapture:
    url: str
    status: int
    body: Any
    request_body: Any


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u200b", "").replace("\ufeff", "")).strip()


def _split_plan_title_and_code(prefix: str) -> tuple[str, str]:
    """Split ``{authored title} {short plan code}`` conservatively."""

    prefix = prefix.strip()
    if not prefix:
        return "", ""
    parts = prefix.rsplit(maxsplit=1)
    if len(parts) == 2 and re.fullmatch(r"[A-Z]\d{0,2}|\d[A-Z]{0,2}", parts[1].upper()):
        return parts[0].strip(), parts[1].strip()
    return prefix, ""


def _parse_card_text(text: str) -> dict[str, Any] | None:
    """Parse one bounded Wix plan record without manufacturing fields."""

    text = _clean_text(text)
    if not text or len(text) > 700:
        return None

    # Original liveatarcos shape.
    specs = _SPECS_RE.search(text)
    if specs:
        starting = _STARTING_AT_RE.search(text)
        if not starting:
            return None
        bed_text = specs.group("beds")
        bedrooms = 0 if bed_text.casefold() == "studio" else int(bed_text)
        title, code = _split_plan_title_and_code(text[: specs.start()].rstrip(" ,;"))
        deposit_match = _DEPOSIT_RE.search(text)
        low = money_to_int(starting.group(1))
        return {
            "title": title,
            "code": code,
            "beds": bedrooms,
            "baths": specs.group("baths"),
            "sqft": specs.group("sqft").replace(",", ""),
            "rent": low,
            "rent_low": low,
            "rent_high": low,
            "deposit": (
                deposit_match.group(1).replace(",", "") if deposit_match else ""
            ),
            "availability_status": "UNKNOWN",
            "shape": "pipe_starting_at",
        }

    # Westerville-style exact plan cards.  Requiring one occurrence avoids
    # treating a parent section containing several plans as a single record.
    if text.casefold().count("rent starting at") == 1:
        labeled = _LABELED_CARD_RE.fullmatch(text)
        if labeled:
            low = money_to_int(labeled.group("low"))
            return {
                "title": labeled.group("title").strip(),
                "code": "",
                "beds": int(labeled.group("beds")),
                "baths": labeled.group("baths"),
                "sqft": "",
                "rent": low,
                "rent_low": low,
                "rent_high": low,
                "deposit": "",
                "availability_status": "UNKNOWN",
                "shape": "labeled_inquiry",
            }

    # Bellagio-style category cards.  Beds/baths are derived only when the
    # authored title itself publishes them; penthouse bath is intentionally
    # left absent.
    ranged = _RANGE_CARD_RE.fullmatch(text)
    if ranged:
        title = ranged.group("title").strip()
        bed_match = _TITLE_BED_RE.search(title)
        bath_match = _TITLE_BATH_RE.search(title)
        low = money_to_int(ranged.group("low"))
        high = money_to_int(ranged.group("high"))
        if not low or not high or high < low:
            return None
        return {
            "title": title,
            "code": "",
            "beds": 0 if title.casefold() == "studio" else int(bed_match.group(1)) if bed_match else "",
            "baths": bath_match.group(1) if bath_match else "",
            "sqft": "",
            "rent": low,
            "rent_low": low,
            "rent_high": high,
            "deposit": "",
            "availability_status": "UNKNOWN",
            "shape": "category_range",
        }
    return None


def _visible_value(value: Any) -> str:
    """Return visible text from a scalar Wix CMS field."""

    if not isinstance(value, str):
        return _clean_text(value)
    if "<" in value and ">" in value:
        return _clean_text(BeautifulSoup(value, "html.parser").get_text(" "))
    return _clean_text(value)


def _field_by_name(record: Mapping[str, Any], *needles: str) -> str:
    wanted = {re.sub(r"[^a-z0-9]", "", needle.casefold()) for needle in needles}
    for key, value in record.items():
        normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
        if normalized in wanted:
            visible = _visible_value(value)
            if visible:
                return visible
    return ""


def _first_dimension(pattern: re.Pattern[str], value: str) -> str:
    match = pattern.search(_clean_text(value))
    return match.group(1).replace(",", "") if match else ""


def _money_values(value: str) -> list[int]:
    return [
        amount
        for match in _MONEY_VALUE_RE.finditer(_clean_text(value))
        if (amount := money_to_int(match.group(1))) is not None
    ]


def _iter_wix_cms_records(html: str) -> list[dict[str, Any]]:
    """Read exact Wix data-store records from the bounded warmup payload."""

    if not html:
        return []
    script = BeautifulSoup(html, "html.parser").find("script", id="wix-warmup-data")
    payload_text = str(script.string or script.get_text() or "") if script else ""
    if not payload_text or len(payload_text) > 5_000_000:
        return []
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError):
        return []

    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            stores = value.get("recordsByCollectionId")
            if isinstance(stores, Mapping):
                for collection in stores.values():
                    if not isinstance(collection, Mapping):
                        continue
                    for record in collection.values():
                        if not isinstance(record, Mapping):
                            continue
                        record_id = _clean_text(record.get("_id"))
                        if record_id and record_id not in seen:
                            seen.add(record_id)
                            records.append(dict(record))
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(payload)
    return records


def _parse_wix_cms_record(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Interpret one CMS record without flattening its labeled fields."""

    record_id = _clean_text(record.get("_id"))
    if not record_id:
        return None

    code = _field_by_name(record, "unitName", "planCode", "floorPlanCode")
    title = _field_by_name(record, "title", "floorPlanName", "planName", "name")
    phase = _field_by_name(record, "phase")
    if not phase:
        phase = next(
            (
                visible
                for value in record.values()
                if (visible := _visible_value(value))
                and re.fullmatch(r"Phase\s+[A-Z0-9IVX -]+", visible, re.IGNORECASE)
            ),
            "",
        )
    plan_name = code or title
    if title and phase and not code:
        plan_name = f"{title} — {phase}"
    if not plan_name:
        return None

    bed_text = _field_by_name(record, "beds", "bed", "bedrooms", "bedroom")
    bath_text = _field_by_name(record, "bath", "baths", "bathrooms", "bathroom")
    if not bed_text:
        bed_text = title
    if not bath_text:
        bath_text = title
    bed_match = _BED_VALUE_RE.search(bed_text)
    bath_match = _BATH_VALUE_RE.search(bath_text)
    beds: str | int = ""
    if bed_match:
        bed_value = bed_match.group(1) or bed_match.group(2)
        beds = 0 if bed_value.casefold() == "studio" else int(bed_value)
    baths = bath_match.group(1) if bath_match else ""

    area_value = _field_by_name(
        record,
        "sqFt",
        "sqft",
        "squareFeet",
        "area",
        "floorPlanArea",
    )
    area_candidates: list[str] = []
    if area_value:
        area_candidates.append(area_value)
    area_candidates.extend(_visible_value(value) for value in record.values())
    area_matches = [
        match
        for value in area_candidates
        if (match := _AREA_VALUE_RE.search(value))
    ]
    sqft = area_matches[0].group(1).replace(",", "") if area_matches else ""
    area_raw = area_matches[0].group(0) if area_matches else ""

    deposit_text = next(
        (
            _visible_value(value)
            for key, value in record.items()
            if "deposit" in re.sub(r"[^a-z0-9]", "", str(key).casefold())
            and _money_values(_visible_value(value))
        ),
        "",
    )
    deposit_values = _money_values(deposit_text)

    rent_text = next(
        (
            _visible_value(value)
            for key, value in record.items()
            if (
                "rent" in re.sub(r"[^a-z0-9]", "", str(key).casefold())
                or re.sub(r"[^a-z0-9]", "", str(key).casefold())
                in {"price", "startingprice"}
            )
            and "deposit" not in re.sub(r"[^a-z0-9]", "", str(key).casefold())
            and _money_values(_visible_value(value))
        ),
        "",
    )
    if not rent_text:
        opaque_money: list[tuple[str, list[int]]] = []
        for key, value in record.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if any(token in normalized_key for token in ("deposit", "image")):
                continue
            visible = _visible_value(value)
            values = _money_values(visible)
            if values:
                opaque_money.append((visible, values))
        if len(opaque_money) == 1:
            rent_text = opaque_money[0][0]
    rents = _money_values(rent_text)
    if not rents:
        return None
    rent_low, rent_high = rents[0], rents[-1]
    if rent_high < rent_low:
        return None
    # A CMS record must publish a plan name, rent, and at least one structured
    # dimension. This keeps unrelated Wix store collections out of inventory.
    if beds == "" and baths == "" and not sqft:
        return None

    return {
        "plan_name": plan_name,
        "title": title,
        "code": code,
        "phase": phase,
        "beds": beds,
        "baths": baths,
        "sqft": sqft,
        "area_raw": area_raw,
        "rent_low": rent_low,
        "rent_high": rent_high,
        "deposit": str(deposit_values[0]) if deposit_values else "",
        "availability_status": "UNKNOWN",
        "availability_date": "",
        "record_id": record_id,
        "shape": "wix_cms_record",
    }


def _route_record_id(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return f"route:{path.casefold()}"


def _nearest_component_id(element: Any) -> str:
    current = element
    for _ in range(8):
        if current is None:
            break
        component_id = _clean_text(getattr(current, "attrs", {}).get("id"))
        if component_id.startswith("comp-"):
            return component_id
        current = getattr(current, "parent", None)
    return ""


def _parse_constellation_records(soup: BeautifulSoup, source_url: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for text_node in soup.find_all(string=re.compile(r"^\s*Floor\s+Plan\s*:", re.IGNORECASE)):
        current = text_node.parent
        parsed: re.Match[str] | None = None
        for _ in range(18):
            if current is None:
                break
            parsed = _CONSTELLATION_CARD_RE.fullmatch(
                _clean_text(" ".join(current.stripped_strings))
            )
            if parsed:
                break
            current = current.parent
        if not parsed:
            continue
        anchor = text_node.find_parent("a", href=True)
        href = urljoin(source_url, str(anchor.get("href") or "")) if anchor else ""
        if not href or _normal_host(href) != _normal_host(source_url):
            continue
        record_id = _route_record_id(href)
        if record_id in seen:
            continue
        seen.add(record_id)
        rent = money_to_int(parsed.group("rent"))
        if rent is None:
            continue
        out.append(
            {
                "plan_name": parsed.group("title").strip(),
                "title": parsed.group("title").strip(),
                "code": "",
                "beds": int(parsed.group("beds")),
                "baths": parsed.group("baths"),
                "sqft": parsed.group("sqft").replace(",", ""),
                "area_raw": f"{parsed.group('sqft')} Sq. Ft.",
                "rent_low": rent,
                "rent_high": rent,
                "deposit": "",
                "availability_status": "UNKNOWN",
                "availability_date": "",
                "record_id": record_id,
                "shape": "wix_authored_plan_route",
            }
        )
    return out


def _parse_gentry_records(soup: BeautifulSoup) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for element in soup.find_all("div", id=True):
        text = _clean_text(" ".join(element.stripped_strings))
        parsed = _GENTRY_CARD_RE.fullmatch(text)
        if not parsed:
            continue
        record_id = _clean_text(element.get("id"))
        if not record_id or record_id in seen:
            continue
        seen.add(record_id)
        title = parsed.group("title").strip()
        bed_match = _BED_VALUE_RE.search(title)
        beds: str | int = ""
        if bed_match:
            bed_value = bed_match.group(1) or bed_match.group(2)
            beds = 0 if bed_value.casefold() == "studio" else int(bed_value)
        low = money_to_int(parsed.group("low"))
        high = money_to_int(parsed.group("high"))
        furnished_low = money_to_int(parsed.group("furnished_low"))
        furnished_high = money_to_int(parsed.group("furnished_high")) or furnished_low
        if low is None or high is None or high < low:
            continue
        sqft = (parsed.group("sqft1") or parsed.group("sqft2") or "").replace(",", "")
        out.append(
            {
                "plan_name": title,
                "title": title,
                "code": "",
                "beds": beds,
                "baths": "",
                "sqft": sqft,
                "area_raw": f"{sqft} SQ. FT.",
                "rent_low": low,
                "rent_high": high,
                "furnished_rent_low": furnished_low,
                "furnished_rent_high": furnished_high,
                "deposit": "",
                "availability_status": "UNKNOWN",
                "availability_date": "",
                "record_id": record_id,
                "shape": "wix_authored_style_card",
            }
        )
    return out


def _parse_westgate_record(soup: BeautifulSoup, source_url: str) -> list[dict[str, Any]]:
    text = _clean_text(" ".join(soup.stripped_strings))
    parsed = _WESTGATE_CARD_RE.search(text)
    if not parsed:
        return []
    title = _clean_text(soup.title.get_text(" ") if soup.title else "").split("|", 1)[0].strip()
    if not re.fullmatch(r"(?:STUDIO|(?:ONE|TWO|THREE|FOUR|[1-4])\s+BEDROOM)", title, re.IGNORECASE):
        return []
    low = money_to_int(parsed.group("low"))
    high = money_to_int(parsed.group("high"))
    if low is None or high is None or high < low:
        return []
    return [
        {
            "plan_name": title,
            "title": title,
            "code": "",
            "beds": int(parsed.group("beds")),
            "baths": parsed.group("baths"),
            "sqft": parsed.group("sqft").replace(",", ""),
            "area_raw": f"{parsed.group('sqft')} SQ.FT.",
            "rent_low": low,
            "rent_high": high,
            "deposit": "Varies",
            "availability_status": "UNKNOWN",
            "availability_date": "",
            "record_id": _route_record_id(source_url),
            "shape": "wix_labeled_route_summary",
        }
    ]


def _parse_indian_village_records(soup: BeautifulSoup) -> list[dict[str, Any]]:
    text = _clean_text(" ".join(soup.stripped_strings))
    if "Current Rates" not in text or "Layout of Apartments" not in text:
        return []
    rents = [
        (money_to_int(low), money_to_int(high) if high else money_to_int(low))
        for low, high in re.findall(
            r"\$\s*([\d,]+)(?:\s*[-–]\s*\$?\s*([\d,]+))?\s+Monthly\s+Rent\b",
            text,
            re.IGNORECASE,
        )
    ]
    deposits = [
        money_to_int(value)
        for value in re.findall(
            r"\$\s*([\d,]+)\s+Security\s+Deposit\b", text, re.IGNORECASE
        )
    ]
    layout_text = text.split("Layout of Apartments", 1)[-1]
    areas: dict[int, int] = {}
    for beds in (1, 2):
        match = re.search(
            rf"{beds}\s+Bedroom\s+Apartment(?:\s+{beds}\s+Bedroom\s+Apartment)?\s+"
            r"(\d[\d,]*)\s+square\s+feet\b",
            layout_text,
            re.IGNORECASE,
        )
        if match:
            areas[beds] = int(match.group(1).replace(",", ""))
    if len(rents) != 2 or len(deposits) != 2 or set(areas) != {1, 2}:
        return []
    out: list[dict[str, Any]] = []
    for index, beds in enumerate((1, 2)):
        low, high = rents[index]
        if low is None or high is None or high < low or deposits[index] is None:
            return []
        name = f"{beds} Bedroom Apartment"
        out.append(
            {
                "plan_name": name,
                "title": name,
                "code": "",
                "beds": beds,
                "baths": "",
                "sqft": str(areas[beds]),
                "area_raw": f"{areas[beds]} square feet",
                "rent_low": low,
                "rent_high": high,
                "deposit": str(deposits[index]),
                "availability_status": "UNKNOWN",
                "availability_date": "",
                "record_id": f"authored:{beds}-bedroom-apartment",
                "shape": "wix_paired_rates_and_layout",
            }
        )
    return out


def _parse_allen_ranch_record(soup: BeautifulSoup) -> list[dict[str, Any]]:
    text = _clean_text(" ".join(soup.stripped_strings))
    parsed = _ALLEN_RANCH_RE.search(text)
    if not parsed:
        return []
    title_match = re.search(r"\b\d+\s+Bedroom\s+Townhouse\b", text, re.IGNORECASE)
    title = title_match.group(0) if title_match else f"{parsed.group('beds')} Bedroom Townhouse"
    rent = money_to_int(parsed.group("rent"))
    deposit = money_to_int(parsed.group("deposit"))
    if rent is None or deposit is None:
        return []
    component = soup.find(string=re.compile(r"Now\s+Available!", re.IGNORECASE))
    record_id = _nearest_component_id(component.parent if component else None)
    return [
        {
            "plan_name": title,
            "title": title,
            "code": "",
            "beds": int(parsed.group("beds")),
            "baths": parsed.group("baths"),
            "sqft": parsed.group("sqft").replace(",", ""),
            "area_raw": f"{parsed.group('sqft')} Sq Ft",
            "rent_low": rent,
            "rent_high": rent,
            "deposit": str(deposit),
            "availability_status": "AVAILABLE",
            "availability_date": "Now",
            "lease_term": parsed.group("term"),
            "record_id": record_id or "authored:now-available-townhouse",
            "shape": "wix_labeled_available_plan",
        }
    ]


def _row_from_parsed_record(
    parsed: Mapping[str, Any],
    source_url: str,
    *,
    verified_plan_only: bool,
) -> dict[str, Any]:
    plan_name = _clean_text(parsed.get("plan_name") or parsed.get("code") or parsed.get("title"))
    beds = parsed.get("beds", "")
    baths = parsed.get("baths", "")
    sqft = _clean_text(parsed.get("sqft"))
    rent_low = parsed.get("rent_low")
    rent_high = parsed.get("rent_high")
    record_id = _clean_text(parsed.get("record_id"))
    row = make_unit_dict(
        floor_plan_name=plan_name,
        bed_label=bed_label_from(beds, plan_name),
        bedrooms=str(beds) if beds != "" else "",
        bathrooms=str(baths),
        sqft=sqft,
        unit_number="",
        rent_low=rent_low if isinstance(rent_low, int) else None,
        rent_high=rent_high if isinstance(rent_high, int) else None,
        rent_range=format_rent_range(
            rent_low if isinstance(rent_low, int) else None,
            rent_high if isinstance(rent_high, int) else None,
        ),
        deposit=_clean_text(parsed.get("deposit")),
        availability_status=_clean_text(parsed.get("availability_status")) or "UNKNOWN",
        availability_date=_clean_text(parsed.get("availability_date")),
        lease_term=_clean_text(parsed.get("lease_term")),
        source_api_url=source_url,
        extraction_tier=_PLAN_TIER,
        source_ids={"wix_plan_record_id": record_id} if record_id else None,
        data_gaps=[
            field
            for field, value in (
                ("unit_number", ""),
                ("sqft", sqft),
                ("availability_date", parsed.get("availability_date")),
            )
            if not value
        ],
    )
    row["wix_plan_record_shape"] = _clean_text(parsed.get("shape"))
    if parsed.get("title"):
        row["wix_plan_title"] = _clean_text(parsed.get("title"))
    if parsed.get("code"):
        row["wix_plan_code"] = _clean_text(parsed.get("code"))
    if parsed.get("phase"):
        row["wix_plan_phase"] = _clean_text(parsed.get("phase"))
    if parsed.get("area_raw"):
        row["area_raw"] = _clean_text(parsed.get("area_raw"))
    for field in ("furnished_rent_low", "furnished_rent_high"):
        if isinstance(parsed.get(field), int):
            row[field] = parsed[field]
    if verified_plan_only:
        row[VERIFIED_PLAN_ONLY_SURFACE_KEY] = "wix.bounded_plan_records"
    return row


def extract_wix_authored_plan_rows(
    html: str,
    source_url: str,
    *,
    verified_plan_only: bool = False,
) -> list[dict[str, Any]]:
    """Extract the highest-authority bounded Wix plan record family."""

    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    parsed_records = [
        parsed
        for record in _iter_wix_cms_records(html)
        if (parsed := _parse_wix_cms_record(record)) is not None
    ]
    if not parsed_records:
        for parser in (
            lambda: _parse_constellation_records(soup, source_url),
            lambda: _parse_gentry_records(soup),
            lambda: _parse_westgate_record(soup, source_url),
            lambda: _parse_indian_village_records(soup),
            lambda: _parse_allen_ranch_record(soup),
        ):
            parsed_records = parser()
            if parsed_records:
                break
    if not parsed_records:
        return parse_wix_floor_plans(
            _bounded_card_texts(html),
            source_url,
            verified_plan_only=verified_plan_only,
        )

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, Any, Any]] = set()
    for parsed in parsed_records:
        key = (
            _clean_text(parsed.get("record_id")).casefold(),
            _clean_text(parsed.get("plan_name")).casefold(),
            parsed.get("rent_low"),
            parsed.get("rent_high"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(
            _row_from_parsed_record(
                parsed,
                source_url,
                verified_plan_only=verified_plan_only,
            )
        )
    return out


def parse_wix_floor_plans(
    cards: list[dict[str, Any]],
    url: str,
    *,
    verified_plan_only: bool = False,
) -> list[dict[str, Any]]:
    """Convert bounded Wix records to source-faithful plan rows."""

    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for card in cards:
        if not isinstance(card, Mapping):
            continue
        parsed = _parse_card_text(str(card.get("text") or ""))
        if not parsed:
            continue
        parsed["plan_name"] = parsed["code"] or parsed["title"]
        parsed["record_id"] = _clean_text(card.get("record_id"))
        key = (
            parsed["title"].casefold(),
            parsed["code"].casefold(),
            parsed["rent_low"],
            parsed["rent_high"],
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(
            _row_from_parsed_record(
                parsed,
                url,
                verified_plan_only=verified_plan_only,
            )
        )
    return out


def _bounded_card_texts(html: str) -> list[dict[str, str]]:
    """Return the smallest semantic Wix containers that parse as one plan."""

    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for element in soup.find_all(("div", "section", "article", "li")):
        text = _clean_text(" ".join(element.stripped_strings))
        if text in seen or len(text) < 18 or len(text) > 700:
            continue
        if _parse_card_text(text) is None:
            continue
        # Prefer leaf record boundaries.  If a child is itself a complete
        # record, the parent is a duplicate/wrapper and is not a new plan.
        child_is_record = False
        for child in element.find_all(("div", "section", "article", "li"), recursive=False):
            child_text = _clean_text(" ".join(child.stripped_strings))
            if child_text != text and _parse_card_text(child_text) is not None:
                child_is_record = True
                break
        if child_is_record:
            continue
        seen.add(text)
        candidates.append({"tag": element.name.upper(), "text": text})
    return candidates


def _normal_host(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold()
    return host[4:] if host.startswith("www.") else host


def _discover_same_site_plan_links(html: str, current_url: str) -> list[_PlanPageLink]:
    """Find only operator-labeled internal plan/pricing pages."""

    if not html or not current_url:
        return []
    current_host = _normal_host(current_url)
    current_clean = current_url.split("#", 1)[0].rstrip("/")
    primary: list[_PlanPageLink] = []
    bedroom_routes: list[_PlanPageLink] = []
    seen: set[str] = set()
    for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        label = _clean_text(
            " ".join(anchor.stripped_strings) or anchor.get("aria-label") or ""
        )
        is_primary = bool(_PLAN_LINK_LABEL_RE.fullmatch(label))
        is_bedroom_route = bool(_BEDROOM_PLAN_LINK_RE.fullmatch(label))
        if not is_primary and not is_bedroom_route:
            continue
        candidate = urljoin(current_url, str(anchor.get("href") or ""))
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or _normal_host(candidate) != current_host:
            continue
        clean = parsed._replace(fragment="").geturl().rstrip("/")
        if clean == current_clean or clean in seen:
            continue
        seen.add(clean)
        # A page explicitly labeled PRICING is authoritative for category
        # rent ranges.  FLOOR PLANS remains authoritative for authored plan
        # codes/maps.  Bellagio currently publishes both pages with different
        # ranges, so unioning by URL would duplicate every category.
        if is_bedroom_route:
            bedroom_routes.append(_PlanPageLink(clean, 220, label))
            continue
        if re.fullmatch(r"pricing", label, re.IGNORECASE):
            priority = 300
        elif re.fullmatch(r"apartments", label, re.IGNORECASE):
            priority = 150
        else:
            priority = 200
        primary.append(_PlanPageLink(clean, priority, label))

    # A lone "One Bedroom" link can be ordinary marketing navigation. Two or
    # more sibling bedroom labels are an authored plan-route catalogue (the
    # current Westgate shape) and remain bounded by the global page budget.
    if len({link.label.casefold() for link in bedroom_routes}) < 2:
        bedroom_routes = []
    return (primary + bedroom_routes)[:_MAX_INTERNAL_PLAN_PAGES]


def _extract_plan_codes(html: str) -> set[str]:
    """Read Wix gallery-authored plan codes used to bind a linked map."""

    if not html:
        return set()
    soup = BeautifulSoup(html, "html.parser")
    out: set[str] = set()
    for element in soup.select(
        '[data-testid="gallery-item-title"], [data-testid="gallery-item-item"] [aria-label]'
    ):
        value = _clean_text(element.get("aria-label") or " ".join(element.stripped_strings))
        if _PLAN_CODE_RE.fullmatch(value):
            out.add(value.upper())
    return out


def _extract_labeled_3dplans_links(html: str, marketing_url: str) -> list[_MapLink]:
    """Accept only exact, visibly labeled 3DPlans availability links."""

    if not html:
        return []
    out: dict[str, _MapLink] = {}
    for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        label = _clean_text(
            anchor.get("aria-label") or " ".join(anchor.stripped_strings) or ""
        )
        if not _AVAILABLE_MAP_LABEL_RE.search(label):
            continue
        candidate = urljoin(marketing_url, str(anchor.get("href") or ""))
        parsed = urlparse(candidate)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").casefold() != _MAP_HOST
            or parsed.path.casefold().rstrip("/") != _MAP_PATH
        ):
            continue
        query = {key.casefold(): values for key, values in parse_qs(parsed.query).items()}
        guid = _clean_text((query.get("id") or [""])[0]).casefold()
        if not _UUID_RE.fullmatch(guid):
            continue
        clean = parsed._replace(fragment="").geturl()
        out.setdefault(guid, _MapLink(clean, guid, label, marketing_url))
    return list(out.values())


def _fetch_public_html(url: str) -> _HtmlSource | None:
    """Fetch one bounded same-property page.  Kept separate for fixtures."""

    try:
        from ma_poc.pms.adapters._probe import probe_get

        response = probe_get(url, unlocker=False, timeout=20)
        status = int(getattr(response, "status_code", 0) or 0)
        body = str(getattr(response, "text", "") or "")
        final_url = str(getattr(response, "url", "") or url)
        if status == 200 and body and _normal_host(final_url) == _normal_host(url):
            return _HtmlSource(final_url, body, status)
    except Exception as exc:
        log.debug("wix internal plan-page fetch failed url=%s err=%s", url, exc)
    return None


def _fetch_result_html(ctx: AdapterContext) -> str:
    fetch_result = getattr(ctx, "fetch_result", None)
    body = getattr(fetch_result, "body", None) if fetch_result is not None else None
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return body if isinstance(body, str) else ""


async def _initial_html(page: Page, ctx: AdapterContext) -> str:
    body = _fetch_result_html(ctx)
    if body:
        return body
    content = getattr(page, "content", None)
    if callable(content):
        try:
            value = await content()
            if isinstance(value, str):
                return value
        except Exception:
            pass
    return ""


def _variables_from_request_body(body: Any) -> Mapping[str, Any]:
    if not isinstance(body, Mapping):
        return {}
    screen_data = body.get("screenData")
    if not isinstance(screen_data, Mapping):
        return {}
    variables = screen_data.get("variables")
    return variables if isinstance(variables, Mapping) else {}


async def _capture_3dplans_response(response: Any) -> _MapCapture | None:
    url = str(getattr(response, "url", "") or "")
    low = url.casefold()
    if "apps.3dplans.com/interactivepropertymap/screenservices/" not in low:
        return None
    if "dataactiongetunits" not in low:
        return None
    try:
        body = await response.json()
    except Exception:
        return None
    request = getattr(response, "request", None)
    request_body: Any = None
    if request is not None:
        try:
            request_body = request.post_data_json
            if callable(request_body):
                request_body = request_body()
        except Exception:
            request_body = None
    return _MapCapture(
        url=url,
        status=int(getattr(response, "status", 0) or 0),
        body=body,
        request_body=request_body,
    )


async def _browser_3dplans_captures(page: Page, link: _MapLink) -> tuple[list[_MapCapture], str]:
    """Open one exact map and retain only its unit-response envelopes."""

    context = getattr(page, "context", None)
    new_page = getattr(context, "new_page", None) if context is not None else None
    if not callable(new_page):
        return [], "3dplans: browser context unavailable"

    map_page: Any = None
    captures: list[_MapCapture] = []
    tasks: list[asyncio.Task[Any]] = []

    def on_response(response: Any) -> None:
        if len(tasks) >= _MAX_MAP_RESPONSES:
            return
        tasks.append(asyncio.create_task(_capture_3dplans_response(response)))

    try:
        map_page = await new_page()
        register = getattr(map_page, "on", None)
        if not callable(register):
            return [], "3dplans: response capture unavailable"
        register("response", on_response)
        await map_page.goto(link.url, wait_until="domcontentloaded", timeout=30000)
        wait = getattr(map_page, "wait_for_timeout", None)
        if callable(wait):
            await wait(3500)
        if tasks:
            for value in await asyncio.gather(*tasks, return_exceptions=True):
                if isinstance(value, _MapCapture):
                    captures.append(value)
        return captures, ""
    except Exception as exc:
        return [], f"3dplans: {type(exc).__name__}: {str(exc)[:120]}"
    finally:
        if map_page is not None:
            try:
                await map_page.close()
            except Exception:
                pass


def _current_property(captures: list[_MapCapture]) -> tuple[dict[str, Any] | None, set[str]]:
    properties: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    request_property_ids: set[str] = set()
    for capture in captures:
        variables = _variables_from_request_body(capture.request_body)
        current = variables.get("CurrentProperty")
        if isinstance(current, Mapping):
            item = {str(key): value for key, value in current.items()}
            key = (
                _clean_text(item.get("PropertyGUID")).casefold(),
                _clean_text(item.get("Id")),
                _clean_text(item.get("Name")).casefold(),
                _clean_text(item.get("Address")).casefold(),
            )
            properties[key] = item
        property_id = _clean_text(variables.get("PropertyId"))
        if property_id and property_id != "0":
            request_property_ids.add(property_id)
    if len(properties) != 1:
        return None, request_property_ids
    return next(iter(properties.values())), request_property_ids


def _property_units(captures: list[_MapCapture]) -> tuple[list[dict[str, Any]], list[_MapCapture], int]:
    by_id: dict[str, dict[str, Any]] = {}
    producing: list[_MapCapture] = []
    maximum_total = 0
    for capture in captures:
        body = capture.body
        data = body.get("data") if isinstance(body, Mapping) else None
        if not isinstance(data, Mapping):
            continue
        wrapper = data.get("PropertyUnits")
        rows = wrapper.get("List") if isinstance(wrapper, Mapping) else None
        if not isinstance(rows, list) or not rows:
            continue
        maximum_total = max(maximum_total, int(data.get("TotalCount") or 0))
        accepted_from_response = False
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            native_id = _clean_text(row.get("Id"))
            if not native_id or native_id == "0":
                continue
            by_id[native_id] = dict(row)
            accepted_from_response = True
        if accepted_from_response:
            producing.append(capture)
    return list(by_id.values()), producing, maximum_total


def _parse_3dplans_units(
    *,
    captures: list[_MapCapture],
    link: _MapLink,
    plan_codes: set[str],
    ctx: AdapterContext,
) -> tuple[list[dict[str, Any]], list[_MapCapture], dict[str, Any] | None, str]:
    """Property/catalogue-bind a current 3DPlans roster or fail closed."""

    current, request_property_ids = _current_property(captures)
    if not current:
        return [], [], None, "3dplans: missing or conflicting provider property metadata"
    provider_guid = _clean_text(current.get("PropertyGUID")).casefold()
    provider_property_id = _clean_text(current.get("Id"))
    if provider_guid != link.guid:
        return [], [], None, "3dplans: marketing-link GUID disagrees with provider property"
    if request_property_ids and request_property_ids != {provider_property_id}:
        return [], [], None, "3dplans: unit request property id disagrees with provider property"
    if not plan_codes:
        return [], [], None, "3dplans: Wix plan catalogue missing; map cannot be bound"

    from ma_poc.pms.property_identity import MATCH, evaluate_from_context

    decision = evaluate_from_context(
        ctx,
        observed_name=current.get("Name"),
        observed_address=current.get("Address"),
        observed_city=current.get("City"),
        observed_zip=current.get("Zip"),
    )
    if decision.status != MATCH:
        return [], [], decision.to_dict(), f"3dplans: property identity {decision.status}"

    email_link = _clean_text(current.get("EmailLink"))
    if email_link and _normal_host(email_link) != _normal_host(ctx.base_url):
        return [], [], decision.to_dict(), "3dplans: provider marketing host disagrees with configured host"

    raw_rows, producing, maximum_total = _property_units(captures)
    if maximum_total and maximum_total > len(raw_rows):
        return [], [], decision.to_dict(), "3dplans: roster pagination incomplete"

    out: list[dict[str, Any]] = []
    for raw in raw_rows:
        plan = _clean_text(raw.get("FloorPlanName")).upper()
        if not plan or plan not in plan_codes:
            return [], [], decision.to_dict(), f"3dplans: unit plan {plan or '<missing>'} absent from Wix catalogue"
        native_id = _clean_text(raw.get("Id"))
        full_label = _clean_text(raw.get("UnitNumber"))
        rent = money_to_int(raw.get("ActualPrice"))
        available_date = _clean_text(raw.get("ActualAvailDate"))
        if not native_id or not full_label or not rent:
            continue
        if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", available_date):
            continue
        source_ids = {
            "three_d_plans_unit_id": native_id,
            "three_d_plans_floor_plan_id": _clean_text(raw.get("FloorPlanID")),
            "three_d_plans_property_guid": provider_guid,
            "three_d_plans_property_id": provider_property_id,
        }
        location_id = _clean_text(raw.get("LocationId"))
        if location_id and location_id != "0":
            source_ids["three_d_plans_location_id"] = location_id
        out.append(
            make_unit_dict(
                floor_plan_name=plan,
                bed_label=bed_label_from(raw.get("BedRoomCount"), plan),
                bedrooms=_clean_text(raw.get("BedRoomCount")),
                bathrooms=_clean_text(raw.get("BathCount")),
                sqft=_clean_text(raw.get("AreaSqFt")),
                unit_number=full_label,
                unit_name=full_label,
                rent_low=rent,
                rent_high=rent,
                rent_range=format_rent_range(rent, rent),
                availability_status="AVAILABLE",
                availability_date=available_date,
                source_api_url=producing[-1].url if producing else link.url,
                extraction_tier=_MAP_TIER,
                source_ids=source_ids,
            )
        )
    identity = decision.to_dict()
    identity.update(
        {
            "three_d_plans_property_guid": provider_guid,
            "three_d_plans_property_id": provider_property_id,
            "provider_property_name": _clean_text(current.get("Name")),
            "provider_address": _clean_text(current.get("Address")),
            "provider_city": _clean_text(current.get("City")),
            "provider_zip": _clean_text(current.get("Zip")),
            "provider_marketing_url": email_link or None,
            "wix_marketing_page": link.marketing_url,
            "wix_plan_code_count": len(plan_codes),
        }
    )
    return out, producing, identity, ""


class WixFloorPlansAdapter:
    """Bounded Wix plan records plus exact labeled 3DPlans maps."""

    pms_name: str = "wix_floor_plans"
    _fingerprints: list[str] = ["static.parastorage.com", "wix.com", "wixstatic.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_PLAN_TIER)
        current_url = self._winning_url(page, ctx)
        html = await _initial_html(page, ctx)
        sources: list[_HtmlSource] = []
        if html:
            sources.append(_HtmlSource(current_url or ctx.base_url, html, 200))
            for candidate in _discover_same_site_plan_links(html, current_url or ctx.base_url):
                fetched = await asyncio.to_thread(_fetch_public_html, candidate.url)
                if fetched and all(fetched.url != source.url for source in sources):
                    sources.append(
                        _HtmlSource(
                            fetched.url,
                            fetched.html,
                            fetched.status,
                            candidate.semantic_priority,
                            candidate.label,
                        )
                    )

        plan_codes: set[str] = set()
        map_links: dict[str, _MapLink] = {}
        for source in sources:
            plan_codes.update(_extract_plan_codes(source.html))
            for link in _extract_labeled_3dplans_links(source.html, source.url):
                map_links.setdefault(link.guid, link)

        if len(map_links) > 1:
            result.errors.append("wix_floor_plans: multiple distinct 3DPlans property GUIDs; failing map closed")
            map_links.clear()

        verified_plan_only = bool(sources and not map_links)
        rows_by_url: dict[str, tuple[_HtmlSource, list[dict[str, Any]]]] = {}
        for source in sources:
            rows = extract_wix_authored_plan_rows(
                source.html,
                source.url,
                verified_plan_only=verified_plan_only,
            )
            if rows:
                rows_by_url[source.url] = (source, rows)

        # Compatibility fallback for live page stubs without content().  It
        # cannot assert a complete plan-only surface because no source boundary
        # was observed.
        if not rows_by_url and not sources:
            evaluate = getattr(page, "evaluate", None)
            if callable(evaluate):
                try:
                    payload = await evaluate(_WIX_DOM_JS)
                except Exception as exc:
                    log.debug("wix_floor_plans evaluate failed err=%s", exc)
                    payload = None
                if isinstance(payload, Mapping) and isinstance(payload.get("cards"), list):
                    fallback_source = _HtmlSource(current_url or ctx.base_url, "", 200)
                    rows_by_url[current_url or ctx.base_url] = (
                        fallback_source,
                        parse_wix_floor_plans(
                            list(payload["cards"]),
                            current_url or ctx.base_url,
                        ),
                    )

        # Reconcile duplicate authored categories by semantic page role.  A
        # PRICING page wins rent conflicts; a FLOOR PLANS page can still add
        # distinct categories and contributes codes/map links separately.
        best_plan_rows: dict[str, tuple[int, dict[str, Any]]] = {}
        for source, rows in rows_by_url.values():
            for row in rows:
                key = _clean_text(row.get("floor_plan_name")).casefold()
                previous = best_plan_rows.get(key)
                if previous is None or source.semantic_priority > previous[0]:
                    row["wix_source_role"] = source.role
                    best_plan_rows[key] = (source.semantic_priority, row)
        raw_rows: list[dict[str, Any]] = [value[1] for value in best_plan_rows.values()]

        producing: list[_MapCapture] = []
        map_identity: dict[str, Any] | None = None
        if map_links:
            link = next(iter(map_links.values()))
            captures, capture_error = await _browser_3dplans_captures(page, link)
            if capture_error:
                result.errors.append(capture_error)
            if captures:
                map_rows, producing, map_identity, parse_error = _parse_3dplans_units(
                    captures=captures,
                    link=link,
                    plan_codes=plan_codes,
                    ctx=ctx,
                )
                if parse_error:
                    result.errors.append(parse_error)
                raw_rows.extend(map_rows)

        if not raw_rows:
            result.confidence = 0.0
            if not result.errors:
                result.errors.append("wix_floor_plans: no bounded plan records or bound unit map")
            return result

        from ma_poc.extraction.post_process import post_process

        pp = post_process(raw_rows, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted == 0:
            result.confidence = 0.0
            result.errors.append(
                f"wix_floor_plans: {len(raw_rows)} rows failed unit_validity post-process"
            )
            return result

        result.units = pp.admitted
        result.plan_summaries = pp.plan_summaries
        if pp.n_unit_level:
            result.tier_used = _MAP_TIER
            result.winning_url = producing[-1].url if producing else next(iter(map_links.values())).url
            result.confidence = min(0.94, 0.82 + 0.03 * pp.n_unit_level)
            if producing and map_identity is not None:
                from ma_poc.pms.source_provenance import (
                    build_unit_source_provenance,
                    response_sha256,
                )

                source_url = producing[-1].url
                bodies = [capture.body for capture in producing]
                result.api_responses.append(
                    {
                        "url": source_url,
                        "status": producing[-1].status,
                        "body": "<3dplans-unit-roster>",
                        "response_sha256": response_sha256(bodies),
                        "identity": map_identity,
                        "via": "wix_labeled_3dplans_map",
                    }
                )
                result.unit_source_provenance.append(
                    build_unit_source_provenance(
                        provider="3dplans",
                        source_url=source_url,
                        body=bodies,
                        unit_count=pp.n_unit_level,
                        identity=map_identity,
                        status=producing[-1].status,
                    )
                )
        else:
            result.winning_url = next(iter(rows_by_url), current_url or ctx.base_url)
            result.confidence = min(0.88, 0.65 + 0.04 * pp.n_plan_level)
        return result

    @staticmethod
    def _winning_url(page: Page, ctx: AdapterContext) -> str:
        try:
            return str(page.url or getattr(ctx, "base_url", "") or "")
        except Exception:
            return str(getattr(ctx, "base_url", "") or "")

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
