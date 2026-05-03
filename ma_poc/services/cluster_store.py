"""
Cluster lookup — Phase 12.

Find HOT profiles sharing a client_account_id, aggregate their high-success
LlmFieldMappings so a COLD onboarding property can attempt them BEFORE any
LLM call.
"""

from __future__ import annotations

from typing import Iterable

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
