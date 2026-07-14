"""POC demo: API-supervised fallback-parser learning.

Runs the induce -> replay -> validate loop against a real flat-JSON API
fixture (Funnel/Nestio) and a real visible-DOM shape (RentManager WP cards),
plus a negative case proving the marketing-unit-# fidelity gate rejects a
parser that doesn't reproduce the gold roster.

    python ma_poc/scripts/demo_induce_parser.py

Exit 0 iff both positive cases pass the gate AND the negative case is
rejected.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow ``python ma_poc/scripts/demo_induce_parser.py`` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ma_poc.pms.learning import induce_fallback_parser, replay  # noqa: E402

_FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

_WP_CARDS = """
<div class="rm-ua-container">
  <a class="individual-item" data-bed="0" data-date="2026/08" data-rent="1030.00" href="/d/?uid=7326">
    <div class="detail-content"><h2>The Pearl <span>8150 W 30 1/2 St, #308</span></h2>
    <div class="unit-specs">Beds 0 Bath 1.0 Rent $1,030.00</div></div></a>
  <a class="individual-item" data-bed="2" data-date="2026/06" data-rent="1445.00" href="/d/?uid=7393">
    <div class="detail-content"><h2>The Emerald <span>8150 W 30 1/2 St, #209</span></h2>
    <div class="unit-specs">Beds 2 Bath 1.5 Rent $1,445.00</div></div></a>
  <a class="individual-item" data-bed="1" data-date="2026/09" data-rent="1725.00" href="/d/?uid=7400">
    <div class="detail-content"><h2>The Ruby <span>8150 W 30 1/2 St, #512</span></h2>
    <div class="unit-specs">Beds 1 Bath 1.0 Rent $1,725.00</div></div></a>
</div>"""


def _rule_summary(parser: object) -> dict[str, str]:
    return {f: (f"{r.kind}:{r.ref}" if r.ref else r.kind) for f, r in parser.field_rules.items()}  # type: ignore[attr-defined]


def demo_json() -> bool:
    body = json.loads((_FIXTURES / "funnel" / "connolly_station_api_response.json").read_text())
    raw = body["result"]["units"]
    gold = [
        {
            "unit_number": u["name"],
            "market_rent_low": u["minimum_rent"],
            "bedrooms": u["beds"],
            "bathrooms": u["baths"],
            "sqft": u["sqft"],
            "floor_plan_name": u["floorplan_name"],
            "availability_date": (u.get("availability_date") or "")[:10],
        }
        for u in raw
    ]
    parser, rep = induce_fallback_parser(gold, body, api_url="funnel/connolly")
    print("── CASE 1  Funnel flat-JSON API → induce JSON-path parser ──────────")
    print(f"   gold units: {len(gold)}  e.g. {[g['unit_number'] for g in gold[:6]]}")
    if parser:
        print(f"   envelope:   {parser.envelope}")
        print(f"   json_paths: {parser.to_llm_field_mapping()['json_paths']}")
    print(f"   VALIDATE:   passed={rep.passed} coverage={rep.coverage:.0%} "
          f"id_fidelity={rep.id_fidelity:.0%} matched={rep.matched}/{rep.gold_total}\n")
    return rep.passed


def demo_dom() -> bool:
    # Gold = what a trusted RentManager Tier-1 extraction returns for these
    # cards (marketing unit numbers + rent). Hardcoded so the demo stays
    # self-contained; identical values are produced by
    # parse_rentmanager_wp_cards in the adapter tests.
    gold = [
        {"unit_number": "308", "market_rent_low": 1030, "bedrooms": "0", "availability_date": "2026-08-01"},
        {"unit_number": "209", "market_rent_low": 1445, "bedrooms": "2", "availability_date": "2026-06-01"},
        {"unit_number": "512", "market_rent_low": 1725, "bedrooms": "1", "availability_date": "2026-09-01"},
    ]
    parser, rep = induce_fallback_parser(gold, _WP_CARDS)
    print("── CASE 2  RentManager visible-DOM cards → induce CSS parser ───────")
    print(f"   gold units: {len(gold)}  {[g['unit_number'] for g in gold]}")
    if parser:
        print(f"   container:  {parser.container}")
        print(f"   rules:      {_rule_summary(parser)}")
        print(f"   replay[0]:  {replay(parser, _WP_CARDS)[0]}")
    print(f"   VALIDATE:   passed={rep.passed} coverage={rep.coverage:.0%} "
          f"id_fidelity={rep.id_fidelity:.0%} matched={rep.matched}/{rep.gold_total}\n")
    return rep.passed


def demo_gate() -> bool:
    # Valid gold, but a DIFFERENT property's body — the gate must reject.
    gold = [{"unit_number": "221", "market_rent_low": 2579, "bedrooms": 1}]
    other = {"result": {"units": [{"name": "999", "minimum_rent": 1, "beds": 9}]}}
    parser, rep = induce_fallback_parser(gold, other)
    print("── CASE 3  fidelity GATE: gold vs mismatched body → must reject ────")
    print(f"   induced:  {parser}")
    print(f"   REJECTED: passed={rep.passed}  reasons={rep.reasons}\n")
    return parser is None and not rep.passed


def main() -> int:
    ok = [demo_json(), demo_dom(), demo_gate()]
    passed = all(ok)
    print("=" * 68)
    print(f"POC RESULT: {'PASS' if passed else 'FAIL'}  "
          f"(json={ok[0]}, dom={ok[1]}, gate-rejects={ok[2]})")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
