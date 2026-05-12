"""ActionDecider — budget-aware action selection.

The RC3 fix lives here: "defer monolithic LLM when DOM analysis found a
navigation_hint and there is a high-confidence hop target."

This is the ONLY place in the codebase where "defer monolithic LLM" logic lives.

Design invariants:
- decide() is pure: given a DecisionContext it returns an ExtractionAction.
- ExtractionAction.budget_after is always a NEW dict (never a mutation of input).
- The RC3 deferral guard: score ≥ 9_000 only (LLM_HINT, PROFILE_WINNING,
  EXTERNAL_PORTAL). PMS_PRIOR (5_000) is NOT sufficient to skip the monolithic
  on the current page — too speculative.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ma_poc.pms.signal_engine.models import SourceKind

if TYPE_CHECKING:
    from ma_poc.pms.signal_engine.ranker import RankedSignal


class ActionType(StrEnum):
    PARSE_API              = "parse_api"
    SEARCH_DOM             = "search_dom"
    ANALYZE_LLM_API        = "analyze_llm_api"
    ANALYZE_LLM_DOM        = "analyze_llm_dom"
    ANALYZE_LLM_MONOLITHIC = "analyze_llm_monolithic"
    HOP_TO_URL             = "hop_to_url"
    STOP                   = "stop"


@dataclass(frozen=True)
class ExtractionAction:
    """Immutable decision returned by ActionDecider.decide().

    budget_after is always a NEW dict — the caller should adopt it as the
    next budget state. Never mutate the input budget dict.
    """

    action_type: ActionType
    target: "RankedSignal | None"
    rationale: str
    budget_after: dict[str, Any]


@dataclass
class DOMAnalysisResult:
    """Subset of DOM LLM output relevant to ActionDecider.

    Populated from the dom_hints envelope returned by analyze_dom_with_llm().
    unit_count=0 with navigation_hint set is the trigger condition for RC3.
    """

    unit_count: int = 0
    navigation_hint: str | None = None


@dataclass
class DecisionContext:
    """Input to ActionDecider.decide()."""

    ranked_signals: "list[RankedSignal]"
    current_unit_count: int
    budget: dict[str, Any]
    dom_analysis_result: DOMAnalysisResult | None
    hop_depth: int = 0
    # True when the current page already has rent or floor-plan signals in
    # its static HTML — suppresses RC3 deferral so the LLM runs on this
    # page immediately instead of chasing a hop that may be equally empty.
    page_has_content_signals: bool = False


class ActionDecider:
    """Decide the next extraction action given current signals and budget.

    Rule priority:
      1. Units already found → STOP
      2. RC3: DOM analysis found hint + high-confidence hop + monolithic budget → HOP (defer LLM)
      3. No signals → STOP
      4. Dispatch top-ranked qualified signal
    """

    # Minimum composite_score to qualify as a "high confidence" hop target
    # for the RC3 deferral. LLM_HINT (10_000), PROFILE_WINNING (10_001), and
    # EXTERNAL_PORTAL (10_000) all exceed this threshold. PMS_PRIOR (5_000)
    # does not — too speculative to skip the monolithic LLM on current page.
    _HIGH_CONF_HOP_THRESHOLD: int = 9_000

    _HIGH_CONF_HOP_KINDS: frozenset[SourceKind] = frozenset({
        SourceKind.LLM_HINT,
        SourceKind.PROFILE_WINNING,
        SourceKind.EXTERNAL_PORTAL,
    })

    def decide(self, ctx: DecisionContext) -> ExtractionAction:
        # Rule 1: units already found → stop immediately
        if ctx.current_unit_count > 0:
            return ExtractionAction(
                action_type=ActionType.STOP,
                target=None,
                rationale="units_found",
                budget_after=dict(ctx.budget),
            )

        # Rule 2 (RC3): defer monolithic LLM to hop page.
        #
        # Conditions (all must hold):
        #   a. DOM analysis ran and returned 0 units
        #   b. DOM analysis provided a navigation_hint (LLM diagnosed where data is)
        #   c. There is a high-confidence (≥9000) hop candidate in ranked_signals
        #   d. The monolithic LLM budget is still available
        #   e. hop_depth == 0 — never cascade RC3 from a hop page back to
        #      another hop. Without this guard, RC3 fires on /floorplans and
        #      defers to securecafe (already blocked from entry-page hop),
        #      burning the monolithic budget on a guaranteed failure.
        #
        # NOTE: page_has_content_signals intentionally does NOT gate this rule.
        # Even when the entry page has some data from free tiers (api_broad /
        # jsonld / dom_scan), high-confidence floor-plan and availability hops
        # should still be followed — the entry page result is the "free minimum"
        # baseline, not the authoritative unit listing. LLM budget is reserved
        # for hop pages, not consumed on the homepage.
        #
        # Effect: HOP_TO_URL is returned with budget_after["llm_monolithic"] UNCHANGED
        # (not decremented) so the monolithic fires on the hop page instead.
        if (
            ctx.dom_analysis_result is not None
            and ctx.dom_analysis_result.unit_count == 0
            and ctx.dom_analysis_result.navigation_hint is not None
            and int(ctx.budget.get("llm_monolithic", 0)) > 0
            and ctx.hop_depth == 0
        ):
            high_conf_hops = [
                rs for rs in ctx.ranked_signals
                if rs.signal.kind in self._HIGH_CONF_HOP_KINDS
                and rs.composite_score >= self._HIGH_CONF_HOP_THRESHOLD
            ]
            if high_conf_hops:
                return ExtractionAction(
                    action_type=ActionType.HOP_TO_URL,
                    target=high_conf_hops[0],
                    rationale="dom_analysis_defer_monolithic_to_hop",
                    budget_after=dict(ctx.budget),  # NOT decremented — used on hop page
                )

        # Rule 3: no signals → stop
        if not ctx.ranked_signals:
            return ExtractionAction(
                action_type=ActionType.STOP,
                target=None,
                rationale="no_signals",
                budget_after=dict(ctx.budget),
            )

        # Rule 4: dispatch top-ranked qualified signal
        top = ctx.ranked_signals[0]
        action = self._map_to_action(top.signal.kind, ctx.budget)
        new_budget = self._decrement(ctx.budget, action)
        return ExtractionAction(
            action_type=action,
            target=top,
            rationale=f"top_signal:{top.reason}",
            budget_after=new_budget,
        )

    def _map_to_action(
        self, kind: SourceKind, budget: dict[str, Any]
    ) -> ActionType:
        if kind in (SourceKind.API_RESPONSE, SourceKind.EMBEDDED_JSON, SourceKind.JSON_LD):
            return ActionType.PARSE_API
        if kind == SourceKind.DOM_SECTION:
            if int(budget.get("llm_dom_calls", 0)) > 0:
                return ActionType.ANALYZE_LLM_DOM
            return ActionType.SEARCH_DOM
        if kind in (
            SourceKind.LLM_HINT,
            SourceKind.PROFILE_WINNING,
            SourceKind.PROFILE_NAV_HINT,
            SourceKind.INTERNAL_LINK,
            SourceKind.EXTERNAL_PORTAL,
            SourceKind.PMS_PRIOR,
            SourceKind.UNIVERSAL_PRIOR,
        ):
            return ActionType.HOP_TO_URL
        return ActionType.STOP

    def _decrement(
        self, budget: dict[str, Any], action: ActionType
    ) -> dict[str, Any]:
        """Return a new budget dict with the appropriate counter decremented."""
        _decrements: dict[ActionType, str] = {
            ActionType.ANALYZE_LLM_API:        "llm_api_calls",
            ActionType.ANALYZE_LLM_DOM:        "llm_dom_calls",
            ActionType.ANALYZE_LLM_MONOLITHIC: "llm_monolithic",
            ActionType.HOP_TO_URL:             "link_hop",
        }
        # Always return a NEW dict — never mutate the input (spec invariant)
        b = dict(budget)
        key = _decrements.get(action)
        if key and int(b.get(key, 0)) > 0:
            b[key] = int(b[key]) - 1
        return b
