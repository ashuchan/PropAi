"""Shared contracts for L3 — Extraction layer.

ExtractResult is the output contract passed from L3 to L4.
ProfileHints carries what the extractor learned back to the profile writer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class ProfileHints:
    """What the extractor learned. Consumed by the profile_updater."""

    api_endpoints_with_data: list[tuple[str, str]] = field(default_factory=list)
    api_endpoints_blocked: list[tuple[str, str]] = field(default_factory=list)
    llm_field_mappings: list[dict[str, Any]] = field(default_factory=list)
    css_selectors: dict[str, str] = field(default_factory=dict)
    platform_detected: str | None = None
    winning_page_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict."""
        return {
            "api_endpoints_with_data": self.api_endpoints_with_data,
            "api_endpoints_blocked": self.api_endpoints_blocked,
            "llm_field_mappings": self.llm_field_mappings,
            "css_selectors": self.css_selectors,
            "platform_detected": self.platform_detected,
            "winning_page_path": self.winning_page_path,
        }


@dataclass(slots=True, frozen=True)
class ExtractResult:
    """Immutable output of L3 extraction, consumed by L4 validation."""

    property_id: str
    records: list[dict[str, Any]]  # Unit-shaped dicts — not yet validated
    tier_used: str  # e.g. "ADAPTER_ENTRATA", "GENERIC_TIER_3_DOM"
    adapter_name: str  # e.g. "entrata", "generic"
    winning_url: str | None  # URL/endpoint that produced data
    confidence: float  # 0-1, adapter's own confidence
    # Cost accounting (L5 consumes this)
    llm_cost_usd: float = 0.0
    vision_cost_usd: float = 0.0
    llm_calls: int = 0
    vision_calls: int = 0
    # Profile-learning payload
    profile_hints: ProfileHints | None = None
    # Non-fatal errors collected during extraction
    errors: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        """True if no records were extracted."""
        return len(self.records) == 0

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for event emission."""
        return {
            "property_id": self.property_id,
            "records_count": len(self.records),
            "tier_used": self.tier_used,
            "adapter_name": self.adapter_name,
            "winning_url": self.winning_url,
            "confidence": self.confidence,
            "llm_cost_usd": self.llm_cost_usd,
            "vision_cost_usd": self.vision_cost_usd,
            "llm_calls": self.llm_calls,
            "vision_calls": self.vision_calls,
            "errors": self.errors,
        }
