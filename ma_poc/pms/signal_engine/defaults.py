"""Default factory functions for signal engine components.

Centralises all configuration (field combinations, media filters, scoring
tables) so there is exactly ONE place to change each constant.

The factory pattern (DI for _UNIT_SIGNAL_KEYS) avoids circular imports:
  qualifier.py has zero imports from pms/adapters/
  defaults.py imports _UNIT_SIGNAL_KEYS from _merge_fns and passes it in.

Invariants enforced here (verified by tests/pms/signal_engine/test_qualifier.py):
  - All FieldCombination keys are lowercase (frozenset literals below are lc)
  - MediaTypeFilter covers JS/CSS/font/image
  - blocked_ttl_days=14, min_noise_verdicts=2 match the spec
"""

from __future__ import annotations

from ma_poc.pms.signal_engine.models import SourceKind
from ma_poc.pms.signal_engine.qualifier import (
    FieldCombination,
    MediaTypeFilter,
    SourceQualifier,
)
from ma_poc.pms.signal_engine.ranker import ScoringTables, SourceRanker


# ── Scoring constants (single source of truth) ────────────────────────────────
# These replace the scattered constants in scraper.py.
# scraper.py imports these during Phase 2; the definitions there are removed
# in Phase 4 after regression tests pass.

LLM_HINT_SCORE: int = 10_000
EMBEDDED_PORTAL_SCORE: int = 10_000
PMS_PRIOR_SCORE: int = 5_000

DEFAULT_KIND_BASE_SCORES: dict[SourceKind, int] = {
    SourceKind.PROFILE_WINNING:  10_001,   # profile:winning_page_url sentinel
    SourceKind.LLM_HINT:         10_000,   # replaces _LLM_HINT_SCORE
    SourceKind.EXTERNAL_PORTAL:  10_000,   # replaces _EMBEDDED_PORTAL_SCORE
    SourceKind.PROFILE_NAV_HINT:  9_000,
    SourceKind.API_RESPONSE:      8_000,   # network-captured = data exists
    SourceKind.EMBEDDED_JSON:     7_500,
    SourceKind.JSON_LD:           6_000,
    SourceKind.DOM_SECTION:       5_500,
    SourceKind.PMS_PRIOR:         5_000,   # replaces _PMS_PRIOR_SCORE
    SourceKind.UNIVERSAL_PRIOR:   4_500,
    SourceKind.INTERNAL_LINK:     4_000,   # base, augmented by keyword scoring
}

DEFAULT_ANCHOR_KEYWORDS: tuple[tuple[str, int], ...] = (
    ("availability", 100),
    ("floor plan", 90),
    ("floor-plan", 90),
    ("floorplan", 85),
    ("pricing", 80),
    ("rent", 70),
    ("find your home", 88),
    ("find a home", 88),
    ("pick your home", 88),
    ("pick a home", 88),
    ("choose your home", 88),
    ("search homes", 85),
    ("search apartments", 85),
    ("view availability", 88),
    ("check availability", 88),
    ("available homes", 85),
    ("available apartments", 85),
    ("see available", 80),
    ("view floor plan", 88),
    ("view floorplan", 88),
    ("apartment", 60),
    ("unit", 55),
    ("lease", 50),
    ("tour", 40),
    ("apply", 30),
    ("schedule", 20),
)

DEFAULT_PATH_KEYWORDS: tuple[tuple[str, int], ...] = (
    ("/floor-plan", 95),
    ("/floorplan", 90),
    ("/availability", 95),
    ("/pricing", 80),
    ("/apartments", 70),
    ("/rent", 60),
    ("/units", 85),
    ("/leasing", 50),
    ("/lease", 45),
    ("/floorplans", 90),
    ("/availabilities", 95),
    ("/conventional/", 88),
    ("/conventional", 85),
)

DEFAULT_HOST_KEYWORDS: tuple[tuple[str, int], ...] = (
    (".rentcafe.com", 120),
    (".appfolio.com", 120),
    (".onlineleasing.realpage.com", 120),
    ("sightmap.com", 110),
    (".entrata.com", 115),
    ("commoncf.entrata.com", 115),
    ("prospectportal.com", 115),
)

DEFAULT_PMS_PRIORS: dict[str, tuple[str, ...]] = {
    "rentcafe": ("/floorplans", "/availability", "/apartments"),
    "entrata": ("/floorplans", "/conventional/", "/apartments/", "/availability", "/leasing"),
    "appfolio": ("/listings", "/apartments", "/floor-plans"),
    "onesite": ("/floorplans", "/availability", "/apartments"),
    "realpage_oll": ("/floorplans", "/availability"),
    "sightmap": ("/floorplans", "/availability"),
    "avalonbay": ("/floor-plans-pricing", "/apartments"),
    "amli": ("/floor-plans", "/availability"),
    "funnel": ("/floorplans", "/availability"),
}

DEFAULT_UNIVERSAL_PRIORS: tuple[str, ...] = (
    "/floorplans",
    "/floor-plans",
    "/availability",
    "/view-availability",
    "/apartments",
    "/units",
    "/leasing",
)


def create_default_qualifier(
    unit_signal_keys: frozenset[str] | None = None,
) -> SourceQualifier:
    """Build the canonical SourceQualifier.

    Args:
        unit_signal_keys: The frozenset from _merge_fns._UNIT_SIGNAL_KEYS,
            passed in by the caller to avoid circular imports. When None,
            the factory imports it directly (safe in contexts where the
            adapters package is already initialised).

    Returns:
        A fully configured SourceQualifier with all known field combinations.
    """
    if unit_signal_keys is None:
        from ma_poc.pms.adapters._merge_fns import _UNIT_SIGNAL_KEYS
        unit_signal_keys = _UNIT_SIGNAL_KEYS

    # Normalise all keys to lowercase — _UNIT_SIGNAL_KEYS contains mixed-case
    # keys (e.g. "minRent", "unitNumber") but SourceSignal.__post_init__ also
    # normalises, so comparisons must be consistent.
    lc_unit_keys = frozenset(k.lower() for k in unit_signal_keys)

    return SourceQualifier(
        combinations=[
            # ── Generic unit data (≥2 keys) ──────────────────────────────────
            # Wraps existing has_unit_signals() from _merge_fns.py.
            # Replaces the has_unit_signals() check in the api_narrow path
            # during Phase 2; preserved alongside it in Phase 1.
            FieldCombination(
                keys=lc_unit_keys,
                min_count=2,
                label="unit_generic",
            ),
            # ── RentCafe floor-plan level (≥3 of 6) ─────────────────────────
            # Replaces _is_rentcafe_response() 3-of-6 check.
            FieldCombination(
                keys=frozenset({
                    "floorplanname", "floorplanid", "minimumrent",
                    "maximumrent", "availableunitscount", "availabilityurl",
                }),
                min_count=3,
                label="rentcafe_floor_plan",
            ),
            # ── RentCafe unit level with ID keys (≥2 of 3) — RC2 ────────────
            # Unit-level RentCafe endpoints ship individual apartment records
            # keyed by RentCafe IDs rather than floor-plan aggregates.
            FieldCombination(
                keys=frozenset({
                    "rentcafeapartmentid",
                    "rentcafefloorplanid",
                    "rentcafepropertyid",
                }),
                min_count=2,
                label="rentcafe_unit",
            ),
            # ── RentCafe unit level with rent fields (≥2 of 3) — RC2 alt ────
            FieldCombination(
                keys=frozenset({
                    "rentcafeapartmentid",
                    "unitrent",
                    "marketrent",
                }),
                min_count=2,
                label="rentcafe_unit_rent",
            ),
            # ── SightMap unit (≥2 of 4) ──────────────────────────────────────
            FieldCombination(
                keys=frozenset({
                    "unit_number", "price", "area", "available_on",
                }),
                min_count=2,
                label="sightmap_unit",
            ),
            # ── Floor-plan physical dimensions (≥3 of 8) ─────────────────────
            # Plan-level signals with no rent required — useful for
            # detecting floor-plan APIs that separate rent from dimensions.
            FieldCombination(
                keys=frozenset({
                    "beds", "bedrooms", "bathrooms", "baths",
                    "sqft", "area", "floor_plan_name", "floorplanname",
                }),
                min_count=3,
                label="floor_plan_physical",
            ),
        ],
        media_filter=MediaTypeFilter(
            blocked_content_types=frozenset({
                "text/javascript",
                "text/css",
                "font/",
                "image/",
                "application/font",
                "application/x-font",
            }),
            blocked_url_suffixes=frozenset({
                ".js", ".css", ".woff", ".woff2", ".ttf", ".otf",
                ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
            }),
        ),
        blocked_ttl_days=14,
        min_noise_verdicts=2,
    )


def create_default_ranker() -> SourceRanker:
    """Build the canonical SourceRanker with all scoring tables."""
    return SourceRanker(
        tables=ScoringTables(
            kind_base_scores=DEFAULT_KIND_BASE_SCORES,
            anchor_keywords=DEFAULT_ANCHOR_KEYWORDS,
            path_keywords=DEFAULT_PATH_KEYWORDS,
            host_keywords=DEFAULT_HOST_KEYWORDS,
            pms_priors=DEFAULT_PMS_PRIORS,
            universal_priors=DEFAULT_UNIVERSAL_PRIORS,
        )
    )
