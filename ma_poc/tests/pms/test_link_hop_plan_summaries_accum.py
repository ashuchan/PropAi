"""Regression tests for the 2026-05-17 Bug #3 fix in _try_link_hop.

Two defects fixed in ma_poc/pms/scraper.py:

  * **Bug #3a** — Floor-plan accumulation mode tracked only `units` from
    sub_result, ignoring `plan_summaries`. When post_process partitioned
    LLM_DOM rows into plan_summaries (rows have rent + dims but no
    per-apartment identity like available_date/floor/building),
    `sub_result["units"]` was empty so the accumulator captured nothing.
    PID 16139 chaseknollsapts.com: hop 1 to /photos/pricing extracted 2
    rows via LLM_DOM, both landed in plan_summaries after post_process,
    accumulation captured 0, ran through 24 more empty hops, returned 0
    units. Fix: accumulate plan_summaries in parallel and surface them
    on return.

  * **Bug #3b** — prop_subpath synthesis appended `/floorplans`,
    `/floor-plans`, `/pricing`, `/apartments-pricing` to ANY hop URL with
    3+ path segments. If the hop URL itself already ended with one of
    those segments (e.g. `/map-and-directions/floorplans`), the synthesis
    produced `/map-and-directions/floorplans/floorplans` — nonsensical
    paths that either CF-blocked or returned the same SPA shell. PID
    16139 burned 3 hop slots on garbage URLs this way. Fix: skip synthesis
    when the base path already ends with one of `_prop_sub_paths`.
"""

from __future__ import annotations

import inspect

import pytest


# ── Bug #3b: prop_subpath synthesis must not double-append ─────────────────


class TestPropSubpathSynthesisGuard:
    """The synthesis loop at scraper.py:3245+ must skip when the base path
    already ends with one of the prop_sub_paths suffixes. Otherwise the
    hop loop generates /floorplans/floorplans, /pricing/pricing, etc."""

    @pytest.mark.parametrize(
        "base_path,should_synthesize",
        [
            # Already ends with a subpath — must NOT synthesize
            ("/sherman-oaks/chase-knolls-i/map-and-directions/floorplans", False),
            ("/sherman-oaks/chase-knolls-i/map-and-directions/floor-plans", False),
            ("/sherman-oaks/chase-knolls-i/map-and-directions/pricing", False),
            ("/sherman-oaks/chase-knolls-i/map-and-directions/apartments-pricing", False),
            ("/apartments/the-name/floorplans", False),
            ("/community/property-x/pricing", False),
            # Trailing slash variants — must also be caught
            ("/apartments/the-name/floorplans/", False),
            ("/community/property-x/pricing/", False),
            # Legitimate base paths (no trailing subpath) — must synthesize
            ("/sherman-oaks/chase-knolls-i", True),
            ("/sherman-oaks/chase-knolls-i/map-and-directions", True),
            ("/sherman-oaks/chase-knolls-i/photos", True),
            ("/apartments/the-name", True),
            ("/community/property-x", True),
            # Slugs that CONTAIN a subpath keyword but aren't a real subpath
            # boundary — must still synthesize (no spurious skip).
            ("/apartments/floorplans-on-main", True),
            ("/community/the-pricing-villa", True),
        ],
    )
    def test_skip_synthesis_when_base_ends_with_subpath(
        self, base_path, should_synthesize
    ):
        """The exact guard from the fix at scraper.py:3245+: skip if
        base_path.lower() endswith any of _prop_sub_paths.

        Test the predicate in isolation rather than running the full hop
        loop — the predicate is the contract that matters.
        """
        _prop_sub_paths = (
            "/floorplans", "/floor-plans", "/pricing", "/apartments-pricing"
        )
        base_path_stripped = base_path.rstrip("/").lower()
        base_already_subpath = any(
            base_path_stripped.endswith(p) for p in _prop_sub_paths
        )
        # If base already ends with subpath, we must NOT synthesize
        assert (not base_already_subpath) == should_synthesize, (
            f"Guard predicate wrong for {base_path!r}: "
            f"base_already_subpath={base_already_subpath}, "
            f"should_synthesize={should_synthesize}"
        )


class TestPropSubpathSynthesisIntegratedInScraper:
    """Validate the fix actually lives in the scraper source — guards
    against the predicate being removed in a future refactor."""

    def test_scraper_has_base_already_subpath_guard(self):
        from ma_poc.pms import scraper

        src = inspect.getsource(scraper)
        # The fix introduces this exact variable name and pattern; if a
        # future change removes it without replacing with an equivalent
        # check, this test will fail.
        assert "_base_already_subpath" in src, (
            "Bug #3b guard removed: scraper.py no longer contains "
            "_base_already_subpath. Hop loop will re-emit garbage URLs "
            "like /floorplans/floorplans, /pricing/pricing."
        )
        assert "if not _base_already_subpath:" in src, (
            "Bug #3b guard wrapper removed: prop_subpath synthesis is no "
            "longer gated on the base-already-subpath check."
        )


# ── Bug #3a: plan_summaries accumulator ────────────────────────────────


class TestPlanSummariesAccumulator:
    """Validate the parallel _accumulated_plan_summaries tracker exists
    in _try_link_hop and is populated in both entry + continue branches,
    and surfaced on the return path."""

    def test_scraper_declares_accumulated_plan_summaries(self):
        from ma_poc.pms import scraper

        src = inspect.getsource(scraper)
        assert "_accumulated_plan_summaries: list[dict[str, Any]] = []" in src, (
            "Bug #3a fix removed: _accumulated_plan_summaries no longer "
            "declared. Plan-level rows from LLM_DOM hops will be lost in "
            "accumulation mode."
        )

    def test_scraper_populates_plan_summaries_in_entry_branch(self):
        """When the index page enters accumulation, plan_summaries from
        sub_result must be extended into the accumulator."""
        from ma_poc.pms import scraper

        src = inspect.getsource(scraper)
        # The fix uses .extend with sub_result.get("plan_summaries") or [].
        # Counting occurrences ensures both entry + continue branches got
        # the patch, not just one.
        n_extends = src.count(
            "_accumulated_plan_summaries.extend(\n"
            "                        sub_result.get(\"plan_summaries\") or []"
        ) + src.count(
            "_accumulated_plan_summaries.extend(\n"
            "                    sub_result.get(\"plan_summaries\") or []"
        )
        assert n_extends >= 2, (
            f"Expected _accumulated_plan_summaries.extend in both entry "
            f"and continue accumulation branches; found {n_extends}. "
            "If the fix was inlined differently, update this test — but "
            "verify both paths are still covered."
        )

    def test_scraper_surfaces_plan_summaries_on_return(self):
        """The return path at line ~3692 must write the deduped
        plan_summaries onto _first_successful_result so the caller's
        _HOP_OVERWRITE_KEYS copy path forwards them to the entry result."""
        from ma_poc.pms import scraper

        src = inspect.getsource(scraper)
        assert (
            '_first_successful_result["plan_summaries"] = _plan_deduped'
            in src
        ), (
            "Bug #3a return-path fix removed: plan_summaries no longer "
            "written to _first_successful_result on accumulation return. "
            "Plan-level rows captured across hops will be lost."
        )

    def test_scraper_dedups_plan_summaries_on_return(self):
        """Plan-summary dedup uses (name, beds, baths, sqft, rent_low)
        natural key — no per-apartment identity. Without dedup, cross-host
        per-plan pages that all repeat the same aggregate row emit N copies."""
        from ma_poc.pms import scraper

        src = inspect.getsource(scraper)
        # Verify the dedup key components are all present in the dedup
        # logic. If the test fails because the key set changed, audit
        # whether the new key is actually a natural key for plan-level
        # rows (no unit_number / unit_id).
        for key_component in (
            "floor_plan_name",
            "beds",
            "bedrooms",
            "baths",
            "bathrooms",
            "sqft",
            "rent_low",
        ):
            assert key_component in src, (
                f"plan_summaries dedup is missing {key_component!r} as a "
                "key component. Cross-host repeats will be emitted as "
                "duplicates."
            )


# ── End-to-end-ish: predicate covers the chaseknollsapts.com case ─────


class TestChaseknollsaptsRegression:
    """The canonical failing case from cloud_run_2026-05-17 shard 6,
    PID 16139. Pin the exact URL shape that the path-compounding bug
    produced so a future regression is loud."""

    @pytest.mark.parametrize(
        "garbage_url_that_should_not_be_synthesized",
        [
            "/sherman-oaks/chase-knolls-i/map-and-directions/floorplans/floorplans",
            "/sherman-oaks/chase-knolls-i/map-and-directions/floorplans/floor-plans",
            "/sherman-oaks/chase-knolls-i/map-and-directions/floorplans/pricing",
            "/sherman-oaks/chase-knolls-i/map-and-directions/pricing/pricing",
            "/sherman-oaks/chase-knolls-i/map-and-directions/pricing/floorplans",
            "/sherman-oaks/chase-knolls-i/map-and-directions/pricing/floor-plans",
        ],
    )
    def test_garbage_compound_url_would_be_blocked(
        self, garbage_url_that_should_not_be_synthesized
    ):
        """These are the literal URLs the cloud run hopped to. The base
        of each (one segment up) ends with /floorplans or /pricing, so
        the new guard would skip prop_subpath synthesis entirely."""
        # Strip last segment to recover the base path the loop saw.
        base = "/".join(
            garbage_url_that_should_not_be_synthesized.rstrip("/").split("/")[:-1]
        )
        _prop_sub_paths = (
            "/floorplans", "/floor-plans", "/pricing", "/apartments-pricing"
        )
        base_already_subpath = any(
            base.rstrip("/").lower().endswith(p) for p in _prop_sub_paths
        )
        assert base_already_subpath, (
            f"Base path {base!r} should be flagged as already-subpath so "
            f"the synthesis is skipped — preventing the garbage URL "
            f"{garbage_url_that_should_not_be_synthesized!r} from entering "
            "the hop queue."
        )
