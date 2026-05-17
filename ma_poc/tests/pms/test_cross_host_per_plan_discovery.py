"""Tests for ``_discover_cross_host_per_plan_urls`` in ``pms.scraper``.

The helper looks at a link-hop candidate queue and returns the subset of
URLs that look like per-floorplan detail pages. Used when a hop yields
plan-summary rows but the same-host HTML-anchor fallback didn't surface
any sub-page links — the per-plan URLs may live cross-host on the
property's marketing site.

Real-world case (PID 52331 alexandriacarmel, 2026-05-17):
  - hop produced 10 plan-shaped rows from ``*.securecafe.com``
  - entry-page queue had ``alexandriacarmel.com/floorplans/the-diplomat-1-br-1-ba``
    at score 5980 with anchor text "the diplomat - 1 br 1 ba"
  - same-host HTML fallback never surfaces this (cross-host)
  - this helper recognises it by URL shape
"""

from __future__ import annotations

from ma_poc.pms.scraper import _discover_cross_host_per_plan_urls


def _candidate(url: str, score: int = 5000, anchor: str = "") -> tuple[str, int, str]:
    """Tuple shape used by the link-hop queue."""
    return (url, score, anchor)


class TestPerPlanUrlShape:
    def test_matches_floorplans_slug_with_br_ba(self):
        """Canonical per-plan URL shape: /floorplans/{slug-with-br-ba}."""
        queue = [
            _candidate("https://www.alexandriacarmel.com/floorplans/the-diplomat-1-br-1-ba", 5980),
        ]
        matched = _discover_cross_host_per_plan_urls(
            queue=queue,
            visited=set(),
            sub_url="https://alexandriacarmel.securecafe.com/onlineleasing/foo.aspx",
            existing_fp_hints=[],
        )
        assert matched == [
            "https://www.alexandriacarmel.com/floorplans/the-diplomat-1-br-1-ba"
        ]

    def test_matches_hyphenated_floor_plans(self):
        """``floor-plans`` (with hyphen) is the same shape as ``floorplans``."""
        queue = [
            _candidate("https://example.com/floor-plans/the-ambassador-2-br-1-ba", 5500),
        ]
        matched = _discover_cross_host_per_plan_urls(
            queue=queue,
            visited=set(),
            sub_url="https://example.com/",
            existing_fp_hints=[],
        )
        assert len(matched) == 1

    def test_matches_plans_alias(self):
        """``/plans/{slug-with-br}`` also qualifies."""
        queue = [
            _candidate("https://example.com/plans/studio-0-bed-1-bath", 5100),
        ]
        matched = _discover_cross_host_per_plan_urls(
            queue=queue,
            visited=set(),
            sub_url="https://example.com/",
            existing_fp_hints=[],
        )
        assert len(matched) == 1

    def test_studio_slug_token_matches(self):
        """``studio`` qualifies as a bed/bath token even without -br-."""
        queue = [
            _candidate("https://example.com/floorplans/the-loft-studio-version", 5200),
        ]
        matched = _discover_cross_host_per_plan_urls(
            queue=queue,
            visited=set(),
            sub_url="https://example.com/",
            existing_fp_hints=[],
        )
        assert len(matched) == 1

    def test_rejects_bare_floorplans_index(self):
        """``/floorplans`` (no slug) is the listing page — excluded."""
        queue = [
            _candidate("https://example.com/floorplans", 5100),
            _candidate("https://example.com/floorplans/", 5100),
        ]
        matched = _discover_cross_host_per_plan_urls(
            queue=queue,
            visited=set(),
            sub_url="https://example.com/",
            existing_fp_hints=[],
        )
        assert matched == []

    def test_rejects_slug_without_bed_bath_token(self):
        """Slug must contain br/bed/bath/ba/studio — generic slugs rejected."""
        queue = [
            _candidate("https://example.com/floorplans/community-gallery", 5100),
            _candidate("https://example.com/floorplans/photos-and-amenities", 5100),
        ]
        matched = _discover_cross_host_per_plan_urls(
            queue=queue,
            visited=set(),
            sub_url="https://example.com/",
            existing_fp_hints=[],
        )
        assert matched == []

    def test_rejects_unrelated_paths(self):
        """Site-wide nav links (contact, sister-properties, interactive map)
        don't match the per-plan path pattern."""
        queue = [
            _candidate("https://example.com/contact", 5000),
            _candidate("https://example.com/sisterproperties", 5100),
            _candidate("https://example.com/interactivepropertymap", 5100),
            _candidate("https://example.com/availability", 5095),
        ]
        matched = _discover_cross_host_per_plan_urls(
            queue=queue,
            visited=set(),
            sub_url="https://example.com/",
            existing_fp_hints=[],
        )
        assert matched == []


class TestQueueFilters:
    def test_skips_visited_urls(self):
        url = "https://example.com/floorplans/the-foo-1-br-1-ba"
        queue = [_candidate(url, 5500)]
        matched = _discover_cross_host_per_plan_urls(
            queue=queue,
            visited={url},
            sub_url="https://example.com/",
            existing_fp_hints=[],
        )
        assert matched == []

    def test_skips_sub_url_itself(self):
        """The just-fetched hop URL is excluded from matches."""
        url = "https://example.com/floorplans/the-foo-1-br-1-ba"
        queue = [_candidate(url, 5500)]
        matched = _discover_cross_host_per_plan_urls(
            queue=queue,
            visited=set(),
            sub_url=url,  # same URL as the candidate
            existing_fp_hints=[],
        )
        assert matched == []

    def test_skips_urls_already_in_fp_hints(self):
        url = "https://example.com/floorplans/the-foo-1-br-1-ba"
        queue = [_candidate(url, 5500)]
        matched = _discover_cross_host_per_plan_urls(
            queue=queue,
            visited=set(),
            sub_url="https://example.com/",
            existing_fp_hints=[(url, "html_subpage")],
        )
        assert matched == []

    def test_skips_low_score_candidates(self):
        """Candidates below the 4_000 score floor (e.g. site-wide nav
        links scoring 3_000) are filtered out."""
        url = "https://example.com/floorplans/the-foo-1-br-1-ba"
        queue = [_candidate(url, 3000)]
        matched = _discover_cross_host_per_plan_urls(
            queue=queue,
            visited=set(),
            sub_url="https://example.com/",
            existing_fp_hints=[],
        )
        assert matched == []

    def test_preserves_queue_order(self):
        """Matched URLs are returned in the order they appeared in the
        input queue (preserving the entry ranker's ordering)."""
        urls = [
            "https://example.com/floorplans/the-ambassador-2-br-1-ba",
            "https://example.com/floorplans/the-justice-1-br-1-ba",
            "https://example.com/floorplans/the-diplomat-1-br-1-ba",
        ]
        queue = [_candidate(u, 5500 - i * 10) for i, u in enumerate(urls)]
        matched = _discover_cross_host_per_plan_urls(
            queue=queue,
            visited=set(),
            sub_url="https://example.com/",
            existing_fp_hints=[],
        )
        assert matched == urls


class TestEdgeCases:
    def test_empty_queue_returns_empty(self):
        matched = _discover_cross_host_per_plan_urls(
            queue=[],
            visited=set(),
            sub_url="https://example.com/",
            existing_fp_hints=[],
        )
        assert matched == []

    def test_cross_host_url_matches(self):
        """The whole point — the candidate's host can differ from
        ``sub_url``'s host (cross-host per-plan discovery)."""
        queue = [
            _candidate(
                "https://www.alexandriacarmel.com/floorplans/the-diplomat-1-br-1-ba",
                5980,
            ),
        ]
        matched = _discover_cross_host_per_plan_urls(
            queue=queue,
            visited=set(),
            sub_url="https://alexandriacarmel.securecafe.com/onlineleasing/x.aspx",
            existing_fp_hints=[],
        )
        assert len(matched) == 1

    def test_malformed_url_does_not_crash(self):
        """Defensive — bad URL strings get skipped silently."""
        queue = [
            _candidate("not-a-real-url", 5000),
            _candidate("https://example.com/floorplans/the-foo-1-br-1-ba", 5500),
        ]
        matched = _discover_cross_host_per_plan_urls(
            queue=queue,
            visited=set(),
            sub_url="https://example.com/",
            existing_fp_hints=[],
        )
        # The valid URL still matches.
        assert "https://example.com/floorplans/the-foo-1-br-1-ba" in matched
