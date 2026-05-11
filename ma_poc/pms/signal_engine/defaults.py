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

from ma_poc.pms.signal_engine.qualifier import (
    FieldCombination,
    MediaTypeFilter,
    SourceQualifier,
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
