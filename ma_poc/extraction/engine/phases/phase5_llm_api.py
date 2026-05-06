"""Phase 5 — LLM-Assisted API Analysis.

Analyzes promising but unparsed API responses one at a time (max 3 per property).
Records noise classifications for profile learning.

Extracted from scripts/entrata.py scrape() Phase 5 block.
"""
from __future__ import annotations

from extraction.engine.phase_context import PhaseContext


class Phase5LlmApiAnalysis:
    """Analyze LLM candidates from Phase 4 one at a time."""

    async def run(self, ctx: PhaseContext) -> None:
        if ctx.units:
            return

        _skip_llm = (
            not ctx.run_full_cascade
            and ctx.skip_to_tier is not None
            and ctx.skip_to_tier <= 3
        )
        if _skip_llm:
            print(
                f"  📋 Profile: HOT with preferred_tier={ctx.skip_to_tier}, "
                f"skipping LLM/Vision (no cascade)"
            )
            return

        if not ctx.llm_candidates or ctx.page_unreachable:
            return

        print(f"\n{'=' * 65}")
        print(f"  PHASE 5: LLM API Analysis — {len(ctx.llm_candidates)} candidates (max 3)")
        print(f"{'=' * 65}")

        _property_ctx = {
            "property_name": ctx.property_name,
            "website": ctx.base_url,
        }

        try:
            from services.llm_extractor import analyze_api_with_llm as _analyze_api

            for i, candidate in enumerate(ctx.llm_candidates[:3]):
                api_url = candidate.get("url", "unknown")
                print(f"\n  LLM [{i + 1}/3]: Analyzing {api_url[:100]}")

                llm_units, mapping_dict, is_noise, interaction = await _analyze_api(
                    candidate,
                    _property_ctx,
                    property_id=ctx.property_id,
                )
                if interaction is not None:
                    ctx.llm_interactions.append(interaction)

                if is_noise:
                    print("    → Noise (will blocklist)")
                    ctx.llm_analysis_results[api_url] = f"noise: {api_url}"
                    continue

                if llm_units:
                    ctx.units = llm_units
                    ctx.extraction_tier_used = "TIER_4_LLM"
                    if mapping_dict:
                        ctx.llm_analysis_results[api_url] = mapping_dict
                    print(f"  ✅ PHASE 5 (LLM API Analysis): {len(ctx.units)} units from {api_url[:80]}")
                    break
                else:
                    print("    → No units extracted")
                    ctx.llm_analysis_results[api_url] = "noise: llm_returned_empty"

        except Exception as e:
            print(f"  ⚠ Phase 5 (LLM API Analysis) error: {e}")
