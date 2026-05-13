"""SourceSignal — the lingua franca for the unified signal engine.

Every signal the scraper touches (API body, DOM link, JSON blob, LLM hint,
profile URL) becomes a SourceSignal before evaluation.

Design constraints:
- frozen=True: signals are facts, never mutated mid-extraction.
- field_keys normalised to lowercase in __post_init__ — eliminates the
  PascalCase mismatch that caused RC2 (RentCafe unit key miss).
- blocked_at / noise_verdicts read from ScrapeProfile.api_hints.blocked_endpoints
  at signal collection time; they are never fetched lazily.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class SourceKind(StrEnum):
    """Classification of where a signal was discovered."""

    API_RESPONSE     = "api_response"      # XHR/fetch captured during render
    EMBEDDED_JSON    = "embedded_json"     # <script type="application/json">
    JSON_LD          = "json_ld"           # JSON-LD structured data
    DOM_SECTION      = "dom_section"       # Rendered DOM section
    INTERNAL_LINK    = "internal_link"     # Same-domain navigable URL
    EXTERNAL_PORTAL  = "external_portal"   # Cross-domain leasing portal
    PMS_PRIOR        = "pms_prior"         # PMS-specific sub-path prior
    UNIVERSAL_PRIOR  = "universal_prior"   # Generic fallback sub-path
    LLM_HINT         = "llm_hint"          # navigation_hint from LLM analysis
    PROFILE_WINNING  = "profile_winning"   # profile.navigation.winning_page_url
    PROFILE_NAV_HINT = "profile_nav_hint"  # profile.navigation.last_navigation_hints


@dataclass(frozen=True)
class SourceSignal:
    """Immutable descriptor for a single candidate signal.

    Constructed once per signal at collection time; never mutated.
    All field_keys are normalised to lowercase in __post_init__.
    """

    kind: SourceKind
    url: str | None = None
    content_type: str | None = None    # "application/json", "text/javascript"
    url_suffix: str | None = None      # ".js", ".css" — derived from URL path
    body_size_bytes: int = 0
    field_keys: frozenset[str] = field(default_factory=frozenset)
    anchor_text: str | None = None
    platform_tag: str | None = None    # "rentcafe", "sightmap", etc.
    provenance: str = ""
    # Profile-state at collection time (populated from ScrapeProfile):
    blocked_at: datetime | None = None        # from BlockedEndpoint.blocked_at
    noise_verdicts: int = 0                   # from BlockedEndpoint.attempts
    is_known_endpoint: bool = False
    profile_score_override: int | None = None  # 10001 for profile:winning_page_url

    def __post_init__(self) -> None:
        # Normalise all field keys to lowercase unconditionally (M7).
        # This eliminates the PascalCase key mismatch that caused RC2:
        # "RentCafeApartmentId" vs "rentcafeapartmentid".
        # Note: full alias normalisation (squareFeet→sqft etc.) is NOT applied
        # here — it would change native PMS field names and break PMS-specific
        # fingerprint checks (e.g. RentCafe's "floorplanname" key). Alias
        # normalisation is applied only in has_unit_signals() (E, _merge_fns.py).
        object.__setattr__(
            self,
            "field_keys",
            frozenset(k.lower() for k in self.field_keys),
        )
        # Derive url_suffix from URL when not explicitly provided.
        if self.url_suffix is None and self.url is not None:
            try:
                from pathlib import PurePosixPath
                raw_path = self.url.split("?")[0]
                suffix = PurePosixPath(raw_path).suffix.lower()
                object.__setattr__(self, "url_suffix", suffix or None)
            except Exception:
                pass
