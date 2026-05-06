"""Phase 2 — Noise Filtering.

Applies the profile-specific blocklist on top of the global filters
already applied at capture time by NetworkCapture (looks_like_availability_api).

Writes ctx.filtered_responses — a filtered copy of ctx.api_responses.
"""
from __future__ import annotations

from extraction.engine.noise_filter import filter_network_noise
from extraction.engine.phase_context import PhaseContext


class Phase2NoiseFilter:
    """Filter ctx.api_responses through global + profile blocklists."""

    def run(self, ctx: PhaseContext) -> None:
        ctx.filtered_responses = list(
            filter_network_noise(ctx.api_responses, ctx.profile)
        )
