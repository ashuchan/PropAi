"""services.llm — LLM extraction sub-package.

Re-exports all public names from sub-modules for backward compatibility.
Consumers may import from either:
  - ma_poc.services.llm_extractor  (legacy shim)
  - ma_poc.services.llm            (this package)
  - ma_poc.services.llm.<module>   (individual sub-modules)
"""

from __future__ import annotations

from services.llm._shared import (
    _normalize_units,
    _parse_llm_response,
    _rank_api_responses,
    _trim_html,
)
from services.llm.api_analyzer import analyze_api_with_llm
from services.llm.dom_analyzer import analyze_dom_with_llm
from services.llm.mapping_replay import apply_saved_mapping
from services.llm.monolithic import (
    _build_prompt,
    _load_prompt_template,
    _resolve_known_plans_block,
    extract_with_llm,
    prepare_llm_input,
)

__all__ = [
    # Public API
    "extract_with_llm",
    "analyze_api_with_llm",
    "analyze_dom_with_llm",
    "apply_saved_mapping",
    "prepare_llm_input",
    # Internal helpers (exported for backward compat)
    "_trim_html",
    "_rank_api_responses",
    "_normalize_units",
    "_parse_llm_response",
    "_build_prompt",
    "_load_prompt_template",
    "_resolve_known_plans_block",
]
