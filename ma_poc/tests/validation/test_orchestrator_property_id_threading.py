"""H1 (signature) + H8 (field_presence preservation) + F9 (identity_gap)
for the orchestrator."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

from ma_poc.validation.orchestrator import _field_presence, validate


@dataclass
class _StubExtractResult:
    property_id: str
    records: list[dict[str, Any]] = field(default_factory=list)


def test_h1_orchestrator_threads_property_id_into_schema_check() -> None:
    """orchestrator.validate must pass property_id into schema_check."""
    er = _StubExtractResult(
        property_id="prop_42",
        records=[{"floor_plan_name": "A1", "beds": 1, "area": 750, "rent_low": 1500}],
    )
    with patch("ma_poc.validation.orchestrator.schema_check") as mock_check:
        mock_check.return_value.accepted = None
        mock_check.return_value.rejection_reasons = ["IDENTITY_FALLBACK_INSUFFICIENT"]
        mock_check.return_value.inferred_id = False
        validate(er)
    mock_check.assert_called_once()
    args, kwargs = mock_check.call_args
    assert "prop_42" in args or kwargs.get("property_id") == "prop_42"


def test_h8_field_presence_unchanged_for_v2_record() -> None:
    """The wide-alias field_presence map must mark v2 fields as present."""
    record = {
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
    }
    presence = _field_presence(record)
    assert presence["floor_plan_name"] is True
    assert presence["beds"] is True
    assert presence["sqft"] is True   # canonical (area aliases to sqft)
    assert presence["rent"] is True   # canonical (rent_low aliased per F9)


def test_h8b_field_presence_unchanged_for_v1_record() -> None:
    """v1-shaped record must still mark all fields present."""
    record = {
        "floor_plan_type": "A1",
        "bedrooms": 1,
        "sqft": 750,
        "asking_rent": 1500,
    }
    presence = _field_presence(record)
    assert presence["floor_plan_name"] is True
    assert presence["beds"] is True
    assert presence["sqft"] is True
    assert presence["rent"] is True


def test_f9_identity_gap_event_emitted_on_fallback_insufficient(monkeypatch) -> None:
    """F9: every IDENTITY_FALLBACK_INSUFFICIENT rejection emits a parallel
    validate.identity_gap event with signature_key + components + presence."""
    captured: list[tuple[object, str, dict[str, Any]]] = []

    def fake_emit(kind, property_id, **kwargs):
        captured.append((kind, property_id, kwargs))

    import ma_poc.validation.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "emit", fake_emit)
    er = _StubExtractResult(
        property_id="prop_99",
        records=[{"floor_plan_name": "A1"}],  # only fp → IDENTITY_FALLBACK_INSUFFICIENT
    )
    validate(er)
    gap_events = [c for c in captured if str(c[0]).endswith("identity_gap")]
    assert gap_events, f"expected IDENTITY_GAP emit; got: {[str(c[0]) for c in captured]}"
    payload = gap_events[0][2]
    assert "signature_key" in payload
    assert payload.get("reason") == "IDENTITY_FALLBACK_INSUFFICIENT"
    # F9 normalizes string values for signature stability (lowercase + whitespace
    # collapse), so the stored value is "a1" not "A1".
    assert payload.get("signature_components", {}).get("floor_plan_name") == "a1"
    assert payload.get("field_presence", {}).get("floor_plan_name") is True
    assert payload.get("field_presence", {}).get("rent") is False


def test_f9_signature_stable_across_cosmetic_reformatting(monkeypatch) -> None:
    """Bug-hunt regression: 'Aspen 1BR' and 'aspen  1br' must produce the
    same signature_key so Phase 1 cross-run matching doesn't fragment
    identities across cosmetic re-formatting of the source page."""
    captured: list[tuple[object, str, dict[str, Any]]] = []

    def fake_emit(kind, property_id, **kwargs):
        captured.append((kind, property_id, kwargs))

    import ma_poc.validation.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "emit", fake_emit)
    validate(_StubExtractResult(property_id="P1", records=[{"floor_plan_name": "Aspen 1BR"}]))
    validate(_StubExtractResult(property_id="P1", records=[{"floor_plan_name": "aspen  1br"}]))
    keys = [c[2].get("signature_key") for c in captured if str(c[0]).endswith("identity_gap")]
    assert len(keys) == 2
    assert keys[0] == keys[1], f"signature must collapse cosmetic variants; got {keys}"
