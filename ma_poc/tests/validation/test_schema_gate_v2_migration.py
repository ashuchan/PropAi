"""F1 + F2 + F3 + F4 invariants. All fixtures are production-shaped.

Validates H1–H7, H11–H15 from VALIDATION_RECOVERY.md §2.
"""
from __future__ import annotations

import re

from ma_poc.validation.schema_gate import check


# ---- H1: static scan of the import surface ---------------------------------


def test_h1_schema_gate_imports_v2_only() -> None:
    """schema_gate.py must not call v1 fallback by name (H10 sister)."""
    from ma_poc.validation import schema_gate

    src = open(schema_gate.__file__, encoding="utf-8").read()
    assert "compute_fallback_unit_id" in src
    # No CALL of the v1 function (the name may be referenced in
    # backward-compat shim docstrings — but never invoked).
    assert re.search(r"\bcompute_fallback_id\s*\(", src) is None, (
        "schema_gate.py must not CALL compute_fallback_id (v1)"
    )


# ---- H2: the central recovery test (production-shape) ----------------------


def test_h2_floor_plan_name_alias_recovery(jugnu_v2_no_unit_id_record: dict) -> None:
    """The 25,634-rejections fix. v2 record with no unit_id must be
    accepted via inferred fallback."""
    result = check(jugnu_v2_no_unit_id_record, property_id="prop_123")
    assert result.accepted is not None, (
        f"Expected accept, got reasons: {result.rejection_reasons}"
    )
    assert result.inferred_id is True
    assert result.accepted["unit_id"].startswith("inferred_")
    # v2 hash digest is 16 hex chars
    assert len(result.accepted["unit_id"]) == len("inferred_") + 16


# ---- H3: legacy v1 shape still works ---------------------------------------


def test_h3_legacy_floor_plan_type_still_works(legacy_v1_record: dict) -> None:
    """Records emitted by scrape_properties._add must still pass."""
    record = dict(legacy_v1_record)
    del record["unit_id"]  # Force fallback path
    result = check(record, property_id="prop_123")
    assert result.accepted is not None
    assert result.inferred_id is True


# ---- H4: floor_plan-only still rejects (Phase 0 contract) ------------------


def test_h4_floor_plan_only_still_rejects() -> None:
    """v2's 'fp + ≥1 other identifying field' rule preserved in Phase 0."""
    record = {"floor_plan_name": "A1"}
    result = check(record, property_id="prop_123")
    assert result.accepted is None
    assert "IDENTITY_FALLBACK_INSUFFICIENT" in result.rejection_reasons


# ---- H5: rent-stability of inferred IDs ------------------------------------


def test_h5_rent_change_does_not_alter_unit_id(jugnu_v2_no_unit_id_record: dict) -> None:
    """v2 inferred IDs must be stable across rent changes."""
    base = dict(jugnu_v2_no_unit_id_record)
    r1 = check({**base, "rent_low": 2000, "rent_high": 2000}, property_id="prop_123")
    r2 = check({**base, "rent_low": 2200, "rent_high": 2200}, property_id="prop_123")
    r3 = check({**base, "rent_low": 1700, "rent_high": 1900}, property_id="prop_123")
    assert r1.accepted is not None and r2.accepted is not None and r3.accepted is not None
    assert r1.accepted["unit_id"] == r2.accepted["unit_id"] == r3.accepted["unit_id"]


def test_h5b_available_date_change_does_not_alter_unit_id(
    jugnu_v2_no_unit_id_record: dict,
) -> None:
    """Companion to H5: available_date must also not affect identity."""
    base = dict(jugnu_v2_no_unit_id_record)
    r1 = check({**base, "available_date": "2026-05-01"}, property_id="prop_123")
    r2 = check({**base, "available_date": "2026-08-15"}, property_id="prop_123")
    assert r1.accepted is not None and r2.accepted is not None
    assert r1.accepted["unit_id"] == r2.accepted["unit_id"]


# ---- H6/H7: F4 date placeholder routing ------------------------------------


def test_h6_date_placeholder_pass_through(coming_soon_record: dict) -> None:
    """Unparseable string date is accepted with placeholder stashing."""
    result = check(coming_soon_record, property_id="prop_123")
    assert result.accepted is not None
    assert result.accepted["available_date"] is None
    assert result.accepted["availability_date"] is None
    assert result.accepted["_date_placeholder"] == "Spring 2026"
    assert "INVALID_DATE_FORMAT" not in (result.rejection_reasons or [])


def test_h6_date_placeholder_emits_telemetry(coming_soon_record: dict, monkeypatch) -> None:
    """F4 emits validate.date_placeholder_observed with the truncated value."""
    captured: list[tuple[object, ...]] = []

    def fake_emit(kind, property_id, **kwargs):
        captured.append((kind, property_id, kwargs))

    import ma_poc.observability.events as events_mod

    monkeypatch.setattr(events_mod, "emit", fake_emit)
    check(coming_soon_record, property_id="prop_123")
    placeholder_calls = [
        c for c in captured if str(c[0]).endswith("date_placeholder_observed")
    ]
    assert placeholder_calls, f"expected DATE_PLACEHOLDER_OBSERVED emit; got {captured}"
    assert placeholder_calls[0][2].get("placeholder_value") == "Spring 2026"


def test_h7_non_string_date_still_rejects() -> None:
    """Non-string corrupted date types (int) still reject — F4 only re-routes
    the string-parse-failure path."""
    record = {
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "available_date": 42,  # int — corrupted type
    }
    result = check(record, property_id="prop_123")
    assert result.accepted is None
    assert "INVALID_DATE_FORMAT" in result.rejection_reasons


def test_f4_empty_string_date_treated_as_absent_not_placeholder() -> None:
    """Bug-hunt regression: empty / whitespace-only date strings must be
    treated as absent (no _date_placeholder set), not as a placeholder
    needing rescue. Pre-fix, these slipped through with
    _date_placeholder='' which downstream readers would treat as a real
    placeholder needing investigation.
    """
    record = {
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "available_date": "   ",  # whitespace only
    }
    result = check(record, property_id="prop_123")
    assert result.accepted is not None
    assert result.accepted.get("_date_placeholder") is None
    assert "INVALID_DATE_FORMAT" not in (result.rejection_reasons or [])


def test_f4_placeholder_value_is_stripped() -> None:
    """The stashed placeholder must be the trimmed string, not the raw
    leading/trailing whitespace version. Otherwise dashboards would group
    'Spring 2026' and ' Spring 2026 ' as distinct phrases."""
    record = {
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "available_date": "  Spring 2026  ",
    }
    result = check(record, property_id="prop_123")
    assert result.accepted is not None
    assert result.accepted["_date_placeholder"] == "Spring 2026"


# ---- H11–H14: F2 + F3 v2 canonical-name reads ------------------------------


def test_h11_v2_rent_low_absurd_rejected() -> None:
    """v2-only record with absurd rent_low must reject. Pre-fix this slipped
    through because the lookup chain returned None."""
    record = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 60000,
    }
    result = check(record, property_id="prop_123")
    assert result.accepted is None
    assert "INVALID_RENT_ABSURD" in result.rejection_reasons


def test_h12_v2_rent_low_negative_rejected() -> None:
    record = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": -100,
    }
    result = check(record, property_id="prop_123")
    assert result.accepted is None
    assert "INVALID_RENT_NEGATIVE" in result.rejection_reasons


def test_h13_v2_area_absurd_rejected() -> None:
    """v2-only record with absurd area must reject. Pre-fix this slipped
    through because sqft lookup never checked area."""
    record = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 99999,
        "rent_low": 1500,
    }
    result = check(record, property_id="prop_123")
    assert result.accepted is None
    assert "INVALID_SQFT_ABSURD" in result.rejection_reasons


def test_h14_v2_area_minus_one_sentinel_accepted() -> None:
    """area=-1 sentinel must NOT fire INVALID_SQFT_NEGATIVE."""
    record = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": -1,
        "rent_low": 1500,
    }
    result = check(record, property_id="prop_123")
    assert result.accepted is not None
    assert "INVALID_SQFT_NEGATIVE" not in (result.rejection_reasons or [])
    assert "INVALID_SQFT_ABSURD" not in (result.rejection_reasons or [])


# ---- H15: rent lookup precedence -------------------------------------------


def test_h15_rent_lookup_precedence_v1_first(mixed_record: dict) -> None:
    """When v1 (asking_rent=60000 absurd) and v2 (rent_low=1500 healthy) both
    present, v1 wins — record rejects on the v1 absurd value, not the v2
    healthy value."""
    record = dict(mixed_record)
    record["asking_rent"] = 60000
    record["rent_low"] = 1500
    result = check(record, property_id="prop_123")
    assert result.accepted is None
    assert "INVALID_RENT_ABSURD" in result.rejection_reasons, (
        "If v2 had won, rent_low=1500 would pass; v1 priority lock failed"
    )


# ---- Migration trace: end-to-end Jugnu-shape happy path --------------------


def test_jugnu_v2_record_passes_unchanged(jugnu_v2_record: dict) -> None:
    """A complete v2 record from a healthy Jugnu adapter passes cleanly."""
    result = check(jugnu_v2_record, property_id="prop_123")
    assert result.accepted is not None
    assert result.accepted["unit_id"] == "1004"
    assert result.inferred_id is False
    assert result.rejection_reasons == []


def test_legacy_v1_record_passes_unchanged(legacy_v1_record: dict) -> None:
    """Symmetric companion: full v1 record passes cleanly too."""
    result = check(legacy_v1_record, property_id="prop_123")
    assert result.accepted is not None
    assert result.accepted["unit_id"] == "1004"
    assert result.inferred_id is False


# ---- Required-property_id contract -----------------------------------------


def test_check_rejects_empty_property_id() -> None:
    """Code-review fix (Issue 2.1): an empty property_id namespace would
    let two physically different units in different properties collide
    on inferred unit_id. check() must refuse rather than silently degrade.
    """
    import pytest

    with pytest.raises(ValueError, match="non-empty property_id"):
        check({"floor_plan_name": "A1", "beds": 1, "area": 750}, property_id="")


def test_orchestrator_coerces_missing_property_id_to_unknown() -> None:
    """Companion to the above: when an extract_result lacks property_id
    the orchestrator must coerce to the literal "unknown" rather than
    let the ValueError bubble up and crash the entire property's run.
    """
    from dataclasses import dataclass, field

    from ma_poc.validation.orchestrator import validate

    @dataclass
    class _ER:
        property_id: str = ""
        records: list = field(default_factory=list)

    er = _ER(property_id="", records=[{"floor_plan_name": "A1", "beds": 1, "area": 750}])
    result = validate(er)
    # Did not raise; the record went through with property_id="unknown"
    assert len(result.accepted) + len(result.rejected) == 1
