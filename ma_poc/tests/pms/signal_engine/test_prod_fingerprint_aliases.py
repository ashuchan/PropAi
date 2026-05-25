"""FIELD_ALIASES additions from the 2026-05-24 prod-fingerprint audit.

Prod's ``scrape_profiles.api_hints.field_patches`` carry LLM-discovered
field-name mappings from real runtime responses. These are direct
evidence of vendor-specific Pascal-case / snake_case variants that the
generic walker missed. This test pins the three additions we ported
from prod (commit follow-up to PROD_FINGERPRINT_LEARNINGS.md):

  * Spherexx /api/unit:                 PriceMin/PriceMax → min_rent/max_rent
  * Repli360 admin endpoint:            customlink → unit_id
  * Pascal-case PriceMinimum/Maximum:   → min_rent/max_rent

Each affects a small but real cohort:
  PriceMin/PriceMax  → ~7 Eagle Rock + Chamberlain + Invitational props
  customlink         → ~17 Olympus + Velo + George + Parc props
"""
from __future__ import annotations

import pytest

from ma_poc.pms.signal_engine.floor_plan_signals import normalize_field_key


@pytest.mark.parametrize("raw, expected", [
    # Spherexx /api/unit Pascal-case variants
    ("PriceMin", "min_rent"),
    ("PRICEMIN", "min_rent"),
    ("pricemin", "min_rent"),
    ("PriceMax", "max_rent"),
    ("pricemax", "max_rent"),
    ("PriceMinimum", "min_rent"),
    ("PriceMaximum", "max_rent"),
    # Repli360 admin endpoint per-unit identifier
    ("customlink", "unit_id"),
    ("CustomLink", "unit_id"),  # normalize_field_key lowercases
    ("CUSTOMLINK", "unit_id"),
    # Already-aliased Pascal-case "Bed" should still map to bedrooms
    # (covered by the existing "bed" → "bedrooms" alias)
    ("Bed", "bedrooms"),
    ("BED", "bedrooms"),
    # Standard fields should pass through unchanged
    ("price", "price"),       # not aliased; passthrough
    ("unit_number", "unit_number"),
])
def test_prod_field_aliases(raw: str, expected: str) -> None:
    assert normalize_field_key(raw) == expected


def test_pricemin_pricemax_distinct_from_unaliased_rent_keys() -> None:
    """PriceMin/PriceMax MUST map to min_rent/max_rent (not generic 'rent')
    so the rent-range gets preserved in downstream cross-page merging.
    The existing 'minrent' / 'maxrent' aliases also map this way; the
    new entries just cover the Pascal-case Spherexx shape."""
    assert normalize_field_key("PriceMin") == "min_rent"
    assert normalize_field_key("PriceMax") == "max_rent"
    # Sanity: the existing aliases still work
    assert normalize_field_key("MinRent") == "min_rent"
    assert normalize_field_key("maxrent") == "max_rent"


# ─────────────────────────────────────────────────────────────────────
# Integration: parse_api_responses end-to-end with the prod-fingerprint
# shapes. Synthesizes the response bodies the LLM saw and confirms our
# walker now picks up all 5 of unit_number + rent + beds + baths +
# avail_date deterministically.
# ─────────────────────────────────────────────────────────────────────


def test_parse_api_responses_handles_spherexx_api_unit_shape() -> None:
    """Spherexx /api/unit ships Pascal-case fields:
    Units / Number / Bed / Bath / PriceMin / PriceMax / SqFt / AvailableDate.
    Affects ~7 not-full props (Eagle Rock × 3, Chamberlain, Invitational).
    """
    from ma_poc.pms.adapters._api_parser import parse_api_responses

    body = {
        "Units": [
            {
                "Number": "203A",
                "Bed": 2, "Bath": 1,
                "PriceMin": 1495, "PriceMax": 1495,
                "FloorPlan": "B1", "SqFt": 1050,
                "AvailableDate": "2026-06-15",
            },
            {
                "Number": "508",
                "Bed": 1, "Bath": 1,
                "PriceMin": 1295, "PriceMax": 1395,
                "FloorPlan": "A1", "SqFt": 750,
                "AvailableDate": "2026-07-01",
            },
        ]
    }
    units = parse_api_responses(
        [{"url": "https://presentation.spherexx.app/api/unit?p=123", "body": body}]
    )
    assert len(units) == 2
    u1 = units[0]
    assert u1["unit_number"] == "203A"
    assert "1,495" in u1["rent_range"]
    assert u1["bedrooms"] == "2"
    assert u1["bathrooms"] == "1"
    assert u1["sqft"] == "1050"
    assert u1["availability_date"] == "2026-06-15"


def test_parse_api_responses_handles_repli360_admin_customlink() -> None:
    """Repli360 admin endpoint /admin/get_apartmentsync_data_for_floorplan_multi_template
    ships ``customlink`` as the per-unit identifier. Affects ~17
    not-full props (Olympus, Velo, George NB, Parc, etc.)."""
    from ma_poc.pms.adapters._api_parser import parse_api_responses

    body = {
        "data": {
            "units": [
                {
                    "customlink": "U-101",
                    "price": "$1,450",
                    "bedrooms": 1,
                    "bathrooms": 1,
                    "sqft": 720,
                    "available_date": "2026-06-10",
                },
                {
                    "customlink": "U-302",
                    "price": "$1,995",
                    "bedrooms": 2,
                    "bathrooms": 2,
                    "sqft": 1100,
                    "available_date": "2026-06-25",
                },
            ]
        }
    }
    units = parse_api_responses([
        {"url": "https://app.repli360.com/admin/get_apartmentsync_data_for_floorplan_multi_template",
         "body": body}
    ])
    assert len(units) == 2
    assert units[0]["unit_number"] == "U-101"
    assert units[1]["unit_number"] == "U-302"
    assert "1,450" in units[0]["rent_range"]


def test_unit_id_priority_order_safe_with_id_fallback() -> None:
    """``id`` remains the LAST fallback in the unit_number priority list,
    after the prod-discovered customlink/Number/standard variants.
    Pins the order so a refactor that moves ``id`` higher can't silently
    re-introduce the AppFolio-style 'internal-id leak' bug."""
    from ma_poc.pms.adapters._api_parser import parse_api_responses

    # Item with BOTH customlink AND id — customlink should win
    body = {"units": [
        {"id": "internal-uuid-abc", "customlink": "Apt-7B", "price": 1500, "bedrooms": 1, "sqft": 600}
    ]}
    units = parse_api_responses([{"url": "https://app.repli360.com/x", "body": body}])
    assert len(units) == 1
    assert units[0]["unit_number"] == "Apt-7B", (
        f"customlink must take priority over id; got {units[0]['unit_number']!r}"
    )


def test_customlink_safe_narrow_alias() -> None:
    """``customlink`` is Repli360-admin-endpoint specific. The alias is
    safe-narrow because the term doesn't appear in any other PMS payload
    we've sampled across 4,982 properties. If a future operator emits
    'customlink' meaning something else, the alias would mis-extract;
    add a regression test then."""
    assert normalize_field_key("customlink") == "unit_id"
    # ``custom_link`` (with underscore) is intentionally NOT aliased —
    # we only see ``customlink`` (no separator) in prod field_patches.
    # If the underscore variant ever appears, add a regression test.
    assert normalize_field_key("custom_link") == "custom_link"
