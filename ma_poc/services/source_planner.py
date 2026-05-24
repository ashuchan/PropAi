"""
Source planner — ranking tables, completeness gates, decision map.

Phase 3 introduced the constants the merger needs (FIELD_GROUP,
CONFIDENCE_FLOORS, DEFAULT_SOURCE_RANKING). Phase 4 adds the
Decision dataclass, plan_next_action, rank_sources_for_field_group,
and compute_budget.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from ma_poc.models.source import SourceId

# F0.1 (2026-05-09): per-property LLM cost cap is env-configurable so prod
# can dial it without a code deploy. The 1.50 default was chosen after the
# 2026-05-09 cloud-run regression analysis showed the prior $1.00 cap
# starved link-hop pages of their LLM rescue path (commit 6a6e389).
# Hop-bonus is added once per ESCALATE_LINK_HOP session and is bounded by
# a 3× hard ceiling inside scraper.py so a misconfigured env var cannot
# uncap spend.
_DEFAULT_PROPERTY_LLM_COST_CAP_USD = 1.50
_DEFAULT_PROPERTY_LLM_COST_CAP_HOP_BONUS_USD = 0.50


def _read_positive_float_env(name: str, default: float) -> float:
    """Read ``name`` as a positive float. Falls back to ``default`` for
    missing, empty, malformed, or non-positive values. Never raises."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0 or value != value:  # rejects 0, negatives, NaN
        return default
    return value


def get_property_llm_cost_cap_usd() -> float:
    """Per-property USD cap on LLM spend, env-overridable.

    Read fresh from env on every call so tests can monkey-patch
    ``PROPERTY_LLM_COST_CAP_USD`` without re-importing the module.
    """
    return _read_positive_float_env(
        "PROPERTY_LLM_COST_CAP_USD",
        _DEFAULT_PROPERTY_LLM_COST_CAP_USD,
    )


def get_property_llm_cost_cap_hop_bonus_usd() -> float:
    """Per-link-hop USD bonus added to the cost cap before a sub-page hop.

    Granted once per ESCALATE_LINK_HOP session in ``pms/scraper.py`` and
    capped there at 3× the base cap (``get_property_llm_cost_cap_usd``).
    """
    return _read_positive_float_env(
        "PROPERTY_LLM_COST_CAP_HOP_BONUS_USD",
        _DEFAULT_PROPERTY_LLM_COST_CAP_HOP_BONUS_USD,
    )

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
    units_all_inferred: bool = False,
) -> Decision:
    """The decision map. Returns at most ONE Decision per call.

    Decision rules (locked):
      STOP:           pct_complete >= floor_complete AND pct_with_transactional >= floor_trans
      TARGET_GAP:     0.50 <= pct_complete < floor_complete
                      → identify smallest axis; pick best untried LLM/link-hop
      BROAD_RECOVERY: pct_complete < 0.50
                      → link-hop preferred, then llm_monolithic

    ``units_all_inferred`` (B5, 2026-05-16): when True, the caller has
    determined that every emitted unit has an inferred (not natural)
    identifier — i.e. these are plan-level summaries, not specific
    apartments. Even if the completeness math says STOP, we should
    continue link-hopping to discover the per-unit detail pages.
    Without this guard, properties like 2982 Cortland on Pike (1 spurious
    DOM unit, manual=80) and 5822 The Izzy (6 JSON-LD floor-plan rows,
    manual=75) STOP at pct_complete=1.0 and never reach /available-apartments/.
    """
    floor = profile_completeness_floor or {}
    floor_pct_complete = max(0.50, float(floor.get("complete", 0.90)))
    floor_pct_trans = max(0.50, float(floor.get("transactional", 0.70)))

    # B5 (2026-05-16): if every emitted unit is plan-level (inferred id),
    # do NOT stop the cascade — there are likely real per-unit rows
    # behind a link-hop or in a portal iframe. Bounded by:
    #   • n_units <= _B5_MAX_INFERRED_FOR_ESCALATION — beyond that, the
    #     cascade has produced enough plan rows that further hops just
    #     inflate the count without adding real units (the 2026-05-16
    #     canary on 722 Canyon Ridge / 8188 Montclair / 67327 Windsong
    #     showed B5 over-firing when the LLM_DOM tier already returned
    #     12+ plan rows).
    #   • link-hop budget > 0 — otherwise we have nothing to escalate to.
    _B5_MAX_INFERRED_FOR_ESCALATION = 6
    if (
        units_all_inferred
        and 0 < report.n_units <= _B5_MAX_INFERRED_FOR_ESCALATION
        and budget_remaining.get("link_hop", 0) > 0
    ):
        return Decision(
            action="ESCALATE_LINK_HOP",
            target_field_group="identity",
            rationale=(
                f"all {report.n_units} units are plan-level (inferred ids); "
                "hop for per-unit detail"
            ),
        )

    # STOP
    # 2026-05-24 Phase 4.2: require N>=3 distinct units before allowing
    # STOP. The pre-fix rule fired STOP as soon as ONE field-complete
    # unit was found — premature for properties with many apartments
    # behind a link-hop. Forensic on run 2026-05-23: HIGH-unit cohort
    # (units>20) avg 8.1 hops; LOW-unit cohort (units=1 SUCCESS) avg
    # 2.8 hops. The STOP gate fired on the very first per-plan hit and
    # dropped 4-8 sibling URLs. With this guard, the cascade continues
    # hopping when there's still link-hop budget AND <3 units captured.
    # When budget is exhausted, fall through to the existing STOP path
    # below (the action="STOP" return at the end of the function).
    _MIN_UNITS_TO_STOP = 3
    _stop_completeness_satisfied = (
        report.pct_complete >= floor_pct_complete
        and report.pct_with_transactional >= floor_pct_trans
    )
    if _stop_completeness_satisfied and report.n_units < _MIN_UNITS_TO_STOP:
        # Don't stop yet — try one more hop if budget allows. Most
        # properties have ≥3 units; the N<3 case is almost always "we
        # found the first plan and would have walked away from the rest".
        if budget_remaining.get("link_hop", 0) > 0:
            return Decision(
                action="ESCALATE_LINK_HOP",
                target_field_group="identity",
                rationale=(
                    f"completeness OK but only {report.n_units} distinct units "
                    f"(< {_MIN_UNITS_TO_STOP}); hop budget remaining"
                ),
            )
        # No budget left — fall through to STOP below.
    if _stop_completeness_satisfied:
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


# Source-confidence tightening — read by compute_budget below.
#
# A profile with consistently-high-confidence saved sources doesn't
# benefit from re-paying for fresh LLM probes; the cached hint should
# fire. We trim per-source LLM caps by 1 (floor at 1) so there's always
# one fresh probe to detect drift, while still freeing budget for
# whichever source IS struggling.

# Min ``avg_confidence_when_won`` for a source to be considered
# "high-confidence enough to skip a probe".
_SOURCE_CONFIDENCE_TIGHTEN_THRESHOLD = 0.85
# Min ``contribution_count`` to avoid trimming on a one-off lucky win.
_SOURCE_CONFIDENCE_MIN_CONTRIBUTIONS = 5
# Floor — never trim below 1 fresh probe per cascade tier.
_SOURCE_CONFIDENCE_MIN_PROBE_FLOOR = 1


def _tighten_budget_by_source_confidence(profile: Any, budget: dict) -> dict:
    """Tighten the per-source LLM budget when saved-source confidence is high.

    Behind ``ENABLE_SOURCE_TIERED_BUDGET`` (default OFF). When ON and
    the profile has any source observation meeting both
    ``avg_confidence_when_won >= 0.85`` and ``contribution_count >= 5``,
    decrement the matching budget key by 1 (floor at 1).

    Maps source_id → budget key:
      - ``llm_api_targeted``   → ``llm_api_calls``
      - ``llm_dom_targeted``   → ``llm_dom_calls``

    Returns a new dict; never mutates the input.
    """
    try:
        from ma_poc.config.feature_flags import enable_source_tiered_budget
        if not enable_source_tiered_budget():
            return budget
    except Exception:
        return budget

    try:
        observations = list(getattr(profile.api_hints, "source_observations", None) or [])
    except Exception:
        observations = []
    if not observations:
        return budget

    src_to_budget_key = {
        "llm_api_targeted": "llm_api_calls",
        "llm_dom_targeted": "llm_dom_calls",
    }

    # Pick the highest-confidence observation per source_id (a profile
    # can have one row per (source_id, field_group) — we want the
    # best-observed signal, not the per-field-group one).
    best_by_source: dict[str, float] = {}
    contrib_by_source: dict[str, int] = {}
    for obs in observations:
        try:
            sid = str(getattr(obs, "source_id", "") or "")
            conf = float(getattr(obs, "avg_confidence_when_won", 0.0) or 0.0)
            contrib = int(getattr(obs, "contribution_count", 0) or 0)
        except Exception:
            continue
        if sid not in src_to_budget_key:
            continue
        if conf > best_by_source.get(sid, -1.0):
            best_by_source[sid] = conf
        contrib_by_source[sid] = max(contrib_by_source.get(sid, 0), contrib)

    out = dict(budget)
    for sid, conf in best_by_source.items():
        if (
            conf >= _SOURCE_CONFIDENCE_TIGHTEN_THRESHOLD
            and contrib_by_source.get(sid, 0) >= _SOURCE_CONFIDENCE_MIN_CONTRIBUTIONS
        ):
            key = src_to_budget_key[sid]
            current = int(out.get(key, 0))
            out[key] = max(_SOURCE_CONFIDENCE_MIN_PROBE_FLOOR, current - 1)
    return out


def compute_budget(profile: Any, is_cold: bool) -> dict:
    """Per-property LLM budget for this run.

    Keys are PER-CALL caps, not per-run. The cascade decrements them
    each time it makes an LLM call.

    HOT/WARM: 3 API LLM probes + 1 DOM LLM probe + 1 monolithic + 3 link-hops.
    COLD: rotates by cold_run_count to vary the API/monolithic/link-hop path each
    retry, but always keeps 1 DOM-LLM probe available — that tier is the only
    extractor that works on marketing/boutique-CMS sites without API or JSON-LD,
    and gating it caused a -15pp success-rate regression in May 2026.

    Every returned dict carries ``_cost_cap_usd`` from
    :func:`get_property_llm_cost_cap_usd` so the GenericAdapter cost gate
    sees the env-configured value rather than its in-code fallback.
    """
    cost_cap = get_property_llm_cost_cap_usd()
    if not is_cold:
        budget = {
            "llm_api_calls": 3,   # per-response cap; mirrors legacy api_llm_budget
            "llm_dom_calls": 1,   # per-page cap; mirrors legacy dom_llm_budget
            "llm_monolithic": 1,  # one shot per run
            "link_hop": 3,
            "_cost_cap_usd": cost_cap,
        }
        return _tighten_budget_by_source_confidence(profile, budget)

    n = 0
    try:
        n = int(getattr(getattr(profile, "confidence", None), "cold_run_count", 0) or 0)
    except Exception:
        n = 0
    if n % 3 == 0:
        return {
            "llm_api_calls": 1,
            "llm_dom_calls": 1,
            "llm_monolithic": 0,
            "link_hop": 1,
            "_cost_cap_usd": cost_cap,
        }
    if n % 3 == 1:
        return {
            "llm_api_calls": 0,
            "llm_dom_calls": 1,
            "llm_monolithic": 0,
            "link_hop": 3,
            "_cost_cap_usd": cost_cap,
        }
    return {
        "llm_api_calls": 0,
        "llm_dom_calls": 1,
        "llm_monolithic": 1,
        "link_hop": 1,
        "_cost_cap_usd": cost_cap,
    }
