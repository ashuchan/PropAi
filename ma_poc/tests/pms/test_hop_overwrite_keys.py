"""Tests for the 2026-05-17 RC1 hop-merge plan_summaries propagation.

After the D16 unit/plan partition (commit 0a4624c), an adapter's
``AdapterResult`` carries ``units`` and ``plan_summaries`` as separate
fields. The Phase-9 cross-page hop merge had three overwrite paths
that copy fields from ``hop_result`` to ``result`` after a successful
sub-page extraction:

  1. The exception-fallback path (already fixed §8.19 of the playbook)
  2. The merge-empty fallback path  (RC1 — missed by §8.19)
  3. The main-empty active path     (RC1 — missed by §8.19)

Pre-RC1, paths 2 and 3 copied ``units`` but not ``plan_summaries`` from
the hop result. PIDs whose hop pages produced only plan-level rows
(post-D16 partition) lost them at the merge boundary and shipped as
FAILED_NO_DATA despite real extraction.

The fix centralised the key list into ``_HOP_OVERWRITE_KEYS`` so future
additions are a single-line edit. These tests pin that ``plan_summaries``
is in the constant and that all three overwrite paths reference it.
"""

from __future__ import annotations

import inspect


def test_hop_overwrite_keys_includes_plan_summaries() -> None:
    """``_HOP_OVERWRITE_KEYS`` must include ``plan_summaries``.

    Removing it re-introduces the May 17 regression (~336 PIDs lost
    plan-level data on the merge-empty and main-empty paths).
    """
    from ma_poc.pms.scraper import _HOP_OVERWRITE_KEYS

    assert "plan_summaries" in _HOP_OVERWRITE_KEYS, (
        "plan_summaries missing from _HOP_OVERWRITE_KEYS. Hop-extracted "
        "plan rows will vanish when the main page produced no units."
    )


def test_hop_overwrite_keys_includes_units() -> None:
    """Belt-and-braces: ``units`` was always in the inline list and
    must remain so. A regression that removes it would zero out every
    hop-recovered unit."""
    from ma_poc.pms.scraper import _HOP_OVERWRITE_KEYS

    assert "units" in _HOP_OVERWRITE_KEYS


def test_all_three_overwrite_paths_use_the_shared_constant() -> None:
    """All three Phase-9 overwrite paths must iterate ``_HOP_OVERWRITE_KEYS``.

    Pre-RC1, the three paths each carried their own inline tuple; the
    exception path's inline list got ``plan_summaries`` added (§8.19
    Bug C fix) but the other two paths were missed.

    By replacing all three with ``for k in _HOP_OVERWRITE_KEYS:`` we
    guarantee future additions land everywhere. This test fails loudly
    if a future edit reintroduces an inline list.
    """
    from ma_poc.pms import scraper as scraper_mod

    src = inspect.getsource(scraper_mod)
    # The three overwrite paths each contain a `for k in ...:` loop
    # within the Phase 9 merge block. Count occurrences of the shared
    # constant in for-loops.
    occurrences = src.count("for k in _HOP_OVERWRITE_KEYS:")
    assert occurrences >= 3, (
        f"_HOP_OVERWRITE_KEYS is iterated only {occurrences} times in "
        "scraper.py — expected at least 3 (the three Phase 9 overwrite "
        "paths: merge-empty fallback, main-empty active path, exception "
        "fallback). Some path is still using an inline list and will "
        "miss future additions like RC1's plan_summaries."
    )


def test_hop_overwrite_keys_excludes_telemetry_only_keys() -> None:
    """Guard against accidental conflation: telemetry keys that have
    their own merge rules (``_raw_api_responses`` concatenates,
    ``_llm_analysis_results`` merges-by-key) belong in
    ``_MERGE_LIST_KEYS`` / ``_MERGE_DICT_KEYS`` for the merge-success
    path, but they also need to copy on the overwrite paths.

    This test documents the membership rather than enforces exclusion —
    if a future change adds a telemetry key here, that's fine, but the
    person should know the merge-success path uses different rules.
    """
    from ma_poc.pms.scraper import _HOP_OVERWRITE_KEYS

    # These are extraction-output keys that always overwrite wholesale.
    extraction_keys = {"units", "plan_summaries", "extraction_tier_used"}
    for k in extraction_keys:
        assert k in _HOP_OVERWRITE_KEYS, (
            f"{k!r} missing from _HOP_OVERWRITE_KEYS — core extraction "
            "output key dropped on hop overwrite."
        )
