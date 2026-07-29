"""Area-absence taxonomy — ``area == -1`` is opaque; this labels WHY.

Task #56. The product decision is to tell the client "area not found" and say
which kind of not-found it is, but ONLY where we are confident. Saying an
operator publishes no square footage when we merely failed to extract one is
a false statement about somebody's business, and is worse than an opaque -1.

The measurements these tests encode (run-2026-07-27-full-0d54ca7, 104,964
rows, re-derived 2026-07-29):

  * 7,752 rows ship ``area == -1``.
  * 5,354 of them are plan-catalogue markers. 18 distinct SightMap
    deployments re-fetched live by plain static GET carry NO area-shaped key
    anywhere in ``data.floor_plans[]`` — square footage is a per-apartment
    attribute there, and a catalogue row has no apartment. That is
    NOT_APPLICABLE, not an extraction gap, even though the same property
    publishes area for its available units.
  * The tempting operator-level rules do not survive live verification. Of
    the 49 properties that published a rent and a real unit anchor on every
    row and an area on none, 31 of the 48 reachable ones publish a square
    footage on their own floor-plans page. NOT_PUBLISHED therefore may never
    be inferred from the shape of our own output.
"""

from __future__ import annotations

import pytest
from ma_poc.core.schema_v2 import (
    AREA_ABSENCE_NOT_APPLICABLE,
    AREA_ABSENCE_NOT_CAPTURED,
    AREA_ABSENCE_NOT_PUBLISHED,
    AREA_ABSENCE_UNKNOWN,
    AREA_SURFACE_NO_SQFT_KEY,
    _format_v2_unit,
    classify_area_absence,
    property_publishes_area,
)

# ── The plan-catalogue predicate, as a table ─────────────────────────────────
#
# The label turns on the ``PLAN_PRESENCE`` convention AND on the row having no
# real apartment anchor. Both halves matter, so both halves get must-match and
# must-NOT-match rows.
#
# (id, unit dict, expected NOT_APPLICABLE?)
_PLAN_PRESENCE_TABLE: list[tuple[str, dict, bool]] = [
    # ── must match ──────────────────────────────────────────────────────────
    ("vendor_prefixed_flag",
     {"data_quality_flag": "SIGHTMAP_PLAN_PRESENCE"}, True),
    ("bare_convention_token",
     {"data_quality_flag": "PLAN_PRESENCE"}, True),
    ("lowercase_flag",
     {"data_quality_flag": "sightmap_plan_presence"}, True),
    ("pipe_joined_with_other_flags",
     {"data_quality_flag": "CARRIED_FORWARD|PLAN_PRESENCE"}, True),
    ("hypothetical_second_vendor",
     {"data_quality_flag": "ACMEPORTAL_PLAN_PRESENCE"}, True),
    ("plan_presence_with_plan_name_only",
     {"data_quality_flag": "SIGHTMAP_PLAN_PRESENCE",
      "floor_plan_name": "B1HC", "bedrooms": "2"}, True),
    ("plan_presence_with_synthetic_id",
     {"data_quality_flag": "SIGHTMAP_PLAN_PRESENCE",
      "unit_number": ""}, True),
    # ── must NOT match ──────────────────────────────────────────────────────
    # A row that proves ONE REAL APARTMENT is never a catalogue marker,
    # whatever it is flagged. This is the branch that stops us telling a
    # client "not applicable" about an apartment that exists.
    ("plan_presence_but_has_unit_number",
     {"data_quality_flag": "SIGHTMAP_PLAN_PRESENCE",
      "unit_number": "204"}, False),
    ("plan_presence_but_has_native_source_id",
     {"data_quality_flag": "SIGHTMAP_PLAN_PRESENCE",
      "source_ids": {"sightmap_unit_id": "8891"}}, False),
    # Near-miss tokens: plan-LEVEL is not plan-PRESENCE. A plan-level row
    # usually means we failed to reach the units, which is our gap, not a
    # plan with nothing behind it.
    ("plan_level_no_unit_anchor",
     {"data_quality_flag": "PLAN_LEVEL_NO_UNIT_ANCHOR"}, False),
    ("plan_level_tier_suffix",
     {"extraction_tier": "TIER_1_API_ENTRATA_SHAPE_REJECTED_PLAN_LEVEL"}, False),
    ("plan_range_only",
     {"data_quality_flag": "PLAN_RANGE_ONLY"}, False),
    # Other absence flags that must not be mistaken for it.
    ("rent_not_published_flag",
     {"data_quality_flag": "RENT_NOT_PUBLISHED"}, False),
    ("presence_word_without_plan",
     {"data_quality_flag": "UNIT_PRESENCE"}, False),
    ("plan_word_without_presence",
     {"data_quality_flag": "PLAN_TEXT"}, False),
    ("no_flag_at_all", {}, False),
    ("empty_flag", {"data_quality_flag": ""}, False),
]


@pytest.mark.parametrize(
    "case_id,unit,expect_na",
    _PLAN_PRESENCE_TABLE,
    ids=[c[0] for c in _PLAN_PRESENCE_TABLE],
)
def test_plan_presence_predicate_table(case_id: str, unit: dict, expect_na: bool) -> None:
    label, _ = classify_area_absence(unit, formatted_area=-1)
    if expect_na:
        assert label == AREA_ABSENCE_NOT_APPLICABLE, (
            f"{case_id}: expected NOT_APPLICABLE, got {label}"
        )
    else:
        assert label != AREA_ABSENCE_NOT_APPLICABLE, (
            f"{case_id}: must NOT be NOT_APPLICABLE, got {label}"
        )


# ── Full-label table ─────────────────────────────────────────────────────────
# (id, unit, formatted_area, supplied_value, property_has_area,
#  expected_label, expected_evidence)
_LABEL_TABLE: list[tuple[str, dict, int, object, bool, str | None, str | None]] = [
    ("area_present_no_label",
     {}, 750, "750", False, None, None),
    ("area_present_even_with_plan_flag",
     {"data_quality_flag": "SIGHTMAP_PLAN_PRESENCE"}, 812, "812", True, None, None),
    ("catalogue_marker",
     {"data_quality_flag": "SIGHTMAP_PLAN_PRESENCE"}, -1, "", False,
     AREA_ABSENCE_NOT_APPLICABLE, "plan_presence_no_apartment"),
    ("catalogue_marker_beats_sibling_evidence",
     {"data_quality_flag": "SIGHTMAP_PLAN_PRESENCE"}, -1, "", True,
     AREA_ABSENCE_NOT_APPLICABLE, "plan_presence_no_apartment"),
    ("sanity_clamp_nulled_it",
     {"_sanity_dropped": ["area"]}, -1, None, False,
     AREA_ABSENCE_NOT_CAPTURED, "value_dropped_by_sanity_bounds"),
    ("sanity_beds_cross_check_nulled_it",
     {"_sanity_dropped": ["rent_low", "area_implausible_for_beds"]}, -1, None, False,
     AREA_ABSENCE_NOT_CAPTURED, "value_dropped_by_sanity_bounds"),
    ("sanity_dropped_something_else",
     {"_sanity_dropped": ["rent_low"]}, -1, None, False,
     AREA_ABSENCE_UNKNOWN, "no_evidence"),
    ("source_sent_zero_we_rejected_it",
     {}, -1, "0", False,
     AREA_ABSENCE_NOT_CAPTURED, "value_rejected_by_area_bounds"),
    ("source_sent_prose_we_rejected_it",
     {}, -1, "Call for details.", False,
     AREA_ABSENCE_NOT_CAPTURED, "value_rejected_by_area_bounds"),
    ("source_sent_a_range_we_rejected_it",
     {}, -1, "0-500", False,
     AREA_ABSENCE_NOT_CAPTURED, "value_rejected_by_area_bounds"),
    ("whitespace_is_not_a_supplied_value",
     {}, -1, "   ", False,
     AREA_ABSENCE_UNKNOWN, "no_evidence"),
    ("sibling_row_had_one",
     {}, -1, None, True,
     AREA_ABSENCE_NOT_CAPTURED, "sibling_row_published_area"),
    ("surface_evidence_gives_not_published",
     {AREA_SURFACE_NO_SQFT_KEY: True}, -1, None, False,
     AREA_ABSENCE_NOT_PUBLISHED, "surface_has_no_sqft_field"),
    ("surface_evidence_loses_to_a_contradicting_sibling",
     {AREA_SURFACE_NO_SQFT_KEY: True}, -1, None, True,
     AREA_ABSENCE_NOT_CAPTURED, "sibling_row_published_area"),
    ("surface_evidence_loses_to_our_own_clamp",
     {AREA_SURFACE_NO_SQFT_KEY: True, "_sanity_dropped": ["area"]}, -1, None, False,
     AREA_ABSENCE_NOT_CAPTURED, "value_dropped_by_sanity_bounds"),
    ("truthy_but_not_true_is_not_evidence",
     {AREA_SURFACE_NO_SQFT_KEY: "yes"}, -1, None, False,
     AREA_ABSENCE_UNKNOWN, "no_evidence"),
    ("no_evidence_at_all",
     {}, -1, None, False,
     AREA_ABSENCE_UNKNOWN, "no_evidence"),
]


@pytest.mark.parametrize(
    "case_id,unit,area,supplied,has_area,label,evidence",
    _LABEL_TABLE,
    ids=[c[0] for c in _LABEL_TABLE],
)
def test_label_table(
    case_id: str,
    unit: dict,
    area: int,
    supplied: object,
    has_area: bool,
    label: str | None,
    evidence: str | None,
) -> None:
    got = classify_area_absence(
        unit,
        formatted_area=area,
        supplied_value=supplied,
        property_publishes_area=has_area,
    )
    assert got == (label, evidence), f"{case_id}: got {got}"


def test_not_published_is_never_inferred_from_our_own_output() -> None:
    """The acceptance criterion, as an executable guard.

    No combination of OUR OWN output shape — a property that published no
    area, plan-level-ness, rents, anchors, tiers — may produce
    NOT_PUBLISHED. Only the adapter's positive surface evidence may. Live
    verification killed every output-shaped rule: 31 of 48 reachable
    "publishes area nowhere" properties do publish a square footage.
    """
    output_shaped = [
        {},
        {"unit_number": "204", "market_rent_low": 1450},
        {"extraction_tier": "TIER_1_API_RENTCAFE_SECURECAFE"},
        {"extraction_tier": "TIER_1_DOM_APPFOLIO_SSR", "unit_number": "12B"},
        {"data_quality_flag": "PLAN_LEVEL_NO_UNIT_ANCHOR"},
        {"data_quality_flag": "RENT_NOT_PUBLISHED", "unit_number": "3"},
        {"_sanity_dropped": []},
        {"is_floor_plan_level": True},
    ]
    for unit in output_shaped:
        for has_area in (False, True):
            for supplied in (None, "", "0"):
                label, _ = classify_area_absence(
                    unit,
                    formatted_area=-1,
                    supplied_value=supplied,
                    property_publishes_area=has_area,
                )
                assert label != AREA_ABSENCE_NOT_PUBLISHED, (
                    f"{unit!r} (has_area={has_area}, supplied={supplied!r}) "
                    f"produced NOT_PUBLISHED from output shape alone"
                )


def test_numeric_minus_one_contract_is_unchanged() -> None:
    """Adding the label must not move the numeric wire contract."""
    from datetime import datetime

    ts = datetime(2026, 7, 27, 12, 0, 0)
    absent = _format_v2_unit({"unit_number": "1", "sqft": ""}, ts, "P1")
    present = _format_v2_unit({"unit_number": "2", "sqft": "742"}, ts, "P1")
    assert absent["area"] == -1
    assert present["area"] == 742
    assert present["area_absence"] is None
    assert present["area_absence_evidence"] is None
    assert absent["area_absence"] == AREA_ABSENCE_UNKNOWN


def test_property_publishes_area_reads_the_formatter_aliases() -> None:
    assert property_publishes_area([{"sqft": "742"}]) is True
    assert property_publishes_area([{"_sqft": 742}]) is True
    assert property_publishes_area([{"squareFeet": "1050"}]) is True
    # 2026-07-29: was pinned ``False`` because ``_format_area`` could not
    # parse a thousands separator — ``"1,050"`` collapsed to the ABSENT
    # sentinel and the row was labelled NOT_CAPTURED
    # (value_rejected_by_area_bounds). That was a parse bug, not evidence
    # about the operator: a source that ships "1,050" HAS published a size.
    # The formatter now strips grouping commas, so this is real evidence.
    assert property_publishes_area([{"sqft": "1,050"}]) is True
    # Only a GENUINE thousands separator counts. "1,2" is malformed, not
    # 12 — it must not become evidence of a published size by way of a
    # blanket comma strip. See test_format_area_thousands_separator.
    assert property_publishes_area([{"sqft": "1,2"}]) is False
    # Out-of-bounds values are not evidence the operator published a size.
    assert property_publishes_area([{"sqft": "0"}]) is False
    assert property_publishes_area([{"sqft": "9"}]) is False
    assert property_publishes_area([{"sqft": "99999"}]) is False
    # Defensive shapes never raise.
    assert property_publishes_area([]) is False
    assert property_publishes_area(None) is False
    assert property_publishes_area(["not-a-dict", None]) is False


def test_formatter_labels_a_sibling_gap_as_ours_not_theirs() -> None:
    """End-to-end through the property formatter: one row with an area and
    one without must label the second NOT_CAPTURED, never NOT_PUBLISHED."""
    from ma_poc.core.schema_v2 import build_v2_property

    target_units = [
        {"unit_number": "101", "sqft": "742", "market_rent_low": 1400},
        {"unit_number": "102", "market_rent_low": 1450},
    ]
    prop = build_v2_property(
        {"id": "7", "url": "https://x.test"},
        None,
        {"units": target_units},
        target_units,
    )
    units = {u["unit_id"]: u for u in prop["units"]}
    assert units["101"]["area"] == 742
    assert units["101"]["area_absence"] is None
    assert units["102"]["area"] == -1
    assert units["102"]["area_absence"] == AREA_ABSENCE_NOT_CAPTURED
    assert units["102"]["area_absence_evidence"] == "sibling_row_published_area"


def test_production_runner_fork_emits_the_same_labels() -> None:
    """``scripts/runners/jugnu`` carries a FORK of the unit formatter and it
    is the one production runs — task #36's ``is_floor_plan_level`` flag was
    lost for 4,024 rows by being added to the other copy only. Pin parity."""
    from datetime import datetime

    from ma_poc.scripts.runners.jugnu import _format_v2_unit as jugnu_format

    ts = datetime(2026, 7, 27, 12, 0, 0)
    cases = [
        ({"unit_number": "1", "sqft": "742"}, False, None, None),
        ({"data_quality_flag": "SIGHTMAP_PLAN_PRESENCE"}, False,
         AREA_ABSENCE_NOT_APPLICABLE, "plan_presence_no_apartment"),
        ({"unit_number": "2", "sqft": "0"}, False,
         AREA_ABSENCE_NOT_CAPTURED, "value_rejected_by_area_bounds"),
        ({"unit_number": "3", "_sanity_dropped": ["area"]}, False,
         AREA_ABSENCE_NOT_CAPTURED, "value_dropped_by_sanity_bounds"),
        ({"unit_number": "4"}, True,
         AREA_ABSENCE_NOT_CAPTURED, "sibling_row_published_area"),
        ({"unit_number": "5"}, False, AREA_ABSENCE_UNKNOWN, "no_evidence"),
        ({"unit_number": "6", AREA_SURFACE_NO_SQFT_KEY: True}, False,
         AREA_ABSENCE_NOT_PUBLISHED, "surface_has_no_sqft_field"),
    ]
    for unit, has_area, label, evidence in cases:
        core = _format_v2_unit(dict(unit), ts, "P1", property_has_area=has_area)
        fork = jugnu_format(dict(unit), ts, "P1", property_has_area=has_area)
        assert core["area_absence"] == label, f"core drifted on {unit!r}"
        assert fork["area_absence"] == label, f"jugnu fork drifted on {unit!r}"
        assert core["area_absence_evidence"] == evidence
        assert fork["area_absence_evidence"] == evidence
        assert core["area"] == fork["area"]


# ── The thousands separator, as a table ──────────────────────────────────────
#
# ``_format_area`` coerced with ``int(float(str(val)))``, which cannot parse a
# grouping comma: every source publishing ``"1,050 sq ft"`` collapsed to the
# ABSENT sentinel -1 and became indistinguishable from a genuine absence. The
# jugnu copy was widened for this on 2026-05-19 and the core copy was not, so
# the two v2 unit formatters disagreed about what a square footage is for two
# months — the same one-label-two-code-paths defect this task hit with
# ``is_floor_plan_level``. The core copy now carries the parse and the jugnu
# copy delegates to it; ``test_both_area_parsers_agree`` holds that shut.
#
# The must-NOT-match rows are the point of the table. The obvious fix — strip
# EVERY comma — reads the malformed ``"1,2"`` as 12 and the European decimal
# ``"950,5"`` as 9505 sqft. Only a comma that is preceded by a digit and
# followed by exactly three more is a thousands separator; everything else
# falls back to the first whole numeric token and meets the [150, 10000]
# bound, which is unchanged.
#
# (raw, expected area, why)
_AREA_PARSE_CASES: list[tuple[object, int, str]] = [
    # ── must match: a real thousands separator, recovered ──
    ("1,050", 1050, "the reported bug: grouped sqft was reading as ABSENT"),
    ("1,250", 1250, "same, second magnitude"),
    ("1,050 sq ft", 1050, "grouped + unit suffix"),
    ("1,050 sqft", 1050, "grouped + unit suffix, no space in token"),
    ("1,050.5", 1050, "grouped + decimal fraction"),
    ("  1,050  ", 1050, "grouped + surrounding whitespace"),
    ("1,050-1,200", 1050, "range → LOW bound, as the jugnu copy has always done"),

    # ── must NOT match: not a thousands separator ──
    # A blanket comma strip reads this as 12 — an in-bounds-looking number
    # built out of a comma that never meant anything.
    ("1,2", -1, "malformed: comma not followed by three digits"),
    # European decimal notation. A blanket strip reads 9505 sqft — in bounds,
    # plausible, and wrong by a factor of ten. The narrow rule leaves the
    # comma alone and the first whole token (950) is the correct answer.
    ("950,5", 950, "european decimal: must not concatenate to 9505"),
    ("1,0500", -1, "four digits after the comma is not a grouping"),
    ("12,345", -1, "genuine grouping, but 12345 sqft is out of bounds"),

    # ── must NOT match: nothing numeric to find ──
    ("Call for details.", -1, "operator-gated text carries no size"),
    ("Studio", -1, "plan name, not a size"),
    ("", -1, "empty string"),
    ("   ", -1, "whitespace only"),
    (None, -1, "absent"),
    (-1, -1, "already the ABSENT sentinel"),
    ("-1", -1, "the sentinel as a string"),

    # ── regression: the bound and the clean forms are UNCHANGED ──
    ("1050", 1050, "clean string int"),
    ("1050.0", 1050, "clean string float"),
    (1050, 1050, "clean int"),
    (1050.0, 1050, "clean float"),
    ("150", 150, "lower bound is inclusive"),
    ("10000", 10000, "upper bound is inclusive"),
    ("149", -1, "just below the bound"),
    ("10001", -1, "just above the bound"),
    ("070", -1, "truncated garbage still rejected"),
    ("9", -1, "bed count still rejected"),
    ("100", -1, "floor number still rejected"),
    ("0", -1, "zero is not a size"),
]


@pytest.mark.parametrize(
    ("raw", "expected", "why"),
    [pytest.param(r, e, w, id=f"{r!r}->{e}") for r, e, w in _AREA_PARSE_CASES],
)
def test_format_area_thousands_separator(
    raw: object, expected: int, why: str
) -> None:
    """A grouped square footage parses; a comma that isn't a grouping doesn't."""
    from ma_poc.core.schema_v2 import _format_area

    assert _format_area(raw) == expected, why


@pytest.mark.parametrize(
    ("raw", "expected", "why"),
    [pytest.param(r, e, w, id=f"{r!r}->{e}") for r, e, w in _AREA_PARSE_CASES],
)
def test_both_area_parsers_agree(raw: object, expected: int, why: str) -> None:
    """The jugnu fork must not drift from core again.

    It delegates today, so this is cheap; it fails loudly the moment somebody
    re-forks the body — which is exactly how the two copies came to disagree
    about the thousands separator for two months.
    """
    from ma_poc.core.schema_v2 import _format_area as core_area
    from ma_poc.scripts.runners.jugnu import _format_area as fork_area

    assert core_area(raw) == fork_area(raw) == expected, why


def test_grouped_area_reaches_the_v2_wire_field() -> None:
    """End-to-end: the parse fix must actually move ``area`` on the record,
    and must clear the absence label that the -1 was carrying."""
    from datetime import datetime

    ts = datetime(2026, 7, 27, 12, 0, 0)
    for fmt in (_format_v2_unit, _jugnu_format_v2_unit()):
        row = fmt({"unit_number": "1", "sqft": "1,050"}, ts, "P1")
        assert row["area"] == 1050
        assert row["area_absence"] is None
        assert row["area_absence_evidence"] is None


def _jugnu_format_v2_unit():  # type: ignore[no-untyped-def]
    from ma_poc.scripts.runners.jugnu import _format_v2_unit as f

    return f
