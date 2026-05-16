"""Tests for DOM data-attr identity extraction + cross-hop merge dedup.

Covers three related extraction-correctness mechanisms:
  * DOM scan reads ``data-unit-id`` / ``data-apartment-id`` /
    ``data-unit-number`` attributes off the container or any descendant
    (real database PKs beat the inferred-id hash collisions that happen
    when many units share the same plan dimensions).
  * Cross-hop accumulation in ``_try_link_hop`` routes per-plan-page
    emissions through ``merge_into_result_units`` (R0a uid match)
    instead of blind ``extend()``.
  * ``merge_rank_signature`` normalises stringified-JSON ``floor_plan_name``
    so the merge layer treats the legacy ``{"name":"..."}`` shape and the
    unwrapped string shape as equivalent at rank R1a/R1c.
"""
from __future__ import annotations

import pytest


# ── DOM data-* attribute identity reading ──────────────────────────────────

class TestDomDataAttrIdentityExtraction:
    """DOM container's data-unit-id / data-apartment-id / data-unit-number
    flow into the unit record instead of being lost to text-only regex.

    Cortland on Pike PID 2982 has 79 anchors each carrying
    ``data-unit-id="2604014" data-apartment-id="201715186" data-unit-number="4"``.
    Without D6, the 25 cards for plan "1 Bed/1 Bath - A1B" all hash to the
    same inferred_* id → false collisions → final count = 22 unique IDs / 127 records.
    """

    def test_helper_reads_node_attrs(self) -> None:
        from bs4 import BeautifulSoup
        from ma_poc.pms.adapters._html_extract import _extract_data_uid_from_node
        html = """
        <a class="apartments__card" data-js-hook="apartment"
           data-apartment-id="201715186" data-unit-id="2604014"
           data-unit-number="4">Apt #C-113</a>
        """
        node = BeautifulSoup(html, "html.parser").select_one("a")
        uid, apt, unum = _extract_data_uid_from_node(node)
        assert uid == "2604014"
        assert apt == "201715186"
        assert unum == "4"

    def test_helper_walks_descendants(self) -> None:
        """data-unit on a descendant button (RentCafe option-row shape)."""
        from bs4 import BeautifulSoup
        from ma_poc.pms.adapters._html_extract import _extract_data_uid_from_node
        html = """
        <div class="option-row">
          <div class="detail first"><span class="mobile-text">Unit</span> 138</div>
          <button data-unit="5704854" data-target="#5384179-terms">See Details</button>
        </div>
        """
        node = BeautifulSoup(html, "html.parser").select_one("div.option-row")
        uid, apt, unum = _extract_data_uid_from_node(node)
        assert uid == "5704854"  # data-unit on descendant
        assert apt == ""
        assert unum == ""

    def test_dom_extraction_records_data_unit_id(self) -> None:
        """End-to-end: extract_units_from_dom emits unit_id from data-unit-id."""
        from ma_poc.pms.adapters._html_extract import extract_units_from_dom
        # Cortland-shaped: each card has data-unit-id + visible text with
        # bed/bath/sqft signals.
        html = """
        <html><body>
          <a class="apartments__card" data-js-hook="apartment"
             data-apartment-id="201715186" data-unit-id="2604014"
             data-unit-number="4">
            Apt #C-113 Studio 1 Bath 567 sq ft Starting at $1,513 Available Now
          </a>
          <a class="apartments__card" data-js-hook="apartment"
             data-apartment-id="201715187" data-unit-id="2604015"
             data-unit-number="5">
            Apt #C-114 Studio 1 Bath 567 sq ft Starting at $1,520 Available Now
          </a>
        </body></html>
        """
        units, mode = extract_units_from_dom(html, "https://cortland.com/x/")
        assert len(units) == 2, f"expected 2 units, got {len(units)}: {units}"
        # Both units carry real data-unit-id values
        uids = sorted([u.get("unit_id") for u in units])
        assert uids == ["2604014", "2604015"]
        # data-apartment-id surfaces as apartmentid
        apt_ids = sorted([u.get("apartmentid") for u in units])
        assert apt_ids == ["201715186", "201715187"]

    def test_inferred_collision_is_avoided(self) -> None:
        """Two cards with IDENTICAL bed/bath/sqft (same plan) but DIFFERENT
        data-unit-id MUST emit as 2 distinct records — not collapsed into 1
        via inferred-hash collision."""
        from ma_poc.pms.adapters._html_extract import extract_units_from_dom
        html = """
        <html><body>
          <a class="apartments__card" data-js-hook="apartment" data-unit-id="111">
            Apt #A 1 Bed 1 Bath 700 sqft $1,500 Available Now
          </a>
          <a class="apartments__card" data-js-hook="apartment" data-unit-id="222">
            Apt #B 1 Bed 1 Bath 700 sqft $1,500 Available Now
          </a>
          <a class="apartments__card" data-js-hook="apartment" data-unit-id="333">
            Apt #C 1 Bed 1 Bath 700 sqft $1,500 Available Now
          </a>
        </body></html>
        """
        units, _ = extract_units_from_dom(html, "https://example.com/")
        assert len(units) == 3
        assert sorted(u["unit_id"] for u in units) == ["111", "222", "333"]


# ── Merge-signature floor_plan_name normalisation (handles JSON-string shape) ─

class TestFpNameNormalisationForMergeDedup:
    """merge_rank_signature treats stringified-JSON fp_name as equivalent
    to its unwrapped form.

    Canyon Ridge PID 722 (cloud-2026-05-14) emitted units where some
    records had ``floor_plan_name='{"name":"1x1Ac T1","provider_id":"6093286"}'``
    (legacy stringified-JSON) and others had ``floor_plan_name='1x1Ac T1'``
    (post-B3 unwrapped). The merge layer treated these as different plans
    → different floor_plan_id → no merge → duplicates.
    """

    def test_stringified_json_normalises_to_unwrapped(self) -> None:
        from ma_poc.pms.adapters._merge_fns import _normalise_fp_name
        a = _normalise_fp_name('{"name":"1x1Ac T1","provider_id":"6093286"}')
        b = _normalise_fp_name("1x1Ac T1")
        assert a == b
        assert a == "1x1ac t1"

    def test_stringified_json_label_field(self) -> None:
        """When the JSON dict has only 'label' (not 'name'), still unwrap."""
        from ma_poc.pms.adapters._merge_fns import _normalise_fp_name
        a = _normalise_fp_name('{"label":"Baywood","color":"red"}')
        assert a == "baywood"

    def test_plain_string_passthrough(self) -> None:
        from ma_poc.pms.adapters._merge_fns import _normalise_fp_name
        assert _normalise_fp_name("Baywood") == "baywood"
        assert _normalise_fp_name(None) == ""
        assert _normalise_fp_name("") == ""

    def test_dict_with_no_name_field_falls_through(self) -> None:
        """Defensive: when the JSON has no name/label/title keys, fall back
        to lowercased string of whole dict (existing merge_norm behaviour)."""
        from ma_poc.pms.adapters._merge_fns import _normalise_fp_name
        # The JSON parse succeeds but no name keys — fall back to lowercased original
        v = _normalise_fp_name('{"color":"red","provider_id":"abc"}')
        # Either approach is acceptable here as long as it doesn't crash.
        # Implementation falls back to merge_norm(name) of original string.
        assert isinstance(v, str)

    def test_signature_dedups_across_legacy_and_modern_fp_name(self) -> None:
        """End-to-end: two units with same unit_id but different fp_name
        shapes should produce identical signatures (R0a uid match works
        but additionally R1a/R1c can match too)."""
        from ma_poc.pms.adapters._merge_fns import merge_rank_signature
        legacy = {
            "unit_id": "E078", "floor_plan_id": "5dc39345fd3c",
            "floor_plan_name": '{"name":"1x1Ac T1","provider_id":"6093286"}',
            "bedrooms": 1, "bathrooms": 1, "sqft": 750,
        }
        modern = {
            "unit_id": "E078", "floor_plan_id": "9a48bfe77b09",
            "floor_plan_name": "1x1Ac T1",
            "bedrooms": 1, "bathrooms": 1, "sqft": 750,
        }
        sig_a = merge_rank_signature(legacy)
        sig_b = merge_rank_signature(modern)
        # fp_name now matches across both shapes
        assert sig_a["fp_name"] == sig_b["fp_name"]
        # uid also matches (always did, but confirms R0a will fire)
        assert sig_a["uid"] == sig_b["uid"]


# ── Cross-hop accumulator dedups overlapping records via merge_into_result_units ─

class TestCrossHopMergeDedup:
    """Per-plan-hop accumulation uses merge_into_result_units (R0a uid match)
    instead of blind extend.

    The actual D7 fix is at scraper.py:2980 and :3031 — switching extend()
    to merge_into_result_units(). The behaviour is tested indirectly via
    the merge function itself (already covered by existing merge tests)
    plus the canary on PID 722. Here we verify the merge contract: when
    given two unit lists with overlapping unit_ids, the result is a
    deduped list."""

    def test_merge_dedups_overlapping_unit_ids_with_different_fp_name_shapes(self) -> None:
        """D7 redirect + D8 normalise: same unit_id emitted from two hops
        with different fp_name shapes (stringified-JSON vs unwrapped) is
        deduped via R0a uid match. The actual Canyon Ridge bug shape."""
        from ma_poc.pms.adapters._merge_fns import merge_into_result_units
        existing = [
            {"unit_id": "E078", "floor_plan_id": "9a48bfe77b09",
             "floor_plan_name": "Baywood", "rent_low": 2195,
             "bedrooms": 1, "bathrooms": 1, "sqft": 750},
        ]
        incoming = [
            # Same unit E078; legacy stringified-JSON name; different fp_id
            # (computed from stringified-JSON). D8 normalisation makes the
            # fp_name comparable; R0a uid match catches the dedup.
            {"unit_id": "E078", "floor_plan_id": "5dc39345fd3c",
             "floor_plan_name": '{"name":"1x1Ac T1","provider_id":"6093286"}',
             "rent_low": 2195, "bedrooms": 1, "bathrooms": 1, "sqft": 750},
        ]
        result = merge_into_result_units(existing, incoming, property_id="722")
        uids = [str(u.get("unit_id")) for u in result]
        # E078 deduped → 1 unique record, not 2
        assert uids == ["E078"], f"E078 should dedup, got {uids}"

    def test_merge_appends_distinct_units(self) -> None:
        """Sanity: completely-distinct units (different uid AND different
        fp_id) are appended, not merged."""
        from ma_poc.pms.adapters._merge_fns import merge_into_result_units
        existing = [
            {"unit_id": "E078", "floor_plan_id": "9a48bfe77b09",
             "floor_plan_name": "Baywood"},
        ]
        incoming = [
            {"unit_id": "G100", "floor_plan_id": "abc111fff222",
             "floor_plan_name": "Rosewood"},
        ]
        result = merge_into_result_units(existing, incoming, property_id="722")
        uids = sorted(str(u.get("unit_id")) for u in result)
        assert uids == ["E078", "G100"]

    def test_r0_guard_preserves_same_plan_different_units(self) -> None:
        """R0 fp_id-only merge MUST NOT fire when both records carry a uid —
        otherwise units of the same plan collapse into one (Canyon Ridge
        pre-D6/D7/D8: 17 distinct units collapsed to 4).

        Three units (E078, F085, G100) all share fp_id=X (same plan
        "Baywood"). Merging incoming [G100] into existing [E078, F085]
        must keep all three distinct."""
        from ma_poc.pms.adapters._merge_fns import merge_into_result_units
        existing = [
            {"unit_id": "E078", "floor_plan_id": "X", "floor_plan_name": "Baywood",
             "bedrooms": 1, "bathrooms": 1, "sqft": 750},
            {"unit_id": "F085", "floor_plan_id": "X", "floor_plan_name": "Baywood",
             "bedrooms": 1, "bathrooms": 1, "sqft": 750},
        ]
        incoming = [
            {"unit_id": "G100", "floor_plan_id": "X", "floor_plan_name": "Baywood",
             "bedrooms": 1, "bathrooms": 1, "sqft": 750},
        ]
        result = merge_into_result_units(existing, incoming, property_id="722")
        uids = sorted(str(u.get("unit_id")) for u in result)
        assert uids == ["E078", "F085", "G100"], f"got {uids}"

    def test_r0_still_merges_plan_aggregate_records(self) -> None:
        """R0 should still merge PLAN-LEVEL records (no uid on either side)
        when fp_id matches — that's its original purpose. JSON-LD FloorPlan
        from entry-page + JSON-LD FloorPlan from hop page describing the
        same plan should dedup at R0."""
        from ma_poc.pms.adapters._merge_fns import merge_into_result_units
        existing = [
            {"floor_plan_id": "X", "floor_plan_name": "Baywood",
             "bedrooms": 1, "bathrooms": 1, "sqft": 750, "rent_low": 2100},
        ]
        incoming = [
            # No uid; same fp_id → R0 merge fires
            {"floor_plan_id": "X", "floor_plan_name": "Baywood",
             "bedrooms": 1, "bathrooms": 1, "sqft": 750, "rent_high": 2400},
        ]
        result = merge_into_result_units(existing, incoming, property_id="722")
        assert len(result) == 1, f"plan aggregates should merge, got {result}"
