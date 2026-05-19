"""Tests for the plan+unit two-list merge in ``_api_parser.py``.

Targets ``_detect_plan_unit_pair`` and ``_merge_units_with_plans``, plus
the ``parse_generic_api`` integration that picks the merged candidates
when the walker returns both lists.

Origin: Knock RentManager API (``doorway-api.knockrentals.com/v1/
property/{id}/units``) returns ``{layouts: [60 plans], units: [30
instances]}``. The pre-fix walker picked ``layouts`` (longer list)
and shipped 60 plan-level rows with ``rent_low=None``,
``available_date=None`` — even though ``units`` carried the
``displayPrice`` and ``availableOn`` per apartment. PID 253774
(2026-05-18) was the canonical case.

The fix is generic: detect ANY (plan-list, unit-list) pair via foreign
key reference (``layoutId`` / ``floorPlanId`` / ``planId`` / ...) and
merge the plan fields into each unit. Adding a new vendor's FK key to
``_PLAN_FK_KEYS`` extends the detector without per-vendor code.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters._api_parser import (
    _detect_plan_unit_pair,
    _merge_units_with_plans,
    parse_api_responses,
)
from ma_poc.services.source_planner import (
    evaluate_completeness,
    plan_next_action,
)


# ── _detect_plan_unit_pair ───────────────────────────────────────────────────


def test_detects_knock_layout_units_pair() -> None:
    """Exact PID 253774 shape: layouts (plan) + units (instances) joined
    by ``layoutId``."""
    layouts = [
        {"id": "p1", "name": "1x1A", "bedrooms": 1, "bathrooms": 1, "area": 821},
        {"id": "p2", "name": "2x2B", "bedrooms": 2, "bathrooms": 2, "area": 1220},
    ]
    units = [
        {"name": "4103", "layoutId": "p1", "displayPrice": "2408",
         "availableOn": "2026-05-19", "available": True},
        {"name": "0243", "layoutId": "p1", "displayPrice": "2251",
         "availableOn": "2026-07-26", "available": True},
        {"name": "0488", "layoutId": "p2", "displayPrice": "3191",
         "availableOn": "2026-05-22", "available": True},
    ]
    result = _detect_plan_unit_pair([layouts, units])
    assert result is not None
    plan_list, unit_list, fk_key = result
    assert plan_list is layouts
    assert unit_list is units
    assert fk_key == "layoutId"


def test_detects_snake_case_fk_key() -> None:
    """Some vendors snake_case the FK — ``floor_plan_id``."""
    plans = [{"id": "p1", "bedrooms": 1, "area": 800}]
    units = [
        {"id": "u1", "floor_plan_id": "p1", "price": "2000", "available_date": "2026-06-01"},
        {"id": "u2", "floor_plan_id": "p1", "price": "2050", "available_date": "2026-06-05"},
    ]
    result = _detect_plan_unit_pair([plans, units])
    assert result is not None
    _, _, fk_key = result
    assert fk_key == "floor_plan_id"


def test_no_pair_when_only_one_list() -> None:
    """A single candidate list (plan-only OR unit-only) — nothing to
    join. Walker falls back to "largest wins"."""
    plans = [{"id": "p1", "bedrooms": 1, "area": 800}]
    assert _detect_plan_unit_pair([plans]) is None


def test_no_pair_when_fk_doesnt_resolve() -> None:
    """Unit list has FK key but the values don't match any plan's ``id``
    — could be a coincidence (the FK field is unrelated), not a real
    plan/unit pair. Need at least 2 matches to admit."""
    plans = [{"id": "X1", "name": "Plan X", "bedrooms": 1, "area": 800}]
    units = [
        {"name": "1", "layoutId": "ORPHAN-1", "displayPrice": "2000",
         "availableOn": "2026-06-01"},
        {"name": "2", "layoutId": "ORPHAN-2", "displayPrice": "2050",
         "availableOn": "2026-06-05"},
    ]
    assert _detect_plan_unit_pair([plans, units]) is None


def test_no_pair_when_unit_list_lacks_per_unit_signal() -> None:
    """A list that has FK + ids but no rent / availability /
    unit-number key is just another plan list. Don't admit it as the
    unit-list — the walker's largest-wins fallback can handle it."""
    plans = [{"id": "p1", "name": "Plan A", "bedrooms": 1, "area": 800}]
    plan_variants = [
        {"id": "v1", "layoutId": "p1", "bedrooms": 1, "area": 800},
        {"id": "v2", "layoutId": "p1", "bedrooms": 1, "area": 820},
    ]
    # plan_variants has FK to plans but no per-unit signal (no rent,
    # no availability, no unit_number) — should NOT trigger merge.
    assert _detect_plan_unit_pair([plans, plan_variants]) is None


def test_picks_best_pair_when_multiple_candidates() -> None:
    """Multiple plan-list candidates exist — picks the one with the
    most FK resolutions. Defends against coincidental matches with
    smaller sibling lists."""
    real_plans = [
        {"id": "p1", "bedrooms": 1, "area": 800},
        {"id": "p2", "bedrooms": 2, "area": 1100},
        {"id": "p3", "bedrooms": 2, "area": 1200},
    ]
    decoy_plans = [
        {"id": "X1", "bedrooms": 1, "area": 500},
    ]
    units = [
        {"name": "1", "layoutId": "p1", "displayPrice": "2000", "availableOn": "2026-06-01"},
        {"name": "2", "layoutId": "p2", "displayPrice": "3000", "availableOn": "2026-06-05"},
        {"name": "3", "layoutId": "p3", "displayPrice": "3100", "availableOn": "2026-06-10"},
    ]
    result = _detect_plan_unit_pair([decoy_plans, real_plans, units])
    assert result is not None
    plan_list, unit_list, _ = result
    assert plan_list is real_plans
    assert unit_list is units


def test_handles_alternate_fk_key_floorplanid() -> None:
    """``floorPlanId`` camelCase — another vendor variant."""
    plans = [{"id": "p1", "bedrooms": 1, "area": 800}]
    units = [
        {"id": "u1", "floorPlanId": "p1", "monthlyRent": "2000",
         "availableDate": "2026-06-01"},
        {"id": "u2", "floorPlanId": "p1", "monthlyRent": "2100",
         "availableDate": "2026-06-05"},
    ]
    result = _detect_plan_unit_pair([plans, units])
    assert result is not None
    _, _, fk_key = result
    assert fk_key == "floorPlanId"


# ── _merge_units_with_plans ──────────────────────────────────────────────────


def test_merge_enriches_unit_with_plan_attributes() -> None:
    """Unit dict has the rent + availability; plan supplies bedrooms /
    area / bathrooms — but unit-side values always win on conflict."""
    plans = [
        {"id": "p1", "name": "1x1A", "bedrooms": 1, "bathrooms": 1, "area": 821},
    ]
    units = [
        {"name": "4103", "layoutId": "p1", "displayPrice": "2408",
         "availableOn": "2026-05-19", "available": True},
    ]
    merged = _merge_units_with_plans(units, plans, "layoutId")
    assert len(merged) == 1
    u = merged[0]
    # Plan-supplied fields
    assert u["bedrooms"] == 1
    assert u["bathrooms"] == 1
    assert u["area"] == 821
    # Unit-supplied fields
    assert u["displayPrice"] == "2408"
    assert u["availableOn"] == "2026-05-19"
    assert u["available"] is True


def test_merge_unit_value_overrides_plan_value_on_conflict() -> None:
    """If a unit has its own area / bedrooms (per-apartment override),
    the unit value wins."""
    plans = [
        {"id": "p1", "bedrooms": 1, "area": 821},
    ]
    units = [
        {"name": "4103", "layoutId": "p1", "bedrooms": 1, "area": 850,
         "displayPrice": "2408"},
    ]
    merged = _merge_units_with_plans(units, plans, "layoutId")
    assert merged[0]["area"] == 850  # unit wins over plan's 821


def test_merge_promotes_unit_name_to_unit_number() -> None:
    """Knock-shape: the unit row's ``name`` field carries the
    apartment number (e.g. "4103"). After merge the canonical
    ``unit_number`` field must be set so the downstream picker can
    grab it directly — without conflating with the plan name (which
    sits in ``layoutName`` / ``planName``)."""
    plans = [{"id": "p1", "name": "Plan A", "bedrooms": 1, "area": 800}]
    units = [
        {"name": "4103", "layoutId": "p1", "displayPrice": "2408",
         "availableOn": "2026-05-19"},
    ]
    merged = _merge_units_with_plans(units, plans, "layoutId")
    assert merged[0]["unit_number"] == "4103"


def test_merge_strips_plan_bookkeeping_fields() -> None:
    """``createdAt`` / ``modifiedAt`` / ``deletedAt`` / etc. on the
    plan side would just be noise on the merged unit. Strip them
    during merge so downstream consumers see a clean row."""
    plans = [
        {"id": "p1", "name": "Plan A", "bedrooms": 1, "area": 800,
         "createdAt": "2025-11-25T19:41:51",
         "modifiedAt": "2025-11-25T19:41:51",
         "deletedAt": None,
         "deletedByVendor": False,
         "integrationId": "abc-123",
         "images": [],
         "description": None},
    ]
    units = [
        {"name": "4103", "layoutId": "p1", "displayPrice": "2408",
         "availableOn": "2026-05-19"},
    ]
    merged = _merge_units_with_plans(units, plans, "layoutId")
    u = merged[0]
    assert "createdAt" not in u
    assert "modifiedAt" not in u
    assert "deletedAt" not in u
    assert "integrationId" not in u
    assert "images" not in u


def test_merge_keeps_unit_when_fk_resolves_to_no_plan() -> None:
    """An orphan unit (FK doesn't match any plan) should still ship —
    the unit-side fields are valid on their own."""
    plans = [{"id": "p1", "name": "Plan A", "bedrooms": 1, "area": 800}]
    units = [
        {"name": "4103", "layoutId": "p1", "displayPrice": "2408"},
        {"name": "9999", "layoutId": "MISSING", "displayPrice": "3000"},
    ]
    merged = _merge_units_with_plans(units, plans, "layoutId")
    assert len(merged) == 2
    assert merged[1]["displayPrice"] == "3000"


def test_merge_skips_non_dict_items() -> None:
    """A robust merge ignores non-dict entries (defensive against
    pathological payloads)."""
    plans = [{"id": "p1", "bedrooms": 1, "area": 800}, "garbage"]
    units = [
        {"name": "1", "layoutId": "p1", "displayPrice": "2000"},
        None,
        ["not a dict"],
    ]
    merged = _merge_units_with_plans(units, plans, "layoutId")
    # Only the one valid unit survives.
    assert len(merged) == 1


# ── parse_api_responses integration ──────────────────────────────────────────


def _make_knock_response() -> dict:
    """Minimal Knock RentManager response shape from PID 253774.

    NB: ``body`` is a parsed dict, not a JSON string. ``pms/scraper.py``
    parses the bytes into a dict via ``json.loads`` upstream of
    ``parse_api_responses``; the parser's type checks
    (``isinstance(data, dict)``) rely on that contract. Tests must mirror
    it.
    """
    return {
        "url": "https://doorway-api.knockrentals.com/v1/property/2012614/units",
        "content_type": "application/json",
        "body": {
            "units_data": {
                "buildings": [],
                "layouts": [
                    {"id": "p1", "name": "1x1A6 Greater HeightsC",
                     "bedrooms": 1, "bathrooms": 1, "area": 836},
                    {"id": "p2", "name": "2x2B3 Post OakB",
                     "bedrooms": 2, "bathrooms": 2, "area": 1220},
                ],
                "units": [
                    {"name": "4103", "layoutId": "p1", "displayPrice": "2408",
                     "availableOn": "2026-05-19", "available": True,
                     "bedrooms": 1, "bathrooms": 1, "area": 836},
                    {"name": "0488", "layoutId": "p2", "displayPrice": "3191",
                     "availableOn": "2026-05-22", "available": True,
                     "bedrooms": 2, "bathrooms": 2, "area": 1220},
                ],
            },
        },
    }


def test_parse_api_responses_picks_unit_list_not_layout_list() -> None:
    """End-to-end: a Knock-shape response must surface the UNIT rows
    (with rent + availability), not the layouts (plan-only). Pre-fix
    we picked layouts because it was the longer list — symptom was
    zero rent / zero availability across every output row."""
    out = parse_api_responses([_make_knock_response()])
    # Two unit instances in the fixture above; expect two output rows.
    assert len(out) == 2

    # Each row must carry rent (from displayPrice) and availability
    # (from availableOn). The pre-fix path returned both as empty.
    rents = sorted(row.get("rent_range") for row in out)
    assert rents == ["$2,408", "$3,191"]
    # Each row carries the unit number from the unit's ``name`` field,
    # not the layout's UUID.
    unit_numbers = sorted(row.get("unit_number") or "" for row in out)
    assert unit_numbers == ["0488", "4103"]
    # Floor-plan name comes from the merged layout. The merge promotes
    # the plan's ``name`` into ``planName`` BEFORE the unit overlay so
    # the unit's own ``name`` field (the unit number) doesn't clobber
    # it. The picker then resolves ``planName`` ahead of ``name``.
    plan_names = sorted(row.get("floor_plan_name") or "" for row in out)
    assert plan_names == ["1x1A6 Greater HeightsC", "2x2B3 Post OakB"]


def test_parse_api_responses_layouts_only_still_works() -> None:
    """Backwards-compat: when the response has only the plans list (no
    units), the existing "largest list wins" path picks the plans and
    emits plan-level rows. No regression for the plan-only case."""
    response = {
        "url": "https://example.com/api/floorplans",
        "content_type": "application/json",
        "body": {
            "floorplans": [
                {"id": "p1", "name": "1x1", "bedrooms": 1, "area": 800},
                {"id": "p2", "name": "2x2", "bedrooms": 2, "area": 1100},
            ],
        },
    }
    out = parse_api_responses([response])
    # 2 plans -> 2 plan-level rows (no rent, no per-unit identity).
    plan_names = sorted(row.get("floor_plan_name") or "" for row in out)
    assert plan_names == ["1x1", "2x2"]


# ── Gap 1 (2026-05-19): unmatched plans appended as plan-summary rows ────────


def test_merge_appends_unmatched_plans_as_plan_rows() -> None:
    """PID 268552 shape: 7 plans, both currently-available units sit on
    the SAME plan (p2). Matched ids = {p2}; unmatched = {p1, p3..p7} = 6.

    Pre-Gap-1 those 6 plans were silently dropped — the property emitted
    2 units (vs ground-truth 7 floor plans on site). Post-fix the merged
    result is 2 unit-shape rows + 6 plan-shape rows = 8 total. The
    downstream classifier in ``extraction.classify`` partitions the plan
    rows to ``plan_summaries`` and the v2 formatter ships them under
    ``floor_plans[]`` (playbook §8.18).
    """
    plans = [
        {"id": "p1", "name": "Studio S1", "bedrooms": 0, "bathrooms": 1,
         "area": 530, "minRent": 1450, "maxRent": 1550},
        {"id": "p2", "name": "1Bed A1",   "bedrooms": 1, "bathrooms": 1,
         "area": 720, "minRent": 1700, "maxRent": 1900},
        {"id": "p3", "name": "1Bed A2",   "bedrooms": 1, "bathrooms": 1,
         "area": 760, "minRent": 1750, "maxRent": 1950},
        {"id": "p4", "name": "1Bed A3",   "bedrooms": 1, "bathrooms": 1.5,
         "area": 810, "minRent": 1850, "maxRent": 2100},
        {"id": "p5", "name": "2Bed B1",   "bedrooms": 2, "bathrooms": 2,
         "area": 1050, "minRent": 2400, "maxRent": 2700},
        {"id": "p6", "name": "2Bed B2",   "bedrooms": 2, "bathrooms": 2,
         "area": 1120, "minRent": 2500, "maxRent": 2800},
        {"id": "p7", "name": "3Bed C1",   "bedrooms": 3, "bathrooms": 2,
         "area": 1320, "minRent": 3100, "maxRent": 3400},
    ]
    units = [
        # Two currently-available apartments, both on the 1Bed A1 plan.
        {"name": "101", "layoutId": "p2", "displayPrice": "1800",
         "availableOn": "2026-06-01", "available": True},
        {"name": "203", "layoutId": "p2", "displayPrice": "1850",
         "availableOn": "2026-06-15", "available": True},
    ]
    merged = _merge_units_with_plans(units, plans, "layoutId")
    # 2 unit + 6 unmatched plans = 8 (only plan p2 was referenced by
    # units and is folded into them; p1, p3..p7 ship as plan rows).
    assert len(merged) == 8

    unit_rows = [r for r in merged if r.get("unit_number") is not None]
    assert len(unit_rows) == 2
    assert sorted(r["unit_number"] for r in unit_rows) == ["101", "203"]

    plan_rows = [r for r in merged if r.get("unit_number") is None]
    assert len(plan_rows) == 6
    plan_names = sorted(
        r.get("planName") or r.get("name") or "" for r in plan_rows
    )
    assert plan_names == [
        "1Bed A2", "1Bed A3", "2Bed B1", "2Bed B2", "3Bed C1", "Studio S1",
    ]
    # Defensive: none of the plan-shape rows carry a key the row-picker
    # would walk for ``unit_number`` — otherwise the plan PK would leak
    # as a unit number downstream.
    for p in plan_rows:
        for k in ("id", "unitNumber", "unit_number", "unitId", "unit_id",
                  "label", "unitCode"):
            assert k not in p, (
                f"plan row carries picker key {k!r} = {p[k]!r}"
            )


def test_merge_unmatched_plans_strip_database_pk_fields() -> None:
    """A plan dict's ``id`` is the plan's database PK. The
    ``parse_api_responses`` row-picker uses ``id`` as the LAST fallback
    for ``unit_number``. If we let it through, the resulting v2 row would
    have a meaningless unit_number (the plan PK) and ``classify`` would
    misroute it to ``units`` instead of ``plan_summaries``.

    Strip every alias the picker walks: ``id`` / ``unitNumber`` /
    ``unitId`` / ``label`` / ``unitCode`` / etc.
    """
    plans = [
        {"id": "PLAN-DB-PK-42", "name": "1Bed A", "bedrooms": 1, "area": 800,
         "minRent": 1700},
        {"id": "PLAN-OTHER", "name": "Other", "bedrooms": 1, "area": 700},
    ]
    units = [
        {"name": "U-01", "layoutId": "PLAN-OTHER", "displayPrice": "1800",
         "availableOn": "2026-06-01"},
        {"name": "U-02", "layoutId": "PLAN-OTHER", "displayPrice": "1850",
         "availableOn": "2026-06-15"},
    ]
    merged = _merge_units_with_plans(units, plans, "layoutId")
    unmatched = [r for r in merged
                 if r.get("planName") == "1Bed A"
                 or r.get("name") == "1Bed A"]
    assert len(unmatched) == 1
    p = unmatched[0]
    for k in ("id", "unitNumber", "unit_number", "unitId", "unit_id",
              "UnitNumber", "label", "display_unit_number",
              "unitCode", "unit_code"):
        assert k not in p, (
            f"unmatched plan still carries picker key {k!r} = {p[k]!r}"
        )


def test_parse_api_responses_ships_unmatched_plans_as_plan_summary_rows() -> None:
    """End-to-end: a Knock-shape response with more plans than units
    must surface every plan downstream. Verifies (1) every plan ships
    as a row in ``parse_api_responses`` output, and (2) the rows for
    plans without a current FK-matched unit have no ``unit_number`` so
    the classifier routes them to ``plan_summaries``.
    """
    response = {
        "url": "https://doorway-api.knockrentals.com/v1/property/268552/units",
        "content_type": "application/json",
        # Wrap in ``units_data`` so the walker fallback runs (matches the
        # real Knock response shape — keyed walker doesn't know
        # ``units_data`` so falls through to ``find_unit_arrays``).
        "body": {
            "units_data": {
                "layouts": [
                    {"id": "p1", "name": "Studio S1", "bedrooms": 0,
                     "bathrooms": 1, "area": 530, "minRent": 1450,
                     "maxRent": 1550},
                    {"id": "p2", "name": "1Bed A1", "bedrooms": 1,
                     "bathrooms": 1, "area": 720, "minRent": 1700,
                     "maxRent": 1900},
                    {"id": "p3", "name": "2Bed B1", "bedrooms": 2,
                     "bathrooms": 2, "area": 1050, "minRent": 2400,
                     "maxRent": 2700},
                ],
                "units": [
                    {"name": "101", "layoutId": "p2", "displayPrice": "1800",
                     "availableOn": "2026-06-01", "available": True,
                     "bedrooms": 1, "bathrooms": 1, "area": 720},
                    {"name": "203", "layoutId": "p2", "displayPrice": "1850",
                     "availableOn": "2026-06-15", "available": True,
                     "bedrooms": 1, "bathrooms": 1, "area": 720},
                ],
            },
        },
    }
    out = parse_api_responses([response])
    # 2 units (Knock-merged) + 2 unmatched plans (Studio S1, 2Bed B1) = 4.
    assert len(out) == 4

    unit_rows = [r for r in out if r.get("unit_number")]
    assert sorted(r["unit_number"] for r in unit_rows) == ["101", "203"]

    plan_rows = [r for r in out if not r.get("unit_number")]
    assert len(plan_rows) == 2
    plan_names = sorted(r.get("floor_plan_name") or "" for r in plan_rows)
    assert plan_names == ["2Bed B1", "Studio S1"]
    for r in plan_rows:
        assert r.get("rent_range"), (
            f"plan row {r.get('floor_plan_name')!r} lost its rent range"
        )


def test_parse_api_responses_emits_plan_rows_even_when_rent_null() -> None:
    """PID 253774 shape: layouts with ``minRent: null`` (the original
    bfeda6c bug case — these were the rows that shipped as units with
    null rent pre-bfeda6c). Post-Gap-1 they still ship as plan rows in
    ``parse_api_responses``; the classifier downstream routes them to
    ``plan_summaries`` because they lack unit_number + per-unit
    signals — they don't re-enter ``units[]``.
    """
    response = {
        "url": "https://doorway-api.knockrentals.com/v1/property/253774/units",
        "content_type": "application/json",
        "body": {
            "units_data": {
                "layouts": [
                    {"id": "p1", "name": "A1", "bedrooms": 1, "bathrooms": 1,
                     "area": 821, "minRent": None, "maxRent": None},
                    {"id": "p2", "name": "B1", "bedrooms": 2, "bathrooms": 2,
                     "area": 1100, "minRent": None, "maxRent": None},
                    # p3 has live units; serves as the FK target so the
                    # pair detector fires.
                    {"id": "p3", "name": "C1", "bedrooms": 1, "bathrooms": 1,
                     "area": 700, "minRent": 1700, "maxRent": 1900},
                ],
                "units": [
                    {"name": "U-01", "layoutId": "p3", "displayPrice": "1800",
                     "availableOn": "2026-06-01",
                     "bedrooms": 1, "bathrooms": 1, "area": 700},
                    {"name": "U-02", "layoutId": "p3", "displayPrice": "1850",
                     "availableOn": "2026-06-15",
                     "bedrooms": 1, "bathrooms": 1, "area": 700},
                ],
            },
        },
    }
    out = parse_api_responses([response])
    # 2 unit rows + 2 unmatched plans with null rent = 4.
    assert len(out) == 4
    plan_rows = [r for r in out if not r.get("unit_number")]
    assert len(plan_rows) == 2
    for r in plan_rows:
        # No unit_number (would route to ``units``). Picker emits empty
        # string when no key resolved; check truthiness.
        assert not r.get("unit_number"), (
            f"plan row leaked unit_number={r.get('unit_number')!r}"
        )
        assert not r.get("availability_date")
        assert (r.get("availability_status") or "") == ""


def test_merge_no_unmatched_plans_when_every_plan_referenced() -> None:
    """Backwards-compat: when every plan is FK-referenced by at least
    one unit, the unmatched-plan appender is a no-op. Pre-Gap-1
    behaviour is preserved exactly."""
    plans = [
        {"id": "p1", "name": "1Bed A", "bedrooms": 1, "area": 800,
         "minRent": 1700},
        {"id": "p2", "name": "2Bed B", "bedrooms": 2, "area": 1100,
         "minRent": 2400},
    ]
    units = [
        {"name": "101", "layoutId": "p1", "displayPrice": "1800",
         "availableOn": "2026-06-01"},
        {"name": "201", "layoutId": "p2", "displayPrice": "2500",
         "availableOn": "2026-06-05"},
    ]
    merged = _merge_units_with_plans(units, plans, "layoutId")
    assert len(merged) == 2
    assert all(r.get("unit_number") for r in merged)


def test_merge_plan_with_only_id_field_not_emitted() -> None:
    """Defensive: a plan stub with NO meaningful attributes beyond its
    PK (deleted-but-not-purged) is appended to the merge output, but
    the picker drops it via the existing ``skipped_no_fields`` gate at
    ``_api_parser.py`` line 1193. Round-trip is safe — only real rows
    survive ``parse_api_responses``."""
    plans = [
        {"id": "p1", "name": "1Bed A", "bedrooms": 1, "area": 800,
         "minRent": 1700},
        {"id": "p2"},  # empty stub
    ]
    units = [
        {"name": "101", "layoutId": "p1", "displayPrice": "1800",
         "availableOn": "2026-06-01", "bedrooms": 1, "area": 800},
        {"name": "102", "layoutId": "p1", "displayPrice": "1850",
         "availableOn": "2026-06-15", "bedrooms": 1, "area": 800},
    ]
    response = {
        "url": "https://example.com/api/inventory",
        "content_type": "application/json",
        "body": {"units_data": {"layouts": plans, "units": units}},
    }
    out = parse_api_responses([response])
    # 2 unit rows; the empty plan stub is dropped by the picker's
    # skipped_no_fields gate.
    assert len(out) == 2
    assert sorted(r["unit_number"] for r in out) == ["101", "102"]


def test_merge_pct_complete_falls_below_stop_floor() -> None:
    """The planner-completeness side-effect (Gap 2 fallout): when
    unmatched plans are appended, the resulting row mix has split
    identity coverage. Plan rows lack ``unit_number`` → ``pct_with_identity``
    drops → ``pct_complete`` drops below the 0.90 STOP floor in
    ``source_planner.plan_next_action``.

    Pre-Gap-1 the 2 merged units had pct_complete=1.0 → STOP; post-Gap-1
    the 8 rows have pct_complete ≈ 2/8 = 0.25 → planner escalates.
    """
    plans = [
        {"id": f"p{i}", "name": f"Plan {i}", "bedrooms": 1, "bathrooms": 1,
         "area": 700 + i * 50, "minRent": 1500 + i * 100,
         "maxRent": 1700 + i * 100}
        for i in range(1, 8)  # 7 plans, p1..p7
    ]
    units = [
        {"name": "101", "layoutId": "p2", "displayPrice": "1800",
         "availableOn": "2026-06-01", "availability_status": "AVAILABLE",
         "bedrooms": 1, "bathrooms": 1, "area": 750},
        {"name": "203", "layoutId": "p2", "displayPrice": "1850",
         "availableOn": "2026-06-15", "availability_status": "AVAILABLE",
         "bedrooms": 1, "bathrooms": 1, "area": 750},
    ]
    response = {
        "url": "https://doorway-api.knockrentals.com/v1/property/268552/units",
        "content_type": "application/json",
        "body": {"units_data": {"layouts": plans, "units": units}},
    }
    out = parse_api_responses([response])

    def _to_planner_row(r: dict) -> dict:
        rent_range = r.get("rent_range") or ""
        rent_low: int | None = None
        rent_high: int | None = None
        if rent_range:
            digits = "".join(c if c.isdigit() else " "
                             for c in rent_range).split()
            if digits:
                rent_low = int(digits[0])
                rent_high = int(digits[-1])
        return {
            "unit_id": r.get("unit_number"),
            "unit_number": r.get("unit_number"),
            "floor_plan_name": r.get("floor_plan_name"),
            "beds": r.get("bedrooms"),
            "baths": r.get("bathrooms"),
            "area": r.get("sqft"),
            "rent_low": rent_low,
            "rent_high": rent_high,
            "available_date": r.get("availability_date"),
            "availability_status": r.get("availability_status"),
        }

    rows = [_to_planner_row(r) for r in out]
    report = evaluate_completeness(rows)

    # 8 rows (2 units on p2 + 6 unmatched plans). 2 carry identity.
    assert report.n_units == 8
    assert report.pct_with_identity == pytest.approx(2 / 8, abs=0.01)
    assert report.pct_complete <= report.pct_with_identity + 1e-9
    decision = plan_next_action(
        report,
        sources_already_run=set(),
        budget_remaining={"link_hop": 1, "llm_monolithic": 1,
                          "llm_api_calls": 1, "llm_dom_calls": 1},
        pms_name="unknown",
    )
    assert decision.action != "STOP", (
        f"planner STOPPED prematurely on FK-merged result "
        f"({report.n_units} rows, pct_complete={report.pct_complete:.2f}); "
        f"Gap 2 regression — got Decision({decision})"
    )
