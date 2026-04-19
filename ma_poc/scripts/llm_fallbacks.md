# CLAUDE_LLM_DIAGNOSTIC.md

> Read this entire document before writing a single line of code. Two new
> capabilities are being added. They share one new service file and two new
> prompt templates. Read all three sections (Overview, Feature 1, Feature 2)
> before starting Phase 0.

---

## What this document delivers

| Feature | Trigger | What it does |
|---|---|---|
| **F1 — Adapter Debugger** | `FAILED_NO_DATA` after any `TIER_1_*` tier | Sends the raw captured API payload + the adapter's full parser source to LLM; gets back a structured JSON diagnosis of what went wrong and exactly what code changes are needed |
| **F2 — Null Field Recovery** | Unit extracted but `rent_low is None` OR `unit_id is None` | Sends the raw source fragment that produced the unit back to LLM; gets back the missing field values and the selector/path fix so the deterministic parser learns |

Both features are **diagnostic and one-time** — they run on first failure/first
null, write their findings to a per-property diagnosis file, and inform profile
learning. They are not recurring per-scrape costs.

---

## Non-negotiables (apply to everything in this document)

1. **Never-fail contract preserved.** Every new code path is wrapped in
   `try/except Exception`. A failure in F1 or F2 must never crash a property
   scrape or the run. Log and continue.
2. **LLM is the teacher, not the worker.** F1 and F2 run once per unknown
   failure pattern. Once a fix is identified and the adapter is patched, the
   same failure should not trigger F1 again (gated by profile state).
3. **No new top-level imports in `jugnu_runner.py` or adapter files.** All new
   code lives in `ma_poc/services/llm_diagnostics.py`. Imports inside
   functions are acceptable.
4. **Pydantic v2 only.** Use `model_dump(mode="json")`, never `.dict()`.
5. **Async throughout.** All LLM calls are `async def`. Use `await`.
6. **Temperature 0.0** on all LLM calls. These are analysis tasks, not
   creative tasks.
7. **Write tests immediately after each phase.** Do not move to the next phase
   until current tests pass.
8. **`hashlib.sha256` for all hashing.** Never `hash()`.

---

## Repository orientation

Before writing any code, run these reads:

```bash
cat ma_poc/pms/adapters/rentcafe.py
cat ma_poc/pms/adapters/sightmap.py
cat ma_poc/services/llm_extractor.py       # existing LLM service — match its client pattern exactly
grep -n "FAILED_NO_DATA\|TIER_1_API\|_extract_result" scripts/jugnu_runner.py | head -40
grep -n "_format_v2_unit\|rent_low\|unit_id\|null" scripts/jugnu_runner.py | head -40
```

The Azure OpenAI client setup, model name (`AZURE_OPENAI_DEPLOYMENT` env var),
and `api_version` are already established in `ma_poc/services/llm_extractor.py`.
Copy that exact client initialisation pattern — do not create a second client.

---

## Phase 0 — Shared infrastructure

### 0.1 Create `ma_poc/services/llm_diagnostics.py`

This is the only new Python file in this entire task. Both F1 and F2 live here.

```
ma_poc/services/llm_diagnostics.py
```

Top-of-file docstring:

```python
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
```

### 0.2 Create two Pydantic v2 response models in `llm_diagnostics.py`

#### `AdapterDiagnosis` — F1 response model

```python
from pydantic import BaseModel, Field
from typing import Literal

class FieldMappingFix(BaseModel):
    original_key: str = Field(description="Key name the parser looks for")
    actual_key: str = Field(description="Key name present in the actual payload")
    fix_type: Literal["rename", "case_normalise", "unwrap", "new_path", "missing_data"] = Field(
        description=(
            "rename: field exists under a different name. "
            "case_normalise: same name, different casing (e.g. FloorplanName vs floorplanName). "
            "unwrap: data exists but is nested under an extra wrapper key. "
            "new_path: data is at a completely different JSON path. "
            "missing_data: field genuinely absent from this payload."
        )
    )
    example_value: str | None = Field(
        default=None,
        description="The actual value from the payload for this field, for verification"
    )
    code_change: str = Field(
        description=(
            "Exact Python expression to extract this field from item_lc (the lowercased "
            "item dict). Examples: "
            "\"item_lc.get('floorplanname')\" "
            "\"item_lc.get('minimumrent') or item_lc.get('startingrent')\" "
            "\"str(item_lc.get('data', {}).get('rent', ''))\" "
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
    ] = Field(
        description=(
            "Primary category. Use 'multiple_issues' only if two or more distinct "
            "categories apply simultaneously."
        )
    )
    wrapper_fix: WrapperFix | None = Field(
        default=None,
        description="Present if the list is nested inside a wrapper the parser doesn't handle"
    )
    field_fixes: list[FieldMappingFix] = Field(
        default_factory=list,
        description="One entry per field that is wrong or missing in the parser"
    )
    can_auto_fix: bool = Field(
        description=(
            "True if the fixes are purely mechanical (rename/case/unwrap) and "
            "can be applied without manual inspection. False if auth, pagination, "
            "or genuinely absent data is involved."
        )
    )
    estimated_units_recoverable: int = Field(
        description="How many unit records would be extractable after applying these fixes"
    )
    adapter_code_patch: str = Field(
        description=(
            "A complete, copy-pasteable Python diff or replacement snippet for the "
            "adapter's parser function. Show ONLY the changed lines with enough "
            "surrounding context (3 lines before/after) to locate them. "
            "Use unified diff format: lines starting with - are removals, "
            "lines starting with + are additions."
        )
    )
```

#### `FieldRecovery` — F2 response model

```python
class RecoveredField(BaseModel):
    field_name: Literal["rent_low", "rent_high", "unit_id", "floor_plan_name",
                        "beds", "baths", "area", "available_date"]
    recovered_value: str | int | float | None = Field(
        description="The actual value extracted from the source fragment"
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
            "provide the exact Python expression. "
            "E.g.: \"item_lc.get('askingrent') or item_lc.get('marketrent')\" "
            "None if the field is only recoverable via LLM/vision."
        )
    )


class FieldRecovery(BaseModel):
    property_id: str
    unit_fragment_hash: str = Field(description="sha256 of the source fragment, for dedup")
    tier_used: str = Field(description="Extraction tier that produced the partial unit")
    recovered_fields: list[RecoveredField]
    recovery_summary: str = Field(
        description="One-sentence summary: which fields were recovered and from where"
    )
    profile_hint: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Key-value pairs to merge into the property profile's llm_field_mappings. "
            "Keys are field names, values are the source_path expressions."
        )
    )
```

### 0.3 Create two prompt template files

```
config/prompts/adapter_debugger.txt
config/prompts/null_field_recovery.txt
```

These are loaded at runtime with `Path(...).read_text()`. Do not hardcode
prompts as Python strings inside the service.

---

## Phase 1 — F1: Adapter Debugger

### 1.1 Prompt template: `config/prompts/adapter_debugger.txt`

Write exactly this content to the file (this is the system prompt):

```
You are a senior Python engineer debugging a web scraping pipeline for an
apartment rent intelligence platform. Your job is to analyse why a specific
PMS (Property Management System) adapter detected the correct platform but
extracted zero unit records.

You will receive:
  1. ADAPTER_NAME — the name of the PMS adapter (e.g. "rentcafe", "sightmap")
  2. PARSER_SOURCE — the complete Python source of the parser function
  3. PAYLOAD — the raw JSON API response body that was captured but not parsed
  4. PROPERTY_CONTEXT — property name, website, and canonical ID

Your task:
  - Carefully read the parser logic in PARSER_SOURCE
  - Carefully read the actual field names and structure in PAYLOAD
  - Identify every mismatch between what the parser expects and what the
    payload actually contains
  - Produce a precise, actionable diagnosis

Rules:
  - Quote actual values from the payload as evidence — do not guess
  - If the payload genuinely contains no unit data (e.g. it is a config
    endpoint, a chatbot payload, or an amenities-only response), say so
    clearly and set can_auto_fix to false
  - For field name mismatches: state the EXACT key the parser looks for and
    the EXACT key present in the payload
  - For the adapter_code_patch: produce a minimal unified diff. Show only the
    lines that change. Do not rewrite the whole function.
  - estimated_units_recoverable: count the actual items in the payload list
    that would become valid unit records after your fixes
  - Be precise about wrapper depth: if the payload is {"data": [...]}, the
    wrapper_key_path is ["data"]. If {"response": {"result": [...]}}, it is
    ["response", "result"]

You MUST respond with valid JSON only. No markdown fences, no explanation
outside the JSON. The JSON must conform exactly to the AdapterDiagnosis schema.

AdapterDiagnosis schema:
{SCHEMA}
```

The `{SCHEMA}` placeholder is replaced at runtime with
`AdapterDiagnosis.model_json_schema()` serialised as a compact JSON string.

### 1.2 Implement `adapter_debugger()` in `llm_diagnostics.py`

```python
async def adapter_debugger(
    *,
    property_id: str,
    adapter_name: str,
    adapter_parser_source: str,
    api_response: dict[str, Any],
    property_context: dict[str, str],
    output_dir: Path,
) -> AdapterDiagnosis | None:
    """
    Diagnose why a Tier-1 adapter returned 0 units for a captured API response.

    Args:
        property_id: Canonical property ID (e.g. "35534").
        adapter_name: PMS name string (e.g. "rentcafe", "sightmap").
        adapter_parser_source: Full source of the adapter's parser function,
            obtained via inspect.getsource(parse_rentcafe_floorplans).
        api_response: The raw captured response dict with keys "url" and "body".
        property_context: {"property_name": ..., "website": ..., "city": ..., "state": ...}
        output_dir: data/runs/{date}/llm_diagnostics/ — diagnosis JSON written here.

    Returns:
        AdapterDiagnosis if LLM call succeeded, None on any error.

    Side effects:
        Writes {output_dir}/{property_id}_adapter_debug.json.
    """
```

Implementation requirements:

**Input assembly:** Build the user message as follows:

```
ADAPTER_NAME: {adapter_name}

PROPERTY_CONTEXT:
{json.dumps(property_context, indent=2)}

PARSER_SOURCE:
```python
{adapter_parser_source}
```

PAYLOAD:
URL: {api_response.get("url", "unknown")}
BODY (first 8000 chars):
{json.dumps(api_response.get("body"), indent=2)[:8000]}
```

Truncate body at 8000 characters to stay within token budget. If truncated,
append `"\n... [truncated at 8000 chars]"`.

**System prompt assembly:** Load `config/prompts/adapter_debugger.txt`, then
replace `{SCHEMA}` with `json.dumps(AdapterDiagnosis.model_json_schema())`.

**LLM call:**
- Model: `os.environ["AZURE_OPENAI_DEPLOYMENT"]` (same as `llm_extractor.py`)
- Temperature: 0.0
- max_tokens: 2000
- response_format: `{"type": "json_object"}` if the deployment supports it;
  otherwise rely on prompt instruction

**Response parsing:**
```python
raw = response.choices[0].message.content
data = json.loads(raw)
diagnosis = AdapterDiagnosis.model_validate(data)
```

**Output:** Write to `{output_dir}/{property_id}_adapter_debug.json`:
```python
output_dir.mkdir(parents=True, exist_ok=True)
out_path = output_dir / f"{property_id}_adapter_debug.json"
out_path.write_text(diagnosis.model_dump_json(indent=2))
```

**Error handling:** Wrap the entire function body in:
```python
try:
    ...
except Exception as exc:
    log.warning("adapter_debugger failed for %s: %s", property_id, exc)
    return None
```

### 1.3 Implement `get_adapter_parser_source()` helper

```python
def get_adapter_parser_source(adapter_name: str) -> str:
    """Return the source of the main parser function for the given adapter.

    Uses inspect.getsource() on the known parser functions. Falls back to
    the adapter module's full source if the specific function is not found.
    """
    import inspect
    _PARSER_MAP = {
        "rentcafe": "ma_poc.pms.adapters.rentcafe.parse_rentcafe_floorplans",
        "sightmap": "ma_poc.pms.adapters.sightmap.parse_sightmap_payload",
        "appfolio": "ma_poc.pms.adapters.appfolio.parse_appfolio_units",
        "entrata":  "ma_poc.pms.adapters.entrata.parse_entrata_units",
        "onesite":  "ma_poc.pms.adapters.onesite.parse_onesite_units",
    }
    dotted = _PARSER_MAP.get(adapter_name)
    if not dotted:
        return f"# No parser source map entry for adapter: {adapter_name}"
    module_path, func_name = dotted.rsplit(".", 1)
    try:
        import importlib
        mod = importlib.import_module(module_path)
        fn = getattr(mod, func_name)
        return inspect.getsource(fn)
    except Exception as exc:
        return f"# Could not retrieve source: {exc}"
```

### 1.4 Hook F1 into `jugnu_runner.py`

Find the section where `scrape_result` is assembled after the Jugnu scrape
call and a property gets `verdict == "FAILED_NO_DATA"`. This is approximately
around the per-property loop where `_format_v2` / `_format_v1` is called and
`_extract_result` is populated.

After the property record is written to the results list and the failure is
confirmed as `FAILED_NO_DATA` with a `TIER_1_*` tier, add this block:

```python
# ── F1: Adapter Debugger (runs once per FAILED_NO_DATA on TIER_1_* tiers) ──
_tier_used = (scrape_result.get("_extract_result") or {}).get("tier_used", "")
if (
    verdict == "FAILED_NO_DATA"
    and _tier_used.startswith("TIER_1_")
):
    try:
        from ma_poc.services.llm_diagnostics import (
            adapter_debugger,
            get_adapter_parser_source,
        )
        _raw_apis = scrape_result.get("_raw_api_responses") or []
        _adapter_name = _tier_used.replace("TIER_1_API_", "").replace("TIER_1_API", "generic").lower()
        _diag_dir = run_dir / "llm_diagnostics"

        for _resp in _raw_apis[:3]:  # max 3 candidates per property
            _diag = await adapter_debugger(
                property_id=canonical_id,
                adapter_name=_adapter_name,
                adapter_parser_source=get_adapter_parser_source(_adapter_name),
                api_response=_resp,
                property_context={
                    "property_name": csv_row.get("name") or csv_row.get("proj_name") or "",
                    "website": csv_row.get("website") or "",
                    "city": csv_row.get("city") or "",
                    "state": csv_row.get("state") or "",
                },
                output_dir=_diag_dir,
            )
            if _diag:
                log.info(
                    "  F1 diagnosis for %s: %s (can_auto_fix=%s, recoverable=%d units)",
                    canonical_id,
                    _diag.failure_category,
                    _diag.can_auto_fix,
                    _diag.estimated_units_recoverable,
                )
                break  # stop after first successful diagnosis per property
    except Exception as _exc:
        log.debug("F1 adapter_debugger hook failed for %s: %s", canonical_id, _exc)
```

**Cost guard:** Before calling `adapter_debugger`, check whether a diagnosis
file already exists for this property and run date. If it does, skip. This
prevents re-spending LLM budget on properties whose failure is already diagnosed
but not yet fixed:

```python
_diag_path = _diag_dir / f"{canonical_id}_adapter_debug.json"
if _diag_path.exists():
    log.debug("F1 diagnosis already exists for %s, skipping", canonical_id)
    # skip the adapter_debugger call
```

---

## Phase 2 — F2: Null Field Recovery

### 2.1 Prompt template: `config/prompts/null_field_recovery.txt`

Write exactly this content:

```
You are a senior Python engineer working on an apartment rent intelligence
pipeline. A scraping adapter successfully extracted a unit record, but one
or more critical fields are null or missing.

You will receive:
  1. TIER_USED — the extraction tier that produced this unit
  2. PARTIAL_UNIT — the incomplete unit record as it currently stands
  3. SOURCE_FRAGMENT — the raw data fragment (JSON object or HTML snippet)
     that the parser processed to produce this unit
  4. PARSER_LOGIC_SUMMARY — a description of how the parser reads this
     fragment, including which fields it looks for and in what order
  5. PROPERTY_CONTEXT — property name, website, city, state

Your task:
  - Find the missing field values inside SOURCE_FRAGMENT
  - For each missing field, provide the exact value AND the path/selector
    to reach it
  - If a field genuinely does not exist in SOURCE_FRAGMENT, say so with
    confidence 0.0 and source_path "not_present"
  - For each recovered field where a deterministic parser fix is possible,
    write the exact Python expression that would extract it
    (e.g. "item_lc.get('askingrent')" or "float(item['data']['price'])")

Critical field priority (in order):
  1. rent_low — market rent low in dollars (must be a number > 0)
  2. unit_id — unique identifier for this specific unit or floor plan
  3. beds — bedroom count (integer; 0 for studio)
  4. area — square footage (integer; use -1 only if truly absent)
  5. available_date — ISO date string "YYYY-MM-DD" or null

Rules:
  - Quote actual values from SOURCE_FRAGMENT as evidence
  - Do not hallucinate values. If you cannot find it, confidence must be < 0.3
  - confidence >= 0.85 means you are certain of the value
  - confidence 0.5–0.84 means the value is probable but verify
  - confidence < 0.5 means you found something but it may not be correct
  - parser_fix must be a Python expression that accesses a dict named
    item_lc (lowercase-normalised) or item (raw)
  - Only include recovered_fields for fields that are currently null in
    PARTIAL_UNIT; do not repeat already-populated fields

You MUST respond with valid JSON only. No markdown fences, no preamble.
The JSON must conform exactly to the FieldRecovery schema.

FieldRecovery schema:
{SCHEMA}
```

### 2.2 Implement `null_field_recovery()` in `llm_diagnostics.py`

```python
async def null_field_recovery(
    *,
    property_id: str,
    partial_unit: dict[str, Any],
    source_fragment: dict[str, Any] | str,
    tier_used: str,
    parser_logic_summary: str,
    property_context: dict[str, str],
    output_dir: Path,
) -> FieldRecovery | None:
    """
    Recover null fields (rent_low, unit_id) from the raw source fragment.

    Args:
        property_id: Canonical property ID.
        partial_unit: The unit dict as produced by the adapter, with null fields.
        source_fragment: The raw JSON object or HTML snippet the adapter parsed.
            For Tier 1 API adapters: the specific item dict from the floorplan list.
            For Tier 3 DOM: the relevant HTML section as a string.
        tier_used: e.g. "TIER_1_API_RENTCAFE", "TIER_3_DOM".
        parser_logic_summary: Plain-English description of what the parser does
            with this fragment (see _build_parser_logic_summary()).
        property_context: {"property_name": ..., "website": ..., "city": ..., "state": ...}
        output_dir: Diagnosis output directory.

    Returns:
        FieldRecovery if successful, None on error.

    Side effects:
        Writes {output_dir}/{property_id}_field_recovery.json.
        Appends (does not overwrite) if the file already exists — one file
        can contain multiple recovery events for the same property.
    """
```

**Null field detection:** Only call this function if the partial unit has at
least one of these conditions:
```python
needs_recovery = (
    partial_unit.get("rent_low") is None
    or partial_unit.get("unit_id") is None
)
```

**Input assembly:**

```
TIER_USED: {tier_used}

PROPERTY_CONTEXT:
{json.dumps(property_context, indent=2)}

PARTIAL_UNIT (current state — null fields need recovery):
{json.dumps(partial_unit, indent=2)}

PARSER_LOGIC_SUMMARY:
{parser_logic_summary}

SOURCE_FRAGMENT (raw data the parser processed):
{json.dumps(source_fragment, indent=2)[:4000]}
```

Truncate source_fragment at 4000 characters.

**Fragment hash:**
```python
import hashlib, json
fragment_str = json.dumps(source_fragment, sort_keys=True, default=str)
fragment_hash = hashlib.sha256(fragment_str.encode()).hexdigest()[:16]
```

**Output:** Write/append to `{output_dir}/{property_id}_field_recovery.json`
as a JSON array (read existing array if file exists, append new entry, rewrite):
```python
out_path = output_dir / f"{property_id}_field_recovery.json"
existing = []
if out_path.exists():
    try:
        existing = json.loads(out_path.read_text())
    except Exception:
        existing = []
existing.append(json.loads(recovery.model_dump_json()))
out_path.write_text(json.dumps(existing, indent=2))
```

### 2.3 Implement `_build_parser_logic_summary()` helper

```python
def _build_parser_logic_summary(adapter_name: str, tier_used: str) -> str:
    """
    Return a human-readable description of what the named adapter's parser
    does with a single item dict, to give the LLM context about what was tried.

    This is a static lookup — not auto-generated. Add entries as adapters grow.
    """
    _SUMMARIES = {
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
    return _SUMMARIES.get(key, _SUMMARIES["generic"])
```

### 2.4 Hook F2 into `jugnu_runner.py`

F2 runs inside `_format_v2_unit()` AFTER units are extracted and formatted but
BEFORE the property record is written. Specifically: after the list comprehension
`[_format_v2_unit(u, scrape_ts) for u in units]` runs, check each unit.

Add this block after the units list is built in `_format_v2()`:

```python
# ── F2: Null Field Recovery ────────────────────────────────────────────────
# Only run for units that come from Tier-1 adapters (raw_api source available)
# and have null rent_low or unit_id. Max 5 units per property per run.
_tier = meta.get("scrape_tier_used", "") or ""
_raw_apis = result.get("_raw_api_responses") or []
if _tier.startswith("TIER_1_") and _raw_apis:
    _null_units = [u for u in prop["units"]
                   if u.get("rent_low") is None or u.get("unit_id") is None]
    if _null_units:
        try:
            from ma_poc.services.llm_diagnostics import (
                null_field_recovery,
                _build_parser_logic_summary,
            )
            import asyncio as _asyncio

            _adapter_name = _tier.replace("TIER_1_API_", "").lower()
            _diag_dir = Path("data") / "runs" / meta.get("run_date", "unknown") / "llm_diagnostics"

            # Build source fragment map: match raw API items to output units by index
            # Use the first captured API response body as the source
            _source_body = _raw_apis[0].get("body") if _raw_apis else {}
            _source_items = []
            if isinstance(_source_body, list):
                _source_items = _source_body
            elif isinstance(_source_body, dict):
                for _k in ("data", "results", "floorplans", "FloorplanList", "Result"):
                    _v = _source_body.get(_k)
                    if isinstance(_v, list):
                        _source_items = _v
                        break

            for _i, _unit in enumerate(_null_units[:5]):
                _fragment = _source_items[_i] if _i < len(_source_items) else _source_body
                _recovery = _asyncio.get_event_loop().run_until_complete(
                    null_field_recovery(
                        property_id=canonical_id,
                        partial_unit=_unit,
                        source_fragment=_fragment,
                        tier_used=_tier,
                        parser_logic_summary=_build_parser_logic_summary(_adapter_name, _tier),
                        property_context={
                            "property_name": prop.get("proj_name") or "",
                            "website": prop.get("website") or "",
                            "city": prop.get("city") or "",
                            "state": prop.get("state") or "",
                        },
                        output_dir=_diag_dir,
                    )
                )
                if _recovery:
                    # Apply high-confidence recoveries directly to the unit record
                    for _rf in _recovery.recovered_fields:
                        if _rf.confidence >= 0.85 and _rf.recovered_value is not None:
                            if _rf.field_name == "rent_low" and _unit.get("rent_low") is None:
                                _unit["rent_low"] = _format_rent(_rf.recovered_value)
                            elif _rf.field_name == "unit_id" and _unit.get("unit_id") is None:
                                _unit["unit_id"] = str(_rf.recovered_value)
        except Exception as _exc:
            log.debug("F2 null_field_recovery hook failed for %s: %s", canonical_id, _exc)
```

**Important:** The in-place update of `_unit["rent_low"]` and `_unit["unit_id"]`
modifies the unit dict that is already in `prop["units"]` (same object reference).
No separate copy/reassignment needed.

**Async note:** If `jugnu_runner.py` is already inside an async context (it is —
the main loop is async), replace `asyncio.get_event_loop().run_until_complete(...)`
with `await null_field_recovery(...)` and make the calling context `async def`.

---

## Phase 3 — Cost ledger integration

Every LLM call made by F1 and F2 must write to the run's cost ledger so the
`llm_cost_per_run` SLO is accurate.

In `llm_diagnostics.py`, after each successful LLM response, compute and emit:

```python
def _estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate cost using GPT-4o-mini pricing as a conservative upper bound."""
    # GPT-4o-mini: $0.00015/1K input, $0.00060/1K output
    return (prompt_tokens * 0.00015 / 1000) + (completion_tokens * 0.00060 / 1000)
```

Add a `cost_usd` field to both `AdapterDiagnosis` and `FieldRecovery`:

```python
cost_usd: float = Field(default=0.0, description="LLM cost in USD for this call")
```

Populate it from `response.usage.prompt_tokens` and
`response.usage.completion_tokens` after the LLM call.

The cost ledger write is already handled by jugnu_runner's LLM interaction
logger — emit a compatible dict from F1/F2:

```python
{
    "property_id": property_id,
    "feature": "adapter_debugger",  # or "null_field_recovery"
    "model": os.environ.get("AZURE_OPENAI_DEPLOYMENT", "unknown"),
    "prompt_tokens": response.usage.prompt_tokens,
    "completion_tokens": response.usage.completion_tokens,
    "cost_usd": cost,
    "timestamp": datetime.now(UTC).isoformat(),
}
```

Store this in `AdapterDiagnosis` / `FieldRecovery` under a field
`_llm_interaction: dict` (prefixed underscore = internal, not part of the
schema contract). The jugnu hook reads this and appends to
`scrape_result["_llm_interactions"]` so the existing cost ledger picks it up.

---

## Phase 4 — Tests

Create `tests/services/test_llm_diagnostics.py`.

All tests that make LLM calls must use `pytest.mark.llm` and be skippable
with `pytest -m "not llm"` for fast CI runs. Tests that do not make LLM calls
have no mark.

### F1 tests

**DG_T01 — `AdapterDiagnosis` model validates correctly**
```
Input: a dict matching the schema with all required fields
Expected: AdapterDiagnosis.model_validate(data) succeeds, no ValidationError
```

**DG_T02 — `get_adapter_parser_source("rentcafe")` returns non-empty source**
```
Expected: returns a string containing "def parse_rentcafe_floorplans"
```

**DG_T03 — `get_adapter_parser_source("unknown_pms")` returns fallback string**
```
Expected: returns a string starting with "# No parser source map entry"
```

**DG_T04 — `adapter_debugger()` returns None on LLM client error (unit test)**
```
Mock the LLM client to raise an Exception.
Expected: adapter_debugger() returns None without raising.
```

**DG_T05 — `adapter_debugger()` with real PascalCase RentCafe payload (LLM test)**
```
pytest.mark.llm
Input: rentcafe adapter, payload = {"data": [{"FloorplanName": "1BR",
       "FloorplanId": "FP1", "MinimumRent": "2100.00", "MaximumRent": "2400.00",
       "AvailableUnitsCount": 2, "Beds": 1, "Baths": 1}]}
Expected:
  - diagnosis.failure_category in ("case_mismatch", "multiple_issues")
  - len(diagnosis.field_fixes) >= 2
  - diagnosis.can_auto_fix == True
  - diagnosis.estimated_units_recoverable >= 1
  - diagnosis.adapter_code_patch contains a "-" line and a "+" line
  - output JSON file written to output_dir
```

**DG_T06 — `adapter_debugger()` with genuinely empty amenities payload (LLM test)**
```
pytest.mark.llm
Input: sightmap adapter, payload = {"data": {"amenities": [{"id":1,"name":"Pool"}],
       "units": [], "floor_plans": []}}
Expected:
  - diagnosis.failure_category == "genuinely_no_data" or "wrong_endpoint_type"
  - diagnosis.can_auto_fix == False
  - diagnosis.estimated_units_recoverable == 0
```

### F2 tests

**NF_T01 — `FieldRecovery` model validates correctly**
```
Input: valid dict with recovered_fields list
Expected: FieldRecovery.model_validate(data) succeeds
```

**NF_T02 — `_build_parser_logic_summary("rentcafe", "TIER_1_API_RENTCAFE")`**
```
Expected: returns a string containing "minimumrent" and "floorplanid"
```

**NF_T03 — `null_field_recovery()` returns None when needs_recovery is False**
```
Input: partial_unit with rent_low=1800.0 and unit_id="FP1" (both populated)
Expected: function returns None immediately without LLM call
```

**NF_T04 — `null_field_recovery()` returns None on LLM error (unit test)**
```
Mock LLM to raise. Expected: returns None, does not raise.
```

**NF_T05 — `null_field_recovery()` with RentCafe PascalCase item (LLM test)**
```
pytest.mark.llm
Input:
  partial_unit = {"beds": 1, "baths": 1, "floor_plan_name": None,
                  "area": 700, "unit_id": None, "rent_low": None,
                  "rent_high": None}
  source_fragment = {"FloorplanName": "Aspen", "FloorplanId": "A1",
                     "Beds": 1, "Baths": 1, "MinimumRent": "2195.00",
                     "MaximumRent": "2395.00", "AvailableUnitsCount": 3,
                     "MinimumSQFT": "685"}
  tier_used = "TIER_1_API_RENTCAFE"
Expected:
  - recovery is not None
  - len(recovery.recovered_fields) >= 2
  - recovered rent_low field has confidence >= 0.85 and recovered_value ~= 2195
  - recovered unit_id field has source_path containing "floorplanid" or "FloorplanId"
  - output JSON file written
```

**NF_T06 — High-confidence recovery is applied to unit dict in jugnu hook**
```
Unit test (no LLM). Mock null_field_recovery to return a FieldRecovery with
one RecoveredField(field_name="rent_low", recovered_value=2195.0, confidence=0.9).
Expected: after hook runs, unit["rent_low"] == 2195.0
```

---

## Phase 5 — Final gate

```bash
# Fast tests only (no LLM calls)
pytest tests/services/test_llm_diagnostics.py -m "not llm" -v

# Full including LLM (requires AZURE_OPENAI_DEPLOYMENT env var)
pytest tests/services/test_llm_diagnostics.py -v

# Regression: no breakage elsewhere
pytest tests/ -m "not llm" -v
```

Pass criteria:
- All non-LLM tests green with no LLM env var set.
- All LLM tests green with env var set.
- Zero regressions in existing test suite.
- `mypy ma_poc/services/llm_diagnostics.py --ignore-missing-imports` exits 0.

---

## What is explicitly out of scope

- Do not modify `rentcafe.py` or `sightmap.py` — those fixes are in
  `CLAUDE_ADAPTER_FIXES.md`. F1's output (the `adapter_code_patch` field)
  describes what to fix; the actual code change is a separate human-reviewed step.
- Do not implement automatic code patching from F1's output. F1 diagnoses;
  a human (or a future Loop A task) applies the patch.
- Do not add F2 to Tier 3 DOM extraction. DOM fragment extraction is complex
  and out of scope; F2 is wired only to Tier 1 API adapters in this task.
- Do not change the 46-key v1 output schema.
- Do not add vision LLM calls. F1 and F2 use text-only models.