"""Entrata verbatim view_unit_spaces harvest (prod 2026-07-12, local-IP fix).

The prospectportal /conventional grid embeds the complete per-plan
availability XHR URLs verbatim in its primary-action buttons. Replaying them
(cookie session + XHR + Referer) upgrades the plan-level grid to unit-level.
Here we test the PURE pieces: the MM/DD/YYYY date parser and the URL harvest
(+ its self-gating). The network replay is validated live, not in CI.
"""

from __future__ import annotations

from ma_poc.pms.adapters.entrata import _extract_vus_urls, _pp_iso

# ── _pp_iso: the MM/DD/YYYY addition ────────────────────────────────────────

def test_pp_iso_us_month_first() -> None:
    # data-unitavailabilitydate="09/17/2026" was previously dropped
    assert _pp_iso("09/17/2026") == "2026-09-17"
    assert _pp_iso("9/1/2026") == "2026-09-01"


def test_pp_iso_year_first_still_works() -> None:
    assert _pp_iso("2026/05/17") == "2026-05-17"
    assert _pp_iso("2026-05-17") == "2026-05-17"


def test_pp_iso_junk_returns_empty() -> None:
    assert _pp_iso("Available Now") == ""
    assert _pp_iso("") == ""


# ── _extract_vus_urls: harvest + self-gating + waitlist skip ────────────────

_GRID = "https://villagewestside.prospectportal.com/dallas/x/conventional/"


def test_harvests_absolute_and_escaped_urls() -> None:
    html = (
        '<button class="primary-action" data-url="https://villagewestside'
        '.prospectportal.com/?module=check_availability&amp;is_secure=1'
        '&amp;property[id]=540130&amp;action=view_unit_spaces'
        '&amp;cached_rate_available=1&amp;property_floorplan[id]=527504'
        '&amp;occupancy_type=conventional">View</button>'
    )
    pairs = _extract_vus_urls([(_GRID, html)], _GRID)
    assert len(pairs) == 1
    grid_url, vus = pairs[0]
    assert grid_url == _GRID
    assert "&amp;" not in vus  # unescaped
    assert "action=view_unit_spaces" in vus
    assert "property_floorplan[id]=527504" in vus


def test_root_relative_url_resolved_against_grid() -> None:
    html = (
        '<a href="/?module=check_availability&action=view_unit_spaces'
        '&property_floorplan[id]=99">go</a>'
    )
    pairs = _extract_vus_urls([(_GRID, html)], _GRID)
    assert len(pairs) == 1
    assert pairs[0][1].startswith("https://villagewestside.prospectportal.com/")


def test_waitlist_plans_are_skipped() -> None:
    """is_availability_alert plans are legit 0-unit — must NOT be harvested
    (their plan-level row stands)."""
    html = (
        '<a href="/?action=view_unit_spaces&is_availability_alert=true'
        '&property_floorplan[id]=1">waitlist</a>'
        '<a href="/?action=view_unit_spaces&cached_rate_available=1'
        '&property_floorplan[id]=2">available</a>'
    )
    pairs = _extract_vus_urls([(_GRID, html)], _GRID)
    assert len(pairs) == 1
    assert "property_floorplan[id]=2" in pairs[0][1]


def test_self_gating_no_buttons_harvests_nothing() -> None:
    """Vanity grids without view_unit_spaces buttons harvest nothing → the
    plan-level path is untouched (zero regression)."""
    html = "<div class='fp-card'>1 Bedroom from $1,500</div>"
    assert _extract_vus_urls([(_GRID, html)], _GRID) == []


def test_dedupes_repeated_urls() -> None:
    dup = (
        '<a href="/?action=view_unit_spaces&property_floorplan[id]=5">a</a>'
        '<a href="/?action=view_unit_spaces&property_floorplan[id]=5">b</a>'
    )
    assert len(_extract_vus_urls([(_GRID, dup)], _GRID)) == 1
