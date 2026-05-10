"""PR-4: Integration tests for the L3 extraction → L4 validation pipeline.

Verifies that units parsed by the new Jugnu-native parsers (pms/adapters/_api_parser.py
and services/llm_prompt.py) flow correctly through validation.orchestrator.validate().
No Playwright, no network — all pure in-process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ma_poc.pms.adapters._api_parser import (
    parse_api_responses,
    parse_sightmap_payload,
    realpage_units_to_adapter_shape,
)
from ma_poc.services.llm_prompt import normalize_units
from ma_poc.validation.orchestrator import validate


# ── Helpers ────────────────────────────────────────────────────────────────────

@dataclass
class _FakeExtractResult:
    property_id: str = "prop_001"
    records: list[dict[str, Any]] = field(default_factory=list)
    tier_used: str = "TIER_1_API_GENERIC"
    adapter_name: str = "generic"
    confidence: float = 0.9


def _to_extract_result(units: list[dict], property_id: str = "prop_001") -> _FakeExtractResult:
    """Convert adapter-shape units into a minimal ExtractResult-compatible object."""
    records = []
    for u in units:
        rec: dict[str, Any] = {}
        # Map adapter keys to validation schema keys
        uid = u.get("unit_number") or u.get("unit_id") or u.get("unit_number")
        if uid:
            rec["unit_id"] = str(uid)
        if u.get("floor_plan_name"):
            rec["floor_plan_type"] = u["floor_plan_name"]
        bed_label = u.get("bed_label", "")
        if "Studio" in bed_label:
            rec["bedrooms"] = 0
        elif "Bedroom" in bed_label:
            try:
                rec["bedrooms"] = int(bed_label.split()[0])
            except (ValueError, IndexError):
                pass
        # Extract rent_range low value
        rent_range = u.get("rent_range", "")
        if rent_range.startswith("$"):
            try:
                low_str = rent_range.split(" - ")[0].replace("$", "").replace(",", "")
                rec["asking_rent"] = int(low_str)
            except (ValueError, IndexError):
                pass
        if u.get("sqft"):
            try:
                rec["sqft"] = int(u["sqft"])
            except (ValueError, TypeError):
                pass
        records.append(rec)
    return _FakeExtractResult(property_id=property_id, records=records)


# ── SightMap → validate ────────────────────────────────────────────────────────

class TestSightmapToValidation:
    def _sightmap_body(self) -> dict:
        return {
            "data": {
                "floor_plans": [
                    {"id": "1", "name": "1BR Classic", "bedroom_count": 1, "bathroom_count": 1},
                    {"id": "2", "name": "Studio", "bedroom_count": 0, "bathroom_count": 1},
                ],
                "units": [
                    {"floor_plan_id": "1", "unit_number": "101", "price": 1500,
                     "area": 750, "available_on": "2026-06-01"},
                    {"floor_plan_id": "2", "unit_number": "201", "price": 1100,
                     "area": 550, "available_on": "2026-07-01"},
                ],
            }
        }

    def test_sightmap_units_pass_validation(self):
        units = parse_sightmap_payload(self._sightmap_body(), "https://app.sightmap.com/api")
        assert len(units) == 2
        er = _to_extract_result(units, "sightmap_prop")
        vr = validate(er)
        assert len(vr.accepted) == 2
        assert len(vr.rejected) == 0
        assert vr.next_tier_requested is False

    def test_studio_labeled_correctly_in_pipeline(self):
        units = parse_sightmap_payload(self._sightmap_body(), "https://app.sightmap.com")
        studio_units = [u for u in units if u.get("bed_label") == "Studio"]
        assert len(studio_units) == 1

    def test_extraction_tier_preserved(self):
        units = parse_sightmap_payload(self._sightmap_body(), "https://app.sightmap.com")
        for u in units:
            assert u["extraction_tier"] == "TIER_1_API_SIGHTMAP"


# ── RealPage → validate ────────────────────────────────────────────────────────

class TestRealPageToValidation:
    def _realpage_body(self) -> dict:
        return {
            "response": [
                {"unitNumber": "A101", "rent": 1450, "availableDate": "2026-06-01"},
                {"unitNumber": "A102", "rent": 1600},
                {"unitNumber": "A103", "rent": 50},  # below sanity min — skipped
            ]
        }

    def test_realpage_valid_units_pass_validation(self):
        units = realpage_units_to_adapter_shape(self._realpage_body(), "https://api.ws.realpage.com/units")
        assert len(units) == 2  # 50 is below minimum
        er = _to_extract_result(units, "rp_prop")
        vr = validate(er)
        assert len(vr.accepted) == 2
        assert vr.next_tier_requested is False

    def test_rent_range_format_preserved(self):
        units = realpage_units_to_adapter_shape(self._realpage_body(), "https://api.ws.realpage.com/units")
        for u in units:
            assert u.get("rent_range", "").startswith("$")


# ── parse_api_responses → validate ────────────────────────────────────────────

class TestGenericApiToValidation:
    def test_flat_list_passes_validation(self):
        responses = [{
            "url": "https://example.com/api/units",
            "body": [
                {"floorPlanName": "1BR", "minRent": 1500, "beds": "1", "sqft": "750"},
                {"floorPlanName": "2BR", "minRent": 2000, "beds": "2", "sqft": "1000"},
            ],
        }]
        units = parse_api_responses(responses)
        assert len(units) == 2
        er = _to_extract_result(units, "generic_prop")
        vr = validate(er)
        assert len(vr.accepted) == 2

    def test_no_rent_unit_has_no_asking_rent_in_record(self):
        responses = [{
            "url": "https://example.com/api/units",
            "body": [
                {"floorPlanName": "1BR", "beds": "1", "sqft": "750"},  # no rent
            ],
        }]
        units = parse_api_responses(responses)
        er = _to_extract_result(units, "prop")
        # Unit has no rent_range → no asking_rent in the record
        for rec in er.records:
            assert rec.get("asking_rent") is None

    def test_deduplication_before_validation(self):
        item = {"floorPlanName": "1BR", "minRent": 1500, "beds": "1"}
        responses = [{"url": "url", "body": [item, item]}]
        units = parse_api_responses(responses)
        assert len(units) == 1
        er = _to_extract_result(units, "dedup_prop")
        vr = validate(er)
        assert len(vr.accepted) == 1

    def test_sightmap_routing_through_parse_api_responses(self):
        body = {
            "data": {
                "floor_plans": [{"id": "1", "name": "2BR", "bedroom_count": 2}],
                "units": [{"floor_plan_id": "1", "price": 2000}],
            }
        }
        responses = [{"url": "https://app.sightmap.com/api", "body": body}]
        units = parse_api_responses(responses)
        assert len(units) == 1
        assert units[0]["extraction_tier"] == "TIER_1_API_SIGHTMAP"
        er = _to_extract_result(units, "sm_prop")
        vr = validate(er)
        assert len(vr.accepted) == 1


# ── normalize_units → validate ─────────────────────────────────────────────────

class TestLlmNormalizeToValidation:
    """Verify that units normalized by llm_prompt.normalize_units() pass L4 validation."""

    def _llm_raw_units(self) -> list[dict]:
        return [
            {
                "unit_id": "LLM-101",
                "floor_plan_name": "1BR Loft",
                "bedrooms": 1,
                "bathrooms": 1.0,
                "sqft": 800,
                "market_rent_low": 1550,
                "market_rent_high": 1650,
                "available_date": "2026-07-01",
                "availability_status": "AVAILABLE",
                "confidence": 0.92,
            },
            {
                "unit_id": "LLM-102",
                "floor_plan_name": "2BR",
                "bedrooms": 2,
                "market_rent_low": 2100,
                "availability_status": "available",  # lowercase — should be normalized
                "confidence": 0.85,
            },
        ]

    def test_llm_normalized_units_pass_validation(self):
        normalized = normalize_units(self._llm_raw_units())
        assert len(normalized) == 2

        # Build extract result from normalized units
        records: list[dict[str, Any]] = []
        for u in normalized:
            rec: dict[str, Any] = {}
            if u.get("unit_id"):
                rec["unit_id"] = u["unit_id"]
            if u.get("floor_plan_name"):
                rec["floor_plan_type"] = u["floor_plan_name"]
            if u.get("bedrooms") is not None:
                rec["bedrooms"] = u["bedrooms"]
            if u.get("market_rent_low") is not None:
                rec["asking_rent"] = u["market_rent_low"]
            if u.get("sqft") is not None:
                rec["sqft"] = u["sqft"]
            records.append(rec)

        er = _FakeExtractResult(property_id="llm_prop", records=records)
        vr = validate(er)
        assert len(vr.accepted) == 2
        assert vr.next_tier_requested is False

    def test_status_normalized_before_validation(self):
        normalized = normalize_units(self._llm_raw_units())
        for u in normalized:
            assert u["availability_status"] in ("AVAILABLE", "UNAVAILABLE", "WAITLIST", "UNKNOWN")

    def test_below_sanity_rent_cleared(self):
        raw = [{"unit_id": "x", "market_rent_low": 100}]  # below $200 min
        normalized = normalize_units(raw)
        assert normalized[0]["market_rent_low"] is None

    def test_no_llm_output_validates_empty(self):
        normalized = normalize_units([])
        er = _FakeExtractResult(property_id="empty_prop", records=normalized)
        vr = validate(er)
        assert len(vr.accepted) == 0
        assert vr.next_tier_requested is False
