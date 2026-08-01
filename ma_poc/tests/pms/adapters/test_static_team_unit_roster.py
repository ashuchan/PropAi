"""Fail-closed coverage for first-party team-card native-unit rosters."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ma_poc.pms.adapters._static_team_unit_roster import (
    has_static_team_unit_roster_shape,
    recover_static_team_unit_roster,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.generic_plan_text import GenericPlanTextAdapter
from ma_poc.pms.detector import DetectedPMS
from ma_poc.pms.scraper import _try_page_local_static_recovery

URL = "https://www.torviewvillageapts.com/"


def _card(plan: str, beds: int, sqft: int, *labels: str) -> str:
    links = "".join(
        f'<p><a href="https://hudsonvalley.craigslist.org/apa/d/{index}"><b>{label}</b></a></p>'
        for index, label in enumerate(labels, start=1)
    )
    return f"""
      <div class="clearfix team-list team-{plan[0].lower()}">
        <div class="team-detail">
          <h3>{plan}</h3>
          <p><strong>{beds} Bedroom – {plan}</strong></p>
          <p><strong>SQFT – {sqft}</strong></p>
          <div><b>Available Units</b>{links}</div>
        </div>
      </div>
    """


def _page(*cards: str) -> str:
    return f"""
      <html><head><title>Tor View Village | Stop Looking. Start Living.</title></head>
      <body>
        <h1>Tor View Village</h1>
        {''.join(cards)}
        <footer>1 Kensington Circle, Garnerville, New York 10923</footer>
      </body></html>
    """


def _positive_page() -> str:
    return _page(
        _card("A Style", 1, 840, "21I Hasbrouck Drive $2625.00"),
        _card(
            "M Style",
            2,
            955,
            "11B Hasbrouck Drive-$2930.00",
            "20C Kensington Circle-$3030.00",
        ),
    )


def _ctx(
    html: str,
    *,
    name: str = "Tor View Village",
    final_url: str = URL,
) -> AdapterContext:
    return AdapterContext(
        base_url=URL,
        detected=DetectedPMS(pms="onesite", confidence=0.9),
        profile=None,
        expected_total_units=None,
        property_id="38677",
        fetch_result=SimpleNamespace(body=html.encode(), final_url=final_url),
        property_name=name,
        address="1 Kensington Cir",
        city="Garnerville",
        state="NY",
        zip_code="10923",
    )


def test_pid38677_exact_page_keeps_unit_street_and_plan_identity_separate() -> None:
    context = _ctx(_positive_page())

    rows = recover_static_team_unit_roster(context)

    assert [row["unit_number"] for row in rows] == ["21I", "11B", "20C"]
    assert [row["source_native_unit_id"] for row in rows] == ["21I", "11B", "20C"]
    assert [row["source_street_label"] for row in rows] == [
        "Hasbrouck Drive",
        "Hasbrouck Drive",
        "Kensington Circle",
    ]
    assert [row["floor_plan_name"] for row in rows] == ["A Style", "M Style", "M Style"]
    assert [row["market_rent_low"] for row in rows] == [2625, 2930, 3030]
    assert all(row["unit_number"] not in row["source_street_label"] for row in rows)
    assert all(row["unit_number"] != row["floor_plan_name"] for row in rows)
    assert context._static_team_unit_roster_telemetry["identity_fields_kept_separate"] is True


@pytest.mark.asyncio
async def test_sibling_property_identity_cannot_fall_through_to_flat_unit_street_parser() -> None:
    """Boundary member two: same markup under the wrong roster identity."""
    context = _ctx(_positive_page(), name="Sibling Apartments")
    assert has_static_team_unit_roster_shape(context) is True
    assert recover_static_team_unit_roster(context) == []

    result = await GenericPlanTextAdapter().extract(None, context)
    assert result.units == []
    assert any("failed exact identity" in error for error in result.errors)
    assert _try_page_local_static_recovery(context, AdapterResult()) is None


def test_plan_heading_cannot_be_promoted_to_native_unit_identity() -> None:
    """Boundary member three: a plan-shaped token in the listing position."""
    malformed = _page(
        _card("M Style", 2, 955, "M Style Kensington Circle-$3030.00")
    )
    context = _ctx(malformed)

    assert has_static_team_unit_roster_shape(context) is True
    assert recover_static_team_unit_roster(context) == []


def test_duplicate_native_unit_or_cross_property_redirect_fails_entire_roster() -> None:
    duplicate = _page(
        _card("A Style", 1, 840, "21I Hasbrouck Drive $2625.00"),
        _card("M Style", 2, 955, "21I Kensington Circle-$3030.00"),
    )
    assert recover_static_team_unit_roster(_ctx(duplicate)) == []

    crossed = _ctx(_positive_page(), final_url="https://portfolio.example/")
    assert has_static_team_unit_roster_shape(crossed) is False
    assert recover_static_team_unit_roster(crossed) == []


def test_page_local_orchestrator_prefers_exact_team_roster_over_plan_text() -> None:
    recovered = _try_page_local_static_recovery(
        _ctx(_positive_page()),
        AdapterResult(errors=["onesite returned no native rows"]),
    )

    assert recovered is not None
    result, adapter_name = recovered
    assert adapter_name == "static_team_unit_roster"
    assert result.tier_used == "TIER_1_DOM_STATIC_TEAM_UNIT_ROSTER"
    assert result.plan_summaries == []
    assert [row["unit_number"] for row in result.units] == ["21I", "11B", "20C"]
    assert all(row["market_rent_low"] > 0 for row in result.units)
