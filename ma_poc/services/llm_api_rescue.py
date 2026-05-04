"""
LLM rescue service for Tier-1 API adapters.

When a generic/entrata/appfolio adapter captures API responses but its
deterministic parser yields zero substantive units, this service sends the
top-ranked response bodies to an LLM and extracts units + replay hints.

The orchestrator (ma_poc/pms/scraper.py) is the ONLY caller. Adapters do not
import this module — they remain deterministic by design.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ma_poc.extraction.heuristics import (
    looks_like_availability_api,
    response_looks_like_units,
)

log = logging.getLogger(__name__)

# Hard caps — do not make configurable without a design review.
MAX_LLM_CALLS_PER_PROPERTY = 2
MAX_BODY_TOKENS = 12_000
MAX_BODY_ARRAY_ITEMS = 200
MAX_CANDIDATES = 3

SUPPORTED_ADAPTERS: frozenset[str] = frozenset({"generic", "entrata", "appfolio"})

KNOWN_PMS_HOSTS: frozenset[str] = frozenset(
    {
        "rentcafe.com",
        "entrata.com",
        "appfolio.com",
        "onesite.realpage.com",
        "sightmap.com",
        "yardione.com",
        "rentmanager.com",
    }
)

_NAV_MARKETING_KEYS = frozenset(
    {
        "nav",
        "navigation",
        "menu",
        "footer",
        "header",
        "banner",
        "promo",
        "promotions",
        "gallery",
        "photos",
        "images",
        "amenities",
        "reviews",
        "testimonials",
        "blog",
        "events",
        "seo",
        "meta",
        "_links",
    }
)

_UNIT_KEY_SIGNALS = frozenset(
    {
        "bed",
        "rent",
        "sqft",
        "floor",
        "unit",
        "plan",
    }
)

_NUMERIC_SEGMENT_RE = re.compile(r"/\d+(?=/|$)")


@dataclass
class RescueInput:
    """Input to the LLM rescue service."""

    property_id: str
    property_context: dict[str, Any]  # keys: name, website, city, expected_units
    source_adapter: str  # 'generic' | 'entrata' | 'appfolio'
    api_responses: list[dict]  # each: {url, body, content_type}
    profile_snapshot: dict | None  # current profile dict — for blocked_endpoints


@dataclass
class RescueOutput:
    """Output from the LLM rescue service."""

    units: list[dict] = field(default_factory=list)
    tier_used: str = ""  # empty on failure
    winning_url: str | None = None
    llm_field_mappings: list[dict] = field(default_factory=list)
    blocked_endpoints: list[tuple[str, str]] = field(default_factory=list)
    cost_usd: float = 0.0
    confidence: float = 0.0
    errors: list[str] = field(default_factory=list)
    n_llm_calls: int = 0


# ── Candidate filtering ───────────────────────────────────────────────────────


def _registered_domain(url: str) -> str:
    """Extract the last two domain labels (registered domain)."""
    try:
        host = urlparse(url).hostname or ""
        parts = host.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return host
    except Exception:
        return ""


def _filter_candidates(
    api_responses: list[dict],
    profile_snapshot: dict | None,
) -> list[dict]:
    """Drop responses that are definitely not unit data."""
    blocked_patterns: list[str] = []
    if profile_snapshot:
        hints = profile_snapshot.get("api_hints") or {}
        for ep in hints.get("blocked_endpoints") or []:
            if isinstance(ep, dict):
                pat = ep.get("url_pattern", "")
            else:
                pat = str(ep)
            if pat:
                blocked_patterns.append(pat)

    kept: list[dict] = []
    for r in api_responses:
        url = r.get("url", "")
        body = r.get("body")

        # Drop empty bodies
        if body is None or body == "" or body == [] or body == {}:
            continue

        # Drop if URL matches a blocked endpoint
        if any(pat in url for pat in blocked_patterns):
            continue

        # Attempt JSON parse if body is a string
        if isinstance(body, str):
            stripped = body.strip()
            if not stripped.startswith(("{", "[")):
                continue
            try:
                body = json.loads(stripped)
                r = dict(r)
                r["body"] = body
            except json.JSONDecodeError:
                continue

        kept.append(r)

    return kept


# ── Candidate ranking ─────────────────────────────────────────────────────────


def _score(r: dict) -> float:
    """Score an API response by likelihood of containing unit data."""
    score = 0.0
    url = r.get("url", "")
    body = r.get("body")
    if looks_like_availability_api(url):
        score += 0.5
    if response_looks_like_units(body):
        score += 1.0
    if isinstance(body, dict):
        unit_keys = _UNIT_KEY_SIGNALS
        all_keys: set[str] = set(body.keys())
        for v in body.values():
            if isinstance(v, dict):
                all_keys.update(v.keys())
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                all_keys.update(v[0].keys())
        overlap = sum(1 for k in all_keys if any(u in k.lower() for u in unit_keys))
        score += min(overlap * 0.1, 1.0)
    return score


def _rank_candidates(candidates: list[dict]) -> list[dict]:
    """Sort candidates by score descending; tie-break by URL length (shorter first)."""
    return sorted(candidates, key=lambda r: (-_score(r), len(r.get("url", ""))))


# ── Body trimming ─────────────────────────────────────────────────────────────


def _find_units_array(obj: Any, depth: int = 0) -> list | None:
    """DFS search for the first array of dicts with unit-like keys."""
    if depth > 5:
        return None
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        sample_keys = set()
        for item in obj[:3]:
            sample_keys.update(str(k).lower() for k in item.keys())
        if any(s in k for k in sample_keys for s in _UNIT_KEY_SIGNALS):
            return obj
    if isinstance(obj, dict):
        for v in obj.values():
            result = _find_units_array(v, depth + 1)
            if result is not None:
                return result
    return None


def _trim_body(body: Any) -> Any:
    """JSON-aware trim to stay within MAX_BODY_TOKENS. Returns a deep copy.

    Unit arrays are always capped at MAX_BODY_ARRAY_ITEMS regardless of total
    body size — a 300-item array is always trimmed to 200 before the LLM sees it.
    """
    body_copy = copy.deepcopy(body)
    truncated = False

    if isinstance(body_copy, dict):
        # Always cap unit arrays
        units_arr = _find_units_array(body_copy)
        if units_arr is not None and len(units_arr) > MAX_BODY_ARRAY_ITEMS:
            del units_arr[MAX_BODY_ARRAY_ITEMS:]
            truncated = True

        # Check token budget after unit truncation
        raw = json.dumps(body_copy)
        if len(raw) / 4 > MAX_BODY_TOKENS:
            # Drop nav/marketing keys to save space
            for k in list(body_copy.keys()):
                if k.lower() in _NAV_MARKETING_KEYS:
                    del body_copy[k]
                    truncated = True

            # If still over budget, keep only the units array path
            if len(json.dumps(body_copy)) / 4 > MAX_BODY_TOKENS:
                units_arr = _find_units_array(body_copy)
                if units_arr is not None:
                    body_copy = {"units": units_arr[:MAX_BODY_ARRAY_ITEMS]}
                    truncated = True

    if truncated:
        if isinstance(body_copy, dict):
            body_copy["_jugnu_truncated"] = True
        elif isinstance(body_copy, list):
            body_copy = {"units": body_copy, "_jugnu_truncated": True}

    return body_copy


# ── Prompt building ───────────────────────────────────────────────────────────


def _load_prompt_template() -> str:
    """Load the api_rescue.txt prompt template."""
    prompt_path = Path(__file__).resolve().parent.parent / "config" / "prompts" / "api_rescue.txt"
    if not prompt_path.exists():
        raise FileNotFoundError(f"api_rescue.txt not found at {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def _resolve_known_plans_block_for_rescue(property_id: str) -> str:
    """Render the KNOWN FLOOR PLANS block for the rescue prompt.

    Returns "" when the catalog is bypassed or the property has no plans.
    Failures are swallowed so prompt construction can never crash.
    """
    if not property_id:
        return ""
    try:
        from ma_poc.services.floorplan_catalog import get_default_catalog

        text, _meta = get_default_catalog().known_plans_block(property_id)
        return text
    except Exception as exc:  # noqa: BLE001
        log.warning("known-plans block unavailable for %s: %s", property_id, exc)
        return ""


def _build_prompt(
    template: str,
    inp: RescueInput,
    url: str,
    trimmed_body: Any,
) -> str:
    """Substitute template placeholders. Uses str.replace — not f-strings or .format()."""
    bodies_json = json.dumps([{"url": url, "body": trimmed_body}], indent=2)
    return (
        template.replace("{{property_name}}", inp.property_context.get("name", ""))
        .replace("{{website}}", inp.property_context.get("website", ""))
        .replace("{{city}}", inp.property_context.get("city", "unknown"))
        .replace("{{source_adapter}}", inp.source_adapter)
        .replace("{{expected_units}}", str(inp.property_context.get("expected_units", "unknown")))
        .replace("{{bodies_as_json}}", bodies_json)
        .replace("{{known_floor_plans}}", _resolve_known_plans_block_for_rescue(inp.property_id))
    )


# ── LLM call ─────────────────────────────────────────────────────────────────

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


async def _call_llm(prompt: str, property_id: str) -> tuple[dict, float, str]:
    """Call the configured LLM and return (parsed_json, cost_usd, raw_text).

    Retries once on JSONDecodeError with an explicit JSON-only reminder appended.
    Never raises — returns ({}, 0.0, "") on unrecoverable failure.
    """
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")

    if not endpoint or not api_key:
        # No Azure credentials → delegate to the configured provider
        # (OpenRouter / Anthropic / etc.) via the shared text provider.
        # This is the path that fires under the production env where
        # LLM_PROVIDER=openrouter and AZURE_OPENAI_* are unset.
        try:
            from llm.factory import get_text_provider
            from llm.interaction_logger import make_interaction
        except Exception as exc:
            log.warning("llm_api_rescue: text provider unavailable: %s", exc)
            return {}, 0.0, ""

        system = "You are a real estate data extraction agent. Return ONLY valid JSON. No markdown, no commentary."
        for attempt in range(2):
            try:
                provider = get_text_provider()
            except Exception as exc:
                log.warning("llm_api_rescue: get_text_provider failed for %s: %s", property_id, exc)
                return {}, 0.0, ""
            current_prompt = prompt
            if attempt == 1:
                current_prompt += "\nReturn ONLY a valid JSON object. No prose, no markdown."
            try:
                raw = await provider.complete(system, current_prompt, max_tokens=4096)
            except Exception as exc:
                log.warning("llm_api_rescue: shared LLM call failed for %s: %s", property_id, exc)
                return {}, 0.0, ""
            usage = getattr(provider, "_last_usage", {}) or {}
            try:
                interaction = make_interaction(
                    property_id=property_id,
                    tier="TIER_6_LLM_RESCUE",
                    call_type="text",
                    provider=str(usage.get("provider", "unknown")),
                    model=str(usage.get("model", "unknown")),
                    system_prompt=system,
                    user_prompt=current_prompt,
                    raw_response=raw,
                    tokens_input=int(usage.get("input_tokens", 0)),
                    tokens_output=int(usage.get("output_tokens", 0)),
                    latency_ms=0,
                    timestamp="",
                    success=True,
                    error=None,
                )
                cost = float((interaction or {}).get("cost_usd", 0.0))
            except Exception:
                cost = 0.0
            try:
                parsed = _parse_json_response(raw)
                return parsed, cost, raw
            except json.JSONDecodeError as exc:
                log.warning(
                    "llm_api_rescue: JSON decode error (attempt %d) for %s: %s",
                    attempt + 1, property_id, exc,
                )
                if attempt == 1:
                    return {}, cost, raw
        return {}, 0.0, ""

    for attempt in range(2):
        current_prompt = prompt
        if attempt == 1:
            current_prompt += "\nReturn ONLY a valid JSON object. No prose, no markdown."
        try:
            import httpx

            headers = {
                "api-key": api_key,
                "Content-Type": "application/json",
            }
            payload = {
                "messages": [{"role": "user", "content": current_prompt}],
                "temperature": 0.0,
                "max_tokens": 4096,
            }
            url = f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions?api-version=2024-02-01"
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
            raw = data["choices"][0]["message"]["content"]
            # Estimate cost: GPT-4o-mini ~$0.15/1M input + $0.60/1M output tokens
            usage = data.get("usage", {})
            in_tok = usage.get("prompt_tokens", 0)
            out_tok = usage.get("completion_tokens", 0)
            cost = (in_tok * 0.15 + out_tok * 0.60) / 1_000_000
            parsed = _parse_json_response(raw)
            return parsed, cost, raw
        except json.JSONDecodeError as exc:
            log.warning(
                "llm_api_rescue: JSON decode error (attempt %d) for %s: %s", attempt + 1, property_id, exc
            )
            if attempt == 1:
                return {}, 0.0, ""
        except Exception as exc:
            log.warning("llm_api_rescue: LLM call failed for %s: %s", property_id, exc)
            return {}, 0.0, ""

    return {}, 0.0, ""


def _parse_json_response(raw: str) -> dict:
    """Parse LLM response, handling optional markdown fences."""
    # Try direct parse first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Strip markdown fences
    m = _JSON_FENCE_RE.search(raw)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    raise json.JSONDecodeError("Cannot parse LLM response as JSON", raw, 0)


# ── Unit normalization ────────────────────────────────────────────────────────


def _normalize_units(raw_units: list[dict]) -> list[dict]:
    """Normalize LLM-extracted units to v2 unit dict shape.

    Applies the same field coercion as the jugnu_runner v2 transform.
    Never raises — malformed values become None.
    """
    out: list[dict] = []
    for u in raw_units:
        if not isinstance(u, dict):
            continue
        normed: dict[str, Any] = {
            "unit_id": _str_or_none(u.get("unit_id")),
            "floor_plan_name": _str_or_none(u.get("floor_plan_name")),
            "beds": _int_or_none(u.get("beds")),
            "baths": _float_or_none(u.get("baths")),
            "area": _int_or_none(u.get("area")),
            "rent_low": _float_or_none(u.get("rent_low")),
            "rent_high": _float_or_none(u.get("rent_high")),
            "available_date": _date_str_or_none(u.get("available_date")),
            "lease_term": _int_or_none(u.get("lease_term")),
            "move_in_date": _date_str_or_none(u.get("move_in_date")),
        }
        out.append(normed)
    return out


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _int_or_none(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _float_or_none(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _date_str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() == "null":
        return None
    # Validate ISO-8601 date format
    try:
        from datetime import date

        date.fromisoformat(s[:10])
        return s[:10]
    except ValueError:
        return None


# ── URL pattern helpers ───────────────────────────────────────────────────────


def _url_to_pattern(url: str) -> str:
    """Strip query string and replace numeric path segments with '*'."""
    try:
        parsed = urlparse(url)
        path = _NUMERIC_SEGMENT_RE.sub("/*/", parsed.path)
        return f"{parsed.scheme}://{parsed.netloc}{path}"
    except Exception:
        return url


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _tier_label_for(adapter: str) -> str:
    return {
        "generic": "TIER_1_API_LLM_RESCUE",
        "entrata": "TIER_1_API_ENTRATA_LLM_RESCUE",
        "appfolio": "TIER_1_API_APPFOLIO_LLM_RESCUE",
    }[adapter]


# ── Main entry point ──────────────────────────────────────────────────────────


async def rescue_from_api_responses(inp: RescueInput) -> RescueOutput:
    """Attempt LLM-based unit extraction from captured API responses.

    Filters and ranks candidates, sends each to the LLM one at a time (never
    batched), normalizes output, and returns the first successful extraction
    with its replay hints. All exceptions are caught — callers receive a
    graceful empty RescueOutput on failure.
    """
    out = RescueOutput()

    if inp.source_adapter not in SUPPORTED_ADAPTERS:
        out.errors.append(f"unsupported adapter: {inp.source_adapter}")
        return out

    if not inp.api_responses:
        out.errors.append("no api_responses to rescue from")
        return out

    try:
        candidates = _filter_candidates(inp.api_responses, inp.profile_snapshot)
        ranked = _rank_candidates(candidates)[:MAX_CANDIDATES]
    except Exception as exc:
        out.errors.append(f"filter/rank failed: {exc}")
        log.exception("rescue filter/rank for %s", inp.property_id)
        return out

    if not ranked:
        out.errors.append("no candidates after filtering")
        return out

    try:
        prompt_template = _load_prompt_template()
    except Exception as exc:
        out.errors.append(f"prompt template load failed: {exc}")
        return out

    from ma_poc.validation.schema_gate import property_passes_quality_gate

    for i, cand in enumerate(ranked):
        if out.n_llm_calls >= MAX_LLM_CALLS_PER_PROPERTY:
            break
        try:
            trimmed = _trim_body(cand["body"])
            prompt = _build_prompt(prompt_template, inp, cand["url"], trimmed)
            parsed, cost, _raw = await _call_llm(prompt, inp.property_id)
            out.n_llm_calls += 1
            out.cost_usd += cost

            llm_units = parsed.get("units") or []
            if not llm_units:
                out.blocked_endpoints.append((cand["url"], "llm_returned_no_units"))
                continue

            normalized = _normalize_units(llm_units)
            if not property_passes_quality_gate(normalized):
                out.blocked_endpoints.append((cand["url"], "llm_units_failed_quality_gate"))
                continue

            # Success
            out.units = normalized
            out.winning_url = parsed.get("winning_url") or cand["url"]
            out.confidence = float(parsed.get("confidence") or 0.0)
            out.tier_used = _tier_label_for(inp.source_adapter)

            mapping: dict[str, Any] = {
                "api_url_pattern": _url_to_pattern(out.winning_url),
                "envelope": parsed.get("envelope") or "",
                "json_paths": parsed.get("json_paths") or {},
                "source_adapter": inp.source_adapter,
                "created_at": _now_iso(),
                "success_count": 1,
            }
            out.llm_field_mappings.append(mapping)
            return out

        except Exception as exc:
            out.errors.append(f"candidate {i} failed: {exc}")
            log.exception("rescue candidate %d for %s", i, inp.property_id)
            continue

    return out
