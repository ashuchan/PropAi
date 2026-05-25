"""SightMap zero-price unit-filter tests (2026-05-25).

Pins the behaviour of ``_drop_zero_info_sightmap_units`` and its
integration into the three SightMap success paths (primary, iframe,
direct probe).

Background — deep-probe 2026-05-25
-----------------------------------
The canary's ``TIER_1_API_SIGHTMAP_IFRAME`` bucket had 2,605 unit rows
with ``rent_low ∈ {0, None}``. Live API probes of 4 of the 6 worst
properties (altisbluelake / eonflaglervillage / hydeparkmckinney /
240parkave — all 100% zero-rent) confirmed the same shape on every raw
unit: ``price=None``, ``display_price=None``, ``available_on=None``,
``display_available_on=None``. These were emitted as AVAILABLE rows
with empty rent — false-positive availability inventory.

The filter drops a unit iff:
  * ``rent_range`` is empty/zero AND no positive numeric market rent
  * AND ``availability_date`` is empty/null-like

Priced units survive, dated-but-not-priced units survive, the all-empty
placeholder rows do not.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from ma_poc.pms.adapters._parsing import make_unit_dict
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.sightmap import (
    _TIER_OPERATOR_RENT_NOT_PUBLISHED,
    SightMapAdapter,
    _drop_zero_info_sightmap_units,
    parse_sightmap_payload,
)
from ma_poc.pms.detector import detect_pms


def _u(
    unit_number: str = "U",
    rent_low: int | None = None,
    rent_range: str = "",
    availability_date: str = "",
    area: int = 800,
) -> dict[str, Any]:
    """Build a SightMap-shaped unit dict at the same shape ``parse_
    sightmap_payload`` emits."""
    return make_unit_dict(
        unit_number=unit_number,
        sqft=str(area),
        bedrooms="1",
        bathrooms="1",
        floor_plan_name="A1",
        rent_low=rent_low,
        rent_range=rent_range,
        availability_date=availability_date,
        availability_status="AVAILABLE",
        extraction_tier="TIER_1_API_SIGHTMAP",
    )


# ── _drop_zero_info_sightmap_units — unit-level semantics ────────────


def test_drops_unit_with_no_rent_and_no_date() -> None:
    """The Altis Blue Lake case — price=null, available_on=null on every
    raw unit. Filter must drop them."""
    units = [_u("101"), _u("102"), _u("103")]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    dropped = _drop_zero_info_sightmap_units(units, result)
    assert dropped == 3
    assert units == []
    assert any("sightmap-zero-info-dropped" in e for e in result.errors)


def test_keeps_priced_unit() -> None:
    """A unit with positive market rent must survive — even if its
    availability date is missing (some operators publish rent without
    turn dates)."""
    units = [_u("201", rent_low=1500, rent_range="$1,500")]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    dropped = _drop_zero_info_sightmap_units(units, result)
    assert dropped == 0
    assert len(units) == 1
    assert units[0]["unit_number"] == "201"


def test_keeps_unit_with_only_date() -> None:
    """A unit with an availability date but no rent is still
    informational (operator publishes turn-date but not price) — must
    not be dropped by the zero-info filter; the existing rent-gap flag
    handles it instead."""
    units = [_u("301", availability_date="2026-06-15")]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    dropped = _drop_zero_info_sightmap_units(units, result)
    assert dropped == 0
    assert len(units) == 1


def test_mixed_priced_and_zero_drops_only_zero() -> None:
    """Mixed-price property: some units priced, some empty. Filter
    keeps the priced subset, drops the rest — fixes per-property zero-
    rent rows without losing the leasable inventory."""
    units = [
        _u("A", rent_low=1500, rent_range="$1,500"),  # priced
        _u("B"),  # zero-info — drop
        _u("C", availability_date="2026-07-01"),  # dated — keep
        _u("D"),  # zero-info — drop
    ]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    dropped = _drop_zero_info_sightmap_units(units, result)
    assert dropped == 2
    surviving = sorted(u["unit_number"] for u in units)
    assert surviving == ["A", "C"]


def test_empty_units_returns_zero_drop_no_log() -> None:
    """Defensive: empty list in → empty list out, no telemetry noise."""
    units: list[dict[str, Any]] = []
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    assert _drop_zero_info_sightmap_units(units, result) == 0
    assert not result.errors


def test_zero_rent_string_treated_as_no_rent() -> None:
    """An explicit ``rent_range="$0"`` is a sentinel for missing rent —
    must NOT count as a priced unit."""
    units = [
        {**_u("Z"), "rent_range": "$0"},
        {**_u("Y"), "rent_range": "0"},
    ]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    assert _drop_zero_info_sightmap_units(units, result) == 2
    assert units == []


def test_null_like_date_treated_as_no_date() -> None:
    """``availability_date="None"`` / ``"null"`` (string sentinel from
    upstream str(None)) must NOT count as a date."""
    for sentinel in ("None", "null", "n/a", "-", ""):
        units = [{**_u("X"), "availability_date": sentinel}]
        result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
        assert _drop_zero_info_sightmap_units(units, result) == 1, (
            f"sentinel {sentinel!r} should drop"
        )
        assert units == []


def test_telemetry_message_includes_drop_count() -> None:
    """The single error line must surface the count for audit."""
    units = [_u("a"), _u("b"), _u("c"), _u("d"), _u("e")]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    _drop_zero_info_sightmap_units(units, result)
    assert any("5 unit(s)" in e for e in result.errors)


# ── parse_sightmap_payload still emits zero-info units ───────────────
# (we filter at extract() level, NOT inside the parser, so the rescue
# chain — Avalon override, subpage recovery — can still see the raw
# units they were built around).


def test_parser_still_emits_zero_info_units() -> None:
    """The parser is unchanged — emits zero-info units. They are
    filtered out one layer up in ``SightMapAdapter.extract``."""
    body = {
        "data": {
            "floor_plans": [{
                "id": "fp1", "name": "A1",
                "bedroom_count": 1, "bathroom_count": 1,
            }],
            "units": [
                {
                    "unit_number": "101", "label": "101", "floor_plan_id": "fp1",
                    "price": None, "display_price": None,
                    "available_on": None, "display_available_on": None,
                    "area": 800,
                },
            ],
        }
    }
    units, dropped = parse_sightmap_payload(body, "test")
    assert len(units) == 1
    assert units[0]["rent_range"] == ""
    assert units[0]["availability_date"] == ""
    assert dropped == 0


# ── SightMapAdapter.extract — end-to-end integration ─────────────────


def _make_ctx(api_responses: list[dict[str, Any]]) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://tour.sightmap.com/embed/12345",
        detected=detect_pms("https://tour.sightmap.com/embed/12345"),
        profile=None,
        expected_total_units=None,
        property_id="TEST",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


class _DummyPage:
    pass


def _make_response(
    units_payload: list[dict[str, Any]],
    fps_payload: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "url": "https://sightmap.com/app/api/v1/x/sightmaps/1",
        "body": {
            "data": {
                "floor_plans": fps_payload or [{
                    "id": "fp1", "name": "A1",
                    "bedroom_count": 1, "bathroom_count": 1,
                }],
                "units": units_payload,
            }
        },
    }


@pytest.mark.asyncio
async def test_extract_drops_all_units_when_every_unit_zero_info() -> None:
    """Altis Blue Lake / EON Squared shape: 100%-null-price units. After
    drop, result.units is empty; tier_used flips to the new code; no
    SUCCESS verdict can ship."""
    payload = [
        {
            "unit_number": str(100 + i),
            "label": str(100 + i),
            "floor_plan_id": "fp1",
            "price": None, "display_price": None,
            "available_on": None, "display_available_on": None,
            "area": 800,
        }
        for i in range(5)
    ]
    adapter = SightMapAdapter()
    ctx = _make_ctx([_make_response(payload)])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.tier_used == _TIER_OPERATOR_RENT_NOT_PUBLISHED
    assert result.confidence == 0.0
    assert any("OPERATOR_RENT_NOT_PUBLISHED" in e for e in result.errors)
    # The drop-telemetry line still fires for auditability.
    assert any("sightmap-zero-info-dropped" in e for e in result.errors)


@pytest.mark.asyncio
async def test_extract_preserves_priced_units_in_mixed_payload() -> None:
    """Subpage-recovery analogue: mixed priced + zero-info payload.
    Priced units survive at TIER_1_API_SIGHTMAP; zero-info shed."""
    payload = [
        {
            "unit_number": "P1", "label": "P1", "floor_plan_id": "fp1",
            "price": 1500, "display_price": "$1,500",
            "available_on": "2026-06-15",
            "area": 800,
        },
        {
            "unit_number": "P2", "label": "P2", "floor_plan_id": "fp1",
            "price": 1600, "display_price": "$1,600",
            "available_on": "2026-07-01",
            "area": 850,
        },
        # zero-info — should drop
        {
            "unit_number": "Z1", "label": "Z1", "floor_plan_id": "fp1",
            "price": None, "display_price": None,
            "available_on": None, "display_available_on": None,
            "area": 800,
        },
    ]
    adapter = SightMapAdapter()
    ctx = _make_ctx([_make_response(payload)])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    surviving = sorted(u["unit_number"] for u in result.units)
    assert surviving == ["P1", "P2"]
    assert result.tier_used == "TIER_1_API_SIGHTMAP"
    # Confidence re-tallied on surviving subset (was previously tallied
    # on raw n_admitted of 3 — now reflects 2).
    assert 0.7 < result.confidence <= 0.95
    assert any("sightmap-zero-info-dropped" in e for e in result.errors)


@pytest.mark.asyncio
async def test_extract_keeps_dated_no_rent_units_with_rent_gap_flag() -> None:
    """The "operator publishes turn-date but not rent" shape: every unit
    has ``available_on`` set but ``price=None``. Filter must NOT drop
    these — the existing rent-gap flag handles them by stamping
    ``data_gaps=["rent"]``."""
    payload = [
        {
            "unit_number": str(100 + i),
            "label": str(100 + i),
            "floor_plan_id": "fp1",
            "price": None, "display_price": None,
            "available_on": "2026-06-15",
            "area": 800,
        }
        for i in range(4)
    ]
    adapter = SightMapAdapter()
    ctx = _make_ctx([_make_response(payload)])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    # All units kept — they have dates.
    assert len(result.units) == 4
    # Rent-gap flag fired (existing behaviour preserved).
    assert all("rent" in u.get("data_gaps", []) for u in result.units)
    # Zero-info filter must NOT have logged drops.
    assert not any("sightmap-zero-info-dropped" in e for e in result.errors)


# ── Regression coverage: existing fixtures still extract ─────────────


@pytest.mark.asyncio
async def test_extract_preserves_existing_synthetic_fixture() -> None:
    """The synthetic_units.json fixture (4 priced units) is the
    long-standing happy-path test for the adapter. The new drop must
    not regress it."""
    from pathlib import Path

    fx = Path(__file__).parent / "fixtures" / "sightmap" / "synthetic_units.json"
    responses = json.loads(fx.read_text(encoding="utf-8"))
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 4
    assert all("$" in u["rent_range"] for u in result.units)
    assert not any("sightmap-zero-info-dropped" in e for e in result.errors)


@pytest.mark.asyncio
async def test_extract_preserves_268836_fixture() -> None:
    """The Hawthorne at Traditions fixture has 44 priced units — must
    survive the filter without any drops."""
    from pathlib import Path

    fx = Path(__file__).parent / "fixtures" / "sightmap" / "268836_amenities_only.json"
    responses = json.loads(fx.read_text(encoding="utf-8"))
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) > 0
    assert not any("sightmap-zero-info-dropped" in e for e in result.errors)


# ── Defensive: telemetry line is idempotent ──────────────────────────


def test_double_call_drops_already_filtered_to_zero() -> None:
    """Running the filter twice on the same list must not double-log."""
    units = [_u("A"), _u("B"), _u("C")]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    _drop_zero_info_sightmap_units(units, result)
    # Second call has nothing to drop — should be a no-op, no extra log.
    _drop_zero_info_sightmap_units(units, result)
    assert sum(1 for e in result.errors if "sightmap-zero-info-dropped" in e) == 1
