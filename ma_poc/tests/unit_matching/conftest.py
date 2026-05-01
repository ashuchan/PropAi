"""
Shared fixtures for unit matching TDD suite.
All fixtures are pure Python — no database, no Playwright, no network.
"""
import hashlib
import pathlib
import sys
import tempfile
import types
from typing import Generator

import pytest

# Ensure scripts/ is on sys.path so `from state_store import StateStore` etc. work.
_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# Stub out playwright so scrape_properties (which imports entrata → playwright)
# can be imported in unit tests without a real browser installed.
def _stub_playwright() -> None:
    if "playwright" in sys.modules:
        return
    pw = types.ModuleType("playwright")
    pw_api = types.ModuleType("playwright.async_api")
    for attr in ("BrowserContext", "Page", "async_playwright", "Playwright",
                 "Browser", "BrowserType", "ElementHandle", "Response",
                 "Request", "Route", "Frame", "Error"):
        setattr(pw_api, attr, object)
    sys.modules["playwright"] = pw
    sys.modules["playwright.async_api"] = pw_api

_stub_playwright()

from state_store import StateStore  # noqa: E402


# ── Date constants ────────────────────────────────────────────────────────────
D1 = "2026-04-25"
D2 = "2026-04-26"
D3 = "2026-04-27"
D4 = "2026-04-28"
CID = "prop_test_001"
CID2 = "prop_test_002"


# ── StateStore fixture ────────────────────────────────────────────────────────
@pytest.fixture
def store() -> Generator[StateStore, None, None]:
    """Fresh in-memory StateStore backed by a temp directory."""
    with tempfile.TemporaryDirectory() as d:
        s = StateStore(pathlib.Path(d))
        s.load()
        yield s


# ── Unit dict builders ────────────────────────────────────────────────────────
def make_api_unit(
    unit_id: str = "101",
    rent_lo: float = 1800.0,
    rent_hi: float = 1800.0,
    avail_date: str = D1,
    floor_plan: str = "Plan A",
    beds: int = 2,
    baths: float = 1.0,
    sqft: int = 950,
) -> dict:
    """
    Unit dict as it arrives from an API-level parser BEFORE _ stripping.
    This is what upsert_units will receive after Phase 3 fix
    (strip moves to after upsert_units).
    """
    return {
        "unit_id": unit_id,
        "market_rent_low": rent_lo,
        "market_rent_high": rent_hi,
        "available_date": avail_date,
        "lease_link": None,
        "concessions": None,
        "amenities": None,
        "_sqft": sqft,
        "_floor_plan": floor_plan,
        "_bedrooms": beds,
        "_bathrooms": baths,
    }


def make_no_id_unit(
    floor_plan: str = "Plan A",
    beds: int = 2,
    baths: float = 1.0,
    sqft: int = 950,
    rent_lo: float = 1800.0,
    avail_date: str = D1,
) -> dict:
    """
    Unit dict with NO unit_id — simulates floor-plan-level API or
    entrata fallback without unit_number.
    _sqft/_floor_plan/_bedrooms present (pre-strip).
    """
    return {
        "unit_id": "",
        "market_rent_low": rent_lo,
        "market_rent_high": rent_lo,
        "available_date": avail_date,
        "lease_link": None,
        "concessions": None,
        "amenities": None,
        "_sqft": sqft,
        "_floor_plan": floor_plan,
        "_bedrooms": beds,
        "_bathrooms": baths,
    }


def make_stripped_unit(
    unit_id: str = "101",
    rent_lo: float = 1800.0,
    rent_hi: float = 1800.0,
    avail_date: str = D1,
) -> dict:
    """
    Unit dict AFTER _ stripping — simulates current broken behaviour
    where _ fields are removed before upsert_units.
    Used in Phase 2 to prove that stripping breaks physical attribute storage.
    """
    return {
        "unit_id": unit_id,
        "market_rent_low": rent_lo,
        "market_rent_high": rent_hi,
        "available_date": avail_date,
        "lease_link": None,
        "concessions": None,
        "amenities": None,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────
def stable_fallback_id(property_id: str, floor_plan: str, beds, baths, sqft) -> str:
    """
    Compute the expected stable fallback unit_id for a unit without unit_number.
    Must match the implementation in identity_fallback.compute_fallback_unit_id
    AFTER Phase 1 fix (rent and available_date excluded).
    """
    def _n(v):
        if v is None or v == -1:
            return ""
        return str(v).strip().lower()

    sqft_rounded = ""
    if sqft and sqft != -1:
        try:
            a = int(float(str(sqft)))
            sqft_rounded = str(round(a / 10) * 10) if a > 0 else ""
        except (ValueError, TypeError):
            sqft_rounded = ""

    parts = [
        property_id or "",
        _n(floor_plan),
        _n(beds),
        _n(baths),
        sqft_rounded,
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"inferred_{digest}"
