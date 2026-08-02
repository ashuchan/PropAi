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

Newer Jonah templates also serialize every listed apartment into an SSR
``script[data-jd-fp-selector="unit-data"]`` block on the per-plan page.  That
surface is preferable when present: it carries the canonical apartment,
base-rent, plan, dimensions, and date without a browser click.  This module
parses both surfaces but does NOT perform navigation; the recovery caller owns
the bounded plain-HTTP plan drill.

NOTE — RealPage-Online-Leasing variant: the same visual template
sometimes wraps a RealPage onlineleasing portal (see
``rent-portofino.com``) instead of the Jonah widget; in that case the
``_pms_portal_hop`` recovery is the correct path, not this one. The
detection signal that selects this parser is the **Jonah Digital marker**
(``JonahWidget|meetelise|jonahdigital`` token in the page HTML).
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import math
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters._parsing import (
    is_junk_unit_number,
    make_unit_dict,
    money_to_int,
    parse_rent_range,
)

log = logging.getLogger(__name__)

_TIER = "TIER_1_DOM_ENCORESKYLINE_TEMPLATE"
JONAH_SSR_TIER = "TIER_1_DOM_JONAH_SSR_UNITS"
_JONAH_RESOURCE_TIER = "TIER_1_DOM_JONAH_RESOURCE_JSON"
JONAH_MAX_PLAN_URLS = 30

# Detection markers — set on every encoreskyline-family page by the
# Jonah Digital data layer at load. Stable across the verified sites.
_JONAH_MARKERS = (
    "jonahwidget",
    "jonahdigital",
    "meetelise",
)

# Modern Jonah Digital floor-plan pages publish the complete live roster in
# one exact, server-rendered JSON resource.  Keep the selector deliberately
# narrow: other ``application/json`` scripts on a marketing page describe
# navigation, SEO, or analytics and must never be interpreted as apartments.
_JONAH_RESOURCE_SCRIPT_RE = re.compile(
    r"<script\b(?=[^>]*\bid\s*=\s*[\"']jd-fp-data-script-resource[\"'])"
    r"[^>]*>(?P<payload>.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
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


_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_GENERATOR_NAME_RE = re.compile(
    r"\bname\s*=\s*(?:[\"']generator[\"']|generator)(?=\s|/?>)",
    re.IGNORECASE,
)


def is_strong_jonah_generator_page(html: str) -> bool:
    """Return whether HTML declares Jonah as its CMS generator.

    ``meetelise`` alone is not a safe cross-label recovery gate because many
    unrelated sites embed the Elise chat widget.  The July-31 cohort's stable
    template signal is the much narrower ``<meta name="generator"
    content="Jonah Systems|Jonah Digital ...">`` declaration.  Attribute
    order and quote style are intentionally ignored.
    """
    if not html:
        return False
    for raw_tag in _META_TAG_RE.findall(html):
        tag = html_lib.unescape(raw_tag)
        low = tag.lower()
        if not _GENERATOR_NAME_RE.search(tag):
            continue
        if "jonah systems" in low or "jonah digital" in low:
            return True
    return False


_PLAN_HREF_RE = re.compile(
    r"\bhref\s*=\s*[\"'](?P<href>[^\"']*/floorplans/[a-z0-9-]+/?(?:[?#][^\"']*)?)[\"']",
    re.IGNORECASE,
)


def jonah_plan_urls_from_html(
    html: str,
    base_url: str,
    *,
    limit: int = JONAH_MAX_PLAN_URLS,
) -> list[str]:
    """Extract bounded Jonah ``/floorplans/{slug}/`` detail URLs.

    The matcher supports both root-level and nested community paths (for
    example ``/apartments/florida/foo/floorplans/a1/``).  It only returns
    HTTP(S) URLs and strips query/fragment noise; the recovery caller applies
    the stricter same-host boundary after redirects are known.
    """
    if not html or not base_url or limit <= 0:
        return []
    seen: dict[str, None] = {}
    for match in _PLAN_HREF_RE.finditer(html):
        href = html_lib.unescape(match.group("href")).strip()
        absolute = urljoin(base_url, href)
        try:
            parts = urlsplit(absolute)
        except ValueError:
            continue
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            continue
        clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        seen.setdefault(clean, None)
        if len(seen) >= limit:
            break
    return list(seen)


_JONAH_UNIT_SCRIPT_RE = re.compile(
    r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)
_JONAH_UNIT_SELECTOR_RE = re.compile(
    r"\bdata-jd-fp-selector\s*=\s*(?:[\"']unit-data[\"']|unit-data)(?=\s|$)",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _json_number_to_rent(value: Any) -> int | None:
    """Parse one numeric JSON rent while rejecting booleans/non-finite values."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return None
        parsed = int(float(value))
    else:
        parsed = money_to_int(str(value))
    if parsed is None or not 200 <= parsed <= 50_000:
        return None
    return parsed


def _jonah_ssr_base_rent(payload: dict[str, Any]) -> tuple[int, int] | None:
    """Return explicit fee-free base rent, never a gross/display total.

    Jonah's ``price_entity.adjusted.low_no_fees`` is authoritative.  Older
    payloads only expose ``rent_min``/``rent_max``; those are accepted when
    the payload does not say its displayed price reflects fees.  Generic
    ``priceLow``/``price``/``price_display`` fields are deliberately ignored.
    """
    price_entity = payload.get("price_entity")
    entity = price_entity if isinstance(price_entity, dict) else {}
    adjusted_raw = entity.get("adjusted")
    adjusted = adjusted_raw if isinstance(adjusted_raw, dict) else {}

    low = _json_number_to_rent(adjusted.get("low_no_fees"))
    high = _json_number_to_rent(adjusted.get("high_no_fees"))
    if low is not None:
        if high is None or high < low:
            high = low
        return low, high

    reflects_fees = entity.get("pricingReflectFees")
    if reflects_fees is True or str(reflects_fees).strip().lower() in {"1", "true"}:
        return None
    low = _json_number_to_rent(payload.get("rent_min"))
    if low is None:
        return None
    high = _json_number_to_rent(payload.get("rent_max"))
    if high is None or high < low:
        high = low
    return low, high


def _jonah_availability_date(payload: dict[str, Any]) -> str:
    entity_raw = payload.get("price_entity")
    entity = entity_raw if isinstance(entity_raw, dict) else {}
    iso = str(entity.get("date") or "").strip()
    if _ISO_DATE_RE.fullmatch(iso):
        return iso

    raw_epoch = payload.get("available_date")
    try:
        if isinstance(raw_epoch, bool) or raw_epoch in (None, ""):
            raise ValueError
        epoch = int(float(str(raw_epoch)))
        if epoch > 10_000_000_000:  # defensive epoch-ms support
            epoch //= 1000
        return datetime.fromtimestamp(epoch, tz=UTC).date().isoformat()
    except (OverflowError, OSError, TypeError, ValueError):
        pass

    display = str(payload.get("available_display") or "").strip()
    return re.sub(r"^available\s+", "", display, flags=re.IGNORECASE)


def _jonah_unit_identity(payload: dict[str, Any]) -> tuple[str, str] | None:
    apartment = str(payload.get("apartment_number") or "").strip()
    building = str(payload.get("building") or "").strip()
    if not apartment:
        return None
    identity = apartment
    if is_junk_unit_number(identity):
        # Some garden-style Jonah feeds publish a letter apartment within a
        # numbered building (building=05, apartment=A).  Neither token is a
        # unit alone, but their source-provided composite is canonical.
        identity = f"{building}-{apartment}" if building else ""
    if not identity or is_junk_unit_number(identity):
        return None
    return identity, building


def parse_jonah_ssr_units(html: str, source_url: str) -> list[dict[str, Any]]:
    """Parse Jonah's SSR ``unit-data`` JSON scripts into canonical units.

    Admission is intentionally narrow: exact selector, ``type=unit``, a real
    apartment identity, and an explicit numeric base rent are all required.
    Floor-plan scripts, gross-price-only rows, malformed JSON, and waitlist
    shells therefore return no units.  The function is pure and never fetches.
    """
    if not html or "unit-data" not in html:
        return []

    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _JONAH_UNIT_SCRIPT_RE.finditer(html):
        if not _JONAH_UNIT_SELECTOR_RE.search(match.group("attrs")):
            continue
        try:
            payload = json.loads(html_lib.unescape(match.group("body")).strip())
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or str(payload.get("type") or "").lower() != "unit":
            continue

        identity = _jonah_unit_identity(payload)
        rents = _jonah_ssr_base_rent(payload)
        if identity is None or rents is None:
            continue
        unit_number, building = identity
        public_apartment = str(payload.get("apartment_number") or "").strip()
        id_value = str(payload.get("id_value") or "").strip()
        record_id = str(payload.get("id") or "").strip()
        unit_slug = str(payload.get("slug") or "").strip()
        property_id = str(payload.get("property_id") or "").strip()
        floorplan_id = str(payload.get("floorplan_id") or "").strip()
        native_unit_id = id_value or record_id or unit_slug
        source_ids: dict[str, str] = {}
        if id_value:
            source_ids["jonah_id_value"] = id_value
        if record_id:
            source_ids["jonah_record_id"] = record_id
        if unit_slug:
            source_ids["jonah_unit_slug"] = unit_slug
        if property_id:
            source_ids["jonah_property_id"] = property_id
        if floorplan_id:
            source_ids["jonah_floorplan_id"] = floorplan_id

        # Current SSR rows publish three exact native identifiers. Prefer the
        # provider's id_value before any mutable visible/building composite;
        # retain the prior display key only as a bounded legacy fallback.
        key = (
            f"native:{native_unit_id}"
            if native_unit_id
            else f"display:{building.casefold()}|{unit_number.casefold()}"
        )
        if key in seen:
            continue

        row = make_unit_dict(
            floor_plan_name=str(payload.get("floorplan_title") or "").strip(),
            bedrooms=str(payload.get("bedrooms") or "").strip(),
            bathrooms=str(payload.get("bathrooms") or "").strip(),
            sqft=str(payload.get("square_feet") or "").strip(),
            unit_number=unit_number,
            unit_name=public_apartment or unit_number,
            floor=str(payload.get("floor") or "").strip(),
            building=building,
            rent_low=rents[0],
            rent_high=rents[1],
            availability_status="AVAILABLE",
            availability_date=_jonah_availability_date(payload),
            source_api_url=source_url,
            extraction_tier=JONAH_SSR_TIER,
            source_ids=source_ids or None,
        )
        if native_unit_id:
            row["unit_id"] = native_unit_id
        if property_id:
            row["source_property_id"] = property_id
        if source_ids:
            row["source_property_provenance"] = "jonah_ssr_unit_data"
        if not unit_has_real_anchor(row):
            continue
        seen.add(key)
        units.append(row)
    return units


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


_RENTPRESS_ATTR_RE = re.compile(r"""data-floorplans\s*=\s*(["'])(?P<json>.*?)\1""", re.DOTALL)


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


def _jonah_date(unit: dict[str, Any]) -> str:
    """Return Jonah's published move-in date as ``YYYY-MM-DD``.

    The selected lease price carries the authoritative ISO date.  Older
    Jonah payloads expose only epoch seconds in ``available_date``; accept
    that as a deterministic fallback (and tolerate epoch milliseconds).
    """
    price_entity = unit.get("price_entity")
    if isinstance(price_entity, dict):
        raw_iso = str(price_entity.get("date") or "").strip()
        if raw_iso:
            iso = _rp_iso(raw_iso[:10])
            if iso:
                return iso

    raw_epoch = unit.get("available_date")
    try:
        epoch = float(str(raw_epoch).strip())
        if epoch > 10_000_000_000:  # milliseconds, not seconds
            epoch /= 1000.0
        return datetime.fromtimestamp(epoch, tz=UTC).date().isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


def _jonah_resource_base_rent(
    unit: dict[str, Any],
) -> tuple[int | None, int | None]:
    """Select base rent, excluding Jonah's mandatory-fee total when present.

    Modern Jonah pages may display ``$1,020.11 Total Rent`` for a unit whose
    actual base rent is ``$1,000``.  The resource explicitly publishes both;
    prefer ``adjusted.*_no_fees`` / ``priceDisplayNoFees`` and use the unit's
    ordinary rent range only when fee-exclusive values are absent.
    """
    price_entity = unit.get("price_entity")
    if isinstance(price_entity, dict):
        adjusted = price_entity.get("adjusted")
        if isinstance(adjusted, dict):
            low, _ = parse_rent_range(str(adjusted.get("low_no_fees") or ""))
            _, high = parse_rent_range(str(adjusted.get("high_no_fees") or ""))
            if low is not None or high is not None:
                return low or high, high or low

        no_fees = str(price_entity.get("priceDisplayNoFees") or "").strip()
        if no_fees:
            low, high = parse_rent_range(no_fees)
            if low is not None or high is not None:
                return low or high, high or low

    low, _ = parse_rent_range(str(unit.get("rent_min") or ""))
    _, high = parse_rent_range(str(unit.get("rent_max") or ""))
    return low or high, high or low


def parse_jonah_resource_json(html: str, source_url: str) -> list[dict[str, Any]]:
    """Parse the exact Jonah Digital per-plan JSON resource into unit rows.

    Current Jonah/GSC floor-plan pages server-render a script with id
    ``jd-fp-data-script-resource``.  Its ``units`` array contains apartment
    number, actual floor-plan name, dimensions, selected lease price, and
    availability date.  Reading this public payload is both cheaper and more
    reliable than clicking the JavaScript availability widget.

    Only the exact script id is accepted.  Malformed JSON, a non-floorplan
    envelope, or plan-level data without a ``units`` array returns ``[]``.
    """
    if not html or "jd-fp-data-script-resource" not in html:
        return []

    match = _JONAH_RESOURCE_SCRIPT_RE.search(html)
    if match is None:
        return []
    try:
        resource = json.loads(match.group("payload").strip())
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(resource, dict) or not isinstance(resource.get("units"), list):
        return []
    if str(resource.get("type") or "floorplan").lower() != "floorplan":
        return []

    root_plan = str(resource.get("title") or resource.get("label") or "").strip()
    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in resource["units"]:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("type") or "unit").lower() != "unit":
            continue
        availability_count = raw.get("availability_count")
        if str(availability_count).strip().lower() in {"0", "false"}:
            continue

        title_number = str(raw.get("title") or "").strip().lstrip("#").strip()
        engrain = raw.get("engrain_data")
        engrain = engrain if isinstance(engrain, dict) else {}
        native_id = str(
            engrain.get("unit_id") or raw.get("id_value") or raw.get("id") or raw.get("slug") or ""
        ).strip()
        unit_number = str(raw.get("apartment_number") or title_number or native_id).strip()
        if not unit_number and not native_id:
            continue

        key = native_id or unit_number
        if key in seen:
            continue
        seen.add(key)

        rent_low, rent_high = _jonah_resource_base_rent(raw)
        source_ids: dict[str, Any] = {}
        # ``engrain_data.unit_id`` is the same stable apartment identifier
        # consumed by the existing SightMap adapter and registry.  Do not
        # invent a new unmeasured Jonah identity namespace here.
        if engrain.get("unit_id") not in (None, ""):
            source_ids["sightmap_unit_id"] = str(engrain["unit_id"])

        price_entity = raw.get("price_entity")
        price_entity = price_entity if isinstance(price_entity, dict) else {}
        row = make_unit_dict(
            floor_plan_name=str(raw.get("floorplan_title") or root_plan).strip(),
            bedrooms=str(raw.get("bedrooms") or resource.get("bedrooms") or ""),
            bathrooms=str(raw.get("bathrooms") or resource.get("bathrooms") or ""),
            sqft=str(raw.get("square_feet") or resource.get("square_feet") or ""),
            unit_number=unit_number,
            floor=str(engrain.get("floor_name") or "").strip(),
            building=str(raw.get("building") or "").strip(),
            rent_low=rent_low,
            rent_high=rent_high,
            availability_status="AVAILABLE",
            availability_date=_jonah_date(raw),
            lease_term=str(price_entity.get("termDisplay") or "").strip(),
            source_api_url=source_url,
            extraction_tier=_JONAH_RESOURCE_TIER,
            source_ids=source_ids,
        )
        if source_ids.get("sightmap_unit_id"):
            # Jonah's visible apartment labels can repeat across buildings
            # (Duke Manor publishes many ``#E`` rows).  Preserve that display
            # label in unit_number but anchor canonical identity on the same
            # stable SightMap id namespace the core identity registry trusts.
            row["unit_id"] = f"sightmap_unit_id-{source_ids['sightmap_unit_id']}"
        units.append(row)
    return units


def parse_rentpress_data_floorplans(html: str, source_url: str) -> list[dict[str, Any]]:
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
            unit_no = str(u.get("unit_name") or u.get("unit_code") or u.get("unit_space_id") or "").strip()
            if not unit_no or unit_no in seen:
                continue
            seen.add(unit_no)
            rent = _int_or_none(
                str(u.get("unit_rent_best") or u.get("unit_rent_min") or u.get("unit_rent_base") or "")
            )
            _sqft = _int_or_none(str(u.get("unit_sqft") or fp.get("floorplan_sqft_min") or ""))
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
                        "AVAILABLE"
                        if str(u.get("unit_available")) in ("1", "True", "true")
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


def parse_encoreskyline_units(rendered_text: str, source_url: str) -> list[dict[str, Any]]:
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
