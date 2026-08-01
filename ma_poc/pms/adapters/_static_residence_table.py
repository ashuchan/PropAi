"""Strict recovery for server-rendered residence availability tables.

Some independent-property sites publish a mixed table containing both
physical residences (``101``, ``8E``) and plan/stack ranges (``205-805``).
The generic text parser correctly sees the rents but cannot distinguish those
two identity scopes.  This helper accepts only the narrow, labelled table
shape and emits only unambiguous physical residence codes.

It is intentionally page-local: no fetch, browser, proxy, or LLM call occurs.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from ma_poc.pms.adapters._parsing import bed_label_from, make_unit_dict
from ma_poc.pms.adapters.base import AdapterContext

_EXPECTED_HEADERS = ("residence", "bed bath", "price", "floorplan")
_BED_BATH_RE = re.compile(
    r"^\s*(?P<beds>\d+(?:\.5)?)\s*beds?\s*/\s*"
    r"(?P<baths>\d+(?:\.5)?)\s*baths?\s*$",
    re.IGNORECASE,
)
_UNIT_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,15}$", re.IGNORECASE)
_NUMERIC_STACK_RANGE_RE = re.compile(r"^\d{2,5}\s*[-–]\s*\d{2,5}$")
_MONEY_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
_ADDRESS_ALIASES = {
    "avenue": "ave",
    "boulevard": "blvd",
    "court": "ct",
    "drive": "dr",
    "highway": "hwy",
    "lane": "ln",
    "parkway": "pkwy",
    "place": "pl",
    "road": "rd",
    "street": "st",
}


def _norm(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _norm_address(value: object) -> str:
    return " ".join(
        _ADDRESS_ALIASES.get(token, token) for token in _norm(value).split()
    )


def _body_from_ctx(ctx: AdapterContext) -> str:
    fetch_result = getattr(ctx, "fetch_result", None)
    body = getattr(fetch_result, "body", None) if fetch_result is not None else None
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return body if isinstance(body, str) else ""


def _page_url(ctx: AdapterContext) -> str:
    fetch_result = getattr(ctx, "fetch_result", None)
    final_url = (
        getattr(fetch_result, "final_url", "")
        if fetch_result is not None
        else ""
    )
    return str(final_url or getattr(ctx, "base_url", "") or "")


def _page_identity_matches(soup: BeautifulSoup, ctx: AdapterContext) -> bool:
    visible = _norm(
        " ".join(
            [soup.get_text(" ", strip=True)]
            + [
                str(node.get("content") or "")
                for node in soup.select("meta[content]")
            ]
        )
    )
    visible_words = set(visible.split())
    normalized_address = _norm_address(visible)
    name_tokens = [
        token
        for token in _norm(getattr(ctx, "property_name", "")).split()
        if token
        not in {
            "apartment",
            "apartments",
            "at",
            "building",
            "community",
            "homes",
            "of",
            "the",
        }
    ]
    address = _norm_address(getattr(ctx, "address", ""))
    city = _norm(getattr(ctx, "city", ""))
    state = _norm(getattr(ctx, "state", ""))
    zip_code = str(getattr(ctx, "zip_code", "") or "").strip()
    return bool(
        name_tokens
        and all(token in visible_words for token in name_tokens)
        and address
        and f" {address} " in f" {normalized_address} "
        and city
        and f" {city} " in f" {visible} "
        and state
        and state in visible_words
        and zip_code
        and zip_code in visible_words
    )


def _matching_table(soup: BeautifulSoup) -> Tag | None:
    matches: list[Tag] = []
    for table in soup.select(".table"):
        if not isinstance(table, Tag):
            continue
        header = table.select_one(":scope > .table-header")
        if not isinstance(header, Tag):
            continue
        labels = tuple(
            _norm(cell.get_text(" ", strip=True))
            for cell in header.select(":scope > .table-cell")
        )
        if labels == _EXPECTED_HEADERS:
            matches.append(table)
    return matches[0] if len(matches) == 1 else None


def _money_values(text: str) -> tuple[int, int] | None:
    values: list[int] = []
    for raw in _MONEY_RE.findall(text):
        try:
            value = int(float(raw.replace(",", "")))
        except (TypeError, ValueError):
            return None
        if not 200 <= value <= 50_000:
            return None
        values.append(value)
    if not values or len(values) > 2:
        return None
    return min(values), max(values)


def recover_static_residence_table(ctx: AdapterContext) -> list[dict[str, Any]]:
    """Return property-scoped physical residence rows from the fetched body.

    A row containing a numeric-to-numeric residence range is classified as a
    plan/stack summary and deliberately omitted. Any other malformed row,
    duplicate physical code, ambiguous table, or property mismatch fails the
    entire recovery closed.
    """
    html = _body_from_ctx(ctx)
    page_url = _page_url(ctx)
    if not html or "availab" not in urlparse(page_url).path.casefold():
        return []
    soup = BeautifulSoup(html, "lxml")
    if not _page_identity_matches(soup, ctx):
        return []
    table = _matching_table(soup)
    if table is None:
        return []
    raw_rows = table.select(":scope > .table-row")
    if not raw_rows or len(raw_rows) > 100:
        return []

    units: list[dict[str, Any]] = []
    skipped_stack_ranges: list[str] = []
    for row in raw_rows:
        cells = row.select(":scope > .table-cell")
        residence_cell = row.select_one(":scope > .table-cell.residence")
        bed_cell = row.select_one(":scope > .table-cell.bed")
        if (
            len(cells) != 4
            or not isinstance(residence_cell, Tag)
            or not isinstance(bed_cell, Tag)
        ):
            return []
        residence = " ".join(residence_cell.get_text(" ", strip=True).split())
        if _NUMERIC_STACK_RANGE_RE.fullmatch(residence):
            skipped_stack_ranges.append(residence)
            continue
        if not _UNIT_CODE_RE.fullmatch(residence) or not any(
            char.isdigit() for char in residence
        ):
            return []
        bed_bath = _BED_BATH_RE.fullmatch(
            " ".join(bed_cell.get_text(" ", strip=True).split())
        )
        rents = _money_values(cells[2].get_text(" ", strip=True))
        if bed_bath is None or rents is None:
            return []
        bedrooms = bed_bath.group("beds")
        bathrooms = bed_bath.group("baths")
        rent_low, rent_high = rents
        unit = make_unit_dict(
            floor_plan_name="",
            bed_label=bed_label_from(bedrooms, ""),
            bedrooms=bedrooms,
            bathrooms=bathrooms,
            sqft="",
            unit_number=residence,
            unit_name=residence,
            rent_low=rent_low,
            rent_high=rent_high,
            availability_status="AVAILABLE",
            available_units="1",
            availability_date="",
            source_api_url=page_url,
            extraction_tier="TIER_1_DOM_STATIC_RESIDENCE_TABLE",
            data_gaps=["floor_plan_name", "sqft", "availability_date"],
            data_quality_flag=(
                "STATIC_RESIDENCE_TABLE_PLAN_NAME_SQFT_DATE_NOT_PUBLISHED"
            ),
        )
        unit.update(
            {
                "availability_date_provenance": (
                    "current_availability_roster_no_explicit_date"
                ),
                "floor_plan_name_provenance": (
                    "provider_table_does_not_publish_floor_plan_name"
                ),
                "source_property_name": str(
                    getattr(ctx, "property_name", "") or ""
                ),
                "source_property_address": ", ".join(
                    value
                    for value in (
                        str(getattr(ctx, "address", "") or "").strip(),
                        str(getattr(ctx, "city", "") or "").strip(),
                        str(getattr(ctx, "state", "") or "").strip(),
                        str(getattr(ctx, "zip_code", "") or "").strip(),
                    )
                    if value
                ),
                "source_property_provenance": (
                    "exact_property_identity_server_rendered_availability_table"
                ),
            }
        )
        units.append(unit)

    unit_numbers = [
        str(unit.get("unit_number") or "").strip().casefold() for unit in units
    ]
    if not units or len(unit_numbers) != len(set(unit_numbers)):
        return []
    try:
        ctx._static_residence_table_telemetry = {"raw_rows": len(raw_rows), "accepted_physical_residences": len(units), "skipped_numeric_stack_ranges": skipped_stack_ranges, "source_url": page_url}
    except Exception:
        pass
    return units


__all__ = ["recover_static_residence_table"]
