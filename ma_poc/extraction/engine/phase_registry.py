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
        """Return the default PR-2 registry: Phase1 + Phase2 wired up."""
        from extraction.engine.phases.phase1_homepage_load import Phase1HomepageLoad
        from extraction.engine.phases.phase2_noise_filter import Phase2NoiseFilter

        return cls([
            Phase1HomepageLoad(),
            Phase2NoiseFilter(),
        ])
