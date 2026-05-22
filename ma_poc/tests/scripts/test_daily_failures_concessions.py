"""Regression test for the 2026-05-21 daily_failures concessions bug.

Pre-fix the script collapsed three V2 concession fields
(``concession_text``, ``concession_text_clean``, ``_concession_quality``)
into a single ``concessions`` stringified column. Reviewers couldn't
distinguish "no concession on this unit" from "concession existed but
the cleaner produced empty output" -- both rendered as a blank cell.

Post-fix: three explicit columns ship in the xlsx, the legacy
``concessions`` key is retained on the row dict for backward
compatibility but no longer drives the column layout.
"""
from __future__ import annotations

import json
from pathlib import Path

from ma_poc.scripts.email.daily_failures import (
    _SCRAPED_COLUMNS,
    _flatten_properties_json,
    _stringify_concessions,
)


def _write_properties_json(tmp_path: Path, props: list[dict]) -> Path:
    """Write a synthetic properties.json fixture and return its path."""
    p = tmp_path / "properties.json"
    p.write_text(json.dumps(props), encoding="utf-8")
    return p


# ────────────────────────────────────────────────────────────────────
# Column-layout invariants
# ────────────────────────────────────────────────────────────────────


def test_scraped_columns_include_three_concession_fields():
    """The xlsx column layout must explicitly carry all three V2
    concession fields. Pre-fix only the single legacy 'concessions'
    column shipped."""
    keys = {k for k, _ in _SCRAPED_COLUMNS}
    assert "concession_text" in keys, "raw concession text missing from xlsx"
    assert "concession_text_clean" in keys, "cleaned concession variant missing"
    assert "_concession_quality" in keys, "concession quality label missing"


def test_legacy_concessions_column_removed_from_xlsx():
    """The single 'concessions' column was the bug source -- it conflated
    three signals into one cell. Must not be in the column layout."""
    keys = [k for k, _ in _SCRAPED_COLUMNS]
    assert "concessions" not in keys, (
        "legacy 'concessions' column should be removed -- replaced by the "
        "three explicit fields. The legacy key may still appear on the "
        "row dict for backward compat, but the xlsx layout must use the "
        "three new columns."
    )


def test_column_headers_are_human_readable():
    """The display labels for the three new columns should make the
    distinction obvious to a reviewer scanning the xlsx."""
    by_key = {k: label for k, label in _SCRAPED_COLUMNS}
    assert by_key["concession_text"] == "Concession (Raw)"
    assert by_key["concession_text_clean"] == "Concession (Cleaned)"
    assert by_key["_concession_quality"] == "Concession Quality"


# ────────────────────────────────────────────────────────────────────
# Row-builder integration: _flatten_properties_json
# (the json-backed path; the SQL path uses identical row-build logic)
# ────────────────────────────────────────────────────────────────────


def _success_property(*, units: list[dict]) -> dict:
    """Minimal-shape success property record."""
    return {
        "canonical_id": "P_TEST",
        "Property Name": "Test Apartments",
        "City": "Austin",
        "State": "TX",
        "ZIP Code": "78701",
        "Management Company": "Test Mgmt",
        "Website": "https://example.com",
        "_meta": {"verdict": "SUCCESS", "tier_used": "TIER_1_API_RENTCAFE"},
        "units": units,
    }


def test_flatten_emits_all_three_concession_fields_per_unit(tmp_path):
    """A unit carrying all three concession variants must emit all
    three values in the row -- not just the cleaned one."""
    path = _write_properties_json(tmp_path, [
        _success_property(units=[{
            "unit_id": "101", "floor_plan_name": "A1",
            "beds": 1, "baths": 1, "area": 750,
            "rent_low": 1500, "rent_high": 1500,
            "available_date": "2026-06-01", "lease_term": 12,
            "concession_text": "1 month free with 13-month lease",
            "concession_text_clean": "1 month free with 13-month lease",
            "_concession_quality": "clean",
        }]),
    ])
    rows = _flatten_properties_json(path)
    assert len(rows) == 1
    r = rows[0]
    assert r["concession_text"] == "1 month free with 13-month lease"
    assert r["concession_text_clean"] == "1 month free with 13-month lease"
    assert r["_concession_quality"] == "clean"


def test_flatten_preserves_raw_when_cleaner_returned_empty(tmp_path):
    """The key fix: when the cleaner produced empty output (e.g. on a
    script-leaked source), the raw column must still carry the
    producer's original text so reviewers can recover the signal."""
    path = _write_properties_json(tmp_path, [
        _success_property(units=[{
            "unit_id": "201", "floor_plan_name": "B2",
            "beds": 2, "baths": 2, "area": 1000,
            "rent_low": 2000, "rent_high": 2000,
            "available_date": "", "lease_term": None,
            # Pre-fix the raw text was dropped by the
            # cleaned-OR-raw-OR-legacy fallback chain when the cleaner
            # produced "". Post-fix the raw is a separate column.
            "concession_text": "Special: <script>alert(1)</script> 1 mo free",
            "concession_text_clean": "",
            "_concession_quality": "unclean_script_leak",
        }]),
    ])
    rows = _flatten_properties_json(path)
    r = rows[0]
    assert r["concession_text"] != "", (
        f"raw concession text dropped: {r['concession_text']!r}"
    )
    assert "1 mo free" in r["concession_text"]
    assert r["_concession_quality"] == "unclean_script_leak"
    # Cleaned column is honest about producing nothing usable.
    assert r["concession_text_clean"] == ""


def test_flatten_emits_empty_strings_for_unit_with_no_concession(tmp_path):
    """Unit with no concession data at all: all three fields are
    empty strings (not None) so xlsx cells render cleanly."""
    path = _write_properties_json(tmp_path, [
        _success_property(units=[{
            "unit_id": "301", "floor_plan_name": "C3",
            "beds": 1, "baths": 1, "area": 700,
            "rent_low": 1400, "rent_high": 1400,
            "available_date": "", "lease_term": None,
            # No concession_text, concession_text_clean, or
            # _concession_quality keys on the unit.
        }]),
    ])
    rows = _flatten_properties_json(path)
    r = rows[0]
    assert r["concession_text"] == ""
    assert r["concession_text_clean"] == ""
    assert r["_concession_quality"] == ""


def test_flatten_falls_back_to_property_level_concession(tmp_path):
    """V2 schema captures the homepage banner at the PROPERTY level
    (``concessions`` / ``concessions_clean`` / ``_concessions_quality``)
    and only adds unit-level concession fields when the adapter parsed
    a per-row offer. In the 2026-05-21 run 2081 / 4982 properties had
    a property-level banner but ZERO units had a per-row concession,
    so the success xlsx rendered blank concession cells for ~38K unit
    rows that should have carried the parent banner.

    Fix: when the unit has no concession_text of its own, fall through
    to the property-level fields."""
    prop = {
        "_meta": {"verdict": "SUCCESS", "canonical_id": "P_TEST"},
        "Property Name": "The Mirage Apartments",
        "City": "Austin", "State": "TX",
        # Property-level banner (V2 emit keys, plural with `s`).
        "concessions": "$99 Move-In Special covers May & June RENT!",
        "concessions_clean": "$99 Move-In Special covers May & June RENT!",
        "_concessions_quality": "clean",
        "units": [
            {"unit_id": "101", "floor_plan_name": "A1", "rent_low": 1400},
            {"unit_id": "102", "floor_plan_name": "A1", "rent_low": 1450},
        ],
    }
    path = _write_properties_json(tmp_path, [prop])
    rows = _flatten_properties_json(path)
    assert len(rows) == 2
    for r in rows:
        assert "$99 Move-In Special" in r["concession_text"], (
            "property-level banner did not propagate to unit row"
        )
        assert "$99 Move-In Special" in r["concession_text_clean"]
        assert r["_concession_quality"] == "clean"


def test_flatten_unit_concession_overrides_property_level(tmp_path):
    """When a unit DOES emit its own concession (e.g. Entrata API
    returned a per-row offer), the unit value wins over the parent
    banner — otherwise per-row offers would be masked by site-wide
    advertised specials."""
    prop = {
        "_meta": {"verdict": "SUCCESS", "canonical_id": "P_TEST"},
        "Property Name": "Mixed Concessions",
        "concessions": "Site-wide $99 special",
        "concessions_clean": "Site-wide $99 special",
        "_concessions_quality": "clean",
        "units": [
            {"unit_id": "101", "concession_text": "1 month free on this unit",
             "concession_text_clean": "1 month free on this unit",
             "_concession_quality": "clean"},
            {"unit_id": "102"},  # no unit-level concession
        ],
    }
    path = _write_properties_json(tmp_path, [prop])
    rows = _flatten_properties_json(path)
    by_unit = {r["unit_id"]: r for r in rows}
    assert by_unit["101"]["concession_text"] == "1 month free on this unit"
    # Unit 102 inherits the parent banner.
    assert "Site-wide $99 special" in by_unit["102"]["concession_text"]


def test_flatten_handles_v2_internal_field_names(tmp_path):
    """Jugnu per-shard properties.json emits the v2 internal key set:
    ``apartment_id`` / ``proj_name`` / lowercase ``city`` / ``state`` /
    ``zip_code`` / ``pmc`` / ``website`` — and ``_meta.canonical_id``
    for the canonical id. Pre-fix the reader looked for the legacy
    title-case keys (``Unique ID``, ``Property Name``, ``City``, ...)
    so every property emitted with the v2 shape had blank rows."""
    prop = {
        "_meta": {"verdict": "SUCCESS", "canonical_id": "1234"},
        "apartment_id": 1234,
        "proj_name": "Villas at Pinecrest",
        "city": "Austin", "state": "TX", "zip_code": "78701",
        "pmc": "Test Mgmt", "website": "https://example.com",
        "concessions": "$50 off first month",
        "concessions_clean": "$50 off first month",
        "_concessions_quality": "clean",
        "units": [{"unit_id": "101", "rent_low": 1400}],
    }
    path = _write_properties_json(tmp_path, [prop])
    rows = _flatten_properties_json(path)
    assert len(rows) == 1
    r = rows[0]
    assert r["canonical_id"] == "1234"
    assert r["property_name"] == "Villas at Pinecrest"
    assert r["city"] == "Austin"
    assert r["state"] == "TX"
    assert r["pmc"] == "Test Mgmt"
    assert "$50 off" in r["concession_text"]


def test_flatten_placeholder_for_property_with_no_units(tmp_path):
    """A property whose 'units' is empty produces a placeholder row.
    The placeholder must include the three concession columns so
    column-by-column zips don't drift."""
    path = _write_properties_json(tmp_path, [_success_property(units=[])])
    rows = _flatten_properties_json(path)
    assert len(rows) == 1
    r = rows[0]
    # All three new fields are present (empty).
    assert "concession_text" in r
    assert "concession_text_clean" in r
    assert "_concession_quality" in r


# ────────────────────────────────────────────────────────────────────
# _stringify_concessions: the single-cell renderer for any future
# consumer that wants the legacy collapsed view.
# ────────────────────────────────────────────────────────────────────


def test_stringify_concessions_handles_str():
    assert _stringify_concessions("1 month free") == "1 month free"


def test_stringify_concessions_handles_dict():
    out = _stringify_concessions({"type": "free_rent", "months": 1})
    assert "free_rent" in out
    assert "1" in out


def test_stringify_concessions_handles_none():
    assert _stringify_concessions(None) == ""


# ────────────────────────────────────────────────────────────────────
# 2026-05-22 enrichment columns — banner / offer_type / target /
# value / conditions. New columns surface deterministic structured
# signals so the xlsx is filterable per-cell instead of full-text.
# ────────────────────────────────────────────────────────────────────


def test_scraped_columns_include_enrichment_fields():
    """The five enrichment columns must appear in _SCRAPED_COLUMNS so
    the xlsx layout exposes them. Order is non-binding but presence is."""
    keys = {k for k, _ in _SCRAPED_COLUMNS}
    assert "concession_banner" in keys
    assert "concession_offer_type" in keys
    assert "concession_target" in keys
    assert "concession_value" in keys
    assert "concession_conditions" in keys


def test_flatten_emits_enrichment_for_property_level_concession(tmp_path):
    """Property-level banner inherited by every unit row must also
    inherit the enrichment fields — otherwise a reviewer can see the
    raw text but can't filter on offer type / target."""
    prop = {
        "_meta": {"verdict": "SUCCESS", "canonical_id": "P_TEST"},
        "Property Name": "Test",
        "concessions": "Get 2 MONTHS FREE on select homes! Move-in by 5/31/2026.",
        "concessions_clean": "Get 2 MONTHS FREE on select homes! Move-in by 5/31/2026.",
        "_concessions_quality": "clean",
        "units": [
            {"unit_id": "101", "rent_low": 1400},
            {"unit_id": "102", "rent_low": 1500},
        ],
    }
    path = _write_properties_json(tmp_path, [prop])
    rows = _flatten_properties_json(path)
    assert len(rows) == 2
    for r in rows:
        assert r["concession_offer_type"] == "free_rent"
        assert r["concession_target"] == "rent"
        assert r["concession_value"] == "2 months"
        assert "2 months" in r["concession_banner"].lower()
        # Conditions string is a semicolon-joined list of kind:value pairs.
        assert "deadline" in r["concession_conditions"]
        assert "unit_scope" in r["concession_conditions"]


def test_flatten_unit_level_concession_drives_its_own_enrichment(tmp_path):
    """When a unit has its OWN per-row concession, the enrichment must
    be derived from THAT, not the property banner — otherwise a unit
    with a unique offer would be misclassified by the parent's terms."""
    prop = {
        "_meta": {"verdict": "SUCCESS", "canonical_id": "P_TEST"},
        "Property Name": "Test",
        "concessions": "Site-wide $99 special",
        "concessions_clean": "Site-wide $99 special",
        "_concessions_quality": "clean",
        "units": [
            # Unit-specific concession overrides parent.
            {"unit_id": "101",
             "concession_text": "1 month free on this unit with 12 month lease",
             "concession_text_clean": "1 month free on this unit with 12 month lease",
             "_concession_quality": "clean"},
            # Unit with no own concession — inherits parent enrichment.
            {"unit_id": "102"},
        ],
    }
    path = _write_properties_json(tmp_path, [prop])
    rows = _flatten_properties_json(path)
    by_unit = {r["unit_id"]: r for r in rows}
    assert by_unit["101"]["concession_offer_type"] == "free_rent"
    assert by_unit["101"]["concession_value"] == "1 month"
    # Parent banner has $99 special — small amount won't promote to a
    # primary; banner stays raw. The point is unit-101's enrichment is
    # different from unit-102's.
    assert by_unit["101"]["concession_value"] != by_unit["102"]["concession_value"]


def test_flatten_html_entities_decoded_in_enrichment(tmp_path):
    """HTML entity literals (``&amp;`` / ``&nbsp;``) must not leak into
    the cleaned cell OR the banner. The 2026-05-21 user report was that
    the xlsx cells showed ``May &amp; June`` verbatim."""
    prop = {
        "_meta": {"verdict": "SUCCESS", "canonical_id": "P_TEST"},
        "Property Name": "Test",
        "concessions": "$99 Move-In Special covers May &amp; June RENT!",
        "concessions_clean": "$99 Move-In Special covers May &amp; June RENT!",
        "_concessions_quality": "clean",
        "units": [{"unit_id": "101"}],
    }
    path = _write_properties_json(tmp_path, [prop])
    r = _flatten_properties_json(path)[0]
    assert "&amp;" not in r["concession_banner"]
    # Enrichment classifies as dollar_off / move_in_cost.
    assert r["concession_offer_type"] in ("dollar_off", "gift_card")


def test_flatten_empty_concession_emits_empty_enrichment_fields(tmp_path):
    """No concession → empty enrichment cells, not None / not omitted."""
    prop = {
        "_meta": {"verdict": "SUCCESS", "canonical_id": "P_TEST"},
        "Property Name": "Test",
        "units": [{"unit_id": "101"}],
    }
    path = _write_properties_json(tmp_path, [prop])
    r = _flatten_properties_json(path)[0]
    assert r["concession_banner"] == ""
    assert r["concession_offer_type"] == ""
    assert r["concession_target"] == ""
    assert r["concession_value"] == ""
    assert r["concession_conditions"] == ""
