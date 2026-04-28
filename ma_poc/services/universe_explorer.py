"""Site-universe driven exploration loop — the "6d" tier.

Replaces the hard-coded 3-API + 1-DOM + 1-monolithic waterfall in
:class:`ma_poc.pms.adapters.generic.GenericAdapter` with a confidence-driven
priority queue:

  1. Gather (already done by caller — see :mod:`ma_poc.services.site_universe`).
  2. Rank — single LLM call scores every candidate.
  3. Loop: pop highest-confidence candidate, extract, merge results.
  4. Quality check — exit if both minimum and important fields met.
  5. Re-rank for availability when minimum-met but important-missing.
  6. Stop when budget exhausted, queue empty, or quality met.

The explorer uses :func:`analyze_api_with_llm` and :func:`analyze_dom_with_llm`
as its per-item extractors so the LLM-cost format stays compatible with
:mod:`ma_poc.observability.cost_ledger`.
"""

from __future__ import annotations

import logging
import os
import time as _time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ma_poc.services.extraction_quality import (
    QualityVerdict,
    aggregate_quality,
    merge_units,
)
from ma_poc.services.llm_ranker import (
    rank_universe,
    rerank_for_availability,
)
from ma_poc.services.site_universe import (
    ApiCandidate,
    LinkCandidate,
    SiteUniverse,
    apply_llm_scores,
    gather_universe,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration — read once from env. All knobs spec'd in the planning doc.
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    """Parse a float env var with safe fallback. Logs and falls through on
    bad values so a typo in the env never crashes the explorer."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("env %s=%r is not a float; falling back to %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("env %s=%r is not an int; falling back to %s", name, raw, default)
        return default


HIGH_CONFIDENCE_THRESHOLD = _env_float("JUGNU_HIGH_CONFIDENCE_THRESHOLD", 0.7)
QUALITY_MIN_THRESHOLD = _env_float("JUGNU_QUALITY_MIN_THRESHOLD", 0.7)
API_WEIGHT_BOOST = _env_float("JUGNU_API_WEIGHT_BOOST", 0.15)
# Effective-score threshold above which a parent-universe API is exhausted
# BEFORE any link is fetched. Same-domain APIs with property_id-style query
# strings (LLM-scored 0.7-0.9) typically carry exactly the unit data we
# want — fetching links via Playwright is much more expensive (1-3 LLM
# calls per link + render time) and should only happen after the cheap
# direct API hits have all been tried.
API_FIRST_THRESHOLD = _env_float("JUGNU_API_FIRST_THRESHOLD", 0.5)
MAX_LLM_CALLS = _env_int("JUGNU_UNIVERSE_MAX_LLM_CALLS", 8)
MAX_LINKS_EXPLORED = _env_int("JUGNU_UNIVERSE_MAX_LINKS_EXPLORED", 3)
MAX_EXTERNAL_LINKS = _env_int("JUGNU_UNIVERSE_MAX_EXTERNAL_LINKS", 1)
# Sub-universe cap — when a link is fetched, gather its sub-universe and
# only the TOP-K items in that sub-universe are eligible for extraction.
# Keeps the budget from being eaten by a single junky portal page.
SUB_UNIVERSE_TOP_K = _env_int("JUGNU_SUB_UNIVERSE_TOP_K", 1)
# Sub-universe LLM-rank skip threshold — sub-universes this small skip the
# LLM rank call entirely and order purely by heuristic base_score. The
# whole reason sub-rank exists is to break body-bytes ties on noisy
# cookie/analytics JSON; with N candidates this small, dedupe + JSON
# filter have already reduced the noise enough that the heuristic order
# is fine.
SUB_UNIVERSE_RANK_MIN = _env_int("JUGNU_SUB_UNIVERSE_RANK_MIN", 4)
LLM_API_CHUNK_BYTES = _env_int("JUGNU_LLM_API_CHUNK_BYTES", 30_000)
LLM_DOM_CHUNK_BYTES = _env_int("JUGNU_LLM_DOM_CHUNK_BYTES", 20_000)
RANKING_PREVIEW_BYTES = _env_int("JUGNU_UNIVERSE_RANKING_PREVIEW_BYTES", 2_000)
MAX_WALL_CLOCK_MS = _env_int("JUGNU_UNIVERSE_MAX_WALL_CLOCK_MS", 90_000)

# When ``JUGNU_UNIVERSE_DEBUG=1`` the explorer captures the full pre-rank
# universe, the raw LLM ranker output, and every per-item LLM exchange on
# ``ExplorationOutcome.debug``. Off by default so production runs don't
# carry verbose payloads through cost ledgers and reports.
DEBUG = os.getenv("JUGNU_UNIVERSE_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")

# External-link confidence floor — spec §13: external links eligible only if
# scored >= HIGH_CONFIDENCE + 0.1.
EXTERNAL_LINK_CONFIDENCE_FLOOR = HIGH_CONFIDENCE_THRESHOLD + 0.10


# ---------------------------------------------------------------------------
# Outcome contract
# ---------------------------------------------------------------------------


@dataclass
class ExplorationOutcome:
    """What the explorer hands back to the GenericAdapter.

    Mirrors the fields the legacy 6a/6b/6c path attaches to AdapterResult,
    so the calling cascade can stitch the outcome into its existing return
    shape without restructuring the adapter contract.
    """

    units: list[dict[str, Any]] = field(default_factory=list)
    quality: QualityVerdict | None = None
    winning_url: str | None = None
    explored_apis: int = 0
    explored_links: int = 0
    llm_calls_made: int = 0
    rerank_fired: bool = False
    tier_attempts: list[dict[str, Any]] = field(default_factory=list)
    llm_interactions: list[dict[str, Any]] = field(default_factory=list)
    llm_field_mappings: list[dict[str, Any]] = field(default_factory=list)
    llm_analysis_results: dict[str, Any] = field(default_factory=dict)
    llm_navigation_hints: list[str] = field(default_factory=list)
    explored_links_map: dict[str, bool] = field(default_factory=dict)
    # Per-property URL-seen set — populated as the parent universe AND
    # every sub-universe gets gathered, so the same noise URL (e.g. the
    # cookielaw OneTrust JSON that re-loads on every sub-page) never
    # consumes a second LLM call.
    seen_urls: set[str] = field(default_factory=set)
    error: str | None = None
    stop_reason: str = ""
    # Populated only when JUGNU_UNIVERSE_DEBUG is on — preserves the
    # gather → rank → explore intermediates so the experiment harness can
    # render exactly what the LLM saw and what it scored back.
    debug: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Budget — single shared counter across rank, rerank, per-item extractions.
# ---------------------------------------------------------------------------


@dataclass
class ExplorationBudget:
    """Per-property exploration budget.

    Decremented BEFORE each LLM call (vs. legacy code that decremented
    after) so a slow-running call can't overshoot the cap. Wall-clock
    bound is checked every loop iteration so a misbehaving site can't
    stall the whole pool.
    """

    llm_calls_remaining: int = MAX_LLM_CALLS
    links_remaining: int = MAX_LINKS_EXPLORED
    external_links_remaining: int = MAX_EXTERNAL_LINKS
    deadline_monotonic: float = 0.0

    @classmethod
    def fresh(cls) -> "ExplorationBudget":
        return cls(
            llm_calls_remaining=MAX_LLM_CALLS,
            links_remaining=MAX_LINKS_EXPLORED,
            external_links_remaining=MAX_EXTERNAL_LINKS,
            deadline_monotonic=_time.monotonic() + MAX_WALL_CLOCK_MS / 1000.0,
        )

    def can_call_llm(self) -> bool:
        return self.llm_calls_remaining > 0 and not self.timed_out()

    def consume_llm(self) -> None:
        self.llm_calls_remaining -= 1

    def can_explore_link(self, candidate: LinkCandidate) -> bool:
        if self.timed_out():
            return False
        if self.links_remaining <= 0:
            return False
        if not candidate.is_internal and not candidate.is_portal:
            return self.external_links_remaining > 0
        return True

    def consume_link(self, candidate: LinkCandidate) -> None:
        self.links_remaining -= 1
        if not candidate.is_internal and not candidate.is_portal:
            self.external_links_remaining -= 1

    def timed_out(self) -> bool:
        return _time.monotonic() >= self.deadline_monotonic


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


# Type aliases for the injected callables — keeps the explorer testable
# without dragging in Playwright or the L1 fetcher at import time.
ApiExtractor = Callable[
    [dict[str, Any], dict[str, Any], str],
    Awaitable[tuple[list[dict[str, Any]], dict[str, Any] | None, bool, dict[str, Any] | None]],
]
DomExtractor = Callable[
    [str, str, dict[str, Any], str],
    Awaitable[tuple[list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None]],
]
LinkFetcher = Callable[
    [str, str],
    Awaitable[tuple[str | None, list[dict[str, Any]]]],
]


async def explore_universe(
    universe: SiteUniverse,
    property_context: dict[str, Any],
    property_id: str,
    *,
    api_extractor: ApiExtractor,
    dom_extractor: DomExtractor,
    link_fetcher: LinkFetcher | None = None,
    entry_html: str | None = None,
    profile: Any | None = None,
    threshold: float | None = None,
) -> ExplorationOutcome:
    """Run the full gather → rank → explore → quality → rerank loop.

    Parameters
    ----------
    universe
        The pre-built SiteUniverse for the entry page.
    property_context
        Standard property-context dict passed to all LLM extractors.
    property_id
        Canonical id, used for interaction records.
    api_extractor / dom_extractor
        The per-item LLM helpers — injected so tests don't need to mock
        the LLM provider. In production these are
        ``analyze_api_with_llm`` and ``analyze_dom_with_llm``.
    link_fetcher
        Optional async callable ``(sub_url, parent_url) -> (html, api_responses)``
        used to fetch internal links into a sub-universe. When None, the
        explorer skips link exploration (useful for fetch-only adapter
        contexts that can't run a second fetch).
    entry_html
        The entry page HTML — used for the initial DOM extraction step
        and as the source for sub-universe DOM chunks.
    profile
        Optional ScrapeProfile. Forwarded to sub-universe gather calls so
        blocked endpoints stay blocked across the loop.
    threshold
        Override for HIGH_CONFIDENCE_THRESHOLD (used by tests and the
        experiment harness; default reads the env-configured constant).
    """
    outcome = ExplorationOutcome()
    cutoff = threshold if threshold is not None else HIGH_CONFIDENCE_THRESHOLD
    budget = ExplorationBudget.fresh()

    # Debug snapshot — pre-rank universe (only populated when DEBUG is on).
    if DEBUG:
        outcome.debug["pre_rank_universe"] = _snapshot_universe(universe)
        outcome.debug["thresholds"] = {
            "high_confidence": cutoff,
            "external_floor": EXTERNAL_LINK_CONFIDENCE_FLOOR,
            "quality_min": QUALITY_MIN_THRESHOLD,
            "api_weight_boost": API_WEIGHT_BOOST,
        }
        log.info(
            "[universe_debug %s] pre-rank: %d apis, %d internal links, %d external links",
            property_id,
            len(universe.api_candidates),
            len(universe.internal_links),
            len(universe.external_links),
        )

    # ── Step 1: rank the universe ─────────────────────────────────────
    if not budget.can_call_llm():
        outcome.stop_reason = "no_llm_budget_at_start"
        return outcome

    budget.consume_llm()
    ranked, interaction = await rank_universe(
        universe=universe,
        property_context=property_context,
        property_id=property_id,
    )
    outcome.llm_calls_made += 1
    extra_chunk_interactions: list[dict[str, Any]] = []
    if interaction is not None:
        outcome.llm_interactions.append(interaction)
        # rank_universe runs N chunked LLM calls but only returns the first
        # interaction record (back-compat). Pull any extra-chunk records
        # off so cost ledger + debug both reflect the true call count.
        extras = interaction.pop("_extra_chunk_interactions", None) or []
        for extra in extras:
            outcome.llm_interactions.append(extra)
            outcome.llm_calls_made += 1
            extra_chunk_interactions.append(extra)
        if DEBUG:
            outcome.debug.setdefault("llm_calls", []).append(
                _capture_interaction(interaction, role="universe_rank_chunk_1")
            )
            for i, extra in enumerate(extra_chunk_interactions, start=2):
                outcome.debug["llm_calls"].append(
                    _capture_interaction(extra, role=f"universe_rank_chunk_{i}")
                )
            log.info(
                "[universe_debug %s] rank LLM: chunks=%d ranked=%d items first_chunk_raw_chars=%d",
                property_id,
                1 + len(extra_chunk_interactions),
                len(ranked),
                len(str(interaction.get("raw_response") or "")),
            )
    apply_llm_scores(universe, ranked)
    if DEBUG:
        outcome.debug["llm_ranking"] = list(ranked)
        outcome.debug["post_rank_universe"] = _snapshot_universe(universe)
    _log_attempt(outcome, "universe:rank", "ran_units" if ranked else "ran_empty",
                 reason=f"{len(ranked)} candidates scored")

    # Seed the seen-URLs set from the parent universe so sub-fetches
    # (which always re-capture global noise like cookielaw / analytics)
    # don't ever get a chance to send those same URLs to the LLM again.
    for c in universe.all_candidates():
        outcome.seen_urls.add(c.url)

    queue = _build_queue(universe)
    if DEBUG:
        outcome.debug["initial_queue"] = [
            {
                "url": c.url,
                "kind": _kind_of(c),
                "effective_score": _effective_score(c),
                "llm_score": c.llm_score,
                "base_score": c.base_score,
            }
            for c in queue
        ]
        log.info(
            "[universe_debug %s] queue (top 10):\n%s",
            property_id,
            "\n".join(
                f"  {i+1:>2}. score={_effective_score(c):.3f}  kind={_kind_of(c):<14}  {c.url}"
                for i, c in enumerate(queue[:10])
            ),
        )
    if not queue:
        outcome.stop_reason = "queue_empty_after_rank"
        outcome.quality = aggregate_quality(outcome.units, threshold=QUALITY_MIN_THRESHOLD)
        return outcome

    # ── Step 2: exploration loop ──────────────────────────────────────
    rerank_attempted = False
    while queue:
        if budget.timed_out():
            outcome.stop_reason = "wall_clock_exhausted"
            break

        candidate = queue.pop(0)
        score = _effective_score(candidate)

        # External-link guard — never explore unless the score clears the
        # extra-strict floor (spec §13.2).
        if (
            isinstance(candidate, LinkCandidate)
            and not candidate.is_internal
            and not candidate.is_portal
            and score < EXTERNAL_LINK_CONFIDENCE_FLOOR
        ):
            continue

        # Once we have a passable extraction, stop chasing low-confidence
        # candidates. Below-threshold items only run while we still have
        # nothing useful.
        if score < cutoff and outcome.units:
            outcome.stop_reason = f"score_below_cutoff_with_units (score={score:.2f}, cutoff={cutoff:.2f})"
            break

        if not budget.can_call_llm():
            outcome.stop_reason = "llm_budget_exhausted"
            break

        if isinstance(candidate, ApiCandidate):
            await _explore_api(
                candidate, outcome, budget, property_context, property_id, api_extractor,
                quality_check=lambda: _quality_met(outcome, cutoff),
            )
        elif isinstance(candidate, LinkCandidate):
            if link_fetcher is None:
                continue  # explorer running in fetch-only mode (no sub-fetch capability)
            if not budget.can_explore_link(candidate):
                continue
            budget.consume_link(candidate)
            # Self-contained sub-exploration. The sub-universe has its
            # own queue capped at SUB_UNIVERSE_TOP_K — control returns
            # here once it's done so the quality gate below decides
            # whether to pop the next item from the parent queue.
            await _explore_link(
                candidate,
                outcome,
                budget,
                property_context,
                property_id,
                api_extractor,
                dom_extractor,
                link_fetcher,
                profile,
                quality_check=lambda: _quality_met(outcome, cutoff),
            )

        # ── Step 3: quality gate ──────────────────────────────────
        outcome.quality = aggregate_quality(outcome.units, threshold=QUALITY_MIN_THRESHOLD)
        verdict = outcome.quality
        if verdict.minimum_met and verdict.important_met and verdict.confidence >= cutoff:
            outcome.stop_reason = "quality_met"
            break

        # ── Step 4: availability re-rank ──────────────────────────
        if (
            verdict.minimum_met
            and not verdict.important_met
            and not rerank_attempted
            and budget.can_call_llm()
            and any(not c.explored for c in universe.all_candidates())
        ):
            rerank_attempted = True
            outcome.rerank_fired = True
            budget.consume_llm()
            re_ranked, re_interaction = await rerank_for_availability(
                universe=universe,
                units_so_far=outcome.units,
                property_context=property_context,
                source_url=outcome.winning_url or universe.source_page_url,
                property_id=property_id,
            )
            outcome.llm_calls_made += 1
            if re_interaction is not None:
                outcome.llm_interactions.append(re_interaction)
                if DEBUG:
                    outcome.debug.setdefault("llm_calls", []).append(
                        _capture_interaction(re_interaction, role="universe_rerank")
                    )
            if DEBUG:
                outcome.debug.setdefault("rerank_outputs", []).append(list(re_ranked))
            apply_llm_scores(universe, re_ranked)
            _log_attempt(
                outcome,
                "universe:rerank_availability",
                "ran_units" if re_ranked else "ran_empty",
                reason=f"{len(re_ranked)} candidates rescored",
            )
            queue = _rebuild_queue_from_unexplored(universe)

    if not outcome.stop_reason:
        outcome.stop_reason = "queue_drained"

    if outcome.quality is None:
        outcome.quality = aggregate_quality(outcome.units, threshold=QUALITY_MIN_THRESHOLD)

    if DEBUG:
        outcome.debug["explored_apis"] = outcome.explored_apis
        outcome.debug["explored_links"] = outcome.explored_links
        outcome.debug["llm_calls_made"] = outcome.llm_calls_made
        outcome.debug["stop_reason"] = outcome.stop_reason
        log.info(
            "[universe_debug %s] stop=%s  apis_explored=%d  links_explored=%d  llm_calls=%d  units=%d",
            property_id,
            outcome.stop_reason,
            outcome.explored_apis,
            outcome.explored_links,
            outcome.llm_calls_made,
            len(outcome.units),
        )

    # The entry page HTML is itself a worthwhile target when we finish
    # the loop with no units. Try one DOM extraction on the page text.
    if not outcome.units and entry_html and budget.can_call_llm():
        await _extract_dom_section(
            entry_html=entry_html,
            section_url=universe.source_page_url,
            outcome=outcome,
            budget=budget,
            property_context=property_context,
            property_id=property_id,
            dom_extractor=dom_extractor,
            tier_key="universe:entry_dom_fallback",
        )
        outcome.quality = aggregate_quality(outcome.units, threshold=QUALITY_MIN_THRESHOLD)

    return outcome


# ---------------------------------------------------------------------------
# Queue construction
# ---------------------------------------------------------------------------


def _effective_score(candidate: Any) -> float:
    """Per-candidate score used by the priority queue.

    Uses the LLM score when available; falls back to a normalised base
    score (clamped to [0, 1]) when the ranker missed an item.

    Tier ordering rules:
      - APIs with ``has_unit_signals`` get the API_WEIGHT_BOOST. These are
        the cheap-to-explore wins — the body already looks unit-shaped.
      - APIs WITHOUT unit signals do NOT get the API boost. A Floor-Plans
        link with anchor score 200 (effective 0.6) should outrank an
        unknown JSON blob with no signature (effective 0.15) — the link
        is at least claiming to lead to floor plans, the blob isn't
        claiming anything.
    """
    if candidate.llm_score is not None:
        score = float(candidate.llm_score)
    else:
        # Map heuristic base score (~0..200) into [0, 1]; cap at 0.6 so the
        # ranker's verdict always outweighs the heuristic when both exist.
        score = min(0.6, max(0.0, candidate.base_score / 200.0))
    if isinstance(candidate, ApiCandidate) and candidate.has_unit_signals:
        score = min(1.0, score + API_WEIGHT_BOOST)
    return score


def _build_queue(universe: SiteUniverse) -> list[Any]:
    """Build the parent-universe queue with API-first policy.

    Two-phase ordering:
      Phase 1 — every API whose effective_score >= API_FIRST_THRESHOLD,
                sorted by score. These are the cheap-to-explore, targeted
                hits (cortland.com/get-session-data?property_id=..., etc).
      Phase 2 — everything else (low-confidence APIs + all links + all
                external links), sorted by score.

    Why: in the previous run an LLM-scored 0.80 same-domain API was
    queued behind two 0.85+ link candidates, so wall-clock exhausted on
    expensive Playwright sub-fetches before the cheap API hit ran.
    """
    candidates = list(universe.all_candidates())
    high_apis = [c for c in candidates if isinstance(c, ApiCandidate) and _effective_score(c) >= API_FIRST_THRESHOLD]
    rest = [c for c in candidates if c not in high_apis]
    high_apis.sort(key=_effective_score, reverse=True)
    rest.sort(key=_effective_score, reverse=True)
    return high_apis + rest


def _rebuild_queue_from_unexplored(universe: SiteUniverse) -> list[Any]:
    """Reorder the queue after a re-rank changed scores. Excludes anything
    we've already explored — the rerank prompt did the same on its side.
    Same API-first policy as ``_build_queue``."""
    candidates = [c for c in universe.all_candidates() if not c.explored]
    high_apis = [c for c in candidates if isinstance(c, ApiCandidate) and _effective_score(c) >= API_FIRST_THRESHOLD]
    rest = [c for c in candidates if c not in high_apis]
    high_apis.sort(key=_effective_score, reverse=True)
    rest.sort(key=_effective_score, reverse=True)
    return high_apis + rest


# ---------------------------------------------------------------------------
# Per-candidate handlers
# ---------------------------------------------------------------------------


def _quality_met(outcome: ExplorationOutcome, cutoff: float) -> bool:
    """Mid-loop quality check used by the per-candidate handlers.

    Re-runs the aggregate_quality scoring against the units accumulated so
    far. Returns True when the explorer can stop — both minimum AND
    important fields satisfied, and aggregate confidence clears the
    cutoff. Used to bail out of multi-chunk extractions and sub-link loops
    AS SOON AS we have what we need, instead of paying for the full
    candidate's call sequence and only checking afterwards.
    """
    q = aggregate_quality(outcome.units, threshold=QUALITY_MIN_THRESHOLD)
    outcome.quality = q
    return q.minimum_met and q.important_met and q.confidence >= cutoff


async def _explore_api(
    candidate: ApiCandidate,
    outcome: ExplorationOutcome,
    budget: ExplorationBudget,
    property_context: dict[str, Any],
    property_id: str,
    api_extractor: ApiExtractor,
    quality_check: Callable[[], bool] | None = None,
) -> None:
    """Run targeted LLM extraction on one API candidate.

    Large bodies are chunked along the unit-array dimension when possible
    (preserves item structure for the LLM), falling back to byte-window
    chunks. Each chunk consumes one LLM call against the budget.

    ``quality_check`` (option F): callable invoked after each chunk. If it
    returns True we stop processing further chunks for this candidate —
    no point running 5 more chunks when we already have what we need.
    """
    candidate.explored = True
    outcome.explored_apis += 1
    outcome.seen_urls.add(candidate.url)

    chunks = _chunk_api_body(candidate)
    if not chunks:
        return

    new_units_total: list[dict[str, Any]] = []
    for chunk_body in chunks:
        if not budget.can_call_llm():
            break
        budget.consume_llm()
        outcome.llm_calls_made += 1
        try:
            units, mapping, is_noise, interaction = await api_extractor(
                {"url": candidate.url, "body": chunk_body},
                property_context,
                property_id,
            )
        except Exception as exc:  # never let a single LLM call break the loop
            outcome.error = f"api_extractor_error: {exc}"
            log.warning("API extractor raised on %s: %s", candidate.url, exc)
            continue
        if interaction is not None:
            outcome.llm_interactions.append(interaction)
            if DEBUG:
                outcome.debug.setdefault("llm_calls", []).append(
                    _capture_interaction(
                        interaction,
                        role="universe_api_extract",
                        target_url=candidate.url,
                    )
                )
        if is_noise:
            outcome.llm_analysis_results[candidate.url] = "noise:no_unit_data"
            continue
        if units:
            new_units_total.extend(units)
            if mapping:
                outcome.llm_field_mappings.append(mapping)
                outcome.llm_analysis_results[candidate.url] = mapping
            # Merge incrementally so the quality check sees up-to-date units.
            outcome.units = merge_units(outcome.units, units)
            if outcome.winning_url is None:
                outcome.winning_url = candidate.url
            if quality_check is not None and quality_check():
                break

    if new_units_total:
        candidate.had_data = True
        candidate.units_extracted = len(new_units_total)

    _log_attempt(
        outcome,
        "universe:api",
        "ran_units" if new_units_total else "ran_empty",
        units=len(new_units_total),
        reason=f"url={_short(candidate.url)} chunks={len(chunks)}",
    )


async def _explore_link(
    candidate: LinkCandidate,
    outcome: ExplorationOutcome,
    budget: ExplorationBudget,
    property_context: dict[str, Any],
    property_id: str,
    api_extractor: ApiExtractor,
    dom_extractor: DomExtractor,
    link_fetcher: LinkFetcher,
    profile: Any | None,
    quality_check: Callable[[], bool] | None = None,
) -> None:
    """Fetch a sub-page and run a self-contained sub-exploration.

    Decision tree is sequential — the sub-universe gets its OWN ranking
    + queue + extraction; the parent queue is NOT mutated. Caller waits
    for this to return, then checks the quality gate, then decides
    whether to pop the next item from the parent queue.

    Sub-universe is capped at ``SUB_UNIVERSE_TOP_K`` items (default 2)
    so a junky portal page can't drain the per-property budget. Captured
    APIs with unit-shaped signals always preempt the inline DOM call —
    SPA portals (RealPage, Entrata) carry their unit data via XHR, not
    in the static HTML.
    """
    candidate.explored = True
    outcome.explored_links += 1

    try:
        sub_html, sub_api_responses = await link_fetcher(candidate.url, candidate.url)
    except Exception as exc:
        outcome.error = f"link_fetcher_error: {exc}"
        outcome.explored_links_map[candidate.url] = False
        log.warning("link_fetcher raised on %s: %s", candidate.url, exc)
        return

    if sub_html is None and not sub_api_responses:
        outcome.explored_links_map[candidate.url] = False
        return

    sub_universe = gather_universe(
        html=sub_html,
        base_url=candidate.url,
        api_responses=sub_api_responses,
        profile=profile,
        preview_bytes=RANKING_PREVIEW_BYTES,
    )

    # Cross-fetch dedupe — drop sub-universe candidates whose URLs the
    # parent (or an earlier sub-fetch) already saw. Catches the common
    # case of a SPA loading global noise (cookielaw OneTrust, analytics,
    # CDN bundles) on every sub-page; without this they were eating most
    # of the per-property LLM budget on repeat-noise extraction calls.
    dedup_dropped = 0
    sub_universe.api_candidates = [
        c for c in sub_universe.api_candidates if c.url not in outcome.seen_urls
    ]
    sub_universe.internal_links = [
        c for c in sub_universe.internal_links if c.url not in outcome.seen_urls
    ]
    sub_universe.external_links = [
        c for c in sub_universe.external_links if c.url not in outcome.seen_urls
    ]
    # Mark everything left as seen so the NEXT sub-fetch dedupes against it too.
    for c in sub_universe.all_candidates():
        outcome.seen_urls.add(c.url)
    dedup_dropped = (
        len(sub_api_responses)
        - len(sub_universe.api_candidates)
    )

    if DEBUG:
        outcome.debug.setdefault("sub_universes", []).append(
            {
                "parent_link": candidate.url,
                "snapshot": _snapshot_universe(sub_universe),
                "deduped_against_parent": dedup_dropped,
            }
        )
        log.info(
            "[universe_debug %s] sub-universe for %s: %d apis (deduped %d), %d internal_links, %d external_links",
            property_id,
            _short(candidate.url),
            len(sub_universe.api_candidates),
            dedup_dropped,
            len(sub_universe.internal_links),
            len(sub_universe.external_links),
        )

    # Rank the sub-universe with the LLM so the top-K decision uses real
    # confidence instead of the "biggest body wins" fallback heuristic —
    # but ONLY when the sub-universe is large enough to warrant it
    # (option A). Below SUB_UNIVERSE_RANK_MIN candidates we just trust
    # heuristic order; the JSON filter + cross-fetch dedupe have already
    # killed the cookie-consent noise that originally motivated sub-rank.
    sub_candidates_count = len(list(sub_universe.all_candidates()))
    if (
        sub_candidates_count >= SUB_UNIVERSE_RANK_MIN
        and budget.can_call_llm()
    ):
        budget.consume_llm()
        sub_ranked, sub_interaction = await rank_universe(
            universe=sub_universe,
            property_context=property_context,
            property_id=property_id,
        )
        outcome.llm_calls_made += 1
        if sub_interaction is not None:
            outcome.llm_interactions.append(sub_interaction)
            if DEBUG:
                outcome.debug.setdefault("llm_calls", []).append(
                    _capture_interaction(
                        sub_interaction,
                        role="universe_sub_rank",
                        target_url=candidate.url,
                    )
                )
            extras = sub_interaction.pop("_extra_chunk_interactions", None) or []
            for extra in extras:
                outcome.llm_interactions.append(extra)
                outcome.llm_calls_made += 1
                if DEBUG:
                    outcome.debug["llm_calls"].append(
                        _capture_interaction(
                            extra,
                            role="universe_sub_rank_extra_chunk",
                            target_url=candidate.url,
                        )
                    )
        apply_llm_scores(sub_universe, sub_ranked)

    # Build the sub-queue from the (now LLM-scored) sub-universe. Top-K
    # picked by effective_score, which prefers LLM scores when present
    # and falls back to base_score otherwise.
    sub_queue = _build_sub_queue(sub_universe, top_k=SUB_UNIVERSE_TOP_K)
    if DEBUG:
        outcome.debug.setdefault("sub_queues", []).append(
            {
                "parent_link": candidate.url,
                "queue": [
                    {
                        "url": c.url,
                        "kind": _kind_of(c),
                        "effective_score": _effective_score(c),
                        "llm_score": getattr(c, "llm_score", None),
                        "has_unit_signals": getattr(c, "has_unit_signals", False),
                    }
                    for c in sub_queue
                ],
            }
        )

    units_before = len(outcome.units)
    had_units_before_link = bool(outcome.units)  # option C — gate per-link DOM fallback
    for sub_cand in sub_queue:
        if not budget.can_call_llm():
            break
        # Option F — bail out of the sub-queue mid-loop the moment we
        # have a passing extraction. Saves the second sub-API call when
        # the first one already produced satisfying units.
        if quality_check is not None and quality_check():
            break
        if isinstance(sub_cand, ApiCandidate):
            await _explore_api(
                sub_cand, outcome, budget, property_context, property_id, api_extractor,
                quality_check=quality_check,
            )
        # We deliberately do NOT recurse into sub-links here; one level
        # deep matches the legacy link-hop budget and keeps the loop bounded.

    # Per-link DOM fallback (option C) — only fires when ALL of:
    #   - we still have zero units globally (not just zero from this link),
    #   - the sub-universe had no JSON APIs to inspect,
    #   - we have HTML to look at,
    #   - LLM budget allows.
    # Once the parent universe has produced ANY units, sub-link DOM
    # extraction is rarely additive and was responsible for ~35 of 169
    # calls in the prior batch (see compare_experiments analysis).
    extracted_anything = len(outcome.units) > units_before
    has_json_apis = bool(sub_universe.api_candidates)
    if (
        not extracted_anything
        and not had_units_before_link
        and not has_json_apis
        and sub_html
        and budget.can_call_llm()
    ):
        await _extract_dom_section(
            entry_html=sub_html,
            section_url=candidate.url,
            outcome=outcome,
            budget=budget,
            property_context=property_context,
            property_id=property_id,
            dom_extractor=dom_extractor,
            tier_key="universe:link_dom",
        )

    outcome.explored_links_map[candidate.url] = len(outcome.units) > units_before


def _build_sub_queue(sub_universe: SiteUniverse, top_k: int) -> list[Any]:
    """Sub-universe queue: top-K by effective score, APIs preferred.

    The sub-universe has been LLM-ranked by the caller, so each candidate
    carries an ``llm_score``. ``_effective_score`` prefers the LLM score
    when present (with the API_WEIGHT_BOOST for unit-signaled APIs) and
    falls back to the heuristic base_score when the ranker missed an
    item. Body bytes is intentionally NOT a tiebreaker any more — large
    cookie-consent JSONs were winning on that signal alone.
    """
    candidates = list(sub_universe.api_candidates)
    candidates.sort(key=_effective_score, reverse=True)
    return candidates[:top_k]


async def _extract_dom_section(
    entry_html: str,
    section_url: str,
    outcome: ExplorationOutcome,
    budget: ExplorationBudget,
    property_context: dict[str, Any],
    property_id: str,
    dom_extractor: DomExtractor,
    tier_key: str,
) -> None:
    """Run a single DOM extraction call on the (possibly truncated) HTML."""
    if not budget.can_call_llm():
        return
    chunk = entry_html if len(entry_html) <= LLM_DOM_CHUNK_BYTES else entry_html[:LLM_DOM_CHUNK_BYTES]
    budget.consume_llm()
    outcome.llm_calls_made += 1
    try:
        units, selectors, interaction = await dom_extractor(
            chunk, section_url, property_context, property_id
        )
    except Exception as exc:
        outcome.error = f"dom_extractor_error: {exc}"
        log.warning("DOM extractor raised on %s: %s", section_url, exc)
        return
    if interaction is not None:
        outcome.llm_interactions.append(interaction)
        if DEBUG:
            outcome.debug.setdefault("llm_calls", []).append(
                _capture_interaction(
                    interaction,
                    role="universe_dom_extract",
                    target_url=section_url,
                )
            )
    if units:
        outcome.units = merge_units(outcome.units, units)
        if outcome.winning_url is None:
            outcome.winning_url = section_url
    if selectors:
        outcome.llm_analysis_results.setdefault("_dom_selectors", []).append(
            {"url": section_url, "selectors": selectors}
        )
    _log_attempt(
        outcome,
        tier_key,
        "ran_units" if units else "ran_empty",
        units=len(units) if units else 0,
        reason=f"url={_short(section_url)}",
    )


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def _chunk_api_body(candidate: ApiCandidate) -> list[Any]:
    """Split a large API body into ranker-sized chunks.

    Strategy:
      - Body small enough → return as a single chunk.
      - Body has a list-of-dicts at top level or one level down → split
        along that list, K items per chunk.
      - Otherwise → byte-slice the stringified preview with overlap.

    Returns chunk *bodies* (not wrapped responses). Caller wraps each
    in ``{"url", "body"}`` for the extractor.
    """
    body = candidate.body
    if body is None:
        return []
    if candidate.body_bytes <= LLM_API_CHUNK_BYTES:
        return [body]

    # Try list-aware splitting.
    items = _find_top_level_list(body)
    if items is not None and len(items) > 1:
        # Roughly target one chunk's worth of items per slice.
        per_chunk = max(1, len(items) * LLM_API_CHUNK_BYTES // max(1, candidate.body_bytes))
        chunks: list[Any] = []
        for i in range(0, len(items), per_chunk):
            sliced = items[i : i + per_chunk]
            if isinstance(body, dict):
                # Keep envelope keys but replace the list field with the slice.
                envelope_key = _find_envelope_key(body, items)
                if envelope_key:
                    chunk_dict = dict(body)
                    chunk_dict[envelope_key] = sliced
                    chunks.append(chunk_dict)
                else:
                    chunks.append(sliced)
            else:
                chunks.append(sliced)
        return chunks

    # Fallback: byte-window the preview. Crude but bounded.
    text = candidate.preview or ""
    if not text:
        return [body]
    overlap = 2_000
    step = max(1, LLM_API_CHUNK_BYTES - overlap)
    return [text[i : i + LLM_API_CHUNK_BYTES] for i in range(0, len(text), step)]


def _find_top_level_list(body: Any) -> list[Any] | None:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for v in body.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
            if isinstance(v, dict):
                for vv in v.values():
                    if isinstance(vv, list) and vv and isinstance(vv[0], dict):
                        return vv
    return None


def _find_envelope_key(body: dict[str, Any], target_list: list[Any]) -> str | None:
    """Locate which top-level key on a dict body holds ``target_list``.

    Used so chunked output retains the envelope keys (``data``, ``results``,
    etc.) — keeps the LLM's existing ``response_envelope`` mapping valid.
    """
    for k, v in body.items():
        if v is target_list:
            return k
    return None


# ---------------------------------------------------------------------------
# Tier-attempt logging — same shape as GenericAdapter so the run report
# treats explorer events identically to legacy events.
# ---------------------------------------------------------------------------


def _log_attempt(
    outcome: ExplorationOutcome,
    key: str,
    outcome_label: str,
    *,
    units: int = 0,
    reason: str = "",
) -> None:
    outcome.tier_attempts.append(
        {
            "tier_key": key,
            "outcome": outcome_label,
            "units_found": units,
            "reason": reason,
            "duration_ms": 0,
        }
    )


def _short(url: str, cap: int = 80) -> str:
    return url if len(url) <= cap else url[:cap] + "..."


# ---------------------------------------------------------------------------
# Debug helpers — only invoked when JUGNU_UNIVERSE_DEBUG is on
# ---------------------------------------------------------------------------


def _kind_of(candidate: Any) -> str:
    if isinstance(candidate, ApiCandidate):
        return "api"
    if isinstance(candidate, LinkCandidate):
        if not candidate.is_internal and not candidate.is_portal:
            return "external_link"
        return "internal_link"
    return "unknown"


def _snapshot_universe(universe: SiteUniverse) -> dict[str, Any]:
    """Return a JSON-safe view of the universe for debug logging.

    Drops the heavy ``body`` field on API candidates (preserved as
    ``preview`` already) so the dump stays small enough to embed in
    per-property JSON without blowing it up to multi-MB."""
    return {
        "source_page_url": universe.source_page_url,
        "blocked_api_count": universe.blocked_api_count,
        "skipped_link_count": universe.skipped_link_count,
        "api_candidates": [
            {
                "url": c.url,
                "base_score": c.base_score,
                "has_unit_signals": c.has_unit_signals,
                "body_bytes": c.body_bytes,
                "preview_first_300": (c.preview or "")[:300],
                "llm_score": c.llm_score,
            }
            for c in universe.api_candidates
        ],
        "internal_links": [
            {
                "url": c.url,
                "anchor": c.anchor,
                "base_score": c.base_score,
                "is_portal": c.is_portal,
                "llm_score": c.llm_score,
            }
            for c in universe.internal_links
        ],
        "external_links": [
            {
                "url": c.url,
                "anchor": c.anchor,
                "base_score": c.base_score,
                "is_portal": c.is_portal,
                "llm_score": c.llm_score,
            }
            for c in universe.external_links
        ],
    }


def _capture_interaction(
    interaction: dict[str, Any],
    *,
    role: str,
    target_url: str | None = None,
) -> dict[str, Any]:
    """Pull the prompt + raw response off an LLM interaction record.

    The existing ``make_interaction`` helper truncates ``user_prompt`` to
    500 chars before storing — we keep that truncated form (full prompt
    would multiply the per-property dump size by 10×). Raw response stays
    full so we can see exactly what the model returned and why a parser
    might have rejected it."""
    return {
        "role": role,
        "target_url": target_url,
        "tier": interaction.get("tier"),
        "provider": interaction.get("provider"),
        "model": interaction.get("model"),
        "tokens_input": interaction.get("tokens_input"),
        "tokens_output": interaction.get("tokens_output"),
        "latency_ms": interaction.get("latency_ms"),
        "cost_usd": interaction.get("cost_usd"),
        "success": interaction.get("success"),
        "error": interaction.get("error"),
        "system_prompt": interaction.get("system_prompt"),
        "user_prompt_truncated_500": interaction.get("user_prompt"),
        "raw_response": interaction.get("raw_response"),
    }
