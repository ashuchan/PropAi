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
from pathlib import Path

import pytest

from ma_poc.pms.adapters._avail_table_recovery import (
    parse_mits_ils_fp_data,
    parse_squarespace_unit_blocks,
    recover_avail_table,
)

_FIXDIR = Path(__file__).resolve().parents[2] / "fixtures" / "avail_table"


@pytest.fixture(scope="module")
def jcm_html() -> str:
    return (_FIXDIR / "jcm_pleasantview.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cricket_html() -> str:
    return (_FIXDIR / "cricket_flats.html").read_text(encoding="utf-8")


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
