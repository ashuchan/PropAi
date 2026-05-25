"""SightMap zero-price unit-filter tests (2026-05-25 + follow-up).

Pins the behaviour of ``_drop_zero_info_sightmap_units`` and its
integration into the three SightMap success paths (primary, iframe,
direct probe).

Background — deep-probe 2026-05-25 (original chip #10, dbd7d77)
---------------------------------------------------------------
The canary's ``TIER_1_API_SIGHTMAP_IFRAME`` bucket had 2,605 unit rows
with ``rent_low ∈ {0, None}``. Live API probes of 4 of the 6 worst
properties (altisbluelake / eonflaglervillage / hydeparkmckinney /
240parkave — all 100% zero-rent) confirmed the same shape on every raw
unit: ``price=None``, ``display_price=None``, ``available_on=None``,
``display_available_on=None``. These were emitted as AVAILABLE rows
with empty rent — false-positive availability inventory.

Follow-up — chip #10 follow-up (this commit, 2026-05-25)
--------------------------------------------------------
The original filter intentionally kept "dated-but-not-priced" rows as
informational. Post-canary, the residue cohort showed:
  * 199 rows in ``TIER_1_API_SIGHTMAP_IFRAME_PLAN_LEVEL``
  * 80 rows in ``TIER_1_API_SIGHTMAP_DIRECT_PLAN_LEVEL``
  * 99 rows in ``TIER_1_API_SIGHTMAP_DIRECT``
  * 15 rows with ``sqft=-1`` in ``TIER_1_API_SIGHTMAP_IFRAME_PLAN_LEVEL``

All were dated-no-rent rows kept by the original courtesy branch, plus
the SightMap parser emitting ``display_area="-1"`` verbatim instead of
treating the non-positive sentinel as missing. The filter now drops
units that lack a positive numeric rent in ANY canonical rent field;
the parser normalises non-positive ``display_area`` to empty.

Filter gate (per unit, default ``keep_dated_no_rent=False``):
  * no positive numeric rent in ANY canonical rent field
  * AND ``rent_range`` is empty / in the sentinel set
    (catches ``"$0.00"``, ``"TBD"``, ``"—"``, ``"Call for pricing"``)

The ``keep_dated_no_rent=True`` opt-in preserves the original
2026-05-25 behaviour for callers that want the dated-no-rent rows.
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


def test_drops_unit_with_only_date_default() -> None:
    """chip #10 follow-up: dated-no-rent rows are the residue cohort
    (199 / 80 / 99 across the IFRAME_PLAN_LEVEL / DIRECT_PLAN_LEVEL /
    DIRECT variants) and must now drop. The original chip #10 kept
    them as informational; the follow-up reverses that decision."""
    units = [_u("301", availability_date="2026-06-15")]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    dropped = _drop_zero_info_sightmap_units(units, result)
    assert dropped == 1
    assert units == []


def test_keeps_unit_with_only_date_when_opted_in() -> None:
    """``keep_dated_no_rent=True`` preserves the original chip #10
    courtesy: dated-no-rent rows are retained as informational."""
    units = [_u("301", availability_date="2026-06-15")]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    dropped = _drop_zero_info_sightmap_units(
        units, result, keep_dated_no_rent=True
    )
    assert dropped == 0
    assert len(units) == 1


def test_mixed_priced_and_dated_drops_dated_by_default() -> None:
    """Mixed-price property under the new strict default: priced rows
    survive, both empty-info AND dated-no-rent are dropped. Matches the
    residue cohort the follow-up targets."""
    units = [
        _u("A", rent_low=1500, rent_range="$1,500"),  # priced — keep
        _u("B"),  # zero-info — drop
        _u("C", availability_date="2026-07-01"),  # dated-no-rent — drop
        _u("D"),  # zero-info — drop
    ]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    dropped = _drop_zero_info_sightmap_units(units, result)
    assert dropped == 3
    surviving = sorted(u["unit_number"] for u in units)
    assert surviving == ["A"]


def test_mixed_priced_and_dated_keeps_dated_when_opted_in() -> None:
    """``keep_dated_no_rent=True`` restores the original chip #10
    mixed-property behaviour: priced + dated rows survive, only the
    fully empty rows drop."""
    units = [
        _u("A", rent_low=1500, rent_range="$1,500"),
        _u("B"),
        _u("C", availability_date="2026-07-01"),
        _u("D"),
    ]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    dropped = _drop_zero_info_sightmap_units(
        units, result, keep_dated_no_rent=True
    )
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
async def test_extract_drops_dated_no_rent_units_to_operator_rent_not_published() -> None:
    """chip #10 follow-up: the "operator publishes turn-date but not
    rent" shape (every unit has ``available_on`` set but ``price=None``)
    is now dropped as zero-rent residue. ``result.units`` becomes empty,
    the tier flips to ``_OPERATOR_RENT_NOT_PUBLISHED``, and the rent-gap
    flag is no longer needed because no rows survive to flag.

    Pre-follow-up behaviour ('kept with data_gaps=[\"rent\"]') was the
    source of the 199 / 80 / 99 residue across the PLAN_LEVEL + DIRECT
    variants — kept rows shipped as AVAILABLE with empty rent."""
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
    assert result.units == []
    assert result.tier_used == _TIER_OPERATOR_RENT_NOT_PUBLISHED
    assert result.confidence == 0.0
    # Drop telemetry fired.
    assert any("sightmap-zero-info-dropped" in e for e in result.errors)


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


# ── chip #10 follow-up: variant-specific coverage ────────────────────


def test_rent_range_broad_sentinel_set_treated_as_no_rent() -> None:
    """Chip #10 originally excluded only ``{"", "$0", "0"}`` which let
    ``"$0.00"`` / ``"TBD"`` / ``"—"`` / ``"Call for pricing"`` etc. pass
    as if they were priced rows. The follow-up broadens the sentinel
    set so every common no-rent placeholder is recognised."""
    sentinels = [
        "$0.00", "0.00", "$0,000",
        "TBD", "Call", "Contact",
        "Call for pricing", "Contact for pricing", "Inquire",
        "N/A", "n/a", "NA", "—", "-",
    ]
    for sentinel in sentinels:
        units = [{**_u("X"), "rent_range": sentinel}]
        result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
        dropped = _drop_zero_info_sightmap_units(units, result)
        assert dropped == 1, f"sentinel {sentinel!r} should drop"
        assert units == [], f"sentinel {sentinel!r} should empty units"


def test_canonical_rent_low_high_recognized() -> None:
    """post-process ``infer`` canonicalises rent via ``rent_low``/
    ``rent_high`` when ``market_rent_low``/``_high`` are absent. The
    original chip #10 only checked the legacy keys, so canonicalised
    units could be dropped despite carrying real rent. Follow-up reads
    all six canonical rent fields."""
    u = {
        **_u("X"),
        "rent_range": "",  # no string form
        "market_rent_low": None,
        "market_rent_high": None,
        "rent_low": 1500.0,  # canonical post-process key
        "rent_high": 1500.0,
    }
    units = [u]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    dropped = _drop_zero_info_sightmap_units(units, result)
    assert dropped == 0
    assert len(units) == 1


def test_asking_rent_recognized() -> None:
    """``asking_rent`` is a schema_gate-supported rent key (alongside
    rent_low/rent_high). A unit carrying just ``asking_rent`` must
    survive the filter."""
    u = {**_u("X"), "rent_range": "", "asking_rent": 1750.0}
    units = [u]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    dropped = _drop_zero_info_sightmap_units(units, result)
    assert dropped == 0
    assert len(units) == 1


def test_zero_numeric_rent_low_treated_as_no_rent() -> None:
    """``rent_low=0`` is explicitly a no-rent value (the operator's
    placeholder), not a real $0 rent. Filter must drop. Guards against
    the v>0 vs v>=0 boundary in the canonical-key check."""
    u = {**_u("X"), "rent_range": "", "rent_low": 0, "rent_high": 0}
    units = [u]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    dropped = _drop_zero_info_sightmap_units(units, result)
    assert dropped == 1
    assert units == []


# ── parse_sightmap_payload: sqft=-1 normalisation ────────────────────


def test_parser_normalizes_negative_display_area_to_empty() -> None:
    """Chip #10 follow-up: 15 rows in TIER_1_API_SIGHTMAP_IFRAME_PLAN_
    LEVEL had ``sqft="-1"``. The parser previously echoed ``display_
    area`` verbatim when ``area`` was absent/non-positive — including
    the literal ``-1`` sentinel some operators use. Now non-positive
    numerics in ``display_area`` normalise to empty so the sqft=-1
    cohort metric stops counting them."""
    body = {
        "data": {
            "floor_plans": [{
                "id": "fp1", "name": "A1",
                "bedroom_count": 1, "bathroom_count": 1,
            }],
            "units": [
                {
                    "unit_number": "101", "label": "101", "floor_plan_id": "fp1",
                    "price": 1500, "display_price": "$1,500",
                    "available_on": "2026-06-15",
                    "area": None,  # primary area absent
                    "display_area": "-1",  # operator-set sentinel
                },
                {
                    "unit_number": "102", "label": "102", "floor_plan_id": "fp1",
                    "price": 1600, "display_price": "$1,600",
                    "available_on": "2026-06-15",
                    "area": None,
                    "display_area": "0",  # explicit zero — also no-area
                },
                {
                    "unit_number": "103", "label": "103", "floor_plan_id": "fp1",
                    "price": 1700, "display_price": "$1,700",
                    "available_on": "2026-06-15",
                    "area": None,
                    "display_area": "850",  # legitimate value — preserved
                },
            ],
        }
    }
    units, _ = parse_sightmap_payload(body, "test")
    assert len(units) == 3
    assert units[0]["sqft"] == ""  # -1 → empty
    assert units[1]["sqft"] == ""  # 0 → empty
    assert units[2]["sqft"] == "850"  # positive → preserved


def test_parser_preserves_positive_area_int() -> None:
    """The positive-area happy path (``area=800``) must still emit
    ``sqft="800"`` — the new non-positive normalisation only fires when
    ``area`` is absent or non-positive."""
    body = {
        "data": {
            "floor_plans": [{
                "id": "fp1", "name": "A1",
                "bedroom_count": 1, "bathroom_count": 1,
            }],
            "units": [{
                "unit_number": "201", "label": "201", "floor_plan_id": "fp1",
                "price": 1500, "display_price": "$1,500",
                "area": 800,
                "display_area": "different value",  # area takes precedence
            }],
        }
    }
    units, _ = parse_sightmap_payload(body, "test")
    assert len(units) == 1
    assert units[0]["sqft"] == "800"


# ── Variant integration: IFRAME path ─────────────────────────────────


@pytest.mark.asyncio
async def test_iframe_path_drops_dated_no_rent_to_operator_rent_not_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TIER_1_API_SIGHTMAP_IFRAME_PLAN_LEVEL`` residue cohort (199
    zero-rent rows). The IFRAME fallback returns dated-no-rent units;
    after the stricter filter, ``result.units`` becomes empty and the
    tier flips to ``TIER_1_API_SIGHTMAP_OPERATOR_RENT_NOT_PUBLISHED_
    IFRAME`` — preventing the downstream verdict layer from demoting
    surviving dated-no-rent rows to ``_IFRAME_PLAN_LEVEL``."""
    from ma_poc.pms.adapters import sightmap as _sm

    async def _stub_iframe_fallback(ctx: Any, result: Any) -> list[dict[str, Any]]:
        # Mimic parse_sightmap_payload output for dated-no-rent shape.
        return [
            make_unit_dict(
                unit_number=str(100 + i),
                sqft="800",
                bedrooms="1", bathrooms="1",
                floor_plan_name="A1",
                rent_range="",
                availability_date="2026-06-15",
                availability_status="AVAILABLE",
                extraction_tier="TIER_1_API_SIGHTMAP",
            )
            for i in range(4)
        ]

    monkeypatch.setattr(_sm, "_try_sightmap_iframe_fallback", _stub_iframe_fallback)
    # No avalon / subpage rescue.
    monkeypatch.setattr(
        _sm, "_try_avalon_override_for_sightmap", lambda *a, **k: []
    )
    monkeypatch.setattr(
        _sm, "_try_subpage_sightmap_with_prices", lambda *a, **k: []
    )

    adapter = SightMapAdapter()
    # No primary API responses → forces the iframe fallback path.
    ctx = _make_ctx([])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.tier_used == f"{_TIER_OPERATOR_RENT_NOT_PUBLISHED}_IFRAME"
    assert result.confidence == 0.0
    assert any("sightmap-zero-info-dropped" in e for e in result.errors)


@pytest.mark.asyncio
async def test_iframe_path_keeps_priced_subset_when_mixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed-price payload on the IFRAME path: priced rows survive at
    ``_IFRAME`` tier; dated-no-rent rows drop. Guards against the
    PLAN_LEVEL downgrade leaking the dated subset."""
    from ma_poc.pms.adapters import sightmap as _sm

    async def _stub_iframe_fallback(ctx: Any, result: Any) -> list[dict[str, Any]]:
        return [
            make_unit_dict(
                unit_number="P1", sqft="800",
                bedrooms="1", bathrooms="1", floor_plan_name="A1",
                rent_low=1500, rent_range="$1,500",
                availability_date="2026-06-15",
                availability_status="AVAILABLE",
                extraction_tier="TIER_1_API_SIGHTMAP",
            ),
            make_unit_dict(
                unit_number="D1", sqft="800",
                bedrooms="1", bathrooms="1", floor_plan_name="A1",
                rent_range="",
                availability_date="2026-07-01",  # dated-no-rent
                availability_status="AVAILABLE",
                extraction_tier="TIER_1_API_SIGHTMAP",
            ),
        ]

    monkeypatch.setattr(_sm, "_try_sightmap_iframe_fallback", _stub_iframe_fallback)
    adapter = SightMapAdapter()
    ctx = _make_ctx([])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    surviving = sorted(u["unit_number"] for u in result.units)
    assert surviving == ["P1"]
    assert result.tier_used == f"{_sm._TIER_BASE}_IFRAME"
    assert any("sightmap-zero-info-dropped" in e for e in result.errors)


# ── Variant integration: DIRECT probe path ───────────────────────────


@pytest.mark.asyncio
async def test_direct_path_drops_dated_no_rent_to_operator_rent_not_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TIER_1_API_SIGHTMAP_DIRECT_PLAN_LEVEL`` residue cohort (80
    zero-rent rows — Cyrene at Meadowlands shape). The direct API
    probe returns dated-no-rent units; the stricter filter empties
    ``result.units`` so the tier flips to
    ``TIER_1_API_SIGHTMAP_OPERATOR_RENT_NOT_PUBLISHED_DIRECT`` and the
    verdict layer can no longer demote to ``_DIRECT_PLAN_LEVEL`` over
    surviving dated-no-rent rows."""
    from ma_poc.pms.adapters import sightmap as _sm

    async def _stub_direct_probe(ctx: Any) -> list[dict[str, Any]]:
        return [
            make_unit_dict(
                unit_number=f"01-{2100 + i}",
                sqft="800",
                bedrooms="1", bathrooms="1",
                floor_plan_name="A1",
                rent_range="",
                availability_date="2026-06-15",
                availability_status="AVAILABLE",
                extraction_tier="TIER_1_API_SIGHTMAP_DIRECT",
                source_api_url="https://sightmap.com/app/api/v1/x/sightmaps/1",
            )
            for i in range(3)
        ]

    monkeypatch.setattr(_sm, "_try_direct_sightmap_api_probe", _stub_direct_probe)
    # Disable the iframe fallback so the test takes the direct probe path.
    async def _empty_iframe(ctx: Any, result: Any) -> list[dict[str, Any]]:
        return []
    monkeypatch.setattr(_sm, "_try_sightmap_iframe_fallback", _empty_iframe)

    adapter = SightMapAdapter()
    # No primary API responses → forces fallback chain → iframe empty →
    # direct probe path.
    ctx = _make_ctx([])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.tier_used == f"{_TIER_OPERATOR_RENT_NOT_PUBLISHED}_DIRECT"
    assert result.confidence == 0.0
    assert any("sightmap-zero-info-dropped" in e for e in result.errors)


@pytest.mark.asyncio
async def test_direct_path_keeps_priced_subset_when_mixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed-price DIRECT probe: priced rows survive at ``_DIRECT``
    tier; dated-no-rent rows drop. Prevents leak into the residue 99
    DIRECT cohort."""
    from ma_poc.pms.adapters import sightmap as _sm

    async def _stub_direct_probe(ctx: Any) -> list[dict[str, Any]]:
        return [
            make_unit_dict(
                unit_number="P1", sqft="800",
                bedrooms="1", bathrooms="1", floor_plan_name="A1",
                rent_low=1500, rent_range="$1,500",
                availability_date="2026-06-15",
                availability_status="AVAILABLE",
                extraction_tier="TIER_1_API_SIGHTMAP_DIRECT",
                source_api_url="https://sightmap.com/app/api/v1/x/sightmaps/1",
            ),
            make_unit_dict(
                unit_number="D1", sqft="800",
                bedrooms="1", bathrooms="1", floor_plan_name="A1",
                rent_range="",
                availability_date="2026-07-01",
                availability_status="AVAILABLE",
                extraction_tier="TIER_1_API_SIGHTMAP_DIRECT",
                source_api_url="https://sightmap.com/app/api/v1/x/sightmaps/1",
            ),
        ]

    monkeypatch.setattr(_sm, "_try_direct_sightmap_api_probe", _stub_direct_probe)
    async def _empty_iframe(ctx: Any, result: Any) -> list[dict[str, Any]]:
        return []
    monkeypatch.setattr(_sm, "_try_sightmap_iframe_fallback", _empty_iframe)

    adapter = SightMapAdapter()
    ctx = _make_ctx([])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    surviving = sorted(u["unit_number"] for u in result.units)
    assert surviving == ["P1"]
    assert result.tier_used == "TIER_1_API_SIGHTMAP_DIRECT"
    assert any("sightmap-zero-info-dropped" in e for e in result.errors)
