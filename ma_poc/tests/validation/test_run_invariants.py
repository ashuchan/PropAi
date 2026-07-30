"""Run-level invariants: cross-property payload collisions and envelope drift.

Both checks exist because a real defect reached production and was found by
accident. Their value is entirely in what they catch WITHOUT anyone looking, so
the tests below pair every positive case with a negative one — a detector that
fires on legitimate data gets muted, and a muted detector is worse than none.

Field vocabulary is exercised deliberately on both sides. Adapter rows carry
`bedrooms`/`sqft`/`market_rent_low`; v2-formatted rows carry `beds`/`area`/
`rent_low`. A check that reads one silently degenerates to a constant on the
other, which is the live defect this codebase has hit three times.
"""

from __future__ import annotations

from typing import Any

import pytest

from ma_poc.validation.run_invariants import (
    find_envelope_drift,
    find_identical_payload_groups,
)


def _prop(
    pid: str,
    rows: list[dict[str, Any]],
    *,
    name: str = "P",
    pms: str = "appfolio",
    channel: str = "units",
) -> dict[str, Any]:
    return {
        "apartment_id": pid,
        "proj_name": name,
        channel: rows,
        "_meta": {"provenance": {"detected_pms": pms}},
    }


def _v2(area: int, rent: int) -> dict[str, Any]:
    return {"area": area, "rent_low": rent, "rent_high": rent, "beds": 1}


def _adapter(sqft: int, rent: int) -> dict[str, Any]:
    """Same numbers in the ADAPTER vocabulary."""
    return {"sqft": sqft, "market_rent_low": rent, "market_rent_high": rent, "bedrooms": 1}


class TestIdenticalPayloadGroups:
    def test_flags_two_properties_with_identical_numbers(self) -> None:
        """The Redwood shape: same inventory attributed to two properties."""
        rows = [_v2(1620, 2500), _v2(1709, 2181), _v2(1381, 1824)]
        groups = find_identical_payload_groups(
            [_prop("278139", list(rows)), _prop("77994", list(rows))]
        )
        assert len(groups) == 1
        assert set(groups[0].property_ids) == {"278139", "77994"}
        assert groups[0].n_rows == 3

    def test_differing_detection_is_marked_suspicious(self) -> None:
        """Identical output from DIFFERENT detection is the strongest tell.

        Redwood was detected rentcafe on one property and funnel on the other.
        """
        rows = [_v2(1620, 2500), _v2(1709, 2181), _v2(1381, 1824)]
        groups = find_identical_payload_groups(
            [
                _prop("278139", list(rows), pms="rentcafe"),
                _prop("77994", list(rows), pms="funnel"),
            ]
        )
        assert groups[0].is_suspicious

    def test_row_order_cannot_hide_a_collision(self) -> None:
        """The signature is sorted, so shuffling must not evade detection."""
        a = [_v2(1620, 2500), _v2(1709, 2181), _v2(1381, 1824)]
        groups = find_identical_payload_groups(
            [_prop("1", list(a)), _prop("2", list(reversed(a)))]
        )
        assert len(groups) == 1

    def test_both_vocabularies_collide_with_each_other(self) -> None:
        """Same numbers, different field names — still the same payload."""
        groups = find_identical_payload_groups(
            [
                _prop("1", [_v2(1620, 2500), _v2(1709, 2181), _v2(1381, 1824)]),
                _prop("2", [_adapter(1620, 2500), _adapter(1709, 2181), _adapter(1381, 1824)]),
            ]
        )
        assert len(groups) == 1, "the check is blind to one field vocabulary"

    def test_rows_in_the_floor_plans_channel_are_counted(self) -> None:
        """promote_verified_unit_rows moves plan rows to `floor_plans`."""
        rows = [_v2(1620, 2500), _v2(1709, 2181), _v2(1381, 1824)]
        groups = find_identical_payload_groups(
            [
                _prop("1", list(rows), channel="floor_plans"),
                _prop("2", list(rows), channel="units"),
            ]
        )
        assert len(groups) == 1

    # ---- negatives: a detector that cries wolf gets switched off ----

    def test_different_inventory_does_not_collide(self) -> None:
        groups = find_identical_payload_groups(
            [
                _prop("1", [_v2(1620, 2500), _v2(1709, 2181), _v2(1381, 1824)]),
                _prop("2", [_v2(700, 1200), _v2(800, 1300), _v2(900, 1400)]),
            ]
        )
        assert groups == []

    def test_a_single_shared_row_is_not_evidence(self) -> None:
        """Two small properties may legitimately list one unit at one price."""
        groups = find_identical_payload_groups(
            [_prop("1", [_v2(700, 1200)]), _prop("2", [_v2(700, 1200)])]
        )
        assert groups == []

    def test_rows_with_no_numbers_are_ignored(self) -> None:
        """Empty rows must not make every barren property collide."""
        blank = [{"floor_plan_name": "A"}, {"floor_plan_name": "B"}, {"floor_plan_name": "C"}]
        groups = find_identical_payload_groups(
            [_prop("1", list(blank)), _prop("2", list(blank))]
        )
        assert groups == []

    def test_absence_sentinel_is_not_a_value(self) -> None:
        """area=-1 means "not published"; it must not create a match."""
        rows = [{"area": -1, "rent_low": None}, {"area": -1, "rent_low": None},
                {"area": -1, "rent_low": None}]
        groups = find_identical_payload_groups(
            [_prop("1", list(rows)), _prop("2", list(rows))]
        )
        assert groups == []

    def test_allow_list_requires_the_exact_id_set(self) -> None:
        """Exemptions are per-group on purpose — no broad muting rule."""
        rows = [_v2(1620, 2500), _v2(1709, 2181), _v2(1381, 1824)]
        props = [_prop("1", list(rows)), _prop("2", list(rows))]
        assert find_identical_payload_groups(props, allow_list={frozenset({"1", "2"})}) == []
        assert find_identical_payload_groups(props, allow_list={frozenset({"1", "9"})}) != []


class TestEnvelopeDrift:
    def test_flags_a_narrowed_rent_ceiling(self) -> None:
        """222727's real shape: rent_high (1351,1952) -> (1351,1472).

        A client reads a ceiling $480 below what the operator advertises, with
        no missing-data signal at all.
        """
        prior = [_prop("222727", [_v2(618, 1351), _v2(1239, 1952)])]
        current = [_prop("222727", [_v2(618, 1351), _v2(1005, 1472)])]
        out = find_envelope_drift(current, prior)
        assert out and out[0].property_id == "222727"
        assert any("rent_high_envelope_narrowed" in f for f in out[0].findings), out[0].findings

    def test_flags_a_lost_bed_class(self) -> None:
        """37979: {4:4,2:3,3:4} -> {2:3,3:4,4:1} lost no class; a real loss must fire."""
        prior = [_prop("1", [{"beds": 2, "rent_low": 1000}, {"beds": 4, "rent_low": 2000}])]
        current = [_prop("1", [{"beds": 2, "rent_low": 1000}])]
        out = find_envelope_drift(current, prior)
        assert any("beds_class_lost" in f for f in out[0].findings), out[0].findings

    # ---- negatives ----

    def test_stable_envelope_is_silent(self) -> None:
        rows = [_v2(618, 1351), _v2(1239, 1952)]
        out = find_envelope_drift(
            [_prop("1", list(rows))], [_prop("1", list(rows))]
        )
        assert out == []

    def test_a_property_that_went_to_zero_is_not_reported_here(self) -> None:
        """Zero inventory is a VISIBLE condition; flagging it buries the silent case."""
        out = find_envelope_drift(
            [_prop("1", [])], [_prop("1", [_v2(618, 1351), _v2(1239, 1952)])]
        )
        assert out == []

    def test_a_new_property_is_not_reported(self) -> None:
        out = find_envelope_drift([_prop("new", [_v2(700, 1200)])], [])
        assert out == []

    @pytest.mark.parametrize("channel", ["units", "floor_plans"])
    def test_reads_both_channels(self, channel: str) -> None:
        prior = [_prop("1", [_v2(618, 1351), _v2(1239, 1952)], channel=channel)]
        current = [_prop("1", [_v2(618, 1351), _v2(1005, 1472)], channel=channel)]
        assert find_envelope_drift(current, prior)
