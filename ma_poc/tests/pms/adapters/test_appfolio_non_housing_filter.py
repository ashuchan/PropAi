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
    # 2026-07-29 — the SSR call site no longer passes the card body here.
    # The keyword regex itself is unchanged and still fires on any text it
    # is handed; the contract is now about WHICH text it is handed. See
    # test_ssr_card_drop_table below for the arm-level table.
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


def test_listings_ssr_garage_in_body_is_kept() -> None:
    """An apartment whose BODY mentions a garage is a real apartment.

    Was ``test_listings_ssr_garage_in_body_is_filtered``, which pinned the
    opposite and carried its own exit condition: "If at scale this drops too
    many real apartments, tighten to scan address only." Measured live
    2026-07-29 across 65 tenants ({slug}.appfolio.com/listings, plain static
    GET, curl_cffi impersonate=chrome, 65/65 HTTP 200): the body arm dropped
    2,352 of 8,430 card containers, 2,094 of which carry a bed/bath blurb AND
    rent >= $500, while the address arm dropped 1. The exit condition fired.

    This assertion is strictly stronger than the one it replaces: the row must
    survive AND its fields must come through.
    """
    html = """
    <div data-listing-id="300" class="js-listing-card">
        <div class="js-listing-blurb-rent">$1,800</div>
        <div class="js-listing-blurb-bed-bath">2 bd / 2 ba</div>
        <div class="js-listing-square-feet">1100</div>
        <div class="js-listing-address">123 Main St Apt 5</div>
        <p>Beautiful unit with attached garage</p>
    </div>
    <footer>cutoff</footer>
    """
    units = parse_appfolio_listings_ssr(html, "x")
    assert len(units) == 1
    assert units[0]["rent_range"]
    assert units[0]["bedrooms"] == "2"
    assert units[0]["sqft"] == "1100"


# ─────────────────────────────────────────────────────────────────────
# Arm table — which text field may trigger the drop
#
# Every "must NOT drop" body string below is verbatim live copy captured
# 2026-07-29 from {slug}.appfolio.com/listings by plain unauthenticated
# static GET (curl_cffi impersonate=chrome). Tenant named per row.
# Every "must drop" address is a real non-housing listing label.
# ─────────────────────────────────────────────────────────────────────

_KEEP = False   # expected_dropped
_DROP = True

_SSR_CARD_TABLE = [
    # ── must NOT drop: ordinary apartments whose own copy names a
    #    parking / storage / garage / bike amenity ───────────────────
    ("richelsonmanagement: 'lots of storage space'", "834 S Cliffs Circle, Apt 201, Spring Lake, NC 28390",
     "Also includes lots of storage space with multiple closets through the apartment!", _KEEP),
    ("becovic: amenity list names a Bike Room", "7528 N Seeley Ave #B4, Chicago, IL 60645",
     "Amenities: Fitness Center, Bike Room, BBQ Stone Patio Seating with Grill, Elevator Building", _KEEP),
    ("wdcproperties: amenity list names Bike Storage", "1600 SE Lava Dr. - 103, Milwaukie, OR 97222",
     "Amenities: Bike Storage, Granite Countertops, Stainless Steel Appliances, Balcony, Game Room", _KEEP),
    ("vdbprop: amenity list names Covered Carport", "2409 Branch Creek Circle #3, Paso Robles, CA 93446",
     "Amenities: dishwasher, Covered Carport, Gas Water Heaters, Mini Blinds, Vertical Blinds", _KEEP),
    ("fairgrove: amenity list names Assigned Car Port", "755 Gaviota Ave - 06, Long Beach, CA 90813",
     "Amenities: 24 Hour On-Site Laundry, Gated Community, Wall Heater, Assigned Car Port", _KEEP),
    ("concordemgmt: 'off-street parking' in description", "6041 Cornhusker Hwy - 01, Lincoln, NE 68507",
     "These apartments feature controlled access entry, all electric units, elevator in"
     " building, and off-street parking.", _KEEP),
    ("investorsmgmt: 'underground parking' in description", "505-511 36th Ave. NE, Minot, ND 58703",
     "Badlands Apartments feature a modern interior design with great amenities like"
     " underground parking, elevators and in-unit laundry.", _KEEP),
    ("brunerrealty: free parking stall in description", "4320 North Towne Court, Unit 104, Windsor, WI 53598",
     "Apartments come equipped with dishwasher, in-unit laundry, and one free off street,"
     " underground, heated parking stall.", _KEEP),
    ("vdbprop: amenity list is only 'Detached Garage'", "11215 North Alicante Drive # 10-306, Fresno, CA 93730",
     "Amenities: Detached Garage Pet Policy: Cats allowed, Dogs allowed", _KEEP),
    ("cathcartres: marketing headline names Attached Garage", "2015 Reserve Circle, Harrisonburg, VA 22801",
     "The Blue Ridge is an 825 sq. ft. one-bedroom apartment home featuring the added"
     " convenience of an attached garage.", _KEEP),
    ("blackrealtymanagement: concession waives carport fee", "1620 N River Ridge Blvd, Spokane, WA 99224",
     "FREE RENT! WAIVING ALL PET, AND CARPORT FEES! - Beautiful Studios and One-Bedrooms", _KEEP),
    ("plain apartment, no keyword anywhere", "123 Main St Apt 5, Springfield, IL 62701",
     "In-unit washer and dryer, quartz countertops, private balcony.", _KEEP),

    # ── must drop: the listing's own ADDRESS says what it is ────────
    ("gmholdings: live 2026-07-29 storage roster row", "2001-15 E Glenwood Ave - Storage Units, Philadelphia, PA 19134",
     "Coming Soon Sea Container Storage Units! These secure units offer ample lighting.", _DROP),
    ("canary pid 54745 non-resident parking", "1919 14th Street, NW - Non-Resident Parking 05, Washington, DC 20009",
     "Assigned space.", _DROP),
    ("parking space by address", "555 Main St - Parking Space A12, Chicago, IL 60626",
     "Reserved outdoor space.", _DROP),
    ("garage bay by address", "88 Elm Ave - Garage Bay 5, Portland, OR 97203",
     "Overhead door, concrete floor.", _DROP),
    ("storage locker by address", "12 Oak Blvd - Locker 14B, Spokane, WA 99201",
     "Ground floor, keyed.", _DROP),
    ("carport by address", "400 Pine St - Carport 12, Long Beach, CA 90813",
     "Covered, assigned.", _DROP),
]


@pytest.mark.parametrize(
    "label,address,body_text,expected_dropped",
    [(r[0], r[1], r[2], r[3]) for r in _SSR_CARD_TABLE],
    ids=[r[0] for r in _SSR_CARD_TABLE],
)
def test_ssr_card_drop_table(
    label: str, address: str, body_text: str, expected_dropped: bool
) -> None:
    """One card per parse — the drop decision must key off the address only.

    One card per HTML document on purpose: with two cards the SSR block regex
    splices each card's tail onto the next card's head (tracked separately as
    the SSR field-pairing bug), which would make this table ambiguous.
    """
    html = f"""
    <div data-listing-id="4242" class="js-listing-card">
        <div class="js-listing-blurb-rent">$1,800</div>
        <div class="js-listing-blurb-bed-bath">2 bd / 2 ba</div>
        <div class="js-listing-square-feet">Square Feet: 1,100</div>
        <div class="js-listing-address">{address}</div>
        <p class="js-listing-description">{body_text}</p>
    </div>
    <footer>cutoff</footer>
    """
    units = parse_appfolio_listings_ssr(html, "https://x.test/listings")
    dropped = units == []
    assert dropped is expected_dropped, (
        f"{label}: address={address!r} body={body_text!r} -> "
        f"{'dropped' if dropped else 'kept'}, expected "
        f"{'dropped' if expected_dropped else 'kept'}"
    )


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
