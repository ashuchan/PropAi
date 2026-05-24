"""Unified API concession extractor tests (2026-05-24).

Test oracle anchored on real HAR samples observed across the 313-HAR
audit. Each PMS family with a known concession-bearing JSON shape has
at least one test case using the actual field path observed in the wild.
"""
from __future__ import annotations

from ma_poc.core.api_concession_extract import (
    extract_api_concession,
    has_concession_flag,
)


# ─────────────────────────────────────────────────────────────────────
# Knock (doorway-api.knockrentals.com)
# Observed on: livebrez.com, hamburgfarmslex.com
# ─────────────────────────────────────────────────────────────────────


def test_knock_leasing_special_extracted() -> None:
    """Real shape from www.livebrez.com HAR."""
    body = {
        "property": {
            "data": {
                "leasing": {
                    "terms": {
                        "leasingSpecial": (
                            "APRIL SHOWERS BRING FREE RENT!\n"
                            "Move In by April 30, 2025 and Receive One Month Free\n"
                            "Restrictions apply. Call our leasing office for details."
                        )
                    }
                },
                "doorway": {"leasingSpecialIsActive": True},
            }
        }
    }
    result = extract_api_concession(body)
    assert result is not None
    assert "APRIL SHOWERS" in result
    assert "One Month Free" in result


def test_knock_flag_only_returns_none_text_but_true_flag() -> None:
    """Real shape from www.hamburgfarmslex.com — flag is True but no
    text is present in the API. Caller should fall back to HTML probe."""
    body = {
        "property": {
            "data": {
                "doorway": {"leasingSpecialIsActive": True},
                "leasing": {"terms": {"leasingSpecial": ""}},
            }
        }
    }
    assert extract_api_concession(body) is None
    assert has_concession_flag(body) is True


# ─────────────────────────────────────────────────────────────────────
# G5 inventory (inventory.g5marketingcloud.com/graphql)
# Observed on: villaswillowglen.com, livemarleymanor.com
# ─────────────────────────────────────────────────────────────────────


def test_g5_floorplan_specials_extracted() -> None:
    """Real shape from villaswillowglen.com HAR — G5 GraphQL response."""
    body = {
        "data": {
            "apartmentComplex": {
                "id": 123,
                "hasFloorplanSpecials": True,
                "floorplans": [
                    {
                        "id": 1,
                        "name": "1BR",
                        "floorplanSpecials": [
                            {"id": 8211998, "name": "1 month free on units #111 and #21!"}
                        ],
                    }
                ],
            }
        }
    }
    result = extract_api_concession(body)
    assert result == "1 month free on units #111 and #21!"


def test_g5_flag_only_when_specials_empty() -> None:
    """G5 commonly returns hasFloorplanSpecials=True with empty
    floorplanSpecials=[] arrays. Flag detection should still fire."""
    body = {
        "data": {
            "apartmentComplex": {
                "hasFloorplanSpecials": True,
                "floorplans": [{"id": 1, "floorplanSpecials": []}],
            }
        }
    }
    assert extract_api_concession(body) is None
    assert has_concession_flag(body) is True


# ─────────────────────────────────────────────────────────────────────
# G5 marketing-center (marketing-center-data.g5devops.com)
# Observed on: liveatautumnoaks.com, livelifeatspringlake.com
# ─────────────────────────────────────────────────────────────────────


def test_g5_marketing_center_special_display_text() -> None:
    """Real shape from liveatautumnoaks.com — different G5 endpoint."""
    body = {
        "summary": {
            "specials": [
                {"id": 1, "specialDisplayText": "6 Weeks Free Rent!"}
            ]
        }
    }
    result = extract_api_concession(body)
    assert result == "6 Weeks Free Rent!"


def test_g5_marketing_center_long_text() -> None:
    """Real shape from livelifeatspringlake.com — longer text."""
    body = {
        "specials": [
            {"specialDisplayText": (
                "Start the Season in Style — Limited Apartments at "
                "Special Rates! Price Drop ALERT!! Call Today!"
            )}
        ]
    }
    result = extract_api_concession(body)
    assert result is not None
    assert "Start the Season" in result
    assert "Special Rates" in result


# ─────────────────────────────────────────────────────────────────────
# RentCafe (various endpoints)
# Observed fields: bannerText / leasingSpecial / offer_description
# ─────────────────────────────────────────────────────────────────────


def test_rentcafe_banner_text() -> None:
    body = {"bannerText": "Save $500 on select 1-bedroom apartments"}
    result = extract_api_concession(body)
    assert result == "Save $500 on select 1-bedroom apartments"


def test_rentcafe_offer_description() -> None:
    body = {
        "offer_description": "Look & lease bonus this week",
        "leasingSpecial": "",
    }
    result = extract_api_concession(body)
    assert result == "Look & lease bonus this week"


# ─────────────────────────────────────────────────────────────────────
# Wix sites
# ─────────────────────────────────────────────────────────────────────


def test_wix_promotion_field() -> None:
    """Real shape: Wix sites stuff offers into ``promotion`` field."""
    body = {
        "promotion": "Move in by May 31 and get $750 off your first month",
    }
    result = extract_api_concession(body)
    assert result is not None
    assert "$750 off" in result


def test_wix_banner_text() -> None:
    body = {"bannerText": "Limited-time offer: 50% off rent"}
    result = extract_api_concession(body)
    assert "50% off rent" in result


# ─────────────────────────────────────────────────────────────────────
# Yardi SecureCafe
# ─────────────────────────────────────────────────────────────────────


def test_yardi_securecafe_banner_text() -> None:
    body = {
        "settings": {
            "bannerText": "Apply within 48 hours for $250 off",
            "floor_plan_specials_enabled": True,
        }
    }
    result = extract_api_concession(body)
    assert result == "Apply within 48 hours for $250 off"


def test_yardi_flag_only_no_text() -> None:
    body = {"settings": {"floor_plan_specials_enabled": True}}
    assert extract_api_concession(body) is None
    assert has_concession_flag(body) is True


# ─────────────────────────────────────────────────────────────────────
# Squarespace
# ─────────────────────────────────────────────────────────────────────


def test_squarespace_specials_dict_with_text() -> None:
    """Squarespace sites put offers in ``specials`` field as a dict."""
    body = {
        "specials": {"text": "Now offering 1 month free on select units"}
    }
    result = extract_api_concession(body)
    assert result == "Now offering 1 month free on select units"


# ─────────────────────────────────────────────────────────────────────
# Generic per-unit / per-floorplan structures
# ─────────────────────────────────────────────────────────────────────


def test_per_unit_specials_array() -> None:
    """Funnel/Nestio-style: ``units[].specials`` with text per unit."""
    body = {
        "units": [
            {"id": "U1", "specials": [{"description": "First month free on lease signing"}]},
            {"id": "U2", "specials": []},
        ]
    }
    result = extract_api_concession(body)
    assert "First month free" in result


def test_nested_concession_object_with_name_field() -> None:
    """Some adapters return ``apartmentSpecial: {name: "..."}`` instead
    of a bare string. Inner ``name`` key is one of the priority keys."""
    body = {
        "apartmentSpecial": {"name": "Look & lease — sign within 24h"}
    }
    result = extract_api_concession(body)
    assert result == "Look & lease — sign within 24h"


# ─────────────────────────────────────────────────────────────────────
# Junk filter — GDPR/cookie consent strings
# ─────────────────────────────────────────────────────────────────────


def test_junk_special_features_label_filtered() -> None:
    """Cookie consent text often appears as ``BSpecialFeaturesText:
    'Special Features'`` (Yardi consent banner). NOT a concession."""
    body = {"BSpecialFeaturesText": "Special Features"}
    assert extract_api_concession(body) is None


def test_junk_special_purposes_label_filtered() -> None:
    body = {"specialPurposes": "Special Purposes:"}
    assert extract_api_concession(body) is None


def test_junk_lone_label_filtered() -> None:
    """Bare 'Promotions' / 'Specials' labels (UI column headers) → None."""
    assert extract_api_concession({"promotions": "Promotions"}) is None
    assert extract_api_concession({"specials": "Specials"}) is None


def test_junk_wix_branding_filtered() -> None:
    """Wix template branding ('{Wix} This website was built on Wix.')
    appears under ``promotion`` on free-template sites — NOT a real
    operator concession. Confirmed false-positive on
    indianvillageapt.wixsite.com HAR (2026-05-24)."""
    body = {"promotion": "{Wix} This website was built on Wix. Create yours today."}
    assert extract_api_concession(body) is None


def test_junk_yardi_placeholder_filtered() -> None:
    """Yardi/SecureCafe empty-state placeholder. Confirmed false-positive
    on gatewayloftslexington.com HAR (2026-05-24)."""
    body = {"specials": {"text": "View the available special offers below."}}
    assert extract_api_concession(body) is None


def test_real_text_with_junk_marker_still_extracted() -> None:
    """A real concession that HAPPENS to contain 'special features' (>80
    chars) is NOT filtered — only short consent strings are."""
    text = (
        "Sign your lease and get our move-in special features bundle: "
        "1 month free rent plus waived admin fee. Conditions apply."
    )
    body = {"leasingSpecial": text}
    result = extract_api_concession(body)
    assert result is not None
    assert "1 month free" in result


# ─────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────


def test_empty_input_returns_none() -> None:
    for inp in (None, "", [], {}, False, 0):
        assert extract_api_concession(inp) is None
        assert has_concession_flag(inp) is False


def test_no_concession_keys_present_returns_none() -> None:
    body = {"floor_plan": "1BR", "rent": 1500, "sqft": 750}
    assert extract_api_concession(body) is None
    assert has_concession_flag(body) is False


def test_deeply_nested_walked_within_depth_cap() -> None:
    """Field 5 levels deep should still be found (default cap is 8)."""
    body = {"a": {"b": {"c": {"d": {"e": {
        "leasingSpecial": "Hidden deep — but reachable"
    }}}}}}
    result = extract_api_concession(body)
    assert result == "Hidden deep — but reachable"


def test_depth_cap_truncates_runaway() -> None:
    """At depth >8, the walker stops — used to bound cost on degenerate
    deeply-nested payloads."""
    # Build a 20-level deep dict with concession only at the bottom
    body: dict = {"leasingSpecial": "deepest"}
    for _ in range(20):
        body = {"deeper": body}
    # Default cap (8) → can't reach it
    assert extract_api_concession(body) is None
    # Explicit larger cap → can reach
    assert extract_api_concession(body, max_depth=25) == "deepest"


def test_longest_candidate_wins_when_multiple_matches() -> None:
    """When multiple concession fields are populated, return the
    longest (most context) string. Ties → first."""
    body = {
        "bannerText": "Short",
        "leasingSpecial": "This is a much longer and more detailed offer description with terms",
        "promotion": "Med-length",
    }
    result = extract_api_concession(body)
    assert result == ("This is a much longer and more detailed offer "
                      "description with terms")


def test_case_insensitive_field_matching() -> None:
    """Field name matching is case-insensitive AND ignores _/-
    separators. ``LEASING_SPECIAL``, ``leasing-special``,
    ``leasingSpecial`` all hash the same."""
    for key in ("leasingSpecial", "LEASING_SPECIAL", "leasing-special",
                "LeasingSpecial", "leasingspecial"):
        body = {key: "Apply within 48h"}
        result = extract_api_concession(body)
        assert result == "Apply within 48h", (
            f"key={key!r} failed to extract"
        )


def test_flag_string_true_also_counts() -> None:
    """Some adapters return ``'True'`` (string) instead of ``True``
    (bool) — both should trip the flag."""
    body = {"hasFloorplanSpecials": "true"}
    assert has_concession_flag(body) is True
    body2 = {"hasFloorplanSpecials": "1"}
    assert has_concession_flag(body2) is True
