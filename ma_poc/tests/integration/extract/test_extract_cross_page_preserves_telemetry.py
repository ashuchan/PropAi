"""Phase 9 — TIER_MERGED_CROSS_PAGE must preserve self-learning telemetry.

Background — when the cross-page merger combined main + sub-page
extractions, the inline telemetry-copy loop only handled list-typed
keys. Dict-typed self-learning keys (``_llm_analysis_results``,
``_llm_hints``, ``_explored_links``) were silently dropped on every
TIER_MERGED_CROSS_PAGE win. This was 63/250 properties in shard_0 of the
2026-05-08 prod run — every one of those properties paid LLM tax to
discover mappings, then forfeited the persistence step.

The fix lifts the merge rules into ``_merge_post_hop_telemetry`` with
explicit list and dict key tables so the next add-a-key change is a
one-liner and so this regression is testable in isolation.
"""

from __future__ import annotations

from typing import Any

# scraper.py imports cleanly without a Playwright runtime — we're only
# touching the merge helper, no browser needed.
from ma_poc.pms.scraper import (
    _MERGE_DICT_KEYS,
    _MERGE_LIST_KEYS,
    _merge_post_hop_telemetry,
)


def test_list_telemetry_concatenates_main_then_sub() -> None:
    """List keys preserve arrival order (main first). Cost-ledger
    consumers walk ``_llm_interactions`` in order, so reordering would
    misattribute spend across the main vs sub-page hop."""
    result: dict[str, Any] = {
        "_llm_interactions": [{"id": "main-1"}, {"id": "main-2"}],
        "_raw_api_responses": [{"url": "main"}],
    }
    hop: dict[str, Any] = {
        "_llm_interactions": [{"id": "sub-1"}],
        "_raw_api_responses": [{"url": "sub"}],
    }
    _merge_post_hop_telemetry(result, hop)
    assert [i["id"] for i in result["_llm_interactions"]] == ["main-1", "main-2", "sub-1"]
    assert [r["url"] for r in result["_raw_api_responses"]] == ["main", "sub"]


def test_llm_analysis_results_dict_merges_sub_wins_on_collision() -> None:
    """``_llm_analysis_results`` is the input ``profile_updater`` reads to
    persist ``LlmFieldMapping`` and update ``blocked_endpoints``. Before
    the fix, the entire dict was dropped on TIER_MERGED_CROSS_PAGE wins
    — every mapping the link-hop discovered was forfeited.

    Sub-page wins on collision because the link-hop is the path the
    merger ultimately kept; if main mis-classified an endpoint as noise
    while sub-page resolved it as a real mapping, sub-page is right."""
    result: dict[str, Any] = {
        "_llm_analysis_results": {
            "https://main.com/api/a": {"api_url_pattern": "/api/a", "json_paths": {"unit_id": "id"}},
            "https://main.com/api/b": "noise:no_unit_data",
        },
    }
    hop: dict[str, Any] = {
        "_llm_analysis_results": {
            "https://sub.com/floor-plans/api": {
                "api_url_pattern": "/floor-plans/api",
                "json_paths": {"unit_id": "uid"},
            },
            # Same URL as main but reclassified — sub-page wins.
            "https://main.com/api/b": {
                "api_url_pattern": "/api/b",
                "json_paths": {"unit_id": "id"},
            },
        },
    }
    _merge_post_hop_telemetry(result, hop)
    merged = result["_llm_analysis_results"]
    assert "https://main.com/api/a" in merged
    assert "https://sub.com/floor-plans/api" in merged
    # Collision — sub-page replaced the noise classification with a real mapping.
    assert merged["https://main.com/api/b"] == {
        "api_url_pattern": "/api/b",
        "json_paths": {"unit_id": "id"},
    }


def test_llm_hints_dict_merges_sub_wins() -> None:
    """``_llm_hints`` carries CSS selectors and navigation hints. Sub-page
    selectors are the ones that worked against the rent-bearing DOM, so
    they replace any main-page guess on collision."""
    result: dict[str, Any] = {
        "_llm_hints": {"css_selectors": {"container": ".main-guess"}, "platform_guess": "rentcafe"},
    }
    hop: dict[str, Any] = {
        "_llm_hints": {"css_selectors": {"container": ".sub-actual"}, "navigation_hint": "/floor-plans"},
    }
    _merge_post_hop_telemetry(result, hop)
    hints = result["_llm_hints"]
    # Sub-page replaces ``css_selectors`` (whole dict, since merge is shallow).
    assert hints["css_selectors"] == {"container": ".sub-actual"}
    # Main-only key survives.
    assert hints["platform_guess"] == "rentcafe"
    # Sub-only key survives.
    assert hints["navigation_hint"] == "/floor-plans"


def test_explored_links_dict_merges_additively() -> None:
    """``_explored_links`` is ``{url: had_data_bool}``. Future runs use
    this to skip URLs the profile already explored — losing it on a
    merge means re-exploring the same dead-end every day."""
    result: dict[str, Any] = {
        "_explored_links": {"https://x.com/floorplans": False},
    }
    hop: dict[str, Any] = {
        "_explored_links": {
            "https://x.com/availability": True,
            # Re-exploration in the same run upgraded "no data" → "had data".
            "https://x.com/floorplans": True,
        },
    }
    _merge_post_hop_telemetry(result, hop)
    explored = result["_explored_links"]
    assert explored["https://x.com/availability"] is True
    # Sub-page wins — the URL did have data after all.
    assert explored["https://x.com/floorplans"] is True


def test_lone_sub_dict_passes_through_when_main_absent() -> None:
    """When main never wrote a key, the sub-page value passes through
    unchanged — no None handling, no empty dict from a partial merge."""
    result: dict[str, Any] = {}
    hop: dict[str, Any] = {
        "_llm_analysis_results": {"https://x.com/api": {"api_url_pattern": "/api", "json_paths": {}}},
        "_llm_hints": {"css_selectors": {"container": ".sub"}},
        "_explored_links": {"https://x.com/p1": True},
    }
    _merge_post_hop_telemetry(result, hop)
    assert result["_llm_analysis_results"] == hop["_llm_analysis_results"]
    assert result["_llm_hints"] == hop["_llm_hints"]
    assert result["_explored_links"] == hop["_explored_links"]


def test_lone_main_dict_is_left_alone_when_sub_absent() -> None:
    """When sub-page didn't contribute a key, main's value must NOT be
    cleared. The previous inline loop wiped main values when sub had
    None, which would have dropped main-page mappings on a hop that
    found new units but no new mappings."""
    main_analysis = {
        "https://main.com/api/a": {"api_url_pattern": "/api/a", "json_paths": {"unit_id": "id"}},
    }
    result: dict[str, Any] = {"_llm_analysis_results": dict(main_analysis)}
    hop: dict[str, Any] = {}
    _merge_post_hop_telemetry(result, hop)
    assert result["_llm_analysis_results"] == main_analysis


def test_all_self_learning_keys_are_in_dict_table() -> None:
    """Pin the contract: any new self-learning telemetry key must be
    explicitly added to ``_MERGE_LIST_KEYS`` or ``_MERGE_DICT_KEYS``.
    Without this guard a future addition could silently regress to the
    pre-fix behaviour where dict-typed keys were dropped."""
    expected_dict_keys = {"_llm_analysis_results", "_llm_hints", "_explored_links"}
    expected_list_keys = {
        "_raw_api_responses",
        "_llm_interactions",
        "_llm_field_mappings",
        "_tier_attempts",
    }
    assert expected_dict_keys.issubset(set(_MERGE_DICT_KEYS))
    assert expected_list_keys.issubset(set(_MERGE_LIST_KEYS))
    # The two tables must not overlap — list-merge and dict-merge have
    # different semantics.
    assert not (set(_MERGE_DICT_KEYS) & set(_MERGE_LIST_KEYS))


def test_provenance_keys_backfilled_only_when_main_missing() -> None:
    """_winning_page_url should NOT be overwritten when main already set
    it (main might be the actual data winner with sub-page just adding
    coverage). Only back-fill when main has nothing."""
    result: dict[str, Any] = {"_winning_page_url": "https://main.com/page"}
    hop: dict[str, Any] = {"_winning_page_url": "https://sub.com/page"}
    _merge_post_hop_telemetry(result, hop)
    assert result["_winning_page_url"] == "https://main.com/page"

    # And the back-fill case.
    result2: dict[str, Any] = {}
    _merge_post_hop_telemetry(result2, hop)
    assert result2["_winning_page_url"] == "https://sub.com/page"
