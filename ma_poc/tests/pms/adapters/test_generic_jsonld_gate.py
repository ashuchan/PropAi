"""JSON-LD gate decision tests (2026-05-23).

Pins the gate logic that decides whether JSON-LD extraction "wins" or
falls through to subsequent sub-tiers. The 2026-05-23 addition rejects
JSON-LD when it has no rent AND the page has signals of a richer
PMS source (SecureCafe, RentCafe XHR, Entrata, SightMap, MAAC API).

Background: ~16 area-but-no-rent properties in the canary stamp
TIER_2_JSONLD + SUCCESS_PLAN_LEVEL because the JSON-LD tier's
existing gate accepts any output carrying sqft (even with no rent),
preventing the SecureCafe drill / Entrata API / SightMap path from
running. Verified live on mainstreetsquareapartments.com: JSON-LD
ships 27 plan-level rows (no rent), SecureCafe drill produces 22
units WITH real rent — the JSON-LD path was hiding them.
"""
from __future__ import annotations

from ma_poc.pms.adapters.generic import _jsonld_gate_decision


def _unit(rent: int | None = None, sqft: str = "", beds: str = "",
          baths: str = "", fp: str = "") -> dict:
    """Build a unit dict in the JSON-LD parser's output shape."""
    return {
        "market_rent_low": rent,
        "rent_range": f"${rent:,}" if rent else "",
        "sqft": sqft,
        "bedrooms": beds,
        "bathrooms": baths,
        "floor_plan_name": fp,
    }


# ─── happy paths: accept ─────────────────────────────────────────────


def test_gate_accepts_units_with_rent() -> None:
    """If rent is present, JSON-LD wins regardless of other fields."""
    units = [_unit(rent=1500, sqft="700", beds="1", baths="1", fp="A1")]
    assert _jsonld_gate_decision(units, "") == "accept"


def test_gate_accepts_full_plan_level_when_no_richer_pms() -> None:
    """Beds + baths + sqft + floor_plan but no rent — accept (the
    operator's site has no richer PMS source either)."""
    units = [_unit(sqft="700", beds="1", baths="1", fp="A1")]
    html = "<html><body>plain marketing site, no PMS markers</body></html>"
    assert _jsonld_gate_decision(units, html) == "accept"


def test_gate_accepts_no_html_input() -> None:
    """Empty html → can't check PMS markers, so fall back to the
    original 'name-only' rejection only. Plan-level with sqft passes."""
    units = [_unit(sqft="700", beds="1", baths="1", fp="A1")]
    assert _jsonld_gate_decision(units, "") == "accept"


# ─── rejection: name-only (existing behavior) ────────────────────────


def test_gate_rejects_pure_floor_plan_names_with_nothing_else() -> None:
    """Original gate behavior — plan_name only with no rent, no sqft,
    no beds/baths → reject."""
    units = [_unit(fp="A1"), _unit(fp="A2")]
    reason = _jsonld_gate_decision(units, "")
    assert reason != "accept"
    assert "names only" in reason


def test_gate_rejects_floor_plan_with_beds_no_rent_no_sqft() -> None:
    """2026-05-23 update: the original rule accepted floor_plan +
    beds even without rent and sqft — but in practice this stamps
    TIER_2_JSONLD and blocks the cascade from reaching the deeper
    plan-text / embedded-JSON / subpage tiers that often carry the
    missing rent+sqft. Probe sample showed 4/6 of these properties
    have full rent+sqft data one level deeper. New: reject so the
    cascade can keep searching."""
    units = [_unit(fp="A1", beds="1")]
    reason = _jsonld_gate_decision(units, "")
    assert reason != "accept"
    assert "neither rent nor sqft" in reason


def test_gate_accepts_floor_plan_beds_with_sqft() -> None:
    """When sqft IS present, the JSON-LD wins (sqft alone is a
    valuable signal even without rent)."""
    units = [_unit(fp="A1", beds="1", sqft="700")]
    assert _jsonld_gate_decision(units, "") == "accept"


# ─── new rejection: no-rent + richer PMS present ─────────────────────


def test_gate_rejects_no_rent_when_securecafe_present() -> None:
    """Main Street Square pattern: JSON-LD has plan rows with sqft +
    beds + baths but no rent. Page has SecureCafe iframe → reject so
    the SC drill can run."""
    units = [_unit(sqft="700", beds="1", baths="1", fp="A1") for _ in range(5)]
    html = (
        '<html><body><iframe src="https://mainsquare.securecafe.com/'
        'onlineleasing/main/availableunits.aspx"></iframe></body></html>'
    )
    reason = _jsonld_gate_decision(units, html)
    assert reason != "accept"
    assert "richer PMS source" in reason


def test_gate_rejects_no_rent_when_rentcafe_xhr_present() -> None:
    units = [_unit(sqft="700", beds="1") for _ in range(3)]
    html = '<script src="https://www.rentcafe.com/edge/v1/something.js"></script>'
    assert _jsonld_gate_decision(units, html) != "accept"


def test_gate_rejects_no_rent_when_entrata_present() -> None:
    units = [_unit(sqft="700", beds="1") for _ in range(3)]
    html = '<script src="https://something.entrata.com/widgets/foo.js"></script>'
    assert _jsonld_gate_decision(units, html) != "accept"


def test_gate_rejects_no_rent_when_sightmap_present() -> None:
    units = [_unit(sqft="700", beds="1") for _ in range(3)]
    html = '<iframe src="https://sightmap.com/embed/abc123"></iframe>'
    assert _jsonld_gate_decision(units, html) != "accept"


def test_gate_rejects_no_rent_when_maac_api_present() -> None:
    units = [_unit(sqft="700", beds="1") for _ in range(3)]
    html = '<script>fetch("https://www.maac.com/api/properties/X/units/available/")</script>'
    assert _jsonld_gate_decision(units, html) != "accept"


# ─── defensive: no-rent + NO richer PMS → still accept ───────────────


def test_gate_accepts_no_rent_when_only_marketing_signals() -> None:
    """A site with sqft+beds but no rent AND no PMS adapter signals
    is a genuine operator data gap. JSON-LD is the best we'll get —
    don't reject (the cascade would just LLM and probably not do
    better)."""
    units = [_unit(sqft="700", beds="1", baths="1", fp="A1") for _ in range(5)]
    html = '<html><body>just some marketing content, no PMS markers</body></html>'
    assert _jsonld_gate_decision(units, html) == "accept"


def test_gate_accepts_rent_present_even_with_richer_pms() -> None:
    """If JSON-LD HAS rent, accept regardless of richer-PMS markers.
    No need to fall through when we already have what we need."""
    units = [_unit(rent=1500, sqft="700")]
    html = '<iframe src="https://x.securecafe.com/onlineleasing/foo/availableunits.aspx"></iframe>'
    assert _jsonld_gate_decision(units, html) == "accept"


# ─── edge cases ──────────────────────────────────────────────────────


def test_gate_returns_no_units_for_empty_list() -> None:
    assert _jsonld_gate_decision([], "") == "no_units"


def test_gate_case_insensitive_pms_markers() -> None:
    """PMS markers in mixed/upper case in raw HTML must still match
    (the gate normalizes to lower)."""
    units = [_unit(sqft="700", beds="1") for _ in range(3)]
    html = '<iframe src="https://X.SECURECAFE.com/onlineleasing/foo/"></iframe>'
    assert _jsonld_gate_decision(units, html) != "accept"
