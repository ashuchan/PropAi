"""
LLM Diagnostics Service
=======================

Two capabilities:

  adapter_debugger(...)  — F1: diagnose why a Tier-1 adapter detected a PMS
                           but extracted zero units. Returns AdapterDiagnosis.

  null_field_recovery(...) — F2: recover missing rent_low / unit_id fields from
                             the raw source fragment that produced a partial unit.
                             Returns FieldRecovery.

Both functions are async, never raise (log + return None on error), and write
their output to data/runs/{date}/llm_diagnostics/{canonical_id}_{feature}.json
for post-run inspection.

LLM model: inherits AZURE_OPENAI_DEPLOYMENT env var, same as llm_extractor.py.
Temperature: 0.0 on all calls.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "config" / "prompts"
_ADAPTER_DEBUGGER_PROMPT = _PROMPTS_DIR / "adapter_debugger.txt"
_NULL_FIELD_RECOVERY_PROMPT = _PROMPTS_DIR / "null_field_recovery.txt"


# ---------------------------------------------------------------------------
# F1 — AdapterDiagnosis response models
# ---------------------------------------------------------------------------


class FieldMappingFix(BaseModel):
    original_key: str = Field(description="Key name the parser looks for")
    actual_key: str = Field(description="Key name present in the actual payload")
    fix_type: Literal["rename", "case_normalise", "unwrap", "new_path", "missing_data"] = Field(
        description=(
            "rename: field exists under a different name. "
            "case_normalise: same name, different casing (e.g. FloorplanName vs floorplanname). "
            "unwrap: data exists but is nested under an extra wrapper key. "
            "new_path: data is at a completely different JSON path. "
            "missing_data: field genuinely absent from this payload."
        )
    )
    example_value: str | None = Field(
        default=None,
        description="The actual value from the payload for this field, for verification",
    )
    code_change: str = Field(
        description=(
            "Exact Python expression to extract this field from item_lc (the lowercased "
            "item dict)."
        )
    )


class WrapperFix(BaseModel):
    wrapper_key_path: list[str] = Field(
        description=(
            "Ordered list of dict keys to traverse to reach the floorplan list. "
            "Single-level: [\"data\"]. Two-level: [\"response\", \"result\"]. "
            "Root list: []."
        )
    )
    evidence: str = Field(description="Quote the relevant JSON structure from the payload")


class AdapterDiagnosis(BaseModel):
    property_id: str
    adapter_name: str
    payload_url: str
    diagnosis_summary: str = Field(
        description="One paragraph plain-English summary of why extraction failed"
    )
    failure_category: Literal[
        "case_mismatch",
        "missing_wrapper_key",
        "wrong_wrapper_depth",
        "field_name_mismatch",
        "empty_payload",
        "pagination_required",
        "auth_required",
        "wrong_endpoint_type",
        "genuinely_no_data",
        "multiple_issues",
    ]
    wrapper_fix: WrapperFix | None = Field(
        default=None,
        description="Present if the list is nested inside a wrapper the parser doesn't handle",
    )
    field_fixes: list[FieldMappingFix] = Field(default_factory=list)
    can_auto_fix: bool
    estimated_units_recoverable: int
    adapter_code_patch: str = Field(
        description=(
            "Unified-diff-style patch showing only the lines that change, with "
            "3 lines of surrounding context."
        )
    )
    cost_usd: float = Field(default=0.0, description="LLM cost in USD for this call")


# ---------------------------------------------------------------------------
# F2 — FieldRecovery response models
# ---------------------------------------------------------------------------


class RecoveredField(BaseModel):
    field_name: Literal[
        "rent_low",
        "rent_high",
        "unit_id",
        "floor_plan_name",
        "beds",
        "baths",
        "area",
        "available_date",
    ]
    recovered_value: str | int | float | None = Field(
        default=None,
        description="The actual value extracted from the source fragment",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    source_path: str = Field(
        description=(
            "Where this value was found. JSON path (e.g. '$.floorplan.rent') "
            "or CSS selector (e.g. '.price-cell span.amount') "
            "or literal 'not_present' if the field cannot be recovered."
        )
    )
    parser_fix: str | None = Field(
        default=None,
        description=(
            "If the deterministic parser can be taught to find this field, "
            "provide the exact Python expression. None if the field is only "
            "recoverable via LLM/vision."
        ),
    )


class FieldRecovery(BaseModel):
    property_id: str
    unit_fragment_hash: str = Field(description="sha256 of the source fragment, for dedup")
    tier_used: str = Field(description="Extraction tier that produced the partial unit")
    recovered_fields: list[RecoveredField] = Field(default_factory=list)
    recovery_summary: str = Field(
        description="One-sentence summary: which fields were recovered and from where"
    )
    profile_hint: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Key-value pairs to merge into the property profile's llm_field_mappings. "
            "Keys are field names, values are the source_path expressions."
        ),
    )
    cost_usd: float = Field(default=0.0, description="LLM cost in USD for this call")


# ---------------------------------------------------------------------------
# Prompt loaders
# ---------------------------------------------------------------------------


def _load_prompt(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("llm_diagnostics prompt not found at %s", path)
        return ""


def _estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """Conservative cost estimate using GPT-4o-mini pricing."""
    return (prompt_tokens * 0.00015 / 1000) + (completion_tokens * 0.00060 / 1000)


# ---------------------------------------------------------------------------
# Parser source lookup (F1 helper)
# ---------------------------------------------------------------------------


def get_adapter_parser_source(adapter_name: str) -> str:
    """Return the source of the main parser function for the given adapter.

    Uses inspect.getsource() on the known parser functions. Falls back to
    a sentinel string if the specific function is not found.
    """
    import importlib
    import inspect

    _parser_map = {
        "rentcafe": "ma_poc.pms.adapters.rentcafe.parse_rentcafe_floorplans",
        "sightmap": "ma_poc.pms.adapters.sightmap.parse_sightmap_payload",
        "appfolio": "ma_poc.pms.adapters.appfolio.parse_appfolio_listings",
        "entrata": "ma_poc.pms.adapters.entrata.parse_entrata_floorplans",
        "onesite": "ma_poc.pms.adapters.onesite.parse_realpage_floorplans",
        "realpage": "ma_poc.pms.adapters.onesite.parse_realpage_floorplans",
    }
    dotted = _parser_map.get(adapter_name.lower())
    if not dotted:
        return f"# No parser source map entry for adapter: {adapter_name}"
    module_path, func_name = dotted.rsplit(".", 1)
    try:
        mod = importlib.import_module(module_path)
        fn = getattr(mod, func_name)
        return inspect.getsource(fn)
    except Exception as exc:
        return f"# Could not retrieve source: {exc}"


# ---------------------------------------------------------------------------
# Parser logic summary (F2 helper)
# ---------------------------------------------------------------------------


def _build_parser_logic_summary(adapter_name: str, tier_used: str) -> str:
    """Return a human-readable description of what the named adapter's parser
    does with a single item dict, to give the LLM context about what was tried.
    """
    _summaries = {
        "rentcafe": (
            "The RentCafe parser receives a single floorplan dict (item_lc, "
            "lowercased keys). It reads: floor_plan_name from item_lc['floorplanname'], "
            "beds from item_lc['beds'] (integer), baths from item_lc['baths'], "
            "sqft_lo from item_lc['minimumsqft'] or item_lc['minsqft'], "
            "sqft_hi from item_lc['maximumsqft'] or item_lc['maxsqft'], "
            "rent_lo from item_lc['min_price'] (preferred, integer) or "
            "item_lc['minimumrent'] (fallback, string like '1349.00'), "
            "rent_hi from item_lc['max_price'] or item_lc['maximumrent'], "
            "unit_number from item_lc['floorplanid'] (string), "
            "avail_count from item_lc['availableunitscount'] or item_lc['unitscount'], "
            "avail_date from item_lc['availabledate']. "
            "All keys are lowercased before access via _normalise_item()."
        ),
        "sightmap": (
            "The SightMap parser joins two lists: data['units'] and data['floor_plans']. "
            "Each unit dict has: price (number), display_price (string), area (number), "
            "display_area (string), unit_number (string), label (string), floor_id, "
            "building, available_on (date string), display_available_on, "
            "specials_description, floor_plan_id (join key). "
            "Floor plan dicts have: id (join key), name, filter_label, "
            "bedroom_count (integer), bathroom_count (float). "
            "rent is taken from unit.price if > 0, else parsed from display_price. "
            "beds/baths/name come from the matched floor plan via floor_plan_id."
        ),
        "appfolio": (
            "The AppFolio parser reads JSON from the AppFolio API. "
            "Typical fields: floorplan_name, min_rent, max_rent, bedrooms, "
            "bathrooms, square_feet, available_units, next_available_date. "
            "unit_id is typically the floorplan slug or ID field."
        ),
        "generic": (
            "The generic Tier-1 parser attempts multiple field name patterns: "
            "rent from 'rent', 'price', 'market_rent', 'asking_rent', 'rentAmount'. "
            "unit_id from 'unit_number', 'unitId', 'unit_id', 'id'. "
            "beds from 'bedrooms', 'beds', 'bedroom_count'. "
            "baths from 'bathrooms', 'baths', 'bathroom_count'. "
            "sqft from 'sqft', 'square_feet', 'area', 'squareFootage'."
        ),
    }
    key = adapter_name.lower().replace("tier_1_api_", "")
    return _summaries.get(key, _summaries["generic"])


# ---------------------------------------------------------------------------
# Shared LLM call helper
# ---------------------------------------------------------------------------


async def _call_llm(
    system_prompt: str,
    user_prompt: str,
) -> tuple[str | None, int, int, str, str, int, str | None]:
    """Invoke the shared text provider at temperature 0.

    Returns:
        (raw_response, prompt_tokens, completion_tokens, model, provider,
         latency_ms, error_message).
        raw_response is None on provider failure.
    """
    try:
        from llm.factory import get_text_provider  # type: ignore[import-not-found]
        provider = get_text_provider()
    except Exception as exc:
        log.warning("llm_diagnostics: failed to get LLM provider: %s", exc)
        return None, 0, 0, "unknown", "unknown", 0, str(exc)

    t0 = time.monotonic()
    raw = ""
    error: str | None = None
    try:
        raw = await provider.complete(system_prompt, user_prompt, max_tokens=2000)
    except Exception as exc:
        error = str(exc)
        log.warning("llm_diagnostics: LLM call failed: %s", exc)

    latency_ms = int((time.monotonic() - t0) * 1000)
    usage: dict[str, Any] = getattr(provider, "_last_usage", {}) or {}
    tokens_in = int(usage.get("input_tokens", 0))
    tokens_out = int(usage.get("output_tokens", 0))
    model = str(usage.get("model", os.environ.get("AZURE_OPENAI_DEPLOYMENT", "unknown")))
    prov_name = str(usage.get("provider", "unknown"))

    if error:
        return None, tokens_in, tokens_out, model, prov_name, latency_ms, error
    return raw, tokens_in, tokens_out, model, prov_name, latency_ms, None


def _parse_json_response(raw: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction from an LLM response."""
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        pass
    # Try to strip markdown fences
    import re

    fence = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start : end + 1])  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            pass
    return None


# ---------------------------------------------------------------------------
# F1 — adapter_debugger
# ---------------------------------------------------------------------------


async def adapter_debugger(
    *,
    property_id: str,
    adapter_name: str,
    adapter_parser_source: str,
    api_response: dict[str, Any],
    property_context: dict[str, str],
    output_dir: Path,
) -> AdapterDiagnosis | None:
    """Diagnose why a Tier-1 adapter returned 0 units for a captured API response.

    Returns None on any error. Writes {output_dir}/{property_id}_adapter_debug.json.
    """
    try:
        system_template = _load_prompt(_ADAPTER_DEBUGGER_PROMPT)
        if not system_template:
            log.warning("adapter_debugger: prompt template missing")
            return None

        schema_str = json.dumps(AdapterDiagnosis.model_json_schema())
        system_prompt = system_template.replace("{SCHEMA}", schema_str)

        body = api_response.get("body")
        body_json = json.dumps(body, indent=2, default=str) if body is not None else "null"
        if len(body_json) > 8000:
            body_json = body_json[:8000] + "\n... [truncated at 8000 chars]"

        user_prompt = (
            f"ADAPTER_NAME: {adapter_name}\n\n"
            f"PROPERTY_CONTEXT:\n{json.dumps(property_context, indent=2)}\n\n"
            f"PARSER_SOURCE:\n```python\n{adapter_parser_source}\n```\n\n"
            f"PAYLOAD:\n"
            f"URL: {api_response.get('url', 'unknown')}\n"
            f"BODY (first 8000 chars):\n{body_json}\n"
        )

        raw, tokens_in, tokens_out, model, prov_name, latency_ms, error = await _call_llm(
            system_prompt, user_prompt
        )
        if raw is None:
            return None

        parsed = _parse_json_response(raw)
        if parsed is None:
            log.warning("adapter_debugger: could not parse LLM JSON for %s", property_id)
            return None

        parsed.setdefault("property_id", property_id)
        parsed.setdefault("adapter_name", adapter_name)
        parsed.setdefault("payload_url", api_response.get("url", "unknown"))

        cost = _estimate_cost(tokens_in, tokens_out)
        parsed["cost_usd"] = round(cost, 8)

        diagnosis = AdapterDiagnosis.model_validate(parsed)

        # Attach an interaction record for cost-ledger integration. The
        # jugnu hook reads scrape_result["_llm_interactions"]; callers merge
        # this dict onto that list.
        diagnosis_dict = diagnosis.model_dump(mode="json")
        diagnosis_dict["_llm_interaction"] = {
            "property_id": property_id,
            "feature": "adapter_debugger",
            "tier": "F1_ADAPTER_DEBUGGER",
            "call_type": "text",
            "provider": prov_name,
            "model": model,
            "prompt_tokens": tokens_in,
            "completion_tokens": tokens_out,
            "tokens_input": tokens_in,
            "tokens_output": tokens_out,
            "tokens_total": tokens_in + tokens_out,
            "cost_usd": round(cost, 8),
            "latency_ms": latency_ms,
            "timestamp": datetime.now(UTC).isoformat(),
            "success": error is None,
            "error": error,
        }

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            out_path = output_dir / f"{property_id}_adapter_debug.json"
            out_path.write_text(
                json.dumps(diagnosis_dict, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("adapter_debugger: failed to write diagnosis file: %s", exc)

        # Stash the interaction record on the returned model via attribute
        # assignment so callers can read it without reparsing the JSON.
        object.__setattr__(diagnosis, "_llm_interaction", diagnosis_dict["_llm_interaction"])
        return diagnosis
    except Exception as exc:
        log.warning("adapter_debugger failed for %s: %s", property_id, exc)
        return None


# ---------------------------------------------------------------------------
# F2 — null_field_recovery
# ---------------------------------------------------------------------------


async def null_field_recovery(
    *,
    property_id: str,
    partial_unit: dict[str, Any],
    source_fragment: Any,
    tier_used: str,
    parser_logic_summary: str,
    property_context: dict[str, str],
    output_dir: Path,
) -> FieldRecovery | None:
    """Recover null fields (rent_low, unit_id) from the raw source fragment.

    Returns None on error OR when the partial unit has no null fields to recover.
    Writes/appends to {output_dir}/{property_id}_field_recovery.json.
    """
    try:
        needs_recovery = (
            partial_unit.get("rent_low") is None
            or partial_unit.get("unit_id") is None
        )
        if not needs_recovery:
            return None

        system_template = _load_prompt(_NULL_FIELD_RECOVERY_PROMPT)
        if not system_template:
            log.warning("null_field_recovery: prompt template missing")
            return None

        schema_str = json.dumps(FieldRecovery.model_json_schema())
        system_prompt = system_template.replace("{SCHEMA}", schema_str)

        fragment_str = json.dumps(source_fragment, sort_keys=True, default=str)
        fragment_hash = hashlib.sha256(fragment_str.encode()).hexdigest()[:16]

        source_json = json.dumps(source_fragment, indent=2, default=str)
        if len(source_json) > 4000:
            source_json = source_json[:4000] + "\n... [truncated at 4000 chars]"

        user_prompt = (
            f"TIER_USED: {tier_used}\n\n"
            f"PROPERTY_CONTEXT:\n{json.dumps(property_context, indent=2)}\n\n"
            f"PARTIAL_UNIT (current state — null fields need recovery):\n"
            f"{json.dumps(partial_unit, indent=2, default=str)}\n\n"
            f"PARSER_LOGIC_SUMMARY:\n{parser_logic_summary}\n\n"
            f"SOURCE_FRAGMENT (raw data the parser processed):\n{source_json}\n"
        )

        raw, tokens_in, tokens_out, model, prov_name, latency_ms, error = await _call_llm(
            system_prompt, user_prompt
        )
        if raw is None:
            return None

        parsed = _parse_json_response(raw)
        if parsed is None:
            log.warning("null_field_recovery: could not parse LLM JSON for %s", property_id)
            return None

        parsed.setdefault("property_id", property_id)
        parsed.setdefault("unit_fragment_hash", fragment_hash)
        parsed.setdefault("tier_used", tier_used)

        cost = _estimate_cost(tokens_in, tokens_out)
        parsed["cost_usd"] = round(cost, 8)

        recovery = FieldRecovery.model_validate(parsed)

        recovery_dict = recovery.model_dump(mode="json")
        recovery_dict["_llm_interaction"] = {
            "property_id": property_id,
            "feature": "null_field_recovery",
            "tier": "F2_NULL_FIELD_RECOVERY",
            "call_type": "text",
            "provider": prov_name,
            "model": model,
            "prompt_tokens": tokens_in,
            "completion_tokens": tokens_out,
            "tokens_input": tokens_in,
            "tokens_output": tokens_out,
            "tokens_total": tokens_in + tokens_out,
            "cost_usd": round(cost, 8),
            "latency_ms": latency_ms,
            "timestamp": datetime.now(UTC).isoformat(),
            "success": error is None,
            "error": error,
        }

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            out_path = output_dir / f"{property_id}_field_recovery.json"
            existing: list[Any] = []
            if out_path.exists():
                try:
                    loaded = json.loads(out_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, list):
                        existing = loaded
                except Exception:
                    existing = []
            existing.append(recovery_dict)
            out_path.write_text(json.dumps(existing, indent=2, default=str), encoding="utf-8")
        except Exception as exc:
            log.warning("null_field_recovery: failed to write recovery file: %s", exc)

        object.__setattr__(recovery, "_llm_interaction", recovery_dict["_llm_interaction"])
        return recovery
    except Exception as exc:
        log.warning("null_field_recovery failed for %s: %s", property_id, exc)
        return None
