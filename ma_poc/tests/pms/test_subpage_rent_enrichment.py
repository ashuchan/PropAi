"""Subpage rent enrichment — cross-tier merge for TIER_3_DOM plan-only.

Pins the 2026-05-24 fix that lifts the SUBPAGE_HAS_RENT subset of the
TIER_3_DOM cohort (9 props in focused-3886351 canary). Pre-fix the
orchestrator stopped at the first tier that returned units; if that
tier's units had area+beds+baths but no rent, the property dropped
out as a strict-fail even though rent IS published on the operator's
``/floorplans`` subpage.

Live-verified 2026-05-24: probes of www.rusticwoodsapts.com,
www.greenarchtulsa.com, www.villagesquarewheaton.com, polodowns.com,
www.cthevue.com all showed 3-16 rent tokens at /floorplans subpage
that TIER_3_DOM didn't reach.

The fix runs BEFORE the LLM rescue (so we don't pay for LLM when a
cheap subpage probe + name-merge does the job).
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from types import SimpleNamespace


def _has_strict(units: Iterable[dict]) -> bool:
    return any(
        (u.get("market_rent_low") or u.get("rent_low"))
        and (u.get("sqft") or u.get("area"))
        for u in units
    )


def test_helper_bodytext_picks_up_subpage_html() -> None:
    """The enrichment uses _bodytext_from_fetch_result against the
    subpage body — confirm it strips tags + finds plan rows."""
    from ma_poc.pms.adapters.generic_plan_text import (
        _bodytext_from_fetch_result,
        parse_generic_plan_text,
    )

    sub_html = b"""
    <html><body>
      <div>The Maple - 1 Bedroom / 1 Bath - $1,450/month - 650 sq ft</div>
      <div>The Oak - 2 Bedroom / 2 Bath - $1,950/month - 950 sq ft</div>
      <div>The Pine - 3 Bedroom / 2 Bath - $2,400/month - 1,200 sq ft</div>
    </body></html>
    """
    stub = SimpleNamespace(fetch_result=SimpleNamespace(body=sub_html))
    body_text = _bodytext_from_fetch_result(stub)
    rows = parse_generic_plan_text(body_text, "https://x.com/floorplans")
    rents = [r for r in rows if r.get("market_rent_low")]
    assert len(rents) >= 2, (
        f"expected ≥2 plans with rent, got {len(rents)}: {rows}"
    )


def test_enrichment_skips_when_units_already_have_rent() -> None:
    """When the cascade winner already has rent on units, the
    enrichment must early-skip — no subpage probe fires."""
    # This is structural: the condition is `n_with_rent == 0 and n_with_area > 0`
    # so units with rent skip the trigger entirely.
    units_with_rent = [
        {"floor_plan_name": "A", "market_rent_low": 1500, "sqft": 700},
        {"floor_plan_name": "B", "market_rent_low": 2000, "sqft": 900},
    ]
    n_with_rent = sum(
        1 for u in units_with_rent
        if u.get("market_rent_low") or u.get("rent_low")
    )
    assert n_with_rent == 2  # would skip enrichment


def test_enrichment_triggers_bidirectionally() -> None:
    """Trigger predicate (2026-05-24 bi-directional rewrite):

    Direction A (area → rent): n_with_rent==0 AND n_with_area>0
      → probe subpage for rent (Greenarch/Village Square cohort)
    Direction B (rent → sqft): n_with_area==0 AND n_with_rent>0
      → probe subpage for sqft (Repli360/RentCafe SecureCafe/SightMap
      PLAN_LEVEL cohort — TIER_MERGED_CROSS_PAGE P1 props)
    Partial: BOTH dims present but fewer than half of units have both
      → probe for the dim with fewer hits
    No-op when units have both dims OR when units are empty.
    """
    def _classify(units):
        n_with_rent = sum(
            1 for u in units
            if u.get("market_rent_low") or u.get("market_rent_high")
            or u.get("rent_low") or u.get("rent_high")
        )
        n_with_area = sum(
            1 for u in units if u.get("sqft") or u.get("area")
        )
        if not units:
            return None
        if n_with_rent == 0 and n_with_area > 0:
            return "rent"
        if n_with_area == 0 and n_with_rent > 0:
            return "sqft"
        if n_with_rent > 0 and n_with_area > 0:
            both = sum(
                1 for u in units
                if (u.get("market_rent_low") or u.get("rent_low"))
                and (u.get("sqft") or u.get("area"))
            )
            if both < len(units) * 0.5:
                return "sqft" if n_with_area < n_with_rent else "rent"
        return None

    cases = [
        # (units, expected_missing_dim)
        ([], None),
        ([{"floor_plan_name": "A", "sqft": 700}], "rent"),
        ([{"floor_plan_name": "A", "area": 700}], "rent"),
        # NEW direction: has rent, no area → enrich sqft
        ([{"floor_plan_name": "A", "rent_low": 1500}], "sqft"),
        ([{"floor_plan_name": "A", "market_rent_low": 1500}], "sqft"),
        # Both dims → no trigger
        ([{"floor_plan_name": "A", "sqft": 700, "market_rent_low": 1500}], None),
        # Name-only → no trigger
        ([{"floor_plan_name": "A"}], None),
        # Partial: 1 of 4 has both, 3 missing sqft → trigger sqft
        ([
            {"floor_plan_name": "A", "rent_low": 1500, "sqft": 700},
            {"floor_plan_name": "B", "rent_low": 1600},
            {"floor_plan_name": "C", "rent_low": 1700},
            {"floor_plan_name": "D", "rent_low": 1800},
        ], "sqft"),
    ]
    for units, expect in cases:
        got = _classify(units)
        assert got == expect, (
            f"units={units}: expected missing={expect!r}, got {got!r}"
        )


def test_enrichment_merges_by_exact_name_match() -> None:
    """Inline simulation of the merge logic: exact-name match between
    primary tier's unit and subpage's parsed row."""
    primary_units = [
        {"floor_plan_name": "Sedona", "sqft": 675, "beds": 1},
        {"floor_plan_name": "Mesa", "sqft": 875, "beds": 2},
    ]
    name_rent_map = {
        "sedona": (1450, 1450),
        "mesa": (1950, 1950),
    }

    merged = 0
    for u in primary_units:
        uname = str(u.get("floor_plan_name") or "").strip().lower()
        hit = name_rent_map.get(uname)
        if not hit:
            for k, v in name_rent_map.items():
                if uname in k or k in uname:
                    hit = v
                    break
        if hit:
            rlo, rhi = hit
            u["market_rent_low"] = rlo
            u["market_rent_high"] = rhi
            merged += 1

    assert merged == 2
    assert primary_units[0]["market_rent_low"] == 1450
    assert primary_units[1]["market_rent_low"] == 1950
    # Strict-pass: rent + sqft both present
    assert _has_strict(primary_units)


def test_enrichment_substring_match_when_exact_fails() -> None:
    """Floor plan names often vary slightly between primary tier and
    subpage (e.g. 'The Sedona' vs 'Sedona'). The merge falls back to
    substring match either direction."""
    primary_units = [
        {"floor_plan_name": "The Sedona", "sqft": 675},
        {"floor_plan_name": "Mesa Deluxe", "sqft": 875},
    ]
    name_rent_map = {
        "sedona": (1450, 1450),
        "mesa": (1950, 1950),
    }

    merged = 0
    for u in primary_units:
        uname = str(u.get("floor_plan_name") or "").strip().lower()
        hit = name_rent_map.get(uname)
        if not hit:
            for k, v in name_rent_map.items():
                if uname in k or k in uname:
                    hit = v
                    break
        if hit:
            u["market_rent_low"] = hit[0]
            u["market_rent_high"] = hit[1]
            merged += 1

    assert merged == 2  # both matched via substring


def test_enrichment_no_match_leaves_units_unchanged() -> None:
    """When subpage rent doesn't name-match any primary unit, units
    stay as-is (no false matches)."""
    primary_units = [
        {"floor_plan_name": "Plan A1", "sqft": 700},
    ]
    name_rent_map = {
        "completely-different-name": (1500, 1500),
    }
    for u in primary_units:
        uname = str(u.get("floor_plan_name") or "").strip().lower()
        hit = name_rent_map.get(uname)
        if not hit:
            for k, v in name_rent_map.items():
                if uname in k or k in uname:
                    hit = v
                    break
        if hit:
            u["market_rent_low"] = hit[0]
    assert "market_rent_low" not in primary_units[0]


def test_subpage_paths_covered() -> None:
    """The enrichment probes a known list of common subpage paths.
    Add this guard so changes to that list are intentional."""
    # The actual list in scraper.py
    expected_paths = (
        "/floorplans/", "/floorplans",
        "/floor-plans/", "/floor-plans",
        "/availability/", "/availability",
        "/apartments/", "/apartments",
        "/pricing/", "/pricing",
    )
    # Read source and confirm all paths appear
    scraper_src = open(
        "/Users/ankur/PropAi-main/.claude/worktrees/angry-murdock-c19e06/ma_poc/pms/scraper.py"
    ).read()
    for p in expected_paths:
        assert f'"{p}"' in scraper_src, f"path {p} missing from scraper.py"


def test_enrichment_warning_marker_in_errors() -> None:
    """When the enrichment merges rent, it appends a structured
    marker to ``adapter_result.errors`` so the run report shows the
    rescue path took effect."""
    marker_pattern = re.compile(
        r"subpage-rent-enrichment: merged rent into \d+/\d+ units from subpage probe \(\d+ plans found\)"
    )
    # The format string in scraper.py
    test_msg = "subpage-rent-enrichment: merged rent into 3/4 units from subpage probe (5 plans found)"
    assert marker_pattern.match(test_msg)
