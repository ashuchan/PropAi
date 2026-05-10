"""local_canary_attribution.py — heuristic fix attribution for local canary reports.

Maps per-property event traces to the fix that most likely caused an IMPROVED
outcome.  The registry is the single source of truth: adding a new fix class
requires one new ``AttributionRule`` entry.

Rules are evaluated in order; the first matching rule wins.  When more than one
rule fires (i.e. the rule has ``exclusive=False``), all matching names are joined
with " + (heuristic)" to signal ambiguity.
"""

from __future__ import annotations

from typing import NamedTuple


class AttributionRule(NamedTuple):
    """One attribution entry in the registry.

    Attributes:
        name: Human-readable fix name surfaced in the report.
        required_events: At least one of these event kinds must have fired
            during the canary run for this rule to match.
        require_tier_improvement: If True the canary tier must be strictly
            *lower* (numerically) than the cloud tier for the rule to fire.
            "Lower tier" means the property succeeded at an earlier, cheaper
            extraction step than it did in the cloud run.
        exclusive: If True no further rules are evaluated once this one fires.
    """

    name: str
    required_events: frozenset[str]
    require_tier_improvement: bool = False
    exclusive: bool = True


# ── Attribution registry ──────────────────────────────────────────────────────
#
# Evaluation order matters: more-specific rules first.
ATTRIBUTION_REGISTRY: list[AttributionRule] = [
    # A saved LLM field-mapping replayed deterministically →
    # the URL-pattern normalisation fix let the mapping match again.
    AttributionRule(
        name="url_pattern_normalization",
        required_events=frozenset({"profile.replay_hit"}),
        require_tier_improvement=False,
        exclusive=True,
    ),
    # A degraded DOM hint was persisted and the property succeeded via DOM tier →
    # the degraded-DOM-hint-persistence fix contributed.
    AttributionRule(
        name="degraded_dom_hint_persistence",
        required_events=frozenset({"dom_hints.degraded_saved"}),
        require_tier_improvement=False,
        exclusive=True,
    ),
    # A field-patch was applied (FieldPatcher found a saved correction) →
    # field_patch_persistence fix.
    AttributionRule(
        name="field_patch_persistence",
        required_events=frozenset({"field_patch.hit"}),
        require_tier_improvement=False,
        exclusive=True,
    ),
    # Property succeeded at a lower (cheaper) tier in the canary than it used in
    # the cloud run → source_tiered_budget saved an LLM call and got an answer.
    AttributionRule(
        name="source_tiered_budget",
        required_events=frozenset(),   # tier improvement alone is sufficient
        require_tier_improvement=True,
        exclusive=True,
    ),
]

# ── Tier order ────────────────────────────────────────────────────────────────
# Lower index == earlier (cheaper) tier.  Used to detect tier improvement.
_TIER_ORDER: list[str] = [
    "TIER_1_PROFILE_MAPPING",
    "TIER_2_API_NARROW",
    "TIER_2_API_BROAD",
    "TIER_3_JSON_LD",
    "TIER_3_EMBEDDED_JSON",
    "TIER_3_DOM_SCAN",
    "TIER_4_LLM_API",
    "TIER_4_LLM_DOM",
    "TIER_4_LLM_FALLBACK",
    "TIER_5_VISION",
]

_TIER_RANK: dict[str, int] = {t: i for i, t in enumerate(_TIER_ORDER)}


def _tier_improved(cloud_tier: str, canary_tier: str) -> bool:
    """Return True if canary used an earlier (cheaper) tier than the cloud run."""
    if not cloud_tier or not canary_tier:
        return False
    cloud_rank = _TIER_RANK.get(cloud_tier, 999)
    canary_rank = _TIER_RANK.get(canary_tier, 999)
    return canary_rank < cloud_rank


def attribute_fix(
    event_kinds: set[str],
    cloud_tier: str,
    canary_tier: str,
) -> str:
    """Return a human-readable attribution string for an IMPROVED property.

    Args:
        event_kinds: All event ``kind`` strings that fired during the canary
            run for this property.
        cloud_tier: ``terminal_tier`` from the cloud run's failures.csv row.
        canary_tier: ``tier_used`` extracted from the canary's
            ``extract.tier_won`` event.

    Returns:
        Attribution string, e.g. ``"url_pattern_normalization"`` or
        ``"url_pattern_normalization + field_patch_persistence (heuristic)"``
        or ``""`` when no rule matches.
    """
    tier_improved = _tier_improved(cloud_tier, canary_tier)
    matched: list[str] = []

    for rule in ATTRIBUTION_REGISTRY:
        # Check required events: at least one must be present (or the set is empty).
        events_match = (
            not rule.required_events
            or bool(event_kinds & rule.required_events)
        )
        tier_match = (not rule.require_tier_improvement) or tier_improved

        if events_match and tier_match:
            matched.append(rule.name)
            if rule.exclusive:
                break

    if not matched:
        return ""
    if len(matched) == 1:
        return matched[0]
    # Multiple non-exclusive rules fired — flag ambiguity.
    return " + ".join(matched) + " (heuristic)"
