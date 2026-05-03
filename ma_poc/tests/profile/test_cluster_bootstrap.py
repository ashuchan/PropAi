"""Phase 12 — cluster bootstrap tests."""

from __future__ import annotations

import pytest

from models.scrape_profile import (
    LlmFieldMapping,
    ProfileMaturity,
    ScrapeProfile,
)
from services.cluster_store import collect_top_cluster_mappings, find_cluster_mates
from services.profile_store import ProfileStore


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / "profiles")


def _make_hot(canonical_id: str, cluster_key: str, store: ProfileStore) -> ScrapeProfile:
    p = ScrapeProfile(canonical_id=canonical_id)
    p.cluster_key = cluster_key
    p.confidence.maturity = ProfileMaturity.HOT
    p.confidence.consecutive_successes = 5
    store.save(p)
    return p


def test_cluster_lookup_returns_only_hot_mates(store: ProfileStore) -> None:
    _make_hot("hot-1", "client-A", store)
    p_warm = ScrapeProfile(canonical_id="warm-1")
    p_warm.cluster_key = "client-A"
    p_warm.confidence.maturity = ProfileMaturity.WARM
    store.save(p_warm)
    p_cold = ScrapeProfile(canonical_id="cold-1")
    p_cold.cluster_key = "client-A"
    store.save(p_cold)

    mates = find_cluster_mates(store, "client-A", "self-1", max_mates=10)
    assert {m.canonical_id for m in mates} == {"hot-1"}


def test_cluster_excludes_self(store: ProfileStore) -> None:
    _make_hot("self", "client-A", store)
    _make_hot("other-1", "client-A", store)
    _make_hot("other-2", "client-A", store)

    mates = find_cluster_mates(store, "client-A", "self", max_mates=10)
    assert {m.canonical_id for m in mates} == {"other-1", "other-2"}


def test_cluster_no_cluster_key_returns_empty(store: ProfileStore) -> None:
    _make_hot("h", "client-A", store)
    mates = find_cluster_mates(store, "", "self", max_mates=10)
    assert mates == []


def test_cluster_max_mates_caps_results(store: ProfileStore) -> None:
    for i in range(8):
        _make_hot(f"hot-{i}", "client-A", store)
    mates = find_cluster_mates(store, "client-A", "self", max_mates=5)
    assert len(mates) == 5


def test_collect_top_cluster_mappings_dedup_by_pattern() -> None:
    p1 = ScrapeProfile(canonical_id="m1")
    p1.api_hints.llm_field_mappings = [
        LlmFieldMapping(api_url_pattern="/api/x", json_paths={"a": "b"}, success_count=5)
    ]
    p2 = ScrapeProfile(canonical_id="m2")
    p2.api_hints.llm_field_mappings = [
        LlmFieldMapping(api_url_pattern="/api/x", json_paths={"a": "b"}, success_count=8)
    ]
    p3 = ScrapeProfile(canonical_id="m3")
    p3.api_hints.llm_field_mappings = [
        LlmFieldMapping(api_url_pattern="/api/x", json_paths={"a": "b"}, success_count=3)
    ]
    out = collect_top_cluster_mappings([p1, p2, p3], min_success_count=3)
    assert len(out) == 1
    assert out[0].success_count == 8


def test_collect_top_filters_low_success_count() -> None:
    p = ScrapeProfile(canonical_id="m")
    p.api_hints.llm_field_mappings = [
        LlmFieldMapping(api_url_pattern="/api/x", json_paths={}, success_count=1),
        LlmFieldMapping(api_url_pattern="/api/y", json_paths={"a": "b"}, success_count=5),
    ]
    out = collect_top_cluster_mappings([p], min_success_count=3)
    assert len(out) == 1
    assert out[0].api_url_pattern == "/api/y"
