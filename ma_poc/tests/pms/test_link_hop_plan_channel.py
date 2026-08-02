"""A plan-only link-hop must not lose its floor plans.

`promote_verified_unit_rows` admits a row into ``units`` only when it carries a
native apartment anchor, and moves every unanchored plan row to
``plan_summaries``. A hopped page can therefore be a perfectly good floor-plan
surface while ``units`` is empty.

Two independent leaks existed, both introduced by f46a490 wiring that new
channel without teaching its consumers:

  A. ``_try_link_hop`` hit ``if not had_data: continue`` and dropped the whole
     ``sub_result`` — the only object holding those plan rows. Its comment
     claimed they "stay attached to the caller's result"; they do not, because
     ``sub_result`` is the HOPPED page's dict while the caller holds the ENTRY
     page's (empty, for a barren homepage).
  B. ``scrape_jugnu``'s post-hop merge copies an explicit key whitelist that
     includes ``units`` and not ``plan_summaries``, and its ``_units_empty``
     branch takes only ``_explored_links``. So ``floor_plans[]`` in the emitted
     v2 record was structurally unreachable for any link-hop-recovered property.

Before f46a490 neither could bite: adapters wired ``result.units =
pp.admitted`` (units AND plan rows together), so ``had_data`` was true for any
page with rows at all.

These tests target the two pure helpers that carry the fix, so they need no
network, no browser and no event loop. The orchestration around them is covered
by the existing link-hop integration tests.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

from ma_poc.pms import scraper as _scraper_mod
from ma_poc.pms.scraper import _attach_hop_plans, _hop_plan_identity

_SCRAPER_SRC = Path(inspect.getsourcefile(_scraper_mod) or "")


class TestDiscardSiteHarvests:
    """Leak A lives in async orchestration, so guard it at the source level.

    The pure-helper tests below cannot reach ``_try_link_hop``'s
    ``if not had_data: continue``, and a mutation run proved it: restoring the
    bare ``continue`` left every other test in this file green. Driving the real
    coroutine would need a fetch stub, adapter dispatch and an event loop — a
    lot of machinery to assert one branch does not throw data away. So assert
    the invariant structurally instead: whatever that branch does, it must not
    reach ``continue`` without first reading ``plan_summaries``.
    """

    @staticmethod
    def _not_had_data_branches() -> list[ast.If]:
        """Every ``if not had_data:`` statement in the module."""
        tree = ast.parse(_SCRAPER_SRC.read_text(encoding="utf-8"))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            t = node.test
            if (
                isinstance(t, ast.UnaryOp)
                and isinstance(t.op, ast.Not)
                and isinstance(t.operand, ast.Name)
                and t.operand.id == "had_data"
            ):
                found.append(node)
        return found

    def test_the_branch_still_exists(self) -> None:
        """Guard the guard — a renamed variable must fail loudly, not vacuously."""
        branches = self._not_had_data_branches()
        assert branches, (
            "no `if not had_data:` branch found in scraper.py. The plan-only hop "
            "gate was renamed or removed; update this guard so it keeps checking "
            "the real branch instead of passing on an empty set."
        )

    def test_plan_rows_are_harvested_before_continue(self) -> None:
        """A plan-only hop must not be discarded without keeping its plans.

        ``sub_result`` is the hopped page's own dict and the only place those
        rows exist — no post-hop copy list reconstructs them.
        """
        for branch in self._not_had_data_branches():
            body = ast.unparse(ast.Module(body=branch.body, type_ignores=[]))
            assert "plan_summaries" in body, (
                "`if not had_data:` discards the hop result without harvesting "
                f"plan_summaries. Branch body:\n{body}\n"
                "sub_result is the HOPPED page's dict; the caller holds the "
                "ENTRY page's plan rows, so dropping it loses them outright."
            )


def _plan(name: str, **over: Any) -> dict[str, Any]:
    """A floor-plan row in the ADAPTER vocabulary (make_unit_dict's output).

    Deliberately uses ``bedrooms`` / ``bathrooms`` / ``sqft`` /
    ``market_rent_low`` — the names ``_parsing.make_unit_dict`` actually writes.
    A key that reads only ``beds`` / ``area`` / ``rent_low`` degenerates to a
    constant here, which is the live defect in ``_plan_key``.
    """
    row = {
        "floor_plan_name": name,
        "bedrooms": 1,
        "bathrooms": 1,
        "sqft": 798,
        "market_rent_low": 1298,
        "market_rent_high": 1298,
    }
    row.update(over)
    return row


class TestHopPlanIdentity:
    """The dedup key must not be dead on either field vocabulary."""

    def test_reads_the_adapter_vocabulary(self) -> None:
        """bedrooms/bathrooms/sqft/market_rent_low must all reach the key.

        Regression guard for the ``_plan_key`` failure mode: reading only the
        v2 spellings leaves the rent and area slots empty for every row that
        came straight from an adapter.
        """
        ident = _hop_plan_identity(_plan("A1"))
        assert ident == ("A1", "1", "1", "798", "1298", "1298"), ident
        assert "" not in ident, (
            f"a slot is dead on adapter-vocabulary rows: {ident} — the key is "
            "reading field names make_unit_dict does not write"
        )

    def test_reads_the_v2_vocabulary(self) -> None:
        """beds/baths/area/rent_low must also reach the key."""
        v2 = {
            "floor_plan_name": "A1",
            "beds": 1,
            "baths": 1,
            "area": 798,
            "rent_low": 1298,
            "rent_high": 1298,
        }
        assert _hop_plan_identity(v2) == ("A1", "1", "1", "798", "1298", "1298")

    def test_rent_is_part_of_identity(self) -> None:
        """Same plan shape at two different rents = two published offers.

        This is the exact collapse ``_plan_key`` causes: its rent slot is dead,
        so these two rows share a key and one is destroyed.
        """
        a = _plan("A1", market_rent_low=1299, market_rent_high=1299)
        b = _plan("A1", market_rent_low=1499, market_rent_high=1499)
        assert _hop_plan_identity(a) != _hop_plan_identity(b)


class TestAttachHopPlans:
    """Harvested plan rows must reach the result without clobbering it."""

    def test_harvest_reaches_an_empty_result(self) -> None:
        """The dominant real case: barren entry page, plan-rich hop page."""
        result: dict[str, Any] = {"units": []}
        _attach_hop_plans(result, [_plan("A1"), _plan("B2")])
        assert [p["floor_plan_name"] for p in result["plan_summaries"]] == ["A1", "B2"]
        assert result["_hop_plan_harvest"] == 2

    def test_entry_page_plans_are_not_discarded(self) -> None:
        """Union, not overwrite.

        Adding ``plan_summaries`` to the merge's key-whitelist would have done
        ``result[k] = hop_result[k]`` and destroyed the entry page's own plans.
        """
        result: dict[str, Any] = {"plan_summaries": [_plan("ENTRY")]}
        _attach_hop_plans(result, [_plan("HOP")])
        assert [p["floor_plan_name"] for p in result["plan_summaries"]] == [
            "ENTRY",
            "HOP",
        ]

    def test_exact_duplicates_collapse(self) -> None:
        """The same plan on both pages ships once."""
        result: dict[str, Any] = {"plan_summaries": [_plan("A1")]}
        _attach_hop_plans(result, [_plan("A1")])
        assert len(result["plan_summaries"]) == 1

    def test_empty_harvest_is_a_no_op(self) -> None:
        """A hop that found real units must be handed back untouched."""
        original = [_plan("KEEP")]
        result: dict[str, Any] = {"plan_summaries": original}
        _attach_hop_plans(result, [])
        assert result["plan_summaries"] is original
        assert "_hop_plan_harvest" not in result

    @pytest.mark.parametrize("junk", [None, "not-a-dict", 42])
    def test_non_dict_rows_are_ignored(self, junk: Any) -> None:
        """Never raise on a malformed row — this runs inside the scrape path."""
        result: dict[str, Any] = {"plan_summaries": [junk]}
        _attach_hop_plans(result, [_plan("A1")])
        names = [
            p.get("floor_plan_name") for p in result["plan_summaries"] if isinstance(p, dict)
        ]
        assert "A1" in names

    def test_rs365_dedicated_plan_replaces_generic_same_name_only(self) -> None:
        generic = _plan(
            "Greenwood",
            market_rent_low=9999,
            market_rent_high=9999,
            extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT",
        )
        dedicated = _plan(
            "Greenwood",
            market_rent_low=1406,
            market_rent_high=1512,
            extraction_tier="TIER_1_DOM_365RESIDENTSERVICES_PLAN_LEVEL",
            source_ids={
                "rs365_floorplan_guid": "6f852f38-fad8-41dc-a594-dda77320fc32"
            },
        )
        unrelated = _plan(
            "Greenwood Plus",
            extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT",
        )
        result: dict[str, Any] = {"plan_summaries": [generic, unrelated]}

        _attach_hop_plans(result, [dedicated])

        assert result["plan_summaries"] == [unrelated, dedicated]

    def test_exact_aspensquare_catalogue_suppresses_generic_hop_extras(self) -> None:
        exact = [
            _plan(
                "The Duke",
                bedrooms=1,
                extraction_tier="TIER_1_DOM_ASPENSQUARE_NEXT",
                source_ids={"aspensquare_floor_plan_id": "2"},
            ),
            _plan(
                "The Essex",
                bedrooms=2,
                extraction_tier="TIER_1_DOM_ASPENSQUARE_NEXT",
                source_ids={"aspensquare_floor_plan_id": "4"},
            ),
            _plan(
                "The Monarch",
                bedrooms=3,
                extraction_tier="TIER_1_DOM_ASPENSQUARE_NEXT",
                source_ids={"aspensquare_floor_plan_id": "6"},
            ),
        ]
        generic = [
            _plan("2 Bedroom", extraction_tier="TIER_2_JSONLD"),
            _plan("3 Bedroom", extraction_tier="TIER_2_JSONLD"),
        ]
        result: dict[str, Any] = {"plan_summaries": exact}

        _attach_hop_plans(result, generic)

        assert [row["floor_plan_name"] for row in result["plan_summaries"]] == [
            "The Duke",
            "The Essex",
            "The Monarch",
        ]
        assert result["_hop_plan_harvest_suppressed"] == 2

    def test_exact_edifice_catalogue_suppresses_sibling_generic_shapes(self) -> None:
        exact = _plan(
            "S5",
            bedrooms=0,
            sqft=500,
            extraction_tier="TIER_1_API_EDIFICECMS",
            source_ids={"edifice_plan_id": "S5"},
        )
        turtle_i_duplicate = _plan(
            "", sqft=500, extraction_tier="TIER_1_API_GENERIC"
        )
        turtle_ii_only = _plan(
            "", sqft=650, extraction_tier="TIER_3_DOM_GENERIC_PLAN_LEVEL"
        )
        result: dict[str, Any] = {
            "extraction_tier_used": "TIER_1_API_EDIFICECMS",
            "plan_summaries": [exact],
        }

        _attach_hop_plans(result, [turtle_i_duplicate, turtle_ii_only])

        assert result["plan_summaries"] == [exact]
        assert result["_hop_plan_harvest_suppressed"] == 2

    def test_marketapts_unit_winner_suppresses_generic_deposit_plans(self) -> None:
        riverbank_generic = [
            _plan(
                "Plan1",
                bedrooms="",
                sqft="",
                market_rent_low=1000,
                market_rent_high=1000,
                extraction_tier="TIER_3_DOM_GENERIC_PLAN_LEVEL",
            ),
            _plan(
                "Plan2",
                bedrooms="",
                sqft="",
                market_rent_low=1000,
                market_rent_high=1000,
                extraction_tier="TIER_3_DOM_GENERIC_PLAN_LEVEL",
            ),
        ]
        result: dict[str, Any] = {
            "extraction_tier_used": "TIER_1_DOM_MARKETAPTS_D_UNIT_LEVEL",
            "units": [{"unit_number": "20-361", "floor_plan_name": "Plan2"}],
            "plan_summaries": [],
        }

        _attach_hop_plans(result, riverbank_generic)

        assert result["plan_summaries"] == []
        assert result["_hop_plan_harvest_suppressed"] == 2

    def test_marketapts_keeps_exact_no_unit_plans_only(self) -> None:
        exact = _plan(
            "1X1-CC",
            extraction_tier="TIER_1_DOM_MARKETAPTS",
            market_rent_low=None,
            market_rent_high=None,
        )
        generic = _plan(
            "1 Bedroom",
            extraction_tier="TIER_3_DOM_GENERIC_PLAN_LEVEL",
        )
        result: dict[str, Any] = {
            "extraction_tier_used": "TIER_1_DOM_MARKETAPTS_B_PLAN_LEVEL",
            "plan_summaries": [exact],
        }

        _attach_hop_plans(result, [generic])

        assert result["plan_summaries"] == [exact]
        assert result["_hop_plan_harvest_suppressed"] == 1

    def test_rentcafe_layout_tab_unit_winner_suppresses_generic_plans(self) -> None:
        generic = [
            _plan(
                "1 Bedroom",
                bedrooms=1,
                market_rent_low=1800,
                market_rent_high=1800,
                extraction_tier="TIER_3_DOM_GENERIC_PLAN_LEVEL",
            )
        ]
        result: dict[str, Any] = {
            "extraction_tier_used": "TIER_1_DOM_RENTCAFE_LT",
            "units": [
                {
                    "unit_number": "219H",
                    "floor_plan_name": "1BR/1BA",
                    "availability_date": "9/1/2026",
                }
            ],
            "plan_summaries": [],
        }

        _attach_hop_plans(result, generic)

        assert result["plan_summaries"] == []
        assert result["_hop_plan_harvest_suppressed"] == 1

    def test_rentcafe_layout_tab_keeps_exact_empty_plan_only(self) -> None:
        exact = _plan(
            "Penthouse",
            extraction_tier="TIER_1_DOM_RENTCAFE_LT",
            market_rent_low=5000,
            market_rent_high=5000,
        )
        generic = _plan(
            "3 Bedroom",
            extraction_tier="TIER_3_DOM_GENERIC_PLAN_LEVEL",
        )
        result: dict[str, Any] = {
            "extraction_tier_used": "TIER_1_DOM_RENTCAFE_LT",
            "plan_summaries": [exact],
        }

        _attach_hop_plans(result, [generic])

        assert result["plan_summaries"] == [exact]
        assert result["_hop_plan_harvest_suppressed"] == 1


class TestPromoteDoesNotCollapseDistinctOffers:
    """The plan-merge de-dup must not destroy a differently-priced offer.

    ``promote_verified_unit_rows`` merges plan rows keyed on its inner
    ``_plan_key``, and a collision DELETES a row. That key used to read
    ``floor_plan_id`` and ``asking_rent`` / ``rent_low`` — none of which
    ``make_unit_dict`` writes (it writes ``market_rent_low`` and no plan id) —
    so two of its seven slots were a constant empty string and the key
    effectively became (name, beds, baths, area).

    Measured on live output, not invented: Rosewood Commons (257324) publishes
    two ``2 Bedroom / 2 Bath`` offers at $1,695 and $1,640, identical on
    beds/baths/area. Pre-fix the run shipped ONE of them. A real advertised
    price was being deleted with no missing-data signal.
    """

    def test_two_offers_differing_only_in_rent_both_survive(self) -> None:
        """Rosewood's exact shape, built through the real adapter helper."""
        from ma_poc.pms.adapters._parsing import make_unit_dict
        from ma_poc.pms.adapters.base import AdapterResult
        from ma_poc.pms.scraper import promote_verified_unit_rows

        rows = [
            make_unit_dict(
                floor_plan_name="2 Bedroom / 2 Bath",
                bedrooms=2,
                bathrooms=2,
                rent_low=rent,
                rent_high=rent,
            )
            for rent in (1695, 1640)
        ]
        adapter_result = AdapterResult(units=list(rows))
        adapter_result.tier_used = "TIER_1_DOM_GENERIC_PLAN_TEXT"

        promote_verified_unit_rows(adapter_result, property_id="257324")

        rents = sorted(
            p.get("market_rent_low") for p in adapter_result.plan_summaries
        )
        assert rents == [1640, 1695], (
            f"expected both published offers to survive, got {rents}. A collision "
            "in _plan_key deleted one — check that every slot of the key reads "
            "the field names make_unit_dict actually writes."
        )
