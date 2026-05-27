"""Regression tests for availability-date normalization.

Root cause (2026-05-19): the production canary runner ``jugnu.py`` had a
DUPLICATE, narrower ``_format_date_str`` that only accepted ISO and
4-digit-year ``m/d/Y``. The capture-first widening shipped in 15b7aab
only touched ``schema_v2._format_date``, so adapter-emitted forms like
``"Available 7/10/26"`` / ``"Available Now"`` / 2-digit-year / no-year
month-name were silently dropped fleet-wide. These tests pin:

  1. ``schema_v2._format_date`` handles every form the PMS adapters emit.
  2. ``jugnu._format_date_str`` is a strict delegate (never diverges).
  3. ISO and 4-digit ``m/d/Y`` behavior is unchanged (additive only).
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ma_poc.core.schema_v2 import _format_date
from ma_poc.pms.adapters._html_extract import extract_available_date_from_card
from ma_poc.pms.adapters.spherexx import parse_razz_models_dom
from ma_poc.scripts.runners.jugnu import _format_date_str

_RAZZ_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "razz_models_sample.html"
)

_CUR = datetime.now(UTC).year
_TODAY = datetime.now(UTC).strftime("%Y-%m-%d")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # AppFolio /listings SSR ("AVAILABLE m/d/yy" with label + 2-digit yr)
        ("Available 7/10/26", "2026-07-10"),
        ("Available 8/21/26", "2026-08-21"),
        ("7/10/26", "2026-07-10"),
        ("5/31/26", "2026-05-31"),
        # AppFolio / RentCafe / OneSite available-now → run date
        ("Available Now", _TODAY),
        ("Available", _TODAY),
        ("Now", _TODAY),
        ("today", _TODAY),
        # Razz/Spherexx /models — no-year month-name → current run year
        ("May 19", f"{_CUR}-05-19"),
        ("Jun. 7", f"{_CUR}-06-07"),
        ("Jul. 18", f"{_CUR}-07-18"),
        ("July 5", f"{_CUR}-07-05"),
        ("Jun 7", f"{_CUR}-06-07"),
        # ── Regression: pre-existing forms MUST be unchanged ──
        ("2026-07-10", "2026-07-10"),
        ("2026-07-10T08:00:00Z", "2026-07-10"),
        ("03/15/2026", "2026-03-15"),
        ("12-25-2026", "2026-12-25"),
        ("Jun 7, 2026", "2026-06-07"),
        # Unparseable → None (no crash, no garbage)
        (None, None),
        ("", None),
        ("call for availability", None),
        ("garbage", None),
        # 2026-05-26 (canary 87b837b QC): YYYYMMDD packed numeric
        # — RentManager / older RealPage XMLs use this. 268 cases.
        ("20260601", "2026-06-01"),
        ("20261231", "2026-12-31"),
        ("20260101", "2026-01-01"),
        # 2026-05-26 (canary 87b837b QC): negative-status tokens
        # must return None — they indicate UNAVAILABLE units, NOT
        # "available now". 14 cases. Pre-fix the availability-prefix
        # strip turned these into bare "Not " / "" which then matched
        # the AVAILABLE_NOW fallback and incorrectly returned today.
        ("Not Available", None),
        ("Not Avail.", None),
        ("Not Avail", None),
        ("Unavailable", None),
        ("UNAVAILABLE", None),
        ("Leased", None),
        ("Occupied", None),
        ("Rented", None),
        ("Off Market", None),
        ("off-market", None),
        ("No Availability", None),
        # 2026-05-26 (canary 87b837b QC): ±5yr sanity bound. Operator-
        # emitted decade-old dates ("2009-07-08") and far-future dates
        # past the acceptance window are clearly garbage; the parser
        # must NOT propagate them. 32+ cases observed in 87b837b output.
        ("2009-07-08", None),     # 17yr stale — junk
        ("2019-04-21", None),     # 7yr stale — junk
        ("1999-12-31", None),     # 27yr stale — junk
        ("2050-01-01", None),     # 24yr future — junk
        ("2040-06-15", None),     # 14yr future — junk
        # In-window dates MUST still pass — these are the legit
        # "available since N months ago" cases the bound preserves.
        ("2025-09-14", "2025-09-14"),  # ~8mo stale — legit
        ("2024-12-01", "2024-12-01"),  # ~17mo stale — legit
    ],
)
def test_format_date_handles_all_adapter_forms(raw: object, expected: str | None) -> None:
    assert _format_date(raw) == expected


def test_sanity_bound_rejects_far_past_iso_dates() -> None:
    """The ±5yr bound applies to ISO dates too — the early-return ISO
    path must NOT bypass the sanity check (pre-fix, "2009-07-08" went
    through directly because it matched the ISO regex at line ~668)."""
    assert _format_date("2009-07-08") is None
    assert _format_date("1995-01-01") is None
    assert _format_date("2050-01-01") is None


def test_sanity_bound_keeps_borderline_dates() -> None:
    """Dates within ±5yr of today must still pass. Don't over-correct."""
    from datetime import UTC, datetime
    today = datetime.now(UTC).date()
    yr = today.year
    # ~3 years stale and ~3 years future — both well within bound
    assert _format_date(f"{yr - 3}-01-15") == f"{yr - 3}-01-15"
    assert _format_date(f"{yr + 3}-01-15") == f"{yr + 3}-01-15"


@pytest.mark.parametrize(
    "raw",
    [
        "Available 7/10/26",
        "Available Now",
        "7/10/26",
        "May 19",
        "Jun. 7",
        "2026-07-10",
        "03/15/2026",
        None,
        "",
        "garbage",
    ],
)
def test_jugnu_format_date_str_is_strict_delegate(raw: object) -> None:
    """jugnu._format_date_str must never diverge from schema_v2._format_date.

    Guards against the duplicate-parser regression returning.
    """
    assert _format_date_str(raw) == _format_date(raw)


def test_iso_and_four_digit_unchanged_additive_guarantee() -> None:
    """The widening is additive: canonical inputs are byte-identical."""
    for v in ("2026-01-02", "2026-12-31", "01/02/2026", "2026/01/02"):
        assert _format_date(v) == _format_date_str(v)
        assert _format_date(v) is not None


# ── Tier-3 DOM card extractor: label-prefixed dates must not be dropped ──
# Regression: dateutil rejects "Available <date>"/"Available Now"; the
# canonical-parser fallback recovers them. dateutil-parseable inputs and
# the past-date guard are unchanged (additive).
@pytest.mark.parametrize(
    ("card", "expected"),
    [
        ('<div class="unit-available">Available 7/10/26</div>', "2026-07-10"),
        ('<div class="availability">Available May 19</div>', f"{_CUR}-05-19"),
        ('<span class="avail-date">Available Now</span>', _TODAY),
        # Regression: dateutil path unchanged
        ('<time datetime="2026-08-01">Aug 1</time>', "2026-08-01"),
        ('<div class="available">7/10/26</div>', "2026-07-10"),
    ],
)
def test_dom_card_label_prefixed_dates_recovered(card: str, expected: str) -> None:
    assert extract_available_date_from_card(card) == expected


# ── Tier-1.5 Razz/myrazz /models DOM parser ──
# Anchors on the stable `wrap-model-item model-list` container + label
# text (the date leaf is an unclassed <div>). Raw "May 19"/"Now" is
# emitted; schema_v2._format_date normalizes it downstream.
def test_razz_models_parser_extracts_units_and_raw_availability() -> None:
    html = _RAZZ_FIXTURE.read_text(encoding="utf-8")
    units = parse_razz_models_dom(html, "https://example-razz.com/")
    by_no = {u["unit_number"]: u for u in units}
    assert set(by_no) == {"2021", "721", "307", "S04"}

    assert by_no["2021"]["bedrooms"] == "1"
    assert by_no["2021"]["bathrooms"] == "1"
    assert by_no["2021"]["availability_date"] == "May 19"
    assert by_no["721"]["availability_date"] == "Jul. 5"
    assert by_no["307"]["bedrooms"] == "2"
    assert by_no["307"]["availability_date"] == "Now"
    assert by_no["S04"]["bedrooms"] == "0"  # Studio → 0 beds
    assert by_no["S04"]["availability_date"] == "6/28/26"
    # rent / sqft captured from labeled rows
    assert "830" in by_no["2021"].get("rent_range", "")
    assert by_no["2021"].get("sqft") == "650"


def test_razz_raw_dates_normalize_via_schema_v2() -> None:
    """End-to-end: the raw strings the parser emits must resolve to real
    dates through the canonical formatter (the whole point of the chain).
    """
    units = parse_razz_models_dom(
        _RAZZ_FIXTURE.read_text(encoding="utf-8"), "https://x.com/"
    )
    by_no = {u["unit_number"]: u for u in units}
    assert _format_date(by_no["2021"]["availability_date"]) == f"{_CUR}-05-19"
    assert _format_date(by_no["721"]["availability_date"]) == f"{_CUR}-07-05"
    assert _format_date(by_no["307"]["availability_date"]) == _TODAY  # Now
    assert _format_date(by_no["S04"]["availability_date"]) == "2026-06-28"


def test_razz_parser_noop_on_non_razz_html() -> None:
    """Must not fire on non-Razz pages (no regression to other adapters)."""
    assert parse_razz_models_dom("<html><body><div>no units</div></body></html>", "u") == []
    assert parse_razz_models_dom("", "u") == []


# ── jugnu._format_rent: noisy single-value rents must not be discarded ──
# Same defect class as the date bug: bare float() dropped valid forms the
# adapters emit. Fallback delegates to parse_rent_range. Additive: clean
# numerics unchanged; non-rent text still None.
from ma_poc.scripts.runners.jugnu import _format_rent  # noqa: E402


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # recovered (were silently None before the fix)
        ("$1450/mo", 1450.0),
        ("$1,450 per month", 1450.0),
        ("From $1,450", 1450.0),
        ("Starting at $1450", 1450.0),
        ("$1,450+", 1450.0),
        ("$1,200 - $1,400", 1200.0),  # low bound (rent_low-correct)
        ("1200-1400", 1200.0),
        # regression: clean forms unchanged
        ("1450", 1450.0),
        ("$1,450", 1450.0),
        ("$1,450.00", 1450.0),
        (1450, 1450.0),
        (1450.0, 1450.0),
        # correctly rejected (no real number / sentinel)
        ("Call for pricing", None),
        ("garbage", None),
        ("$0", None),
        ("1", None),
        (None, None),
    ],
)
def test_format_rent_recovers_noisy_single_values(raw: object, expected: float | None) -> None:
    assert _format_rent(raw) == expected


# ── jugnu._format_area / _safe_int_gt1: same narrow-parser defect class ──
from ma_poc.scripts.runners.jugnu import _format_area, _safe_int_gt1  # noqa: E402


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # recovered (were -1 before)
        ("1,200", 1200),
        ("1,200 sq ft", 1200),
        ("1200 sqft", 1200),
        ("1,200-1,400", 1200),  # range → low bound
        # regression: clean + sanity bound UNCHANGED
        ("1200", 1200),
        ("1200.0", 1200),
        (1200, 1200),
        ("070", -1),       # truncated garbage still rejected
        ("9", -1),         # bed count still rejected
        ("12500", -1),     # out-of-bound still rejected
        ("Studio", -1),
        (None, -1),
        (-1, -1),
    ],
)
def test_format_area_recovers_comma_suffixed_sqft(raw: object, expected: int) -> None:
    assert _format_area(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12 Months", 12),
        ("12 mo", 12),
        ("13-month", 13),
        ("12", 12),
        ("12.0", 12),
        (12, 12),
        ("1", None),        # > 1 guard preserved
        ("Flexible", None),
        (None, None),
    ],
)
def test_safe_int_gt1_extracts_leading_int(raw: object, expected: int | None) -> None:
    assert _safe_int_gt1(raw) == expected


# ── Capture-first: every emitted field gets a first-class <field>_raw ──
from ma_poc.scripts.runners.jugnu import _format_v2_unit  # noqa: E402

_PROCESSED_FIELDS = [
    "beds", "baths", "floor_plan_name", "floor_plan_id", "area", "unit_id",
    "rent_low", "rent_high", "date_captured", "available_date",
    "lease_term", "move_in_date",
]


def test_every_emitted_field_has_raw_companion_preserving_source() -> None:
    u = {
        "bedrooms": "2 Bedroom", "bathrooms": "1 Bath", "floor_plan_name": "A1",
        "sqft": "1,200 sq ft", "unit_number": "307",
        "market_rent_low": "From $1,450", "available_date": "Available 7/10/26",
        "lease_term": "12 Months",
    }
    o = _format_v2_unit(u, datetime(2026, 5, 19, tzinfo=UTC), "P1")

    # 1. raw companion exists for EVERY processed field
    for f in _PROCESSED_FIELDS:
        assert f"{f}_raw" in o, f"missing {f}_raw"

    # 2. raw preserves the uncoerced extracted value
    assert o["area"] == 1200 and o["area_raw"] == "1,200 sq ft"
    assert o["rent_low"] == 1450.0 and o["rent_low_raw"] == "From $1,450"
    assert o["available_date"] == "2026-07-10"
    assert o["available_date_raw"] == "Available 7/10/26"
    assert o["lease_term"] == 12 and o["lease_term_raw"] == "12 Months"

    # 3. recoverability: a normalization MISS is now visible in the data
    #    (deferred beds/baths normalizer) instead of silently lost
    assert o["beds"] is None and o["beds_raw"] == "2 Bedroom"
    assert o["baths"] is None and o["baths_raw"] == "1 Bath"

    # 4. derived/generated fields → raw None (honest, not fabricated)
    assert o["floor_plan_id_raw"] is None
    assert o["date_captured_raw"] is None


# ── jugnu._format_floor: ID-garbage must not ship as a floor number ──
from ma_poc.scripts.runners.jugnu import _format_floor  # noqa: E402


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3", 3),
        ("2nd", 2),
        ("Floor 12", 12),
        ("1", 1),
        ("100", 100),
        # garbage / out-of-range → None (the bug: IDs mis-mapped to floor)
        ("92657", None),
        ("50278", None),
        ("0", None),
        ("105", None),
        ("", None),
        ("B", None),
        (None, None),
    ],
)
def test_format_floor_rejects_id_garbage(raw: object, expected: int | None) -> None:
    assert _format_floor(raw) == expected


def test_floor_is_first_class_with_raw_companion_preserving_garbage() -> None:
    o = _format_v2_unit(
        {"unit_number": "A1", "floor": "92657", "beds": "1", "baths": "1"},
        datetime(2026, 5, 19, tzinfo=UTC),
        "P",
    )
    assert "floor" in o and "floor_raw" in o
    assert o["floor"] is None          # ID rejected from the clean field
    assert o["floor_raw"] == "92657"   # but preserved for post-processing


# ── available_units / building capture-first + source_ids merge plumbing ──
from ma_poc.pms.adapters._parsing import make_unit_dict  # noqa: E402


def test_available_units_building_and_source_ids_plumbed() -> None:
    u = make_unit_dict(
        unit_number="A1", bedrooms="1", building="Building 3",
        available_units="2",
        source_ids={"model_id": "B11", "building_id": "1", "listing_id": "9116"},
    )
    assert u["source_ids"] == {"model_id": "B11", "building_id": "1", "listing_id": "9116"}
    o = _format_v2_unit(u, datetime(2026, 5, 19, tzinfo=UTC), "P")
    assert o["building"] == "Building 3" and o["building_raw"] == "Building 3"
    assert o["available_units"] == 2 and o["available_units_raw"] == "2"
    assert o["source_ids"] == {"model_id": "B11", "building_id": "1", "listing_id": "9116"}


def test_source_ids_additive_default_is_empty_no_breakage() -> None:
    """Adapters that don't pass source_ids must be unaffected (empty {})."""
    u = make_unit_dict(unit_number="A2", bedrooms="1")
    assert u["source_ids"] == {}
    o = _format_v2_unit(u, datetime(2026, 5, 19, tzinfo=UTC), "P")
    assert o["source_ids"] == {}
    assert o["building"] is None and o["available_units"] is None


# ── Tier-A source_ids population: AppFolio / Spherexx / SightMap ──
def test_grounded_adapters_populate_source_ids() -> None:
    from ma_poc.pms.adapters.appfolio import parse_appfolio_listings_ssr
    from ma_poc.pms.adapters.sightmap import parse_sightmap_payload
    from ma_poc.pms.adapters.spherexx import _parse_spherexx_unit

    af = parse_appfolio_listings_ssr(
        '<div data-listing-id="165">'
        '<dd class="detail-box__value js-listing-blurb-rent">$1,200</dd>'
        '<dd class="detail-box__value js-listing-available">Available Now</dd></div>',
        "u",
    )
    assert af and af[0]["source_ids"] == {"appfolio_listing_id": "165"}

    sx = _parse_spherexx_unit(
        {"ID": "U1", "FloorplanID": "F1", "Name": "A1", "Bed": 1,
         "Bath": 1.0, "Price": 1500, "Sqft": 700}, "u")
    assert sx["source_ids"] == {"spherexx_unit_id": "U1", "spherexx_floorplan_id": "F1"}

    sm, _ = parse_sightmap_payload(
        {"data": {"units": [{"id": "S1", "floor_plan_id": "7", "price": 1400,
                             "unit_number": "12", "area": 700}],
                  "floor_plans": [{"id": "7", "name": "A1",
                                   "bedroom_count": 1, "bathroom_count": 1}]}}, "u")
    assert sm and sm[0]["source_ids"] == {
        "sightmap_unit_id": "S1", "sightmap_floor_plan_id": "7"}


# ── api_samples capture targeting (grounding aid for un-probeable PMS) ──
from ma_poc.scripts.runners.jugnu import (  # noqa: E402
    _API_SAMPLE_CAP_PER_ADAPTER,
    _API_SAMPLE_TIER_MARKERS,
)


@pytest.mark.parametrize(
    ("tier", "should_capture", "adapter"),
    [
        ("TIER_1_API_ENTRATA", True, "entrata"),
        ("TIER_1_API_ONESITE", True, "onesite"),
        ("TIER_1_API_RENTCAFE", True, "rentcafe"),
        ("TIER_1_KNOCK", True, "knock"),
        # already grounded / out of scope → never captured
        ("TIER_1_API_SIGHTMAP", False, None),
        ("TIER_1_DOM_APPFOLIO_SSR", False, None),
        ("TIER_4_LLM", False, None),
    ],
)
def test_api_sample_capture_targets_only_ungrounded_pms(
    tier: str, should_capture: bool, adapter: str | None
) -> None:
    hit = tier.startswith("TIER_1") and any(
        mk in tier.upper() for mk in _API_SAMPLE_TIER_MARKERS
    )
    assert hit is should_capture
    if hit:
        picked = next(
            mk.lower() for mk in _API_SAMPLE_TIER_MARKERS if mk in tier.upper()
        )
        assert picked == adapter
    assert _API_SAMPLE_CAP_PER_ADAPTER > 0
