"""Deterministic replay of previously-saved LLM field mappings.

No LLM calls — pure JSON navigation using stored json_paths and
response_envelope values learned during a prior LLM analysis pass.
"""

from __future__ import annotations

import logging
from typing import Any

from services.llm._shared import _normalize_units

log = logging.getLogger(__name__)


def apply_saved_mapping(api_response_body: Any, mapping: dict) -> list[dict]:
    """Deterministic extraction using a previously-saved LLM field mapping.

    Navigates the JSON using response_envelope to find the unit list,
    then maps fields using json_paths. Returns [] if the mapping doesn't
    produce valid units (schema may have changed).

    Args:
        api_response_body: The raw JSON body from the API response.
        mapping: Dict with keys "response_envelope", "json_paths".

    Returns:
        List of normalized unit dicts, or [] on failure.
    """
    envelope = mapping.get("response_envelope", "")
    json_paths = mapping.get("json_paths", {})
    if not json_paths:
        return []

    # Navigate to the unit list using the envelope path
    data = api_response_body
    if envelope:
        for key in envelope.split("."):
            if isinstance(data, dict):
                data = data.get(key)
            elif isinstance(data, list) and key.isdigit():
                idx = int(key)
                data = data[idx] if idx < len(data) else None
            else:
                return []
            if data is None:
                return []

    # data should now be a list of unit dicts
    if not isinstance(data, list):
        # Maybe it's a single dict wrapping a list
        if isinstance(data, dict):
            # Try common wrapper keys
            for k in ("units", "floorPlans", "floor_plans", "results", "data", "items"):
                if isinstance(data.get(k), list):
                    data = data[k]
                    break
            else:
                return []
        else:
            return []

    if not data:
        return []

    # Extract fields using json_paths mapping
    units: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue

        unit: dict[str, Any] = {}

        # Helper to navigate dot-separated paths
        def _get_nested(obj: Any, path: str) -> Any:
            if not path:
                return None
            for part in path.split("."):
                if isinstance(obj, dict):
                    obj = obj.get(part)
                elif isinstance(obj, list) and part.isdigit():
                    idx = int(part)
                    obj = obj[idx] if idx < len(obj) else None
                else:
                    return None
            return obj

        # Map each field
        uid_path = json_paths.get("unit_id", "")
        if uid_path:
            unit["unit_id"] = _get_nested(item, uid_path)

        fp_path = json_paths.get("floor_plan_name", "")
        if fp_path:
            unit["floor_plan_name"] = _get_nested(item, fp_path)

        rent_low_path = json_paths.get("rent_low", "")
        if rent_low_path:
            unit["market_rent_low"] = _get_nested(item, rent_low_path)

        rent_high_path = json_paths.get("rent_high", "")
        if rent_high_path:
            unit["market_rent_high"] = _get_nested(item, rent_high_path)

        beds_path = json_paths.get("bedrooms", "")
        if beds_path:
            unit["bedrooms"] = _get_nested(item, beds_path)

        baths_path = json_paths.get("bathrooms", "")
        if baths_path:
            unit["bathrooms"] = _get_nested(item, baths_path)

        sqft_path = json_paths.get("sqft", "")
        if sqft_path:
            unit["sqft"] = _get_nested(item, sqft_path)

        date_path = json_paths.get("available_date", "")
        if date_path:
            unit["available_date"] = _get_nested(item, date_path)

        status_path = json_paths.get("availability_status", "")
        if status_path:
            unit["availability_status"] = _get_nested(item, status_path)

        # Default confidence for mapping-based extraction
        unit["confidence"] = 0.85

        units.append(unit)

    normalized = _normalize_units(units)
    if normalized:
        log.info("apply_saved_mapping: produced %d units from %d items", len(normalized), len(data))
    return normalized
