"""MITS-ILS ``window.__FP_DATA__`` roster recovery (#93, 2026-07-31).

A JCM-Living / handiwork-theme WordPress site (Pleasant View) embeds its full
UNIT-LEVEL roster in a ``window.__FP_DATA__`` MITS-ILS blob. The generic
embedded-blob scanner reaches it but dies on a Cloudflare ``__cf_email__``
injection (unescaped inner quotes), and the MITS-ILS shape is outside the
generic normalizer's vocabulary — so the property lands with no data despite
publishing every apartment. ``recover_avail_table`` (code-only recovery arm)
parses it directly.

The 6 rows are ground truth from the saved fixture: real apartment numbers
232 / 211A / 307A / 059 / 159 / 080A across three floor plans.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ma_poc.pms.adapters._avail_table_recovery import (
    parse_mits_ils_fp_data,
    parse_squarespace_apartment_figures,
    parse_squarespace_unit_blocks,
    recover_avail_table,
)
from ma_poc.scripts.runners.jugnu import _format_v2_unit

_FIXDIR = Path(__file__).resolve().parents[2] / "fixtures" / "avail_table"


@pytest.fixture(scope="module")
def jcm_html() -> str:
    return (_FIXDIR / "jcm_pleasantview.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cricket_html() -> str:
    return (_FIXDIR / "cricket_flats.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def landmark_html() -> str:
    return (_FIXDIR / "squarespace_landmark_figures.html").read_text(encoding="utf-8")


class _Ctx:
    """Minimal AdapterContext stand-in: only what the recovery reads."""

    class _FR:
        def __init__(self, body: str, final_url: str) -> None:
            self.body = body
            self.final_url = final_url

    def __init__(self, body: str, final_url: str = "https://jcm.test/") -> None:
        self.fetch_result = self._FR(body, final_url)
        self.base_url = final_url


class TestMitsIlsParse:
    def test_recovers_all_six_units(self, jcm_html: str) -> None:
        rows = parse_mits_ils_fp_data(jcm_html, "https://jcm.test/")
        assert len(rows) == 6

    def test_unit_numbers_are_the_real_apartments(self, jcm_html: str) -> None:
        nums = {r["unit_number"] for r in parse_mits_ils_fp_data(jcm_html)}
        assert nums == {"232", "211A", "307A", "059", "159", "080A"}

    def test_every_row_carries_rent_and_is_unit_level(self, jcm_html: str) -> None:
        rows = parse_mits_ils_fp_data(jcm_html)
        # Real apartment number + a positive rent on every row = gold unit-level.
        assert all(r["unit_number"] for r in rows)
        assert all(r.get("market_rent_low") for r in rows)

    def test_field_values_for_a_known_unit(self, jcm_html: str) -> None:
        by_num = {r["unit_number"]: r for r in parse_mits_ils_fp_data(jcm_html)}
        u = by_num["232"]
        assert u["market_rent_low"] == 2749
        assert str(u.get("sqft")) == "925"
        assert str(u.get("bedrooms")) == "2"
        assert u.get("floor_plan_name") == "2 Bedroom Townhouse"

    def test_misspelled_floonplan_key_is_read(self, jcm_html: str) -> None:
        # The source key is "FloonplanName" (SIC); a plan name proves we read it.
        rows = parse_mits_ils_fp_data(jcm_html)
        assert all(r.get("floor_plan_name") for r in rows)

    def test_does_not_emit_inflated_floorplan_counts(self, jcm_html: str) -> None:
        # FloorPlan[].UnitsAvailable are bogus marketing totals (852/245/…).
        # We must emit the 6 real ILS_Unit rows, never 4 plan summaries.
        rows = parse_mits_ils_fp_data(jcm_html)
        assert len(rows) == 6  # not 4 (plans), not 850+ (inflated)


class TestCfEmailSanitizer:
    def test_survives_the_cf_email_injection(self, jcm_html: str) -> None:
        # The blob contains a __cf_email__ anchor with unescaped quotes that
        # breaks a naive json.loads. Recovery still yields all 6 rows.
        assert len(parse_mits_ils_fp_data(jcm_html)) == 6


class TestGuards:
    def test_no_blob_returns_empty(self) -> None:
        assert parse_mits_ils_fp_data("<html><body>no data</body></html>") == []

    def test_malformed_blob_returns_empty_not_raises(self) -> None:
        assert parse_mits_ils_fp_data("window.__FP_DATA__ = {not valid") == []

    def test_recover_reads_body_off_ctx(self, jcm_html: str) -> None:
        rows = asyncio.run(recover_avail_table(_Ctx(jcm_html)))
        assert len(rows) == 6

    def test_recover_tolerates_missing_fetch_result(self) -> None:
        class _Empty:
            fetch_result = None
            base_url = ""

        assert asyncio.run(recover_avail_table(_Empty())) == []

    def test_recover_on_unrelated_page_is_empty(self) -> None:
        rows = asyncio.run(recover_avail_table(_Ctx("<html>marketing shell</html>")))
        assert rows == []


class TestSquarespaceUnitBlocks:
    """Cricket Flats: 8 real apartments in Squarespace pre-wrap <p> blocks."""

    def test_recovers_all_eight_units(self, cricket_html: str) -> None:
        assert len(parse_squarespace_unit_blocks(cricket_html)) == 8

    def test_unit_numbers_are_the_real_apartments(self, cricket_html: str) -> None:
        nums = {r["unit_number"] for r in parse_squarespace_unit_blocks(cricket_html)}
        assert nums == {"406", "514", "303", "517", "217", "213", "318", "212"}

    def test_rent_and_spelled_out_beds(self, cricket_html: str) -> None:
        by = {r["unit_number"]: r for r in parse_squarespace_unit_blocks(cricket_html)}
        assert by["406"]["market_rent_low"] == 2850
        assert str(by["406"].get("bedrooms")) == "1"  # "One Bedroom" -> 1
        assert str(by["517"].get("bedrooms")) == "2"  # "Two Bedroom" -> 2

    def test_plus_den_is_not_a_second_bedroom(self, cricket_html: str) -> None:
        by = {r["unit_number"]: r for r in parse_squarespace_unit_blocks(cricket_html)}
        assert str(by["303"].get("bedrooms")) == "1"  # "One Bedroom plus Den" -> 1

    def test_preserves_visible_availability_tokens(self, cricket_html: str) -> None:
        by = {r["unit_number"]: r for r in parse_squarespace_unit_blocks(cricket_html)}
        assert by["303"]["availability_date"] == "Available 9/1"
        assert by["406"]["availability_date"] == "Available Now"

    def test_footer_and_contact_blocks_are_excluded(self, cricket_html: str) -> None:
        # 13 pre-wrap <p> exist; only the 8 that START "Unit N" + carry $rent
        # are units — the address / phone / CTA blocks must not become rows.
        assert len(parse_squarespace_unit_blocks(cricket_html)) == 8

    def test_no_unit_blocks_returns_empty(self) -> None:
        html = '<div class="sqs-html-content"><p style="white-space:pre-wrap">Hello</p></div>'
        assert parse_squarespace_unit_blocks(html) == []

    def test_recover_dispatches_to_squarespace(self, cricket_html: str) -> None:
        rows = asyncio.run(recover_avail_table(_Ctx(cricket_html)))
        assert len(rows) == 8


class TestSquarespaceApartmentFigures:
    """The Landmark: five complete apartments, each bound to one figure."""

    def test_recovers_all_five_figures(self, landmark_html: str) -> None:
        rows = parse_squarespace_apartment_figures(
            landmark_html,
            "https://www.fxmessina.com/the-landmark",
        )
        assert {row["unit_number"] for row in rows} == {
            "203",
            "212",
            "218",
            "304",
            "317",
        }

    def test_exact_dimensions_rents_and_no_invented_plan(self, landmark_html: str) -> None:
        rows = parse_squarespace_apartment_figures(landmark_html)
        by_unit = {row["unit_number"]: row for row in rows}
        assert by_unit["203"]["sqft"] == "860"
        assert by_unit["203"]["market_rent_low"] == 2600
        assert by_unit["304"]["market_rent_low"] == 2626
        assert by_unit["317"]["unit_name"] == "Landmark # 317"
        assert all(row["bedrooms"] == "1" for row in rows)
        assert all(row["bathrooms"] == "1" for row in rows)
        assert all(row["floor_plan_name"] == "" for row in rows)
        assert all("floor_plan_name" in row["data_gaps"] for row in rows)

    def test_visible_date_semantics_survive_to_production_formatter(
        self,
        landmark_html: str,
    ) -> None:
        capture = datetime(2026, 8, 1, 12, tzinfo=UTC)
        by_unit = {
            row["unit_number"]: row
            for row in parse_squarespace_apartment_figures(landmark_html)
        }
        immediate = _format_v2_unit(by_unit["203"], capture, "56903")
        historical = _format_v2_unit(by_unit["218"], capture, "56903")
        assert immediate["available_date"] == "2026-08-01"
        assert immediate["availability_date_provenance"] == "available_now"
        assert historical["available_date"] == "2026-06-15"
        assert historical["availability_date_provenance"] == "historical_embedded"

    def test_never_combines_signals_across_figures(self) -> None:
        html = """
        <figure class="sqs-block-image-figure">
          <figcaption class="image-caption-wrapper">
            <p>Landmark # 999 - One (1) bedroom and one (1) full bathroom.</p>
            <p>Availability: Immediate</p>
          </figcaption>
        </figure>
        <figure class="sqs-block-image-figure">
          <figcaption class="image-caption-wrapper">
            <p>Approx. 900 sq. ft.</p><p>Monthly Rent: $2,500</p>
          </figcaption>
        </figure>
        """
        assert parse_squarespace_apartment_figures(html) == []

    def test_recover_dispatches_to_figure_surface(self, landmark_html: str) -> None:
        rows = asyncio.run(
            recover_avail_table(
                _Ctx(landmark_html, "https://www.fxmessina.com/the-landmark")
            )
        )
        assert len(rows) == 5
