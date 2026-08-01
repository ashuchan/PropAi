"""ProspectPortal ``/floor-plan/<type>/<design>.html`` pages ship their units as
schema.org JSON-LD (a ``FloorPlan`` node + an ``ItemList`` of ``Apartment``
items), NOT DOM or ``unitsData``. This is #91 Lever A's third URL template
(Broadway / Chase / Ravel) — these properties shipped SUCCESS_PLAN_LEVEL because
no parser read the per-plan JSON-LD one hop deeper.

Pinned to two real captured pages (curl_cffi, 2026-07-30):
  * Ravel & Royale — Royale EA10 studio, 3 available units
  * Chase at Overlook Ridge — Design EB-E studio, 1 available unit
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ma_poc.pms.adapters import entrata as entrata_mod
from ma_poc.pms.adapters.entrata import (
    EntrataAdapter,
    parse_entrata_floorplan_html_jsonld,
)
from ma_poc.validation.unit_validity import has_dimension

_FIX = Path(__file__).parent / "fixtures" / "entrata"


def _rows(stem: str) -> list[dict]:
    html = (_FIX / f"{stem}.html").read_text(encoding="utf-8")
    return parse_entrata_floorplan_html_jsonld(html, "https://x/floor-plan/studio/y.html")


class TestUnitLevelFromJsonLd:
    @pytest.mark.parametrize(
        ("stem", "n"),
        [
            ("prospectportal_floorplan_html_jsonld_ravel", 3),
            ("prospectportal_floorplan_html_jsonld_chase", 1),
        ],
    )
    def test_unit_count(self, stem: str, n: int) -> None:
        rows = _rows(stem)
        assert len(rows) == n, [r["unit_number"] for r in rows]

    def test_every_row_is_a_valid_unit(self) -> None:
        for stem in (
            "prospectportal_floorplan_html_jsonld_ravel",
            "prospectportal_floorplan_html_jsonld_chase",
        ):
            for r in _rows(stem):
                assert r["unit_number"], r
                assert has_dimension(r), r  # studio (beds=0) is a real dimension

    def test_ravel_fields(self) -> None:
        rows = {r["unit_number"]: r for r in _rows("prospectportal_floorplan_html_jsonld_ravel")}
        u = rows["3B-202"]
        assert u["market_rent_low"] == 2061  # from "Starting at $2061.0"
        assert str(u["bedrooms"]) == "0" and str(u["bathrooms"]) == "1"  # "0.0"/"1.0" -> 0/1
        assert str(u["sqft"]) == "472"
        assert u["floor_plan_name"] == "Royale EA10"

    def test_chase_single_unit(self) -> None:
        (u,) = _rows("prospectportal_floorplan_html_jsonld_chase")
        assert u["unit_number"] == "19-318"
        assert u["market_rent_low"] == 2005
        assert str(u["sqft"]) == "462"
        assert u["availability_date"] == "Available Aug 29"

    def test_visible_available_now_survives_as_source_token(self) -> None:
        rows = _rows("prospectportal_floorplan_html_jsonld_ravel")
        assert {u["availability_date"] for u in rows} == {"Available Now"}


class TestDegradesSafely:
    def test_no_floorplan_jsonld_returns_empty(self) -> None:
        assert parse_entrata_floorplan_html_jsonld("<html><body>no jsonld</body></html>", "x") == []

    def test_empty_returns_empty(self) -> None:
        assert parse_entrata_floorplan_html_jsonld("", "x") == []

    def test_malformed_jsonld_returns_empty_not_raise(self) -> None:
        html = '<script type="application/ld+json">{"@type":"FloorPlan", broken</script>'
        assert parse_entrata_floorplan_html_jsonld(html, "x") == []

    def test_floorplan_without_available_units_returns_empty(self) -> None:
        html = (
            '<script type="application/ld+json">'
            '{"@graph":[{"@type":"FloorPlan","numberOfBedrooms":"1.0"}]}'
            "</script>"
        )
        assert parse_entrata_floorplan_html_jsonld(html, "x") == []


class TestWiredIntoExtract:
    @pytest.mark.asyncio
    async def test_extract_discovers_and_parses_floorplan_html(self, monkeypatch: Any) -> None:
        """#91 Lever A: the cascade discovers /floor-plan/*.html links from the
        (code-only) captured landing and parses each via JSON-LD -> unit-level,
        flipping a plan-level property to gold without needing render."""
        ravel = (_FIX / "prospectportal_floorplan_html_jsonld_ravel.html").read_text()

        async def _fake_fetch(url: str, *, unlocker: bool = True) -> str:
            return ravel if url.endswith(".html") else ""

        async def _no_probe(self: Any, page: Any, ctx: Any) -> list[Any]:
            return []

        monkeypatch.setattr(entrata_mod, "_entrata_static_fetch", _fake_fetch)
        monkeypatch.setattr(EntrataAdapter, "_probe_known_endpoints", _no_probe)

        landing = (
            "<html><body>"
            '<a href="/floor-plan/studio/royale-ea10.html">EA10</a>'
            '<a href="/floor-plan/1-bedroom/royale-1a.html">1A</a>'
            "</body></html>"
        )
        ctx = SimpleNamespace(
            _api_responses=[], base_url="https://www.ravelandroyale.com/",
            property_id="245323", address="", zip_code="",
            fetch_result=SimpleNamespace(
                final_url="https://www.ravelandroyale.com/", body=landing,
            ),
        )
        result = await EntrataAdapter().extract(None, cast(Any, ctx))
        assert result.units, f"expected units, got errors={result.errors}"
        assert all(u.get("unit_number") for u in result.units)
        assert result.tier_used and "ENTRATA" in result.tier_used
