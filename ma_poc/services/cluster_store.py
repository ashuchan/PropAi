"""
Cluster lookup — Phase 12.

Find HOT profiles sharing a client_account_id, aggregate their high-success
LlmFieldMappings so a COLD onboarding property can attempt them BEFORE any
LLM call.

Warm-start (arch-hardening #1, 2026-07-19)
------------------------------------------
``warm_start_profile_from_cluster`` is the missing consumer: it pulls the
aggregated mate mappings and injects them into a COLD property's in-memory
profile so the existing deterministic replay tier (``generic:profile_replay``)
attempts them ahead of any LLM call. This is what turns the per-property
learning loop into a per-CLUSTER one — a mapping a sibling paid an LLM to
discover is reused across every COLD property in the same account/template.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from ma_poc.models.scrape_profile import LlmFieldMapping, ProfileMaturity, ScrapeProfile


def find_cluster_mates(
    store,
    cluster_key: str,
    self_property_id: str,
    max_mates: int = 5,
) -> list[ScrapeProfile]:
    """Returns up to N HOT profiles with same cluster_key, excluding self."""
    if not cluster_key:
        return []
    mates: list[ScrapeProfile] = []
    iter_method = getattr(store, "iter_profiles_by_cluster_key", None)
    if iter_method is None:
        return []
    candidates: Iterable[ScrapeProfile] = iter_method(cluster_key)
    for prof in candidates:
        if prof.canonical_id == self_property_id:
            continue
        if prof.confidence.maturity != ProfileMaturity.HOT:
            continue
        mates.append(prof)
        if len(mates) >= max_mates:
            break
    return mates


def collect_top_cluster_mappings(
    mates: list[ScrapeProfile],
    min_success_count: int = 3,
) -> list[LlmFieldMapping]:
    """Aggregate mappings across mates, sort by total success_count desc.

    Dedup by api_url_pattern: if two mates have the same pattern, keep the
    one with the higher success_count.
    """
    by_pattern: dict[str, LlmFieldMapping] = {}
    for mate in mates:
        for m in mate.api_hints.llm_field_mappings:
            if m.success_count < min_success_count:
                continue
            existing = by_pattern.get(m.api_url_pattern)
            if existing is None or m.success_count > existing.success_count:
                by_pattern[m.api_url_pattern] = m
    return sorted(by_pattern.values(), key=lambda m: m.success_count, reverse=True)


def collect_modal_availability_path(mates: list[ScrapeProfile]) -> str | None:
    """Return the most common non-empty ``availability_page_path`` across mates.

    A cluster shares a template, so the availability sub-path (``/floor-plans``,
    ``/floorplans``, …) that worked for HOT siblings is a strong prior for a
    COLD property that hasn't found its own yet. Ties broken by first-seen.
    """
    counter: Counter[str] = Counter()
    for mate in mates:
        path = (mate.navigation.availability_page_path or "").strip()
        if path:
            counter[path] += 1
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def _borrow_mapping(src: LlmFieldMapping) -> LlmFieldMapping:
    """Copy a mate's mapping for injection into a COLD profile.

    Two changes vs. a straight copy, both load-bearing:

    * ``source_envelope_hash`` is CLEARED. It records the body hash of the
      response the mate learned from; two different properties never produce
      the same body, so keeping it would make the replay drift-guard
      (``generic.py`` ~L1540) treat every borrow as drift and skip it. Cleared,
      the guard is bypassed and ``apply_saved_mapping`` does the real
      validation (it must yield units from THIS property's response).
    * ``success_count`` / ``consecutive_replay_failures`` reset to 0 — the
      mapping is unproven on this property; it earns its own history from here.

    ``quality_score`` is capped at 0.9 so a property's own future-proven
    mapping outranks a borrowed one on a confidence tie.
    """
    return LlmFieldMapping(
        api_url_pattern=src.api_url_pattern,
        json_paths=dict(src.json_paths),
        response_envelope=src.response_envelope,
        discovered_at=datetime.utcnow(),
        success_count=0,
        consecutive_replay_failures=0,
        last_replayed_at=None,
        source_envelope_hash="",
        quality_score=min(0.9, src.quality_score),
    )


def warm_start_profile_from_cluster(
    profile: ScrapeProfile,
    store: Any,
    *,
    max_mates: int = 5,
    min_success_count: int = 3,
    max_borrowed: int = 8,
) -> int:
    """Seed a COLD ``profile`` in place from its HOT cluster mates.

    Borrows the mates' top-proven ``llm_field_mappings`` (deduped against the
    property's own) and, when the property has no availability path of its own,
    the cluster's modal ``availability_page_path``. Mutates ``profile`` and
    returns the number of mappings borrowed (0 = no-op).

    Guarded to be a strict no-op unless there is real onboarding value:
    only COLD profiles with a ``cluster_key`` are eligible, only HOT mates
    contribute, and a mapping already present on the profile is never
    duplicated. All borrows are independently re-validated downstream by the
    replay tier, so this can only ADD $0 attempts — never override or corrupt
    the property's own learned state.
    """
    # Eligibility: only warm-start a COLD property (never-succeeded or demoted).
    # WARM/HOT properties already carry their own winning method; leave them be.
    if profile is None or not profile.cluster_key:
        return 0
    if profile.confidence.maturity != ProfileMaturity.COLD:
        return 0

    mates = find_cluster_mates(
        store, profile.cluster_key, profile.canonical_id, max_mates=max_mates
    )
    if not mates:
        return 0

    top = collect_top_cluster_mappings(mates, min_success_count=min_success_count)
    if not top:
        # Still worth borrowing the modal availability path even with no mappings.
        _maybe_borrow_availability_path(profile, mates)
        return 0

    own_patterns = {
        m.api_url_pattern for m in profile.api_hints.llm_field_mappings
    }
    borrowed = 0
    for src in top:
        if borrowed >= max_borrowed:
            break
        if src.api_url_pattern in own_patterns:
            continue
        profile.api_hints.llm_field_mappings.append(_borrow_mapping(src))
        own_patterns.add(src.api_url_pattern)
        borrowed += 1

    _maybe_borrow_availability_path(profile, mates)
    return borrowed


def _maybe_borrow_availability_path(
    profile: ScrapeProfile, mates: list[ScrapeProfile]
) -> None:
    """Set the cluster's modal availability path only if the property lacks one.

    Never overwrites a path the property already learned (its own winning URL
    or availability path always wins).
    """
    nav = profile.navigation
    if nav.availability_page_path or nav.winning_page_url:
        return
    modal = collect_modal_availability_path(mates)
    if modal:
        nav.availability_page_path = modal
