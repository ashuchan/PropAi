"""Regression: SightMap floor plans with no published units in units[] must
still appear in the output as catalogue-presence rows.

QC 2026-06-27 against apartments.com ground truth surfaced 3 Billingsley/
SightMap properties dropping townhome/secondary plans:
  - The Hudson:      18/30 (missing HHS1, HHS2, A6.2, A11, HHA9.4, HHA9.2,
                            B1, B5, B4.1, THB1, THB2, THC2.2)
  - Hastings End:    12/25
  - August Hills:     9/15

Root cause: parse_sightmap_payload() iterated data.units[] only, so floor
plans without a current unit listing (sold-out / not-yet-released cohorts —
notably townhome prefixes HH*/TH*) never made it into the catalogue. Fix
emits one row per such plan with data_quality_flag="SIGHTMAP_PLAN_PRESENCE"
and availability_status="UNAVAILABLE".
"""
from __future__ import annotations

import json
from pathlib import Path

from ma_poc.pms.adapters.sightmap import parse_sightmap_payload

FIXTURES = Path(__file__).parent / "fixtures" / "sightmap"

# apartments.com ground truth captured 2026-06-27.
HUDSON_TRUTH_PLANS = {
    "A1", "A11", "A2.1", "A2.2", "A3", "A5.1", "A6.1", "A6.2",
    "A8.1", "A8.2", "B1", "B2.1", "B2.2", "B3", "B4.1", "B4.2 ADA",
    "B5", "HHA5.2 ADA", "HHA7", "HHA9.2", "HHA9.3", "HHA9.4",
    "HHS1", "HHS2", "THA1", "THB1", "THB2", "THC1GG", "THC2.1", "THC2.2",
}


def test_hudson_emits_all_30_plans_including_empty_ones() -> None:
    responses = json.loads(
        (FIXTURES / "hudson" / "api_response.json").read_text(encoding="utf-8")
    )
    units, dropped = parse_sightmap_payload(responses[0]["body"], responses[0]["url"])
    assert dropped == 0

    plans = {u["floor_plan_name"] for u in units}
    missing = HUDSON_TRUTH_PLANS - plans
    extra = plans - HUDSON_TRUTH_PLANS
    assert not missing, f"Plans missing vs apartments.com truth: {sorted(missing)}"
    assert not extra, f"Unexpected plans not in truth: {sorted(extra)}"

    presence = [u for u in units if u.get("data_quality_flag") == "SIGHTMAP_PLAN_PRESENCE"]
    # 30 plans total, 18 have units → 12 plan-presence rows.
    assert len(presence) == 12
    for p in presence:
        assert p["availability_status"] == "UNAVAILABLE"
        assert p["available_units"] == "0"
        assert p["rent_range"] == ""
        assert p["unit_number"] == ""
        assert p["source_ids"].get("sightmap_floor_plan_id")
