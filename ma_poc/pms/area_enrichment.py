"""Evidence-gated recovery of published unit area and area ranges.

Acceptance criteria (affected-386 area audit, 2026-08-02):

* recover a scalar only from a source-published scalar joined one-to-one;
* retain a published range as ``area_low``/``area_high`` without a midpoint;
* require at least three exact-label joins for a partial alternate roster;
* require a complete >=3-row bijection before using a non-identity fingerprint;
* retain the exact response URL, SHA-256 and record locator on every patch;
* use direct public HTTP only (no unlocker, solver, proxy, LLM or browser);
* preserve the original unit identity, rent, date and availability state.

The module is intentionally additive.  A failed or ambiguous join returns the
input rows unchanged and leaves a compact diagnostic on the adapter context.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag

from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.source_provenance import (
    build_unit_source_provenance,
    response_sha256,
    sanitise_source_url,
)

_AREA_MIN = 100
_AREA_MAX = 20_000
_MONEY_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_AREA_TEXT_RE = re.compile(
    r"(?P<low>(?:\d{1,2},\d{3}|\d{3,5}))"
    r"(?:\s*(?:-|–|—|to)\s*(?P<high>(?:\d{1,2},\d{3}|\d{3,5})))?"
    r"\s*(?:sq\.?\s*ft\.?|square\s+feet|sqft|sf)\b",
    re.IGNORECASE,
)
_PLAN_AREA_RE = re.compile(
    r"(?P<label>studio|\d+(?:\.5)?\s*(?:bed(?:room)?s?|br))"
    r"(?:\s*[,/|-]\s*(?P<baths>\d+(?:\.5)?)\s*(?:bath(?:room)?s?|ba))?"
    r"[^\d$]{0,45}"
    r"(?P<low>(?:\d{1,2},\d{3}|\d{3,5}))"
    r"(?:\s*(?:-|–|—|to)\s*(?P<high>(?:\d{1,2},\d{3}|\d{3,5})))?"
    r"\s*(?:sq\.?\s*ft\.?|square\s+feet|sqft|sf)\b",
    re.IGNORECASE,
)
_ROUTE_TOKEN_RE = re.compile(r"floor[\s_-]*plans?|availab", re.IGNORECASE)
_ROUTE_REJECT_RE = re.compile(
    r"(?:login|resident|portal|apply|application|contact|privacy|terms|\.pdf(?:$|\?))",
    re.IGNORECASE,
)
_APTS247_MARKER_RE = re.compile(
    r"(?:static\d*\.apts247\.info|window\.main_247|/api/v1/community_info/)",
    re.IGNORECASE,
)
_APTS247_KEY_RE = re.compile(r"^[a-f0-9]{20,80}$", re.IGNORECASE)


@dataclass(frozen=True)
class PublishedAreaRecord:
    """One source-published unit area record."""

    unit_number: str
    area_low: int
    area_high: int
    source_url: str
    source_record_locator: str
    rent_low: int | None = None
    rent_high: int | None = None
    beds: float | None = None
    baths: float | None = None
    availability_date: str = ""
    floor_plan_name: str = ""


@dataclass(frozen=True)
class PublishedPlanArea:
    """One source-published plan/family area scalar or range."""

    beds: float
    baths: float | None
    area_low: int
    area_high: int
    source_url: str
    source_record_locator: str
    floor_plan_name: str = ""


def _bounded_int(value: Any, low: int, high: int) -> int | None:
    """Parse one bounded integer without accepting booleans."""

    if isinstance(value, bool) or value in (None, ""):
        return None
    match = _NUMBER_RE.search(str(value).replace(",", ""))
    if match is None:
        return None
    try:
        parsed = int(float(match.group(0).replace(",", "")))
    except (TypeError, ValueError):
        return None
    return parsed if low <= parsed <= high else None


def _float_value(value: Any) -> float | None:
    """Parse a small non-negative bed/bath value."""

    if value in (None, ""):
        return None
    text = str(value).strip()
    if re.search(r"\bstudio\b", text, re.IGNORECASE):
        return 0.0
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match is None:
        return None
    try:
        parsed = float(match.group(0))
    except ValueError:
        return None
    return parsed if 0 <= parsed <= 10 else None


def _money_pair(value: Any) -> tuple[int | None, int | None]:
    """Return bounded low/high money values from source text."""

    values: list[int] = []
    for raw in _MONEY_RE.findall(str(value or "")):
        parsed = _bounded_int(raw, 200, 100_000)
        if parsed is not None:
            values.append(parsed)
    if not values:
        parsed = _bounded_int(value, 200, 100_000)
        return (parsed, parsed) if parsed is not None else (None, None)
    return min(values), max(values)


def _area_pair(value: Any) -> tuple[int | None, int | None]:
    """Return an exact published area pair, preserving a real range."""

    match = _AREA_TEXT_RE.search(str(value or ""))
    if match is None:
        scalar = _bounded_int(value, _AREA_MIN, _AREA_MAX)
        return (scalar, scalar) if scalar is not None else (None, None)
    low = _bounded_int(match.group("low"), _AREA_MIN, _AREA_MAX)
    high = _bounded_int(match.group("high"), _AREA_MIN, _AREA_MAX) if match.group("high") else low
    if low is None or high is None:
        return None, None
    return min(low, high), max(low, high)


def parse_published_area_pair(value: Any) -> tuple[int | None, int | None]:
    """Parse a source-published scalar/range without midpoint imputation."""

    return _area_pair(value)


def _clean_unit_label(value: Any) -> str:
    """Normalize one displayed unit label while retaining leading zeroes."""

    text = " ".join(str(value or "").strip().split())
    text = re.sub(r"^(?:apartment|apt|unit|residence)\s*#?\s*", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", "", text).casefold()


def _numeric_alias(value: Any) -> str | None:
    """Return a leading-zero-insensitive key only for a wholly numeric id."""

    clean = _clean_unit_label(value)
    if not clean.isdigit():
        return None
    return str(int(clean))


def _normal_header(value: Any) -> str:
    """Normalize a table header for exact semantic matching."""

    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _best_record(existing: PublishedAreaRecord, new: PublishedAreaRecord) -> PublishedAreaRecord:
    """Prefer the duplicate record with more corroborating fields."""

    def score(record: PublishedAreaRecord) -> int:
        return sum(
            value not in (None, "")
            for value in (
                record.rent_low,
                record.rent_high,
                record.beds,
                record.baths,
                record.availability_date,
                record.floor_plan_name,
            )
        )

    return new if score(new) > score(existing) else existing


def parse_published_unit_area_roster(html: str, source_url: str) -> list[PublishedAreaRecord]:
    """Parse exact unit/area rows from three audited first-party HTML shapes.

    Supported shapes are deliberately explicit: Windsor ``data-spaces-*``,
    property-authored ``data-unit``/``data-area`` cards, and labelled tables
    containing both a Unit/Residence column and a Sq Ft column.
    """

    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    records: list[PublishedAreaRecord] = []

    for node in soup.select("[data-spaces-unit][data-spaces-sort-area]"):
        unit_number = str(node.get("data-spaces-unit") or "").strip()
        area = _bounded_int(node.get("data-spaces-sort-area"), _AREA_MIN, _AREA_MAX)
        if not unit_number or area is None:
            continue
        rent_low, rent_high = _money_pair(node.get("data-spaces-sort-price"))
        records.append(
            PublishedAreaRecord(
                unit_number=unit_number,
                area_low=area,
                area_high=area,
                source_url=source_url,
                source_record_locator=(
                    f"data-spaces-unit-id:{node.get('data-spaces-unit-id') or unit_number}"
                ),
                rent_low=rent_low,
                rent_high=rent_high,
                beds=_float_value(node.get("data-spaces-sort-bed")),
                baths=_float_value(node.get("data-spaces-sort-bath")),
                availability_date=str(node.get("data-spaces-soonest") or "").strip(),
                floor_plan_name=str(node.get("data-spaces-sort-plan-name") or "").strip(),
            )
        )

    for index, node in enumerate(soup.select("[data-unit][data-area]")):
        unit_number = str(node.get("data-unit") or "").strip()
        area = _bounded_int(node.get("data-area"), _AREA_MIN, _AREA_MAX)
        if not unit_number or area is None:
            continue
        rent_low, rent_high = _money_pair(node.get("data-rent") or node.get("data-price"))
        records.append(
            PublishedAreaRecord(
                unit_number=unit_number,
                area_low=area,
                area_high=area,
                source_url=source_url,
                source_record_locator=f"data-unit:{unit_number}:index:{index}",
                rent_low=rent_low,
                rent_high=rent_high,
                beds=_float_value(node.get("data-bed") or node.get("data-beds")),
                baths=_float_value(node.get("data-bath") or node.get("data-baths")),
                availability_date=str(node.get("data-available") or "").strip(),
                floor_plan_name=str(node.get("data-plan") or "").strip(),
            )
        )

    for table_index, table in enumerate(soup.select("table")):
        header_row = table.select_one("thead tr") or table.select_one("tr")
        if not isinstance(header_row, Tag):
            continue
        headers = [_normal_header(cell.get_text(" ", strip=True)) for cell in header_row.select("th,td")]
        unit_index = next(
            (
                i
                for i, value in enumerate(headers)
                if value in {"unit", "unit number", "residence", "apartment"}
            ),
            None,
        )
        area_index = next(
            (
                i
                for i, value in enumerate(headers)
                if value in {"sq ft", "sqft", "square feet", "area", "unit size"}
            ),
            None,
        )
        if unit_index is None or area_index is None:
            continue
        optional = {
            "rent": next((i for i, value in enumerate(headers) if value in {"rent", "price"}), None),
            "beds": next(
                (i for i, value in enumerate(headers) if value in {"bed", "beds", "bedroom", "bedrooms"}),
                None,
            ),
            "baths": next(
                (i for i, value in enumerate(headers) if value in {"bath", "baths", "bathroom", "bathrooms"}),
                None,
            ),
            "date": next(
                (
                    i
                    for i, value in enumerate(headers)
                    if value in {"avail", "available", "availability", "available date"}
                ),
                None,
            ),
            "plan": next(
                (i for i, value in enumerate(headers) if value in {"floorplan", "floor plan", "plan"}), None
            ),
        }
        body_rows = table.select("tbody tr") or table.select("tr")[1:]
        for row_index, row in enumerate(body_rows):
            cells = [" ".join(cell.get_text(" ", strip=True).split()) for cell in row.select("th,td")]
            if max(unit_index, area_index) >= len(cells):
                continue
            unit_number = cells[unit_index].strip()
            area_low, area_high = _area_pair(cells[area_index])
            if not unit_number or area_low is None or area_high is None or area_low != area_high:
                continue

            values: dict[str, str] = {}
            for name, position in optional.items():
                values[name] = cells[position] if position is not None and position < len(cells) else ""
            rent_low, rent_high = _money_pair(values["rent"])
            records.append(
                PublishedAreaRecord(
                    unit_number=unit_number,
                    area_low=area_low,
                    area_high=area_high,
                    source_url=source_url,
                    source_record_locator=(f"table:{table_index}:row:{row_index}:unit:{unit_number}"),
                    rent_low=rent_low,
                    rent_high=rent_high,
                    beds=_float_value(values["beds"]),
                    baths=_float_value(values["baths"]),
                    availability_date=values["date"],
                    floor_plan_name=values["plan"],
                )
            )

    by_unit: dict[str, PublishedAreaRecord] = {}
    conflicts: set[str] = set()
    for record in records:
        key = _clean_unit_label(record.unit_number)
        if not key:
            continue
        prior = by_unit.get(key)
        if prior is None:
            by_unit[key] = record
        elif (prior.area_low, prior.area_high) != (record.area_low, record.area_high):
            conflicts.add(key)
        else:
            by_unit[key] = _best_record(prior, record)
    return [record for key, record in by_unit.items() if key not in conflicts]


def parse_published_plan_areas(html: str, source_url: str) -> list[PublishedPlanArea]:
    """Parse small, labelled plan captions while retaining exact ranges."""

    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[str, str]] = []
    for index, node in enumerate(soup.select(".kad_caption_inner, [data-caption], .floorplan-sqft")):
        if node.get("data-caption"):
            text = " ".join(str(node.get("data-caption") or "").split())
        else:
            text = " ".join(node.get_text(" ", strip=True).split())
        if text and len(text) <= 240:
            candidates.append((f"caption:{index}", text))

    # Some austere WordPress galleries publish only caption text.  Inspect
    # leaf nodes as a fallback, but keep the 240-character bound so a whole
    # page or amenity paragraph can never become a plan fact.
    for index, node in enumerate(soup.find_all(True)):
        if node.find(True):
            continue
        text = " ".join(node.get_text(" ", strip=True).split())
        if text and len(text) <= 240 and _PLAN_AREA_RE.search(text):
            candidates.append((f"leaf:{index}", text))

    out: list[PublishedPlanArea] = []
    seen: set[tuple[float, float | None, int, int, str]] = set()
    for locator, text in candidates:
        match = _PLAN_AREA_RE.search(text)
        if match is None:
            continue
        beds = 0.0 if match.group("label").casefold() == "studio" else (_float_value(match.group("label")))
        baths = _float_value(match.group("baths"))
        low = _bounded_int(match.group("low"), _AREA_MIN, _AREA_MAX)
        high = _bounded_int(match.group("high"), _AREA_MIN, _AREA_MAX) if match.group("high") else low
        if beds is None or low is None or high is None:
            continue
        low, high = min(low, high), max(low, high)
        key = (beds, baths, low, high, text.casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append(
            PublishedPlanArea(
                beds=beds,
                baths=baths,
                area_low=low,
                area_high=high,
                source_url=source_url,
                source_record_locator=locator,
                floor_plan_name=text,
            )
        )
    return out


def discover_published_area_urls(html: str, base_url: str) -> list[str]:
    """Return bounded authored floor-plan/availability URLs in priority order."""

    if not html or not base_url:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
        base_host = (urlparse(base_url).hostname or "").casefold()
    except Exception:
        return []
    scored: list[tuple[int, str]] = []
    for node in soup.select("iframe[src], a[href]"):
        is_iframe = node.name == "iframe"
        raw = str(node.get("src") if is_iframe else node.get("href") or "").strip()
        text = " ".join(node.get_text(" ", strip=True).split())
        if not raw or (not _ROUTE_TOKEN_RE.search(raw) and not _ROUTE_TOKEN_RE.search(text)):
            continue
        try:
            joined = urljoin(base_url, raw)
            parsed = urlparse(joined)
        except (TypeError, ValueError):
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        clean = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, ""))
        if _ROUTE_REJECT_RE.search(clean):
            continue
        same_host = (parsed.hostname or "").casefold() == base_host
        if not same_host and not is_iframe:
            continue
        score = 100 if is_iframe else 50
        if "availability" in parsed.path.casefold():
            score += 30
        if "floor" in parsed.path.casefold():
            score += 20
        scored.append((score, clean))
    ordered: list[str] = []
    for _score, url in sorted(scored, key=lambda item: (-item[0], item[1])):
        if url not in ordered:
            ordered.append(url)
    return ordered[:6]


def _row_scalar_area(unit: dict[str, Any]) -> int | None:
    """Return a real scalar already present on an internal unit row."""

    for key in ("sqft", "area", "_sqft", "square_feet", "squareFeet"):
        value = unit.get(key)
        parsed = _bounded_int(value, _AREA_MIN, _AREA_MAX)
        if parsed is not None and not re.search(r"(?:-|–|—|\bto\b)", str(value or "")):
            return parsed
    return None


def _row_area_bounds(unit: dict[str, Any]) -> tuple[int, int] | None:
    """Return a published scalar/range already present on an internal row."""

    low = _bounded_int(unit.get("area_low"), _AREA_MIN, _AREA_MAX)
    high = _bounded_int(unit.get("area_high"), _AREA_MIN, _AREA_MAX)
    if low is not None and high is not None:
        return min(low, high), max(low, high)
    scalar = _row_scalar_area(unit)
    return (scalar, scalar) if scalar is not None else None


def _unit_accepts_exact_area_refinement(unit: dict[str, Any]) -> bool:
    """Whether a unit roster may replace a coarser plan-derived area."""

    if _row_area_bounds(unit) is None:
        return True
    # Exact apartment evidence outranks a plan/family fallback. Native adapter
    # area (including a native range) is never overwritten by this enrichment.
    return str(unit.get("area_provenance") or "").startswith("published_plan_")


def _row_ids(unit: dict[str, Any]) -> list[str]:
    """Return the displayed physical-id candidates on one internal row."""

    values: list[str] = []
    for key in ("unit_number", "unit_id", "source_unit_id", "unit_name"):
        value = str(unit.get(key) or "").strip()
        if value and value not in values:
            values.append(value)
    return values


def _row_rent(unit: dict[str, Any]) -> int | None:
    """Return the row's low/base asking rent for a strict fingerprint."""

    for key in ("market_rent_low", "rent_low", "asking_rent", "rent"):
        parsed = _bounded_int(unit.get(key), 200, 100_000)
        if parsed is not None:
            return parsed
    return None


def _row_rent_high(unit: dict[str, Any]) -> int | None:
    """Return the row's high asking-rent bound for alias corroboration."""

    for key in ("market_rent_high", "rent_high", "asking_rent", "rent"):
        parsed = _bounded_int(unit.get(key), 200, 100_000)
        if parsed is not None:
            return parsed
    return _row_rent(unit)


def _row_beds(unit: dict[str, Any]) -> float | None:
    return _float_value(unit.get("bedrooms") if unit.get("bedrooms") not in (None, "") else unit.get("beds"))


def _row_baths(unit: dict[str, Any]) -> float | None:
    return _float_value(
        unit.get("bathrooms") if unit.get("bathrooms") not in (None, "") else unit.get("baths")
    )


def _same_dimension(value: float | None, source: float | None) -> bool:
    """Treat a missing corroborator as neutral, never contradictory."""

    return value is None or source is None or value == source


def _normal_plan_label(value: Any) -> str:
    """Normalize a provider plan label for exact semantic comparison."""

    return " ".join(re.findall(r"[a-z0-9.]+", str(value or "").casefold()))


def _verified_duplicate_numeric_aliases(units: list[dict[str, Any]]) -> set[str]:
    """Find identical rows whose numeric ids differ only by leading zeroes.

    This mirrors the downstream alias-collapse contract but runs early enough
    for both spellings to receive the same exact apartment area. Required
    rent/bed/bath corroborators and equality across plan, building, floor,
    availability and provider ids prevent a repeated apartment number in two
    buildings from entering the alias set.
    """

    groups: dict[str, list[dict[str, Any]]] = {}
    for unit in units:
        aliases = {_numeric_alias(value) for value in _row_ids(unit)} - {None}
        for alias in aliases:
            groups.setdefault(str(alias), []).append(unit)

    verified: set[str] = set()
    for alias, group in groups.items():
        if len(group) < 2:
            continue
        display_ids = {
            _clean_unit_label(value)
            for unit in group
            for value in _row_ids(unit)
            if _numeric_alias(value) == alias
        }
        if len(display_ids) < 2:
            continue

        fingerprints: set[tuple[Any, ...]] = set()
        source_ids: list[dict[str, Any]] = []
        valid = True
        for unit in group:
            rent_low = _row_rent(unit)
            rent_high = _row_rent_high(unit)
            beds = _row_beds(unit)
            baths = _row_baths(unit)
            if rent_low is None or rent_high is None or beds is None or baths is None:
                valid = False
                break
            fingerprints.add(
                (
                    rent_low,
                    rent_high,
                    beds,
                    baths,
                    _normal_plan_label(unit.get("floor_plan_name")),
                    str(unit.get("building") or unit.get("building_id") or "").strip().casefold(),
                    str(unit.get("floor") or "").strip().casefold(),
                    str(unit.get("available_date") or unit.get("availability_date") or "").strip(),
                    str(unit.get("availability_status") or "").strip().casefold(),
                    str(unit.get("lease_term") or "").strip(),
                )
            )
            native_ids = unit.get("source_ids")
            if isinstance(native_ids, dict) and native_ids:
                source_ids.append(native_ids)
        if not valid or len(fingerprints) != 1:
            continue
        if source_ids and (
            len(source_ids) != len(group)
            or len({json.dumps(value, sort_keys=True, default=str) for value in source_ids}) != 1
        ):
            continue
        verified.add(alias)
    return verified


def _label_matches(
    units: list[dict[str, Any]],
    records: list[PublishedAreaRecord],
) -> list[tuple[dict[str, Any], PublishedAreaRecord, str]]:
    """Return collision-free exact-label joins, then safe numeric aliases."""

    by_exact: dict[str, list[PublishedAreaRecord]] = {}
    by_numeric: dict[str, list[PublishedAreaRecord]] = {}
    for record in records:
        by_exact.setdefault(_clean_unit_label(record.unit_number), []).append(record)
        numeric = _numeric_alias(record.unit_number)
        if numeric is not None:
            by_numeric.setdefault(numeric, []).append(record)

    unit_numeric_counts: dict[str, int] = {}
    for unit in units:
        aliases = {_numeric_alias(value) for value in _row_ids(unit)}
        for alias in aliases - {None}:
            unit_numeric_counts[str(alias)] = unit_numeric_counts.get(str(alias), 0) + 1
    duplicate_aliases = _verified_duplicate_numeric_aliases(units)

    matches: list[tuple[dict[str, Any], PublishedAreaRecord, str]] = []
    used: set[str] = set()
    for unit in units:
        if not _unit_accepts_exact_area_refinement(unit):
            continue
        exact: list[PublishedAreaRecord] = []
        for value in _row_ids(unit):
            exact.extend(by_exact.get(_clean_unit_label(value), []))
        exact = list({record.source_record_locator: record for record in exact}.values())
        match: PublishedAreaRecord | None = exact[0] if len(exact) == 1 else None
        method = "exact_display_unit"
        if match is None:
            numeric_hits: list[PublishedAreaRecord] = []
            for value in _row_ids(unit):
                alias = _numeric_alias(value)
                if alias is None or (unit_numeric_counts.get(alias) != 1 and alias not in duplicate_aliases):
                    continue
                values = by_numeric.get(alias, [])
                if len(values) == 1:
                    numeric_hits.extend(values)
            numeric_hits = list({record.source_record_locator: record for record in numeric_hits}.values())
            match = numeric_hits[0] if len(numeric_hits) == 1 else None
            method = "unique_leading_zero_alias"
        matched_alias = _numeric_alias(match.unit_number) if match is not None else None
        duplicate_alias_match = matched_alias in duplicate_aliases
        if match is None or (match.source_record_locator in used and not duplicate_alias_match):
            continue
        if not _same_dimension(_row_beds(unit), match.beds) or not _same_dimension(
            _row_baths(unit), match.baths
        ):
            continue
        if duplicate_alias_match:
            method = "verified_duplicate_leading_zero_alias"
        used.add(match.source_record_locator)
        matches.append((unit, match, method))
    return matches


def _fingerprint_matches(
    units: list[dict[str, Any]],
    records: list[PublishedAreaRecord],
) -> list[tuple[dict[str, Any], PublishedAreaRecord, str]]:
    """Return a complete one-to-one rent/bed/bath roster bijection.

    This is the audited 70 Pine shape: the primary API has an opaque native
    record id while the property-authored iframe has the displayed residence.
    A partial match is never admissible.
    """

    missing = [unit for unit in units if _unit_accepts_exact_area_refinement(unit)]
    if len(missing) < 3 or len(missing) != len(records):
        return []

    def unit_key(unit: dict[str, Any]) -> tuple[int, float, float] | None:
        rent, beds, baths = _row_rent(unit), _row_beds(unit), _row_baths(unit)
        if rent is None or beds is None or baths is None:
            return None
        return rent, beds, baths

    def record_key(record: PublishedAreaRecord) -> tuple[int, float, float] | None:
        if record.rent_low is None or record.beds is None or record.baths is None:
            return None
        return record.rent_low, record.beds, record.baths

    unit_map: dict[tuple[int, float, float], list[dict[str, Any]]] = {}
    record_map: dict[tuple[int, float, float], list[PublishedAreaRecord]] = {}
    for unit in missing:
        key = unit_key(unit)
        if key is None:
            return []
        unit_map.setdefault(key, []).append(unit)
    for record in records:
        key = record_key(record)
        if key is None:
            return []
        record_map.setdefault(key, []).append(record)
    if set(unit_map) != set(record_map):
        return []
    if any(len(unit_map[key]) != 1 or len(record_map[key]) != 1 for key in unit_map):
        return []
    return [
        (unit_map[key][0], record_map[key][0], "complete_rent_bed_bath_bijection") for key in sorted(unit_map)
    ]


def _apply_area_record(
    unit: dict[str, Any],
    record: PublishedAreaRecord,
    *,
    method: str,
    body_hash: str,
) -> None:
    """Apply one exact source record without touching unrelated values."""

    unit["sqft"] = str(record.area_low)
    unit["area_low"] = record.area_low
    unit["area_high"] = record.area_high
    unit["area_range"] = (
        str(record.area_low)
        if record.area_low == record.area_high
        else f"{record.area_low}-{record.area_high}"
    )
    unit["area_provenance"] = f"published_unit_{method}"
    unit["area_source_url"] = sanitise_source_url(record.source_url)
    unit["source_response_sha256"] = body_hash
    unit["source_response_url"] = sanitise_source_url(record.source_url)
    unit["source_record_locator"] = record.source_record_locator
    source_ids = dict(unit.get("source_ids") or {})
    source_ids.setdefault("authored_unit_number", record.unit_number)
    unit["source_ids"] = source_ids
    if method == "complete_rent_bed_bath_bijection" and not unit.get("unit_name"):
        unit["unit_name"] = record.unit_number


def _apply_plan_ranges(
    units: list[dict[str, Any]],
    plans: list[PublishedPlanArea],
    *,
    body_hash: str,
    prefer_plan_name: bool = False,
) -> tuple[int, int]:
    """Attach an exact scalar or honest family range by bed/bath context."""

    exact_patched = 0
    range_patched = 0
    by_shape: dict[tuple[float, float | None], list[PublishedPlanArea]] = {}
    by_name: dict[str, list[PublishedPlanArea]] = {}
    for plan in plans:
        by_shape.setdefault((plan.beds, plan.baths), []).append(plan)
        normalized_name = _normal_plan_label(plan.floor_plan_name)
        if normalized_name:
            by_name.setdefault(normalized_name, []).append(plan)
    for unit in units:
        if _row_scalar_area(unit) is not None or unit.get("area_low") not in (None, ""):
            continue
        beds, baths = _row_beds(unit), _row_baths(unit)
        if beds is None:
            continue
        normalized_unit_name = _normal_plan_label(unit.get("floor_plan_name"))
        candidates = (
            by_name.get(normalized_unit_name, []) if prefer_plan_name and normalized_unit_name else []
        )
        exact_name_match = bool(candidates)
        if not candidates:
            candidates = by_shape.get((beds, baths), [])
        if not candidates:
            # A source may omit bath count in an otherwise explicit plan
            # caption. Accept only when that looser bed shape is unique.
            candidates = by_shape.get((beds, None), [])
        if not candidates:
            continue
        low = min(candidate.area_low for candidate in candidates)
        high = max(candidate.area_high for candidate in candidates)
        source_urls = {candidate.source_url for candidate in candidates}
        if len(source_urls) != 1:
            continue
        scalar = low == high
        if scalar:
            unit["sqft"] = str(low)
        unit["area_low"] = low
        unit["area_high"] = high
        unit["area_range"] = str(low) if scalar else f"{low}-{high}"
        unit["area_provenance"] = (
            "published_plan_name_exact"
            if scalar and exact_name_match
            else "published_plan_name_range_no_midpoint"
            if exact_name_match
            else "published_plan_shape_exact"
            if scalar
            else "published_plan_family_range_no_midpoint"
        )
        source_url = next(iter(source_urls))
        unit["area_source_url"] = sanitise_source_url(source_url)
        unit["source_response_sha256"] = body_hash
        unit["source_response_url"] = sanitise_source_url(source_url)
        unit["source_record_locator"] = ";".join(
            sorted(candidate.source_record_locator for candidate in candidates)
        )
        if scalar:
            exact_patched += 1
        else:
            range_patched += 1
    return exact_patched, range_patched


def _response_text(response: Any) -> str:
    """Read a requests-like response body without raising."""

    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    if isinstance(content, bytearray):
        return bytes(content).decode("utf-8", errors="replace")
    if isinstance(content, str):
        return content
    return ""


def _response_status(response: Any) -> int:
    """Read either requests-style or FetchResult-style HTTP status."""

    for name in ("status_code", "status"):
        try:
            value = int(getattr(response, name, 0) or 0)
        except (TypeError, ValueError):
            continue
        if value:
            return value
    return 0


def _response_content_type(response: Any) -> str | None:
    """Return a response content type when its header mapping is available."""

    headers = getattr(response, "headers", None)
    if not isinstance(headers, dict):
        return None
    value = headers.get("content-type") or headers.get("Content-Type")
    return str(value).strip() if value not in (None, "") else None


def _apts247_plan_area_enrichment(
    ctx: AdapterContext,
    adapter_result: AdapterResult,
    units: list[dict[str, Any]],
    marker_url: str,
    *,
    fetch: Callable[..., Any],
    max_fetches: int,
) -> dict[str, Any]:
    """Recover an honest Apts247 plan-family area behind another PMS route.

    Some marketing sites use On-Site for their physical availability roster
    and Apts247 for the authored floor-plan catalogue.  The two providers do
    not share a plan key, so this lane never claims an exact plan join unless
    the names agree.  Otherwise it retains the complete bed/bath family's
    published min/max as a range.  Both Apts247 responses must independently
    identify the configured property before any value is admitted.
    """

    diagnostic: dict[str, Any] = {
        "attempted": False,
        "fetch_count": 0,
        "identity": None,
        "published_plan_count": 0,
        "patched_units": 0,
        "exact_units": 0,
        "range_units": 0,
    }
    if max_fetches < 2:
        diagnostic["reason"] = "fetch_budget_exhausted"
        return diagnostic
    try:
        parsed = urlparse(marker_url)
    except (TypeError, ValueError):
        diagnostic["reason"] = "invalid_marker_url"
        return diagnostic
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        diagnostic["reason"] = "invalid_marker_origin"
        return diagnostic
    origin = f"{parsed.scheme}://{parsed.netloc}"
    info_url = f"{origin}/api/v1/community_info/?format=json"
    diagnostic["community_info_url"] = sanitise_source_url(info_url)
    diagnostic["attempted"] = True
    try:
        info_response = fetch(info_url, timeout=12, unlocker=False)
    except Exception as exc:
        diagnostic["reason"] = f"community_info_{type(exc).__name__}"
        return diagnostic
    diagnostic["fetch_count"] = 1
    info_status = _response_status(info_response)
    info_body = _response_text(info_response)
    info_identity: dict[str, Any] = {
        "status": "UNVERIFIED",
        "configured_property_id": str(getattr(ctx, "property_id", "") or ""),
        "admission_reason": "pending_identity_evaluation",
    }
    info_archive_record = {
        "url": str(getattr(info_response, "url", "") or info_url),
        "status": info_status,
        "body": info_body,
        "content_type": _response_content_type(info_response) or "application/json",
        "response_kind": "apts247_community_identity",
        "via": "published_area_enrichment",
        "identity": info_identity,
    }
    # Archive the response even when it is invalid or rejected. A failure body
    # is precisely what lets a future audit distinguish HTTP drift, schema
    # drift, and a real sibling-property mismatch without probing again.
    adapter_result.api_responses.append(info_archive_record)
    if info_status != 200 or not info_body:
        diagnostic["reason"] = f"community_info_http_{info_status}"
        info_identity["admission_reason"] = diagnostic["reason"]
        return diagnostic
    try:
        info_payload = json.loads(info_body)
    except (TypeError, ValueError, json.JSONDecodeError):
        diagnostic["reason"] = "community_info_invalid_json"
        info_identity["admission_reason"] = diagnostic["reason"]
        return diagnostic
    info_objects = info_payload.get("objects") if isinstance(info_payload, dict) else None
    if not isinstance(info_objects, list) or len(info_objects) != 1 or not isinstance(info_objects[0], dict):
        diagnostic["reason"] = "community_info_ambiguous_shape"
        info_identity["admission_reason"] = diagnostic["reason"]
        return diagnostic
    community = info_objects[0]
    from ma_poc.pms.property_identity import MATCH, evaluate_from_context

    identity = evaluate_from_context(
        ctx,
        observed_name=community.get("name"),
        observed_address=community.get("address"),
        observed_city=community.get("city"),
        observed_state=community.get("state"),
        observed_zip=community.get("zip_code"),
    )
    diagnostic["identity"] = identity.to_dict()
    common_identity = {
        **identity.to_dict(),
        "configured_property_id": str(getattr(ctx, "property_id", "") or ""),
    }
    info_archive_record["identity"] = common_identity
    if identity.status != MATCH:
        diagnostic["reason"] = "community_identity_not_match"
        common_identity["admission_reason"] = diagnostic["reason"]
        return diagnostic
    api_key = str(community.get("api_key") or "").strip()
    if _APTS247_KEY_RE.fullmatch(api_key) is None:
        diagnostic["reason"] = "community_api_key_missing"
        common_identity["admission_reason"] = diagnostic["reason"]
        return diagnostic

    floorplan_url = f"{origin}/api/v1/floorplans/?format=json&api_key={api_key}"
    diagnostic["floorplans_url"] = sanitise_source_url(floorplan_url)
    try:
        floorplan_response = fetch(floorplan_url, timeout=12, unlocker=False)
    except Exception as exc:
        diagnostic["reason"] = f"floorplans_{type(exc).__name__}"
        return diagnostic
    diagnostic["fetch_count"] = 2
    floorplan_status = _response_status(floorplan_response)
    floorplan_body = _response_text(floorplan_response)
    floorplan_archive_identity: dict[str, Any] = {
        "status": "UNVERIFIED",
        "configured_property_id": str(getattr(ctx, "property_id", "") or ""),
        "upstream_community_identity": common_identity,
        "source_record_count": 0,
        "admitted_field_count": 0,
        "admission_reason": "pending_floorplan_identity_evaluation",
    }
    floorplan_archive_record = {
        "url": str(getattr(floorplan_response, "url", "") or floorplan_url),
        "status": floorplan_status,
        "body": floorplan_body,
        "content_type": _response_content_type(floorplan_response) or "application/json",
        "response_kind": "apts247_floorplan_area_catalogue",
        "via": "published_area_enrichment",
        "identity": floorplan_archive_identity,
    }
    adapter_result.api_responses.append(floorplan_archive_record)
    if floorplan_status != 200 or not floorplan_body:
        diagnostic["reason"] = f"floorplans_http_{floorplan_status}"
        floorplan_archive_identity["admission_reason"] = diagnostic["reason"]
        return diagnostic
    try:
        floorplan_payload = json.loads(floorplan_body)
    except (TypeError, ValueError, json.JSONDecodeError):
        diagnostic["reason"] = "floorplans_invalid_json"
        floorplan_archive_identity["admission_reason"] = diagnostic["reason"]
        return diagnostic
    objects = floorplan_payload.get("objects") if isinstance(floorplan_payload, dict) else None
    if not isinstance(objects, list) or not 1 <= len(objects) <= 200:
        diagnostic["reason"] = "floorplans_invalid_shape"
        floorplan_archive_identity["admission_reason"] = diagnostic["reason"]
        return diagnostic
    floorplan_archive_identity["source_record_count"] = len(objects)

    plans: list[PublishedPlanArea] = []
    matched_plan_identities = 0
    for plan in objects:
        if not isinstance(plan, dict):
            continue
        plan_community = plan.get("community")
        if isinstance(plan_community, dict):
            plan_identity = evaluate_from_context(
                ctx,
                observed_name=plan_community.get("name"),
                observed_address=plan_community.get("address"),
                observed_city=plan_community.get("city"),
                observed_state=plan_community.get("state"),
                observed_zip=plan_community.get("zip_code"),
            )
            if plan_identity.status != MATCH:
                diagnostic["reason"] = "floorplan_property_identity_not_match"
                floorplan_archive_identity["admission_reason"] = diagnostic["reason"]
                floorplan_archive_identity["rejected_record_identity"] = plan_identity.to_dict()
                return diagnostic
            matched_plan_identities += 1
        beds = _float_value(plan.get("bed") or plan.get("display_bed"))
        baths = _float_value(plan.get("bath"))
        area_low, area_high = _area_pair(plan.get("sq_ft"))
        if beds is None or area_low is None or area_high is None:
            continue
        plans.append(
            PublishedPlanArea(
                beds=beds,
                baths=baths,
                area_low=area_low,
                area_high=area_high,
                source_url=floorplan_url,
                source_record_locator=f"apts247_plan:{plan.get('id') or len(plans)}",
                floor_plan_name=str(plan.get("name") or "").strip(),
            )
        )
    if not plans or matched_plan_identities != len(objects):
        diagnostic["reason"] = "floorplan_identity_or_area_incomplete"
        floorplan_archive_identity["admission_reason"] = diagnostic["reason"]
        return diagnostic

    floorplan_hash = response_sha256(floorplan_body)
    exact_patched, range_patched = _apply_plan_ranges(
        units,
        plans,
        body_hash=floorplan_hash,
        prefer_plan_name=True,
    )
    patched = exact_patched + range_patched
    diagnostic["published_plan_count"] = len(plans)
    diagnostic["patched_units"] = patched
    diagnostic["exact_units"] = exact_patched
    diagnostic["range_units"] = range_patched
    diagnostic["reason"] = "admitted" if patched else "no_eligible_unit_shape"
    floorplan_archive_identity.update(common_identity)
    floorplan_archive_identity["admission_reason"] = diagnostic["reason"]
    floorplan_archive_identity["admitted_field_count"] = patched

    safe_info_url = sanitise_source_url(info_url)
    safe_floorplan_url = sanitise_source_url(floorplan_url)
    if patched:
        adapter_result.unit_source_provenance.append(
            build_unit_source_provenance(
                provider="apts247_plan_area",
                source_url=safe_floorplan_url,
                body=floorplan_body,
                unit_count=patched,
                identity=common_identity,
                response_kind="plan_family_area_enrichment",
                status=floorplan_status,
            )
        )
    diagnostic["community_info_url"] = safe_info_url
    diagnostic["floorplans_url"] = safe_floorplan_url
    return diagnostic


def enrich_missing_unit_areas(
    ctx: AdapterContext,
    adapter_result: AdapterResult,
    *,
    fetch: Callable[..., Any] = probe_get,
    max_fetches: int = 4,
) -> dict[str, Any]:
    """Recover exact/range area from bounded property-authored surfaces.

    Returns a compact diagnostic.  The same object is attached to ``ctx`` so
    the runner can persist attempts and non-admissions without a live probe.
    """

    units = [unit for unit in (adapter_result.units or []) if isinstance(unit, dict)]
    missing = [unit for unit in units if _row_area_bounds(unit) is None]
    diagnostic: dict[str, Any] = {
        "attempted": False,
        "eligible_units": len(missing),
        "fetched_urls": [],
        "matched_units": 0,
        "exact_units": 0,
        "range_units": 0,
        "patched_units": 0,
        "join_method": None,
        "surface_evaluations": [],
    }
    if not missing:
        return diagnostic

    def refresh_final_patch_counts() -> None:
        """Count final unique rows, not intermediate fallback replacements."""

        exact = 0
        ranged = 0
        for unit in missing:
            bounds = _row_area_bounds(unit)
            if bounds is None:
                continue
            if bounds[0] == bounds[1]:
                exact += 1
            else:
                ranged += 1
        diagnostic["exact_units"] = exact
        diagnostic["range_units"] = ranged
        diagnostic["patched_units"] = exact + ranged

    tier = str(adapter_result.tier_used or "").upper()
    if "STATIC_RESIDENCE_TABLE" in tier:
        try:
            from ma_poc.pms.adapters._static_residence_table import (
                enrich_static_residence_asset_areas,
            )

            asset_diagnostic = enrich_static_residence_asset_areas(
                ctx,
                units,
                fetch=fetch,
            )
            diagnostic["asset_enrichment"] = asset_diagnostic
            asset_responses = list(getattr(ctx, "_static_residence_asset_responses", []) or [])
            adapter_result.asset_responses.extend(asset_responses)
            for response in asset_responses:
                adapter_result.unit_source_provenance.append(
                    build_unit_source_provenance(
                        provider="verified_floor_plan_asset",
                        source_url=str(response.get("url") or ""),
                        body=response.get("body"),
                        unit_count=1,
                        identity=response.get("identity"),
                        response_kind="verified_floor_plan_asset",
                        status=int(response.get("status") or 0),
                    )
                )
            asset_matches = int(asset_diagnostic.get("matched_units") or 0)
            diagnostic["matched_units"] += asset_matches
            diagnostic["exact_units"] += asset_matches
            diagnostic["patched_units"] = diagnostic["exact_units"] + diagnostic["range_units"]
        except Exception as exc:
            diagnostic["asset_enrichment"] = {
                "attempted": True,
                "error": type(exc).__name__,
            }

    # Limit the network lane to the two audited partial-source families. Plan
    # caption parsing still runs against the already-fetched body for every
    # adapter and therefore adds no fleet latency.
    allow_network = any(token in tier for token in ("RENTCAFE_SECURECAFE", "ONSITE_APPLY"))
    fr = getattr(ctx, "fetch_result", None)
    raw = getattr(fr, "body", None) if fr is not None else None
    if isinstance(raw, bytes):
        current_html = raw.decode("utf-8", errors="replace")
    else:
        current_html = raw if isinstance(raw, str) else ""
    current_url = str(
        (getattr(fr, "final_url", "") if fr is not None else "") or getattr(ctx, "base_url", "") or ""
    )
    if not current_html or not current_url:
        refresh_final_patch_counts()
        return diagnostic

    diagnostic["attempted"] = True
    surfaces: list[tuple[str, str, int, str]] = [(current_url, current_html, 200, "primary_fetch")]

    def remember_html_response(
        *,
        source_url: str,
        body: str,
        status: int,
        via: str,
        response_kind: str,
        identity: dict[str, Any],
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Upsert one bounded probe body for the offline source manifest."""

        body_hash = response_sha256(body)
        safe_url = sanitise_source_url(source_url)
        for existing in adapter_result.html_responses:
            if not isinstance(existing, dict):
                continue
            if sanitise_source_url(str(existing.get("url") or "")) != safe_url:
                continue
            if response_sha256(existing.get("body")) != body_hash:
                continue
            existing.update(
                {
                    "status": status,
                    "content_type": content_type or existing.get("content_type") or "text/html",
                    "response_kind": response_kind,
                    "via": via,
                    "identity": identity,
                }
            )
            return existing
        record = {
            "url": source_url,
            "status": status,
            "body": body,
            "content_type": content_type or "text/html",
            "response_kind": response_kind,
            "via": via,
            "identity": identity,
        }
        adapter_result.html_responses.append(record)
        return record

    visited = {current_url.split("#", 1)[0]}
    queue = discover_published_area_urls(current_html, current_url) if allow_network else []
    fetched = 0
    while queue and fetched < max_fetches:
        url = queue.pop(0)
        key = url.split("#", 1)[0]
        if key in visited:
            continue
        visited.add(key)
        try:
            response = fetch(url, timeout=12, unlocker=False)
        except Exception as exc:
            diagnostic["fetched_urls"].append(
                {"url": sanitise_source_url(url), "status": 0, "error": type(exc).__name__}
            )
            fetched += 1
            continue
        fetched += 1
        status = _response_status(response)
        body = _response_text(response)
        final_url = str(getattr(response, "url", "") or url)
        diagnostic["fetched_urls"].append(
            {"url": sanitise_source_url(final_url), "status": status, "body_bytes": len(body.encode("utf-8"))}
        )
        remember_html_response(
            source_url=final_url,
            body=body,
            status=status,
            via="direct_authored_area_probe",
            response_kind="published_area_probe_html",
            content_type=_response_content_type(response),
            identity={
                "status": "UNVERIFIED",
                "configured_property_id": str(getattr(ctx, "property_id", "") or ""),
                "admission_reason": (
                    "pending_supported_record_evaluation"
                    if status == 200 and body
                    else f"http_{status}_or_empty_body"
                ),
                "admitted_field_count": 0,
            },
        )
        if status != 200 or not body:
            continue
        surfaces.append((final_url, body, status, "direct_authored_area_probe"))
        # Apts247's authored page points to several auxiliary floor-plan
        # views (tour/screen-reader). Reserve the remaining bounded calls for
        # its property-identity and catalogue APIs instead of spending them
        # on equivalent presentation pages.
        if _APTS247_MARKER_RE.search(body):
            queue.clear()
            break
        for nested in discover_published_area_urls(body, final_url):
            if nested.split("#", 1)[0] not in visited and nested not in queue:
                queue.append(nested)

    apts247_surface = next(
        (
            (source_url, body)
            for source_url, body, _status, _via in surfaces
            if _APTS247_MARKER_RE.search(body)
        ),
        None,
    )
    if apts247_surface is not None:
        apts247_diagnostic = _apts247_plan_area_enrichment(
            ctx,
            adapter_result,
            units,
            apts247_surface[0],
            fetch=fetch,
            max_fetches=max_fetches - fetched,
        )
        diagnostic["apts247_enrichment"] = apts247_diagnostic
        fetched += int(apts247_diagnostic.get("fetch_count") or 0)
        diagnostic["exact_units"] += int(apts247_diagnostic.get("exact_units") or 0)
        diagnostic["range_units"] += int(apts247_diagnostic.get("range_units") or 0)

    for source_url, body, status, via in surfaces:
        records = parse_published_unit_area_roster(body, source_url)
        plans = parse_published_plan_areas(body, source_url)
        if not records and not plans:
            continue
        body_hash = response_sha256(body)
        matches = _label_matches(units, records)
        method = "exact_display_unit"
        # Partial alternate rosters can drift as apartments lease. Require
        # three exact identities before applying any of their fields.
        if len(matches) < 3:
            matches = _fingerprint_matches(units, records)
            method = "complete_rent_bed_bath_bijection"
        if len(matches) >= 3:
            for unit, record, row_method in matches:
                _apply_area_record(unit, record, method=row_method, body_hash=body_hash)
            diagnostic["matched_units"] += len(matches)
            diagnostic["exact_units"] += len(matches)
            diagnostic["join_method"] = method
        plan_exact_patched, range_patched = _apply_plan_ranges(
            units,
            plans,
            body_hash=body_hash,
        )
        diagnostic["exact_units"] += plan_exact_patched
        diagnostic["range_units"] += range_patched
        admitted_count = len(matches) + plan_exact_patched + range_patched
        diagnostic["surface_evaluations"].append(
            {
                "source_url": sanitise_source_url(source_url),
                "status": status,
                "response_sha256": body_hash,
                "published_unit_record_count": len(records),
                "published_plan_record_count": len(plans),
                "join_method": method if matches else "plan_shape" if admitted_count else None,
                "exact_units": len(matches) + plan_exact_patched,
                "range_units": range_patched,
                "admitted_field_count": admitted_count,
            }
        )
        if admitted_count:
            remember_html_response(
                source_url=source_url,
                status=status,
                body=body,
                content_type="text/html",
                response_kind="unit_area_enrichment_html",
                via=via,
                identity={
                    "status": "MATCH",
                    "configured_property_id": str(getattr(ctx, "property_id", "") or ""),
                    "join_method": method if matches else "plan_shape",
                    "source_record_count": len(records) + len(plans),
                    "admitted_field_count": admitted_count,
                },
            )
            adapter_result.unit_source_provenance.append(
                build_unit_source_provenance(
                    provider="published_area_roster",
                    source_url=source_url,
                    body=body,
                    unit_count=admitted_count,
                    identity={
                        "status": "MATCH",
                        "configured_property_id": str(getattr(ctx, "property_id", "") or ""),
                        "join_method": method if matches else "plan_shape",
                    },
                    response_kind="unit_area_enrichment",
                    status=status,
                )
            )
        else:
            remember_html_response(
                source_url=source_url,
                status=status,
                body=body,
                content_type="text/html",
                response_kind="published_area_probe_not_admitted",
                via=via,
                identity={
                    "status": "NOT_ADMITTED",
                    "configured_property_id": str(getattr(ctx, "property_id", "") or ""),
                    "source_record_count": len(records) + len(plans),
                    "admitted_field_count": 0,
                    "admission_reason": (
                        "evidence_gate_not_met" if records or plans else "no_supported_area_records"
                    ),
                },
            )

    refresh_final_patch_counts()
    try:
        ctx._area_enrichment_diagnostic = diagnostic  # type: ignore[attr-defined]
    except Exception:
        pass
    return diagnostic


__all__ = [
    "PublishedAreaRecord",
    "PublishedPlanArea",
    "discover_published_area_urls",
    "enrich_missing_unit_areas",
    "parse_published_plan_areas",
    "parse_published_area_pair",
    "parse_published_unit_area_roster",
]
