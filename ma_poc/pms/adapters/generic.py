"""Generic fallback adapter — backward-compat re-export shim.

All logic has moved to:
  - pms/adapters/tier_orchestrator.py  (GenericAdapter class + helpers)
  - pms/adapters/tiers/               (individual tier classes)

This module re-exports every public symbol that callers depend on so that
existing ``from ma_poc.pms.adapters.generic import ...`` statements continue
to work without modification.
"""

from __future__ import annotations

# Re-export everything from tier_orchestrator so existing callers work unchanged.
from ma_poc.pms.adapters.tier_orchestrator import (  # noqa: F401
    GenericAdapter,
    _assess_and_decide,
    _availability_count_aware_merge,
    _extract_rent_dom_section,
    _find_unit_list,
    _get_page_html,
    _has_unit_signals,
    _looks_field_rich,
    _merge_field_values,
    _merge_into_result_units,
    _merge_rank_signature,
    parse_generic_api,
)

__all__ = [
    "GenericAdapter",
    "_assess_and_decide",
    "_availability_count_aware_merge",
    "_extract_rent_dom_section",
    "_find_unit_list",
    "_get_page_html",
    "_has_unit_signals",
    "_looks_field_rich",
    "_merge_field_values",
    "_merge_into_result_units",
    "_merge_rank_signature",
    "parse_generic_api",
]
