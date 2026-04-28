"""LLM-driven ranking and re-ranking of site-universe candidates.

Two callers:
- :func:`rank_universe` is the explorer's first move — one LLM call gets
  confidence scores for every candidate (API + link) so the priority
  queue knows what to try first.
- :func:`rerank_for_availability` runs after a partial extraction succeeded
  but is missing rent or availability dates. It re-scores remaining
  candidates with that bias and returns a fresh order.

Both functions are async, never raise (any error returns the input order
unchanged plus an interaction record), and emit cost-tracking interaction
records compatible with :mod:`llm.interaction_logger`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ma_poc.services.site_universe import SiteUniverse, candidate_summary_for_ranking

log = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Per-chunk candidate count. The slim summary (URL, anchor, kind, signals)
# is small per item — 40 candidates fits comfortably under provider limits
# and lets a typical property's universe rank in a single call. The
# previous 25/80 split forced 2-4 chunks per property.
RANK_CHUNK_SIZE = _env_int("JUGNU_RANK_CHUNK_SIZE", 40)
# Hard cap on total candidates ranked per property (across all chunks).
RANK_TOTAL_CAP = _env_int("JUGNU_RANK_TOTAL_CAP", 40)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "config" / "prompts"
_RANKING_PROMPT_PATH = _PROMPT_DIR / "universe_ranking.txt"
_RERANK_PROMPT_PATH = _PROMPT_DIR / "availability_rerank.txt"

_SYSTEM_PROMPT = (
    "You are a real estate data extraction agent ranking exploration "
    "candidates for a multifamily property website. Return ONLY valid JSON. "
    "No markdown, no commentary."
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def rank_universe(
    universe: SiteUniverse,
    property_context: dict[str, Any],
    property_id: str = "unknown",
    max_candidates: int = RANK_TOTAL_CAP,
    chunk_size: int = RANK_CHUNK_SIZE,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Score every candidate in the universe via chunked LLM calls.

    Splits the candidate list into chunks of ``chunk_size`` items, sends
    one LLM call per chunk, then merges the rankings. Each call is much
    cheaper than the old monolithic one — typical chunk prompt is
    ~1-2K tokens vs. ~28K previously — and the model can comfortably
    score every item in the chunk without hitting an output token cap.

    Returns
    -------
    (ranked, interaction)
        ``ranked`` is the merged ``[{url, kind, confidence, reason}, ...]``
        across all chunks. ``interaction`` is the FIRST chunk's cost-tracking
        record (kept for backwards compatibility with callers that expected
        a single interaction record); subsequent chunks' interactions are
        attached to the explorer's outcome via the loop.
    """
    template = _load_template(_RANKING_PROMPT_PATH)
    if not template:
        return [], None

    payload = _serialize_for_prompt(universe, max_candidates=max_candidates)
    if not payload:
        return [], None

    # Slice into chunks. Single-chunk runs work the same as before.
    chunks = [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)]
    chunk_total = len(chunks)

    merged: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    first_interaction: dict[str, Any] | None = None
    extra_interactions: list[dict[str, Any]] = []

    for idx, chunk in enumerate(chunks, start=1):
        prompt = _format_prompt(
            template,
            property_context,
            candidates_json=json.dumps(chunk, indent=2),
            chunk_index=str(idx),
            chunk_total=str(chunk_total),
        )
        parsed, interaction = await _llm_call(
            prompt, property_id, tier=f"UNIVERSE_RANKING_CHUNK_{idx}_OF_{chunk_total}"
        )
        if first_interaction is None:
            first_interaction = interaction
        elif interaction is not None:
            extra_interactions.append(interaction)

        for entry in _extract_ranked_array(parsed):
            url = entry.get("url") or ""
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            merged.append(entry)

    # Attach extra-chunk interactions onto the first interaction's record
    # so the explorer's debug/cost ledger picks them all up via the
    # outcome.llm_interactions list. The explorer reads .extra below.
    if first_interaction is not None and extra_interactions:
        first_interaction["_extra_chunk_interactions"] = extra_interactions

    return merged, first_interaction


async def rerank_for_availability(
    universe: SiteUniverse,
    units_so_far: list[dict[str, Any]],
    property_context: dict[str, Any],
    source_url: str,
    property_id: str = "unknown",
    max_candidates: int = 30,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Re-rank the remaining (un-explored) candidates with an availability bias.

    Only candidates whose ``explored`` flag is False are sent to the LLM —
    we don't want it telling us to retry something we already tried. The
    counts of units, units-with-rent, and units-with-date are passed in so
    the model knows exactly what we're missing.
    """
    template = _load_template(_RERANK_PROMPT_PATH)
    if not template:
        return [], None

    remaining = [c for c in universe.all_candidates() if not c.explored]
    if not remaining:
        return [], None

    summary = candidate_summary_for_ranking(
        _filter_universe_to(universe, remaining)
    )[:max_candidates]

    units_with_rent = sum(1 for u in units_so_far if _has_rent(u))
    units_with_date = sum(1 for u in units_so_far if _has_date(u))

    prompt = _format_prompt(
        template,
        property_context,
        candidates_json=json.dumps(summary, indent=2),
        units_extracted=str(len(units_so_far)),
        units_with_rent=str(units_with_rent),
        units_with_date=str(units_with_date),
        source_url=source_url,
    )

    parsed, interaction = await _llm_call(prompt, property_id, tier="UNIVERSE_RERANK_AVAILABILITY")
    return _extract_ranked_array(parsed), interaction


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def _load_template(path: Path) -> str:
    """Read a prompt file. Returns "" when the file is missing — the caller
    treats that as a non-fatal degradation (no LLM call, no ranking)."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("Ranking prompt not found at %s", path)
        return ""
    except Exception as exc:
        log.warning("Failed to read ranking prompt %s: %s", path, exc)
        return ""


def _format_prompt(template: str, property_context: dict[str, Any], **extras: str) -> str:
    """Substitute property context + extras into the template.

    Uses string ``.replace`` rather than ``str.format`` because the
    templates contain literal JSON braces that ``str.format`` would try
    to interpret as fields.
    """
    out = template.replace("{property_name}", str(property_context.get("property_name") or "Unknown"))
    out = out.replace("{city}", str(property_context.get("city") or ""))
    out = out.replace("{state}", str(property_context.get("state") or ""))
    out = out.replace("{pmc}", str(property_context.get("pmc") or ""))
    out = out.replace("{website}", str(property_context.get("website") or ""))
    for k, v in extras.items():
        out = out.replace("{" + k + "}", v)
    return out


def _serialize_for_prompt(universe: SiteUniverse, max_candidates: int) -> list[dict[str, Any]]:
    """Build the JSON list the ranker prompt embeds.

    Order: APIs first (most important — APIs with unit signals first within
    that bucket), then internal links by their pre-computed base_score on
    the LinkCandidate, then external links. We re-look-up base_score from
    the universe (not the slim summary, which intentionally drops it).
    Truncated to ``max_candidates`` so prompt size stays bounded.
    """
    base_by_url: dict[str, int] = {c.url: c.base_score for c in universe.all_candidates()}
    has_signal_by_url: dict[str, bool] = {c.url: c.has_unit_signals for c in universe.api_candidates}

    summary = candidate_summary_for_ranking(universe)
    apis = sorted(
        (s for s in summary if s["kind"] == "api"),
        key=lambda s: (not has_signal_by_url.get(s["url"], False), -base_by_url.get(s["url"], 0)),
    )
    internal = sorted(
        (s for s in summary if s["kind"] == "internal_link"),
        key=lambda s: -base_by_url.get(s["url"], 0),
    )
    external = sorted(
        (s for s in summary if s["kind"] == "external_link"),
        key=lambda s: -base_by_url.get(s["url"], 0),
    )
    return (apis + internal + external)[:max_candidates]


def _filter_universe_to(universe: SiteUniverse, candidates: list[Any]) -> SiteUniverse:
    """Return a shallow universe view containing only the given candidates.

    Used by the rerank path to feed the prompt only un-explored items
    without rebuilding the gather logic.
    """
    keep_urls = {c.url for c in candidates}
    return SiteUniverse(
        source_page_url=universe.source_page_url,
        api_candidates=[c for c in universe.api_candidates if c.url in keep_urls],
        internal_links=[c for c in universe.internal_links if c.url in keep_urls],
        external_links=[c for c in universe.external_links if c.url in keep_urls],
        blocked_api_count=universe.blocked_api_count,
        skipped_link_count=universe.skipped_link_count,
    )


# ---------------------------------------------------------------------------
# LLM dispatch — mirrors the pattern in llm_extractor for consistency
# ---------------------------------------------------------------------------


async def _llm_call(
    prompt: str,
    property_id: str,
    tier: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run the prompt against the configured text provider.

    Returns ``(parsed_json, interaction_record)``. On any failure the
    parsed dict is empty and the interaction still records the failure
    so cost accounting + telemetry stay honest.
    """
    try:
        from llm.factory import get_text_provider

        provider = get_text_provider()
    except Exception as exc:
        log.error("Failed to obtain LLM provider for %s: %s", tier, exc)
        return {}, None

    t0 = time.monotonic()
    timestamp = datetime.now(UTC).isoformat()
    raw_response = ""
    error_msg: str | None = None

    try:
        raw_response = await provider.complete(_SYSTEM_PROMPT, prompt, max_tokens=4096)
    except Exception as exc:
        log.error("%s LLM call failed: %s", tier, exc)
        error_msg = str(exc)
        raw_response = f"llm_error: {exc}"

    latency_ms = int((time.monotonic() - t0) * 1000)
    usage: dict[str, Any] = getattr(provider, "_last_usage", {}) or {}
    tokens_in = int(usage.get("input_tokens", 0))
    tokens_out = int(usage.get("output_tokens", 0))
    model = str(usage.get("model", "unknown"))
    prov_name = str(usage.get("provider", "unknown"))

    interaction: dict[str, Any] | None
    try:
        from llm.interaction_logger import make_interaction

        interaction = make_interaction(
            property_id=property_id,
            tier=tier,
            call_type="text",
            provider=prov_name,
            model=model,
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=prompt[:500] + "...(truncated)",
            raw_response=raw_response,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            latency_ms=latency_ms,
            timestamp=timestamp,
            success=error_msg is None,
            error=error_msg,
        )
    except Exception as exc:
        log.warning("Failed to build %s interaction record: %s", tier, exc)
        interaction = None

    if error_msg:
        return {}, interaction

    parsed = _parse_response(raw_response)
    return parsed, interaction


def _parse_response(text: str) -> dict[str, Any]:
    """Parse the ranker's response — defers to the existing parser used
    by other LLM helpers so handling of markdown fences / leading commentary
    stays consistent across the codebase."""
    try:
        from ma_poc.services.llm_extractor import _parse_llm_response  # type: ignore[attr-defined]

        return _parse_llm_response(text)
    except Exception:
        try:
            return json.loads(text)
        except Exception:
            return {}


def _extract_ranked_array(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull ``ranked`` out of the parsed response, validating each entry.

    Accepts both ``{"ranked": [...]}`` and a top-level array (some models
    drop the wrapper despite the prompt). Drops entries missing url or
    confidence; clamps confidence into [0.0, 1.0].
    """
    raw: Any
    if isinstance(parsed, dict):
        raw = parsed.get("ranked", [])
    else:
        raw = parsed

    if not isinstance(raw, list):
        return []

    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        conf_raw = item.get("confidence")
        if not url or conf_raw is None:
            continue
        try:
            conf = float(conf_raw)
        except (TypeError, ValueError):
            continue
        conf = max(0.0, min(1.0, conf))
        out.append(
            {
                "url": url,
                "kind": str(item.get("kind") or ""),
                "confidence": conf,
                "reason": str(item.get("reason") or "")[:240],
            }
        )
    return out


_RENT_KEYS = ("rent_low", "market_rent_low", "asking_rent", "rent")
_DATE_KEYS = ("available_date", "availability_date", "move_in_date")


def _has_rent(unit: dict[str, Any]) -> bool:
    return any(_present(unit.get(k)) for k in _RENT_KEYS)


def _has_date(unit: dict[str, Any]) -> bool:
    return any(_present(unit.get(k)) for k in _DATE_KEYS)


def _present(v: Any) -> bool:
    if v is None or v == -1:
        return False
    if isinstance(v, str) and not v.strip():
        return False
    return True
