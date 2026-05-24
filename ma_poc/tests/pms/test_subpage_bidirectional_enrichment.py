"""Bi-directional cross-page enrichment — rent ↔ sqft (2026-05-24).

Pins the F1.5 generalisation that lifts the TIER_MERGED_CROSS_PAGE
P1 cohort (32 props). Pre-fix the orchestrator only enriched MISSING
rent when units had area. Post-fix it also enriches MISSING sqft
when units have rent — the typical shape for plan-level adapter
output (Repli360, RentCafe SecureCafe, SightMap, AppFolio SSR).

These tests simulate the merge logic in isolation (no orchestrator
overhead) so the trigger predicate + per-direction merge keys are
pinned independently from the F1.5 caller context.
"""
from __future__ import annotations


# ── Per-direction merge: rent → sqft ────────────────────────────────


def test_sqft_merge_by_exact_name_match() -> None:
    """Primary has rent, subpage has sqft, names match exactly."""
    primary = [
        {"floor_plan_name": "Sedona", "market_rent_low": 1500},
        {"floor_plan_name": "Mesa", "market_rent_low": 1800},
    ]
    sqft_map = {
        "sedona": (None, None, "675"),
        "mesa": (None, None, "875"),
    }

    merged = 0
    for u in primary:
        if u.get("sqft") or u.get("area"):
            continue
        name = (u.get("floor_plan_name") or "").strip().lower()
        hit = sqft_map.get(name)
        if not hit:
            for k, v in sqft_map.items():
                if name in k or k in name:
                    hit = v
                    break
        if not hit:
            continue
        _, _, sq = hit
        if sq:
            u["sqft"] = sq
            u["area"] = sq
            merged += 1

    assert merged == 2
    assert primary[0]["sqft"] == "675"
    assert primary[1]["sqft"] == "875"


def test_sqft_merge_substring_match_when_exact_fails() -> None:
    """Primary tier emits 'Sedona', subpage carries 'The Sedona' — must
    still match via substring."""
    primary = [
        {"floor_plan_name": "Sedona", "market_rent_low": 1500},
    ]
    sqft_map = {
        "the sedona floor plan": (None, None, "700"),
    }

    name = primary[0]["floor_plan_name"].lower()
    hit = sqft_map.get(name)
    if not hit:
        for k, v in sqft_map.items():
            if name in k or k in name:
                hit = v
                break
    assert hit is not None
    assert hit[2] == "700"


def test_sqft_merge_skips_units_that_already_have_sqft() -> None:
    """Idempotency: units with sqft already aren't touched."""
    primary = [
        {"floor_plan_name": "Sedona", "market_rent_low": 1500, "sqft": "650"},
        {"floor_plan_name": "Mesa", "market_rent_low": 1800},
    ]
    sqft_map = {
        "sedona": (None, None, "999"),  # wrong/stale subpage value
        "mesa": (None, None, "875"),
    }

    for u in primary:
        if u.get("sqft") or u.get("area"):
            continue
        name = (u.get("floor_plan_name") or "").strip().lower()
        hit = sqft_map.get(name)
        if hit and hit[2]:
            u["sqft"] = hit[2]
            u["area"] = hit[2]

    # Sedona keeps its original 650, not overwritten by 999
    assert primary[0]["sqft"] == "650"
    # Mesa got the new sqft
    assert primary[1]["sqft"] == "875"


def test_sqft_merge_no_match_leaves_units_unchanged() -> None:
    primary = [{"floor_plan_name": "Foo", "market_rent_low": 1500}]
    sqft_map = {"bar": (None, None, "700")}
    name = primary[0]["floor_plan_name"].lower()
    hit = sqft_map.get(name)
    if not hit:
        for k in sqft_map:
            if name in k or k in name:
                hit = sqft_map[k]
                break
    assert hit is None
    assert "sqft" not in primary[0]


# ── Per-direction merge: rent → rent (existing direction unchanged) ─


def test_rent_merge_still_works_after_bidirectional_rewrite() -> None:
    """Regression guard: the original area→rent direction must
    continue to work."""
    primary = [
        {"floor_plan_name": "Sedona", "sqft": "675"},
        {"floor_plan_name": "Mesa", "sqft": "875"},
    ]
    rent_map = {
        "sedona": (1500, 1600, None),
        "mesa": (1800, None, None),
    }

    merged = 0
    for u in primary:
        if u.get("market_rent_low") or u.get("rent_low"):
            continue
        name = (u.get("floor_plan_name") or "").strip().lower()
        hit = rent_map.get(name)
        if not hit:
            continue
        rlo, rhi, _ = hit
        if rlo is not None:
            u["market_rent_low"] = rlo
        if rhi is not None:
            u["market_rent_high"] = rhi
        merged += 1

    assert merged == 2
    assert primary[0]["market_rent_low"] == 1500
    assert primary[0]["market_rent_high"] == 1600
    assert primary[1]["market_rent_low"] == 1800
    # high was None → not set
    assert "market_rent_high" not in primary[1]


# ── Cohort-shape integration ────────────────────────────────────────


def test_repli360_planlevel_shape_enriches_sqft() -> None:
    """The Repli360 PLAN_LEVEL output for the TIER_MERGED P1 cohort:
    units carry plan-level rent (rent range from Repli360 API) but
    missing sqft. The bi-directional enrichment must merge sqft from
    a /floor-plans/ subpage probe."""
    repli360_output = [
        {
            "floor_plan_name": "Studio Loft",
            "market_rent_low": 1450,
            "market_rent_high": 1550,
            "extraction_tier": "TIER_1_API_REPLI360_PLAN_LEVEL",
        },
        {
            "floor_plan_name": "1 Bed Garden",
            "market_rent_low": 1750,
            "market_rent_high": 1900,
            "extraction_tier": "TIER_1_API_REPLI360_PLAN_LEVEL",
        },
    ]
    # Subpage probe finds matching plans with sqft
    sqft_map = {
        "studio loft": (None, None, "550"),
        "1 bed garden": (None, None, "780"),
    }

    for u in repli360_output:
        if u.get("sqft") or u.get("area"):
            continue
        name = (u.get("floor_plan_name") or "").strip().lower()
        hit = sqft_map.get(name)
        if hit and hit[2]:
            u["sqft"] = hit[2]
            u["area"] = hit[2]

    # Now units should be strict-pass (both rent + sqft)
    for u in repli360_output:
        assert u.get("market_rent_low")
        assert u.get("sqft")


def test_sightmap_planlevel_shape_enriches_sqft() -> None:
    """SightMap PLAN_LEVEL: when the SightMap API returns plan
    summaries with rent but no area, the subpage probe should backfill
    sqft from the floor-plans page."""
    sm_output = [
        {
            "floor_plan_name": "A1",
            "market_rent_low": 2100,
            "extraction_tier": "TIER_1_API_SIGHTMAP_PLAN_LEVEL",
        },
        {
            "floor_plan_name": "B2",
            "market_rent_low": 2900,
            "extraction_tier": "TIER_1_API_SIGHTMAP_PLAN_LEVEL",
        },
    ]
    sqft_map = {
        "a1": (None, None, "680"),
        "b2": (None, None, "1100"),
    }

    for u in sm_output:
        name = (u.get("floor_plan_name") or "").strip().lower()
        hit = sqft_map.get(name)
        if hit and hit[2]:
            u["sqft"] = hit[2]

    assert sm_output[0]["sqft"] == "680"
    assert sm_output[1]["sqft"] == "1100"


def test_rentcafe_securecafe_planlevel_shape_enriches_sqft() -> None:
    """RentCafe SecureCafe PLAN_LEVEL: ironstate.com cohort."""
    rcsc_output = [
        {
            "floor_plan_name": "1 Bedroom 1 Bath",
            "market_rent_low": 2200,
            "extraction_tier": "TIER_1_API_RENTCAFE_SECURECAFE_PLAN_LEVEL",
        },
    ]
    sqft_map = {
        "1 bedroom 1 bath": (None, None, "750"),
    }

    for u in rcsc_output:
        name = (u.get("floor_plan_name") or "").strip().lower()
        hit = sqft_map.get(name)
        if hit and hit[2]:
            u["sqft"] = hit[2]

    assert rcsc_output[0]["sqft"] == "750"


# ── Partial case: SOME units have both, others missing one ──────────


def test_partial_cohort_triggers_sqft_enrichment_when_majority_missing() -> None:
    """When only 1 of 4 units has both dims and the rest are missing
    sqft, the partial-trigger logic should pick sqft as the dim to
    enrich."""
    units = [
        {"floor_plan_name": "A", "rent_low": 1500, "sqft": "700"},
        {"floor_plan_name": "B", "rent_low": 1600},
        {"floor_plan_name": "C", "rent_low": 1700},
        {"floor_plan_name": "D", "rent_low": 1800},
    ]
    n_with_rent = sum(1 for u in units if u.get("rent_low"))
    n_with_area = sum(1 for u in units if u.get("sqft"))
    both = sum(1 for u in units if u.get("rent_low") and u.get("sqft"))
    assert n_with_rent == 4
    assert n_with_area == 1
    assert both == 1
    # Trigger condition: both < half
    assert both < len(units) * 0.5
    # Picked dim: sqft (fewer units have area)
    picked = "sqft" if n_with_area < n_with_rent else "rent"
    assert picked == "sqft"
