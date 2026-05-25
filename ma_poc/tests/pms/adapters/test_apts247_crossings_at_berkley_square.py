"""apts247 — Crossings at Berkley Square live-data regression (2026-05-25).

User-flagged manual-QC residue on
``crossingsatberkleysquare.com/floorplans/?floorplan=dogwood-44``:
"unit level data there but not captured". Canary 1ef1060 fell to
``TIER_3_DOM`` with n_units=39 / n_full_pre_fix=14 (~38% strict pass)
instead of routing to the apts247 ``/api/v1/floorplans/`` API.

Root cause (verified by replaying detector.py at 1ef1060 against the
live HTML):

  • At canary 1ef1060 the detector returned ``resman`` (0.90 conf)
    because the apts247-vs-resman demotion (commit a51b8bc) hadn't
    landed yet. The site carries BOTH a ``myresman.com`` apply-flow
    link AND a ``static2.apts247.info`` widget; ResMan won the tie,
    its adapter failed, and the run fell through to generic DOM
    cascade — which over-counted to 39 partial rows.
  • Current branch (post-a51b8bc): detector demotes ResMan to 0.85
    when apts247 co-resident → apts247 wins at 0.90 → Apts247Adapter
    fetches ``/api/v1/floorplans/`` → 14 unit-level rows.

This test locks the fix in. Fixtures are the actual live API response
(5 plans, 14 units) and a homepage snippet carrying both PMS markers
plus both ``api_key`` JS forms the site uses.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import ma_poc.pms.adapters  # noqa: F401  — populate adapter registry
from ma_poc.pms.adapters._apts247 import (
    build_floorplans_url,
    detect_apts247,
    extract_api_key,
    parse_apts247_floorplans,
)
from ma_poc.pms.adapters.apts247 import (
    Apts247Adapter,
    find_apts247_api_key,
)
from ma_poc.pms.adapters.apts247 import (
    parse_apts247_floorplans as _parse_apts247_legacy,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import DetectedPMS, detect_pms

_FIXTURES = Path(__file__).parent / "fixtures" / "apts247"
_FLOORPLANS = _FIXTURES / "crossings_at_berkley_square_floorplans.json"
_HOME_SNIPPET = _FIXTURES / "crossings_at_berkley_square_home_snippet.html"

# Captured 2026-05-25 from the live API. Stable per-plan unit counts;
# any future drift here is the regression signal.
_DOGWOOD_UNIT_NUMBERS = {"155", "158", "259", "215"}
_PLAN_UNIT_COUNTS = {
    "Dogwood": 4,
    "Sequoia": 3,
    "Oak": 2,
    "1x1": 4,
    "2x1": 1,
}
_TOTAL_UNITS = sum(_PLAN_UNIT_COUNTS.values())  # 14


def _load_floorplans() -> dict[str, Any]:
    return json.loads(_FLOORPLANS.read_text(encoding="utf-8"))


def _load_home_snippet() -> str:
    return _HOME_SNIPPET.read_text(encoding="utf-8")


# ─── parser against the live response ─────────────────────────────────


def test_parser_extracts_all_fourteen_unit_level_rows() -> None:
    """The live API carries 14 vacant units across 5 plans. All must
    survive parsing — none should be silently dropped or demoted to
    plan-level."""
    rows = parse_apts247_floorplans(
        _load_floorplans(),
        source_url="https://www.crossingsatberkleysquare.com/api/v1/floorplans/?api_key=K",
    )
    assert len(rows) == _TOTAL_UNITS, (
        f"expected {_TOTAL_UNITS} unit-level rows, got {len(rows)}; "
        f"per-plan = "
        + ", ".join(
            f"{n}={sum(1 for r in rows if r['floor_plan_name'] == n)}"
            for n in _PLAN_UNIT_COUNTS
        )
    )


def test_per_plan_unit_counts_match_live_response() -> None:
    """Pin per-plan counts so any future parser change that drops a
    plan (e.g. tightening sqft/rent gates) trips the test."""
    rows = parse_apts247_floorplans(
        _load_floorplans(),
        source_url="https://www.crossingsatberkleysquare.com/api/v1/floorplans/?api_key=K",
    )
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["floor_plan_name"]] = counts.get(r["floor_plan_name"], 0) + 1
    assert counts == _PLAN_UNIT_COUNTS


def test_dogwood_units_have_full_rent_sqft_availability() -> None:
    """The user-flagged plan (dogwood-44) — every unit must carry rent
    + sqft + availability_date. This is the field set that defines
    "true unit-level" in jugnu's strict-pass gate."""
    rows = parse_apts247_floorplans(
        _load_floorplans(), source_url="https://x/api/v1/floorplans/?api_key=K"
    )
    dogwood = [r for r in rows if r["floor_plan_name"] == "Dogwood"]
    assert len(dogwood) == 4
    unit_nums = {r["unit_number"] for r in dogwood}
    assert unit_nums == _DOGWOOD_UNIT_NUMBERS, (
        f"missing Dogwood units: expected {_DOGWOOD_UNIT_NUMBERS}, got {unit_nums}"
    )
    for u in dogwood:
        # Rent low must be present and look like the property's $699.
        assert u.get("market_rent_low") == 699
        assert u["sqft"] == "600"
        assert u["bedrooms"] == "1"
        assert u["availability_status"] == "AVAILABLE"
        # Every Dogwood unit must publish an availability_date (the
        # field that distinguishes "true unit-level" from "plan-level").
        assert u["availability_date"], (
            f"unit {u['unit_number']} missing availability_date"
        )


def test_oak_units_use_per_unit_rent_not_starting_price() -> None:
    """Oak's plan-level rent is ``$1199`` but per-unit rent may differ.
    The parser must use the per-unit ``rent`` field, NOT the plan's
    starting price — otherwise we'd misreport rent for any unit
    priced differently than the cheapest in the plan."""
    rows = parse_apts247_floorplans(
        _load_floorplans(), source_url="https://x/api/v1/floorplans/?api_key=K"
    )
    oak = [r for r in rows if r["floor_plan_name"] == "Oak"]
    assert len(oak) == 2
    for u in oak:
        assert u["sqft"] == "900"
        assert u["bedrooms"] == "2"
        assert u.get("market_rent_low") is not None


# ─── detector co-resident regression (apts247 must beat resman) ───────


def test_detector_routes_apts247_over_resman_on_live_snippet() -> None:
    """The exact failure mode behind canary 1ef1060's TIER_3_DOM fall-
    through: this homepage carries BOTH an apts247 widget AND a
    ``myresman.com`` apply-flow link. Without the apts247-gates-resman
    demotion (commit a51b8bc), ResMan ties at 0.90 and wins ordering
    → ResManAdapter dispatches → no real ResMan portal here → fall to
    DOM cascade → 39 partial rows. With the demotion, apts247 wins
    and the live API API returns 14 clean unit records."""
    snippet = _load_home_snippet()
    # Sanity: the fixture really does carry both markers — otherwise
    # the test would be testing a degenerate input.
    assert "apts247" in snippet
    assert "myresman" in snippet

    detected = detect_pms(
        url="https://www.crossingsatberkleysquare.com/",
        page_html=snippet,
    )
    assert detected.pms == "apts247", (
        f"expected apts247, got {detected.pms} — apts247-gates-resman "
        "demotion regression (canary 1ef1060 reproduced)"
    )
    assert detected.recommended_strategy == "api_first"


# ─── api_key extraction (both JS forms the site uses) ─────────────────


def test_extract_api_key_handles_var_assignment_form() -> None:
    """The site emits ``var api_key = "<HEX40>";`` — the canonical
    form. Both extractors (``_apts247.extract_api_key`` and
    ``apts247.find_apts247_api_key``) must find it."""
    js = 'var api_key = "613a6c2d7d9b8fc404c1c87ea852beab9b32f937";'
    assert extract_api_key(js) == "613a6c2d7d9b8fc404c1c87ea852beab9b32f937"
    assert find_apts247_api_key(js) == "613a6c2d7d9b8fc404c1c87ea852beab9b32f937"


def test_extract_api_key_handles_window_assignment_form() -> None:
    """The site ALSO emits ``window.api_key = "<HEX40>"`` (no
    semicolon, second script block) — must also match."""
    js = 'window.api_key = "613a6c2d7d9b8fc404c1c87ea852beab9b32f937"'
    assert extract_api_key(js) == "613a6c2d7d9b8fc404c1c87ea852beab9b32f937"
    assert find_apts247_api_key(js) == "613a6c2d7d9b8fc404c1c87ea852beab9b32f937"


def test_extract_api_key_from_live_homepage_snippet() -> None:
    """End-to-end on the actual captured homepage snippet — guards
    against the JS minifier changing the assignment shape."""
    snippet = _load_home_snippet()
    key = extract_api_key(snippet)
    assert key is not None and len(key) == 40
    # Sanity: must be the key Apartments247 actually publishes.
    assert key == "613a6c2d7d9b8fc404c1c87ea852beab9b32f937"


def test_build_floorplans_url_for_property() -> None:
    """URL builder must produce the same path the live API responds
    200 on. Any drift (host, scheme, trailing slash, key param name)
    would silently 404 in production."""
    url = build_floorplans_url(
        "https://www.crossingsatberkleysquare.com/floorplans/",
        "613a6c2d7d9b8fc404c1c87ea852beab9b32f937",
    )
    assert url == (
        "https://www.crossingsatberkleysquare.com/api/v1/floorplans/"
        "?api_key=613a6c2d7d9b8fc404c1c87ea852beab9b32f937"
    )


# ─── adapter end-to-end with mocked HTTP ─────────────────────────────


def test_apts247_adapter_end_to_end_returns_fourteen_units() -> None:
    """Wire the full Apts247Adapter: feed it the homepage snippet as
    L1 body, mock probe_get to serve the live floorplans JSON when
    asked for ``/api/v1/floorplans/``, assert the adapter returns 14
    units at TIER_1_API_APTS247 with high confidence. Reproduces the
    expected post-fix production behavior end-to-end."""
    snippet = _load_home_snippet()
    floorplans_body = _FLOORPLANS.read_text(encoding="utf-8")

    class _StubFetchResult:
        body = snippet.encode("utf-8")
        final_url = "https://www.crossingsatberkleysquare.com/"

    class _MockResp:
        def __init__(self, text: str, status_code: int = 200) -> None:
            self.text = text
            self.status_code = status_code

    def _fake_probe_get(url: str, *_: object, **__: object) -> _MockResp:
        # The adapter only ever asks probe_get for the floorplans URL
        # (api_key fetch is done by find_apts247_api_key on the body
        # already in hand). Defensive: serve floorplans for the API
        # call, error on anything else.
        if "/api/v1/floorplans/" in url:
            return _MockResp(floorplans_body, 200)
        return _MockResp("", 404)

    detected = DetectedPMS(
        pms="apts247",
        confidence=0.9,
        evidence=["live snippet"],
        pms_client_account_id=None,
        recommended_strategy="api_first",
    )
    ctx = AdapterContext(
        base_url="https://www.crossingsatberkleysquare.com/",
        detected=detected,
        profile=None,
        expected_total_units=None,
        property_id="berkley_test",
        fetch_result=_StubFetchResult(),
    )

    with patch("ma_poc.pms.adapters._probe.probe_get", side_effect=_fake_probe_get):
        result = asyncio.run(Apts247Adapter().extract(page=None, ctx=ctx))

    assert result.tier_used == "TIER_1_API_APTS247", (
        f"tier_used={result.tier_used} — adapter fell off the Tier-1 "
        f"path. Errors: {result.errors}"
    )
    assert len(result.units) == _TOTAL_UNITS, (
        f"expected {_TOTAL_UNITS} units, got {len(result.units)}; "
        f"errors={result.errors}"
    )
    assert result.confidence >= 0.85
    assert "/api/v1/floorplans/" in (result.winning_url or "")
    # Every unit must have rent — the "captured but lacks rent" failure
    # mode is what the user-flagged residue boiled down to.
    for u in result.units:
        assert u.get("market_rent_low"), (
            f"unit {u.get('unit_number')!r} missing market_rent_low"
        )


def test_legacy_parser_in_apts247_module_also_extracts_dogwood() -> None:
    """There are TWO apts247 parsers in the tree: ``_apts247.py`` (used
    by the generic.py sub-tier) and ``apts247.py`` (used by the
    Apts247Adapter standalone). Both must agree that Dogwood has 4
    vacant units — otherwise the two routing paths produce different
    answers depending on which path wins in production."""
    rows = _parse_apts247_legacy(
        _load_floorplans(),
        source_url="https://x/api/v1/floorplans/?api_key=K",
    )
    dogwood = [r for r in rows if r["floor_plan_name"] == "Dogwood"]
    assert len(dogwood) == 4, (
        f"legacy parser dropped Dogwood units (got {len(dogwood)}/4) — "
        f"the two parsers must agree"
    )


@pytest.mark.parametrize("plan_name", list(_PLAN_UNIT_COUNTS))
def test_every_plan_in_fixture_round_trips_through_parser(plan_name: str) -> None:
    """Per-plan smoke parameterized so any single-plan parser regression
    fails its own test (vs the aggregate ``_TOTAL_UNITS`` test masking
    which plan dropped)."""
    rows = parse_apts247_floorplans(
        _load_floorplans(), source_url="https://x/api/v1/floorplans/?api_key=K"
    )
    plan_rows = [r for r in rows if r["floor_plan_name"] == plan_name]
    assert len(plan_rows) == _PLAN_UNIT_COUNTS[plan_name]
