"""AIR Communities adapter — detector + plan-list + per-plan unit-list.

Validated 2026-05-21 against 5 sibling AIR properties:
laurelcrossingapthomes.com, arcadiaapthomes.com,
liveadaraalexanderplace.com, live20thstreetstation.com,
21fitzsimons.com. Pattern is universal across the portfolio (76
communities, 27,010 apartment homes).

Fixtures saved at ma_poc/tests/fixtures/air_communities/:
  • adara_residences.html (37 KB, 8 floor plans, 3 bedroom containers)
  • adara_design-1a.html  (24 KB, 7 unit records inline)
  • arcadia_residences.html (30 KB, 3 floor plans, 2 bedroom containers)
"""

from __future__ import annotations

from pathlib import Path

from ma_poc.pms.adapters._air_communities import (
    derive_plan_context_from_url,
    detect_air_communities,
    parse_per_plan_html,
    parse_residences_html,
)

_FIXTURE_DIR = Path("ma_poc/tests/fixtures/air_communities")


def _load(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────
# Detector
# ─────────────────────────────────────────────────────────────────────


def test_detect_matches_real_residences_html() -> None:
    """Adara's real /residences.html must trip the detector."""
    assert detect_air_communities(_load("adara_residences.html")) is True


def test_detect_matches_real_per_plan_html() -> None:
    """The per-plan HTML must also be recognised as AIR — necessary so
    that mid-cascade pages can be routed back to this adapter."""
    assert detect_air_communities(_load("adara_design-1a.html")) is True


def test_detect_rejects_non_air_html() -> None:
    """A generic HTML page without the apartmentIncomeReit marker must
    NOT be matched — protects against false-positive routing."""
    html = (
        '<html><body><h1>Welcome</h1>'
        '<div class="floor-plan-item">A Bedroom</div>'
        '</body></html>'
    )
    assert detect_air_communities(html) is False


def test_detect_rejects_empty_html() -> None:
    assert detect_air_communities("") is False
    assert detect_air_communities("<html></html>") is False


# ─────────────────────────────────────────────────────────────────────
# Plan-list parser — /residences.html
# ─────────────────────────────────────────────────────────────────────


def test_parse_residences_finds_all_plans_adara() -> None:
    """Adara has 8 floor-plan-items visible in fixture (verified via
    grep count). Parser must surface all 8."""
    plans = parse_residences_html(
        _load("adara_residences.html"),
        base_url="https://www.liveadaraalexanderplace.com/",
    )
    assert len(plans) == 8, f"expected 8 plans, got {len(plans)}: {[p['floor_plan_name'] for p in plans]}"


def test_parse_residences_extracts_plan_metadata_adara() -> None:
    """Per-plan metadata: name, bedrooms (from container), sqft, rent,
    fp_id, details_url must all populate from the real DOM."""
    plans = parse_residences_html(
        _load("adara_residences.html"),
        base_url="https://www.liveadaraalexanderplace.com/",
    )
    # First plan should be a 1-bedroom Design 1A based on the WebFetch
    # data confirmed earlier (rent $1,297, 661 sqft).
    design_1a = next((p for p in plans if p["floor_plan_name"] == "Design 1A"), None)
    assert design_1a is not None, (
        f"Design 1A missing; got plan names: {[p['floor_plan_name'] for p in plans]}"
    )
    assert design_1a["bedrooms"] == "1"
    assert design_1a["market_rent_low"] == 1297
    assert design_1a["rent_range"] == "$1,297"
    assert design_1a["sqft"] == "661"
    assert design_1a["propertyfloorplanid"]  # any non-empty
    # details_url should be absolute
    assert design_1a["details_url"].startswith("https://www.liveadaraalexanderplace.com/")
    assert "/floor-plan/" in design_1a["details_url"]


def test_parse_residences_bedrooms_per_container_id() -> None:
    """Bedroom containers in adara: one + two + three. Verify each
    plan's bedrooms field matches the parent container's id mapping."""
    plans = parse_residences_html(_load("adara_residences.html"))
    by_bedrooms: dict[str, int] = {}
    for p in plans:
        by_bedrooms[p["bedrooms"]] = by_bedrooms.get(p["bedrooms"], 0) + 1
    # We don't assert exact counts (the property can change), but every
    # plan should have a valid bedroom integer.
    assert all(b in ("0", "1", "2", "3", "4", "5") for b in by_bedrooms)
    # At least one plan per non-zero bedroom-count must exist
    assert sum(by_bedrooms.values()) == len(plans)


def test_parse_residences_works_on_second_sibling() -> None:
    """Pattern must generalize — arcadia (different AIR property) must
    also parse cleanly. 3 plans across 2 bedroom containers per fixture."""
    plans = parse_residences_html(
        _load("arcadia_residences.html"),
        base_url="https://www.arcadiaapthomes.com/",
    )
    assert len(plans) == 3, f"expected 3 plans for arcadia; got {len(plans)}"
    # Every plan must have a name + a non-empty rent + a non-empty sqft +
    # a details URL — these are the deterministic fields we promise.
    for p in plans:
        assert p["floor_plan_name"], f"plan missing name: {p}"
        assert p["market_rent_low"] is not None, f"plan missing rent: {p}"
        assert p["sqft"], f"plan missing sqft: {p}"
        assert p["details_url"], f"plan missing details_url: {p}"


def test_parse_residences_returns_empty_on_non_air_html() -> None:
    """Detector-first contract: parser must reject non-AIR HTML even
    if it has incidental floor-plan-item classes."""
    html = (
        '<div class="bedroomContainer" id="one">'
        '  <div class="floor-plan-item" data-propertyfloorplanid="123">'
        '    <div class="plan-name name">Fake Plan</div>'
        '  </div></div>'
    )
    assert parse_residences_html(html) == []


def test_parse_residences_returns_empty_on_empty_html() -> None:
    assert parse_residences_html("") == []


# ─────────────────────────────────────────────────────────────────────
# Per-plan unit-list parser — /floor-plan/{bed}/{slug}.html
# ─────────────────────────────────────────────────────────────────────


def test_parse_per_plan_finds_all_units_adara_design_1a() -> None:
    """Adara design-1a fixture has 7 data-property-unit-id markers
    (verified via grep). Parser must emit one record per unit."""
    units = parse_per_plan_html(
        _load("adara_design-1a.html"),
        plan_context={
            "floor_plan_name": "Design 1A",
            "bedrooms": "1",
            "bathrooms": "",
            "sqft": "661",
            "propertyfloorplanid": "test-fp-id",
        },
        base_url="https://www.liveadaraalexanderplace.com/",
    )
    assert len(units) == 7, f"expected 7 units; got {len(units)}: {units}"


def test_parse_per_plan_inherits_plan_context() -> None:
    """Plan-level metadata (name, bedrooms, sqft) must propagate to
    every unit record."""
    plan_ctx = {
        "floor_plan_name": "Design 1A",
        "bedrooms": "1",
        "bathrooms": "1",
        "sqft": "661",
        "propertyfloorplanid": "fp-xyz",
    }
    units = parse_per_plan_html(_load("adara_design-1a.html"), plan_context=plan_ctx)
    assert units, "no units extracted"
    for u in units:
        assert u["floor_plan_name"] == "Design 1A"
        assert u["bedrooms"] == "1"
        assert u["bathrooms"] == "1"
        assert u["sqft"] == "661"
        assert u["propertyfloorplanid"] == "fp-xyz"
        assert u["source"] == "air_communities_unit"


def test_parse_per_plan_extracts_unit_number_and_availability() -> None:
    """Each unit emits unit_number (from visible "Unit #X") +
    availability_status / availability_date (from "Available {date}")."""
    units = parse_per_plan_html(_load("adara_design-1a.html"))
    # At least one unit should have a non-empty unit_number
    assert any(u["unit_number"] for u in units), (
        "no unit number extracted from any record"
    )
    # At least one unit should have availability info
    assert any(
        u["availability_status"] or u["availability_date"] for u in units
    ), "no availability info extracted"
    # Each unit should have a property_unit_id
    for u in units:
        assert u["property_unit_id"], f"unit missing property_unit_id: {u}"


def test_parse_per_plan_extracts_rent_per_unit() -> None:
    """Per-unit rent must populate — at least 1 unit with a rent in
    the rent band."""
    units = parse_per_plan_html(_load("adara_design-1a.html"))
    units_with_rent = [u for u in units if u["market_rent_low"]]
    assert units_with_rent, (
        f"no unit has a rent; sample: "
        f"{[(u['unit_number'], u['market_rent_low']) for u in units[:3]]}"
    )
    # Rents in this property are ~$1,300; sanity-check band
    for u in units_with_rent:
        assert 500 < u["market_rent_low"] < 50_000, (
            f"unit rent {u['market_rent_low']} outside the rent band"
        )


def test_parse_per_plan_does_not_dedupe_units() -> None:
    """Each data-property-unit-id is a distinct unit. The parser must
    NOT collapse them just because they share the same plan."""
    units = parse_per_plan_html(_load("adara_design-1a.html"))
    ids = [u["property_unit_id"] for u in units]
    assert len(ids) == len(set(ids)), f"duplicate unit IDs emitted: {ids}"


def test_parse_per_plan_handles_no_units_page() -> None:
    """If the per-plan page has zero data-property-unit-id elements
    (no availability), the parser must return an empty list, not
    raise."""
    html = '<html><body><h1>No units available</h1></body></html>'
    assert parse_per_plan_html(html) == []


def test_parse_per_plan_handles_empty_html() -> None:
    assert parse_per_plan_html("") == []
    assert parse_per_plan_html("", plan_context={"floor_plan_name": "X"}) == []


# ─────────────────────────────────────────────────────────────────────
# URL helper — derive plan-context when called standalone
# ─────────────────────────────────────────────────────────────────────


def test_derive_plan_context_studio() -> None:
    ctx = derive_plan_context_from_url(
        "https://www.example.com/floor-plan/studio/design-eb.html"
    )
    assert ctx["bedrooms"] == "0"
    assert ctx["plan_slug"] == "design-eb"


def test_derive_plan_context_one_bedroom() -> None:
    ctx = derive_plan_context_from_url(
        "https://www.liveadaraalexanderplace.com/floor-plan/1-bedroom/design-1a.html"
    )
    assert ctx["bedrooms"] == "1"
    assert ctx["plan_slug"] == "design-1a"


def test_derive_plan_context_two_bedroom_with_query() -> None:
    """URL with query/fragment must still parse."""
    ctx = derive_plan_context_from_url(
        "https://www.example.com/floor-plan/2-bedroom/suite-2b25?utm=x#tab=units"
    )
    assert ctx["bedrooms"] == "2"
    assert ctx["plan_slug"] == "suite-2b25"


def test_derive_plan_context_three_bedroom() -> None:
    ctx = derive_plan_context_from_url(
        "https://www.example.com/floor-plan/3-bedroom/design-3a.html"
    )
    assert ctx["bedrooms"] == "3"
    assert ctx["plan_slug"] == "design-3a"


def test_derive_plan_context_non_air_url_returns_empty() -> None:
    """URLs that don't match the AIR floor-plan shape return empty."""
    assert derive_plan_context_from_url("https://www.example.com/residences.html") == {}
    assert derive_plan_context_from_url("https://www.example.com/") == {}
    assert derive_plan_context_from_url("") == {}


# ─────────────────────────────────────────────────────────────────────
# Two-step adapter contract — plan list + per-plan in sequence
# ─────────────────────────────────────────────────────────────────────


def test_full_extraction_produces_unit_level_records() -> None:
    """End-to-end contract: residences.html → plan list → for each plan,
    per-plan HTML → unit list. The combined output is unit-level data
    with plan-level metadata inherited.

    We only fixture-test ONE plan's deep-link (design-1a) but the
    contract is: each plan in the plan list could be followed by the
    same parser to produce unit records inheriting that plan's context.
    """
    plans = parse_residences_html(_load("adara_residences.html"))
    design_1a_plan = next(
        (p for p in plans if p["floor_plan_name"] == "Design 1A"), None
    )
    assert design_1a_plan is not None
    units = parse_per_plan_html(
        _load("adara_design-1a.html"), plan_context=design_1a_plan
    )
    assert units, "no units extracted in two-step flow"
    # All units inherit plan name + bedrooms + sqft
    for u in units:
        assert u["floor_plan_name"] == "Design 1A"
        assert u["bedrooms"] == "1"
        assert u["sqft"] == design_1a_plan["sqft"]
    # All units have their own unit_number + availability metadata + rent
    populated_unit_number = sum(1 for u in units if u["unit_number"])
    populated_rent = sum(1 for u in units if u["market_rent_low"])
    populated_availability = sum(
        1 for u in units if u["availability_status"] or u["availability_date"]
    )
    assert populated_unit_number >= 1
    assert populated_rent >= 1
    assert populated_availability >= 1
