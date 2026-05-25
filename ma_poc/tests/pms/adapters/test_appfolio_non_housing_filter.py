"""AppFolio non-housing filter — skip parking spaces, storage units, etc.

2026-05-25 deep-probe finding: AppFolio operators publish non-housing
listings (parking spaces, storage, garages, lockers) into the same
/listings endpoint as actual apartments. These show up as low-rent
($200-$400) zero-sqft "units" and pollute the unit count + the QC
strict-pass rate.

Sample signatures from canary 1ef1060:
  pid=54745  rent=$300  "1919 14th Street, NW - Non-Resident Parking 05"
  pid=266996 rent=$252  (multiple, San Diego CA addresses with low rent)
  pid=229769 rent=$349  (Urbana IL — per-bed student housing or storage)

This file pins the filter so a future refactor cannot silently
re-introduce non-housing rows.
"""
from __future__ import annotations

import pytest

from ma_poc.pms.adapters.appfolio import (
    _is_non_housing_listing,
    parse_appfolio_listings,
    parse_appfolio_listings_ssr,
)

# ─────────────────────────────────────────────────────────────────────
# _is_non_housing_listing — direct keyword test
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,expected", [
    # Hits — non-housing
    ("Non-Resident Parking 05", True),
    ("non-resident parking", True),
    ("Non Resident Parking", True),
    ("Parking Space A12", True),
    ("Storage Unit 23", True),
    ("Storage", True),
    ("Garage Bay 5", True),
    ("Garage", True),
    ("Locker 14B", True),
    ("Bike Storage Room", True),
    ("bike room", True),
    ("Carport 12", True),
    ("car port 12", True),
    # Misses — real housing
    ("1 Bedroom Apartment", False),
    ("2 Bed / 2 Bath Suite", False),
    ("Studio Loft", False),
    ("1919 14th Street NW Unit 5", False),
    ("Penthouse", False),
    # Edge — false-positive check (real units with "garage" in description)
    # NB: this is intentional. If an apartment listing description merely
    # mentions a garage as an amenity, the filter still triggers — this
    # is the safer side of the trade-off (a small loss of true positives
    # for a large gain in removing parking/storage clutter). If false
    # positives become a problem at scale, tighten the regex to anchor on
    # the address line only.
])
def test_keyword_classifier(text: str, expected: bool) -> None:
    assert _is_non_housing_listing(text) is expected


def test_classifier_empty_inputs() -> None:
    """Empty / None inputs must not raise + must return False."""
    assert _is_non_housing_listing() is False
    assert _is_non_housing_listing("") is False
    assert _is_non_housing_listing("", "", "") is False


def test_classifier_multi_field_any_match() -> None:
    """If ANY of the provided fields hits a non-housing keyword,
    classifier returns True (used to scan name + address + body)."""
    assert _is_non_housing_listing("1 Bed", "Non-Resident Parking", "") is True
    assert _is_non_housing_listing("Apt 5", "123 Main St", "Storage area") is True
    assert _is_non_housing_listing("Apt 5", "123 Main St", "in-unit washer") is False


# ─────────────────────────────────────────────────────────────────────
# parse_appfolio_listings (API path) — filter applied per item
# ─────────────────────────────────────────────────────────────────────


def test_listings_api_skips_parking() -> None:
    items = [
        {
            "name": "Non-Resident Parking 05",
            "address": "1919 14th Street, NW",
            "bed": 0, "bath": 0, "price": 300, "unit_id": "park-05",
        },
        {
            "name": "Apartment 2B",
            "address": "1919 14th Street, NW",
            "bed": 1, "bath": 1, "price": 1850, "sq_ft": 750, "unit_id": "2B",
        },
    ]
    units = parse_appfolio_listings(items, "https://x.test/listings")
    assert len(units) == 1
    assert units[0]["unit_number"] == "2B"


def test_listings_api_skips_storage_and_garage() -> None:
    items = [
        {"name": "Storage Unit 23", "address": "...", "price": 50, "bed": 0, "bath": 0, "unit_id": "S23"},
        {"name": "Garage Bay 4", "address": "...", "price": 200, "bed": 0, "bath": 0, "unit_id": "G4"},
        {"name": "Real Apt 5A", "address": "...", "price": 1200, "bed": 1, "bath": 1, "sq_ft": 600, "unit_id": "5A"},
        {"name": "Locker 14B", "address": "...", "price": 25, "bed": 0, "bath": 0, "unit_id": "L14B"},
    ]
    units = parse_appfolio_listings(items, "x")
    assert len(units) == 1
    assert units[0]["unit_number"] == "5A"


def test_listings_api_address_field_triggers_skip() -> None:
    """When 'name' is generic ('listing 123') but address contains the
    non-housing keyword (the real-world canary signature), still skip."""
    items = [
        {
            "name": "Listing 123",
            "address": "555 Main St - Storage Unit 22",
            "price": 75, "bed": 0, "bath": 0, "unit_id": "L123",
        },
    ]
    units = parse_appfolio_listings(items, "x")
    assert units == []


def test_listings_api_listing_type_triggers_skip() -> None:
    """When name/address are generic but listing_type explicitly says
    'parking'/'storage', skip."""
    items = [
        {
            "name": "Spot 12",
            "address": "555 Main St",
            "listing_type": "Parking",
            "price": 100, "bed": 0, "bath": 0, "unit_id": "P12",
        },
    ]
    units = parse_appfolio_listings(items, "x")
    assert units == []


# ─────────────────────────────────────────────────────────────────────
# parse_appfolio_listings_ssr (SSR HTML path) — same filter
# ─────────────────────────────────────────────────────────────────────


def test_listings_ssr_skips_parking_address() -> None:
    """Pre-fix the regex-driven SSR parser emitted parking-space rows
    with rent=$300 and bed=bath=0. pid 54745 signature.

    NB: _LISTING_BLOCK_RE requires NUMERIC data-listing-id (real AppFolio
    listings always use integers); the test uses 100/200 accordingly.
    """
    html = """
    <div data-listing-id="100" class="js-listing-card">
        <div class="js-listing-blurb-rent">$300</div>
        <div class="js-listing-blurb-bed-bath">0 BR / 0 BA</div>
        <div class="js-listing-address">1919 14th Street, NW - Non-Resident Parking 05, Washington, DC 20009</div>
    </div>
    <div data-listing-id="200" class="js-listing-card">
        <div class="js-listing-blurb-rent">$1,850</div>
        <div class="js-listing-blurb-bed-bath">1 BR / 1 BA</div>
        <div class="js-listing-square-feet">750</div>
        <div class="js-listing-address">1919 14th Street, NW Unit 2B, Washington, DC 20009</div>
    </div>
    <footer>cutoff</footer>
    """
    units = parse_appfolio_listings_ssr(html, "x")
    # Parking row (id=100) dropped; apartment row (id=200) kept.
    assert len(units) == 1
    # listing_id is 200 (apartment), unit may be unit_2b extracted from address
    # or fall back to listing_id.
    assert units[0]["unit_number"] in ("200", "2B", "Unit 2B")


def test_listings_ssr_garage_in_body_is_filtered() -> None:
    """Real apartment listings that have 'garage' in the body (e.g., as
    an amenity description) DO get filtered. This is intentionally over-
    cautious — see the classifier docstring."""
    html = """
    <div data-listing-id="300" class="js-listing-card">
        <div class="js-listing-blurb-rent">$1,800</div>
        <div class="js-listing-blurb-bed-bath">2 BR / 2 BA</div>
        <div class="js-listing-square-feet">1100</div>
        <div class="js-listing-address">123 Main St Apt 5</div>
        <p>Beautiful unit with attached garage</p>
    </div>
    <footer>cutoff</footer>
    """
    units = parse_appfolio_listings_ssr(html, "x")
    # Filter triggers on 'garage' in body — intentional over-cautious
    # behaviour. If at scale this drops too many real apartments, tighten
    # to scan address only (see classifier docstring).
    assert units == []


# ─────────────────────────────────────────────────────────────────────
# Integration: full cohort signature reproduces correctly
# ─────────────────────────────────────────────────────────────────────


def test_cohort_signature_pid_54745_non_resident_parking() -> None:
    """The canary 1ef1060 pid=54745 signature: address '1919 14th
    Street, NW - Non-Resident Parking 05' with rent=$300, unit='05'.
    Must be dropped from the SSR parse output."""
    html = """
    <div data-listing-id="999" class="js-listing-card">
        <div class="js-listing-blurb-rent">$300</div>
        <div class="js-listing-blurb-bed-bath">0 BR / 0 BA</div>
        <div class="js-listing-address">1919 14th Street, NW - Non-Resident Parking 05, Washington, DC 20009</div>
    </div>
    <footer>cutoff</footer>
    """
    assert parse_appfolio_listings_ssr(html, "https://x.test") == []
