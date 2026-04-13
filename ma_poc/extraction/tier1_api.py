"""
Tier 1 — API interception extraction.

Acceptance criteria (CLAUDE.md PR-03 / Tier 1):
- Match intercepted URLs against config/api_catalogue.json regexes
  (/api/, /availability, /pricing, /floorplans, /units, /apartments)
- Required fields: unit_number, asking_rent, availability_status
- Preferred fields: sqft, floor_plan_type
- Confidence = present required fields / total required fields (composite scoring)
- Per-field confidence populates field_confidences
- Bug-hunt #3: every json.loads is wrapped in try/except JSONDecodeError
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from extraction.confidence import composite, low_confidence_fields
from models.extraction_result import ExtractionResult, ExtractionStatus, ExtractionTier
from scraper.browser import BrowserSession


_RENT_KEYS = ("asking_rent", "rent", "price", "marketRent", "rent_price", "minRent")
_AVAIL_KEYS = ("availability_status", "availability", "status", "available")
_UNIT_KEYS = ("unit_number", "unitNumber", "unit", "unitName", "name")
_SQFT_KEYS = ("sqft", "squareFeet", "size", "sq_ft")
_FP_KEYS = ("floor_plan_type", "floorPlan", "bedBath", "type")


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _coerce_unit(d: dict[str, Any]) -> Optional[dict[str, Any]]:
    unit_number = _first(d, _UNIT_KEYS)
    rent = _first(d, _RENT_KEYS)
    avail = _first(d, _AVAIL_KEYS)
    if unit_number is None and rent is None and avail is None:
        return None
    out: dict[str, Any] = {
        "unit_number": str(unit_number) if unit_number is not None else None,
        "asking_rent": _to_float(rent),
        "availability_status": _normalize_avail(avail),
        "sqft": _to_int(_first(d, _SQFT_KEYS)),
        "floor_plan_type": _first(d, _FP_KEYS),
    }
    return out


def _to_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        if isinstance(x, str):
            x = re.sub(r"[^0-9.]", "", x)
            if not x:
                return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _to_int(x: Any) -> Optional[int]:
    f = _to_float(x)
    return int(f) if f is not None else None


def _normalize_avail(x: Any) -> Optional[str]:
    if x is None:
        return None
    s = str(x).strip().lower()
    if s in ("true", "1", "yes", "y", "available"):
        return "AVAILABLE"
    if s in ("false", "0", "no", "n", "unavailable", "leased"):
        return "UNAVAILABLE"
    if "avail" in s and "un" not in s:
        return "AVAILABLE"
    if "unavail" in s or "leased" in s:
        return "UNAVAILABLE"
    return "UNKNOWN"


def _walk_for_units(payload: Any) -> list[dict[str, Any]]:
    """Walk an arbitrary JSON payload looking for unit-shaped dicts."""
    found: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        coerced = _coerce_unit(payload)
        if coerced and coerced["unit_number"] is not None:
            found.append(coerced)
        for v in payload.values():
            found.extend(_walk_for_units(v))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_walk_for_units(item))
    return found


def matches_catalogue(url: str, catalogue: dict[str, Any]) -> bool:
    patterns = catalogue.get("patterns", []) if catalogue else []
    for p in patterns:
        rx = p.get("url_regex")
        if rx and re.search(rx, url, flags=re.IGNORECASE):
            return True
    return False


async def extract(session: BrowserSession, catalogue: Optional[dict[str, Any]] = None) -> ExtractionResult:
    """
    Walk session.intercepted_api_responses, match against the API catalogue,
    parse JSON bodies (with try/except — bug-hunt #3), produce a list of unit
    dicts and a composite confidence score.
    """
    catalogue = catalogue or {}
    units: list[dict[str, Any]] = []
    matched_any = False

    for resp in session.intercepted_api_responses:
        if not matches_catalogue(resp.url, catalogue):
            continue
        if "json" not in (resp.content_type or "").lower():
            continue
        matched_any = True
        try:
            payload = json.loads(resp.body.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue  # malformed JSON silently discarded
        units.extend(_walk_for_units(payload))

    if not matched_any or not units:
        return ExtractionResult(
            property_id=session.property_id,
            tier=ExtractionTier.API_INTERCEPTION,
            status=ExtractionStatus.FAILED,
            confidence_score=0.0,
            error_message=("api_catalogue miss" if not matched_any else "no parseable units"),
        )

    # Per-field confidence: ratio of records that contain each required field.
    n = len(units)
    field_conf = {
        f: sum(1 for u in units if u.get(f) not in (None, "")) / n
        for f in ("unit_number", "asking_rent", "availability_status", "sqft", "floor_plan_type")
    }
    avg = sum(composite(u) for u in units) / n
    low = sorted({fld for u in units for fld in low_confidence_fields(u)})

    return ExtractionResult(
        property_id=session.property_id,
        tier=ExtractionTier.API_INTERCEPTION,
        status=ExtractionStatus.SUCCESS if avg >= 0.7 else ExtractionStatus.FAILED,
        confidence_score=avg,
        raw_fields={"units": units},
        field_confidences=field_conf,
        low_confidence_fields=low,
    )
