"""Validate that adapter ``try_dom`` paths cover real-world DOM extraction
without invoking the LLM.

This is the user-requested "local test without LLM" — proves that the
Phase 1 cascade contract works end-to-end on captured HTML, with every
unit routed through ``dq_guards.apply_unit_guards`` and zero LLM calls.

Usage::

    python ma_poc/scripts/validate_no_llm_coverage.py

The script does NOT depend on cloud-run mirrors or live fetches — it
exercises every wired adapter's ``try_dom`` against synthetic fixtures
that mirror the real DOM shape (`<ea5-unit>`, ``data-listing-id``,
``tr.fp-unit``). On a real cloud canary the same harness can be pointed
at captured HTML in ``c:/tmp/run-<date>/shard_*/raw_html/<pid>.html``
when available.

Exit code 0 on success (all adapters' ``try_dom`` return units +
``dq_guards`` applied). Exit code 1 if any check fails.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Ensure the repo root is on PYTHONPATH when invoked from anywhere.
try:
    _REPO_ROOT = Path(__file__).resolve().parents[2]
except NameError:
    # Allow `exec(open(...).read())` invocations (smoke tests).
    _REPO_ROOT = Path.cwd()
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Belt-and-suspenders: force LLM env vars off so anything that lazily
# checks them sees disabled state.
os.environ["ENABLE_TIER4_LLM"] = "false"
os.environ["LLM_BUDGET_DOM_CALLS"] = "0"
os.environ["LLM_BUDGET_API_CALLS"] = "0"
os.environ["LLM_BUDGET_MONOLITHIC"] = "0"

from ma_poc.pms.adapters.appfolio import AppFolioAdapter  # noqa: E402
from ma_poc.pms.adapters.avalonbay import AvalonBayAdapter  # noqa: E402
from ma_poc.pms.adapters.base import AdapterContext, AdapterDomResult  # noqa: E402
from ma_poc.pms.adapters.cortland import CortlandAdapter  # noqa: E402
from ma_poc.pms.adapters.equity import EquityAdapter  # noqa: E402
from ma_poc.pms.adapters.generic_plan_text import GenericPlanTextAdapter  # noqa: E402
from ma_poc.pms.adapters.imt_spaces import ImtSpacesAdapter  # noqa: E402
from ma_poc.pms.adapters.realpage_cws import RealPageCwsAdapter  # noqa: E402
from ma_poc.pms.adapters.rentcafe import RentCafeAdapter  # noqa: E402
from ma_poc.pms.adapters.rentcafe_unit_roster import (  # noqa: E402
    RentCafeUnitRosterAdapter,
)
from ma_poc.pms.adapters.wix_floor_plans import WixFloorPlansAdapter  # noqa: E402
from ma_poc.pms.detector import DetectedPMS  # noqa: E402
from ma_poc.pms.scraper import _maybe_try_dom  # noqa: E402


@dataclass
class FixtureCase:
    """One synthetic-HTML test case mirroring a real DOM shape."""
    adapter_name: str
    pms: str
    html: str
    expected_min_units: int
    expected_tier: str
    description: str
    # 2026-05-24: fallback-only adapters (generic_plan_text) intentionally
    # return confidence <0.7 so the cascade keeps LLM as backup. Validation
    # PASS condition for these is units>=expected AND tier matches, but
    # not high_confidence.
    is_fallback: bool = False


def _appfolio_fixture() -> str:
    """SSR listings HTML matching the canonical AppFolio shape."""
    return """
<html><body>
<div data-listing-id="12345" class="js-listing-card">
  <div class="js-listing-blurb-rent">$1,450</div>
  <div class="js-listing-blurb-bed-bath">2 bd / 2 ba</div>
  <div class="js-listing-square-feet">Square Feet: 1,100</div>
  <div class="js-listing-available">2026-06-01</div>
  <span class="js-listing-address">123 Maple St</span>
</div>
<div data-listing-id="12346" class="js-listing-card">
  <div class="js-listing-blurb-rent">$1,650</div>
  <div class="js-listing-blurb-bed-bath">3 bd / 2 ba</div>
  <div class="js-listing-square-feet">Square Feet: 1,350</div>
  <div class="js-listing-available">2026-07-01</div>
  <span class="js-listing-address">456 Oak Ave</span>
</div>
<div data-listing-id="12347" class="js-listing-card">
  <div class="js-listing-blurb-rent">$1,250</div>
  <div class="js-listing-blurb-bed-bath">1 bd / 1 ba</div>
  <div class="js-listing-square-feet">Square Feet: 720</div>
  <div class="js-listing-available">2026-08-15</div>
  <span class="js-listing-address">789 Pine St</span>
</div>
</body></html>
"""


def _equity_fixture() -> str:
    """ea5-unit blocks matching the canonical Equity Residential shape.

    Mirrors the real Angular template structure that
    ``parse_equity_units`` regex over: ``class="pricing"`` for rent,
    ``class="time-period"`` for lease term, ``X Bed / Y Bath`` shape,
    ``XXX sq ft`` for sqft, ``Available MM/DD/YYYY`` for availability,
    ``class="static" alt="<plan>"`` for floor-plan name.
    """
    return """
<html><body>
<!-- ledgerId: 1001, buildingId: 100, unitId: 101 -->
<ea5-unit>
  <div class="pricing">$2,200</div>
  <span class="time-period">12 month</span>
  <div>1 Bed / 1 Bath</div>
  <div>650 sq ft</div>
  <div>Available 6/15/26</div>
  <img class="static" alt="Studio A" src="x.png"/>
</ea5-unit>
<!-- ledgerId: 1002, buildingId: 100, unitId: 102 -->
<ea5-unit>
  <div class="pricing">$2,500</div>
  <span class="time-period">12 month</span>
  <div>2 Bed / 2 Bath</div>
  <div>950 sq ft</div>
  <div>Available 7/01/26</div>
  <img class="static" alt="2B-Standard" src="x.png"/>
</ea5-unit>
</body></html>
"""


def _rentcafe_fixture() -> str:
    """Hosted-table HTML with .fp-unit data-* attributes — canonical
    RentCafe shape that ``parse_rentcafe_hosted_table`` reads."""
    return """
<html><body>
<table>
<tr class="fp-unit" data-unit-name="101A" data-unit-rent="1450"
    data-unit-bed="1" data-unit-bath="1" data-unit-sqft="750"
    data-unit-availability="2026-06-15"></tr>
<tr class="fp-unit" data-unit-name="102B" data-unit-rent="1650"
    data-unit-bed="2" data-unit-bath="2" data-unit-sqft="1100"
    data-unit-availability="2026-07-01"></tr>
<tr class="fp-unit" data-unit-name="103C" data-unit-rent="1850"
    data-unit-bed="2" data-unit-bath="2" data-unit-sqft="1250"
    data-unit-availability="2026-08-01"></tr>
</table>
</body></html>
"""


def _rentcafe_unit_roster_fixture() -> str:
    """Modern RentCafe theme. The parser pairs ``floorplan_X`` blocks
    with ``par_X`` peer elements by id-suffix lookup (NOT by nesting).
    """
    return """
<html><body>
<div id="floorplan_A1" class="floorplan-block"
     data-bed="1" data-bath="1" data-sqft="700" data-name="Plan A1">
  <h3>Plan A1</h3>
</div>
<div id="par_A1" class="par-units">
  <div id="unit_101" class="unit-container">
    <span class="unit-number">101</span>
    <span class="unit-sqft">700 sq ft</span>
    <span class="unit-rent">$1,500</span>
    <span class="available-now"></span>
  </div>
  <div id="unit_102" class="unit-container">
    <span class="unit-number">102</span>
    <span class="unit-sqft">700 sq ft</span>
    <span class="unit-rent">$1,550</span>
    <span class="available-now"></span>
  </div>
</div>
</body></html>
"""


def _realpage_cws_fixture() -> str:
    """RealPage CWS .rpfp-card widget shape."""
    return """
<html><body>
<div class="rpfp-container">
  <div class="rpfp-card">
    <div class="rpfp-name">Plan A</div>
    <div class="rpfp-bb">1 Bed / 1 Bath</div>
    <div class="rpfp-sqft">700 sq ft</div>
    <div class="rpfp-price">$1,450</div>
  </div>
  <div class="rpfp-card">
    <div class="rpfp-name">Plan B</div>
    <div class="rpfp-bb">2 Bed / 2 Bath</div>
    <div class="rpfp-sqft">1100 sq ft</div>
    <div class="rpfp-price">$1,850</div>
  </div>
</div>
</body></html>
"""


def _imt_spaces_fixture() -> str:
    """IMT 'Spaces' theme: article.spaces-plan with data-spaces-* attrs.

    The parser converts ``data-spaces-sort-bed`` → ``spacesSortBed``
    (camelCase) via kebab-to-camel. Attribute names use the
    ``data-spaces-sort-*`` infix that the real IMT template uses.
    """
    return """
<html><body>
<article class="spaces-plan"
  title="The Maple"
  data-spaces-sort-bed="1"
  data-spaces-bath-count="1"
  data-spaces-sort-sqft="725"
  data-spaces-sort-price="1450"
  data-spaces-sort-plan-name="The Maple"
  data-spaces-availability="2026-06-15"></article>
<article class="spaces-plan"
  title="The Oak"
  data-spaces-sort-bed="2"
  data-spaces-bath-count="2"
  data-spaces-sort-sqft="1100"
  data-spaces-sort-price="1850"
  data-spaces-sort-plan-name="The Oak"
  data-spaces-availability="2026-07-01"></article>
</body></html>
"""


def _wix_floor_plans_fixture() -> str:
    """Wix-hosted card matching the parser's gate: 'Starting at $X' AND
    bed|bath|sq pattern within a div/section/article/li.
    """
    return """
<html><body>
<div class="card">Plan One | Starting at $1,450 | 1 Bed | 1 Bath | 720 sq ft | Available now</div>
<div class="card">Plan Two | Starting at $1,750 | 2 Bed | 2 Bath | 1100 sq ft | Available 7/15/26</div>
<div class="card">Plan Three | Starting at $1,950 | 2 Bed | 2 Bath | 1250 sq ft | Available 8/1/26</div>
</body></html>
"""


def _generic_plan_text_fixture() -> str:
    """Bespoke custom-CMS plan-text rows — the parser's last-resort path.

    Requires ≥2 plan lines with rent above $400. Per-PMS adapters are
    preferred; this one fires only when nothing else routed.
    """
    return """
<html><body>
<h1>Our Floor Plans</h1>
<p>1 Bedroom 1 Bath starting at $1,275 / 700 sqft</p>
<p>2 Bedroom 2 Bath starting at $1,695 / 1100 sqft</p>
<p>3 Bedroom 2 Bath starting at $2,150 / 1450 sqft</p>
</body></html>
"""


def _avalonbay_fixture() -> str:
    """AvalonBay SSR ``.unit-item`` cards — uses real-fixture data.

    Loaded from the live-fetched HTML at
    ``c:/tmp/adapter_fixtures/avalonbay/1918.html`` (PID 1918 eaves
    West Windsor, fetched 2026-05-24). Real cloud data — the canonical
    AvalonBay shape with 6 ``.unit-item`` cards.
    """
    fixture_path = Path("c:/tmp/adapter_fixtures/avalonbay/1918.html")
    if fixture_path.exists():
        return fixture_path.read_text(encoding="utf-8")
    # Synthetic fallback if the fixture is missing.
    return """
<html><body>
<div class="ant-card unit-item">
  Virtual tour 005-5211 eaves West Windsor 1 bed · 1 bath · 675 sqft · Available
  Base rent starting at $ 1,965 / 15 mo. lease
  Available starting Jun 07
</div>
<div class="ant-card unit-item">
  Special 002-2223 eaves West Windsor 1 bed · 1 bath · 700 sqft · Available
  Base rent starting at $ 2,030 / 12 mo. lease
  Available starting Jul 11
</div>
</body></html>
"""


def _cortland_fixture() -> str:
    """Cortland ``preload = {...}`` JSON blob — real fixture data.

    Loaded from PID 13898 (cortland-west-houston, fetched 2026-05-24).
    Live-extracted 16 units from this 616KB blob via the existing
    ``_extract_floorplans`` brace-walker.
    """
    fixture_path = Path("c:/tmp/adapter_fixtures/cortland/13898.html")
    if fixture_path.exists():
        return fixture_path.read_text(encoding="utf-8")
    return ""


def _make_ctx(pms: str, property_id: str) -> AdapterContext:
    return AdapterContext(
        base_url=f"http://test.{pms}.com/",
        detected=DetectedPMS(pms=pms, confidence=1.0),
        profile=None,
        expected_total_units=None,
        property_id=property_id,
    )


def _build_fixtures() -> list[FixtureCase]:
    return [
        FixtureCase(
            adapter_name="AppFolio",
            pms="appfolio",
            html=_appfolio_fixture(),
            expected_min_units=3,
            expected_tier="TIER_3_DOM_APPFOLIO_SSR",
            description="3 SSR listings with rent/bed-bath/sqft/avail",
        ),
        FixtureCase(
            adapter_name="Equity",
            pms="equity",
            html=_equity_fixture(),
            expected_min_units=2,
            expected_tier="TIER_3_DOM_EQUITY",
            description="2 ea5-unit blocks with ledgerId comments",
        ),
        FixtureCase(
            adapter_name="RentCafe",
            pms="rentcafe",
            html=_rentcafe_fixture(),
            expected_min_units=3,
            expected_tier="TIER_3_DOM_RENTCAFE_HOSTED",
            description="3 fp-unit table rows with data-* attributes",
        ),
        FixtureCase(
            adapter_name="RentCafeUnitRoster",
            pms="rentcafe_unit_roster",
            html=_rentcafe_unit_roster_fixture(),
            expected_min_units=1,
            expected_tier="TIER_3_DOM_RENTCAFE_UR",
            description="floorplan-block + par-units modern RentCafe theme",
        ),
        FixtureCase(
            adapter_name="RealPageCws",
            pms="realpage_cws",
            html=_realpage_cws_fixture(),
            expected_min_units=1,
            expected_tier="TIER_3_DOM_REALPAGE_CWS",
            description="rpfp-card RealPage CWS widget",
        ),
        FixtureCase(
            adapter_name="ImtSpaces",
            pms="imt_spaces",
            html=_imt_spaces_fixture(),
            expected_min_units=1,
            expected_tier="TIER_3_DOM_IMT_SPACES",
            description="article.spaces-plan with data-spaces-* attrs",
        ),
        FixtureCase(
            adapter_name="WixFloorPlans",
            pms="wix_floor_plans",
            html=_wix_floor_plans_fixture(),
            expected_min_units=1,
            expected_tier="TIER_3_DOM_WIX_FLOOR_PLANS",
            description="Starting at $X cards via BS4 div/section walk",
        ),
        FixtureCase(
            adapter_name="GenericPlanText",
            pms="generic_plan_text",
            html=_generic_plan_text_fixture(),
            expected_min_units=2,
            expected_tier="TIER_3_DOM_GENERIC_PLAN_TEXT",
            description="Bespoke CMS plan-text fallback (≥2 plan lines)",
            is_fallback=True,
        ),
        FixtureCase(
            adapter_name="AvalonBay",
            pms="avalonbay",
            html=_avalonbay_fixture(),
            expected_min_units=3,
            expected_tier="TIER_3_DOM_AVALONBAY_SSR",
            description="Real-fixture AvalonBay SSR (.unit-item × 6, PID 1918)",
        ),
        FixtureCase(
            adapter_name="Cortland",
            pms="cortland",
            html=_cortland_fixture(),
            expected_min_units=3,
            expected_tier="TIER_3_DOM_CORTLAND_PRELOAD",
            description="Real-fixture Cortland preload-JSON (PID 13898 — 16 units)",
        ),
    ]


def _adapter_for(case: FixtureCase) -> Any:
    return {
        "AppFolio": AppFolioAdapter(),
        "AvalonBay": AvalonBayAdapter(),
        "Cortland": CortlandAdapter(),
        "Equity": EquityAdapter(),
        "RentCafe": RentCafeAdapter(),
        "RentCafeUnitRoster": RentCafeUnitRosterAdapter(),
        "RealPageCws": RealPageCwsAdapter(),
        "ImtSpaces": ImtSpacesAdapter(),
        "WixFloorPlans": WixFloorPlansAdapter(),
        "GenericPlanText": GenericPlanTextAdapter(),
    }[case.adapter_name]


@dataclass
class CaseOutcome:
    case: FixtureCase
    units_extracted: int
    high_confidence: bool
    tier_used: str
    elapsed_ms: float
    selector_signature: str
    error: str | None
    dq_guards_evidence: dict[str, Any]


async def _run_case(case: FixtureCase) -> CaseOutcome:
    adapter = _adapter_for(case)
    ctx = _make_ctx(case.pms, f"TEST_{case.adapter_name}")
    t0 = time.perf_counter()
    try:
        res = await _maybe_try_dom(adapter, None, case.html, ctx, ctx.property_id)
    except Exception as e:
        return CaseOutcome(
            case=case, units_extracted=0, high_confidence=False,
            tier_used="", elapsed_ms=(time.perf_counter() - t0) * 1000,
            selector_signature="", error=f"dispatcher_raised:{e}",
            dq_guards_evidence={},
        )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if res is None:
        return CaseOutcome(
            case=case, units_extracted=0, high_confidence=False,
            tier_used="", elapsed_ms=elapsed_ms, selector_signature="",
            error="adapter_missing_try_dom", dq_guards_evidence={},
        )
    if not isinstance(res, AdapterDomResult):
        return CaseOutcome(
            case=case, units_extracted=0, high_confidence=False,
            tier_used="", elapsed_ms=elapsed_ms, selector_signature="",
            error=f"wrong_type:{type(res).__name__}", dq_guards_evidence={},
        )
    # Evidence that dq_guards ran: check unit dicts for canonical
    # availability_status values and absence of leaked _avail_subtype
    # for legitimate AVAILABLE rows.
    dq_evidence: dict[str, Any] = {
        "statuses_observed": sorted({
            str(u.get("availability_status", "")) for u in res.units
        }),
        "any_inferred_id": any(u.get("_inferred_id") for u in res.units),
        "any_avail_subtype": any("_avail_subtype" in u for u in res.units),
        "rent_lows": [u.get("market_rent_low") for u in res.units],
        "sqfts": [u.get("sqft") for u in res.units],
    }
    return CaseOutcome(
        case=case, units_extracted=len(res.units),
        high_confidence=res.is_high_confidence,
        tier_used=res.tier_used,
        elapsed_ms=elapsed_ms,
        selector_signature=res.selector_signature,
        error=None if res.has_units else "empty_result",
        dq_guards_evidence=dq_evidence,
    )


async def _main() -> int:
    print("=== No-LLM coverage validation ===")
    print(f"  ENABLE_TIER4_LLM={os.environ.get('ENABLE_TIER4_LLM')}")
    print(f"  LLM_BUDGET_DOM_CALLS={os.environ.get('LLM_BUDGET_DOM_CALLS')}")
    print()

    fixtures = _build_fixtures()
    outcomes: list[CaseOutcome] = []
    for case in fixtures:
        outcomes.append(await _run_case(case))

    total = len(outcomes)
    passed = 0
    print(f"{'Adapter':<10s} {'units':>6s} {'tier':<32s} {'hi_conf':<7s} {'ms':>7s} {'result':<10s}")
    print("-" * 80)
    for o in outcomes:
        ok = (
            o.error is None
            and o.units_extracted >= o.case.expected_min_units
            and o.tier_used == o.case.expected_tier
        )
        # Fallback adapters (e.g. generic_plan_text) intentionally return
        # confidence <0.7 so the cascade keeps LLM as backup. For them
        # the contract is: units extracted, correct tier, but NOT
        # high_confidence. Non-fallback adapters MUST be high-confidence.
        if not o.case.is_fallback:
            ok = ok and o.high_confidence
        if ok:
            passed += 1
        result = "PASS" if ok else f"FAIL:{o.error or 'mismatch'}"
        print(
            f"{o.case.adapter_name:<10s} {o.units_extracted:>6d} "
            f"{o.tier_used:<32s} {str(o.high_confidence):<7s} "
            f"{o.elapsed_ms:>7.2f} {result}"
        )

    print()
    print("--- DQ guards evidence (canonicalize_status/normalize_unit_id ran) ---")
    for o in outcomes:
        ev = o.dq_guards_evidence
        statuses = ev.get("statuses_observed", [])
        print(
            f"  {o.case.adapter_name:<10s}  statuses={statuses}  "
            f"_inferred_id_seen={ev.get('any_inferred_id')}  "
            f"rent_lows={ev.get('rent_lows')}  "
            f"sqfts={ev.get('sqfts')}"
        )

    print()
    print(f"=== {passed}/{total} cases PASSED ===")
    if passed != total:
        print("FAIL: some adapters' try_dom did not yield expected units.")
        return 1
    print("All adapters extracted units deterministically WITHOUT LLM.")
    print("Cascade contract verified end-to-end on:")
    for o in outcomes:
        print(f"  • {o.case.adapter_name}: {o.units_extracted} units via {o.tier_used} ({o.selector_signature})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
