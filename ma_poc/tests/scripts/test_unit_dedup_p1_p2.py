"""Post-extraction unit dedup — P1 EXACT_DUPE + P2 building disambiguation.

Origin: 87b837b canary QC found 9,773 dup-unit_id rows across 347 props.
66.5% EXACT_DUPE (multi-source merge), 11.9% BUILDING_DIFFERS (legit
cross-bldg reuse). Pipeline order matters: P2 runs BEFORE P1, because
rewriting unit_id → ``{building}-{unit_id}`` makes the fingerprint of
a legit cross-bldg unit distinct from its sibling — without that
ordering, P1 would drop the sibling and we'd lose 632 cross-bldg units.
"""
from __future__ import annotations

from ma_poc.scripts.runners.jugnu import (
    _apply_p1_exact_dedup,
    _apply_p2_building_disambiguation,
    _apply_p2b_floor_plan_id_disambiguation,
    _emit_v2_units_for_property,
)


# ─────────────────────────────────────────────────────────────────────
# P1 — EXACT_DUPE filter
# ─────────────────────────────────────────────────────────────────────


def _unit(**overrides):
    """Minimal v2 unit dict (only the fields the dedup fingerprint cares
    about). Tests pass overrides on top."""
    base = {
        "unit_id": "101",
        "beds": 1,
        "baths": 1.0,
        "area": 750,
        "rent_low": 1500.0,
        "rent_high": 1500.0,
        "floor_plan_name": "A1",
        "floor_plan_id": "abc12345-deadbeef",
        "building": "",
        "availability_status": "AVAILABLE",
    }
    base.update(overrides)
    return base


def test_p1_drops_byte_identical_duplicate() -> None:
    """The 66.5% case — same unit emitted twice with identical fields."""
    units = [_unit(unit_id="207"), _unit(unit_id="207")]
    dropped = _apply_p1_exact_dedup(units)
    assert dropped == 1
    assert len(units) == 1
    assert units[0]["unit_id"] == "207"


def test_p1_keeps_units_with_any_field_different() -> None:
    """Tiny rent diff (RENT_DIFFERS class) — keep both rows so a
    follow-up policy can choose; P1 only drops EXACT matches."""
    a = _unit(unit_id="207", rent_low=1500.0)
    b = _unit(unit_id="207", rent_low=1510.0)
    units = [a, b]
    dropped = _apply_p1_exact_dedup(units)
    assert dropped == 0
    assert len(units) == 2


def test_p1_first_occurrence_is_kept() -> None:
    """Deterministic ordering — first row wins so downstream consumers
    that pick units[0] get the canonical record."""
    a = _unit(unit_id="207", rent_low=1500.0)
    b = _unit(unit_id="207", rent_low=1500.0)  # exact dupe of a
    c = _unit(unit_id="208")
    units = [a, b, c]
    _apply_p1_exact_dedup(units)
    assert units == [a, c]


def test_p1_ignores_inferred_unit_ids_as_normal_records() -> None:
    """``inferred_*`` IDs go through the same fingerprint check — if
    two inferred-id rows are byte-identical, drop one. They're
    legitimate-looking dups even when the id was synthesised."""
    a = _unit(unit_id="inferred_abc123")
    b = _unit(unit_id="inferred_abc123")
    units = [a, b]
    dropped = _apply_p1_exact_dedup(units)
    assert dropped == 1


def test_p1_handles_empty_unit_list() -> None:
    units: list = []
    dropped = _apply_p1_exact_dedup(units)
    assert dropped == 0
    assert units == []


def test_p1_handles_three_way_dup() -> None:
    """Three rows with identical fingerprint → keep first, drop two."""
    units = [_unit(), _unit(), _unit()]
    dropped = _apply_p1_exact_dedup(units)
    assert dropped == 2
    assert len(units) == 1


# ─────────────────────────────────────────────────────────────────────
# P2 — Cross-building disambiguation
# ─────────────────────────────────────────────────────────────────────


def test_p2_rewrites_unit_id_when_buildings_differ() -> None:
    """Canonical BUILDING_DIFFERS case (livetessasprings example):
    same unit_id "207" in buildings "1" and "2" → rewrite both to
    "1-207" and "2-207" so dedup treats them as distinct."""
    a = _unit(unit_id="207", building="1", rent_low=1555.0)
    b = _unit(unit_id="207", building="2", rent_low=1489.0)
    units = [a, b]
    rewritten = _apply_p2_building_disambiguation(units)
    assert rewritten == 2
    assert {u["unit_id"] for u in units} == {"1-207", "2-207"}


def test_p2_skips_collision_when_building_missing_on_any_member() -> None:
    """Conservative: if EVEN ONE row in the collision group has a blank
    building, leave the whole group alone. We can't safely rewrite
    only the ones with building (would create a phantom collision)."""
    a = _unit(unit_id="207", building="1")
    b = _unit(unit_id="207", building="")
    units = [a, b]
    rewritten = _apply_p2_building_disambiguation(units)
    assert rewritten == 0
    assert a["unit_id"] == "207"
    assert b["unit_id"] == "207"


def test_p2_skips_collision_when_buildings_all_same() -> None:
    """If both rows say building="A", that's a same-building dup —
    EXACT_DUPE territory. Don't rewrite (would create A-207 / A-207
    which is the same thing twice)."""
    a = _unit(unit_id="207", building="A")
    b = _unit(unit_id="207", building="A")
    units = [a, b]
    rewritten = _apply_p2_building_disambiguation(units)
    assert rewritten == 0


def test_p2_skips_single_occurrence() -> None:
    """No collision → no rewrite. Bare ``207`` stays ``207`` so
    we don't introduce a ``A-207`` style id where none was needed."""
    units = [_unit(unit_id="207", building="A")]
    rewritten = _apply_p2_building_disambiguation(units)
    assert rewritten == 0
    assert units[0]["unit_id"] == "207"


def test_p2_skips_inferred_ids() -> None:
    """``inferred_*`` ids are hash-fallbacks — disambiguating them by
    building muddles two distinct concepts. Leave alone."""
    a = _unit(unit_id="inferred_abc123", building="1")
    b = _unit(unit_id="inferred_abc123", building="2")
    units = [a, b]
    rewritten = _apply_p2_building_disambiguation(units)
    assert rewritten == 0


def test_p2_skips_empty_and_null_unit_ids() -> None:
    """Empty unit_ids can't collide meaningfully; don't try."""
    a = _unit(unit_id="", building="1")
    b = _unit(unit_id=None, building="2")
    units = [a, b]
    rewritten = _apply_p2_building_disambiguation(units)
    assert rewritten == 0


# ─────────────────────────────────────────────────────────────────────
# Combined pipeline — P2-then-P1 ordering is critical
# ─────────────────────────────────────────────────────────────────────


def test_combined_p2_runs_before_p1_so_cross_bldg_units_survive() -> None:
    """If P1 ran first, both ``207`` units would have the same
    fingerprint (only building differs) and P1 would drop one. P2
    must rewrite the ids first so each gets a distinct fingerprint."""
    a = _unit(unit_id="207", building="1", rent_low=1555.0)
    b = _unit(unit_id="207", building="2", rent_low=1489.0)
    units = [a, b]
    out = _emit_v2_units_for_property(units)
    # BOTH must survive
    assert len(out) == 2
    assert {u["unit_id"] for u in out} == {"1-207", "2-207"}


def test_combined_drops_exact_dupe_after_disambiguation() -> None:
    """Three rows: two are EXACT_DUPE in building "1"; one is in
    building "2". After P2 the collision group is {1-207, 1-207, 2-207}
    — wait, P2 skips because not all buildings differ within the
    collision group. So they stay as 207/207/207 and P1 keeps the first
    207 (any building "1") and drops the second 207. The "2"-building
    207 has a different fingerprint (different building) so it survives.
    """
    a = _unit(unit_id="207", building="1", rent_low=1500.0)
    b = _unit(unit_id="207", building="1", rent_low=1500.0)  # exact dupe of a
    c = _unit(unit_id="207", building="2", rent_low=1600.0)
    units = [a, b, c]
    out = _emit_v2_units_for_property(units)
    # Exact dupe (a, b) collapses to one; c has distinct fingerprint
    # (different building + rent) → survives.
    assert len(out) == 2
    rents = sorted(u["rent_low"] for u in out)
    assert rents == [1500.0, 1600.0]


def test_combined_handles_realistic_mixed_case() -> None:
    """A property with:
      - 2 exact-dupe rows of unit 101  (P1 drops 1)
      - 2 cross-bldg unit 207  (P2 disambiguates)
      - 1 unique unit 305
      - 1 inferred_xxx (untouched)
    Final: 5 rows survive (was 6 in)."""
    units = [
        _unit(unit_id="101"),
        _unit(unit_id="101"),  # exact dupe
        _unit(unit_id="207", building="A", rent_low=1555.0),
        _unit(unit_id="207", building="B", rent_low=1489.0),
        _unit(unit_id="305"),
        _unit(unit_id="inferred_xyz"),
    ]
    out = _emit_v2_units_for_property(units)
    assert len(out) == 5
    ids = sorted(str(u["unit_id"]) for u in out)
    assert ids == ["101", "305", "A-207", "B-207", "inferred_xyz"]


def test_emit_handles_empty_units_list() -> None:
    assert _emit_v2_units_for_property([]) == []


def test_emit_returns_same_list_object_when_no_dedup_needed() -> None:
    """Empty/already-clean lists must be a no-op pass-through. Important
    so the per-property unit count stays semantically identical when
    no dups exist."""
    units = [_unit(unit_id="101", floor_plan_id="fp1"), _unit(unit_id="102", floor_plan_id="fp2")]
    out = _emit_v2_units_for_property(units)
    assert len(out) == 2
    assert out[0]["unit_id"] == "101"
    assert out[1]["unit_id"] == "102"


# ─────────────────────────────────────────────────────────────────────
# P2b — Floor-plan-id disambiguation (DIFFERENT_FLOOR_PLAN cohort)
#
# 87b837b QC found 1,035 cases / 19.5% of real-ID dups in this class.
# Dominated by:
#   • TIER_1_DOM_APPFOLIO_VANITY (794 = 77%) — fp_name = street address,
#     two distinct physical units share a unit-number ("3").
#   • TIER_1_KNOCK_API (48) — Knock CRM reuses unit_id across plans.
#   • TIER_1_DOM_RENTCAFE_NESTIN (34) — cross-bldg with null building.
# ─────────────────────────────────────────────────────────────────────


def test_p2b_appfolio_address_in_floor_plan_name_case() -> None:
    """Canonical AppFolio VANITY case (concordemgmt.com pid=262310):
    two units with unit_id="3" at different street addresses. Each
    has a distinct floor_plan_id (hashed from fp_name+beds+baths).
    Expected: both rewritten so neither collides on bare "3"."""
    a = _unit(unit_id="3", floor_plan_id="b7d98132e532abcd",
              floor_plan_name="712 S 11th St #3, Lincoln, NE 68508")
    b = _unit(unit_id="3", floor_plan_id="5e3950bc57d1efff",
              floor_plan_name="2224 A St - #3, Lincoln, NE 68502")
    units = [a, b]
    rewritten = _apply_p2b_floor_plan_id_disambiguation(units)
    assert rewritten == 2
    assert {u["unit_id"] for u in units} == {"b7d98132-3", "5e3950bc-3"}


def test_p2b_knock_cross_plan_collision_case() -> None:
    """Knock CRM case (broadwaypalaceapartments.com pid=64390): same
    unit_id "432" used by genuinely different plans B1 + CL. Both
    survive with distinct unit_ids prefixed by their fp_id hash."""
    a = _unit(unit_id="432", floor_plan_id="d926592c2902",
              floor_plan_name="B1 - 1 bed/1 bath (South)",
              beds=1, baths=1.0, area=730, rent_low=2339.0, rent_high=2339.0)
    b = _unit(unit_id="432", floor_plan_id="359a5882824f",
              floor_plan_name="CL",
              beds=2, baths=2.0, area=983, rent_low=2689.0, rent_high=2689.0)
    units = [a, b]
    rewritten = _apply_p2b_floor_plan_id_disambiguation(units)
    assert rewritten == 2
    # First 8 chars of each fp_id: "d926592c" and "359a5882"
    assert {u["unit_id"] for u in units} == {"d926592c-432", "359a5882-432"}


def test_p2b_skips_when_any_fp_id_empty() -> None:
    """Conservative gate: any null/empty fp_id → no rewrite. Same logic
    as P2 — partial knowledge isn't safe to act on."""
    a = _unit(unit_id="7", floor_plan_id="abc12345")
    b = _unit(unit_id="7", floor_plan_id="")
    c = _unit(unit_id="7", floor_plan_id=None)
    units = [a, b, c]
    rewritten = _apply_p2b_floor_plan_id_disambiguation(units)
    assert rewritten == 0
    assert all(u["unit_id"] == "7" for u in units)


def test_p2b_skips_when_all_fp_ids_same() -> None:
    """If both rows have fp_id "abc12345", that's same-plan dup territory
    — EXACT_DUPE handles it. Don't rewrite (would make "abc12345-7" /
    "abc12345-7" which is the same thing twice and still collides)."""
    a = _unit(unit_id="7", floor_plan_id="abc12345")
    b = _unit(unit_id="7", floor_plan_id="abc12345")
    units = [a, b]
    rewritten = _apply_p2b_floor_plan_id_disambiguation(units)
    assert rewritten == 0


def test_p2b_skips_single_occurrence() -> None:
    units = [_unit(unit_id="7", floor_plan_id="abc12345")]
    rewritten = _apply_p2b_floor_plan_id_disambiguation(units)
    assert rewritten == 0
    assert units[0]["unit_id"] == "7"


def test_p2b_skips_inferred_ids() -> None:
    a = _unit(unit_id="inferred_xxx", floor_plan_id="fp1")
    b = _unit(unit_id="inferred_xxx", floor_plan_id="fp2")
    units = [a, b]
    rewritten = _apply_p2b_floor_plan_id_disambiguation(units)
    assert rewritten == 0


def test_p2b_uses_first_8_chars_only() -> None:
    """Stability/readability invariant: longer fp_ids get truncated to
    8 chars so the resulting unit_id remains human-readable."""
    a = _unit(unit_id="X", floor_plan_id="abcdefgh-9999-0000-aaaa")
    b = _unit(unit_id="X", floor_plan_id="00000000-deaddead-0000-bbbb")
    units = [a, b]
    _apply_p2b_floor_plan_id_disambiguation(units)
    # Both prefixes are exactly 8 chars long
    for u in units:
        prefix = u["unit_id"].split("-")[0]
        assert len(prefix) == 8
    assert {u["unit_id"] for u in units} == {"abcdefgh-X", "00000000-X"}


def test_p2b_runs_after_p2_so_bldg_units_arent_double_prefixed() -> None:
    """If both bldg and fp_id are present and distinct, P2 wins (more
    semantic) and P2b sees already-rewritten ids that no longer collide.
    Specifically: P2 turns unit_id "207" into "1-207"/"2-207"; on the
    P2b pass those don't collide anymore so no double-prefix happens."""
    a = _unit(unit_id="207", building="1", floor_plan_id="fp1")
    b = _unit(unit_id="207", building="2", floor_plan_id="fp2")
    units = [a, b]
    out = _emit_v2_units_for_property(units)
    # P2 rewrote to "1-207" / "2-207"; P2b finds no collision on those
    assert {u["unit_id"] for u in out} == {"1-207", "2-207"}
    # NOT double-prefixed like "fp1-1-207"
    assert all("-1-" not in u["unit_id"] or u["unit_id"] == "1-207" for u in out)
    assert all("-2-" not in u["unit_id"] or u["unit_id"] == "2-207" for u in out)


def test_p2b_drops_exact_dupes_when_fp_ids_identical() -> None:
    """When floor_plan_ids are the SAME but other fields are too,
    P2b skips and P1 cleans them up. End-to-end: 2 identical units →
    one survives."""
    a = _unit(unit_id="7", floor_plan_id="abc12345")
    b = _unit(unit_id="7", floor_plan_id="abc12345")  # exact dupe
    units = [a, b]
    out = _emit_v2_units_for_property(units)
    assert len(out) == 1


def test_p2b_combined_realistic_appfolio_multi_address() -> None:
    """End-to-end AppFolio multi-address case: a small-PMC site with
    3 properties, each with a unit "3" at a different address. Without
    P2b they'd collide; with P2b each gets a distinct fp_id-prefixed
    id and all 3 survive."""
    units = [
        _unit(unit_id="3", floor_plan_id="aaaaaaaa-1111",
              floor_plan_name="712 S 11th St #3, Lincoln, NE"),
        _unit(unit_id="3", floor_plan_id="bbbbbbbb-2222",
              floor_plan_name="2224 A St #3, Lincoln, NE"),
        _unit(unit_id="3", floor_plan_id="cccccccc-3333",
              floor_plan_name="901 Oak Ave #3, Lincoln, NE"),
    ]
    out = _emit_v2_units_for_property(units)
    assert len(out) == 3
    assert {u["unit_id"] for u in out} == {"aaaaaaaa-3", "bbbbbbbb-3", "cccccccc-3"}
