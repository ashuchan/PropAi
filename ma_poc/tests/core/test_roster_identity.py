"""Tests for property-identity assurance.

Fixtures use the verbatim shapes observed in run ``2026-07-27-full-0d54ca7`` — the
AppFolio scattered-site address-as-floor-plan-name form from property 32716 (Porto Bella)
and the salted-synthetic-id form from the Redwood pair — so a regression that stops
catching the real cases fails here rather than in production.
"""

from __future__ import annotations

from typing import Any

import pytest
from ma_poc.core.roster_identity import (
    EVIDENCE_KEY,
    MAX_DISTINCT_LOCATIONS,
    PLANS_KEY,
    QUARANTINE_KEY,
    QUARANTINE_PLANS_KEY,
    SCOPE_AVAILABLE_ONLY,
    SCOPE_FULL_ROLL,
    SCOPE_KEY,
    SCOPE_UNKNOWN,
    UNVERIFIED_VERDICT,
    RosterVerdict,
    apply_roster_identity,
    find_roster_collisions,
    guard_property_record,
    parse_unit_locations,
    plan_level_rows,
    roster_fingerprint,
    roster_is_foreign,
    roster_scope,
    scope_rows_to_property,
    unit_level_rows,
)


def _unit(**kw: Any) -> dict[str, Any]:
    """A unit row with the fields the guard reads, overridable per test."""
    base: dict[str, Any] = {
        "unit_id": "101",
        "unit_name": None,
        "floor_plan_name": "A1",
        "rent_low": 1500.0,
        "area": 750,
        "beds": 1,
        "is_floor_plan_level": False,
    }
    base.update(kw)
    return base


# Verbatim from property 32716 — the address lives in floor_plan_name, not unit_id.
_PORTO_BELLA = [
    _unit(floor_plan_name="2045 South Haster Street #M-1, Anaheim, CA 92802", rent_low=2480.0),
    _unit(floor_plan_name="111 West Orangewood Avenue #N-1, Anaheim, CA 92802", rent_low=2770.0),
    _unit(floor_plan_name="821 West Stevens Avenue #15, Santa Ana, CA 92707", rent_low=3065.0),
    _unit(floor_plan_name="4945 Hayter Avenue, Lakewood, CA 90712", rent_low=2835.0),
    _unit(floor_plan_name="6390 De Longpre Avenue #1708, Los Angeles, CA 90028", rent_low=4045.0),
    _unit(floor_plan_name="2830 West Ball Road #Q-44, Anaheim, CA 92804", rent_low=2750.0),
]

# Verbatim shape from property 58321 — address in unit_id, hyphen-delimited.
_APPFOLIO_SCATTERED = [
    _unit(unit_id="10309-92nd-sw-07-tacoma-wa-98498"),
    _unit(unit_id="10214-lakeview-avenue-sw-52-lakewood-wa-98499"),
    _unit(unit_id="10402-walmart-blvd-se-yelm-wa-98597"),
    _unit(unit_id="1233-lake-park-dr-sw-23-tumwater-wa-98512"),
    _unit(unit_id="4923-dunham-drive-101-olympia-wa-98501"),
    _unit(unit_id="6390-de-longpre-avenue-1708-seattle-wa-98122"),
]


class TestParseUnitLocations:
    def test_parses_hyphenated_and_comma_forms(self) -> None:
        assert parse_unit_locations([_unit(unit_id="4923-dunham-drive-101-olympia-wa-98501")]) == [
            ("olympia", "wa", "98501")
        ]
        assert parse_unit_locations(
            [_unit(floor_plan_name="821 West Stevens Avenue #15, Santa Ana, CA 92707")]
        ) == [("santa ana", "ca", "92707")]

    def test_one_location_per_row_even_when_several_fields_carry_one(self) -> None:
        """A single unit must not inflate dispersion by repeating its address."""
        row = _unit(
            unit_id="4923-dunham-drive-101-olympia-wa-98501",
            unit_name="4923 Dunham Drive, Olympia, WA 98501",
            floor_plan_name="4923 Dunham Drive, Olympia, WA 98501",
        )
        assert len(parse_unit_locations([row])) == 1

    def test_ordinary_unit_numbers_yield_nothing(self) -> None:
        rows = [_unit(unit_id=n) for n in ("101", "1101", "B101", "01-101", "2-D1-1")]
        assert parse_unit_locations(rows) == []

    def test_plan_names_that_look_like_places_are_not_locations(self) -> None:
        """'Chatham' and 'Concord Point' are real floor plans at a Memphis property.

        The parser is anchored on a state+zip tail precisely so a bare place-like word
        cannot register as a location.
        """
        rows = [
            _unit(floor_plan_name=n)
            for n in ("Cape Cod", "Cape Poge", "Chatham", "Tenants Harbor", "Concord Point")
        ]
        assert parse_unit_locations(rows) == []


class TestRosterIsForeign:
    def test_flags_the_confirmed_porto_bella_shape(self) -> None:
        verdict = roster_is_foreign(_PORTO_BELLA)
        assert verdict is not None
        assert verdict.signal == "ADDRESS_DISPERSION"
        assert verdict.location_count > MAX_DISTINCT_LOCATIONS
        assert "anaheim" in verdict.reason

    def test_flags_the_confirmed_appfolio_scattered_shape(self) -> None:
        verdict = roster_is_foreign(_APPFOLIO_SCATTERED)
        assert verdict is not None
        assert verdict.parsed_rows == len(_APPFOLIO_SCATTERED)

    def test_single_metro_scattered_site_is_not_demoted(self) -> None:
        """Genuine scattered-site operators straddle a couple of adjacent municipalities."""
        rows = [
            _unit(unit_id="100-main-st-1-tacoma-wa-98402"),
            _unit(unit_id="102-main-st-2-tacoma-wa-98402"),
            _unit(unit_id="200-oak-ave-1-lakewood-wa-98499"),
            _unit(unit_id="202-oak-ave-2-lakewood-wa-98499"),
            _unit(unit_id="300-pine-rd-1-lakewood-wa-98499"),
            _unit(unit_id="302-pine-rd-2-tacoma-wa-98402"),
        ]
        assert roster_is_foreign(rows) is None

    def test_ordinary_roster_is_invisible_to_this_signal(self) -> None:
        rows = [_unit(unit_id=f"{100 + i}") for i in range(50)]
        assert roster_is_foreign(rows) is None

    def test_too_few_addresses_to_judge(self) -> None:
        """Two addresses in two cities is noise, not evidence."""
        rows = [
            _unit(unit_id="100-main-st-1-tacoma-wa-98402"),
            _unit(unit_id="200-oak-ave-1-miami-fl-33101"),
        ]
        assert roster_is_foreign(rows) is None

    def test_plan_level_rows_DO_count(self) -> None:
        """INVERTED 2026-07-28 (task #61/#63). This test previously asserted the opposite.

        It encoded a real bug, not a decision. The original rationale — "plan rows are
        labelled as plans and are not asserting per-apartment identity" — is sound for the
        roster *fingerprint*, where a generic ``A1 / $1500 / 750sqft`` card collides by
        chance between unrelated properties. It does not transfer to an *address*: a card
        named "2045 South Haster Street #M-1, Anaheim, CA 92802" asserts a location as
        plainly as a unit row does.

        And the exclusion was load-bearing in the wrong direction.
        ``promote_verified_unit_rows`` (ma_poc/pms/scraper.py) moves every row it cannot
        anchor to a native apartment id out of ``units`` and into ``plan_summaries``,
        which the runner emits as ``floor_plans[]``. A contaminated roster whose rows
        happen to lack anchors therefore left ``units`` empty and landed entirely in the
        plan channel, where the guard — filtering to unit-level rows before parsing
        locations — saw nothing and passed it as clean.

        Cost of the change, measured by replaying 2026-07-27-full-0d54ca7 (4,982
        properties, 5,427 plan-level rows): the flag set widened by ZERO properties. The
        inclusion is monotone (more rows can only add locations), so it can only ever
        widen, and on the only full run available it widened by nothing.
        """
        rows = [dict(u, is_floor_plan_level=True) for u in _PORTO_BELLA]
        verdict = roster_is_foreign(rows)
        assert verdict is not None
        assert verdict.signal == "ADDRESS_DISPERSION"

    def test_plan_level_rows_are_still_excluded_from_the_fingerprint(self) -> None:
        """The fingerprint's exclusion is correct and must survive the inversion above.

        Generic plan shapes collide between unrelated properties; that is exactly the
        false-positive mode the module docstring records as measured and rejected.
        """
        rows = [_unit(unit_id=str(i), rent_low=1000.0 + i) for i in range(6)]
        assert roster_fingerprint(rows) == roster_fingerprint(
            rows + [dict(_unit(), is_floor_plan_level=True, rent_low=1.0)]
        )

    def test_empty_and_none_are_safe(self) -> None:
        assert roster_is_foreign(None) is None
        assert roster_is_foreign([]) is None


class TestRosterFingerprint:
    def test_identity_is_excluded_so_salted_ids_still_collide(self) -> None:
        """The whole point: synthetic ids are salted per property.

        Property 278139 and 77994 held the identical roster but received different
        ``inferred_*`` ids for the same apartment. Hashing identity would let exactly the
        case we are hunting slip through.
        """
        a = [_unit(unit_id="inferred_b1e46d9383fef17e-917bd3", rent_low=2500.0, area=1620)]
        b = [_unit(unit_id="inferred_223c1b9194c0b14b-917bd3", rent_low=2500.0, area=1620)]
        a += [_unit(unit_id=f"inferred_aaa{i}", rent_low=2000.0 + i) for i in range(5)]
        b += [_unit(unit_id=f"inferred_bbb{i}", rent_low=2000.0 + i) for i in range(5)]
        assert roster_fingerprint(a) == roster_fingerprint(b)

    def test_row_order_does_not_matter(self) -> None:
        rows = [_unit(unit_id=str(i), rent_low=1000.0 + i) for i in range(6)]
        assert roster_fingerprint(rows) == roster_fingerprint(list(reversed(rows)))

    def test_differing_content_differs(self) -> None:
        a = [_unit(unit_id=str(i), rent_low=1000.0 + i) for i in range(6)]
        b = [_unit(unit_id=str(i), rent_low=9999.0) for i in range(6)]
        assert roster_fingerprint(a) != roster_fingerprint(b)

    def test_small_rosters_are_not_fingerprinted(self) -> None:
        assert roster_fingerprint([_unit()] * 4) is None
        assert roster_fingerprint(None) is None

    def test_plan_level_rows_excluded_from_the_hash(self) -> None:
        rows = [_unit(unit_id=str(i)) for i in range(6)]
        with_plans = rows + [dict(_unit(), is_floor_plan_level=True)]
        assert roster_fingerprint(rows) == roster_fingerprint(with_plans)


class TestFindRosterCollisions:
    @staticmethod
    def _prop(pid: str, units: list[dict[str, Any]]) -> dict[str, Any]:
        return {"apartment_id": pid, "units": units}

    def test_every_member_of_a_collision_group_is_flagged(self) -> None:
        shared = [_unit(unit_id=str(i), rent_low=1000.0 + i) for i in range(8)]
        props = [self._prop(p, list(shared)) for p in ("58321", "41738", "19712")]
        verdicts = find_roster_collisions(props)
        assert set(verdicts) == {"58321", "41738", "19712"}
        assert all(v.signal == "SHARED_ROSTER" for v in verdicts.values())

    def test_a_member_lists_the_others_but_not_itself(self) -> None:
        shared = [_unit(unit_id=str(i), rent_low=1000.0 + i) for i in range(8)]
        props = [self._prop(p, list(shared)) for p in ("a", "b", "c")]
        verdicts = find_roster_collisions(props)
        assert verdicts["a"].colliding_property_ids == ("b", "c")
        assert "a" not in verdicts["a"].colliding_property_ids

    def test_unique_rosters_are_absent(self) -> None:
        props = [
            self._prop("p1", [_unit(unit_id=str(i), rent_low=1000.0 + i) for i in range(8)]),
            self._prop("p2", [_unit(unit_id=str(i), rent_low=5000.0 + i) for i in range(8)]),
        ]
        assert find_roster_collisions(props) == {}

    def test_small_rosters_cannot_collide(self) -> None:
        """Two tiny properties of the same shape must not be demoted for it."""
        tiny = [_unit(unit_id="101"), _unit(unit_id="102")]
        props = [self._prop("p1", list(tiny)), self._prop("p2", list(tiny))]
        assert find_roster_collisions(props) == {}

    def test_pure_does_not_mutate_input(self) -> None:
        shared = [_unit(unit_id=str(i), rent_low=1000.0 + i) for i in range(8)]
        props = [self._prop(p, list(shared)) for p in ("a", "b")]
        before = repr(props)
        find_roster_collisions(props)
        assert repr(props) == before

    def test_empty_run(self) -> None:
        assert find_roster_collisions([]) == {}


class TestRosterVerdict:
    def test_location_count_reflects_distinct_triples(self) -> None:
        v = RosterVerdict(
            signal="ADDRESS_DISPERSION",
            reason="x",
            locations=(("tacoma", "wa", "98402"), ("olympia", "wa", "98501")),
        )
        assert v.location_count == 2

    def test_is_frozen(self) -> None:
        v = RosterVerdict(signal="SHARED_ROSTER", reason="x")
        with pytest.raises(Exception):
            v.signal = "other"  # type: ignore[misc]


class TestUnitLevelRows:
    def test_filters_plan_level(self) -> None:
        rows = [_unit(), dict(_unit(), is_floor_plan_level=True), _unit()]
        assert len(unit_level_rows(rows)) == 2

    def test_none_is_safe(self) -> None:
        assert unit_level_rows(None) == []

    def test_plan_level_rows_is_the_complement(self) -> None:
        rows = [_unit(), dict(_unit(), is_floor_plan_level=True), _unit()]
        assert len(plan_level_rows(rows)) == 1
        assert plan_level_rows(None) == []


class TestRosterScope:
    def test_full_roll_when_any_row_is_occupied(self) -> None:
        rows = [_unit(availability_status="AVAILABLE"), _unit(availability_status="UNAVAILABLE")]
        assert roster_scope(rows) == SCOPE_FULL_ROLL

    def test_available_only_when_none_occupied(self) -> None:
        rows = [_unit(availability_status="AVAILABLE") for _ in range(3)]
        assert roster_scope(rows) == SCOPE_AVAILABLE_ONLY

    def test_unknown_without_rows_or_status(self) -> None:
        assert roster_scope(None) == SCOPE_UNKNOWN
        assert roster_scope([]) == SCOPE_UNKNOWN
        assert roster_scope([_unit(availability_status=None)]) == SCOPE_UNKNOWN

    def test_plan_level_rows_ignored(self) -> None:
        rows = [_unit(availability_status="AVAILABLE"),
                dict(_unit(), is_floor_plan_level=True, availability_status="UNAVAILABLE")]
        assert roster_scope(rows) == SCOPE_AVAILABLE_ONLY


class TestApplyRosterIdentity:
    @staticmethod
    def _prop(pid: str, units: list[dict[str, Any]]) -> dict[str, Any]:
        return {"apartment_id": pid, "units": units}

    def test_foreign_roster_leaves_units_and_is_quarantined(self) -> None:
        props = [self._prop("p1", list(_PORTO_BELLA))]
        out, rep = apply_roster_identity(props)
        assert out[0]["units"] == []
        assert len(out[0][QUARANTINE_KEY]) == len(_PORTO_BELLA)
        assert rep.demoted_properties == 1
        assert rep.demoted_rows == len(_PORTO_BELLA)

    def test_demoted_property_gets_an_honest_verdict(self) -> None:
        out, _ = apply_roster_identity([self._prop("p1", list(_PORTO_BELLA))])
        assert out[0]["_meta"]["verdict"] == "UNIT_ROUTE_UNVERIFIED"
        assert "ADDRESS_DISPERSION" in out[0]["_meta"]["verdict_reason"]

    def test_shared_roster_demotes_every_member(self) -> None:
        shared = [_unit(unit_id=str(i), rent_low=1000.0 + i) for i in range(8)]
        props = [self._prop(p, list(shared)) for p in ("a", "b", "c")]
        out, rep = apply_roster_identity(props)
        assert rep.demoted_properties == 3
        assert all(o["units"] == [] for o in out)

    def test_occupied_units_are_KEPT_and_tagged_not_dropped(self) -> None:
        """The decision: contaminated rows are removed, occupied units are kept + labelled."""
        rows = [_unit(unit_id=str(i), availability_status="UNAVAILABLE") for i in range(5)]
        rows += [_unit(unit_id="99", availability_status="AVAILABLE")]
        out, rep = apply_roster_identity([self._prop("p1", rows)])
        assert len(out[0]["units"]) == 6            # nothing dropped
        assert out[0][SCOPE_KEY] == SCOPE_FULL_ROLL  # and clearly labelled
        assert rep.demoted_rows == 0

    def test_clean_property_is_untouched_apart_from_the_scope_tag(self) -> None:
        rows = [
            _unit(unit_id=str(100 + i), availability_status="AVAILABLE") for i in range(30)
        ]
        out, rep = apply_roster_identity([self._prop("p1", rows)])
        assert len(out[0]["units"]) == 30
        assert rep.demoted_properties == 0
        assert out[0][SCOPE_KEY] == SCOPE_AVAILABLE_ONLY

    def test_roster_without_availability_info_is_scoped_unknown(self) -> None:
        """Absence of availability data must not be reported as AVAILABLE_ONLY."""
        rows = [_unit(unit_id=str(100 + i)) for i in range(30)]  # fixture has no status
        out, _ = apply_roster_identity([self._prop("p1", rows)])
        assert out[0][SCOPE_KEY] == SCOPE_UNKNOWN

    def test_input_is_not_mutated(self) -> None:
        props = [self._prop("p1", list(_PORTO_BELLA))]
        before = repr(props)
        apply_roster_identity(props)
        assert repr(props) == before

    def test_scope_counts_cover_every_property(self) -> None:
        props = [self._prop("p1", [_unit(availability_status="UNAVAILABLE")]),
                 self._prop("p2", [_unit(availability_status="AVAILABLE")]),
                 self._prop("p3", [])]
        _, rep = apply_roster_identity(props)
        assert sum(rep.scope_counts.values()) == 3


class TestDemotionTargetsTheRecordJudged:
    """A verdict belongs to the record that earned it, not to everything sharing its id.

    ``apartment_id`` is optional, so ``str(prop.get(id_key))`` is ``"None"`` for every
    record that has not been resolved yet — a whole class of records collapsing onto one
    dict key. The pass keyed its verdicts by that string and then re-read them per record,
    so ONE foreign roster quarantined EVERY id-less property in the run:

        FOREIGN-scattered-site   units=0 quarantined=6  verdict='UNIT_ROUTE_UNVERIFIED'
        CLEAN-normal-portal      units=0 quarantined=2  verdict='UNIT_ROUTE_UNVERIFIED'

    That is data loss dressed as a safety guard — the clean property's real units were
    removed from inventory and it was marked as having no verified roster. Verdicts are now
    keyed by the record's position, which names exactly one record.
    """

    @staticmethod
    def _foreign_rows() -> list[dict[str, Any]]:
        """Six unit ids in six different cities — the AppFolio scattered-site shape."""
        return [
            _unit(unit_id="10309-92nd-sw-07-tacoma-wa-98498", rent_low=1000.0),
            _unit(unit_id="12-main-st-seattle-wa-98101", rent_low=1100.0),
            _unit(unit_id="44-oak-ave-spokane-wa-99201", rent_low=1200.0),
            _unit(unit_id="9-pine-rd-olympia-wa-98501", rent_low=1300.0),
            _unit(unit_id="8-elm-ct-everett-wa-98201", rent_low=1400.0),
            _unit(unit_id="7-cedar-ln-yakima-wa-98901", rent_low=1500.0),
        ]

    def test_clean_id_less_property_survives_a_foreign_id_less_sibling(self) -> None:
        clean_rows = [_unit(unit_id="101"), _unit(unit_id="102", rent_low=1600.0)]
        props = [
            {"apartment_id": None, "units": self._foreign_rows()},
            {"apartment_id": None, "units": clean_rows},
        ]

        out, rep = apply_roster_identity(props)

        assert out[0]["units"] == []                      # foreign roster: demoted
        assert len(out[0][QUARANTINE_KEY]) == 6
        assert len(out[1]["units"]) == 2                  # clean roster: untouched
        assert not out[1].get(QUARANTINE_KEY)
        assert out[1].get("_meta", {}).get("verdict") != UNVERIFIED_VERDICT
        assert rep.demoted_properties == 1
        assert rep.demoted_rows == 6

    def test_order_does_not_decide_who_is_demoted(self) -> None:
        """Clean-first must give the same answer as foreign-first."""
        clean_rows = [_unit(unit_id="101"), _unit(unit_id="102", rent_low=1600.0)]
        out, rep = apply_roster_identity(
            [
                {"apartment_id": None, "units": clean_rows},
                {"apartment_id": None, "units": self._foreign_rows()},
            ]
        )
        assert len(out[0]["units"]) == 2
        assert out[1]["units"] == []
        assert rep.demoted_properties == 1

    def test_shared_roster_between_two_id_less_properties_still_names_the_other(
        self,
    ) -> None:
        """Signal A must not lose its collision partner to id collapse.

        Grouping by id made two ``None``-id members of a collision group deduplicate into
        a group of one distinct id, so each verdict reported ``0 other properties`` — the
        evidence for its own demotion erased.
        """
        shared = [_unit(unit_id=str(i), rent_low=1000.0 + i) for i in range(8)]
        props = [{"apartment_id": None, "units": list(shared)} for _ in range(2)]

        out, rep = apply_roster_identity(props)

        assert rep.demoted_properties == 2
        assert all(o["units"] == [] for o in out)
        for record in out:
            evidence = record["_meta"][EVIDENCE_KEY]
            assert evidence["signal"] == "SHARED_ROSTER"
            assert evidence["colliding_property_ids"] == ["None"]



class TestPlanChannelIsCovered:
    """Task #61 item 2 — contamination routed into ``floor_plans[]`` must not escape.

    ``promote_verified_unit_rows`` sends every row it cannot anchor to a native apartment
    id into ``plan_summaries``, which is emitted as ``floor_plans[]``. Whether a
    contaminated roster lands in ``units`` or in ``floor_plans`` is therefore an accident
    of whether the parser found anchors — it says nothing about whose apartments they are.
    """

    def test_foreign_roster_living_only_in_floor_plans_is_caught(self) -> None:
        plans = [dict(u, is_floor_plan_level=True) for u in _PORTO_BELLA]
        record = {"apartment_id": "32716", "units": [], PLANS_KEY: plans}
        assert guard_property_record(record) is not None
        assert record[PLANS_KEY] == []
        assert len(record[QUARANTINE_PLANS_KEY]) == len(_PORTO_BELLA)

    def test_run_level_pass_also_sweeps_the_plan_channel(self) -> None:
        plans = [dict(u, is_floor_plan_level=True) for u in _PORTO_BELLA]
        out, rep = apply_roster_identity(
            [{"apartment_id": "32716", "units": [], PLANS_KEY: plans}]
        )
        assert out[0][PLANS_KEY] == []
        assert rep.demoted_plan_rows == len(_PORTO_BELLA)
        assert out[0]["_meta"]["verdict"] == UNVERIFIED_VERDICT

    def test_both_channels_leave_together(self) -> None:
        """A split roster must not have half of it survive."""
        record = {
            "apartment_id": "p1",
            "units": list(_PORTO_BELLA[:3]),
            PLANS_KEY: [dict(u, is_floor_plan_level=True) for u in _PORTO_BELLA[3:]],
        }
        assert guard_property_record(record) is not None
        assert record["units"] == [] and record[PLANS_KEY] == []
        assert len(record[QUARANTINE_KEY]) == 3
        assert len(record[QUARANTINE_PLANS_KEY]) == 3

    def test_clean_plan_cards_are_untouched(self) -> None:
        """The common shape — a SightMap plan card with no address — must not be swept."""
        record = {
            "apartment_id": "p1",
            "units": [_unit(unit_id=str(100 + i)) for i in range(10)],
            PLANS_KEY: [
                dict(_unit(floor_plan_name=n), is_floor_plan_level=True)
                for n in ("A1", "B2", "Chatham", "Concord Point")
            ],
        }
        assert guard_property_record(record) is None
        assert len(record[PLANS_KEY]) == 4
        assert QUARANTINE_PLANS_KEY not in record


class TestGuardPropertyRecord:
    """The per-record output boundary."""

    def test_returns_none_and_tags_scope_for_a_clean_record(self) -> None:
        record = {
            "apartment_id": "p1",
            "units": [_unit(unit_id=str(100 + i), availability_status="AVAILABLE")
                      for i in range(20)],
        }
        assert guard_property_record(record) is None
        assert len(record["units"]) == 20
        assert record[SCOPE_KEY] == SCOPE_AVAILABLE_ONLY

    def test_demotes_in_place_and_records_auditable_evidence(self) -> None:
        record = {"apartment_id": "32716", "units": list(_PORTO_BELLA)}
        verdict = guard_property_record(record)
        assert verdict is not None
        assert record["units"] == []
        assert len(record[QUARANTINE_KEY]) == len(_PORTO_BELLA)
        assert record["_meta"]["verdict"] == UNVERIFIED_VERDICT
        evidence = record["_meta"][EVIDENCE_KEY]
        assert evidence["signal"] == "ADDRESS_DISPERSION"
        assert evidence["parsed_rows"] == len(_PORTO_BELLA)
        assert len(evidence["locations"]) > MAX_DISTINCT_LOCATIONS

    def test_meta_object_identity_is_preserved(self) -> None:
        """Jugnu's ``_format_v2`` shares ``_meta`` with the in-process result dict.

        Replacing it instead of mutating it is Bug A — every verdict written after the
        formatter returns is silently dropped. Pinned here because the guard is the newest
        thing touching ``_meta`` inside that formatter.
        """
        meta: dict[str, Any] = {"canonical_id": "32716"}
        record = {"apartment_id": "32716", "units": list(_PORTO_BELLA), "_meta": meta}
        guard_property_record(record)
        assert record["_meta"] is meta
        assert meta["verdict"] == UNVERIFIED_VERDICT

    def test_is_idempotent(self) -> None:
        record = {"apartment_id": "32716", "units": list(_PORTO_BELLA)}
        guard_property_record(record)
        guard_property_record(record)
        assert len(record[QUARANTINE_KEY]) == len(_PORTO_BELLA)  # not doubled

    def test_a_record_demoted_at_the_formatter_still_gets_its_verdict_at_the_run_pass(
        self,
    ) -> None:
        """The two call sites must compose.

        After the formatter demotes, ``units`` is empty — the detector cannot re-derive
        the finding from data that is no longer there. Without the ``_meta`` evidence
        hand-off the run-level pass would silently report the property as clean.
        """
        record = {"apartment_id": "32716", "units": list(_PORTO_BELLA)}
        guard_property_record(record)
        record["_meta"]["verdict"] = "SUCCESS"  # what compute_verdict does afterwards
        out, rep = apply_roster_identity([record])
        assert out[0]["_meta"]["verdict"] == UNVERIFIED_VERDICT
        assert rep.demoted_properties == 1
        assert rep.demoted_rows == len(_PORTO_BELLA)  # counted, not double-moved
        assert len(out[0][QUARANTINE_KEY]) == len(_PORTO_BELLA)


class TestScopeRowsToProperty:
    """Prevention: filter an account-wide payload down to one property."""

    def test_keeps_only_this_property_and_reports_account_wide(self) -> None:
        res = scope_rows_to_property(
            _APPFOLIO_SCATTERED, city="Tacoma", zip_code="98498"
        )
        assert res.scopable is True
        assert res.is_account_wide is True
        assert len(res.kept) == 1
        assert len(res.dropped) == len(_APPFOLIO_SCATTERED) - 1

    def test_zip_plus_four_on_the_property_record_still_matches(self) -> None:
        res = scope_rows_to_property(
            _APPFOLIO_SCATTERED, city="Tacoma", zip_code="98498-1234"
        )
        assert len(res.kept) == 1

    def test_city_case_and_spacing_do_not_matter(self) -> None:
        rows = [_unit(unit_id="821-west-stevens-avenue-15-santa-ana-ca-92707")] * 5
        res = scope_rows_to_property(rows, city="  SANTA   ANA ", zip_code="92707")
        assert len(res.kept) == 5 and not res.dropped

    def test_unscopable_when_no_row_carries_an_address(self) -> None:
        """The critical case: caller must NOT fall back to emitting everything."""
        rows = [_unit(unit_id=str(100 + i)) for i in range(20)]
        res = scope_rows_to_property(rows, city="Tacoma", zip_code="98498")
        assert res.scopable is False
        assert res.kept == () and res.dropped == ()

    def test_unscopable_without_a_property_city_or_zip(self) -> None:
        assert scope_rows_to_property(_APPFOLIO_SCATTERED, city=None,
                                      zip_code="98498").scopable is False
        assert scope_rows_to_property(_APPFOLIO_SCATTERED, city="Tacoma",
                                      zip_code=None).scopable is False

    def test_single_property_account_keeps_everything_and_is_not_account_wide(self) -> None:
        rows = [_unit(unit_id=f"614-central-pkwy-{n}-new-braunfels-tx-78130") for n in
                (326, 226, 136, 411, 512)]
        res = scope_rows_to_property(rows, city="New Braunfels", zip_code="78130")
        assert res.scopable is True
        assert len(res.kept) == 5 and not res.dropped
        assert res.is_account_wide is False

    def test_rows_without_an_address_ride_along_when_payload_is_scopable(self) -> None:
        rows = list(_APPFOLIO_SCATTERED) + [_unit(unit_id="101")]
        res = scope_rows_to_property(rows, city="Tacoma", zip_code="98498")
        assert res.scopable is True
        assert any(r.get("unit_id") == "101" for r in res.kept)

    def test_empty_input(self) -> None:
        assert scope_rows_to_property(None, city="X", zip_code="12345").scopable is False
