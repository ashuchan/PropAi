"""Cluster/template cross-property warm-start (arch-hardening #1, 2026-07-19).

The per-property self-learning loop re-pays LLM discovery on every property in
a shared PMS client-account cluster. ``warm_start_profile_from_cluster`` closes
that gap: a COLD property borrows its HOT siblings' proven ``llm_field_mappings``
so the $0 deterministic replay tier can attempt them before any LLM call.

Pinned contract:
- Only COLD profiles with a cluster_key are eligible (WARM/HOT untouched).
- Only HOT mates contribute; a WARM sibling is ignored.
- Borrowed mappings clear ``source_envelope_hash`` (else the replay drift-guard
  skips every borrow — two properties never share a body hash) and reset
  success_count to 0 (unproven on the borrower).
- Never duplicates a mapping the property already owns.
- Modal availability path is borrowed only when the property has none.
- End-to-end through the FS ProfileStore + the runner store wrapper.
"""

from __future__ import annotations

from typing import Any

import pytest

from ma_poc.models.scrape_profile import (
    ApiHints,
    LlmFieldMapping,
    NavigationConfig,
    ProfileMaturity,
    ScrapeProfile,
)
from ma_poc.services.cluster_store import (
    collect_modal_availability_path,
    warm_start_profile_from_cluster,
)
from ma_poc.services.profile_store import ProfileStore


@pytest.fixture
def store(tmp_path: Any) -> ProfileStore:
    return ProfileStore(tmp_path / "profiles")


def _mapping(
    pattern: str, *, success: int = 5, hash_: str = "deadbeefcafe0001"
) -> LlmFieldMapping:
    return LlmFieldMapping(
        api_url_pattern=pattern,
        json_paths={"rent_low": "minRent", "unit_id": "unitNumber"},
        response_envelope="data.units",
        success_count=success,
        source_envelope_hash=hash_,
    )


def _hot_mate(
    cid: str,
    cluster_key: str,
    *,
    mappings: list[LlmFieldMapping] | None = None,
    avail_path: str | None = None,
) -> ScrapeProfile:
    p = ScrapeProfile(canonical_id=cid, cluster_key=cluster_key)
    p.confidence.maturity = ProfileMaturity.HOT
    if mappings:
        p.api_hints = ApiHints(llm_field_mappings=mappings)
    if avail_path:
        p.navigation = NavigationConfig(availability_page_path=avail_path)
    return p


def _cold(cid: str, cluster_key: str = "acct-777") -> ScrapeProfile:
    return ScrapeProfile(canonical_id=cid, cluster_key=cluster_key)


# ── happy path ────────────────────────────────────────────────────────────────


def test_cold_borrows_hot_mate_mapping(store: ProfileStore) -> None:
    mate = _hot_mate(
        "mate-1", "acct-777", mappings=[_mapping("securecafe.com/api/units")]
    )
    store.save(mate)
    cold = _cold("cold-1")
    store.save(cold)

    n = warm_start_profile_from_cluster(cold, store)

    assert n == 1
    borrowed = cold.api_hints.llm_field_mappings
    assert [m.api_url_pattern for m in borrowed] == ["securecafe.com/api/units"]
    # json_paths + envelope carried across so replay is deterministic
    assert borrowed[0].json_paths == {"rent_low": "minRent", "unit_id": "unitNumber"}
    assert borrowed[0].response_envelope == "data.units"


def test_borrowed_mapping_clears_envelope_hash(store: ProfileStore) -> None:
    """The load-bearing detail: keeping the mate's body hash would make the
    replay drift-guard skip every borrow, since no two properties share a body."""
    mate = _hot_mate(
        "mate-2", "acct-777", mappings=[_mapping("x.com/api", hash_="aaaa1111bbbb2222")]
    )
    store.save(mate)
    cold = _cold("cold-2")

    warm_start_profile_from_cluster(cold, store)

    b = cold.api_hints.llm_field_mappings[0]
    assert b.source_envelope_hash == ""
    assert b.success_count == 0
    assert b.consecutive_replay_failures == 0
    assert b.quality_score <= 0.9


# ── eligibility guards ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("maturity", [ProfileMaturity.WARM, ProfileMaturity.HOT])
def test_noop_when_not_cold(store: ProfileStore, maturity: ProfileMaturity) -> None:
    mate = _hot_mate("mate-3", "acct-777", mappings=[_mapping("x.com/api")])
    store.save(mate)
    target = _cold("target-3")
    target.confidence.maturity = maturity  # already learning on its own
    store.save(target)

    n = warm_start_profile_from_cluster(target, store)

    assert n == 0
    assert target.api_hints.llm_field_mappings == []


def test_noop_when_no_cluster_key(store: ProfileStore) -> None:
    mate = _hot_mate("mate-4", "acct-777", mappings=[_mapping("x.com/api")])
    store.save(mate)
    cold = _cold("cold-4", cluster_key="")

    assert warm_start_profile_from_cluster(cold, store) == 0
    assert cold.api_hints.llm_field_mappings == []


def test_noop_when_only_warm_mates(store: ProfileStore) -> None:
    """A sibling that hasn't hit HOT is not yet a trustworthy donor."""
    warm_sibling = ScrapeProfile(canonical_id="warm-sib", cluster_key="acct-777")
    warm_sibling.confidence.maturity = ProfileMaturity.WARM
    warm_sibling.api_hints = ApiHints(llm_field_mappings=[_mapping("x.com/api")])
    store.save(warm_sibling)
    cold = _cold("cold-5")

    assert warm_start_profile_from_cluster(cold, store) == 0


def test_low_success_mapping_not_borrowed(store: ProfileStore) -> None:
    mate = _hot_mate(
        "mate-6", "acct-777", mappings=[_mapping("x.com/api", success=1)]
    )
    store.save(mate)
    cold = _cold("cold-6")

    # min_success_count default 3 → the success=1 mapping is below the bar
    assert warm_start_profile_from_cluster(cold, store) == 0


# ── dedup + caps ────────────────────────────────────────────────────────────────


def test_does_not_duplicate_own_mapping(store: ProfileStore) -> None:
    shared = "securecafe.com/api/units"
    mate = _hot_mate("mate-7", "acct-777", mappings=[_mapping(shared)])
    store.save(mate)
    cold = _cold("cold-7")
    cold.api_hints = ApiHints(
        llm_field_mappings=[_mapping(shared, success=2)]  # property already has it
    )

    n = warm_start_profile_from_cluster(cold, store)

    assert n == 0
    assert len(cold.api_hints.llm_field_mappings) == 1


def test_respects_max_borrowed_cap(store: ProfileStore) -> None:
    many = [_mapping(f"x.com/api/{i}") for i in range(12)]
    store.save(_hot_mate("mate-8", "acct-777", mappings=many))
    cold = _cold("cold-8")

    n = warm_start_profile_from_cluster(cold, store, max_borrowed=3)

    assert n == 3
    assert len(cold.api_hints.llm_field_mappings) == 3


def test_excludes_self(store: ProfileStore) -> None:
    """A property must not borrow from itself even if HOT-in-store."""
    p = _hot_mate("solo", "acct-777", mappings=[_mapping("x.com/api")])
    store.save(p)
    cold = ScrapeProfile(canonical_id="solo", cluster_key="acct-777")  # same id, COLD

    assert warm_start_profile_from_cluster(cold, store) == 0


# ── availability path borrow ────────────────────────────────────────────────────


def test_modal_availability_path_helper() -> None:
    mates = [
        _hot_mate("a", "k", avail_path="/floor-plans"),
        _hot_mate("b", "k", avail_path="/floor-plans"),
        _hot_mate("c", "k", avail_path="/availability"),
    ]
    assert collect_modal_availability_path(mates) == "/floor-plans"
    assert collect_modal_availability_path([]) is None


def test_borrows_availability_path_when_missing(store: ProfileStore) -> None:
    store.save(
        _hot_mate(
            "mate-9",
            "acct-777",
            mappings=[_mapping("x.com/api")],
            avail_path="/floorplans",
        )
    )
    cold = _cold("cold-9")

    warm_start_profile_from_cluster(cold, store)

    assert cold.navigation.availability_page_path == "/floorplans"


def test_does_not_overwrite_own_availability_path(store: ProfileStore) -> None:
    store.save(
        _hot_mate(
            "mate-10",
            "acct-777",
            mappings=[_mapping("x.com/api")],
            avail_path="/floorplans",
        )
    )
    cold = _cold("cold-10")
    cold.navigation = NavigationConfig(winning_page_url="https://own.example/units")

    warm_start_profile_from_cluster(cold, store)

    # own winning URL present → cluster path is NOT imposed
    assert cold.navigation.availability_page_path is None


# ── end-to-end through the runner store wrapper ─────────────────────────────────


def test_through_runner_store_wrapper(tmp_path: Any) -> None:
    """The runner wraps the FS store in _SimpleProfileStore; warm-start must
    resolve iter_profiles_by_cluster_key through that wrapper (delegation)."""
    from scripts.runners.jugnu import _build_profile_store

    profiles_dir = tmp_path / "profiles"
    wrapper = _build_profile_store(profiles_dir)
    wrapper.save(
        _hot_mate("mate-11", "acct-777", mappings=[_mapping("securecafe.com/api")])
    )
    cold = _cold("cold-11")

    n = warm_start_profile_from_cluster(cold, wrapper)

    assert n == 1
    assert cold.api_hints.llm_field_mappings[0].api_url_pattern == "securecafe.com/api"
