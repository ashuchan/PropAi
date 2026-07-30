"""The modern Entrata theme ships its roster as `var unitsData` JSON, not DOM.

`parse_entrata_modern_units_data` is what turns those properties from plan-level
into unit-level (task #80 / the plan-level recovery program). These tests run
against three real captured `/conventional/` listing pages
(ma_poc/tests/fixtures/entrata_modern/) — one fee-transparency property
(Steeplechase) and two plain ones (Manor, Rise) — so the field mapping is pinned
to ground truth, not a synthetic blob.

The load-bearing assertion is the RENT one: fee-transparency properties expose
both a base rent and a fee-inclusive total, and shipping the total as the asking
rent is the #65 inversion this codebase already paid for once. The parser must
take `min_advertised_base_rent` (base), never `min_rent_unit` (gross).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ma_poc.pms.adapters.entrata import parse_entrata_modern_units_data

_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "entrata_modern"


def _rows(stem: str) -> list[dict]:
    return parse_entrata_modern_units_data(
        (_FIX / f"{stem}_listing.html").read_text(encoding="utf-8", errors="replace"),
        "https://example.com/city/prop/conventional/",
    )


class TestOneFetchWholeRoster:
    """One /conventional/ listing fetch yields every available unit."""

    @pytest.mark.parametrize(
        ("stem", "min_units"),
        [("manor", 10), ("rise", 7), ("steeplechase", 14)],
    )
    def test_unit_count(self, stem: str, min_units: int) -> None:
        rows = _rows(stem)
        assert len(rows) == min_units, [r["unit_number"] for r in rows]

    def test_every_row_is_unit_level(self) -> None:
        """A row with no unit_number would be plan-level — defeats the point."""
        for stem in ("manor", "rise", "steeplechase"):
            for r in _rows(stem):
                assert r["unit_number"], f"{stem}: empty unit_number {r}"


class TestFeeTransparencyRentIsBaseNotGross:
    """Steeplechase runs fee-transparency: base 1150 vs fee-inclusive unit 1350."""

    def test_specific_unit_takes_base_rent(self) -> None:
        rows = {r["unit_number"]: r for r in _rows("steeplechase")}
        u = rows["1725SD"]
        # Base is 1150; the fee-inclusive total (min_rent_unit) is 1350.
        assert u["market_rent_low"] == 1150, u
        assert u["market_rent_low"] != 1350, "shipped the fee-inclusive gross as rent"

    def test_no_unit_prices_at_the_known_gross(self) -> None:
        """1350 is 1725SD's gross; no unit should surface exactly that value
        for the 2-bed plan (its whole plan is base 1150)."""
        two_bed = [
            r for r in _rows("steeplechase")
            if str(r.get("floor_plan_name", "")).startswith("Two Bedroom")
        ]
        assert two_bed
        for r in two_bed:
            assert r["market_rent_low"] == 1150, r


class TestFieldMapping:
    def test_identity_carries_engrain_and_uid(self) -> None:
        r = {x["unit_number"]: x for x in _rows("steeplechase")}["1725SD"]
        sid = r.get("source_ids") or {}
        assert sid.get("unit_id_engrain") == "1725SD"
        assert sid.get("entrata_uid")  # Entrata's stable int, as a string
        assert sid.get("entrata_fpid")

    def test_alphanumeric_unit_number_preserved(self) -> None:
        nums = {r["unit_number"] for r in _rows("steeplechase")}
        assert "1725SD" in nums  # not coerced to an int / stripped

    def test_sqft_falls_back_to_sqft_unit(self) -> None:
        """`sqft` is null on the listing; `sqft_unit` carries it."""
        r = {x["unit_number"]: x for x in _rows("steeplechase")}["1725SD"]
        assert str(r.get("sqft")) == "1200"

    def test_beds_baths_from_json(self) -> None:
        r = {x["unit_number"]: x for x in _rows("steeplechase")}["1725SD"]
        assert str(r.get("bedrooms")) == "2"
        assert str(r.get("bathrooms")) == "1.5"

    def test_availability_date_is_iso(self) -> None:
        r = {x["unit_number"]: x for x in _rows("manor")}["9006"]
        # available_on 09/04/2026 -> ISO 2026-09-04
        assert r.get("availability_date") == "2026-09-04"

    def test_plain_property_takes_base_range(self) -> None:
        """Manor 9006: base range 1298-2890 across lease terms."""
        r = {x["unit_number"]: x for x in _rows("manor")}["9006"]
        assert r["market_rent_low"] == 1298
        assert r["market_rent_high"] == 2890


class TestDegradesSafely:
    """A shape change must yield zero rows, never a raise — QC-adjacent code."""

    def test_no_units_data_returns_empty(self) -> None:
        assert parse_entrata_modern_units_data("<html>no blob here</html>", "x") == []

    def test_empty_html_returns_empty(self) -> None:
        assert parse_entrata_modern_units_data("", "x") == []

    def test_malformed_blob_returns_empty_not_raise(self) -> None:
        # Has the marker but the JSON is broken — must not raise.
        assert parse_entrata_modern_units_data(
            "<script>var unitsData = '{not valid json';</script>", "x"
        ) == []

    def test_non_object_blob_returns_empty(self) -> None:
        # unitsData is a JSON array, not the expected floorplan-keyed object.
        assert parse_entrata_modern_units_data(
            "<script>var unitsData = '[1,2,3]';</script>", "x"
        ) == []


class TestGuardFiltersDimensionlessRows:
    """#90 guard: the adapter admits a modern row into the unit-level channel
    only when it carries a physical dimension. A ``unitsData`` blob whose units
    have rent but NO beds/baths/sqft must not supersede — and thereby ORPHAN —
    the property's plan-level baseline (a modern /conventional/ property has no
    downstream /floorplans/ path to re-derive the plan rows, so the loss would
    be plan->FAILED, not plan->plan). The parser stays a pure mapper; the gate
    the adapter applies is ``has_dimension``, exercised directly here.
    """

    _DIMLESS = (
        "<script>var unitsData = '"
        '{"1":[{"unit_number":"101","bedroom":null,"bathroom":null,'
        '"sqft":null,"sqft_unit":null,"min_advertised_base_rent":1500,'
        '"floorplan_name":"Loft"}]}'
        "';</script>"
    )
    _DIMENSIONED = (
        "<script>var unitsData = '"
        '{"1":[{"unit_number":"102","bedroom":2,"bathroom":2,"sqft":900,'
        '"min_advertised_base_rent":1600,"floorplan_name":"Two Bed"}]}'
        "';</script>"
    )

    def test_parser_still_emits_the_dimensionless_row(self) -> None:
        # The parser is a pure mapper — it does NOT filter; the adapter does.
        assert len(parse_entrata_modern_units_data(self._DIMLESS, "x")) == 1

    def test_dimensionless_row_fails_has_dimension(self) -> None:
        from ma_poc.validation.unit_validity import has_dimension

        row = parse_entrata_modern_units_data(self._DIMLESS, "x")[0]
        # Guard drops it -> pp_unit_card_rows stays empty -> plan baseline kept.
        assert has_dimension(row) is False

    def test_dimensioned_row_passes_has_dimension(self) -> None:
        from ma_poc.validation.unit_validity import has_dimension

        row = parse_entrata_modern_units_data(self._DIMENSIONED, "x")[0]
        # Real rosters carry explicit dims -> flow through the guard untouched.
        assert has_dimension(row) is True
