"""ThinkRESIDE / Resite Multi Family Marketing adapter — tests.

Acceptance (deep-probe 2026-05-25 — Orchard Ridge / Indy Flats /
Deer Run cohort, canary 1ef1060 post-phase16-v2):

* Detector HTML markers (``thinkresite.dev`` / ``themes.thinkresite
  .cloud`` / ``resite-themes.nyc3.digitaloceanspaces.com`` / ``Resite
  Multi Family Marketing``) → pms="thinkreside" @ 0.87.
* ``<li data-beds>`` plan cards on a Pattern-A ``/floorplans`` index
  → plan-summary dicts with beds/baths/sqft/price/detail_url/status.
* ``<div class="floorplan-item" data-beds>`` plan cards on a Pattern-B
  ascent-theme home page → plan-summary dicts (name from ``id`` attr).
* ``<table class="fp-availability-list"> <tbody> <tr>`` rows on a per-
  plan detail page → unit dicts with unit_number, ISO date, rent.
* Empty ``<tbody>`` (operator hasn't published units) → 0 unit rows
  (caller emits plan-level summary).
* End-to-end ``ThinkResideAdapter.extract`` on live HTML + mocked
  per-plan fetches emits ≥10 admitted unit rows from Indy Flats.
* Bedroom 0 (Studio) preserved as ``"0"`` not coerced to ``""``.
* Date boundary: raw ``"Now"`` survives to the formatter, which resolves it
  against the capture date with ``available_now`` provenance; explicit dates
  remain exact.
* Rent parsing: ``"$830.0000"`` → 830; ``"$1,250.00"`` → 1250;
  ``"Call for pricing"`` → ``None``.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import ma_poc.pms.adapters  # noqa: F401 — populate adapter registry
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.registry import get_adapter
from ma_poc.pms.adapters.thinkreside import (
    ThinkResideAdapter,
    _fetch_text,
    _norm_avail_date,
    _strip_dollars,
    parse_thinkreside_plan_index,
    parse_thinkreside_unit_table,
    thinkreside_plan_summary_row,
)
from ma_poc.pms.detector import _detect_html_markers

FIXTURES = Path(__file__).parent / "fixtures" / "thinkreside"
INDY_FLOORPLANS = (FIXTURES / "indyflats_floorplans_index.html").read_text(encoding="utf-8")
INDY_BARBEE_DETAIL = (FIXTURES / "indyflats_barbee_1bd_detail.html").read_text(encoding="utf-8")
INDY_HOME = (FIXTURES / "indyflats_home.html").read_text(encoding="utf-8")
OR_HOME = (FIXTURES / "orchardridge_home_ascent.html").read_text(encoding="utf-8")
OR_ONE_BD_DETAIL = (FIXTURES / "orchardridge_one_bedroom_detail.html").read_text(encoding="utf-8")
DEER_HOME = (FIXTURES / "deerrun_home_townth.html").read_text(encoding="utf-8")
DEER_FLOORPLANS = (FIXTURES / "deerrun_floorplans_towncommunity.html").read_text(
    encoding="utf-8"
)


# ─── unit helpers ────────────────────────────────────────────────────


def test_norm_avail_date_handles_now_iso_and_mmddyyyy() -> None:
    """Raw Now survives; explicit dates normalize without value loss."""
    assert _norm_avail_date("Now") == "Now"
    assert _norm_avail_date("  now  ") == "now"  # source case, whitespace trimmed
    assert _norm_avail_date("06/23/2026") == "2026-06-23"
    assert _norm_avail_date("6/3/2026") == "2026-06-03"  # single-digit pad
    assert _norm_avail_date("2026-06-23") == "2026-06-23"
    # Two-digit-year fallback (POSIX strptime convention).
    assert _norm_avail_date("3/7/26") == "2026-03-07"
    # Garbage / empty → ""
    assert _norm_avail_date("") == ""
    assert _norm_avail_date("Available soon!") == ""
    assert _norm_avail_date("13/40/2026") == ""  # invalid date


def test_strip_dollars_parses_vendor_money_formats() -> None:
    """ThinkRESIDE rents come as $830.0000 (4 dec) or $1,250.00."""
    assert _strip_dollars("$830.0000") == 830
    assert _strip_dollars("$1,250.00") == 1250
    assert _strip_dollars("$680") == 680
    # Embedded in text → first dollar value wins.
    assert _strip_dollars("Starting at $1,795 — call for details") == 1795
    # No numeric → None (NOT 0).
    assert _strip_dollars("Call for pricing") is None
    assert _strip_dollars("") is None


# ─── plan-index parsing (Pattern A: <li data-beds>) ──────────────────


def test_pattern_a_li_plan_cards_parsed_from_indyflats() -> None:
    """Indy Flats /floorplans index has 21 <li data-beds> plan cards."""
    plans = parse_thinkreside_plan_index(
        INDY_FLOORPLANS, "https://www.indyflatsapts.com"
    )
    assert len(plans) == 21
    # First card: Windsor Studio (0 BR / 1 BA / 467 sqft / $680 / 8 units).
    first = plans[0]
    assert first["name"] == "Windsor Studio"
    assert first["beds"] == "0"  # Studio preserved as "0", not ""
    assert first["baths"] == "1"
    assert first["sqft"] == "467"
    assert first["price_raw"] == "$680"
    assert first["rent_low"] == 680
    assert first["detail_url"] == (
        "https://www.indyflatsapts.com/floorplans/windsor-studio"
    )
    assert first["status_units"] == 8
    # Plan list includes barbee-1-bedroom (target of detail-page test).
    slugs = {p["detail_url"].rsplit("/", 1)[-1] for p in plans}
    assert "barbee-1-bedroom" in slugs


# ─── plan-index parsing (Pattern B: <div class="floorplan-item">) ────


def test_pattern_b_div_plan_cards_parsed_from_orchardridge_home() -> None:
    """Orchard Ridge (ascent theme) ships floorplan-item divs on home."""
    plans = parse_thinkreside_plan_index(
        OR_HOME, "https://www.liveatorchardridge.com"
    )
    assert len(plans) == 3
    names = {p["name"] for p in plans}
    assert names == {"One Bedroom", "Two Bedroom", "Three Bedroom"}
    one_bd = next(p for p in plans if p["name"] == "One Bedroom")
    assert one_bd["beds"] == "1"
    assert one_bd["baths"] == "1"
    assert one_bd["sqft"] == "648"
    assert one_bd["price_raw"] == "Call for pricing"
    # Non-numeric price → rent_low is None (no fabricated 0).
    assert one_bd["rent_low"] is None
    assert one_bd["detail_url"] == (
        "https://www.liveatorchardridge.com/floorplans/one-bedroom"
    )


# ─── plan-index parsing (Pattern C: <li class="floorplan">) ─────────


def test_pattern_c_towncommunity_cards_preserve_exact_deer_run_catalogue() -> None:
    """Current Deer Run source is exactly four ordered catalogue plans."""
    plans = parse_thinkreside_plan_index(
        DEER_FLOORPLANS, "https://www.liveatdeerrunapts.com"
    )

    assert [p["name"] for p in plans] == [
        "2 Bdrm 1.5 Bath -Ranch or Split Ranch Style",
        "One Bedroom - Ranch Style",
        "Two Bedroom 1.5 Bath - Garden Style",
        "Two Bedroom 2 Bath - Ranch or Garden Style",
    ]
    assert [(p["beds"], p["baths"]) for p in plans] == [
        ("2", "1.5"),
        ("1", "1"),
        ("2", "1.5"),
        ("2", "2"),
    ]
    assert [p["sqft"] for p in plans] == ["1050", "728", "1150", "1,050 - 1,150"]
    assert (plans[-1]["sqft_low"], plans[-1]["sqft_high"]) == (1050, 1150)
    assert [(p["rent_low"], p["rent_high"]) for p in plans] == [
        (1430, 1430),
        (1225, 1225),
        (1420, 1430),
        (1450, 1450),
    ]
    assert [p["detail_url"].rsplit("/", 1)[-1] for p in plans] == [
        "2-bdrm-15-bath-ranch-or-split-ranch-style",
        "one-bedroom-ranch-style",
        "two-bedroom-15-bath-garden-style",
        "two-bedroom-2-bath-ranch-or-garden-style",
    ]


def test_pattern_c_rejects_cross_property_and_ambiguous_detail_links() -> None:
    """A card is admitted only with one same-property detail route."""
    cross_host = DEER_FLOORPLANS.replace(
        'href="/floorplans/one-bedroom-ranch-style"',
        'href="https://sibling.example/floorplans/one-bedroom-ranch-style"',
        1,
    )
    plans = parse_thinkreside_plan_index(
        cross_host, "https://www.liveatdeerrunapts.com"
    )
    assert len(plans) == 3
    assert "One Bedroom - Ranch Style" not in {p["name"] for p in plans}

    ambiguous = DEER_FLOORPLANS.replace(
        '<a href="/floorplans/one-bedroom-ranch-style">View</a>',
        '<a href="/floorplans/one-bedroom-ranch-style">View</a>'
        '<a href="/floorplans/sibling-plan">Recommended</a>',
        1,
    )
    plans = parse_thinkreside_plan_index(
        ambiguous, "https://www.liveatdeerrunapts.com"
    )
    assert len(plans) == 3
    assert "One Bedroom - Ranch Style" not in {p["name"] for p in plans}


# ─── per-plan unit-table parsing ──────────────────────────────────────


def test_unit_table_parsed_from_indyflats_barbee_detail() -> None:
    """Barbee 1-bedroom detail page has 3 unit rows in fp-availability-list."""
    plan = {
        "name": "Barbee 1 Bedroom",
        "beds": "1",
        "baths": "1",
        "sqft": "650",
        "rent_low": 850,
    }
    units = parse_thinkreside_unit_table(
        INDY_BARBEE_DETAIL,
        plan,
        "https://www.indyflatsapts.com/floorplans/barbee-1-bedroom",
    )
    assert len(units) == 3
    unit_numbers = {u["unit_number"] for u in units}
    assert unit_numbers == {"210", "304", "312"}
    # Per-row rent ($830.0000) parses to 830.
    u210 = next(u for u in units if u["unit_number"] == "210")
    assert u210["market_rent_low"] == 830
    assert u210["market_rent_high"] == 830
    # The source-relative token survives until the capture-aware formatter.
    assert u210["availability_date"] == "Now"
    assert u210["availability_status"] == "AVAILABLE"
    # Plan-level dims spliced onto each row.
    assert u210["bedrooms"] == "1"
    assert u210["bathrooms"] == "1"
    # sqft cell empty → falls back to plan's data-sqft.
    assert u210["sqft"] == "650"
    assert u210["floor_plan_name"] == "Barbee 1 Bedroom"
    assert u210["extraction_tier"] == "TIER_1_DOM_THINKRESIDE"
    # source_ids carries plan slug + unit number.
    assert u210["source_ids"]["thinkreside_plan_slug"] == "barbee-1-bedroom"
    assert u210["source_ids"]["thinkreside_unit"] == "210"
    # 06/23/2026 → 2026-06-23 (ISO normalised).
    u312 = next(u for u in units if u["unit_number"] == "312")
    assert u312["availability_date"] == "2026-06-23"


def test_thinkreside_now_and_future_dates_survive_source_to_final() -> None:
    """Formatter owns capture-date resolution and provenance classification."""
    from ma_poc.scripts.runners.jugnu import _format_v2_unit

    plan = {
        "name": "Barbee 1 Bedroom",
        "beds": "1",
        "baths": "1",
        "sqft": "650",
    }
    html = """
    <table class="fp-availability-list"><tbody>
      <tr><td>210</td><td data-date="Now">Now</td><td></td><td>$830.0000</td></tr>
      <tr><td>312</td><td data-date="08/25/2026">08/25/2026</td><td></td><td>$850.0000</td></tr>
    </tbody></table>
    """
    parsed = parse_thinkreside_unit_table(
        html, plan, "https://www.indyflatsapts.com/floorplans/barbee-1-bedroom"
    )
    capture = datetime(2026, 8, 1, 23, 30, tzinfo=UTC)
    now_out = _format_v2_unit(parsed[0], capture, "271195")
    future_out = _format_v2_unit(parsed[1], capture, "271195")

    assert parsed[0]["availability_date"] == "Now"
    assert now_out["available_date"] == "2026-08-01"
    assert now_out["availability_date_provenance"] == "available_now"
    assert parsed[1]["availability_date"] == "2026-08-25"
    assert future_out["available_date"] == "2026-08-25"
    assert future_out["availability_date_provenance"] == "explicit_future"


def test_unit_table_empty_tbody_returns_no_rows() -> None:
    """Orchard Ridge one-bedroom detail has empty tbody — operator hasn't
    published per-unit inventory. parse returns [] so the caller emits
    a plan-level summary."""
    plan = {"name": "One Bedroom", "beds": "1", "baths": "1", "sqft": "648"}
    units = parse_thinkreside_unit_table(
        OR_ONE_BD_DETAIL,
        plan,
        "https://www.liveatorchardridge.com/floorplans/one-bedroom",
    )
    assert units == []


def test_unit_table_no_fp_availability_list_returns_no_rows() -> None:
    """HTML without an fp-availability-list table → 0 rows (defensive)."""
    plan = {"name": "Studio", "beds": "0", "baths": "1", "sqft": "467"}
    assert parse_thinkreside_unit_table("", plan, "u") == []
    assert (
        parse_thinkreside_unit_table(
            "<html><body>no table here</body></html>", plan, "u"
        )
        == []
    )


# ─── plan-level summary fallback ──────────────────────────────────────


def test_plan_summary_price_without_inventory_remains_unknown() -> None:
    """A catalogue rent without a roster/count is not availability proof."""
    plan = {
        "name": "Barbee 1 Bedroom",
        "beds": "1",
        "baths": "1",
        "sqft": "650",
        "price_raw": "$850",
        "rent_low": 850,
        "detail_url": "https://x.com/floorplans/barbee-1-bedroom",
        "status_units": None,
    }
    row = thinkreside_plan_summary_row(plan, plan["detail_url"])
    assert row is not None
    assert row["availability_status"] == "UNKNOWN"
    assert row["market_rent_low"] == 850
    assert row["floor_plan_name"] == "Barbee 1 Bedroom"
    assert row["unit_number"] == ""  # plan-level, no unit
    assert row["bedrooms"] == "1"
    assert row["source_ids"]["thinkreside_plan_slug"] == "barbee-1-bedroom"


def test_plan_summary_positive_unit_count_is_available() -> None:
    plan = {
        "name": "Barbee 1 Bedroom",
        "beds": "1",
        "baths": "1",
        "sqft": "650",
        "rent_low": 850,
        "rent_high": 875,
        "detail_url": "https://x.com/floorplans/barbee-1-bedroom",
        "status_units": 3,
    }
    row = thinkreside_plan_summary_row(plan, plan["detail_url"])
    assert row is not None
    assert row["availability_status"] == "AVAILABLE"
    assert row["available_units"] == "3"
    assert row["market_rent_low"] == 850
    assert row["market_rent_high"] == 875


def test_plan_summary_emits_unknown_for_call_for_pricing() -> None:
    """"Call for pricing" plans → UNKNOWN, no rent emitted."""
    plan = {
        "name": "One Bedroom",
        "beds": "1",
        "baths": "1",
        "sqft": "648",
        "price_raw": "Call for pricing",
        "rent_low": None,
        "detail_url": "https://x.com/floorplans/one-bedroom",
        "status_units": None,
    }
    row = thinkreside_plan_summary_row(plan, plan["detail_url"])
    assert row is not None
    assert row["availability_status"] == "UNKNOWN"
    assert row["market_rent_low"] is None


def test_plan_summary_emits_unavailable_when_status_zero() -> None:
    """status_units==0 (explicit "0 left") → UNAVAILABLE row."""
    plan = {
        "name": "Sold-out Studio",
        "beds": "0",
        "baths": "1",
        "sqft": "388",
        "price_raw": "$680",
        "rent_low": 680,
        "detail_url": "",
        "status_units": 0,
    }
    row = thinkreside_plan_summary_row(plan, "")
    assert row is not None
    assert row["availability_status"] == "UNAVAILABLE"
    assert row["available_units"] == "0"


# ─── detector wiring ──────────────────────────────────────────────────


def test_detector_routes_thinkreside_via_dev_api_marker() -> None:
    """``api.thinkresite.dev`` in HTML → pms="thinkreside" @ 0.87."""
    html = (
        "<html><body>"
        '<script>fetch("https://api.thinkresite.dev/neighborhoods/abc")</script>'
        "</body></html>"
    ).lower()
    res = _detect_html_markers(html)
    assert res is not None
    assert res[0] == "thinkreside"
    assert res[1] >= 0.87


def test_detector_routes_thinkreside_via_resite_themes_cdn() -> None:
    """resite-themes.nyc3.digitaloceanspaces.com (asset CDN) → thinkreside."""
    html = (
        '<script src="https://resite-themes.nyc3.digitaloceanspaces.com/'
        'ascent/assets/js/scripts.js"></script>'
    ).lower()
    res = _detect_html_markers(html)
    assert res is not None
    assert res[0] == "thinkreside"


def test_detector_routes_thinkreside_via_resite_powered_by_footer() -> None:
    """"Powered by Resite Multi Family Marketing" footer → thinkreside."""
    html = (
        "<footer>Powered by <a href=\"https://thinkresite.com\">"
        "Resite Multi Family Marketing</a></footer>"
    ).lower()
    res = _detect_html_markers(html)
    assert res is not None
    assert res[0] == "thinkreside"


def test_detector_routes_thinkreside_on_live_indyflats_home() -> None:
    """Indy Flats home HTML (live-captured) routes to thinkreside.

    Indy Flats also embeds a MeetElise chat widget — encoreskyline_template
    fires at 0.85. Thinkreside is bumped to 0.87 specifically to win in
    this co-resident pattern: the SSR Resite plan list is the data
    source, the chat widget is decorative.
    """
    res = _detect_html_markers(INDY_HOME.lower())
    assert res is not None, "detector returned None on live Indy Flats HTML"
    assert res[0] == "thinkreside", f"got {res[0]} not thinkreside"


def test_detector_routes_thinkreside_on_live_orchardridge_home() -> None:
    """Orchard Ridge ascent-theme home HTML yields thinkreside as a
    candidate (Knock Doorway co-resident at 0.90 wins routing — that's
    by design, Knock IS the real PMS when its widget is loaded).
    """
    from ma_poc.pms.detector import _iter_html_markers

    all_yields = list(_iter_html_markers(OR_HOME.lower()))
    pms_set = {m[0] for m in all_yields}
    assert "thinkreside" in pms_set, (
        f"thinkreside not in yielded PMSs: {pms_set}"
    )


# ─── registry wiring + adapter contract ───────────────────────────────


def test_adapter_registered_with_correct_pms_name() -> None:
    a = get_adapter("thinkreside")
    assert isinstance(a, ThinkResideAdapter)
    assert a.pms_name == "thinkreside"


def test_adapter_static_fingerprints_returns_copy() -> None:
    a = ThinkResideAdapter()
    fps = a.static_fingerprints()
    assert "thinkresite.dev" in fps
    assert "resite multi family marketing" in fps
    # Returned list is a copy — mutation doesn't leak.
    fps.append("mutation")
    assert "mutation" not in a.static_fingerprints()


def test_adapter_matches_response_body() -> None:
    a = ThinkResideAdapter()
    assert a.matches_response_body(INDY_HOME) is True
    assert a.matches_response_body(INDY_HOME.encode("utf-8")) is True
    assert a.matches_response_body("nothing resite here") is False
    assert a.matches_response_body(None) is False
    assert a.matches_response_body(12345) is False


# ─── full adapter dispatch (end-to-end) ───────────────────────────────


class _FakeFetchResult:
    """Mimics jugnu's FetchResult — only ``body`` is read."""

    def __init__(self, body: str, final_url: str = "") -> None:
        self.body = body
        self.final_url = final_url


def _ctx(html: str, base_url: str) -> AdapterContext:
    from ma_poc.pms.detector import DetectedPMS

    return AdapterContext(
        base_url=base_url,
        detected=DetectedPMS(pms="thinkreside", confidence=0.87, evidence=["test"]),
        profile=None,
        expected_total_units=None,
        property_id="thinkreside_test_prop",
        fetch_result=_FakeFetchResult(html, base_url),
    )


def test_adapter_extract_end_to_end_on_indyflats_floorplans_index() -> None:
    """Full dispatch: L1 body is /floorplans, per-plan fetches mocked.

    Mocks return the Barbee detail HTML for every per-plan probe — all
    21 plans drill to the same 3-unit table, so the adapter should
    emit 21 × 3 = 63 unit-level rows. Plan-level fallbacks fire only
    for plans whose detail page returns no table, which doesn't
    happen here.
    """
    fetched: list[str] = []

    def mock_fetch(url: str, timeout: int = 20) -> str:
        fetched.append(url)
        # Pretend every per-plan URL returns the Barbee detail page.
        return INDY_BARBEE_DETAIL

    adapter = ThinkResideAdapter()
    ctx = _ctx(INDY_FLOORPLANS, "https://www.indyflatsapts.com")
    with patch(
        "ma_poc.pms.adapters.thinkreside._fetch_text", side_effect=mock_fetch
    ):
        result = asyncio.run(adapter.extract(page=None, ctx=ctx))  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_DOM_THINKRESIDE", (
        f"unexpected tier: {result.tier_used}; errors: {result.errors}"
    )
    # 21 plans × 3 units each = 63 admitted rows (all unit-level).
    assert len(result.units) == 63
    # No plan-level fallbacks fire — every plan had a unit table.
    # The fetch helper was called once per plan with a detail_url.
    assert len(fetched) == 21
    # All fetched URLs are absolute and rooted at the site host.
    for u in fetched:
        assert u.startswith("https://www.indyflatsapts.com/floorplans/")


def test_adapter_extract_falls_back_to_plan_level_when_detail_empty() -> None:
    """Orchard Ridge has plan cards on home + empty unit tables on
    detail pages — adapter should emit 3 plan-level summaries, 0 units.
    """

    def mock_fetch(url: str, timeout: int = 20) -> str:
        # Every detail page is the empty-tbody Orchard Ridge variant.
        return OR_ONE_BD_DETAIL

    adapter = ThinkResideAdapter()
    ctx = _ctx(OR_HOME, "https://www.liveatorchardridge.com")
    with patch(
        "ma_poc.pms.adapters.thinkreside._fetch_text", side_effect=mock_fetch
    ):
        result = asyncio.run(adapter.extract(page=None, ctx=ctx))  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_DOM_THINKRESIDE"
    # 3 plan-summaries (One/Two/Three Bedroom), 0 unit-level.
    assert len(result.units) == 0
    assert len(result.plan_summaries) == 3
    # All plans were "Call for pricing" → UNKNOWN status.
    assert {s["availability_status"] for s in result.plan_summaries} == {"UNKNOWN"}


def test_adapter_extract_deer_run_emits_exact_four_undated_plans() -> None:
    """Dedicated Pattern-C success suppresses the lossy generic overlap."""
    from ma_poc.scripts.runners.jugnu import _format_v2_floor_plan

    fetches: list[str] = []

    def mock_fetch(url: str, timeout: int = 20) -> str:
        fetches.append(url)
        if url.rstrip("/").endswith("/floorplans"):
            return DEER_FLOORPLANS
        return "<html><body><p>Catalogue detail only.</p></body></html>"

    adapter = ThinkResideAdapter()
    ctx = _ctx(DEER_HOME, "https://www.liveatdeerrunapts.com")
    with patch(
        "ma_poc.pms.adapters.thinkreside._fetch_text", side_effect=mock_fetch
    ):
        result = asyncio.run(adapter.extract(page=None, ctx=ctx))  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_DOM_THINKRESIDE"
    assert result.units == []
    assert len(result.plan_summaries) == 4
    assert [row["floor_plan_name"] for row in result.plan_summaries] == [
        "2 Bdrm 1.5 Bath -Ranch or Split Ranch Style",
        "One Bedroom - Ranch Style",
        "Two Bedroom 1.5 Bath - Garden Style",
        "Two Bedroom 2 Bath - Ranch or Garden Style",
    ]
    assert {row["availability_status"] for row in result.plan_summaries} == {
        "UNKNOWN"
    }
    assert {row["availability_date"] for row in result.plan_summaries} == {""}
    assert [row["source_ids"]["thinkreside_plan_slug"] for row in result.plan_summaries] == [
        "2-bdrm-15-bath-ranch-or-split-ranch-style",
        "one-bedroom-ranch-style",
        "two-bedroom-15-bath-garden-style",
        "two-bedroom-2-bath-ranch-or-garden-style",
    ]
    assert result.plan_summaries[2]["rent_range"] == "$1,420 - $1,430"
    assert result.plan_summaries[3]["sqft"] == "1,050 - 1,150"
    assert fetches[0] == "https://www.liveatdeerrunapts.com/floorplans"

    final = [
        _format_v2_floor_plan(
            row,
            datetime(2026, 8, 2, 12, tzinfo=UTC),
            "51921",
        )
        for row in result.plan_summaries
    ]
    assert [row["area"] for row in final] == [1050, 728, 1150, 1050]
    assert [(row["rent_low"], row["rent_high"]) for row in final] == [
        (1430, 1430),
        (1225, 1225),
        (1420, 1430),
        (1450, 1450),
    ]
    assert {row["availability_status"] for row in final} == {"UNKNOWN"}
    assert {row["available_date"] for row in final} == {None}
    assert {row["availability_date_provenance"] for row in final} == {"missing"}


def test_fetch_text_disables_web_unlocker() -> None:
    """The production ThinkReside route stays on direct first-party HTTP."""
    class Response:
        status_code = 200
        text = "ok"

    with patch(
        "ma_poc.pms.adapters._probe.probe_get", return_value=Response()
    ) as mocked:
        assert _fetch_text("https://example.test/floorplans") == "ok"
    mocked.assert_called_once_with(
        "https://example.test/floorplans", timeout=20, unlocker=False
    )


def test_adapter_extract_no_fingerprint_bails_without_fetch() -> None:
    """HTML missing every Resite marker → NO_FINGERPRINT, no fetches fired."""
    fired = False

    def boom(url: str, timeout: int = 20) -> str:
        nonlocal fired
        fired = True
        return ""

    adapter = ThinkResideAdapter()
    ctx = _ctx(
        "<html><body>generic marketing site</body></html>",
        "https://example.com",
    )
    with patch(
        "ma_poc.pms.adapters.thinkreside._fetch_text", side_effect=boom
    ):
        result = asyncio.run(adapter.extract(page=None, ctx=ctx))  # type: ignore[arg-type]
    assert result.tier_used.endswith("_NO_FINGERPRINT")
    assert fired is False, "fetch fired despite missing fingerprint"
    assert result.units == []


def test_adapter_extract_no_html_body_bails_cleanly() -> None:
    """Empty ctx.fetch_result.body → NO_HTML, no crash."""
    adapter = ThinkResideAdapter()
    ctx = _ctx("", "https://example.com")
    result = asyncio.run(adapter.extract(page=None, ctx=ctx))  # type: ignore[arg-type]
    assert result.tier_used.endswith("_NO_HTML")
    assert result.units == []


def test_adapter_extract_probes_floorplans_when_home_has_no_cards() -> None:
    """L1 body is a Resite home page with no plan cards → adapter
    probes ``{base}/floorplans`` once for the index. Verifies the
    fallback discovery path.
    """
    fetches: list[str] = []

    def mock_fetch(url: str, timeout: int = 20) -> str:
        fetches.append(url)
        if url.endswith("/floorplans"):
            # Return the index page when probed.
            return INDY_FLOORPLANS
        if url.endswith("/barbee-1-bedroom"):
            return INDY_BARBEE_DETAIL
        # Other per-plan probes — empty so they fall to plan-summary.
        return "<html></html>"

    adapter = ThinkResideAdapter()
    # L1 body is the Indy Flats home (Resite fingerprint, no plan cards).
    ctx = _ctx(INDY_HOME, "https://www.indyflatsapts.com")
    with patch(
        "ma_poc.pms.adapters.thinkreside._fetch_text", side_effect=mock_fetch
    ):
        result = asyncio.run(adapter.extract(page=None, ctx=ctx))  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_DOM_THINKRESIDE"
    # /floorplans was probed exactly once first.
    assert fetches[0] == "https://www.indyflatsapts.com/floorplans"
    # 3 Barbee units + 20 plan-level fallbacks (empty <html> details) = 23.
    assert len(result.units) == 3
    assert len(result.plan_summaries) == 20
