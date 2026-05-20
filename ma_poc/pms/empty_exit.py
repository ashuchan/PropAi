"""Empty-exit label registry — Path B Piece 1.

When a PMS adapter runs but produces no usable units, it doesn't fail
silently — it sets a structured ``tier_used`` label that tells the
orchestrator exactly why. This module is the catalog of those labels
and a predicate the orchestrator (or any caller) can use to decide
whether the property is a candidate for retry with a different PMS.

The registry is the source of truth for the Path B retry mechanism
designed at ``investigations/2026-05-20-path-b-design/DESIGN.md``.

Reading the labels
==================

Every PMS adapter emits ``tier_used`` strings shaped like
``TIER_1_API_<PMS>`` for success and ``TIER_1_API_<PMS>_<REASON>`` for
empty exits. A few adapters use one-off verdict labels for
deterministic-empty exits that don't fit that pattern (e.g.
``NOT_ENCORESKYLINE_TEMPLATE``, ``SYNDICATION_ONLY_WIX``).

Empty-exit suffixes
-------------------

Each suffix means the adapter ran far enough to know that this
property's data is not at the path/shape/identifier it expected — i.e.
*the right thing to do is route to a different PMS*, not LLM rescue.

``_EMPTY``
    Adapter completed but produced 0 units. Most common — applies when
    the API call succeeded but the response list was empty (or all
    rows failed unit-validity).

``_NO_URN`` / ``_NO_RESPONSE``
    Adapter couldn't make the API call at all because the
    identifier/URN it needs (e.g. ``g5-cl-<id>``, ``community_id``)
    wasn't on the page or no network response was captured.

``_SHAPE_REJECTED``
    Responses were captured but none matched the adapter's expected
    envelope (e.g. ``data.{units|floor_plans|sightmap_id}`` for
    SightMap, ``Data.PageData[].Floorplan`` for RentCafe).

``_PARSE_FAILED``
    Envelope matched but the join produced 0 records — typically
    field-name drift on the upstream side.

``_AMENITIES_ONLY``
    A SightMap-specific case: the response carried only the amenities
    map, no units array. Configuration choice, not a bug.

``_API_ERROR``
    The adapter's API call returned a non-2xx or threw — distinct from
    `_SHAPE_REJECTED` (which got a 2xx body) because the right
    follow-up may be a proxy escalation rather than a different PMS.

``_NO_PLAN``
    Plan-level page expected but missing — encoreskyline /
    multifamily-CMS pattern.

Verdict labels
--------------

``NOT_ENCORESKYLINE_TEMPLATE``
    encoreskyline_template adapter dispatched but the page is NOT the
    Jonah-template layout it expects (e.g. RentCafe-Securecafe backed).
    Retry with a real PMS adapter.

``ENCORESKYLINE_NO_PLAN_LINKS``
    Template layout matched but no per-plan ``/floorplans/{slug}/``
    links rendered.

``SYNDICATION_ONLY_WIX`` / ``SYNDICATION_ONLY_SQUARESPACE``
    The no-PMS adapter (wix_nopms / squarespace_nopms) couldn't find a
    real PMS portal hop. Today's in-adapter recoveries
    (``recover_appfolio_embed``, ``recover_pms_portal``) handle some
    of this — Path B can pick up the rest by re-dispatching.

Excluded — these are SUCCESS, not empty exit
============================================

- ``TIER_1_API`` (bare; generic adapter Tier-1 win)
- ``TIER_1_API_<PMS>`` (bare; PMS-specific Tier-1 win)
- ``TIER_2_JSONLD``, ``TIER_3_DOM``, ``TIER_4_LLM*``
- ``TIER_MERGED_*``, ``TIER_1_DOM_*_SSR``
- The various ``_IFRAME`` / ``_INLINE`` / ``_SPACES_SSR`` / ``_RAZZ`` /
  ``_ZRS`` suffixes — these are *successful* sub-paths within an
  adapter, not empty exits.

LLM-tier failures (``TIER_4_LLM_API_EMPTY`` and similar) are also
excluded — the LLM tier IS already the orchestrator's last resort;
retrying from there has nowhere to go.
"""

from __future__ import annotations

# ----- Configuration: the registry itself -----

# Suffixes that signal an empty exit. Matched as a *suffix* of the
# `tier_used` string, e.g. ``TIER_1_API_G5_EMPTY`` → ``_EMPTY``.
_EMPTY_EXIT_SUFFIXES: frozenset[str] = frozenset(
    {
        "_EMPTY",
        "_NO_URN",
        "_NO_RESPONSE",
        "_SHAPE_REJECTED",
        "_PARSE_FAILED",
        "_AMENITIES_ONLY",
        "_API_ERROR",
        "_NO_PLAN",
        "_NO_PLAN_LINKS",
        "_RESEARCH_BLOCKED",
    }
)

# Verbatim labels that signal an empty exit without fitting the
# suffix shape. Kept separately so the catalog is readable.
_EMPTY_EXIT_VERDICTS: frozenset[str] = frozenset(
    {
        "NOT_ENCORESKYLINE_TEMPLATE",
        "ENCORESKYLINE_NO_PLAN_LINKS",
        "SYNDICATION_ONLY_WIX",
        "SYNDICATION_ONLY_SQUARESPACE",
    }
)

# Labels that LOOK like an empty exit by suffix but are actually
# success exits within the LLM tier — Path B explicitly does NOT retry
# from these since LLM IS the last-resort tier already. Names matched
# as substrings of the tier_used string.
_LLM_TIER_PREFIXES: tuple[str, ...] = (
    "TIER_4_LLM",
)


# ----- Public API -----


def is_empty_exit(tier_used: str | None) -> bool:
    """Return True iff *tier_used* signals a Path-B-retryable empty exit.

    An empty exit means the adapter knew enough to try but produced no
    usable units. Common reasons: wrong-PMS routing, page-level URN
    not present, response envelope drift, or syndication-only shells.

    Returns False for:
      - None / empty string (no signal)
      - Successful tier labels (TIER_1_API, TIER_2_JSONLD, etc.)
      - LLM-tier failures (the LLM is already the last-resort retry)

    Parameters
    ----------
    tier_used:
        The ``tier_used`` field from an ``AdapterResult``, exactly as
        the adapter set it. ``None`` or empty string returns False.
    """
    if not tier_used:
        return False
    # LLM-tier outcomes (success or failure) are never retried from —
    # LLM is already the orchestrator's last-resort tier.
    for prefix in _LLM_TIER_PREFIXES:
        if tier_used.startswith(prefix):
            return False
    # Exact-match verdict labels.
    if tier_used in _EMPTY_EXIT_VERDICTS:
        return True
    # Suffix match on the registered empty-exit suffixes.
    for suffix in _EMPTY_EXIT_SUFFIXES:
        if tier_used.endswith(suffix):
            return True
    return False


def empty_exit_reason(tier_used: str | None) -> str | None:
    """Return the matched empty-exit suffix/verdict for telemetry.

    Returns the registered suffix or verbatim label that triggered the
    `is_empty_exit` decision, or None when `tier_used` is not an empty
    exit. Useful for the orchestrator's ``RETRY_DISPATCHED`` event
    payload so reports can split retry attempts by what triggered them.
    """
    if not tier_used:
        return None
    for prefix in _LLM_TIER_PREFIXES:
        if tier_used.startswith(prefix):
            return None
    if tier_used in _EMPTY_EXIT_VERDICTS:
        return tier_used
    for suffix in _EMPTY_EXIT_SUFFIXES:
        if tier_used.endswith(suffix):
            return suffix
    return None


__all__ = ["is_empty_exit", "empty_exit_reason"]
