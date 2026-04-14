"""
LLM Interaction Logger — captures every LLM API call for cost accounting.

Produces two output files per daily run:
  data/runs/{date}/llm_report/{property_id}.json  — per-property report
  data/runs/{date}/llm_report.json                — run-wide aggregate

Each interaction record contains:
  property_id, tier, call_type, provider, model,
  system_prompt, user_prompt, raw_response,
  tokens_input, tokens_output, cost_usd, latency_ms,
  timestamp, success, error

Usage (in service layer):
    from llm.interaction_logger import make_interaction, compute_cost

Usage (in daily_runner):
    from llm.interaction_logger import write_property_report, write_run_summary
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Token pricing (USD per million tokens).
# Keys are the model deployment name as returned by each provider.
# Update this table when provider pricing changes.
# Sources: Anthropic pricing page / Azure OpenAI pricing — April 2026.
# ---------------------------------------------------------------------------
_MODEL_PRICING: dict[str, dict[str, float]] = {
    # Anthropic — Claude 4 series
    "claude-opus-4-20250514":     {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-20250514":   {"input":  3.00, "output": 15.00},
    "claude-haiku-4-20250514":    {"input":  0.80, "output":  4.00},
    # Anthropic — Claude 3.5 series
    "claude-3-5-sonnet-20241022": {"input":  3.00, "output": 15.00},
    "claude-3-5-haiku-20241022":  {"input":  0.80, "output":  4.00},
    # Anthropic — Claude 3 series
    "claude-3-opus-20240229":     {"input": 15.00, "output": 75.00},
    "claude-3-sonnet-20240229":   {"input":  3.00, "output": 15.00},
    "claude-3-haiku-20240307":    {"input":  0.25, "output":  1.25},
    # Azure OpenAI — GPT-4o family
    "gpt-4o":                     {"input":  2.50, "output": 10.00},
    "gpt-4o-mini":                {"input":  0.15, "output":  0.60},
    # Azure OpenAI — legacy
    "gpt-4-turbo":                {"input": 10.00, "output": 30.00},
    "gpt-4":                      {"input": 30.00, "output": 60.00},
    "gpt-35-turbo":               {"input":  0.50, "output":  1.50},
}

# Conservative fallback when the model name is not in the table.
_DEFAULT_PRICING: dict[str, float] = {"input": 3.00, "output": 15.00}


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def compute_cost(model: str, tokens_input: int, tokens_output: int) -> float:
    """Compute USD cost for a single LLM call.

    Args:
        model: Model deployment name (e.g. ``'gpt-4o-mini'``).
        tokens_input: Number of prompt/input tokens consumed.
        tokens_output: Number of completion/output tokens generated.

    Returns:
        Estimated cost in USD (not rounded — caller rounds as needed).
    """
    pricing = _MODEL_PRICING.get(model, _DEFAULT_PRICING)
    return (
        tokens_input  * pricing["input"]
        + tokens_output * pricing["output"]
    ) / 1_000_000


def make_interaction(
    *,
    property_id: str,
    tier: str,
    call_type: str,
    provider: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    raw_response: str,
    tokens_input: int,
    tokens_output: int,
    latency_ms: int,
    timestamp: str,
    success: bool,
    error: str | None = None,
) -> dict[str, Any]:
    """Build one complete LLM interaction record (plain dict, JSON-serialisable).

    Args:
        property_id: Canonical property identifier.
        tier: Extraction tier label (e.g. ``'TIER_6_LLM'``, ``'TIER_7_VISION'``).
        call_type: ``'text'`` or ``'vision'``.
        provider: ``'anthropic'`` or ``'azure'``.
        model: Model deployment name used for the call.
        system_prompt: Full system prompt sent to the API.
        user_prompt: Full user prompt / content sent to the API.
        raw_response: Raw text returned by the API.
        tokens_input: Actual input token count from the API response.
        tokens_output: Actual output token count from the API response.
        latency_ms: Wall-clock duration of the API call in milliseconds.
        timestamp: ISO-8601 UTC timestamp of when the call was made.
        success: ``True`` if the call returned a usable response.
        error: Error message if the call failed; ``None`` otherwise.

    Returns:
        Dict suitable for direct JSON serialisation.
    """
    cost = compute_cost(model, tokens_input, tokens_output)
    return {
        "property_id":   property_id,
        "tier":          tier,
        "call_type":     call_type,
        "provider":      provider,
        "model":         model,
        "system_prompt": system_prompt,
        "user_prompt":   user_prompt,
        "raw_response":  raw_response,
        "tokens_input":  tokens_input,
        "tokens_output": tokens_output,
        "tokens_total":  tokens_input + tokens_output,
        "cost_usd":      round(cost, 8),
        "latency_ms":    latency_ms,
        "timestamp":     timestamp,
        "success":       success,
        "error":         error,
    }


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------

def write_property_report(
    property_id: str,
    interactions: list[dict[str, Any]],
    run_dir: Path,
) -> None:
    """Write a per-property LLM report to ``run_dir/llm_report/{property_id}.json``.

    Args:
        property_id: Canonical property identifier (used as filename).
        interactions: All LLM interaction records for this property.
        run_dir: The daily run directory (``data/runs/{date}/``).

    Returns:
        None.  Silently no-ops when ``interactions`` is empty.
    """
    if not interactions:
        return

    llm_dir = run_dir / "llm_report"
    llm_dir.mkdir(parents=True, exist_ok=True)

    total_cost     = sum(i.get("cost_usd", 0.0)      for i in interactions)
    total_input    = sum(i.get("tokens_input", 0)     for i in interactions)
    total_output   = sum(i.get("tokens_output", 0)    for i in interactions)
    successful     = sum(1 for i in interactions if i.get("success"))

    report: dict[str, Any] = {
        "property_id":  property_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "total_calls":         len(interactions),
            "successful_calls":    successful,
            "failed_calls":        len(interactions) - successful,
            "total_tokens_input":  total_input,
            "total_tokens_output": total_output,
            "total_tokens":        total_input + total_output,
            "total_cost_usd":      round(total_cost, 6),
            "by_tier":             _group_by("tier",     interactions),
            "by_provider":         _group_by("provider", interactions),
            "by_model":            _group_by("model",    interactions),
        },
        "interactions": interactions,
    }

    safe_pid = _safe_filename(property_id)
    out_path = llm_dir / f"{safe_pid}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)


def write_run_summary(
    all_interactions: list[dict[str, Any]],
    run_dir: Path,
) -> None:
    """Write the aggregate LLM cost report for the full run.

    Output: ``run_dir/llm_report.json``

    Args:
        all_interactions: Flat list of every LLM interaction across all properties.
        run_dir: The daily run directory (``data/runs/{date}/``).

    Returns:
        None.  Silently no-ops when ``all_interactions`` is empty.
    """
    if not all_interactions:
        return

    total_cost   = sum(i.get("cost_usd", 0.0)   for i in all_interactions)
    total_input  = sum(i.get("tokens_input", 0)  for i in all_interactions)
    total_output = sum(i.get("tokens_output", 0) for i in all_interactions)
    successful   = sum(1 for i in all_interactions if i.get("success"))

    # Per-property breakdown (only summary — full records in per-property files).
    by_property: dict[str, list[dict[str, Any]]] = {}
    for i in all_interactions:
        pid = i.get("property_id", "unknown")
        by_property.setdefault(pid, []).append(i)

    property_summaries: list[dict[str, Any]] = []
    for pid, items in sorted(by_property.items()):
        prop_cost  = sum(x.get("cost_usd", 0.0)   for x in items)
        prop_in    = sum(x.get("tokens_input", 0)  for x in items)
        prop_out   = sum(x.get("tokens_output", 0) for x in items)
        property_summaries.append({
            "property_id":    pid,
            "calls":          len(items),
            "tokens_input":   prop_in,
            "tokens_output":  prop_out,
            "tokens_total":   prop_in + prop_out,
            "cost_usd":       round(prop_cost, 6),
            "successful":     sum(1 for x in items if x.get("success")),
            "failed":         sum(1 for x in items if not x.get("success")),
        })

    summary: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "total_properties_with_llm": len(by_property),
            "total_calls":               len(all_interactions),
            "successful_calls":          successful,
            "failed_calls":              len(all_interactions) - successful,
            "total_tokens_input":        total_input,
            "total_tokens_output":       total_output,
            "total_tokens":              total_input + total_output,
            "total_cost_usd":            round(total_cost, 6),
            "by_tier":                   _group_by("tier",     all_interactions),
            "by_provider":               _group_by("provider", all_interactions),
            "by_model":                  _group_by("model",    all_interactions),
        },
        "by_property": property_summaries,
    }

    out_path = run_dir / "llm_report.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _group_by(field: str, interactions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate interactions by a single field (tier / provider / model)."""
    result: dict[str, dict[str, Any]] = {}
    for i in interactions:
        key = str(i.get(field, "unknown"))
        if key not in result:
            result[key] = {"calls": 0, "cost_usd": 0.0, "tokens_total": 0}
        result[key]["calls"]        += 1
        result[key]["cost_usd"]     += i.get("cost_usd", 0.0)
        result[key]["tokens_total"] += i.get("tokens_total", 0)
    for v in result.values():
        v["cost_usd"] = round(v["cost_usd"], 6)
    return result


def _safe_filename(s: str, max_len: int = 80) -> str:
    """Convert an arbitrary string to a safe filename fragment."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:max_len]
