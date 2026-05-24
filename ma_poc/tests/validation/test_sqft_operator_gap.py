"""Tests for the operator-data-gap sqft flag (2026-05-23).

Background — the honest-provenance fix:
  ~38 SecureCafe-PLAN_LEVEL properties have a successful unit-level
  drill (rent + unit_number + plan_name) but no extractable sqft. The
  operator simply doesn't publish sqft anywhere reachable (verified
  by exhausting 3 enrichment paths: plan-name regex, WP-cards merge,
  apts247 API). Today's pipeline treats this as a quality failure
  (no_area retry → fail → _PLAN_LEVEL suffix), conflating "parser
  bug" with "operator data gap".

  Fix:
    - make_unit_dict accepts data_gaps + data_quality_flag kwargs.
    - schema_gate._has_area treats documented sqft gaps as
      area-present so the no_area retry doesn't fire.
    - Adapters that finish their enrichment chain stamp the flag.

  Effect: byelon-style properties (5), the 19 truly-empty SC singletons,
  and any future adapter that follows the same evidence chain ship as
  SUCCESS with transparent provenance, not SUCCESS_PLAN_LEVEL.
"""
from __future__ import annotations

from ma_poc.pms.adapters._parsing import make_unit_dict
from ma_poc.validation.schema_gate import (
    _has_area,
    _has_rent,
    _rent_gap_documented,
    _sqft_gap_documented,
    property_has_area_signal,
    property_has_rent_signal,
)

# ─── make_unit_dict accepts the new kwargs ───────────────────────────


def test_make_unit_dict_defaults_data_gaps_empty() -> None:
    """Backward-compat: existing callers that don't pass data_gaps /
    data_quality_flag get safe defaults — no behavior change for any
    pre-2026-05-23 adapter."""
    u = make_unit_dict(unit_number="101", rent_low=1500)
    assert u["data_gaps"] == []
    assert u["data_quality_flag"] == ""


def test_make_unit_dict_accepts_sqft_gap_flag() -> None:
    u = make_unit_dict(
        unit_number="101", rent_low=1500,
        data_gaps=["sqft"], data_quality_flag="SQFT_NOT_PUBLISHED",
    )
    assert u["data_gaps"] == ["sqft"]
    assert u["data_quality_flag"] == "SQFT_NOT_PUBLISHED"


def test_make_unit_dict_data_gaps_is_owned_copy() -> None:
    """Caller-supplied list must be defensively copied — mutating the
    returned unit's list must not affect the caller's."""
    src = ["sqft"]
    u = make_unit_dict(unit_number="101", data_gaps=src)
    u["data_gaps"].append("something_else")
    assert src == ["sqft"]


# ─── _sqft_gap_documented predicate ──────────────────────────────────


def test_sqft_gap_documented_via_data_gaps_list() -> None:
    assert _sqft_gap_documented({"data_gaps": ["sqft"]}) is True
    assert _sqft_gap_documented({"data_gaps": ["sqft", "bedrooms"]}) is True
    assert _sqft_gap_documented({"data_gaps": ["bedrooms"]}) is False
    assert _sqft_gap_documented({"data_gaps": []}) is False


def test_sqft_gap_documented_via_quality_flag() -> None:
    assert _sqft_gap_documented({"data_quality_flag": "SQFT_NOT_PUBLISHED"}) is True
    # Case-insensitive — defensive against caller casing variants.
    assert _sqft_gap_documented({"data_quality_flag": "sqft_not_published"}) is True


def test_sqft_gap_documented_absent_keys() -> None:
    """The dominant case: pre-2026-05-23 unit dicts that have neither
    field must report False (no gap documented)."""
    assert _sqft_gap_documented({}) is False
    assert _sqft_gap_documented({"unit_number": "101"}) is False
    assert _sqft_gap_documented({"data_gaps": None}) is False
    assert _sqft_gap_documented({"data_quality_flag": ""}) is False


# ─── _has_area honors the flag ───────────────────────────────────────


def test_has_area_returns_true_for_real_sqft_value() -> None:
    """Sanity: the numeric path still wins — no regression."""
    assert _has_area({"sqft": "950"}) is True
    assert _has_area({"sqft": 950}) is True
    assert _has_area({"area": "1100"}) is True


def test_has_area_returns_false_for_empty_unit() -> None:
    assert _has_area({"sqft": ""}) is False
    assert _has_area({"sqft": "0"}) is False  # zero is "not populated"
    assert _has_area({}) is False


def test_has_area_returns_true_when_sqft_gap_documented() -> None:
    """The new path: a unit with empty sqft + documented gap still
    reports area-present so the no_area retry doesn't fire."""
    flagged = {
        "sqft": "", "unit_number": "101", "market_rent_low": 1500,
        "data_gaps": ["sqft"],
        "data_quality_flag": "SQFT_NOT_PUBLISHED",
    }
    assert _has_area(flagged) is True


def test_has_area_unflagged_unit_still_fails() -> None:
    """A unit with empty sqft and NO documented gap continues to fail
    (parser miss is still a parser miss — the flag must be a deliberate
    adapter signal, not a default)."""
    unflagged = {"sqft": "", "unit_number": "101", "market_rent_low": 1500}
    assert _has_area(unflagged) is False


# ─── property_has_area_signal at the cohort level ────────────────────


def test_property_has_area_signal_passes_with_all_flagged() -> None:
    """5 byelon-style units, all carrying rent + unit_number, all with
    documented sqft gaps → property has area signal at 100%, no_area
    retry will not fire, verdict ships as SUCCESS."""
    byelon_units = [
        make_unit_dict(
            unit_number=str(100 + i), rent_low=1200 + 50 * i,
            data_gaps=["sqft"], data_quality_flag="SQFT_NOT_PUBLISHED",
        )
        for i in range(5)
    ]
    assert property_has_area_signal(byelon_units) is True


def test_property_has_area_signal_mixed_flagged_and_real() -> None:
    """Realistic case: SC drill produced 5 units, the 3 enrichment
    paths filled sqft on 2, the rest got the flag. 5/5 satisfy
    _has_area → area signal True."""
    units = [
        # Two units got real sqft from an enrichment path.
        make_unit_dict(unit_number="101", rent_low=1500, sqft="700"),
        make_unit_dict(unit_number="102", rent_low=1525, sqft="700"),
        # Three units got the not-published flag.
        make_unit_dict(
            unit_number="201", rent_low=1800,
            data_gaps=["sqft"], data_quality_flag="SQFT_NOT_PUBLISHED",
        ),
        make_unit_dict(
            unit_number="202", rent_low=1850,
            data_gaps=["sqft"], data_quality_flag="SQFT_NOT_PUBLISHED",
        ),
        make_unit_dict(
            unit_number="203", rent_low=1900,
            data_gaps=["sqft"], data_quality_flag="SQFT_NOT_PUBLISHED",
        ),
    ]
    assert property_has_area_signal(units) is True


def test_property_has_area_signal_unflagged_units_still_fail() -> None:
    """Belt-and-braces: a unit lacking sqft AND lacking the flag must
    still pull the signal below threshold — flagging is opt-in, not
    a free pass for parser bugs."""
    units = [
        make_unit_dict(unit_number=str(i), rent_low=1500)  # no sqft, no flag
        for i in range(5)
    ]
    assert property_has_area_signal(units) is False


# ─── _rent_gap_documented (SightMap "operator hides rent") ───────────


def test_rent_gap_documented_via_data_gaps_list() -> None:
    """Parallel to _sqft_gap_documented — rent gap in data_gaps list
    is the canonical signal."""
    assert _rent_gap_documented({"data_gaps": ["rent"]}) is True
    assert _rent_gap_documented({"data_gaps": ["rent", "sqft"]}) is True
    assert _rent_gap_documented({"data_gaps": ["sqft"]}) is False
    assert _rent_gap_documented({"data_gaps": []}) is False


def test_rent_gap_documented_via_quality_flag() -> None:
    assert _rent_gap_documented({"data_quality_flag": "RENT_NOT_PUBLISHED"}) is True
    assert _rent_gap_documented({"data_quality_flag": "rent_not_published"}) is True
    # The sqft flag must NOT also report a rent gap.
    assert _rent_gap_documented({"data_quality_flag": "SQFT_NOT_PUBLISHED"}) is False


def test_rent_gap_documented_absent_keys() -> None:
    assert _rent_gap_documented({}) is False
    assert _rent_gap_documented({"unit_number": "101"}) is False


# ─── _has_rent honors the flag ───────────────────────────────────────


def test_has_rent_returns_true_for_real_rent_value() -> None:
    assert _has_rent({"market_rent_low": 1500}) is True
    assert _has_rent({"asking_rent": 2000}) is True
    assert _has_rent({"rent_low": 1750, "rent_high": 2000}) is True


def test_has_rent_returns_false_for_empty_unit() -> None:
    assert _has_rent({}) is False
    assert _has_rent({"market_rent_low": None}) is False
    assert _has_rent({"asking_rent": 0}) is False


def test_has_rent_returns_true_when_rent_gap_documented() -> None:
    """The new path: a unit with no rent + documented gap still
    reports rent-present so the no_rent retry doesn't fire."""
    flagged = {
        "unit_number": "101", "market_rent_low": None,
        "area": 950,
        "data_gaps": ["rent"],
        "data_quality_flag": "RENT_NOT_PUBLISHED",
    }
    assert _has_rent(flagged) is True


def test_has_rent_unflagged_unit_still_fails() -> None:
    """A unit with empty rent and NO documented gap continues to fail
    (parser miss must remain detectable — flag is a deliberate signal,
    not a default)."""
    unflagged = {"unit_number": "101", "area": 950}
    assert _has_rent(unflagged) is False


def test_property_has_rent_signal_passes_with_all_flagged() -> None:
    """SightMap-style cohort: 5 units, all with area, all with rent
    gap documented. property_has_rent_signal must return True so
    the no_rent retry doesn't fire."""
    flagged_units = [
        make_unit_dict(
            unit_number=str(100 + i), sqft="850",
            data_gaps=["rent"], data_quality_flag="RENT_NOT_PUBLISHED",
        )
        for i in range(5)
    ]
    assert property_has_rent_signal(flagged_units) is True


def test_property_has_rent_signal_unflagged_units_still_fail() -> None:
    """Mirror of the sqft test: opt-in only."""
    units = [
        make_unit_dict(unit_number=str(i), sqft="850")  # no rent, no flag
        for i in range(5)
    ]
    assert property_has_rent_signal(units) is False
