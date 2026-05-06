"""PhaseRegistry — ordered collection of extraction phases.

Each phase has a run(ctx) or async run(ctx) method. The registry provides a
default ordering that matches the 7-phase entrata.py pipeline and allows
callers to swap phases for testing or specialisation.
"""
from __future__ import annotations

from typing import Any


class PhaseRegistry:
    """Ordered list of phase objects."""

    def __init__(self, phases: list[Any]) -> None:
        self.phases = phases

    @classmethod
    def default(cls) -> "PhaseRegistry":
        """Return the default full 7-phase registry."""
        from extraction.engine.phases.phase1_homepage_load import Phase1HomepageLoad
        from extraction.engine.phases.phase2_noise_filter import Phase2NoiseFilter
        from extraction.engine.phases.phase3_known_patterns import Phase3KnownPatternExtraction
        from extraction.engine.phases.phase4_link_exploration import Phase4LinkExploration
        from extraction.engine.phases.phase5_llm_api import Phase5LlmApiAnalysis
        from extraction.engine.phases.phase6_dom_fallback import Phase6DomFallback
        from extraction.engine.phases.phase7_finalize import Phase7Finalize

        return cls([
            Phase1HomepageLoad(),
            Phase2NoiseFilter(),
            Phase3KnownPatternExtraction(),
            Phase4LinkExploration(),
            Phase5LlmApiAnalysis(),
            Phase6DomFallback(),
            Phase7Finalize(),
        ])
