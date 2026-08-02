"""Strict recovery for server-rendered residence availability tables.

Some independent-property sites publish a mixed table containing both
physical residences (``101``, ``8E``) and plan/stack ranges (``205-805``).
The generic text parser correctly sees the rents but cannot distinguish those
two identity scopes.  This helper accepts only the narrow, labelled table
shape and emits only unambiguous physical residence codes.

It is intentionally page-local: no fetch, browser, proxy, or LLM call occurs.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag

from ma_poc.pms.adapters._parsing import bed_label_from, make_unit_dict
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.source_provenance import response_sha256, sanitise_source_url

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

# 1515 Park Place's availability table publishes image links rather than text
# sqft. Each image was downloaded from the same property host and visually
# verified on 2026-08-02; the immutable byte hash makes the recovery fail
# closed if the operator replaces or reuses an asset. Unit 102's table link
# incorrectly points to ``105.jpg`` (404), while the same-directory
# ``102.jpg`` is live and carries the verified 950 SF label.
_VERIFIED_ASSET_AREAS_BY_SHA256: dict[str, tuple[str, int]] = {
    "dc8b80736a7529fa2717b5c4690b322b550fc28d3c80e913a76e13892a2d8d9f": (
        "101",
        1271,
    ),
    "0c14b689f4dbc30771d5473bcdcb8c381c9085c90c6416253f2a52c6bf45cf6b": (
        "102",
        950,
    ),
    "77ea43af412153941a39223bbf48b730dd3dfa9c7cf145d5a82871c187a4555b": (
        "103",
        1124,
    ),
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
    plans: list[dict[str, Any]] = []
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
        is_stack_range = bool(_NUMERIC_STACK_RANGE_RE.fullmatch(residence))
        if not is_stack_range and (
            not _UNIT_CODE_RE.fullmatch(residence) or not any(
            char.isdigit() for char in residence
            )
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
        if is_stack_range:
            links = [
                urljoin(page_url, str(anchor.get("href") or ""))
                for anchor in cells[3].select("a[href]")
                if str(anchor.get("href") or "").strip()
            ]
            links = list(dict.fromkeys(links))
            if (
                len(links) != 1
                or urlparse(links[0]).scheme not in {"http", "https"}
                or urlparse(links[0]).hostname != urlparse(page_url).hostname
            ):
                return []
            plan_asset_url = links[0]
            plan = make_unit_dict(
                floor_plan_name=residence,
                bed_label=bed_label_from(bedrooms, residence),
                bedrooms=bedrooms,
                bathrooms=bathrooms,
                sqft="",
                unit_number="",
                rent_low=rent_low,
                rent_high=rent_high,
                availability_status="UNKNOWN",
                available_units="0",
                availability_date="",
                source_api_url=page_url,
                extraction_tier="TIER_1_DOM_STATIC_RESIDENCE_TABLE_PLAN",
                source_ids={
                    "static_residence_stack_id": residence,
                    "static_residence_plan_asset": plan_asset_url,
                },
                data_gaps=["sqft", "availability_date", "unit_id"],
                data_quality_flag="STATIC_RESIDENCE_TABLE_PLAN_STACK",
            )
            plan.update(
                {
                    "is_floor_plan_level": True,
                    "floor_plan_url": plan_asset_url,
                    "floor_plan_name_provenance": "provider_residence_stack_code",
                    "availability_date_provenance": "missing",
                    "source_property_provenance": (
                        "exact_property_identity_server_rendered_availability_table"
                    ),
                }
            )
            plans.append(plan)
            skipped_stack_ranges.append(residence)
            continue
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
                "source_asset_candidate_urls": [
                    urljoin(page_url, str(anchor.get("href") or ""))
                    for anchor in cells[3].select("a[href]")
                    if str(anchor.get("href") or "").strip()
                    and urlparse(
                        urljoin(page_url, str(anchor.get("href") or ""))
                    ).hostname
                    == urlparse(page_url).hostname
                ],
                "source_response_sha256": response_sha256(html),
                "source_response_url": sanitise_source_url(page_url),
                "source_record_locator": f"residence_table:{residence}",
                "identity_quality": "provider_explicit_physical_unit",
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
        ctx._static_residence_table_plan_summaries = list(plans)
        ctx._static_residence_table_telemetry = {
            "raw_rows": len(raw_rows),
            "accepted_physical_residences": len(units),
            "accepted_plan_stacks": len(plans),
            "skipped_numeric_stack_ranges": skipped_stack_ranges,
            "source_url": page_url,
        }
    except Exception:
        pass
    from ma_poc.pms.source_provenance import (
        build_unit_source_provenance,
        record_context_unit_source_provenance,
    )

    record_context_unit_source_provenance(
        ctx,
        build_unit_source_provenance(
            provider="static_residence_table",
            source_url=page_url,
            body=html,
            unit_count=len(units),
            identity={
                "status": "MATCH",
                "evidence": [
                    "configured_name",
                    "configured_address",
                    "city_state_zip",
                    "single_exact_table_shape",
                ],
                "configured_property_id": str(ctx.property_id or ""),
                "source_count": len(raw_rows),
                "admitted_unit_count": len(units),
                "admitted_plan_count": len(plans),
            },
            response_kind="mixed_unit_plan_table",
        ),
    )
    return units


def _asset_candidates(unit: dict[str, Any]) -> list[str]:
    """Return bounded same-directory asset candidates for one residence."""

    residence = str(unit.get("unit_number") or "").strip()
    candidates: list[str] = []
    for raw in unit.get("source_asset_candidate_urls") or []:
        url = str(raw or "").strip()
        if not url:
            continue
        try:
            parsed = urlparse(url)
        except (TypeError, ValueError):
            continue
        path = parsed.path or ""
        if "." in path.rsplit("/", 1)[-1] and residence.isdigit():
            name = path.rsplit("/", 1)[-1]
            stem, extension = name.rsplit(".", 1)
            if stem.isdigit() and stem != residence:
                corrected_path = f"{path.rsplit('/', 1)[0]}/{residence}.{extension}"
                candidates.append(
                    urlunparse(
                        (
                            parsed.scheme,
                            parsed.netloc,
                            corrected_path,
                            parsed.params,
                            parsed.query,
                            "",
                        )
                    )
                )
        candidates.append(url)
    return list(dict.fromkeys(candidates))[:2]


def enrich_static_residence_asset_areas(
    ctx: AdapterContext,
    units: list[dict[str, Any]],
    *,
    fetch: Callable[..., Any],
    max_fetches: int = 6,
) -> dict[str, Any]:
    """Attach sqft only when a same-host asset matches a verified SHA-256.

    Args:
        ctx: Property-scoped adapter context whose table identity already
            passed :func:`recover_static_residence_table`.
        units: Physical residence rows emitted by that parser.
        fetch: Requests-compatible direct HTTP callable.
        max_fetches: Hard property-level network bound.

    Returns:
        Compact attempts/admissions diagnostic. Raw winning bytes are attached
        to the context for the runner's content-addressed source archive.
    """

    page_host = (urlparse(_page_url(ctx)).hostname or "").casefold()
    diagnostic: dict[str, Any] = {
        "attempted": False,
        "fetch_count": 0,
        "matched_units": 0,
        "attempts": [],
    }
    asset_responses: list[dict[str, Any]] = []
    if not units or not page_host:
        return diagnostic
    diagnostic["attempted"] = True
    for unit in units:
        if diagnostic["fetch_count"] >= max_fetches:
            break
        residence = str(unit.get("unit_number") or "").strip()
        for url in _asset_candidates(unit):
            if diagnostic["fetch_count"] >= max_fetches:
                break
            if (urlparse(url).hostname or "").casefold() != page_host:
                continue
            diagnostic["fetch_count"] += 1
            try:
                response = fetch(url, timeout=12, unlocker=False)
            except Exception as exc:
                diagnostic["attempts"].append(
                    {
                        "url": sanitise_source_url(url),
                        "status": 0,
                        "error": type(exc).__name__,
                    }
                )
                continue
            status = int(getattr(response, "status_code", 0) or 0)
            content = getattr(response, "content", b"")
            if isinstance(content, bytearray):
                content = bytes(content)
            elif isinstance(content, str):
                content = content.encode("utf-8")
            elif not isinstance(content, bytes):
                content = str(content or "").encode("utf-8")
            body_hash = hashlib.sha256(content).hexdigest() if content else ""
            diagnostic["attempts"].append(
                {
                    "url": sanitise_source_url(url),
                    "status": status,
                    "response_sha256": body_hash or None,
                    "body_bytes": len(content),
                }
            )
            verified = _VERIFIED_ASSET_AREAS_BY_SHA256.get(body_hash)
            if status != 200 or verified is None or verified[0] != residence:
                continue
            area = verified[1]
            unit.update(
                {
                    "sqft": str(area),
                    "area_low": area,
                    "area_high": area,
                    "area_range": str(area),
                    "area_range_raw": str(area),
                    "area_provenance": "verified_source_asset_sha256",
                    "area_source_url": sanitise_source_url(url),
                    "source_asset_url": sanitise_source_url(url),
                    "source_asset_sha256": body_hash,
                    "source_response_url": sanitise_source_url(url),
                    "source_response_sha256": body_hash,
                    "source_parent_record_locator": unit.get("source_record_locator"),
                    "source_record_locator": f"asset_sha256:{body_hash}",
                }
            )
            asset_responses.append(
                {
                    "url": url,
                    "status": status,
                    "body": content,
                    "content_type": str(
                        (getattr(response, "headers", {}) or {}).get(
                            "content-type",
                            "image/jpeg",
                        )
                    ),
                    "response_kind": "verified_floor_plan_asset",
                    "identity": {
                        "status": "MATCH",
                        "configured_property_id": str(ctx.property_id or ""),
                        "unit_number": residence,
                        "verification": "sha256_allowlist_and_same_host",
                    },
                }
            )
            diagnostic["matched_units"] += 1
            break
    try:
        ctx._static_residence_asset_responses = asset_responses  # type: ignore[attr-defined]
        ctx._static_residence_asset_diagnostic = diagnostic  # type: ignore[attr-defined]
    except Exception:
        pass
    return diagnostic


__all__ = [
    "enrich_static_residence_asset_areas",
    "recover_static_residence_table",
]
