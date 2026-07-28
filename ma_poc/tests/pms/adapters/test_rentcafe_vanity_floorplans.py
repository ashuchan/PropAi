"""SecureCafe sqft FK-join from vanity /floorplans tests (2026-05-25).

Background — the sqft=-1 cluster B fix:
  TIER_1_API_RENTCAFE_SECURECAFE shows ~67% adapter-miss rate for sqft
  across ~52 properties (~475 units). Three independent fallbacks
  already chain (plan-name → WP-cards → apts247), but a residual cohort
  ships units missing sqft because:
    - they are NOT apts247-backed (no ``window.api_key`` token)
    - the homepage is NOT a RentCafe-WP template (no ``floorplans-box``)
    - the SC plan-name header lacks the ``<beds>x<baths> <sqft>`` token

  Fix: fetch the public marketing ``/floorplans`` (or ``/floor-plans``)
  page, parse plan-level (beds, baths, sqft, rent?) records via four
  pattern recognisers, then FK-join sqft onto SC units by (beds, baths).
  When more than one plan shares the bucket, pick the closest-rent plan.

Live-probed 2026-05-25 (user-flagged cluster, 4 distinct patterns):
  - vestaviaplace.com (prose): "1 Bedroom, 1 Bathroom 700 sq. ft."
  - alvista23.com (ysi.floorplansList JSON): MinSqFt + Beds + Baths +
    MinRent + MaxRent inline
  - ardencebloom.com (data-attr): data-beds="2 Bed" data-baths="2"
    data-sqft=" 1119"
  - themtroyal.com (operator-data-gap): sqft only in disjoint English
    prose; no per-plan record → confirms the operator-gap flag path
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ma_poc.pms.adapters.base import AdapterResult
from ma_poc.pms.adapters.rentcafe import (
    VANITY_FLOORPLAN_PATHS,
    _enrich_securecafe_units_with_vanity_floorplans,
    _slice_balanced_json_array,
    fetch_vanity_floorplans_html,
    merge_vanity_floorplans_into_securecafe,
    parse_vanity_floorplans_for_sqft,
)

_FIX_DIR = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "rentcafe"
    / "vanity_floorplans"
)


def _load_fixture(name: str) -> str:
    return (_FIX_DIR / name).read_text(encoding="utf-8")


# ─── parser pattern (1): ysi.floorplansList JSON (alvista23) ─────────────


def test_parse_ysi_floorplans_list_live_alvista23() -> None:
    """alvista23.com (live 2026-05-25) embeds ysi.floorplansList with
    Beds/Baths/MinSqFt/MinRent/MaxRent for 3 plans. Verifies the parser
    extracts all three with full rent metadata for the closest-rent
    tie-break path."""
    html = _load_fixture("alvista23_floorplans.html")
    plans = parse_vanity_floorplans_for_sqft(html)
    sources = {p["source"] for p in plans}
    assert "ysi_floorplansList" in sources
    ysi_plans = [p for p in plans if p["source"] == "ysi_floorplansList"]
    assert len(ysi_plans) == 3
    # Live values from alvista23 — verified by hand against the rendered
    # page on 2026-05-25.
    keys = {(p["beds"], p["baths"], p["sqft"]) for p in ysi_plans}
    assert (1, 1.0, 832) in keys
    assert (2, 2.0, 1050) in keys
    assert (3, 2.0, 1178) in keys
    # Rent metadata must be populated for the multi-plan-bucket tie-break.
    one_bed = [p for p in ysi_plans if p["beds"] == 1][0]
    assert one_bed["rent_lo"] == 1476
    assert one_bed["rent_hi"] == 1717


def test_parse_ysi_filters_js_template_minSqft_noise() -> None:
    """themtroyal.com has 'minSqft' as JS function args
    (``function setGA4Cookie(tour, fpname, size, minSqft, maxSqft,...)``)
    but NO ysi.floorplansList data record. The parser must NOT confuse
    JS-template tokens for data — return 0 plans for themtroyal."""
    html = _load_fixture("themtroyal_floorplans.html")
    plans = parse_vanity_floorplans_for_sqft(html)
    # themtroyal publishes sqft only in disjoint English prose like
    # "550 to 1,100 square feet" — no co-located (beds, baths, sqft)
    # triple anywhere. Operator-data-gap is the correct outcome.
    assert plans == []


# ─── parser pattern (2): data-beds/baths/sqft cluster (ardencebloom) ─────


def test_parse_data_attr_cluster_live_ardencebloom() -> None:
    """ardencebloom.com (live 2026-05-25) emits <li class='unitPlaceholder'
    data-beds=... data-baths=... data-sqft=...> elements. The dedup by
    (beds, baths, sqft) collapses unit-level duplicates into plan-level
    records — exactly what the FK-join needs."""
    html = _load_fixture("ardencebloom_floorplans.html")
    plans = parse_vanity_floorplans_for_sqft(html)
    sources = {p["source"] for p in plans}
    assert "data_attr_bbs" in sources
    # User-flagged plan B5: 2 Bed / 2 Bath / 1119 sq ft.
    assert any(
        p["beds"] == 2 and p["baths"] == 2.0 and p["sqft"] == 1119
        for p in plans
    )
    # The 1-bed/1-bath/761 plan is on the page too — guard one more.
    assert any(
        p["beds"] == 1 and p["baths"] == 1.0 and p["sqft"] == 761
        for p in plans
    )


def test_parse_data_attr_cluster_synthetic() -> None:
    """Minimal synthetic markup — confirms the regex captures attrs
    regardless of order within the opening tag and tolerates whitespace
    inside the value (live data has data-sqft=' 1119' with leading
    space)."""
    html = """
    <li class="unitPlaceholder"
        data-floor="3"
        data-beds="2"
        data-baths="2"
        data-sqft=" 1050"
        data-planTitle="B3">
    </li>
    """
    plans = parse_vanity_floorplans_for_sqft(html)
    assert any(
        p["beds"] == 2 and p["baths"] == 2.0 and p["sqft"] == 1050
        for p in plans
    )


# ─── parser pattern (4): natural-language prose (vestaviaplace) ──────────


def test_parse_prose_pattern_live_vestaviaplace() -> None:
    """vestaviaplace.com (live 2026-05-25) carries floorplan info in
    English copy: "1 Bedroom, 1 Bathroom 700 sq. ft.". Three distinct
    plans after dedup; no rent metadata (prose pattern doesn't carry
    rent — the FK-join falls back to median-sqft tie-break)."""
    html = _load_fixture("vestaviaplace_floor-plans.html")
    plans = parse_vanity_floorplans_for_sqft(html)
    sources = {p["source"] for p in plans}
    assert "prose" in sources
    prose_plans = [p for p in plans if p["source"] == "prose"]
    assert len(prose_plans) == 3
    keys = {(p["beds"], p["baths"], p["sqft"]) for p in prose_plans}
    # Live values from vestaviaplace — verified by hand 2026-05-25.
    assert (1, 1.0, 700) in keys
    assert (2, 1.0, 900) in keys
    assert (2, 1.5, 900) in keys
    # Prose pattern carries no rent.
    for p in prose_plans:
        assert p["rent_lo"] is None and p["rent_hi"] is None


def test_parse_prose_does_not_false_match_unrelated_numbers() -> None:
    """The prose regex requires the bed/bath/sq.ft anchor sequence with
    a comma between bedroom and bathroom — unrelated marketing copy
    must NOT produce a false plan record."""
    # Common false-positive shapes: rent in marketing copy, year, ZIP.
    benign = (
        "<p>Studio apartments starting at $1,200/mo. "
        "Located in zip 35216. Built in 1999.</p>"
        "<p>Two-bedroom homes from $1,800.</p>"
    )
    assert parse_vanity_floorplans_for_sqft(benign) == []


# ─── parser pattern (3): slash-separated label ───────────────────────────


def test_parse_slash_separated_label() -> None:
    """The "X Bed / Y Bath / Z sq ft" pattern. ardencebloom carries this
    label inside the card heading; the standalone regex must match it
    too in case data-attr capture misses a card variant."""
    html = "<h3>B5 2 Bed / 2 Bath / 1119 sq ft</h3>"
    plans = parse_vanity_floorplans_for_sqft(html)
    keys = {(p["beds"], p["baths"], p["sqft"]) for p in plans}
    assert (2, 2.0, 1119) in keys


# ─── parser: dedup ───────────────────────────────────────────────────────


def test_parse_dedups_same_plan_across_patterns() -> None:
    """When a vanity page emits the same (beds, baths, sqft) tuple via
    both data-attrs AND the slash label, dedup collapses them so the
    FK-join doesn't see a fake multi-plan bucket. The first-seen record
    wins (preserves rent metadata when ysi.floorplansList runs first)."""
    html = """
    <li data-beds="2" data-baths="2" data-sqft="1119"></li>
    <h3>B5 2 Bed / 2 Bath / 1119 sq ft</h3>
    """
    plans = parse_vanity_floorplans_for_sqft(html)
    matches = [
        p
        for p in plans
        if p["beds"] == 2 and p["baths"] == 2.0 and p["sqft"] == 1119
    ]
    assert len(matches) == 1


# ─── balanced-bracket helper ─────────────────────────────────────────────


def test_slice_balanced_json_array_handles_nested_brackets() -> None:
    """The ysi.floorplansList JSON contains nested ``Amenities: []``
    arrays. A naive ``.search(r'\\[.*?\\]')`` regex would truncate at
    the first ``]``; the stack-free scanner must respect bracket depth."""
    text = "junk before ysi.floorplansList = [ {\"a\": [1,2,3]}, {\"b\": 4} ] // tail"
    start = text.find("[")
    blob = _slice_balanced_json_array(text, start)
    assert blob == '[ {"a": [1,2,3]}, {"b": 4} ]'


def test_slice_balanced_json_array_handles_brackets_in_strings() -> None:
    """A ``]`` inside a string literal must not close the array."""
    text = '[ {"name": "Stop [Right] Here"}, {"x": 1} ]'
    blob = _slice_balanced_json_array(text, 0)
    assert blob == text


# ─── FK-join: single-plan bucket ─────────────────────────────────────────


def _sc_unit(
    unit_number: str = "119",
    beds: str = "1",
    baths: str = "1.0",
    sqft: str = "",
    rent_low: int | None = 1500,
    rent_high: int | None = 1700,
) -> dict[str, Any]:
    """Build a SecureCafe-shape unit dict matching what
    ``parse_securecafe_availableunits`` produces."""
    return {
        "unit_number": unit_number,
        "bedrooms": beds,
        "bathrooms": baths,
        "sqft": sqft,
        "market_rent_low": rent_low,
        "market_rent_high": rent_high,
        "floor_plan_name": "",
        "source_ids": {},
    }


def _plan(
    beds: int, baths: float, sqft: int,
    rent_lo: int | None = None, rent_hi: int | None = None, name: str = "",
) -> dict[str, Any]:
    return {
        "beds": beds, "baths": baths, "sqft": sqft,
        "rent_lo": rent_lo, "rent_hi": rent_hi,
        "name": name, "source": "test",
    }


def test_merge_single_plan_bucket_fills_sqft() -> None:
    """The simplest case: one plan matches the unit's (beds, baths) — use
    it directly. This is also the existing apts247-fallback behavior;
    parity is intentional."""
    units = [_sc_unit(beds="2", baths="2.0", sqft="")]
    plans = [_plan(2, 2.0, 1119)]
    n = merge_vanity_floorplans_into_securecafe(units, plans)
    assert n == 1
    assert units[0]["sqft"] == "1119"


def test_merge_preserves_existing_sqft() -> None:
    """Per-unit values WIN — the vanity merge must NOT overwrite a
    non-zero sqft that SC already populated (e.g. when the operator
    populated the cell on this unit but not the rest)."""
    units = [_sc_unit(beds="2", baths="2.0", sqft="1100")]
    plans = [_plan(2, 2.0, 1119)]
    n = merge_vanity_floorplans_into_securecafe(units, plans)
    assert n == 0
    assert units[0]["sqft"] == "1100"


def test_merge_overwrites_zero_sqft() -> None:
    """The pure-zero case — same convention as the apts247 fallback.
    SC AvailUnitRow with an empty Sq.Ft cell can serialise as either
    ``""`` or ``"0"``; both must be treated as missing."""
    units = [_sc_unit(beds="1", baths="1.0", sqft="0")]
    plans = [_plan(1, 1.0, 700)]
    n = merge_vanity_floorplans_into_securecafe(units, plans)
    assert n == 1
    assert units[0]["sqft"] == "700"


# ─── FK-join: multi-plan bucket → exact plan identity ─────────────────────


def test_merge_multi_plan_bucket_uses_exact_plan_name() -> None:
    """A plan-name foreign key safely selects the sibling plan's sqft."""
    units = [
        _sc_unit(beds="1", baths="1.0", sqft="", rent_low=1500, rent_high=1500),
    ]
    units[0]["floor_plan_name"] = "B2 - Renovated"
    plans = [
        _plan(1, 1.0, 700, name="A1"),
        _plan(1, 1.0, 850, name="B2 Renovated"),
        _plan(1, 1.0, 1000, name="C3"),
    ]
    n = merge_vanity_floorplans_into_securecafe(units, plans)
    assert n == 1
    assert units[0]["sqft"] == "850"


def test_merge_multi_plan_bucket_without_exact_name_stays_partial() -> None:
    """Do not choose a sibling plan by rent or median area."""
    units = [
        _sc_unit(beds="2", baths="1.0", sqft="", rent_low=None, rent_high=None),
    ]
    plans = [
        _plan(2, 1.0, 800),  # min
        _plan(2, 1.0, 900),  # median
        _plan(2, 1.0, 1100),  # max
    ]
    n = merge_vanity_floorplans_into_securecafe(units, plans)
    assert n == 0
    assert units[0]["sqft"] == ""


def test_merge_skips_unit_when_no_bucket_match() -> None:
    """A unit whose (beds, baths) doesn't appear in any plan bucket is
    left untouched. The operator-gap flag pass will then stamp it as
    SQFT_NOT_PUBLISHED."""
    units = [_sc_unit(beds="3", baths="2.0", sqft="")]
    plans = [_plan(1, 1.0, 700), _plan(2, 1.0, 900)]
    n = merge_vanity_floorplans_into_securecafe(units, plans)
    assert n == 0
    assert units[0]["sqft"] == ""


def test_merge_skips_unit_with_unparseable_bedbath() -> None:
    """A SC unit with non-numeric beds or baths (defensive — bad upstream
    data) must NOT crash; just skip it."""
    units = [_sc_unit(beds="", baths="")]
    plans = [_plan(1, 1.0, 700)]
    n = merge_vanity_floorplans_into_securecafe(units, plans)
    assert n == 0
    assert units[0]["sqft"] == ""


# ─── end-to-end FK-join on real fixture data ─────────────────────────────


def test_e2e_alvista23_fk_join_via_homepage_html() -> None:
    """End-to-end against the alvista23 fixture: 3 SC-shape units
    spanning (1,1.0), (2,2.0), (3,2.0) — all three pull sqft from
    the embedded ysi.floorplansList. This is the most common path
    in production because ysi.floorplansList is often duplicated on
    the homepage, eliminating the round-trip."""
    html = _load_fixture("alvista23_floorplans.html")
    plans = parse_vanity_floorplans_for_sqft(html)
    units = [
        _sc_unit(unit_number="101", beds="1", baths="1.0"),
        _sc_unit(unit_number="201", beds="2", baths="2.0"),
        _sc_unit(unit_number="301", beds="3", baths="2.0"),
    ]
    n = merge_vanity_floorplans_into_securecafe(units, plans)
    assert n == 3
    assert units[0]["sqft"] == "832"
    assert units[1]["sqft"] == "1050"
    assert units[2]["sqft"] == "1178"


def test_e2e_vestaviaplace_fk_join_via_prose_pattern() -> None:
    """End-to-end against the vestaviaplace fixture: prose pattern
    yields 3 plans with no rent metadata. SC units with no rent fall
    through to the median-sqft tie-break; with rent, they pick the
    closest. Here the (2, 1.0) bucket has only one plan (900) and the
    (2, 1.5) bucket has only one (900) — both single-plan."""
    html = _load_fixture("vestaviaplace_floor-plans.html")
    plans = parse_vanity_floorplans_for_sqft(html)
    units = [
        _sc_unit(unit_number="A1", beds="1", baths="1.0"),
        _sc_unit(unit_number="B1", beds="2", baths="1.0"),
        _sc_unit(unit_number="C1", beds="2", baths="1.5"),
    ]
    n = merge_vanity_floorplans_into_securecafe(units, plans)
    assert n == 3
    assert units[0]["sqft"] == "700"
    assert units[1]["sqft"] == "900"
    assert units[2]["sqft"] == "900"


# ─── enrichment wrapper: in-hand HTML vs probe round-trip ────────────────


def test_enrich_uses_homepage_html_when_it_carries_ysi_list() -> None:
    """When the homepage HTML already carries ysi.floorplansList (common
    for Yardi vanity templates), the enrichment must NOT make a network
    round-trip — it parses the in-hand body first. We verify by patching
    ``fetch_vanity_floorplans_html`` to raise if called."""
    homepage = _load_fixture("alvista23_floorplans.html")
    units = [_sc_unit(beds="1", baths="1.0")]
    result = AdapterResult()

    class _Ctx:
        # Minimal AdapterContext stand-in — _origin_from_ctx tolerates
        # missing attrs by returning "" so this only matters if the
        # round-trip path runs. Test asserts it does NOT run.
        url = "https://www.alvista23.com/"
        fetch_result = None

    with patch(
        "ma_poc.pms.adapters.rentcafe.fetch_vanity_floorplans_html"
    ) as mock_fetch:
        mock_fetch.side_effect = AssertionError(
            "must not round-trip when in-hand HTML carries plans"
        )
        _enrich_securecafe_units_with_vanity_floorplans(
            units, _Ctx(), homepage, result
        )
        mock_fetch.assert_not_called()
    assert units[0]["sqft"] == "832"


def test_enrich_falls_back_to_probe_when_homepage_lacks_plans() -> None:
    """When the homepage HTML carries no plan data, the enrichment
    falls through to fetching the vanity /floorplans page. We patch
    ``fetch_vanity_floorplans_html`` to return the alvista23 fixture
    body and verify the join still works end-to-end."""
    homepage = "<html><body>Welcome — see our floor plans</body></html>"
    vp_body = _load_fixture("alvista23_floorplans.html")
    units = [_sc_unit(beds="2", baths="2.0")]
    result = AdapterResult()

    class _Ctx:
        url = "https://www.alvista23.com/"
        fetch_result = None

    with patch(
        "ma_poc.pms.adapters.rentcafe.fetch_vanity_floorplans_html",
        return_value=vp_body,
    ) as mock_fetch, patch(
        "ma_poc.pms.adapters.rentcafe._origin_from_ctx",
        return_value="https://www.alvista23.com",
    ):
        _enrich_securecafe_units_with_vanity_floorplans(
            units, _Ctx(), homepage, result
        )
        mock_fetch.assert_called_once_with("https://www.alvista23.com")
    assert units[0]["sqft"] == "1050"


def test_enrich_swallows_errors_silently() -> None:
    """Any unhandled exception path in the round-trip helpers MUST NOT
    raise out to the SC drill — the unit list still ships, the gap
    flag pass picks them up. Specifically: when origin is empty the
    helper returns; when probe raises ImportError curl_cffi-missing
    the fetcher returns ''. Both are no-ops on units."""
    units = [_sc_unit(beds="1", baths="1.0")]
    result = AdapterResult()

    class _Ctx:
        url = ""  # forces _origin_from_ctx to return ""
        fetch_result = None

    # Empty page_html → no plans from in-hand; empty origin → no probe.
    _enrich_securecafe_units_with_vanity_floorplans(units, _Ctx(), "", result)
    assert units[0]["sqft"] == ""  # untouched
    # No spurious error entries from a no-op enrichment.
    assert not any(
        "vanity-floorplans-enrich" in e for e in result.errors
    )


# ─── constants/contract ──────────────────────────────────────────────────


def test_vanity_floorplan_paths_constant() -> None:
    """The probe tries both un-hyphenated /floorplans and hyphenated
    /floor-plans — vestaviaplace uses the hyphenated form, alvista23
    uses the unhyphenated. Order matters (unhyphenated first matches
    the larger share of the cohort)."""
    assert VANITY_FLOORPLAN_PATHS == ("/floorplans", "/floor-plans")


def test_fetch_returns_empty_on_empty_origin() -> None:
    """Defensive: caller may pass an empty origin string (eg
    _origin_from_ctx couldn't resolve). Fetcher returns '' — does NOT
    raise and does NOT issue a network call."""
    assert fetch_vanity_floorplans_html("") == ""


def test_parse_returns_empty_on_empty_html() -> None:
    """Defensive: empty body returns []. The four-pattern walk MUST
    handle the empty-string base case without raising or scanning."""
    assert parse_vanity_floorplans_for_sqft("") == []
    assert parse_vanity_floorplans_for_sqft("   ") == []


# ─── parametrized live fixtures ──────────────────────────────────────────


@pytest.mark.parametrize(
    "fixture,expected_pattern,min_plans",
    [
        ("alvista23_floorplans.html", "ysi_floorplansList", 3),
        ("vestaviaplace_floor-plans.html", "prose", 3),
        ("ardencebloom_floorplans.html", "data_attr_bbs", 5),
    ],
    ids=["alvista23-yardi-json", "vestavia-prose", "ardence-data-attr"],
)
def test_live_fixture_pattern_coverage(
    fixture: str, expected_pattern: str, min_plans: int
) -> None:
    """Sanity check: each live fixture covers its expected pattern. If
    a future template change drops the pattern, this regresses early
    instead of silently losing the cohort."""
    html = _load_fixture(fixture)
    plans = parse_vanity_floorplans_for_sqft(html)
    sources = {p["source"] for p in plans}
    assert expected_pattern in sources, (
        f"{fixture} expected to carry '{expected_pattern}' but got {sources}"
    )
    matching = [p for p in plans if p["source"] == expected_pattern]
    assert len(matching) >= min_plans, (
        f"{fixture} expected ≥{min_plans} {expected_pattern} plans, "
        f"got {len(matching)}"
    )
