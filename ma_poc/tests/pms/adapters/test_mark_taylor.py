"""Mark-Taylor adapter tests (2026-05-23).

Pins the Mark-Taylor Residential extraction path:
  • Detect ``window.PRELOADED_STATE`` + mark-taylor / gounion host marker
  • Walk the parsed state for ``floor_plan_meta`` nodes
  • Synthesize one plan-level row per bedroom count, all carrying the
    property-wide ``min_rent`` + ``min_sqft_rent`` floor

Background: 10 mark-taylor.com properties were mis-flagged as
"operator-data-gap" in the 2026-05-22 grind. The per-unit modal does
sit behind an authenticated XHR (api.selftournow.com), but the page
HTML already publishes property-level ``floor_plan_meta`` in
``window.PRELOADED_STATE``. That gives us enough to clear the Surgex
success bar (≥1 unit with rent+sqft) for the whole cohort.

Fixture: ``ma_poc/tests/fixtures/mark_taylor/waterside_at_ocotillo.html``
(live HTML pulled 2026-05-23, 1.3 MB).
"""
from __future__ import annotations

from pathlib import Path

from ma_poc.pms.adapters._mark_taylor import (
    derive_floor_plans_url,
    detect_mark_taylor,
    extract_preloaded_state,
    parse_mark_taylor_html,
)

# Anchor on this file, not the process CWD — ``pytest tests/pms`` from inside
# ma_poc/ must resolve fixtures the same way a repo-root run does.
_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "mark_taylor"
    / "waterside_at_ocotillo.html"
)


def _read_fixture() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


# ─── detector ────────────────────────────────────────────────────────


def test_detect_matches_live_mark_taylor_fixture() -> None:
    """The real Waterside-at-Ocotillo HTML must trip the detector."""
    assert detect_mark_taylor(
        _read_fixture(),
        "https://www.mark-taylor.com/apartments/az/chandler/"
        "waterside-at-ocotillo/floor-plans/",
    ) is True


def test_detect_rejects_empty_html() -> None:
    assert detect_mark_taylor("", "https://www.mark-taylor.com/x/") is False


def test_detect_rejects_html_without_preloaded_state() -> None:
    """Plain marketing HTML that happens to be on mark-taylor.com but
    has no ``window.PRELOADED_STATE`` (e.g. a static blog post) must
    not be claimed."""
    assert detect_mark_taylor(
        "<html><body>plain mark-taylor page</body></html>",
        "https://www.mark-taylor.com/",
    ) is False


def test_detect_falls_back_to_body_markers_when_url_missing() -> None:
    """CDN-fronted requests may not surface the mark-taylor host in
    the URL. The detector should still fire when the HTML body
    references the gounion CRM origin."""
    html = (
        '<script>window.PRELOADED_STATE = {"crm":"https://my.gounion.com"};'
        '</script>'
    )
    assert detect_mark_taylor(html, "") is True


def test_detect_rejects_preloaded_state_on_unrelated_host() -> None:
    """``window.PRELOADED_STATE`` is generic enough that other Redux
    sites use it. Without ANY mark-taylor / gounion marker we must
    not claim the page."""
    html = '<script>window.PRELOADED_STATE = {"x": 1};</script>'
    assert detect_mark_taylor(html, "https://www.example.com/") is False


# ─── extract_preloaded_state ─────────────────────────────────────────


def test_extract_preloaded_state_parses_live_fixture() -> None:
    state = extract_preloaded_state(_read_fixture())
    assert state is not None
    # Real fixture has the seo / sitePage / findYourHome / floorPlans keys
    assert "seo" in state
    assert "sitePage" in state


def test_extract_preloaded_state_returns_none_when_missing() -> None:
    assert extract_preloaded_state("") is None
    assert extract_preloaded_state("<html>no global</html>") is None


def test_extract_preloaded_state_handles_inline_braces_in_strings() -> None:
    """Braces inside JSON strings must not close the object early."""
    html = (
        'window.PRELOADED_STATE = {"name":"Apt {with} braces",'
        '"min":1300};'
    )
    state = extract_preloaded_state(html)
    assert state == {"name": "Apt {with} braces", "min": 1300}


def test_extract_preloaded_state_returns_none_on_malformed_json() -> None:
    html = "window.PRELOADED_STATE = {oops not json};"
    assert extract_preloaded_state(html) is None


def test_extract_preloaded_state_handles_trailing_semicolon_and_sibling_globals(
) -> None:
    """The script tag carries other ``window.X = …`` assignments after
    PRELOADED_STATE. The extractor must stop at the matching ``}``."""
    html = (
        '<script>window.A = "x"; '
        'window.PRELOADED_STATE = {"k": 42}; '
        'window.B = 1;</script>'
    )
    assert extract_preloaded_state(html) == {"k": 42}


# ─── parse_mark_taylor_html — synthesize plan-level rows ─────────────


def test_parse_live_fixture_emits_three_bedroom_rows() -> None:
    """Waterside-at-Ocotillo publishes ``bedrooms=[1,2,3], min_rent=1300,
    min_sqft_rent=756``. We must emit exactly three plan-level rows
    (1BR / 2BR / 3BR), each carrying both rent and sqft."""
    rows = parse_mark_taylor_html(
        _read_fixture(),
        "https://www.mark-taylor.com/apartments/az/chandler/"
        "waterside-at-ocotillo/floor-plans/",
    )
    assert len(rows) == 3
    beds = sorted(r["bedrooms"] for r in rows)
    assert beds == ["1", "2", "3"]
    for r in rows:
        assert r["market_rent_low"] == 1300
        assert r["sqft"] == "756"
        assert r["data_quality_flag"] == "PLAN_LEVEL_MIN_ONLY"
        assert "unit_number" in r["data_gaps"]


def test_parse_live_fixture_rows_carry_property_name_and_seo_slug() -> None:
    rows = parse_mark_taylor_html(
        _read_fixture(),
        "https://www.mark-taylor.com/apartments/az/chandler/"
        "waterside-at-ocotillo/floor-plans/",
    )
    assert rows
    assert rows[0]["source_ids"]["seo_url"] == "waterside-at-ocotillo"
    assert rows[0]["source_ids"]["property_name"] == "Waterside at Ocotillo"
    assert "Waterside at Ocotillo" in rows[0]["floor_plan_name"]


def test_parse_live_fixture_extraction_tier_is_marked() -> None:
    rows = parse_mark_taylor_html(
        _read_fixture(),
        "https://www.mark-taylor.com/apartments/az/chandler/"
        "waterside-at-ocotillo/floor-plans/",
    )
    assert rows
    assert (
        rows[0]["extraction_tier"]
        == "TIER_1_EMBEDDED_MARK_TAYLOR_PRELOADED_STATE"
    )


def test_parse_returns_empty_for_non_mark_taylor_html() -> None:
    """Pages without the detector hit must produce no rows — never
    a false positive on an unrelated site."""
    rows = parse_mark_taylor_html(
        "<html><body>just marketing</body></html>",
        "https://www.example.com/",
    )
    assert rows == []


def test_parse_handles_studio_in_bedrooms_array() -> None:
    """Studios (bedroom=0) must produce a row labeled 'Studio' rather
    than '0 Bed'."""
    html = (
        '<script>window.gounion = 1; window.PRELOADED_STATE = '
        '{"sitePage":{"property":{"name":"Test","seo_url":"test",'
        '"floor_plan_meta":{"bedrooms":[0,1],"min_rent":1100,'
        '"bathrooms":[1],"min_sqft_rent":450}}}};</script>'
    )
    rows = parse_mark_taylor_html(html, "https://gounion.com/test/")
    assert len(rows) == 2
    studio = [r for r in rows if r["bedrooms"] == "0"][0]
    assert studio["bed_label"] == "Studio"
    assert "Studio" in studio["floor_plan_name"]


def test_parse_skips_when_min_rent_missing_or_zero() -> None:
    """An operator that publishes ``floor_plan_meta`` with no rent
    floor — pre-launch property, etc. — must NOT produce rows.
    Better to fail-closed and let downstream cascade handle it than
    emit a misleading $0 row."""
    html = (
        '<script>window.PRELOADED_STATE = {"sitePage":{"property":{'
        '"name":"X","seo_url":"x","floor_plan_meta":{"bedrooms":[1],'
        '"min_rent":0,"bathrooms":[1],"min_sqft_rent":500}}}};'
        'gounion.com</script>'
    )
    assert parse_mark_taylor_html(html, "https://www.mark-taylor.com/x/") == []


def test_parse_skips_when_min_sqft_missing_or_zero() -> None:
    html = (
        '<script>window.PRELOADED_STATE = {"sitePage":{"property":{'
        '"name":"X","seo_url":"x","floor_plan_meta":{"bedrooms":[1],'
        '"min_rent":1200,"bathrooms":[1],"min_sqft_rent":0}}}};'
        'gounion.com</script>'
    )
    assert parse_mark_taylor_html(html, "https://www.mark-taylor.com/x/") == []


def test_parse_skips_invalid_bedroom_values() -> None:
    """Bedrooms outside 0–10 (corrupt data) must be filtered, but
    valid bedrooms in the same list must still produce rows."""
    html = (
        '<script>window.PRELOADED_STATE = {"sitePage":{"property":{'
        '"name":"X","seo_url":"x","floor_plan_meta":{'
        '"bedrooms":[1,99,"junk",2],"min_rent":1500,'
        '"bathrooms":[1],"min_sqft_rent":600}}}};'
        'gounion.com</script>'
    )
    rows = parse_mark_taylor_html(html, "https://www.mark-taylor.com/x/")
    beds = sorted(r["bedrooms"] for r in rows)
    assert beds == ["1", "2"]


def test_parse_uses_minimum_bathroom_as_floor() -> None:
    """When ``bathrooms=[1,2]``, every row gets ``bathrooms=1`` (we
    can't pair beds-to-baths without per-plan detail; floor is safer
    than guessing)."""
    rows = parse_mark_taylor_html(
        _read_fixture(),
        "https://www.mark-taylor.com/apartments/az/chandler/"
        "waterside-at-ocotillo/floor-plans/",
    )
    for r in rows:
        assert r["bathrooms"] == "1"


# ─── derive_floor_plans_url ──────────────────────────────────────────


def test_derive_floor_plans_url_from_property_home() -> None:
    home = "https://www.mark-taylor.com/apartments/az/chandler/waterside-at-ocotillo/"
    assert (
        derive_floor_plans_url(home)
        == "https://www.mark-taylor.com/apartments/az/chandler/"
        "waterside-at-ocotillo/floor-plans/"
    )


def test_derive_floor_plans_url_handles_no_trailing_slash() -> None:
    home = "https://www.mark-taylor.com/apartments/az/chandler/waterside-at-ocotillo"
    out = derive_floor_plans_url(home)
    assert out is not None
    assert out.endswith("/waterside-at-ocotillo/floor-plans/")


def test_derive_floor_plans_url_returns_none_for_non_property_url() -> None:
    """URLs that aren't ``/apartments/{state}/{city}/{slug}/`` must
    return None — we don't want to send junk requests."""
    assert derive_floor_plans_url("https://www.mark-taylor.com/") is None
    assert (
        derive_floor_plans_url("https://www.mark-taylor.com/about/")
        is None
    )
    assert derive_floor_plans_url("") is None


# ─── rendered-DOM per-plan extractor ─────────────────────────────────


def test_rendered_plan_cards_extracted_when_present() -> None:
    """When the page is JS-hydrated (Playwright RENDER mode), Mark-
    Taylor renders one card per plan with starting rent + sqft + beds
    + baths in canonical text. Verify the rendered-DOM extractor wins
    over PRELOADED_STATE plan-level synthesis."""
    rendered = (
        '<script>window.PRELOADED_STATE = {"sitePage":{"property":{'
        '"name":"Waterside at Ocotillo","seo_url":"waterside-at-ocotillo",'
        '"floor_plan_meta":{"bedrooms":[1,2,3],"min_rent":1300,'
        '"bathrooms":[1,2],"min_sqft_rent":756}}}}; '
        'window.gounion = 1;</script>'
        '<section>A1\n\n$1,420+\n\n3 Available\n1 bed\n1 bath\n756 sq. ft.</section>'
        '<section>B1\n\n$1,300+\n\n4 Available\n1 bed\n1 bath\n804 sq. ft.</section>'
        '<section>A2\n\n$1,620+\n\n5 Available\n2 bed\n2 bath\n939 sq. ft.</section>'
        '<section>B2\n\n$1,570+\n\n4 Available\n2 bed\n2 bath\n1023 sq. ft.</section>'
        '<section>C3\n\n$1,940+\n\n3 Available\n3 bed\n2 bath\n1142 sq. ft.</section>'
    )
    rows = parse_mark_taylor_html(
        rendered,
        "https://www.mark-taylor.com/apartments/az/chandler/"
        "waterside-at-ocotillo/floor-plans/",
    )
    # 5 cards extracted (A1/B1/A2/B2/C3), each with rent+sqft+beds+baths
    assert len(rows) == 5
    plan_codes = sorted(r["source_ids"]["plan_code"] for r in rows)
    assert plan_codes == ["A1", "A2", "B1", "B2", "C3"]
    for r in rows:
        assert r["extraction_tier"] == "TIER_1_DOM_MARK_TAYLOR_RENDERED_PLAN_CARD"
        assert r["data_quality_flag"] == "PLAN_LEVEL_STARTING_RENT"


def test_rendered_plan_cards_preserve_per_plan_rent_and_sqft() -> None:
    """Verify the canonical Waterside-at-Ocotillo card values
    (A1: $1,420 / 756 / 1 bed; C3: $1,940 / 1142 / 3 bed)."""
    rendered = (
        '<script>window.PRELOADED_STATE = {"sitePage":{"property":{'
        '"name":"Waterside","seo_url":"x","floor_plan_meta":'
        '{"bedrooms":[1],"min_rent":1300,"bathrooms":[1],"min_sqft_rent":756}}}};'
        ' gounion.com</script>'
        '<div>A1\n\n$1,420+\n\n3 Available\n1 bed\n1 bath\n756 sq. ft.</div>'
        '<div>C3\n\n$1,940+\n\n3 Available\n3 bed\n2 bath\n1142 sq. ft.</div>'
    )
    rows = parse_mark_taylor_html(rendered, "https://gounion.com/test/")
    by_plan = {r["source_ids"]["plan_code"]: r for r in rows}
    assert by_plan["A1"]["market_rent_low"] == 1420
    assert by_plan["A1"]["sqft"] == "756"
    assert by_plan["C3"]["market_rent_low"] == 1940
    assert by_plan["C3"]["sqft"] == "1142"
    assert by_plan["C3"]["bedrooms"] == "3"


def test_rendered_plan_cards_fall_back_to_preloaded_state_when_absent() -> None:
    """When the static HTML has only skeleton cards (no rendered text),
    the rendered-DOM extractor returns [] and we fall back to the
    PRELOADED_STATE plan-level synthesis."""
    rows = parse_mark_taylor_html(
        _read_fixture(),
        "https://www.mark-taylor.com/apartments/az/chandler/"
        "waterside-at-ocotillo/floor-plans/",
    )
    # Live fixture is the static (un-hydrated) HTML — only 3 plan-
    # level synthesis rows, NOT 5 per-plan cards.
    assert len(rows) == 3
    for r in rows:
        assert (
            r["extraction_tier"]
            == "TIER_1_EMBEDDED_MARK_TAYLOR_PRELOADED_STATE"
        )
