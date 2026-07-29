"""``is_floor_plan_level`` completeness at the PRODUCTION output boundary.

Production runs ``scripts/runners/jugnu.py``, which carries its own fork of
``_format_v2_unit``. The flag has been lost in that fork before (2026-07-25:
added to ``core/schema_v2.py`` only, 4,024 SightMap plan rows shipped
unflagged), so every assertion here goes through ``jugnu._format_v2`` — the
function that actually writes ``properties.json`` — not the library copy.

Measured on run-2026-07-27-full-0d54ca7 (4,982 properties / 104,964 unit rows):

  * 5,427 rows carried the flag; 5,399 (99.5%) were TIER_1_API_SIGHTMAP* —
    the only adapter that stamps a ROW-level ``data_quality_flag``.
  * 1,675 rows were plan-level in shape (area == -1 AND no ``unit_name``) yet
    carried no flag, from tiers that name plan-ness themselves:
    404 TIER_1_API_RENTCAFE_SECURECAFE, 259 …_SHAPE_REJECTED_PLAN_LEVEL,
    145 TIER_1_DOM_GENERIC_PLAN_TEXT, 117 TIER_MERGED_CROSS_PAGE, …
  * Offline replay of the fixed predicate over the same run: 5,427 → 7,919
    flagged rows (+2,492), 0 rows un-flagged.
"""
from __future__ import annotations

import pytest

from ma_poc.scripts.runners.jugnu import _format_v2

_CSV = {"apartmentid": "9999"}


def _row(**kw):
    base = {
        "floor_plan_name": "A1",
        "bedrooms": "1",
        "bathrooms": "1",
        "availability_status": "AVAILABLE",
    }
    base.update(kw)
    return base


# ── the property-level marker (the half the row-only predicate could not see)
@pytest.mark.parametrize(
    "property_tier",
    [
        "TIER_1_API_RENTCAFE_NO_RESPONSE_PLAN_LEVEL",
        "TIER_1_API_RENTCAFE_SHAPE_REJECTED_PLAN_LEVEL",
        "TIER_1_DOM_ENTRATA_PP_SSR_PLAN_LEVEL",
        "TIER_1_API_ONESITE_NO_RESPONSE_PLAN_LEVEL",
        "SYNDICATION_ONLY_SQUARESPACE_PLAN_LEVEL",
        "ENCORESKYLINE_NO_PLAN_LINKS_PLAN_LEVEL",
        "TIER_3_PLAN_TEXT",
    ],
)
def test_property_plan_level_tier_flags_its_anchorless_rows(property_tier: str) -> None:
    """Every one of these tiers shipped unflagged rows in the 2026-07-27 run.

    The adapters record plan-ness on ``AdapterResult.tier_used``; the rows keep
    the plain adapter tier, so the row-only predicate never saw the marker.
    """
    result = {
        "extraction_tier_used": property_tier,
        "units": [_row(unit_number="", market_rent_low=1500)],
        "plan_summaries": [],
    }
    prop = _format_v2(result, _CSV)
    assert len(prop["units"]) == 1
    assert prop["units"][0]["is_floor_plan_level"] is True, property_tier


def test_property_verdict_quality_plan_level_flags_its_rows() -> None:
    """``_verdict_quality=SUCCESS_PLAN_LEVEL`` is the sibling convention the
    scraper writes at the same two emission sites (scraper.py :2152 / :2309)."""
    result = {
        "extraction_tier_used": "TIER_1_API_RENTCAFE_SECURECAFE",
        "_verdict_quality": "SUCCESS_PLAN_LEVEL",
        "units": [_row(unit_number="", market_rent_low=1500)],
        "plan_summaries": [],
    }
    prop = _format_v2(result, _CSV)
    assert prop["units"][0]["is_floor_plan_level"] is True


# ── the row-level marker ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("row_tier", "dqf"),
    [
        ("TIER_3_PLAN_TEXT", ""),
        ("TIER_1_DOM_GENERIC_PLAN_TEXT", ""),
        ("TIER_1_DOM_GENERIC_PLAN_TEXT_JSONLD_PRICERANGE", "PLAN_RANGE_ONLY"),
        ("TIER_1_DOM_GENERIC_PLAN_TEXT_LABELED_PRICE", "PLAN_RANGE_ONLY"),
        ("TIER_1_API_RENTCAFE", "PLAN_LEVEL_NO_UNIT_ANCHOR"),
    ],
)
def test_row_level_plan_marker_flags_the_row(row_tier: str, dqf: str) -> None:
    """A plan marker on the ROW alone is enough — the property need not be
    plan-level (mixed properties emit both kinds of row)."""
    result = {
        "extraction_tier_used": "TIER_1_API_RENTCAFE",
        "units": [
            _row(unit_number="", market_rent_low=1500,
                 extraction_tier=row_tier, data_quality_flag=dqf)
        ],
        "plan_summaries": [],
    }
    prop = _format_v2(result, _CSV)
    assert prop["units"][0]["is_floor_plan_level"] is True


# ── INVERSE ERROR: a genuine apartment must never be flagged ────────────────
def test_anchored_rows_in_a_plan_level_property_stay_unit_level() -> None:
    """385 rows in the 2026-07-27 run sit inside a plan-level-tier property yet
    carry a real apartment anchor. Flagging them would delete real units from
    any client filtering on the flag."""
    result = {
        "extraction_tier_used": "TIER_1_API_RENTCAFE_SHAPE_REJECTED_PLAN_LEVEL",
        "units": [
            _row(unit_number="", market_rent_low=1500),      # plan row
            _row(unit_number="412", market_rent_low=1600),   # real apartment
        ],
        "plan_summaries": [],
    }
    prop = _format_v2(result, _CSV)
    by_id = {u["unit_id"]: u for u in prop["units"]}
    assert by_id["412"]["is_floor_plan_level"] is False
    plan = next(u for u in prop["units"] if u["unit_id"] != "412")
    assert plan["is_floor_plan_level"] is True


def test_plan_text_tier_emitting_a_real_unit_stays_unit_level() -> None:
    """``generic_plan_text.py:903`` deliberately emits a UNIT-level row under
    ``TIER_1_DOM_GENERIC_PLAN_TEXT_UNIT_STREET`` ("20H Kensington Circle
    -$2600.00"). The tier contains PLAN_TEXT; the row is a real apartment."""
    result = {
        "extraction_tier_used": "TIER_1_DOM_GENERIC_PLAN_TEXT",
        "units": [
            _row(unit_number="20H", market_rent_low=2600,
                 extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT_UNIT_STREET")
        ],
        "plan_summaries": [],
    }
    prop = _format_v2(result, _CSV)
    assert prop["units"][0]["is_floor_plan_level"] is False


def test_ordinary_property_does_not_flag_anything() -> None:
    """No marker anywhere → the flag stays False, including for a row whose
    sqft simply is not published (area == -1 is NOT the predicate)."""
    result = {
        "extraction_tier_used": "TIER_1_API_SIGHTMAP",
        "units": [
            _row(unit_number="101", market_rent_low=1500, sqft="700"),
            _row(unit_number="102", market_rent_low=1600),  # no sqft → area -1
        ],
        "plan_summaries": [],
    }
    prop = _format_v2(result, _CSV)
    assert [u["is_floor_plan_level"] for u in prop["units"]] == [False, False]
    assert prop["units"][1]["area"] == -1


def test_provenance_plan_level_counter_agrees_with_the_shipped_column() -> None:
    """``_meta.provenance.data_quality.plan_level_units`` and the shipped
    column are computed from the same predicate + the same property marker, so
    they cannot disagree (they read 0 vs 4,024 in the 2026-07-25 canary)."""
    from ma_poc.scripts.runners.jugnu import _provenance_block

    result = {
        "extraction_tier_used": "TIER_1_API_RENTCAFE_SHAPE_REJECTED_PLAN_LEVEL",
        "units": [
            _row(unit_number="", market_rent_low=1500),
            _row(unit_number="", market_rent_low=1600),
            _row(unit_number="412", market_rent_low=1700),
        ],
        "plan_summaries": [],
    }
    prov = _provenance_block(dict(result), {}, None, "SUCCESS")
    prop = _format_v2(result, _CSV)
    shipped = sum(1 for u in prop["units"] if u["is_floor_plan_level"])
    assert prov["data_quality"]["plan_level_units"] == shipped == 2
