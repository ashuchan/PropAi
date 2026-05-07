"""
Source planner — ranking tables, completeness gates, decision map.

Phase 3 introduced the constants the merger needs (FIELD_GROUP,
CONFIDENCE_FLOORS, DEFAULT_SOURCE_RANKING). Phase 4 adds the
Decision dataclass, plan_next_action, rank_sources_for_field_group,
and compute_budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ma_poc.models.source import SourceId

# Maps (field_group) -> ordered list of (SourceId, base_confidence)
DEFAULT_SOURCE_RANKING: dict[str, list[tuple[SourceId, float]]] = {
    "identity": [
        (SourceId.API_RENTCAFE_UNITS, 0.95),
        (SourceId.API_SIGHTMAP, 0.95),
        (SourceId.API_ONESITE, 0.90),
        (SourceId.API_APPFOLIO_LISTINGS, 0.90),
        (SourceId.API_ENTRATA_WIDGET, 0.85),
        (SourceId.API_GENERIC_NARROW, 0.80),
        (SourceId.MAPPING_REPLAY, 0.85),
        (SourceId.CLUSTER_MAPPING_REPLAY, 0.75),
        (SourceId.LLM_API_TARGETED, 0.70),
    ],
    "physical": [
        (SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
        (SourceId.API_ENTRATA_WIDGET, 0.95),
        (SourceId.API_GENERIC_NARROW, 0.90),
        (SourceId.API_SIGHTMAP, 0.90),
        (SourceId.MAPPING_REPLAY, 0.90),
        (SourceId.CLUSTER_MAPPING_REPLAY, 0.75),
        (SourceId.JSON_LD, 0.85),
        (SourceId.EMBEDDED_JSON, 0.80),
        (SourceId.DOM_PROFILE_HINTS, 0.75),
        (SourceId.DOM_CASCADE, 0.70),
        (SourceId.LLM_DOM_TARGETED, 0.70),
        (SourceId.LLM_API_TARGETED, 0.65),
        (SourceId.FIELD_PATCH, 0.60),
        (SourceId.LLM_MONOLITHIC, 0.55),
    ],
    "transactional": [
        (SourceId.API_RENTCAFE_UNITS, 0.95),
        (SourceId.API_SIGHTMAP, 0.95),
        (SourceId.API_ONESITE, 0.90),
        (SourceId.API_APPFOLIO_LISTINGS, 0.90),
        (SourceId.API_ENTRATA_WIDGET, 0.85),
        (SourceId.MAPPING_REPLAY, 0.85),
        (SourceId.CLUSTER_MAPPING_REPLAY, 0.75),
        (SourceId.API_GENERIC_NARROW, 0.80),
        (SourceId.JSON_LD, 0.75),
        (SourceId.DOM_CASCADE, 0.70),
        (SourceId.DOM_PROFILE_HINTS, 0.70),
        (SourceId.LLM_DOM_TARGETED, 0.65),
        (SourceId.LLM_API_TARGETED, 0.65),
        (SourceId.FIELD_PATCH, 0.60),
        (SourceId.LLM_MONOLITHIC, 0.55),
        (SourceId.DEFAULT_AVAILABILITY, 0.30),
    ],
}

# Field membership in groups
FIELD_GROUP: dict[str, str] = {
    "unit_id": "identity",
    "unit_number": "identity",
    "floor_plan_name": "physical",
    "floor_plan_id": "physical",
    "beds": "physical",
    "bedrooms": "physical",
    "baths": "physical",
    "bathrooms": "physical",
    "sqft": "physical",
    "rent_low": "transactional",
    "rent_high": "transactional",
    "asking_rent": "transactional",
    "market_rent_low": "transactional",
    "market_rent_high": "transactional",
    "available_date": "transactional",
    "availability_date": "transactional",
    "availability_status": "transactional",
}

CONFIDENCE_FLOORS: dict[str, float] = {
    "identity": 0.70,
    "physical": 0.50,
    "transactional": 0.55,
}


@dataclass(frozen=True)
class CompletenessReport:
    n_units: int
    pct_with_identity: float
    pct_with_physical: float
    pct_with_transactional: float
    pct_complete: float


def evaluate_completeness(units: list) -> CompletenessReport:
    """Compute completeness fractions. Pure function.

    A unit "has" a group iff at least one field in that group is present
    on the unit (above its confidence floor). pct_complete is the
    fraction of units that have all three groups.
    """
    n = len(units)
    if n == 0:
        return CompletenessReport(0, 0.0, 0.0, 0.0, 0.0)

    n_id = n_phys = n_trans = n_all = 0
    for u in units:
        has_id = _unit_has_group(u, "identity")
        has_phys = _unit_has_group(u, "physical")
        has_trans = _unit_has_group(u, "transactional")
        if has_id:
            n_id += 1
        if has_phys:
            n_phys += 1
        if has_trans:
            n_trans += 1
        if has_id and has_phys and has_trans:
            n_all += 1
    return CompletenessReport(
        n_units=n,
        pct_with_identity=n_id / n,
        pct_with_physical=n_phys / n,
        pct_with_transactional=n_trans / n,
        pct_complete=n_all / n,
    )


_PHYSICAL_REQUIRED_MIN = 2  # spec: ≥2 of {beds, baths, sqft, floor_plan_name}


def has_field_group(unit: Any, group: str, min_fields: int | None = None) -> bool:
    """Public version of _unit_has_group. Used by both planner and cascade.

    group: 'identity' | 'physical' | 'transactional'
    min_fields: override the threshold (default: 1 for identity/transactional, 2 for physical)

    Treats 0 and "0" as ABSENT (same as None/""/−1). This means sqft=0
    does not count as "having physical", and beds=0 (studio) does not
    count for completeness scoring — use _ZERO_IS_VALID_FIELDS in
    models.source for the separate concern of preserving studio data.
    """
    if min_fields is None:
        min_fields = _PHYSICAL_REQUIRED_MIN if group == "physical" else 1
    n = 0
    for field_name, fv in unit.items():
        if FIELD_GROUP.get(field_name) != group:
            continue
        try:
            value = fv.value
        except AttributeError:
            value = fv
        if value not in (None, "", -1, "-1", 0, "0"):
            n += 1
            if n >= min_fields:
                return True
    return False


def _unit_has_group(unit: Any, group: str) -> bool:
    """Internal alias retained for back-compat in evaluate_completeness."""
    return has_field_group(unit, group)


# ── Phase 4: Decision dataclass + plan_next_action ──────────────────────


@dataclass(frozen=True)
class Decision:
    action: str  # STOP | ESCALATE_LLM_TARGETED | ESCALATE_LINK_HOP | ESCALATE_LLM_MONOLITHIC | ACCEPT_PARTIAL
    target_field_group: str | None = None
    target_url: str | None = None
    rationale: str = ""


# Sources that the cascade ALREADY collects deterministically — never
# emit ESCALATE_* for these, since the cascade has run them already.
_DETERMINISTIC_CASCADE_SOURCES = frozenset(
    {
        SourceId.DOM_PROFILE_HINTS,
        SourceId.MAPPING_REPLAY,
        SourceId.CLUSTER_MAPPING_REPLAY,
        SourceId.FIELD_PATCH,
        SourceId.JSON_LD,
        SourceId.EMBEDDED_JSON,
        SourceId.DOM_CASCADE,
        SourceId.DEFAULT_AVAILABILITY,
    }
)

_LLM_TARGETED_SOURCES = frozenset({SourceId.LLM_API_TARGETED, SourceId.LLM_DOM_TARGETED})


# Patch #1 (2026-05-06 audit) — anchor patterns that strongly imply a
# property has a dedicated pricing/availability sub-page. If the entry HTML
# contains any anchor href OR visible text matching one of these patterns
# AND the URL hasn't been visited yet, the planner returns ESCALATE_LINK_HOP
# regardless of per-unit completeness. The homepage is rarely the canonical
# pricing page on multifamily sites; if a sub-page link exists, it almost
# always carries more or different data than the hero/landing area.
_CANONICAL_SUBPAGE_HREF_PATTERNS = (
    "/floor-plans",
    "/floorplans",
    "/floor_plans",
    "/availability",
    "/availabilities",
    "/apartments",
    "/pricing",
    "/units",
    "/lease",
    "/leasing",
    "/rates",
    "/availability-by-floor-plan",
    "/our-apartments",
    "/our-units",
    "/our-floorplans",
    "/our-floor-plans",
)
_CANONICAL_SUBPAGE_TEXT_PATTERNS = (
    "floor plan",
    "floorplan",
    "view availability",
    "see availability",
    "check availability",
    "all apartments",
    "all units",
    "view pricing",
    "view rates",
    "view rentals",
    "view our apartments",
    "live here",
    "lease now",
    "browse units",
)


def has_unvisited_canonical_subpage(
    entry_html: str | None,
    visited_urls: set[str] | None = None,
    *,
    base_url: str = "",
) -> bool:
    """Return True iff the entry HTML contains an anchor whose href OR
    visible text suggests a dedicated floor-plans / availability / pricing
    sub-page that we haven't yet fetched.

    The check is deliberately broad — false positives are cheap (one extra
    sub-page fetch under the link-hop budget) while false negatives mean we
    declare a property "done" with only homepage data, which has been the
    single largest cause of partial-rent extractions in production.

    Pure-Python and never raises. If the HTML cannot be parsed (None, empty,
    or malformed), returns False so the planner falls through to its
    standard pct_complete logic.
    """
    if not entry_html or len(entry_html) < 100:
        return False

    visited = {u.strip() for u in (visited_urls or set()) if u}

    # Fast-path: split anchors by `<a ` tokens — full HTML parsing is
    # overkill and parser failures must not break the planner.
    import re

    anchor_re = re.compile(r"<a\b[^>]*\bhref\s*=\s*['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
    text_strip_re = re.compile(r"<[^>]+>")

    href_patterns = tuple(p.lower() for p in _CANONICAL_SUBPAGE_HREF_PATTERNS)
    text_patterns = tuple(p.lower() for p in _CANONICAL_SUBPAGE_TEXT_PATTERNS)

    base_lower = base_url.strip().rstrip("/").lower()

    for match in anchor_re.finditer(entry_html):
        href = (match.group(1) or "").strip()
        anchor_text = (match.group(2) or "").strip()
        if not href:
            continue
        href_lower = href.lower()
        anchor_text_clean = text_strip_re.sub(" ", anchor_text).strip().lower()

        # href-pattern hit?
        href_hit = any(p in href_lower for p in href_patterns)
        # text-pattern hit?
        text_hit = any(p in anchor_text_clean for p in text_patterns)

        if not (href_hit or text_hit):
            continue

        # Skip non-navigational anchors.
        if href_lower.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        # Skip if already visited (compare both raw and normalised).
        normalised = href.strip()
        # Make absolute if relative.
        if normalised.startswith("/") and base_lower:
            normalised = base_lower + normalised
        if normalised in visited or href in visited:
            continue
        # Skip the entry URL itself.
        if base_lower and normalised.rstrip("/").lower() == base_lower:
            continue

        return True
    return False


def rank_sources_for_field_group(
    field_group: str,
    pms_name: str = "unknown",
    profile_preferences: list[Any] | None = None,
) -> list[tuple[SourceId, float]]:
    """Return the source ranking for a field group.

    If profile_preferences (list of SourceObservation) has data, observed
    winners float to the top within their tier; default ranking otherwise.
    """
    default = list(DEFAULT_SOURCE_RANKING.get(field_group, []))
    if not profile_preferences:
        return default

    prefs_for_group = [p for p in profile_preferences if getattr(p, "field_group", None) == field_group]
    if not prefs_for_group:
        return default
    prefs_for_group.sort(
        key=lambda p: getattr(p, "contribution_count", 0), reverse=True
    )

    promoted: list[tuple[SourceId, float]] = []
    seen: set[SourceId] = set()
    for p in prefs_for_group:
        try:
            sid = SourceId(getattr(p, "source_id", ""))
        except ValueError:
            continue  # stale source_id from older schema
        base_conf = next((c for s, c in default if s == sid), 0.7)
        promoted.append((sid, base_conf))
        seen.add(sid)
    for sid, conf in default:
        if sid not in seen:
            promoted.append((sid, conf))
    return promoted


def plan_next_action(
    report: CompletenessReport,
    sources_already_run: set,
    budget_remaining: dict,
    pms_name: str = "unknown",
    profile_completeness_floor: dict | None = None,
    profile_preferences: list[Any] | None = None,
    *,
    entry_html: str | None = None,
    entry_url: str = "",
    visited_urls: set[str] | None = None,
) -> Decision:
    """The decision map. Returns at most ONE Decision per call.

    Decision rules (locked):
      HOMEPAGE_ONLY:  if extraction has only sourced from the entry/homepage
                      and the entry HTML contains an anchor to a floor-plans /
                      availability / pricing sub-page we haven't visited,
                      ESCALATE_LINK_HOP unconditionally (Patch #1, 2026-05-06).
                      This rule fires *before* STOP because a 1-unit "complete"
                      extraction from the homepage hero is structurally
                      insufficient when a /floorplans page exists.
      STOP:           pct_complete >= floor_complete AND pct_with_transactional >= floor_trans
      TARGET_GAP:     0.50 <= pct_complete < floor_complete
                      → identify smallest axis; pick best untried LLM/link-hop
      BROAD_RECOVERY: pct_complete < 0.50
                      → link-hop preferred, then llm_monolithic
    """
    floor = profile_completeness_floor or {}
    floor_pct_complete = max(0.50, float(floor.get("complete", 0.90)))
    floor_pct_trans = max(0.50, float(floor.get("transactional", 0.70)))

    # Patch #1 — structural homepage check. If we've only sourced from the
    # entry URL and an unvisited canonical sub-page exists, hop unconditionally.
    # `sources_already_run` being empty is a proxy for "no link-hop has fired
    # yet on this property"; this method is called before each escalation,
    # so the first call sees an empty set.
    if (
        not sources_already_run
        and budget_remaining.get("link_hop", 0) > 0
        and has_unvisited_canonical_subpage(entry_html, visited_urls, base_url=entry_url)
    ):
        return Decision(
            action="ESCALATE_LINK_HOP",
            rationale="homepage-only extraction; canonical sub-page unvisited",
        )

    # STOP
    if report.pct_complete >= floor_pct_complete and report.pct_with_transactional >= floor_pct_trans:
        return Decision(
            action="STOP",
            rationale=f"complete={report.pct_complete:.2f}>={floor_pct_complete:.2f}",
        )

    pct_by_group = {
        "identity": report.pct_with_identity,
        "physical": report.pct_with_physical,
        "transactional": report.pct_with_transactional,
    }
    failing_group = min(pct_by_group, key=lambda k: pct_by_group[k])

    # BROAD_RECOVERY (pct_complete < 0.50)
    if report.pct_complete < 0.50:
        if budget_remaining.get("link_hop", 0) > 0:
            return Decision(
                action="ESCALATE_LINK_HOP",
                target_field_group=failing_group,
                rationale=f"broad recovery: pct={report.pct_complete:.2f}",
            )
        if budget_remaining.get("llm_monolithic", 0) > 0:
            return Decision(
                action="ESCALATE_LLM_MONOLITHIC",
                rationale=f"broad recovery: pct={report.pct_complete:.2f}",
            )
        return Decision(
            action="ACCEPT_PARTIAL",
            rationale="broad recovery: no budget",
        )

    # TARGET_GAP (0.50 <= pct_complete < floor_complete)
    ranking = rank_sources_for_field_group(failing_group, pms_name, profile_preferences)
    for source_id, _conf in ranking:
        if source_id in sources_already_run:
            continue
        if source_id in _LLM_TARGETED_SOURCES:
            if (budget_remaining.get("llm_api_calls", 0) + budget_remaining.get("llm_dom_calls", 0)) > 0:
                return Decision(
                    action="ESCALATE_LLM_TARGETED",
                    target_field_group=failing_group,
                    rationale=f"gap in {failing_group}, best untried={source_id.value}",
                )
            continue
        if source_id == SourceId.LLM_MONOLITHIC:
            continue  # only fires in BROAD_RECOVERY
        if source_id in _DETERMINISTIC_CASCADE_SOURCES:
            continue  # the cascade should have already collected these
        # Otherwise it's a PMS-specific API source the cascade didn't capture
        # (e.g. a sub-page hosts API_RENTCAFE_UNITS while main has only
        # API_RENTCAFE_FLOORPLANS) → link-hop is the lever.
        if budget_remaining.get("link_hop", 0) > 0:
            return Decision(
                action="ESCALATE_LINK_HOP",
                target_field_group=failing_group,
                rationale=f"gap in {failing_group}, untried API source={source_id.value}",
            )
    return Decision(action="ACCEPT_PARTIAL", rationale="no untried source within budget")


def compute_budget(profile: Any, is_cold: bool) -> dict:
    """Per-property LLM budget for this run.

    Keys are PER-CALL caps, not per-run. The cascade decrements them
    each time it makes an LLM call.

    HOT/WARM: 3 API LLM probes + 1 DOM LLM probe + 1 monolithic + 3 link-hops.
    COLD: rotates by cold_run_count to vary the API/monolithic/link-hop path each
    retry, but always keeps 1 DOM-LLM probe available — that tier is the only
    extractor that works on marketing/boutique-CMS sites without API or JSON-LD,
    and gating it caused a -15pp success-rate regression in May 2026.
    """
    if not is_cold:
        return {
            "llm_api_calls": 3,   # per-response cap; mirrors legacy api_llm_budget
            "llm_dom_calls": 1,   # per-page cap; mirrors legacy dom_llm_budget
            "llm_monolithic": 1,  # one shot per run
            "link_hop": 3,
        }

    n = 0
    try:
        n = int(getattr(getattr(profile, "confidence", None), "cold_run_count", 0) or 0)
    except Exception:
        n = 0
    if n % 3 == 0:
        return {"llm_api_calls": 1, "llm_dom_calls": 1, "llm_monolithic": 0, "link_hop": 1}
    if n % 3 == 1:
        return {"llm_api_calls": 0, "llm_dom_calls": 1, "llm_monolithic": 0, "link_hop": 3}
    return {"llm_api_calls": 0, "llm_dom_calls": 1, "llm_monolithic": 1, "link_hop": 1}
