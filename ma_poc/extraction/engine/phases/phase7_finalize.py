"""Phase 7 — Finalization + Profile Learning.

Applies availability defaults to extracted units, sets final tier labels,
and records which page/URL produced the winning data.

Extracted from scripts/entrata.py scrape() Phase 7 block.
"""
from __future__ import annotations

import sys
from pathlib import Path

from extraction.engine.phase_context import PhaseContext

_SCRIPTS_DIR = Path(__file__).resolve().parents[4] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from entrata import apply_availability_defaults  # type: ignore[import]


class Phase7Finalize:
    """Apply availability defaults, record winning URL, set final tier."""

    async def run(self, ctx: PhaseContext) -> None:
        # Determine final extraction tier label
        if not ctx.units and ctx.page_unreachable:
            print("  ⛔ UNREACHABLE — skipped LLM/Vision phases to avoid wasting budget.")
            ctx.extraction_tier_used = "FAILED_UNREACHABLE"
        elif not ctx.units and not ctx.extraction_tier_used:
            ctx.extraction_tier_used = "FAILED"

        if ctx.units:
            ctx.units = apply_availability_defaults(ctx.units)

        # Record winning page URL
        if ctx.units and not ctx.winning_page_url:
            try:
                ctx.winning_page_url = ctx.page.url
            except Exception:
                ctx.winning_page_url = ctx.base_url
