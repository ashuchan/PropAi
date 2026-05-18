"""Re-exports from _api_parser for backwards compatibility.

All callers should migrate to importing directly from
``ma_poc.pms._vendor_main_dom._api_parser``.  This shim exists so adapters that
previously depended on the scripts/entrata.py + scripts/scrape_properties.py
bridge continue to work without change during the transition.
"""

from __future__ import annotations

from ma_poc.pms._vendor_main_dom._api_parser import (
    TARGET_JSONLD_TYPES,
    _RENT_KEYS,
    _RENT_MAX,
    _RENT_MIN,
    _UNIT_ID_KEYS,
    _extract_rent,
    _jsonld_floor_size,
    _jsonld_item_has_unit_signal,
    _money_to_int,
    _walk_jsonld,
    parse_api_responses,
    parse_sightmap_payload,
    realpage_units_from_body as _realpage_units_internal_shape,
    realpage_units_to_adapter_shape,
)

__all__ = [
    "parse_api_responses",
    "parse_sightmap_payload",
    "realpage_units_to_adapter_shape",
    "TARGET_JSONLD_TYPES",
    "_jsonld_floor_size",
    "_jsonld_item_has_unit_signal",
    "_walk_jsonld",
    "_UNIT_ID_KEYS",
    "_RENT_KEYS",
    "_extract_rent",
    "_money_to_int",
    "_RENT_MIN",
    "_RENT_MAX",
]
