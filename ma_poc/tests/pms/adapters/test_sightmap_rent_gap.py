"""SightMap operator-rent-gap flagging tests (2026-05-23).

Pins the gating + flag-application semantics of
``_flag_sightmap_units_operator_rent_gap``. The fix lifts 8 non-Avalon
SightMap properties (1,571 units) out of SUCCESS_PLAN_LEVEL by
recognising that the operator has configured SightMap to hide rent
(every unit's ``price`` is null while every other field — unit_id,
area, plan_name, beds, baths — is fully populated).

Live-verified 2026-05-23 against the SightMap embed/API for:
  - wimberlyapthome.com (Eaves North Dallas): 372/372 units null
    price + show_pricing=true
  - roserawesmont.com: 295/295 units null price + show_pricing=true
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ma_poc.pms.adapters._parsing import make_unit_dict
from ma_poc.pms.adapters.base import AdapterResult
from ma_poc.pms.adapters.sightmap import (
    _flag_sightmap_units_operator_rent_gap,
    _try_avalon_override_for_sightmap,
    _try_subpage_sightmap_with_prices,
)


def _sm_unit(area: int = 850, rent_lo: int | None = None) -> dict:
    """A SightMap-shaped unit dict (post post_process)."""
    return make_unit_dict(
        unit_number="UNIT", sqft=str(area),
        bedrooms="1", bathrooms="1",
        floor_plan_name="A1",
        rent_low=rent_lo,
        extraction_tier="TIER_1_API_SIGHTMAP",
    )


# ─── happy path: all units area + zero rent → flag fires ─────────────


def test_flag_applies_when_all_units_area_no_rent() -> None:
    """The wimberly/rosera pattern: ≥3 units, all with area, all
    without rent → every unit gets flagged."""
    units = [_sm_unit(area=850 + 10 * i) for i in range(5)]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    n = _flag_sightmap_units_operator_rent_gap(units, result)
    assert n == 5
    for u in units:
        assert "rent" in u["data_gaps"]
        assert u["data_quality_flag"] == "RENT_NOT_PUBLISHED"


# ─── defensive no-ops ────────────────────────────────────────────────


def test_flag_does_not_apply_when_any_unit_has_rent() -> None:
    """If even ONE unit has rent, the property publishes pricing —
    the missing-rent on others is parser-side, not operator-side.
    Bail out completely so a parser bug is still surfaced."""
    units = [
        _sm_unit(area=850, rent_lo=1500),  # has rent
        _sm_unit(area=900),  # no rent
        _sm_unit(area=950),  # no rent
    ]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    n = _flag_sightmap_units_operator_rent_gap(units, result)
    assert n == 0
    for u in units:
        assert u.get("data_gaps", []) == []


def test_flag_does_not_apply_when_unit_count_below_three() -> None:
    """≥3 units required to confirm portfolio-wide pattern; ≤2 could
    be edge cases like a single-unit listing."""
    units = [_sm_unit(area=850), _sm_unit(area=900)]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    assert _flag_sightmap_units_operator_rent_gap(units, result) == 0


def test_flag_does_not_apply_when_any_unit_missing_area() -> None:
    """area presence on EVERY unit confirms real unit-level structure.
    If any unit lacks area, the structure is suspect — bail."""
    units = [
        _sm_unit(area=850),
        _sm_unit(area=900),
        make_unit_dict(unit_number="X", bedrooms="1"),  # no area
    ]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    assert _flag_sightmap_units_operator_rent_gap(units, result) == 0


def test_flag_does_not_apply_when_empty_units_list() -> None:
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    assert _flag_sightmap_units_operator_rent_gap([], result) == 0


# ─── idempotent + non-clobbering ─────────────────────────────────────


def test_flag_idempotent_on_double_call() -> None:
    units = [_sm_unit(area=850 + 10 * i) for i in range(3)]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    _flag_sightmap_units_operator_rent_gap(units, result)
    _flag_sightmap_units_operator_rent_gap(units, result)
    # data_gaps must contain exactly one "rent" — no duplicates.
    for u in units:
        assert u["data_gaps"] == ["rent"]


def test_flag_appends_to_existing_gap_list() -> None:
    """If a unit already documents a different gap (e.g. bedrooms),
    we APPEND rather than overwrite."""
    units = [_sm_unit(area=850 + 10 * i) for i in range(3)]
    for u in units:
        u["data_gaps"] = ["bedrooms"]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    _flag_sightmap_units_operator_rent_gap(units, result)
    for u in units:
        assert set(u["data_gaps"]) == {"bedrooms", "rent"}


def test_flag_preserves_existing_quality_flag() -> None:
    """A stronger upstream flag (e.g. CARRIED_FORWARD) wins; we only
    set data_quality_flag when it's blank."""
    units = [_sm_unit(area=850 + 10 * i) for i in range(3)]
    for u in units:
        u["data_quality_flag"] = "CARRIED_FORWARD"
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    _flag_sightmap_units_operator_rent_gap(units, result)
    for u in units:
        # quality flag preserved
        assert u["data_quality_flag"] == "CARRIED_FORWARD"
        # but the gap list still records the rent gap
        assert "rent" in u["data_gaps"]


# ─── area accepted in multiple shapes ────────────────────────────────


def test_flag_accepts_area_as_numeric_or_string() -> None:
    """SightMap stamps sqft as string; tests use numeric area too.
    Both must satisfy the area-present check."""
    units = [
        _sm_unit(area=850),  # sqft="850" (string from make_unit_dict)
        {"unit_number": "x", "area": 900},  # numeric area
        {"unit_number": "y", "sqft": "1100"},  # string sqft
    ]
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    n = _flag_sightmap_units_operator_rent_gap(units, result)
    assert n == 3


# ─── _try_avalon_override_for_sightmap — Wimberly-case correctness ───


@dataclass
class _FakeFetchResult:
    body: str | bytes = ""


@dataclass
class _FakeCtx:
    """Minimal stand-in for AdapterContext — only the fields the
    override helper reads."""
    base_url: str = ""
    fetch_result: _FakeFetchResult = field(default_factory=_FakeFetchResult)


def test_avalon_override_returns_units_when_avalon_markers_present() -> None:
    """The smoking gun — user caught this on 2026-05-23. Wimberly's
    SightMap embed returns null prices, but the same homepage HTML
    has the Avalon Fusion blob with real rent. The override helper
    must extract those Avalon units."""
    # Synthetic Fusion blob — the minimum that parse_avalonbay_html
    # accepts (real Wimberly HTML at 700KB+ is overkill for unit test).
    import json
    units_json = json.dumps([
        {
            "unitId": "AVB-TX016-002-236", "unitName": "236",
            "bedroomNumber": 1, "bathroomNumber": 1, "squareFeet": 662,
            "floorPlan": {"name": "A1"},
            "startingAtPricesUnfurnished": {
                "prices": {"price": 1150, "totalPrice": 1250},
            },
        },
        {
            "unitId": "AVB-TX016-002-237", "unitName": "237",
            "bedroomNumber": 1, "bathroomNumber": 1, "squareFeet": 700,
            "floorPlan": {"name": "A1"},
            "startingAtPricesUnfurnished": {"prices": {"price": 1200}},
        },
    ])
    html = (
        '<html><head><script id="fusion-metadata">'
        'Fusion.globalContent={"units":' + units_json + '};'
        '</script></head></html>'
    )
    ctx = _FakeCtx(
        base_url="https://www.avaloncommunities.com/texas/dallas-apartments/eaves-north-dallas/",
        fetch_result=_FakeFetchResult(body=html),
    )
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    units = _try_avalon_override_for_sightmap(ctx, result)
    assert len(units) == 2
    assert units[0]["market_rent_low"] == 1150
    assert units[0]["sqft"] == "662"
    assert units[0]["unit_number"] == "236"


def test_avalon_override_returns_empty_when_no_avalon_markers() -> None:
    """For genuinely non-Avalon SightMap sites (Rosera, Decron, etc.),
    the override must NOT misfire — leaving the rent-gap flag to do
    its job as the honest-provenance fallback."""
    html = '<html><body>just a regular SightMap site, no AVB markers</body></html>'
    ctx = _FakeCtx(base_url="https://roserawesmont.com/", fetch_result=_FakeFetchResult(body=html))
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    assert _try_avalon_override_for_sightmap(ctx, result) == []


def test_avalon_override_returns_empty_when_no_fetch_body() -> None:
    """Defensive: an empty fetch_result body must not raise."""
    ctx = _FakeCtx(base_url="https://example.com/", fetch_result=_FakeFetchResult(body=""))
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    assert _try_avalon_override_for_sightmap(ctx, result) == []


# ─── _try_subpage_sightmap_with_prices (Rosera two-embed pattern) ────


def test_subpage_helper_returns_empty_when_no_base_url() -> None:
    """Defensive: no base_url on ctx → don't crash, just return []."""
    ctx = _FakeCtx(base_url="", fetch_result=_FakeFetchResult(body=""))
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    assert _try_subpage_sightmap_with_prices(ctx, result) == []


def test_subpage_helper_returns_empty_for_invalid_url() -> None:
    """A malformed base_url (no scheme/netloc) must not raise."""
    ctx = _FakeCtx(base_url="not-a-url", fetch_result=_FakeFetchResult(body=""))
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    assert _try_subpage_sightmap_with_prices(ctx, result) == []


def test_avalon_override_handles_bytes_body() -> None:
    """L1 fetch returns body as bytes for binary safety — the helper
    must decode rather than crash."""
    import json
    units_json = json.dumps([{
        "unitId": "AVB-X-1", "unitName": "1",
        "bedroomNumber": 1, "bathroomNumber": 1, "squareFeet": 600,
        "floorPlan": {"name": "A1"},
        "startingAtPricesUnfurnished": {"prices": {"price": 1500}},
    }])
    html_bytes = (
        '<script id="fusion-metadata">Fusion.globalContent={"units":'
        + units_json + '};</script>'
    ).encode("utf-8")
    ctx = _FakeCtx(
        base_url="https://avaloncommunities.com/",
        fetch_result=_FakeFetchResult(body=html_bytes),
    )
    result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
    units = _try_avalon_override_for_sightmap(ctx, result)
    assert len(units) == 1
    assert units[0]["market_rent_low"] == 1500
