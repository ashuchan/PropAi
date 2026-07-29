"""AppFolio must not put a street ADDRESS in ``floor_plan_name``.

Regression cover for the 2026-07-28 finding on
``run-2026-07-27-full-0d54ca7``: 11,877 of 103,246 named unit rows carried
a full street address in ``floor_plan_name`` instead of a plan name —

    TIER_1_DOM_APPFOLIO_SSR              11,123
    TIER_1_DOM_APPFOLIO_VANITY              734
    TIER_1_DOM_APPFOLIO_VANITY_PLAN_LEVEL    20

e.g. ``'7090 Constitution Square Heights - 7-101, Colorado Springs, CO
80915'``. In 100% of those rows the SAME string was already present in
``unit_name``, so the plan-name column carried nothing the address column
did not.

Root cause: ``parse_appfolio_listings_ssr`` (shared by the SSR *and*
VANITY tiers) assigned the listing card's ``js-listing-address`` text to
``floor_plan_name``. AppFolio's listing card publishes no plan name at
all — verified live 2026-07-28 (curl_cffi impersonate=chrome) against
olympicmanagement (300 cards), americancapitalrealty (147) and pagewood
(3). Every card exposes exactly:

    js-listing-blurb-rent · js-listing-blurb-bed-bath ("2 bd / 1 ba")
    js-listing-square-feet · js-listing-available · js-listing-address

with detail-box labels RENT / Square Feet / Bed / Bath / Available. The
only other candidate, ``js-listing-title``, is free-text marketing copy
("Welcome Home to Talise", "Absolutely Gorgeous, Fully Furnished 2x2!")
and sometimes a bare property name ("Enclave at Arrowhead") — not a plan
name.

So the correct behaviour is: leave ``floor_plan_name`` EMPTY and let the
address live in ``unit_name``. The bed/bath descriptor is the only
plan-like signal AppFolio publishes and it is already captured in
bedrooms/bathrooms (which feed ``compute_floor_plan_id`` downstream).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ma_poc.pms.adapters._appfolio_websites_duda import (
    parse_appfolio_websites_listing,
)
from ma_poc.pms.adapters.appfolio import (
    parse_appfolio_detail_page,
    parse_appfolio_listings_ssr,
)

# Captured live 2026-07-28 from https://pagewood.appfolio.com/listings
# (curl_cffi impersonate=chrome). Card markup is byte-identical to the
# live response; only the surrounding chrome was trimmed.
_FIXTURE = (
    Path(__file__).parent / "fixtures" / "appfolio_ssr" / "pagewood_listings.html"
)

# Matches "…, TX 77042" — a US state + ZIP tail, the shape used to count
# the 11,877 affected rows in the run corpus.
_ADDRESS_TAIL_RE = re.compile(r",\s*[A-Z]{2}\s+\d{5}")


def _units() -> list[dict[str, object]]:
    return parse_appfolio_listings_ssr(
        _FIXTURE.read_text(), "https://pagewood.appfolio.com/listings"
    )


def test_ssr_never_puts_an_address_in_floor_plan_name() -> None:
    """The headline invariant: no SSR row may ship an address as a plan name."""
    units = _units()
    assert units, "fixture must yield units or this test proves nothing"

    offenders = [
        u for u in units if _ADDRESS_TAIL_RE.search(str(u.get("floor_plan_name") or ""))
    ]
    assert not offenders, (
        "floor_plan_name must never hold a street address; got "
        f"{[u['floor_plan_name'] for u in offenders]!r}"
    )


def test_ssr_leaves_floor_plan_name_empty() -> None:
    """AppFolio SSR publishes no plan name, so the field stays empty.

    Guards the weaker failure mode too: substituting the synthetic
    ``AppFolio listing {id}`` placeholder, or the ``js-listing-title``
    marketing string, would also be wrong.
    """
    for u in _units():
        assert u.get("floor_plan_name") == "", (
            f"expected empty floor_plan_name, got {u.get('floor_plan_name')!r}"
        )


def test_ssr_address_is_preserved_in_unit_name() -> None:
    """Blanking the plan name must not LOSE the address — it moves fields.

    This is the other half of the fix: a change that simply dropped the
    address would pass the invariant above while destroying data.
    """
    units = _units()
    with_address = [
        u for u in units if _ADDRESS_TAIL_RE.search(str(u.get("unit_name") or ""))
    ]
    assert len(with_address) >= 2, (
        "the fixture's addressed cards must surface their address in "
        f"unit_name; got {[u.get('unit_name') for u in units]!r}"
    )
    assert "9767 Pagewood Lane #216, Houston, TX 77042" in {
        u.get("unit_name") for u in units
    }


def test_ssr_keeps_unit_identity_and_dimensions() -> None:
    """Zero-row-loss + identity guard.

    The address-derived ``unit_id`` is computed from the address itself,
    not from ``floor_plan_name``, so it must be byte-identical after the
    fix. Every row must also retain a substantive field — ``schema_gate``
    counts ``floor_plan_name`` among SUBSTANTIVE_FIELDS, so a row whose
    only content was the address would otherwise be dropped.
    """
    units = _units()
    assert len(units) == 3, f"row count changed: {len(units)}"

    assert "9767-pagewood-lane-216-houston-tx-77042" in {
        u.get("unit_id") for u in units
    }

    for u in units:
        assert any(
            [
                str(u.get("bedrooms") or ""),
                u.get("market_rent_low") is not None,
                str(u.get("sqft") or ""),
            ]
        ), f"row lost every substantive field: {u!r}"


def test_ssr_bed_label_ignores_street_names_containing_studio() -> None:
    """``bed_label`` must come from beds, not from the address text.

    ``bed_label_from`` tests its name argument for the substring "studio"
    BEFORE the numeric arm, so feeding it the address made
    "4419 Ludlow St - Standard Studio, Philadelphia, PA 19104" return
    "Studio" for a beds=1 unit (2 such rows in the 07-27 run).
    """
    html = """
    <article class="listing-item js-listing-item" data-listing-id="77">
      <div class="js-listing-blurb-rent">$1,500</div>
      <div class="js-listing-blurb-bed-bath">1 bd / 1 ba</div>
      <div class="js-listing-square-feet">Square Feet: 600</div>
      <span class="js-listing-address">4419 Ludlow St - Standard Studio, Philadelphia, PA 19104</span>
    </article>
    """
    units = parse_appfolio_listings_ssr(html, "https://x.appfolio.com/listings")
    assert len(units) == 1
    assert units[0]["bed_label"] == "1 Bedroom", (
        "a street name containing 'Studio' must not override beds=1"
    )


@pytest.mark.parametrize(
    ("h1", "expect_plan", "expect_unit_name"),
    [
        # Verified live 2026-07-28: the detail-page h1 renders the address.
        (
            "9767 Pagewood Lane #710, Houston, TX 77042",
            "",
            "9767 Pagewood Lane #710, Houston, TX 77042",
        ),
        # A genuine plan label must survive — the detail h1 is a TITLE and
        # is genuinely ambiguous, so it is routed on shape, not blanked.
        ("Gage Crossing — 2BR Modern", "Gage Crossing — 2BR Modern", ""),
    ],
)
def test_detail_page_routes_h1_on_address_shape(
    h1: str, expect_plan: str, expect_unit_name: str
) -> None:
    """Address-shaped detail titles go to unit_name; real plan names stay."""
    html = f"""
    <html><body><main>
      <h1>{h1}</h1>
      <div class="rent-block">$2,150 / month</div>
      <div class="specs">2 bd / 2 ba</div>
      <div class="sqft">1,150 sqft</div>
    </main></body></html>
    """
    units = parse_appfolio_detail_page(
        html, "https://x.appfolio.com/listings/detail/abc-123"
    )
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == expect_plan
    assert units[0]["unit_name"] == expect_unit_name


def test_duda_prefers_real_plan_name_and_never_falls_back_to_address() -> None:
    """The Duda collection path has the same defect shape.

    ``unit_template_name`` is AppFolio's real plan label; ``full_address``
    and ``marketing_title`` were chained behind it as fallbacks, so a unit
    with no template name got an address as its plan name.
    """
    # Real plan name published → it wins, address still captured.
    unit = parse_appfolio_websites_listing(
        {
            "data": {
                "unit_template_name": "The Ashlawn Deluxe",
                "full_address": "250 Colonnade Dr, Charlottesville, VA 22903",
                "bedrooms": 1,
                "bathrooms": 1,
                "market_rent": 1365,
                "square_feet": 710,
            }
        },
        "https://example.com/collection",
    )
    assert unit is not None
    assert unit["floor_plan_name"] == "The Ashlawn Deluxe"
    assert unit["unit_name"] == "250 Colonnade Dr, Charlottesville, VA 22903"

    # No plan name → EMPTY, not the address and not the marketing title.
    unit2 = parse_appfolio_websites_listing(
        {
            "data": {
                "full_address": "250 Colonnade Dr, Charlottesville, VA 22903",
                "marketing_title": "Welcome Home to Talise!",
                "bedrooms": 2,
                "bathrooms": 2,
                "market_rent": 1800,
                "square_feet": 950,
            }
        },
        "https://example.com/collection",
    )
    assert unit2 is not None
    assert unit2["floor_plan_name"] == "", (
        f"expected empty, got {unit2['floor_plan_name']!r}"
    )
    assert unit2["unit_name"] == "250 Colonnade Dr, Charlottesville, VA 22903"
