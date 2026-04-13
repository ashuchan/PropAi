"""
Tier orchestrator. First result with confidence >= 0.7 wins.

Acceptance criteria (CLAUDE.md PR-03 / pipeline.py — exact logic):
- Iterate Tiers 1..4 in priority order
- Return immediately on first .succeeded result (downstream tiers skipped)
- If none succeed, return the best result with status=FAILED for Vision signal
- Tier and confidence are always logged on the returned ExtractionResult
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from extraction import tier1_api, tier2_jsonld, tier3_templates, tier4_llm
from models.extraction_result import ExtractionResult, ExtractionStatus, ExtractionTier
from scraper.browser import BrowserSession

TierFn = Callable[[BrowserSession], Awaitable[ExtractionResult]]


async def _t1(s: BrowserSession, catalogue: dict[str, Any] | None) -> ExtractionResult:
    return await tier1_api.extract(s, catalogue)


async def _t2(s: BrowserSession, catalogue: dict[str, Any] | None) -> ExtractionResult:
    return await tier2_jsonld.extract(s)


async def _t3(s: BrowserSession, catalogue: dict[str, Any] | None) -> ExtractionResult:
    return await tier3_templates.extract(s)


async def _t4(s: BrowserSession, catalogue: dict[str, Any] | None) -> ExtractionResult:
    return await tier4_llm.extract(s)


async def run_extraction_pipeline(
    session: BrowserSession,
    api_catalogue: dict[str, Any] | None = None,
) -> ExtractionResult:
    """Run Tiers 1–4. First .succeeded result wins; otherwise return best as FAILED."""
    tiers: list[tuple[ExtractionTier, Callable[..., Awaitable[ExtractionResult]]]] = [
        (ExtractionTier.API_INTERCEPTION, _t1),
        (ExtractionTier.JSON_LD, _t2),
        (ExtractionTier.PLAYWRIGHT_TPL, _t3),
        (ExtractionTier.LLM_GPT4O_MINI, _t4),
    ]
    best: ExtractionResult | None = None
    for tier_enum, fn in tiers:
        result = await fn(session, api_catalogue)
        result.tier = tier_enum
        if result.succeeded:
            return result
        if best is None or result.confidence_score > best.confidence_score:
            best = result
    assert best is not None
    best.status = ExtractionStatus.FAILED
    return best
