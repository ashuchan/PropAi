"""Tests for the 2026-05-17 RC4 link-hop junk-URL filter fixes.

The 2026-05-17 cloud run regressed because hop candidates with
``/schedule-tour``, ``/apply``, ``/gallery`` sub-paths admitted at score
5_100 burned the per-property 600s wallclock on URLs that never carry
inventory. PID 78134 alone burned 1406s of its 2045s wallclock (68.8%)
chasing these.

Two defects, both covered here:

  * **RC4a** — the anti-signal regex was anchored at ``^/``, so it only
    fired on top-level paths. Portfolio sites under a property-slug
    prefix (``/apartments/{slug}/schedule-tour``) evaded it. Fix:
    de-anchor to ``(?:^|/)keyword(?:/|$)`` so the keyword matches as a
    path segment anywhere in the URL.

  * **RC4b** — the floor-plan accumulator composed ``/floorplans``,
    ``/floor-plans``, ``/pricing``, ``/apartments-pricing`` suffix
    variants off ANY hop URL with 3+ path segments, including junk
    base URLs. Fix: skip composition when the base URL itself has an
    anti-signal path segment.
"""

from __future__ import annotations

import inspect
import re

import pytest


# ── RC4a: anti-signal regex de-anchoring ─────────────────────────────────────


def _get_anti_signal_pattern():
    """Pull the ``-2000`` anti-signal regex out of scraper._URL_SHAPE_PATTERNS.

    The patterns are a private module constant of tuples
    ``(compiled_regex, score, label)``. We locate the entry whose label
    is ``anti_signal_non_inventory`` so the test pins behaviour by
    semantic label rather than position (the table grows over time).
    """
    from ma_poc.pms import scraper as scraper_mod

    for pat, score, label in scraper_mod._URL_SHAPE_PATTERNS:
        if label == "anti_signal_non_inventory":
            assert score == -2_000, (
                f"anti_signal_non_inventory score changed from -2000 to {score} — "
                "if intentional, update this test; otherwise the score change "
                "may regress the junk-URL filter."
            )
            return pat
    raise AssertionError(
        "anti_signal_non_inventory pattern not found in _URL_SHAPE_PATTERNS"
    )


class TestAntiSignalRegexMatchesSubPath:
    """RC4a — the de-anchored regex must match anti-signal keywords as
    path segments anywhere in the URL, not just at the path root."""

    @pytest.mark.parametrize(
        "path",
        [
            "/apartments/the-24/schedule-tour",        # PID 78134 (uncommondevelopers)
            "/apartments/the-24/schedule-tour/",
            "/apartments/the-24/schedule-tour/floorplans",
            "/apartments/the-24/schedule-tour/pricing",
            "/properties/skyline/apply",
            "/properties/skyline/apply/",
            "/properties/skyline/gallery",
            "/properties/skyline/photos",
            "/properties/skyline/amenities",
            "/properties/skyline/neighborhood",
            "/properties/skyline/contact",
            "/properties/skyline/login",
            "/properties/skyline/sign-in",
            "/properties/skyline/about",
            # Top-level paths still match (preserves the pre-RC4a contract)
            "/tour",
            "/tour/",
            "/gallery",
            "/login",
            "/sign-in",
            "/careers",
        ],
    )
    def test_anti_signal_matches_keyword_as_path_segment(self, path):
        pat = _get_anti_signal_pattern()
        assert pat.search(path), (
            f"anti-signal regex did not match {path!r} — junk URLs will be "
            "lifted to score 5_100 by the anchor+keyword floor and burn "
            "hop budget. See PID 78134 in the 2026-05-17 cloud run."
        )

    @pytest.mark.parametrize(
        "path",
        [
            # Slugs that CONTAIN an anti-signal keyword as a substring but
            # NOT as a path segment must not be penalised. The boundary
            # check ``(?:^|/)...(?:/|$)`` is what makes this distinction.
            "/apartments/applewood-pricing",        # `apply` is substring of `applewood`
            "/apartments/the-24-tour-king-blvd",    # `tour` is substring of slug
            "/apartments/the-24-tour-king-blvd/pricing",
            "/apartments/contact-grove",            # `contact` is substring of slug
            "/apartments/login-square-park",        # `login` is substring of slug
            "/floorplans/the-diplomat-1-br-1-ba",   # legitimate per-plan URL
            "/floor-plans",                          # legitimate index
            "/availability",
            "/units",
            "/pricing",
        ],
    )
    def test_anti_signal_does_not_match_substring_overlaps(self, path):
        pat = _get_anti_signal_pattern()
        assert not pat.search(path), (
            f"anti-signal regex incorrectly matched {path!r} — substring "
            "overlap should NOT trigger the penalty. The (?:^|/)...(?:/|$) "
            "path-segment boundary is the distinguishing rule."
        )


# ── RC4b: accumulator gates on base-URL shape ────────────────────────────────


def test_anti_signal_path_segment_set_present_in_accumulator():
    """The accumulator at the 'Property sub-path priors' block must
    define an ``_ANTI_SIGNAL_PATH_SEGMENTS`` set and check the hop URL's
    path segments against it before composing ``/floorplans`` etc.
    variants.

    Source-level assertion: this test pins the gate by name. A future
    edit that removes it (or renames the set without updating the check)
    fails this test. The runtime behaviour is exercised by the cloud
    canary; the unit test guards the contract.
    """
    from ma_poc.pms import scraper as scraper_mod

    src = inspect.getsource(scraper_mod)
    assert "_ANTI_SIGNAL_PATH_SEGMENTS" in src, (
        "_ANTI_SIGNAL_PATH_SEGMENTS set missing from scraper.py. "
        "Without it the accumulator composes /floorplans, /pricing, etc. "
        "off junk base URLs like /apartments/{slug}/schedule-tour, "
        "burning 4x junk fetches per hop. See PID 78134 (68.8% wallclock "
        "on schedule-tour variants) in the 2026-05-17 cloud run."
    )
    assert "_has_anti_signal_segment" in src, (
        "_has_anti_signal_segment guard missing — the segment set is "
        "defined but never checked at the composition site."
    )


def test_accumulator_gate_blocks_composition_when_junk_segment_present():
    """The composition block ``if (len(_hop_path_parts) >= 3 ... )`` must
    include ``and not _has_anti_signal_segment`` so junk base URLs do
    NOT spawn 4x suffix variants.

    This is a structural check: we grep the runtime gate to confirm the
    guard is wired. A semantic test would require spinning up the whole
    link-hop loop with a mocked fetch — disproportionate cost for this
    contract.
    """
    from ma_poc.pms import scraper as scraper_mod

    src = inspect.getsource(scraper_mod)
    gate_block = re.search(
        r"if \(\s*len\(_hop_path_parts\) >= 3"
        r"[\s\S]{0,600}?"
        r"and not _has_anti_signal_segment\s*\n",
        src,
    )
    assert gate_block is not None, (
        "Accumulator composition gate does not include "
        "`and not _has_anti_signal_segment`. Junk base URLs will still "
        "spawn /floorplans, /floor-plans, /pricing, /apartments-pricing "
        "suffix variants and burn hop budget."
    )


def test_anti_signal_segment_set_covers_known_offenders():
    """The segment set must include the keywords that triggered the
    May 17 regression — schedule-tour, apply, gallery, neighborhood,
    contact. These were the specific path segments on URLs that hit
    score 5_100 and burned PID 78134's wallclock."""
    from ma_poc.pms import scraper as scraper_mod

    src = inspect.getsource(scraper_mod)
    required_keywords = (
        "schedule-tour",
        "tour",
        "gallery",
        "photos",
        "amenities",
        "neighborhood",
        "about",
        "contact",
        "login",
        "sign-in",
    )
    for kw in required_keywords:
        assert f'"{kw}"' in src, (
            f"_ANTI_SIGNAL_PATH_SEGMENTS missing {kw!r}. This keyword was "
            "observed on regressed PIDs in the 2026-05-17 cloud run; "
            "removing it re-opens that failure mode."
        )
