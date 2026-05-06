"""Phase 3 — Known Pattern Extraction.

Tries extraction on homepage data using all deterministic methods:
  Tier 1   — Profile LLM field mapping replay + global API parser
  Tier 1.5 — Embedded JSON (SSR/JS globals)
  Tier 2   — JSON-LD structured data
  Tier 3   — CSS-selector DOM parsing

First successful tier wins. Skips when ctx.units already populated by a
prior phase (e.g., profile HOT skip).

Extracted from scripts/entrata.py scrape() Phase 3 block.
"""
from __future__ import annotations

import sys
from pathlib import Path

from extraction.engine.phase_context import PhaseContext

# Ensure scripts/ is importable (entrata.py helpers live there).
_SCRIPTS_DIR = Path(__file__).resolve().parents[4] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from entrata import (  # type: ignore[import]
    _is_low_signal_units,
    _response_looks_like_units,
    _units_below_expected,
    extract_embedded_json,
    parse_api_responses,
    parse_dom,
    parse_jsonld,
    try_known_patterns,
)


def _phase3_accept(
    candidate: list[dict],
    tier_name: str,
    expected_units: int | None,
) -> bool:
    """Return False when the result looks like a teaser or placeholder."""
    if _is_low_signal_units(candidate):
        print(
            f"  ✗ {tier_name}: rejected — all units have no rent "
            f"and no unit_id (continuing cascade)"
        )
        return False
    if _units_below_expected(candidate, expected_units):
        print(
            f"  ✗ {tier_name}: rejected — {len(candidate)} units "
            f"<< expected {expected_units} (continuing cascade)"
        )
        return False
    return True


class Phase3KnownPatternExtraction:
    """Try deterministic extraction tiers on homepage data."""

    async def run(self, ctx: PhaseContext) -> None:
        if ctx.units:
            return

        page = ctx.page
        filtered = ctx.filtered_responses

        # Effective expected unit count
        _expected = ctx.expected_total_units
        if not _expected and ctx.profile is not None:
            conf = getattr(ctx.profile, "confidence", None)
            _expected = getattr(conf, "last_unit_count", None) if conf else None

        print(f"\n{'=' * 65}")
        print(f"  PHASE 3: Known Pattern Extraction — {len(filtered)} API responses")
        print(f"{'=' * 65}")

        # Tier 1 — Profile LLM mapping replay + global API parser
        units = try_known_patterns(filtered, ctx.profile)
        if units and not _phase3_accept(units, "TIER_1_API", _expected):
            units = []
        if units:
            tier_label = "TIER_1_API"
            if ctx.profile is not None:
                api_hints = getattr(ctx.profile, "api_hints", None)
                if api_hints and getattr(api_hints, "llm_field_mappings", []):
                    tier_label = "TIER_1_PROFILE_MAPPING"
            print(f"  ✅ {tier_label}: {len(units)} units/floor plans")
            ctx.units = units
            ctx.extraction_tier_used = tier_label
            unit_api_srcs = [
                r["url"] for r in filtered if _response_looks_like_units(r["body"])
            ]
            ctx.winning_page_url = unit_api_srcs[0] if unit_api_srcs else ctx.base_url
            return

        # Tier 1.5 — Embedded JSON on homepage
        embedded_blobs = await extract_embedded_json(page)
        if embedded_blobs:
            ctx.api_responses.extend(embedded_blobs)
            candidate = parse_api_responses(embedded_blobs)
            if candidate and _phase3_accept(candidate, "TIER_1_5_EMBEDDED", _expected):
                print(f"  ✅ TIER 1.5 (Embedded JSON — homepage): {len(candidate)} units/floor plans")
                ctx.units = candidate
                ctx.extraction_tier_used = "TIER_1_5_EMBEDDED"
                return

        # Tier 2 — JSON-LD
        candidate = await parse_jsonld(page)
        if candidate and _phase3_accept(candidate, "TIER_2_JSONLD", _expected):
            print(f"  ✅ TIER 2 (JSON-LD): {len(candidate)} items")
            ctx.units = candidate
            ctx.extraction_tier_used = "TIER_2_JSONLD"
            return

        # Tier 3 — DOM parsing
        candidate = await parse_dom(page, ctx.base_url)
        if candidate and _phase3_accept(candidate, "TIER_3_DOM", _expected):
            print(f"  ✅ TIER 3 (DOM — homepage): {len(candidate)} units/floor plans")
            ctx.units = candidate
            ctx.extraction_tier_used = "TIER_3_DOM"
            return

        if not ctx.units:
            print("  ↳ Phase 3: no units found on homepage")
