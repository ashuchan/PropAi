"""Plan-summary emission across PMS adapters.

When an adapter's source API exposes a parent floor-plan envelope
(``layouts``, ``floorplans``, ``floor_plans``, ``unitTypes``,
``totalPricesStartingAt`` bedroom buckets, …) alongside the per-unit
list, the parser must surface plans with **no available units** as
plan-only rows (``unit_number=""``) so the floor plan identity reaches
the v2 schema's ``floor_plans[]`` output. The post-process classifier
routes those rows to ``plan_summaries`` automatically.

The "no-dup" invariant is the structural property exercised by every
suite below: a plan represented by an emitted unit row must NOT also
appear as a plan_summary; conversely, a plan-summary row must NEVER
land in ``result.units`` with an ``inferred_*`` unit_id (the
regression that surfaced on PID 271966 in the local canary after the
APTS247 fix landed — fixed by the ``pp.admitted`` semantic change in
``ma_poc.extraction.post_process``).

Adapter coverage:

* **Knock** (``parse_knock_payload``): emits one plan_summary per
  ``layout`` whose ``id`` is not covered by an emitted unit's
  ``layoutId``.
* **APTS247** (``parse_apts247_floorplans``): emits a plan-only row
  for floorplans whose ``units`` array is empty (rent absent OR
  present — pre-fix the rent-absent case was silently dropped).
* **SightMap** (``parse_sightmap_payload``): emits a plan-only row
  for every ``data.floor_plans[]`` entry whose ``id`` is not joined
  to an emitted unit via ``floor_plan_id``.
* **G5** (``parse_g5_apartments``): emits a plan-only row for every
  ``apartmentComplex.floorplans[]`` entry whose ``id`` is not
  represented by an apartment.
* **AvalonBay** (``AvalonBayAdapter.extract``): emits a plan-only row
  for every bedroom bucket in ``unitsSummary.totalPricesStartingAt``
  whose count is not represented by a live unit row.
"""

from __future__ import annotations

from typing import Any

from ma_poc.extraction.post_process import post_process
from ma_poc.pms.adapters.apts247 import parse_apts247_floorplans
from ma_poc.pms.adapters.avalonbay import parse_avalonbay_units
from ma_poc.pms.adapters.g5 import parse_g5_apartments
from ma_poc.pms.adapters.knock import parse_knock_payload, parse_knock_units
from ma_poc.pms.adapters.sightmap import parse_sightmap_payload

# ──────────────────────────────────────────────────────────────────────
# Knock — parse_knock_payload
# ──────────────────────────────────────────────────────────────────────


def _knock_payload_five_layouts_one_unit() -> dict[str, Any]:
    """Real shape from PID 77913 Sierra Vista: 5 layouts, 1 unit.

    Only ``Layout 2`` has an emitted unit (1312); the other 4 layouts
    have no available units and should each become a plan_summary.
    """
    return {
        "units_data": {
            "layouts": [
                {"id": 1, "name": "Studio", "bedrooms": 0, "bathrooms": 1, "area": 500},
                {"id": 2, "name": "2", "bedrooms": 1, "bathrooms": 1, "area": 857,
                 "minPrice": 1500, "maxPrice": 1700},
                {"id": 3, "name": "Two Bed", "bedrooms": 2, "bathrooms": 2, "area": 1100},
                {"id": 4, "name": "Three Bed", "bedrooms": 3, "bathrooms": 2, "area": 1400},
                {"id": 5, "name": "Penthouse", "bedrooms": 2, "bathrooms": 2.5, "area": 1500},
            ],
            "units": [
                {
                    "name": "1312", "layoutId": 2, "price": 1615,
                    "available": True, "occupied": True,
                    "availableOn": "2026-08-02",
                    "bedrooms": 1, "bathrooms": 1, "area": 857,
                },
            ],
        }
    }


def test_knock_layouts_without_units_become_plan_summaries() -> None:
    """5 layouts + 1 unit → 1 unit + 4 plan_summaries (dedup by layoutId)."""
    units, plans = parse_knock_payload(_knock_payload_five_layouts_one_unit())
    assert len(units) == 1
    assert units[0]["unit_number"] == "1312"
    assert len(plans) == 4
    assert sorted(p["floor_plan_name"] for p in plans) == [
        "Penthouse", "Studio", "Three Bed", "Two Bed",
    ]
    # Layout id=2 must NOT appear in plan_summaries (would duplicate the unit).
    assert "2" not in {p["floor_plan_name"] for p in plans}


def test_knock_plan_summary_rows_have_empty_unit_number() -> None:
    """Empty ``unit_number`` is the routing signal for
    ``post_process.classify()`` to land the row in ``plan_summaries``."""
    _units, plans = parse_knock_payload(_knock_payload_five_layouts_one_unit())
    assert all(p["unit_number"] == "" for p in plans)


def test_knock_empty_payload_returns_no_rows() -> None:
    """Defensive: empty payload returns (empty, empty)."""
    units, plans = parse_knock_payload({"units_data": {"layouts": [], "units": []}})
    assert units == []
    assert plans == []


def test_knock_every_layout_has_unit_emits_no_plan_rows() -> None:
    """When every layout has a unit row, no plan_summary is emitted —
    enforcing the no-dup invariant."""
    payload = {
        "units_data": {
            "layouts": [
                {"id": 1, "name": "A", "bedrooms": 1, "bathrooms": 1, "area": 700},
                {"id": 2, "name": "B", "bedrooms": 2, "bathrooms": 2, "area": 1000},
            ],
            "units": [
                {"name": "101", "layoutId": 1, "price": 1500, "available": True,
                 "bedrooms": 1, "bathrooms": 1, "area": 700},
                {"name": "202", "layoutId": 2, "price": 2200, "available": True,
                 "bedrooms": 2, "bathrooms": 2, "area": 1000},
            ],
        }
    }
    units, plans = parse_knock_payload(payload)
    assert len(units) == 2
    assert plans == []


def test_knock_legacy_parse_knock_units_returns_only_units() -> None:
    """The legacy ``parse_knock_units`` wrapper is units-only —
    third-party callers that only need unit rows keep working."""
    units = parse_knock_units(_knock_payload_five_layouts_one_unit())
    assert isinstance(units, list)
    assert len(units) == 1
    assert units[0]["unit_number"] == "1312"


# ──────────────────────────────────────────────────────────────────────
# APTS247 — parse_apts247_floorplans
# ──────────────────────────────────────────────────────────────────────


def test_apts247_plan_with_no_units_and_no_rent_emits_plan_row() -> None:
    """Plan with neither units nor plan_rent emits a plan-level row.

    Pre-fix this branch silently dropped the floor plan entirely
    (PID 271966 Windswept lost its 4th floor plan this way)."""
    data = {
        "objects": [
            {
                "name": "Townhouse",
                "display_bed": "2 Bed",
                "bath": 2,
                "sq_ft": "1100",
                # No "rent", no "units"
            },
        ]
    }
    rows = parse_apts247_floorplans(data, "https://x.com/api/v1/floorplans/")
    assert len(rows) == 1
    row = rows[0]
    assert row["floor_plan_name"] == "Townhouse"
    assert row["unit_number"] == ""
    assert row["market_rent_low"] is None
    assert row["market_rent_high"] is None
    assert row["bedrooms"] == "2"
    assert row["bathrooms"] == "2"


def test_apts247_plan_with_units_does_not_emit_redundant_plan_row() -> None:
    """When a plan has units, only unit rows are emitted — the plan's
    identity is carried by each unit's ``floor_plan_name``."""
    data = {
        "objects": [
            {
                "name": "Studio",
                "display_bed": "Studio",
                "bath": 1,
                "sq_ft": "441",
                "rent": "$649",
                "units": [
                    {"id": 1001, "number": "673", "rent": "$649",
                     "available_date": "2026-03-31"},
                    {"id": 1002, "number": "674", "rent": "$649",
                     "available_date": "2025-11-01"},
                ],
            },
        ]
    }
    rows = parse_apts247_floorplans(data, "https://x.com/api/v1/floorplans/")
    assert len(rows) == 2
    assert all(r["unit_number"] != "" for r in rows)
    assert sorted(r["unit_number"] for r in rows) == ["673", "674"]


def test_apts247_adapter_keeps_plan_summary_out_of_units(
) -> None:
    """End-to-end through ``Apts247Adapter.extract``: a plan with no units
    appears ONCE in ``result.plan_summaries`` and NEVER in
    ``result.units`` (as an ``inferred_*`` row). Regression guard for the
    ``pp.admitted`` dup that surfaced on PID 271966 in the local canary."""
    import asyncio
    import json as _json
    from dataclasses import dataclass

    import ma_poc.pms.adapters.apts247 as mod
    from ma_poc.pms.adapters.apts247 import Apts247Adapter
    from ma_poc.pms.adapters.base import AdapterContext
    from ma_poc.pms.detector import DetectedPMS

    payload = {
        "objects": [
            {
                "name": "Studio", "display_bed": "Studio", "bath": 1,
                "sq_ft": "441", "rent": "$649",
                "units": [
                    {"id": 100, "number": "673", "rent": "$649",
                     "available_date": "2026-03-31"},
                ],
            },
            {
                "name": "Townhouse", "display_bed": "2 Bed", "bath": 2,
                "sq_ft": "1100",
            },
        ]
    }

    @dataclass
    class _FR:
        body: bytes
        final_url: str = "https://x.com/"

    api_key = "deadbeef" * 4
    home_html = f'<script>var api_key="{api_key}";</script>'

    # ``**_kw`` swallows the production adapter's ``ctx=`` / ``stage=``
    # kwargs (added 2026-05-23 for the proxy_gate). The fake doesn't
    # need them to satisfy the test assertion — accepting and ignoring
    # them keeps the test resilient to future fetch-helper additions.
    async def _fake_fetch(url: str, *_args, **_kw) -> str:
        if "/api/v1/floorplans/" in url:
            return _json.dumps(payload)
        return home_html

    orig = mod._fetch
    mod._fetch = _fake_fetch  # type: ignore[assignment]
    try:
        ctx = AdapterContext(
            base_url="https://x.com/",
            detected=DetectedPMS(pms="apts247", confidence=0.9),
            profile=None,
            expected_total_units=None,
            property_id="TEST",
            fetch_result=_FR(body=home_html.encode("utf-8")),
        )
        result = asyncio.run(Apts247Adapter().extract(None, ctx))  # type: ignore[arg-type]
    finally:
        mod._fetch = orig  # type: ignore[assignment]

    assert len(result.units) == 1, f"expected 1 unit, got {len(result.units)}"
    assert result.units[0].get("unit_number") == "673"
    assert len(result.plan_summaries) == 1, (
        f"expected 1 plan_summary, got {len(result.plan_summaries)}"
    )
    assert result.plan_summaries[0].get("floor_plan_name") == "Townhouse"
    unit_plan_names = {u.get("floor_plan_name") for u in result.units}
    assert "Townhouse" not in unit_plan_names, (
        "Townhouse leaked from plan_summaries into units — no-dup invariant violated"
    )


def test_apts247_mixed_floorplans_partition_correctly() -> None:
    """Real PID 271966 shape (4 plans: 3 with units, 1 without).
    Expected: 3 unit rows + 1 plan-only row."""
    data = {
        "objects": [
            {
                "name": "Studio", "display_bed": "Studio", "bath": 1,
                "sq_ft": "441", "rent": "$649",
                "units": [
                    {"id": 100, "number": "673", "rent": "$649",
                     "available_date": "2026-03-31"},
                ],
            },
            {
                "name": "1 Bedroom Townhome", "display_bed": "1 Bed", "bath": 1.5,
                "sq_ft": "886", "rent": "$979",
                "units": [
                    {"id": 200, "number": "747", "rent": "$979",
                     "available_date": "2026-04-09"},
                ],
            },
            {
                "name": "2 Bedroom Townhome", "display_bed": "2 Bed", "bath": 2,
                "sq_ft": "1100", "rent": "$1,299",
                "units": [
                    {"id": 300, "number": "830", "rent": "$1,299",
                     "available_date": "2026-05-15"},
                ],
            },
            {
                "name": "3 Bedroom Penthouse", "display_bed": "3 Bed", "bath": 2,
                "sq_ft": "1400",
            },
        ]
    }
    rows = parse_apts247_floorplans(data, "https://x.com/api/v1/floorplans/")
    unit_level = [r for r in rows if r["unit_number"]]
    plan_level = [r for r in rows if not r["unit_number"]]
    assert len(unit_level) == 3
    assert len(plan_level) == 1
    assert plan_level[0]["floor_plan_name"] == "3 Bedroom Penthouse"
    assert plan_level[0]["market_rent_low"] is None


# ──────────────────────────────────────────────────────────────────────
# SightMap — parse_sightmap_payload
# ──────────────────────────────────────────────────────────────────────


def _sightmap_body(
    floor_plans: list[dict[str, Any]], units: list[dict[str, Any]]
) -> dict[str, Any]:
    return {"data": {"floor_plans": floor_plans, "units": units}}


def test_sightmap_plans_without_units_emit_plan_rows() -> None:
    """3 floor plans, only 1 has units → 1 unit row + 2 plan_summary rows."""
    body = _sightmap_body(
        floor_plans=[
            {"id": 1, "name": "Studio", "bedroom_count": 0, "bathroom_count": 1,
             "lowest_rent": 1500, "highest_rent": 1700, "area_min": 500, "area_max": 600},
            {"id": 2, "name": "1BR", "bedroom_count": 1, "bathroom_count": 1,
             "lowest_rent": 1800, "highest_rent": 2100},
            {"id": 3, "name": "2BR", "bedroom_count": 2, "bathroom_count": 2,
             "lowest_rent": 2400, "highest_rent": 2700},
        ],
        units=[
            {"floor_plan_id": 1, "unit_number": "101", "price": 1550,
             "area": 520, "available_on": "2026-06-01"},
        ],
    )
    rows, dropped = parse_sightmap_payload(body, "https://x.com/api")
    assert dropped == 0
    assert len(rows) == 3

    unit_rows = [r for r in rows if r.get("unit_number")]
    plan_rows = [r for r in rows if not r.get("unit_number")]
    assert len(unit_rows) == 1
    assert unit_rows[0]["unit_number"] == "101"
    assert sorted(r["floor_plan_name"] for r in plan_rows) == ["1BR", "2BR"]


def test_sightmap_partition_holds_through_post_process() -> None:
    """End-to-end: emitted rows → post_process → ``units`` and
    ``plan_summaries`` partitions never share a floor_plan_name."""
    body = _sightmap_body(
        floor_plans=[
            {"id": 1, "name": "Studio", "bedroom_count": 0, "bathroom_count": 1},
            {"id": 2, "name": "1BR", "bedroom_count": 1, "bathroom_count": 1,
             "lowest_rent": 1800, "highest_rent": 2100, "area_min": 700, "area_max": 800},
        ],
        units=[
            {"floor_plan_id": 1, "unit_number": "S-1", "price": 1500,
             "area": 500, "available_on": "2026-06-01"},
        ],
    )
    rows, _ = parse_sightmap_payload(body, "https://x.com/api")
    pp = post_process(rows, property_id="P1")
    unit_fps = {u.get("floor_plan_name") for u in pp.units}
    plan_fps = {p.get("floor_plan_name") for p in pp.plan_summaries}
    assert unit_fps.isdisjoint(plan_fps)
    assert plan_fps == {"1BR"}


def test_sightmap_all_plans_have_units_emits_no_plan_rows() -> None:
    """When every plan has at least one unit, no plan_summary rows are emitted."""
    body = _sightmap_body(
        floor_plans=[
            {"id": 1, "name": "Studio", "bedroom_count": 0, "bathroom_count": 1},
            {"id": 2, "name": "1BR", "bedroom_count": 1, "bathroom_count": 1},
        ],
        units=[
            {"floor_plan_id": 1, "unit_number": "101", "price": 1500, "area": 500},
            {"floor_plan_id": 2, "unit_number": "201", "price": 1800, "area": 700},
        ],
    )
    rows, _ = parse_sightmap_payload(body, "https://x.com/api")
    plan_rows = [r for r in rows if not r.get("unit_number")]
    assert plan_rows == []


def test_sightmap_fully_leased_property_still_surfaces_plans() -> None:
    """Property with floor_plans but zero available units now surfaces
    the plans (pre-fix returned []) — critical for fully-leased
    properties where the marketing site still lists the plan catalog."""
    body = _sightmap_body(
        floor_plans=[
            {"id": 1, "name": "Studio", "bedroom_count": 0, "bathroom_count": 1,
             "lowest_rent": 1500},
            {"id": 2, "name": "1BR", "bedroom_count": 1, "bathroom_count": 1,
             "lowest_rent": 1800},
        ],
        units=[],
    )
    rows, _ = parse_sightmap_payload(body, "https://x.com/api")
    assert len(rows) == 2
    assert all(r.get("unit_number") == "" for r in rows)


# ──────────────────────────────────────────────────────────────────────
# G5 — parse_g5_apartments
# ──────────────────────────────────────────────────────────────────────


def _g5_payload(
    floorplans: list[dict[str, Any]], apartments: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "data": {"apartmentComplex": {"floorplans": floorplans, "apartments": apartments}}
    }


def test_g5_floorplans_without_apartments_emit_plan_rows() -> None:
    """4 floorplans, 2 with apartments → 2 unit rows + 2 plan_summary rows."""
    payload = _g5_payload(
        floorplans=[
            {"id": "A", "name": "Plan A", "beds": 1, "baths": 1, "sqft": 700,
             "minPrice": 1500, "maxPrice": 1700},
            {"id": "B", "name": "Plan B", "beds": 2, "baths": 2, "sqft": 1000,
             "minPrice": 2000, "maxPrice": 2300},
            {"id": "C", "name": "Plan C", "beds": 0, "baths": 1, "sqft": 500,
             "minPrice": 1200, "maxPrice": 1300},
            {"id": "D", "name": "Plan D", "beds": 3, "baths": 2, "sqft": 1300,
             "minPrice": 2800},
        ],
        apartments=[
            {"name": "101", "prices": [{"priceType": "min_rent", "value": 1500}],
             "floorplan": {"id": "A", "name": "Plan A", "beds": 1, "baths": 1, "sqft": 700}},
            {"name": "201", "prices": [{"priceType": "min_rent", "value": 2100}],
             "floorplan": {"id": "B", "name": "Plan B", "beds": 2, "baths": 2, "sqft": 1000}},
        ],
    )
    rows = parse_g5_apartments(payload)
    assert len(rows) == 4

    unit_rows = [r for r in rows if r.get("unit_number")]
    plan_rows = [r for r in rows if not r.get("unit_number")]
    assert {r["unit_number"] for r in unit_rows} == {"101", "201"}
    assert {r["floor_plan_name"] for r in plan_rows} == {"Plan C", "Plan D"}


def test_g5_partition_holds_through_post_process() -> None:
    """Plans covered by an apartment must not appear in plan_summaries
    after the post_process round-trip."""
    payload = _g5_payload(
        floorplans=[
            {"id": "A", "name": "Plan A", "beds": 1, "baths": 1, "sqft": 700},
            {"id": "X", "name": "Plan X (no units)", "beds": 2, "baths": 2,
             "sqft": 1000, "minPrice": 2200},
        ],
        apartments=[
            {"name": "101", "prices": [{"priceType": "min_rent", "value": 1500}],
             "floorplan": {"id": "A", "name": "Plan A", "beds": 1, "baths": 1, "sqft": 700}},
        ],
    )
    rows = parse_g5_apartments(payload)
    pp = post_process(rows, property_id="P1")
    unit_fps = {u.get("floor_plan_name") for u in pp.units}
    plan_fps = {p.get("floor_plan_name") for p in pp.plan_summaries}
    assert unit_fps == {"Plan A"}
    assert plan_fps == {"Plan X (no units)"}


def test_g5_empty_payload_emits_no_rows() -> None:
    """No floorplans + no apartments → empty list (no spurious plan rows)."""
    payload = _g5_payload(floorplans=[], apartments=[])
    rows = parse_g5_apartments(payload)
    assert rows == []


# ──────────────────────────────────────────────────────────────────────
# AvalonBay — AvalonBayAdapter.extract (bedroom-bucket plan rows)
# ──────────────────────────────────────────────────────────────────────


class _FakeCtx:
    def __init__(self, api_responses: list[dict[str, Any]]) -> None:
        self._api_responses = api_responses
        self.property_id = "TEST"


def _ab_response(units: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": "https://www.avaloncommunities.com/api/community-units",
        "body": {"units": units, "unitsSummary": summary},
    }


def test_avalonbay_unit_parser_emits_one_row_per_unit() -> None:
    """Direct parser regression guard — unit-level rows still emit
    correctly after the plan_summary emission was added."""
    units = [
        {
            "unitName": "0101", "bedroomNumber": "1", "bathroomNumber": "1",
            "squareFeet": "700", "floorPlan": {"name": "1A"},
            "availableDateUnfurnished": "2026-06-15",
        },
    ]
    summary = {"totalPricesStartingAt": {"1": {"unfurnished": 1800}}}
    rows = parse_avalonbay_units(units, "https://x.com/api", summary)
    assert len(rows) == 1
    assert rows[0]["unit_number"] == "0101"


def test_avalonbay_bedroom_buckets_without_live_units_emit_plan_rows() -> None:
    """A bedroom bucket in unitsSummary with no live unit row becomes a
    plan_summary (the marketing site shows it as e.g. "Studios from $1,500")."""
    import asyncio

    from ma_poc.pms.adapters.avalonbay import AvalonBayAdapter

    # Bedrooms 1+2 have live units; bedrooms 0+3 are summary-only.
    units = [
        {
            "unitName": "1A-101", "bedroomNumber": "1", "bathroomNumber": "1",
            "squareFeet": "700", "floorPlan": {"name": "1A"},
        },
        {
            "unitName": "2B-201", "bedroomNumber": "2", "bathroomNumber": "2",
            "squareFeet": "1000", "floorPlan": {"name": "2B"},
        },
    ]
    summary = {
        "totalPricesStartingAt": {
            "0": {"unfurnished": 1500},
            "1": {"unfurnished": 1800},
            "2": {"unfurnished": 2400},
            "3": {"unfurnished": 3200},
        }
    }
    ctx = _FakeCtx([_ab_response(units, summary)])
    result = asyncio.run(AvalonBayAdapter().extract(None, ctx))  # type: ignore[arg-type]

    assert len(result.units) == 2
    assert {u.get("bedrooms") for u in result.units} == {"1", "2"}
    assert len(result.plan_summaries) == 2
    assert sorted(p.get("bedrooms") for p in result.plan_summaries) == ["0", "3"]


def test_avalonbay_every_bucket_has_a_unit_emits_no_plan_rows() -> None:
    """No-dup invariant: every summary bucket with a live unit → no plan rows."""
    import asyncio

    from ma_poc.pms.adapters.avalonbay import AvalonBayAdapter

    units = [
        {
            "unitName": "S-101", "bedroomNumber": "0", "bathroomNumber": "1",
            "squareFeet": "500", "floorPlan": {"name": "Studio"},
        },
        {
            "unitName": "1A-101", "bedroomNumber": "1", "bathroomNumber": "1",
            "squareFeet": "700", "floorPlan": {"name": "1A"},
        },
    ]
    summary = {
        "totalPricesStartingAt": {
            "0": {"unfurnished": 1500},
            "1": {"unfurnished": 1800},
        }
    }
    ctx = _FakeCtx([_ab_response(units, summary)])
    result = asyncio.run(AvalonBayAdapter().extract(None, ctx))  # type: ignore[arg-type]
    assert len(result.units) == 2
    assert result.plan_summaries == []
