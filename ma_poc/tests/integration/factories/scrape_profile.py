"""Factory helpers for ScrapeProfile test objects.

ScrapeProfile is a Pydantic BaseModel with many nested sub-models.
The helpers here let tests express only the fields relevant to the
scenario and get valid, fully-populated objects for free.
"""

from __future__ import annotations

from typing import Any

from ma_poc.models.scrape_profile import (
    ApiHints,
    BlockedEndpoint,
    DomHints,
    ExtractionConfidence,
    FieldPatch,
    LlmFieldMapping,
    ProfileMaturity,
    ScrapeProfile,
)


def build_profile(
    canonical_id: str = "test-prop-001",
    *,
    maturity: ProfileMaturity = ProfileMaturity.COLD,
    preferred_tier: int | None = None,
    consecutive_successes: int = 0,
    consecutive_failures: int = 0,
    last_unit_count: int = 0,
    llm_field_mappings: list[LlmFieldMapping] | None = None,
    field_patches: list[FieldPatch] | None = None,
    blocked_endpoints: list[BlockedEndpoint] | None = None,
    cluster_key: str = "",
) -> ScrapeProfile:
    """Return a ScrapeProfile with the given maturity and hints.

    Examples::

        # Cold profile with no learned hints
        p = build_profile()

        # Hot profile that prefers tier 1
        p = build_profile(
            maturity=ProfileMaturity.HOT,
            preferred_tier=1,
            consecutive_successes=3,
        )

        # Profile with a saved LLM field mapping for replay
        p = build_profile(
            maturity=ProfileMaturity.WARM,
            llm_field_mappings=[
                LlmFieldMapping(
                    api_url_pattern="/api/units",
                    json_paths={"unit_id": "id", "rent_low": "minRent"},
                    response_envelope="data.units",
                )
            ],
        )
    """
    confidence = ExtractionConfidence(
        maturity=maturity,
        preferred_tier=preferred_tier,
        consecutive_successes=consecutive_successes,
        consecutive_failures=consecutive_failures,
        last_unit_count=last_unit_count,
    )
    api_hints = ApiHints(
        llm_field_mappings=llm_field_mappings or [],
        field_patches=field_patches or [],
        blocked_endpoints=blocked_endpoints or [],
    )
    return ScrapeProfile(
        canonical_id=canonical_id,
        confidence=confidence,
        api_hints=api_hints,
        cluster_key=cluster_key,
    )


class ScrapeProfileFactory:
    """Stateful factory that generates unique canonical_ids per test run."""

    def __init__(self, prefix: str = "test-prop") -> None:
        self._prefix = prefix
        self._counter = 0

    def build(self, **kwargs: Any) -> ScrapeProfile:
        """Build a profile with an auto-incremented canonical_id."""
        self._counter += 1
        canonical_id = kwargs.pop("canonical_id", f"{self._prefix}-{self._counter:03d}")
        return build_profile(canonical_id=canonical_id, **kwargs)

    def reset(self) -> None:
        self._counter = 0
