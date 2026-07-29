"""Task #70 — the positional half of the fallback unit_id must be DECLARED.

``compute_fallback_unit_id`` hashes a PLAN phenotype
(``canonical_id|floor_plan|beds|baths|sqft``), so N apartments on one plan mint
one id. ``jugnu._apply_p3_inferred_id_collision_suffix`` re-splits the
collision with a suffix hashed from the row's STABLE physical fields. When two
rows are identical on ALL of those, the split falls through to the row's
ORDINAL IN THE INPUT LIST — a property of this run's capture order, not of the
apartment. Measured on the 2026-07-27 run (104,964 rows): 381 such collision
groups, differing only on rent (374 groups), availability_status (42) and
available_date (24). No stable discriminator exists, so the id cannot be made
joinable — it can only stop pretending to be.

These tests pin, in order:
  * the instability itself (reorder / departure reassigns apartments),
  * that every member of an order-decided bucket carries
    ``UNIT_ID_POSITIONAL``, including the one keeping the bare base,
  * that a collision broken by a STABLE field is NOT flagged,
  * that no id changed (the flag is additive — zero re-key),
  * that fix #33 is not reintroduced (ids stay rent-independent),
  * that the token cannot be mistaken for a plan-level marker.
"""
from __future__ import annotations

from typing import Any

import pytest

from ma_poc.core.identity import (
    POSITIONAL_UNIT_ID_FLAG,
    append_data_quality_flag,
    unit_has_real_anchor,
)
from ma_poc.core.schema_v2 import _has_plan_marker, _is_floor_plan_level
from ma_poc.scripts.runners.jugnu import _apply_p3_inferred_id_collision_suffix


def _row(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "unit_id": "inferred_deadbeefdeadbeef",
        "floor_plan_name": "A1",
        "beds": 1,
        "baths": 1.0,
        "area": 700,
        "building": None,
        "floor": None,
        "rent_low": 1500.0,
        "rent_high": 1500.0,
        "availability_status": "AVAILABLE",
        "data_quality_flag": None,
    }
    base.update(kw)
    return base


def _flagged(u: dict[str, Any]) -> bool:
    return POSITIONAL_UNIT_ID_FLAG in str(u.get("data_quality_flag") or "").split("|")


# ── the defect: the id follows the LIST POSITION, not the apartment ──────────


def test_input_order_reassigns_apartments_between_ids() -> None:
    """Same three apartments, different capture order → 2 of 3 swap ids.

    This is the cross-run corruption: the daily join reads the swap as a rent
    change on a unit that never moved.
    """
    apartments = [
        _row(rent_low=1592.0, rent_high=1592.0, availability_status="AVAILABLE"),
        _row(rent_low=1592.0, rent_high=1592.0, availability_status="UNAVAILABLE"),
        _row(rent_low=1594.0, rent_high=1594.0, availability_status="UNAVAILABLE"),
    ]

    def ids_for(order: list[int]) -> dict[tuple[Any, Any], str]:
        rows = [dict(apartments[i]) for i in order]
        _apply_p3_inferred_id_collision_suffix(rows)
        return {(r["rent_low"], r["availability_status"]): r["unit_id"] for r in rows}

    forward = ids_for([0, 1, 2])
    reversed_ = ids_for([2, 1, 0])
    moved = [k for k in forward if forward[k] != reversed_[k]]
    assert len(moved) == 2, (
        "the positional tiebreak is expected to reassign on reorder; if this "
        "ever becomes 0 the id has been made order-independent and the flag "
        f"below should be revisited. forward={forward} reversed={reversed_}"
    )


def test_a_departing_sibling_shifts_every_later_ordinal() -> None:
    """One apartment leases → the survivors inherit the departed unit's ids."""
    apartments = [
        _row(rent_low=1592.0, availability_status="AVAILABLE"),
        _row(rent_low=1592.0, availability_status="UNAVAILABLE"),
        _row(rent_low=1594.0, availability_status="UNAVAILABLE"),
    ]
    day1 = [dict(a) for a in apartments]
    _apply_p3_inferred_id_collision_suffix(day1)
    day2 = [dict(a) for a in apartments[1:]]  # row 0 leased
    _apply_p3_inferred_id_collision_suffix(day2)
    assert day2[0]["unit_id"] == day1[0]["unit_id"], (
        "the surviving apartment inherits the departed one's id — that is the "
        "silent reassignment this task is about"
    )
    assert day2[0]["unit_id"] != day1[1]["unit_id"]


# ── the fix: the instability is declared on every affected row ───────────────


def test_every_member_of_an_order_decided_bucket_is_flagged() -> None:
    """Including the row that keeps the BARE base — it swaps too."""
    rows = [
        _row(rent_low=1592.0),
        _row(rent_low=1594.0),
        _row(rent_low=1596.0),
    ]
    _apply_p3_inferred_id_collision_suffix(rows)
    assert len({r["unit_id"] for r in rows}) == 3, "rows must stay distinct"
    assert all(_flagged(r) for r in rows), [r["data_quality_flag"] for r in rows]
    # the bare-base row is the one without a trailing ordinal
    bare = [r for r in rows if not r["unit_id"].endswith(("-1", "-2"))]
    assert len(bare) == 1 and _flagged(bare[0])


def test_collision_broken_by_a_stable_field_is_not_flagged() -> None:
    """Distinct area → the suffix is derived, not positional. No flag."""
    rows = [_row(area=700), _row(area=830), _row(area=960)]
    _apply_p3_inferred_id_collision_suffix(rows)
    assert len({r["unit_id"] for r in rows}) == 3
    assert not any(_flagged(r) for r in rows), [r["data_quality_flag"] for r in rows]


def test_mixed_group_flags_only_the_indistinguishable_bucket() -> None:
    """Two rows share area (positional); a third is stable-distinct (clean)."""
    rows = [_row(area=700, rent_low=1500), _row(area=700, rent_low=1600), _row(area=950)]
    _apply_p3_inferred_id_collision_suffix(rows)
    assert _flagged(rows[0]) and _flagged(rows[1])
    assert not _flagged(rows[2]), rows[2]["data_quality_flag"]


def test_solo_inferred_id_is_untouched_and_unflagged() -> None:
    rows = [_row()]
    assert _apply_p3_inferred_id_collision_suffix(rows) == 0
    assert rows[0]["unit_id"] == "inferred_deadbeefdeadbeef"
    assert not _flagged(rows[0])


def test_real_adapter_ids_are_never_flagged() -> None:
    """P3 skips non-``inferred_`` ids, so a real duplicate id gets no flag."""
    rows = [
        _row(unit_id="101", rent_low=1500),
        _row(unit_id="101", rent_low=1600),
    ]
    assert _apply_p3_inferred_id_collision_suffix(rows) == 0
    assert not any(_flagged(r) for r in rows)


# ── the flag is ADDITIVE: no id changed ─────────────────────────────────────


def test_flagging_did_not_re_key_anything() -> None:
    """Byte-exact ids for the two shapes the pass can emit.

    Replayed over the whole 2026-07-27 run (104,964 rows), the post-change pass
    reproduced the shipped id for every single row — 0 mismatches. These
    literals pin the same contract in-suite so a future edit to the suffix
    inputs cannot silently re-key the daily join.
    """
    positional = [_row(rent_low=1500), _row(rent_low=1600)]
    _apply_p3_inferred_id_collision_suffix(positional)
    assert positional[0]["unit_id"] == "inferred_deadbeefdeadbeef-e10d3f"
    assert positional[1]["unit_id"] == "inferred_deadbeefdeadbeef-e10d3f-1"

    stable = [_row(area=700), _row(area=830)]
    _apply_p3_inferred_id_collision_suffix(stable)
    assert stable[0]["unit_id"] == "inferred_deadbeefdeadbeef-e10d3f"
    assert stable[1]["unit_id"] == "inferred_deadbeefdeadbeef-2e1426"


def test_ids_stay_rent_independent_fix_33_not_reintroduced() -> None:
    """#33: an id must not move when rent moves.

    Both the STABLE-suffix path and the flagged POSITIONAL path are checked —
    a "fix" that ordered the positional bucket by rent would pass the first
    assertion and fail the second.
    """

    def stable_rows(r1: float, r2: float) -> list[dict[str, Any]]:
        return [_row(area=700, rent_low=r1, rent_high=r1),
                _row(area=830, rent_low=r2, rent_high=r2)]

    day1, day2 = stable_rows(1500, 1600), stable_rows(1900, 2000)
    _apply_p3_inferred_id_collision_suffix(day1)
    _apply_p3_inferred_id_collision_suffix(day2)
    assert [u["unit_id"] for u in day1] == [u["unit_id"] for u in day2]

    def positional_rows(r1: float, r2: float) -> list[dict[str, Any]]:
        return [_row(rent_low=r1, rent_high=r1), _row(rent_low=r2, rent_high=r2)]

    # rents cross over between the two days — a rent-ordered tiebreak would
    # swap the two ids here.
    p1, p2 = positional_rows(1500, 1600), positional_rows(2000, 1900)
    _apply_p3_inferred_id_collision_suffix(p1)
    _apply_p3_inferred_id_collision_suffix(p2)
    assert [u["unit_id"] for u in p1] == [u["unit_id"] for u in p2], (
        "the positional ordinal must not be derived from rent — that is fix "
        "#33's churn re-entering through the ordering instead of the hash"
    )


def test_flagging_is_idempotent_across_repeated_passes() -> None:
    rows = [_row(rent_low=1500), _row(rent_low=1600)]
    _apply_p3_inferred_id_collision_suffix(rows)
    first = [(r["unit_id"], r["data_quality_flag"]) for r in rows]
    # a second pass sees ids that no longer start a fresh collision group
    _apply_p3_inferred_id_collision_suffix(rows)
    assert [(r["unit_id"], r["data_quality_flag"]) for r in rows] == first


def test_existing_data_quality_tokens_are_preserved() -> None:
    rows = [
        _row(rent_low=1500, data_quality_flag="RENT_NOT_PUBLISHED"),
        _row(rent_low=1600, data_quality_flag="RENT_NOT_PUBLISHED|SQFT_NOT_PUBLISHED"),
    ]
    _apply_p3_inferred_id_collision_suffix(rows)
    assert rows[0]["data_quality_flag"] == f"RENT_NOT_PUBLISHED|{POSITIONAL_UNIT_ID_FLAG}"
    assert rows[1]["data_quality_flag"] == (
        f"RENT_NOT_PUBLISHED|SQFT_NOT_PUBLISHED|{POSITIONAL_UNIT_ID_FLAG}"
    )


# ── end-to-end: the flag reaches the SHIPPED row, not just the helper ───────


def test_flag_survives_the_real_emit_path() -> None:
    """Two same-plan rows through ``_format_v2_unit`` + the emit pipeline.

    Guards the wiring, not the predicate: P3 runs last inside
    ``_emit_v2_units_for_property`` and mutates already-formatted rows, so the
    token has to land on the ``data_quality_flag`` column the formatter emits.
    A future reordering of the P0/P2/P2b/P1/P3 passes, or a formatter that
    stopped emitting that column, would strand the declaration.
    """
    from datetime import datetime

    from ma_poc.scripts.runners.jugnu import (
        _emit_v2_units_for_property,
        _format_v2_unit,
    )

    ts = datetime(2026, 7, 29, 12, 0, 0)

    def adapter_row(rent: int) -> dict[str, Any]:
        return {
            "floor_plan_name": "A1",
            "beds": 1,
            "baths": 1.0,
            "sqft": 700,
            "market_rent_low": rent,
            "market_rent_high": rent,
            "availability_status": "AVAILABLE",
        }

    emitted = _emit_v2_units_for_property(
        [_format_v2_unit(adapter_row(r), ts, "P1") for r in (1500, 1600)]
    )
    assert len(emitted) == 2, "P1 must not collapse two differently-priced rows"
    assert len({u["unit_id"] for u in emitted}) == 2
    assert all(_flagged(u) for u in emitted), [u.get("data_quality_flag") for u in emitted]
    # …and the declaration must not have re-classified the rows as plan cards.
    assert not any(u.get("is_floor_plan_level") for u in emitted)


def test_the_token_cannot_reach_the_exact_equality_gate_readers() -> None:
    """``validation.schema_gate`` compares ``data_quality_flag`` with ``==``.

    ``_rent_gap_documented`` / ``_area_gap_documented`` / the SightMap
    plan-presence check are exact-equality reads, so ANY second token would
    flip them. They are safe here for a structural reason worth pinning: they
    run on the RAW adapter row (``pms.scraper`` + ``reporting.verdict``, both
    upstream of output formatting), while the token is appended to the
    formatter's OUTPUT copy. This test pins that the raw row is not mutated —
    if ``_format_v2_unit`` ever started formatting in place, the gate would
    start reading ``RENT_NOT_PUBLISHED|UNIT_ID_POSITIONAL`` and silently stop
    honouring documented rent gaps.
    """
    from datetime import datetime

    from ma_poc.scripts.runners.jugnu import (
        _emit_v2_units_for_property,
        _format_v2_unit,
    )
    from ma_poc.validation.schema_gate import _rent_gap_documented

    ts = datetime(2026, 7, 29, 12, 0, 0)
    raw_rows = [
        {
            "floor_plan_name": "A1", "beds": 1, "baths": 1.0, "sqft": 700,
            "market_rent_low": rent, "market_rent_high": rent,
            "availability_status": "AVAILABLE",
            "data_quality_flag": "RENT_NOT_PUBLISHED",
        }
        for rent in (1500, 1600)
    ]
    emitted = _emit_v2_units_for_property(
        [_format_v2_unit(r, ts, "P1") for r in raw_rows]
    )
    assert all(_flagged(u) for u in emitted), "the emitted rows carry the token"
    for raw in raw_rows:
        assert raw["data_quality_flag"] == "RENT_NOT_PUBLISHED", raw["data_quality_flag"]
        assert _rent_gap_documented(raw) is True


# ── the token must not be mistaken for a plan-level marker ──────────────────


def test_token_does_not_trip_the_plan_level_predicates() -> None:
    """A positional id says nothing about plan-vs-apartment.

    ``_is_floor_plan_level`` substring-matches ``PLAN_LEVEL``/``PLAN_PRESENCE``
    and ``_has_plan_marker`` prefix-matches ``PLAN_`` on each token; a token
    tripping either would flip 1,486 rows to plan-level as a side effect.
    """
    assert not _has_plan_marker("", POSITIONAL_UNIT_ID_FLAG)
    assert not _is_floor_plan_level(
        {"unit_number": "101", "data_quality_flag": POSITIONAL_UNIT_ID_FLAG}
    )
    assert unit_has_real_anchor(
        {"unit_number": "101", "data_quality_flag": POSITIONAL_UNIT_ID_FLAG}
    )


# ── the flag DECISION: table test, including must-NOT-flag rows ─────────────


@pytest.mark.parametrize(
    ("label", "rows", "expected"),
    [
        # ── MUST flag: the bucket's split was decided by list position ──────
        ("two rows differ on rent only",
         [{"rent_low": 1500}, {"rent_low": 1600}], [True, True]),
        ("three rows differ on rent only",
         [{"rent_low": 1}, {"rent_low": 2}, {"rent_low": 3}], [True, True, True]),
        ("differ on availability only",
         [{"availability_status": "AVAILABLE"}, {"availability_status": "UNAVAILABLE"}],
         [True, True]),
        ("differ on available_date only",
         [{"available_date": "2026-08-01"}, {"available_date": "2026-09-01"}],
         [True, True]),
        ("area absent on both (-1 sentinel), rent differs",
         [{"area": -1, "rent_low": 1500}, {"area": -1, "rent_low": 1600}], [True, True]),
        ("mixed bucket — 2 positional + 1 stable-distinct",
         [{"area": 700, "rent_low": 1}, {"area": 700, "rent_low": 2}, {"area": 950}],
         [True, True, False]),
        # ── MUST NOT flag ───────────────────────────────────────────────────
        ("a stable field (area) separates them", [{"area": 700}, {"area": 830}],
         [False, False]),
        ("a stable field (building) separates them",
         [{"building": "A"}, {"building": "B"}], [False, False]),
        ("a stable field (floor) separates them",
         [{"floor": 1}, {"floor": 2}], [False, False]),
        ("solo row — nothing collided", [{}], [False]),
        ("real adapter unit_ids are never touched by P3",
         [{"unit_id": "101", "rent_low": 1500}, {"unit_id": "101", "rent_low": 1600}],
         [False, False]),
        ("distinct phenotype hashes never form one group",
         [{"unit_id": "inferred_aaaaaaaaaaaaaaaa", "rent_low": 1500},
          {"unit_id": "inferred_bbbbbbbbbbbbbbbb", "rent_low": 1600}],
         [False, False]),
        ("unkeyable_* ids are out of P3's scope",
         [{"unit_id": "unkeyable_ffffffffffffffff", "rent_low": 1500},
          {"unit_id": "unkeyable_ffffffffffffffff", "rent_low": 1600}],
         [False, False]),
        ("null unit_id is out of P3's scope",
         [{"unit_id": None, "rent_low": 1500}, {"unit_id": None, "rent_low": 1600}],
         [False, False]),
    ],
)
def test_positional_flag_decision_table(
    label: str, rows: list[dict[str, Any]], expected: list[bool]
) -> None:
    built = [_row(**spec) for spec in rows]
    _apply_p3_inferred_id_collision_suffix(built)
    assert [_flagged(u) for u in built] == expected, (
        f"{label}: {[(u['unit_id'], u.get('data_quality_flag')) for u in built]}"
    )


# ── append_data_quality_flag: table test, including must-NOT-change rows ────


@pytest.mark.parametrize(
    ("existing", "token", "expected"),
    [
        # (1) empty field → just the token
        (None, "UNIT_ID_POSITIONAL", "UNIT_ID_POSITIONAL"),
        ("", "UNIT_ID_POSITIONAL", "UNIT_ID_POSITIONAL"),
        # (2) append preserves order
        ("QA_HELD", "UNIT_ID_POSITIONAL", "QA_HELD|UNIT_ID_POSITIONAL"),
        ("A|B", "UNIT_ID_POSITIONAL", "A|B|UNIT_ID_POSITIONAL"),
        # (3) idempotent — already present anywhere in the list
        ("UNIT_ID_POSITIONAL", "UNIT_ID_POSITIONAL", "UNIT_ID_POSITIONAL"),
        ("A|UNIT_ID_POSITIONAL|B", "UNIT_ID_POSITIONAL", "A|UNIT_ID_POSITIONAL|B"),
        # (4) empty / whitespace segments are dropped, never duplicated
        ("A||B", "UNIT_ID_POSITIONAL", "A|B|UNIT_ID_POSITIONAL"),
        ("  A  |  B  ", "UNIT_ID_POSITIONAL", "A|B|UNIT_ID_POSITIONAL"),
        # (5) must-NOT-match: an empty token changes nothing
        ("QA_HELD", "", "QA_HELD"),
        ("QA_HELD", "   ", "QA_HELD"),
        # (6) must-NOT-match: a DIFFERENT token that merely contains the flag
        #     as a substring is a separate token and is still appended
        ("PRE_UNIT_ID_POSITIONAL", "UNIT_ID_POSITIONAL",
         "PRE_UNIT_ID_POSITIONAL|UNIT_ID_POSITIONAL"),
        # (7) non-string junk in the field is not carried through
        (0, "UNIT_ID_POSITIONAL", "UNIT_ID_POSITIONAL"),
    ],
)
def test_append_data_quality_flag_table(
    existing: Any, token: str, expected: str
) -> None:
    unit: dict[str, Any] = {"data_quality_flag": existing}
    assert append_data_quality_flag(unit, token) == expected
    assert unit["data_quality_flag"] == (expected or None)
