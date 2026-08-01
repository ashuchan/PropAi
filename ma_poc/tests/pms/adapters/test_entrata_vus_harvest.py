"""Entrata verbatim view_unit_spaces harvest (prod 2026-07-12, local-IP fix).

The prospectportal /conventional grid embeds the complete per-plan
availability XHR URLs verbatim in its primary-action buttons. Replaying them
(cookie session + XHR + Referer) upgrades the plan-level grid to unit-level.
Here we test the PURE pieces: the MM/DD/YYYY date parser and the URL harvest
(+ its self-gating). The network replay is validated live, not in CI.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ma_poc.pms.adapters._parsing import make_unit_dict
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.entrata import (
    EntrataAdapter,
    _extract_vus_urls,
    _pp_iso,
    _recover_embedded_vus_direct,
)
from ma_poc.pms.detector import DetectedPMS

# ── _pp_iso: the MM/DD/YYYY addition ────────────────────────────────────────

def test_pp_iso_us_month_first() -> None:
    # data-unitavailabilitydate="09/17/2026" was previously dropped
    assert _pp_iso("09/17/2026") == "2026-09-17"
    assert _pp_iso("9/1/2026") == "2026-09-01"


def test_pp_iso_year_first_still_works() -> None:
    assert _pp_iso("2026/05/17") == "2026-05-17"
    assert _pp_iso("2026-05-17") == "2026-05-17"


def test_pp_iso_junk_returns_empty() -> None:
    assert _pp_iso("Available Now") == "Available Now"
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


def test_harvest_preserves_spaces_and_the_query_tail() -> None:
    html = (
        '<button data-url="https://villagewestside.prospectportal.com/'
        '?module=check_availability&amp;property[id]=540130'
        '&amp;action=view_unit_spaces&amp;lease_term_name=13mo lease'
        '&amp;property_floorplan[id]=527504&amp;move_in_date=2026-08-01'
        '&amp;occupancy_type=conventional">View</button>'
    )
    pairs = _extract_vus_urls([(_GRID, html)], _GRID)
    assert len(pairs) == 1
    vus = pairs[0][1]
    assert "lease_term_name=13mo lease" in vus
    assert "property_floorplan[id]=527504" in vus
    assert vus.endswith("occupancy_type=conventional")


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


def _ctx(body: str, *, name: str = "The Henry at Rosenberg") -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://www.thehenryatrosenberg.com/",
        detected=DetectedPMS(
            pms="entrata",
            confidence=0.9,
            recommended_strategy="api_first",
        ),
        profile=None,
        expected_total_units=None,
        property_id="henry-1",
        fetch_result=SimpleNamespace(
            body=body.encode(),
            final_url="https://www.thehenryatrosenberg.com/",
        ),
        property_name=name,
    )
    setattr(ctx, "_api_responses", [])
    return ctx


@pytest.mark.asyncio
async def test_direct_replay_binds_cross_host_twin_and_keeps_full_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grid = (
        "https://henryatrosenberg.prospectportal.com/rosenberg/"
        "the-henry-at-rosenberg/conventional/"
    )
    html = (
        f'<a href="{grid}">Floor Plans</a>'
        '<button data-url="https://henryatrosenberg.prospectportal.com/'
        '?module=check_availability&amp;property[id]=1185867'
        '&amp;action=view_unit_spaces&amp;lease_term_name=13mo lease'
        '&amp;property_floorplan[id]=713990&amp;move_in_date=2026-08-01'
        '&amp;occupancy_type=conventional">View</button>'
    )
    captured: list[tuple[str, str]] = []

    def fake_replay(pairs: list[tuple[str, str]]) -> list[dict[str, object]]:
        captured.extend(pairs)
        return [
            make_unit_dict(
                floor_plan_name="A1",
                bedrooms="1",
                bathrooms="1",
                sqft="700",
                unit_number="1204",
                rent_low=1310,
                rent_high=1310,
            )
        ]

    monkeypatch.setattr(
        "ma_poc.pms.adapters.entrata._replay_vus_sync",
        fake_replay,
    )

    rows, winning_grid = await _recover_embedded_vus_direct(_ctx(html))

    assert len(rows) == 1
    assert winning_grid == grid
    assert captured[0][0] == grid
    assert "lease_term_name=13mo lease" in captured[0][1]
    assert captured[0][1].endswith("occupancy_type=conventional")


@pytest.mark.asyncio
async def test_direct_replay_rejects_cross_host_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = (
        '<a href="https://sibling.prospectportal.com/austin/sibling/conventional/">x</a>'
        '<button data-url="https://sibling.prospectportal.com/'
        '?property[id]=999999&amp;action=view_unit_spaces&amp;'
        'property_floorplan[id]=123456">View</button>'
    )

    def unexpected_replay(_pairs: object) -> object:
        raise AssertionError("sibling roster must not be replayed")

    monkeypatch.setattr(
        "ma_poc.pms.adapters.entrata._replay_vus_sync",
        unexpected_replay,
    )
    assert await _recover_embedded_vus_direct(_ctx(html)) == ([], "")


@pytest.mark.asyncio
async def test_direct_vus_win_preempts_hyperbrowser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = make_unit_dict(
        floor_plan_name="A1",
        bedrooms="1",
        bathrooms="1",
        sqft="700",
        unit_number="1204",
        rent_low=1310,
        rent_high=1310,
    )

    async def fake_direct(_ctx: object) -> tuple[list[dict[str, object]], str]:
        return [row], _GRID

    async def unexpected_hb(_ctx: object) -> object:
        raise AssertionError("Hyperbrowser must not run after a direct VUS win")

    monkeypatch.setattr(
        "ma_poc.pms.adapters.entrata._recover_embedded_vus_direct",
        fake_direct,
    )
    monkeypatch.setattr(
        "ma_poc.pms.adapters._entrata_hb_recovery.recover_entrata_hb_conventional",
        unexpected_hb,
    )

    result = await EntrataAdapter().extract(None, _ctx("<html></html>"))

    assert [unit["unit_number"] for unit in result.units] == ["1204"]
    assert result.tier_used == "TIER_1_DOM_ENTRATA_EMBEDDED_VUS_DIRECT"
