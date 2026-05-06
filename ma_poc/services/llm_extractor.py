"""Backward-compatibility shim for services/llm_extractor.py.

The implementation has been split into services/llm/ sub-modules.
This module re-exports all public names so existing imports continue to work.
"""

from services.llm import *  # noqa: F401,F403
from services.llm import (  # noqa: F401 — explicit re-export for static analysis
    _build_prompt,
    _load_prompt_template,
    _normalize_units,
    _parse_llm_response,
    _rank_api_responses,
    _resolve_known_plans_block,
    _trim_html,
    analyze_api_with_llm,
    analyze_dom_with_llm,
    apply_saved_mapping,
    extract_with_llm,
    prepare_llm_input,
)
