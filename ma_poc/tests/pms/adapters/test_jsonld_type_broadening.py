"""Phase 6.4 — broaden TARGET_JSONLD_TYPES to cover lodging-coherent
Schema.org types observed in the HAR sample.

2026-05-21: ~5 properties in the ``actionable_html_extractor`` bucket
ship unit-shaped JSON-LD using ``Accommodation``, ``House``, or
``Suite`` instead of ``Apartment`` / ``ApartmentUnit``. The existing
``_walk_jsonld`` filter rejected these as non-target types, leaving
them to Tier-4 LLM despite having clean Schema.org payloads.

Why ``Hotel`` / ``LodgingBusiness`` were NOT added:
  • Both describe whole-building hotel inventory.
  • The matcher would accept Hilton/Marriott marketing pages that
    happen to be linked from a property-management portfolio, polluting
    the unit stream with hotel-room records.

The existing ``_jsonld_item_has_unit_signal`` second-pass (the
normalize_field_key path) protects against accommodation pages with
only marketing metadata — if the item has no offers / numberOfRooms /
floorSize, it gets filtered out before emission.
"""

from __future__ import annotations

from ma_poc.pms.adapters._api_parser import (
    TARGET_JSONLD_TYPES,
    _jsonld_item_has_unit_signal,
    _walk_jsonld,
)
from ma_poc.pms.adapters._html_extract import extract_jsonld_from_html

# ─────────────────────────────────────────────────────────────────────
# Constant contract — pin the additions
# ─────────────────────────────────────────────────────────────────────


def test_legacy_types_remain_in_target_set() -> None:
    """Don't regress: the pre-6.4 type set must still be present."""
    for t in (
        "Apartment",
        "ApartmentUnit",
        "ApartmentComplex",
        "Offer",
        "FloorPlan",
        "Residence",
        "SingleFamilyResidence",
    ):
        assert t in TARGET_JSONLD_TYPES, (
            f"{t!r} dropped — would silently break every property "
            "currently extracted via that schema type."
        )


def test_phase_6_4_types_added() -> None:
    """Accommodation/House/Suite must be in the matched set now."""
    for t in ("Accommodation", "House", "Suite"):
        assert t in TARGET_JSONLD_TYPES, (
            f"{t!r} missing — Phase 6.4 cohort will fall back to LLM."
        )


def test_excluded_hotel_types_stay_out() -> None:
    """Hotel/LodgingBusiness deliberately omitted (whole-building
    non-residential inventory). Pin so a well-meaning future
    contributor doesn't add them back."""
    for t in ("Hotel", "LodgingBusiness", "Motel"):
        assert t not in TARGET_JSONLD_TYPES, (
            f"{t!r} added — would pollute the unit stream with "
            "hotel-room records from cross-linked marketing pages."
        )


# ─────────────────────────────────────────────────────────────────────
# Walking behavior — make sure the new types actually surface
# ─────────────────────────────────────────────────────────────────────


def test_walk_jsonld_collects_accommodation_node() -> None:
    """A bare Accommodation node with floorSize must come out of
    ``_walk_jsonld``."""
    data = {
        "@context": "https://schema.org",
        "@type": "Accommodation",
        "name": "The Aspen",
        "numberOfRooms": 1,
        "floorSize": {"value": 720, "unitCode": "FTK"},
        "offers": {"@type": "Offer", "price": 1450, "priceCurrency": "USD"},
    }
    out: list[dict] = []
    _walk_jsonld(data, out)
    # _walk_jsonld collects EVERY target type encountered — the
    # nested Offer also lands in `out`. What matters here is that
    # the Accommodation node itself is present.
    acc = [n for n in out if n.get("@type") == "Accommodation"]
    assert len(acc) == 1, (
        f"expected one Accommodation match; got types: "
        f"{[n.get('@type') for n in out]}"
    )
    assert acc[0]["name"] == "The Aspen"


def test_walk_jsonld_collects_suite_node() -> None:
    """Suite is Schema.org's per-unit type for multi-unit lodgings;
    must be matched."""
    data = {
        "@context": "https://schema.org",
        "@type": "Suite",
        "name": "Penthouse B",
        "numberOfRooms": 2,
        "floorSize": {"value": 1240, "unitCode": "FTK"},
        "offers": {"price": 2250},
    }
    out: list[dict] = []
    _walk_jsonld(data, out)
    suites = [n for n in out if n.get("@type") == "Suite"]
    assert len(suites) == 1


def test_walk_jsonld_collects_house_node() -> None:
    """``House`` is the documented Schema.org type for SFR-style
    listings; some single-family-rental marketing CMSes use it."""
    data = {
        "@graph": [
            {"@type": "Organization", "name": "Mgmt Co"},
            {
                "@type": "House",
                "name": "4-Bed Detached",
                "numberOfRooms": 4,
                "offers": {"price": 3800},
            },
        ]
    }
    out: list[dict] = []
    _walk_jsonld(data, out)
    assert any(node.get("@type") == "House" for node in out), (
        f"House node missed; got types: {[n.get('@type') for n in out]}"
    )


def test_signal_gate_emits_phase_6_4_types_without_offers() -> None:
    """An Accommodation/House/Suite with NO offers but matching type
    should still pass ``_jsonld_item_has_unit_signal`` (the type-list
    gate in pass 1) so the downstream emitter sees it.

    Why this matters: some marketing CMSes ship a bare Accommodation
    shell with floor-plan metadata only — rent loads from a separate
    XHR. The signal gate must still accept the node so the existing
    plan-name-only filter (in ``generic.py``) gets a chance to make
    the policy call. Rejecting at this layer turns it into silent
    LLM-fallback.
    """
    for t in ("Accommodation", "House", "Suite"):
        item = {"@type": t, "name": "X"}
        assert _jsonld_item_has_unit_signal(item) is True, (
            f"{t!r} with bare shell rejected by signal gate — "
            "would silently disappear into Tier-4 LLM."
        )


def test_signal_gate_rejects_non_unit_lodging_type() -> None:
    """Hotel / LodgingBusiness with no offers/dimensions must NOT pass
    the signal gate — confirming the exclusion is enforced both at
    walk-time and at signal-time."""
    for t in ("Hotel", "LodgingBusiness"):
        item = {"@type": t, "name": "Marketing Page"}
        assert _jsonld_item_has_unit_signal(item) is False, (
            f"{t!r} accepted by signal gate — would emit a hotel "
            "marketing page as a unit."
        )


# ─────────────────────────────────────────────────────────────────────
# End-to-end extraction
# ─────────────────────────────────────────────────────────────────────


def _accommodation_jsonld_html(jsonld_payload: str) -> str:
    return (
        '<html><body><script type="application/ld+json">'
        + jsonld_payload +
        '</script></body></html>'
    )


def test_extract_jsonld_emits_accommodation_unit() -> None:
    """End-to-end smoke: an Accommodation node with offers becomes a
    unit dict on the adapter side."""
    payload = (
        '{"@context":"https://schema.org","@graph":['
        '{"@type":"Accommodation","name":"Aspen 1BR",'
        '"numberOfRooms":1,"floorSize":{"value":720,"unitCode":"FTK"},'
        '"offers":{"@type":"Offer","price":1450,"priceCurrency":"USD"}},'
        '{"@type":"Accommodation","name":"Birch 2BR",'
        '"numberOfRooms":2,"floorSize":{"value":980,"unitCode":"FTK"},'
        '"offers":{"@type":"Offer","price":1850,"priceCurrency":"USD"}}'
        ']}'
    )
    html = _accommodation_jsonld_html(payload)
    units = extract_jsonld_from_html(html, "https://example.com/floorplans")
    assert len(units) >= 1, (
        f"expected ≥1 unit from Accommodation @graph; got {len(units)}"
    )
