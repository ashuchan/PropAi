"""
Tier 2 — JSON-LD / Schema.org extraction.

Acceptance criteria (CLAUDE.md PR-03 / Tier 2):
- Use extruct library; target Apartment, ApartmentComplex, Offer schemas
- Field mappings: floorSize → sqft, offers[].price → asking_rent,
  offers[].availability → availability_status
- Multiple Apartment objects → multiple UnitRecord rows
- Confidence: 1.0 if all required fields present; degrade 0.15 per missing required
- Bug-hunt #3: tolerate parse errors (extruct raises on malformed pages)
"""
from __future__ import annotations

import json
from typing import Any

from bs4 import BeautifulSoup

from extraction.confidence import composite, low_confidence_fields
from models.extraction_result import ExtractionResult, ExtractionStatus, ExtractionTier
from scraper.browser import BrowserSession

try:
    import extruct
    _EXTRUCT_OK = True
except Exception:
    _EXTRUCT_OK = False


_AVAIL_MAP = {
    "InStock": "AVAILABLE",
    "https://schema.org/InStock": "AVAILABLE",
    "http://schema.org/InStock": "AVAILABLE",
    "OutOfStock": "UNAVAILABLE",
    "https://schema.org/OutOfStock": "UNAVAILABLE",
    "http://schema.org/OutOfStock": "UNAVAILABLE",
}


def _types_of(node: dict[str, Any]) -> list[str]:
    t = node.get("@type")
    if isinstance(t, list):
        return [str(x) for x in t]
    if isinstance(t, str):
        return [t]
    return []


def _to_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        if isinstance(x, str):
            x = x.replace("$", "").replace(",", "").strip()
        return float(x)
    except (TypeError, ValueError):
        return None


def _to_int(x: Any) -> int | None:
    f = _to_float(x)
    return int(f) if f is not None else None


def _walk(node: Any, hits: list[dict[str, Any]]) -> None:
    if isinstance(node, dict):
        types = _types_of(node)
        if any(t.endswith("Apartment") for t in types):
            hits.append(node)
        for v in node.values():
            _walk(v, hits)
    elif isinstance(node, list):
        for v in node:
            _walk(v, hits)


def _coerce_apartment(node: dict[str, Any]) -> dict[str, Any]:
    fs = node.get("floorSize")
    if isinstance(fs, dict):
        sqft = _to_int(fs.get("value"))
    else:
        sqft = _to_int(fs)
    offers = node.get("offers") or []
    if isinstance(offers, dict):
        offers = [offers]
    rent: float | None = None
    avail: str | None = None
    for off in offers:
        if not isinstance(off, dict):
            continue
        if rent is None:
            rent = _to_float(off.get("price"))
        if avail is None:
            avail = _AVAIL_MAP.get(str(off.get("availability") or ""), None)
    return {
        "unit_number": str(node.get("name") or node.get("identifier") or "").strip() or None,
        "asking_rent": rent,
        "availability_status": avail or "UNKNOWN",
        "sqft": sqft,
        "floor_plan_type": node.get("accommodationCategory") or node.get("numberOfRooms"),
    }


def _extract_jsonld_from_html(html: str) -> list[Any]:
    """Fallback: parse <script type='application/ld+json'> tags with stdlib json."""
    soup = BeautifulSoup(html, "lxml")
    results: list[Any] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = tag.string
        if not text:
            continue
        try:
            results.append(json.loads(text))
        except json.JSONDecodeError:
            continue
    return results


async def extract(session: BrowserSession) -> ExtractionResult:
    if not session.html:
        return ExtractionResult(
            property_id=session.property_id,
            tier=ExtractionTier.JSON_LD,
            status=ExtractionStatus.FAILED,
            confidence_score=0.0,
            error_message="no html",
        )
    # Try extruct first; fall back to direct JSON-LD parse if extruct is broken
    data: Any = None
    if _EXTRUCT_OK:
        try:
            data = extruct.extract(
                session.html,
                base_url=session.url,
                syntaxes=["json-ld", "microdata"],
                uniform=True,
            )
        except Exception:
            data = None
    if data is None:
        data = _extract_jsonld_from_html(session.html)

    apartments: list[dict[str, Any]] = []
    _walk(data, apartments)

    units = [u for u in (_coerce_apartment(a) for a in apartments) if u.get("unit_number")]
    if not units:
        return ExtractionResult(
            property_id=session.property_id,
            tier=ExtractionTier.JSON_LD,
            status=ExtractionStatus.FAILED,
            confidence_score=0.0,
            error_message="no Apartment schema found",
        )

    n = len(units)
    field_conf = {
        f: sum(1 for u in units if u.get(f) not in (None, "", "UNKNOWN")) / n
        for f in ("unit_number", "asking_rent", "availability_status", "sqft", "floor_plan_type")
    }
    avg = sum(composite(u) for u in units) / n
    low = sorted({fld for u in units for fld in low_confidence_fields(u)})

    return ExtractionResult(
        property_id=session.property_id,
        tier=ExtractionTier.JSON_LD,
        status=ExtractionStatus.SUCCESS if avg >= 0.7 else ExtractionStatus.FAILED,
        confidence_score=avg,
        raw_fields={"units": units},
        field_confidences=field_conf,
        low_confidence_fields=low,
    )
