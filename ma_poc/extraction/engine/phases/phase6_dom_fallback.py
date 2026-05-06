"""Phase 6 — DOM Fallback with Targeted LLM → Legacy LLM → Vision.

Three fallback stages when all API-based approaches fail:
  6a: Find DOM sections with rent signals → analyze_dom_with_llm
  6b: Full page HTML + APIs → extract_with_llm (legacy fallback)
  6c: Screenshot → extract_with_vision (absolute last resort)

Extracted from scripts/entrata.py scrape() Phase 6 block.
"""
from __future__ import annotations

import sys
from pathlib import Path

from extraction.engine.phase_context import PhaseContext

_SCRIPTS_DIR = Path(__file__).resolve().parents[4] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from entrata import _extract_dom_sections_with_rent_signals  # type: ignore[import]


class Phase6DomFallback:
    """DOM + LLM + Vision fallback chain."""

    async def run(self, ctx: PhaseContext) -> None:
        if ctx.units or ctx.page_unreachable:
            return

        _skip_llm = (
            not ctx.run_full_cascade
            and ctx.skip_to_tier is not None
            and ctx.skip_to_tier <= 3
        )
        if _skip_llm:
            return

        print(f"\n{'=' * 65}")
        print("  PHASE 6: DOM Fallback + LLM")
        print(f"{'=' * 65}")

        _property_ctx = {
            "property_name": ctx.property_name,
            "website": ctx.base_url,
        }

        # 6a — Targeted DOM LLM
        dom_sections = await _extract_dom_sections_with_rent_signals(ctx.page)
        if dom_sections:
            print(f"  Found {len(dom_sections)} DOM sections with rent signals")
            try:
                from services.llm_extractor import analyze_dom_with_llm as _analyze_dom

                current_url = ctx.page.url
                dom_units, css_selectors, dom_interaction = await _analyze_dom(
                    dom_sections[0],
                    current_url,
                    _property_ctx,
                    property_id=ctx.property_id,
                )
                if dom_interaction is not None:
                    ctx.llm_interactions.append(dom_interaction)

                if dom_units:
                    ctx.units = dom_units
                    ctx.extraction_tier_used = "TIER_3_DOM_LLM"
                    print(f"  ✅ PHASE 6a (DOM LLM): {len(ctx.units)} units")
                    return

            except Exception as e:
                print(f"  ⚠ Phase 6a (DOM LLM) error: {e}")
        else:
            print("  ↳ No DOM sections with rent signals found")

        # 6b — Legacy LLM (full page + APIs)
        if not ctx.units:
            try:
                from services.llm_extractor import extract_with_llm, prepare_llm_input

                llm_input = prepare_llm_input(
                    page_html=await ctx.page.content(),
                    api_responses=ctx.api_responses,
                    property_context=_property_ctx,
                )
                llm_units, llm_hints, _, llm_interaction = await extract_with_llm(
                    llm_input,
                    property_id=ctx.property_id,
                )
                if llm_interaction is not None:
                    ctx.llm_interactions.append(llm_interaction)

                if llm_units:
                    ctx.units = llm_units
                    ctx.extraction_tier_used = "TIER_4_LLM"
                    print(f"  ✅ PHASE 6b (Legacy LLM): {len(ctx.units)} units/floor plans")
                    return

            except Exception as e:
                print(f"  ⚠ Phase 6b (Legacy LLM) error: {e}")

        # 6c — Vision LLM (absolute last resort)
        if not ctx.units:
            try:
                from services.vision_extractor import extract_with_vision

                screenshot = await ctx.page.screenshot(full_page=True)
                vision_units, vision_hints, _, vision_interaction = await extract_with_vision(
                    screenshot=screenshot,
                    property_context=_property_ctx,
                    property_id=ctx.property_id,
                )
                if vision_interaction is not None:
                    ctx.llm_interactions.append(vision_interaction)

                if vision_units:
                    ctx.units = vision_units
                    ctx.extraction_tier_used = "TIER_5_VISION"
                    print(f"  ✅ PHASE 6c (Vision): {len(ctx.units)} units")
                    return

            except Exception as e:
                print(f"  ⚠ Phase 6c (Vision) error: {e}")

        if not ctx.units:
            print("  ⚠ ALL PHASES (3-6) FAILED — no units extracted.")
            ctx.extraction_tier_used = "FAILED"
