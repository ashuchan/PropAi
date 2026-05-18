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

from ma_poc.pms.adapters._api_parser import (
    _detect_plan_unit_pair,
    _merge_units_with_plans,
    parse_api_responses,
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
