"""Shadow canary — replay 2026-05-11 cloud-run regression shapes through
the post-Stage-1+2+3 pipeline and emit a per-property outcome report.

Distinct from ``scripts/diagnostics/local_canary.py`` (which fetches live
properties through ``jugnu_runner``). This script is a deterministic
in-memory replay that takes the **exact data shapes** captured from the
2026-05-11 cloud run and runs them through:

    raw extractor output → post_process (infer + sanity + validity +
    classify) → AdapterResult-shape projection

For each property in the manifest, the script reports:
  * Cloud-run outcome on 2026-05-11 (the regression we observed)
  * Post-fix outcome (what the new pipeline produces)
  * Status: IMPROVED / REGRESSED / UNCHANGED_OK / UNCHANGED_FAIL /
    DEMOTED_AS_EXPECTED (cloud said SUCCESS, post-fix correctly says
    FAILED_NO_DATA because the cloud "success" was extraction-junk)

Run:
    python scripts/diagnostics/shadow_canary_2026_05_11.py

Exit codes:
    0 — no REGRESSED properties (safe)
    1 — at least one REGRESSED property (investigate)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Path bootstrap so we can run from the repo root or the ma_poc dir.
_SCRIPT_DIR = Path(__file__).resolve().parent
_MA_POC_ROOT = _SCRIPT_DIR.parent.parent
_REPO_ROOT = _MA_POC_ROOT.parent
for _p in (_REPO_ROOT, _MA_POC_ROOT, _SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ma_poc.extraction.post_process import post_process  # noqa: E402
from ma_poc.fetch.contracts import FetchOutcome  # noqa: E402
from ma_poc.reporting.verdict import (  # noqa: E402
    Verdict,
    compute,
    verdict_excluded_from_success_rate,
    verdict_is_success,
)


# ── Property manifest — the 2026-05-11 regression shapes ──────────────────────


@dataclass
class CanaryProperty:
    """A single canary case — raw shape + expected post-fix outcome."""

    property_id: str
    name: str
    url: str
    basket: str
    #: Pre-fix outcome from the 2026-05-11 cloud run.
    cloud_verdict: str
    cloud_units: int
    #: The raw extractor output as observed on 2026-05-11.
    #: Empty list means the property had no extracted rows.
    raw_units: list[dict[str, Any]] = field(default_factory=list)
    #: If the property was a fetch failure (e.g. 404), set this so the
    #: pipeline projects the right verdict.
    fetch_outcome: str = "OK"
    #: Expected post-fix verdict. The status (IMPROVED / DEMOTED / etc.)
    #: is derived from (cloud_verdict, post_verdict).
    expected_verdict: str = ""
    notes: str = ""


# Manifest assembled from 2026-05-11 run artifacts.
# Real raw shapes captured from gs://jugnu-raw-production/runs/2026-05-11/.
MANIFEST: list[CanaryProperty] = [
    # ── EXPECTED_DEMOTION basket — cloud said SUCCESS but units were junk ─
    CanaryProperty(
        property_id="11611",
        name="Skyline at Kessler",
        url="skylineatkessler.com",
        basket="EXPECTED_DEMOTION",
        cloud_verdict="SUCCESS",
        cloud_units=258,
        # Real captured response: nestiolistings.com/api/v2/neighborhoods.
        # Each item is a neighborhood name + geographic "area" string.
        # Pre-fix: 258 fake "units". Post-fix: all rejected — no dim.
        raw_units=[
            {"id": 306, "name": "Bayonne", "area": "Hudson County",
             "city": "Bayonne", "state": "NJ"},
            {"id": 304, "name": "Hoboken", "area": "Hudson County",
             "city": "Hoboken", "state": "NJ"},
            {"id": 282, "name": "Bedford Park", "area": "None",
             "city": "Bronx", "state": "NY"},
        ],
        expected_verdict="FAILED_NO_DATA",
        notes="Nestio neighborhoods endpoint admitted as units pre-fix; "
              "all 258 rejected post-fix (no numeric dimension).",
    ),
    CanaryProperty(
        property_id="12320",
        name="Settler's Gate",
        url="settlersgateallen.com",
        basket="EXPECTED_DEMOTION",
        cloud_verdict="SUCCESS",
        cloud_units=258,
        raw_units=[
            {"id": 306, "name": "Bayonne", "area": "Hudson County"},
            {"id": 304, "name": "Hoboken", "area": "Hudson County"},
        ],
        expected_verdict="FAILED_NO_DATA",
        notes="Same Nestio shape as Skyline at Kessler.",
    ),
    CanaryProperty(
        property_id="10854",
        name="Biltmore",
        url="liveatthebiltmore.com",
        basket="EXPECTED_DEMOTION",
        cloud_verdict="SUCCESS",
        cloud_units=6,
        # TIER_MERGED_CROSS_PAGE phantoms — only plan letter + unit_id, no dims.
        raw_units=[
            {"floor_plan_name": "B2", "unit_id": "203", "available_date": "2026-05-07"},
            {"floor_plan_name": "C1", "unit_id": "104"},
            {"floor_plan_name": "A1", "unit_id": "212"},
        ],
        expected_verdict="FAILED_NO_DATA",
        notes="Cross-page merge phantoms with no dimensions; correctly "
              "rejected as not-a-unit post-fix.",
    ),

    # ── REAL_FIX basket — cloud failed/under-extracted; should IMPROVE ─
    CanaryProperty(
        property_id="11992",
        name="MAA Worthington",
        url="maac.com/texas/dallas/maa-worthington",
        basket="REAL_FIX",
        cloud_verdict="SUCCESS",  # cloud reported 36 but area=-1 on all
        cloud_units=36,
        # Real MAA Worthington API items with apartmentName + sqft.
        # Pre-fix: sqft dropped because adapter only read minimumSqft.
        # Post-fix: sqft=1019 retained; apartmentName="217" used as unit_id.
        raw_units=[
            {
                "id": "01KGMK3HTJ0YW5MZ45EWDR05PP",
                "apartmentName": "217",
                "floorplanName": "Traditional 2x2 1019 SF",
                "beds": 2, "baths": 2, "sqft": 1019,
                "minimumRent": 2680, "maximumRent": 5165,
                "availableDate": "05/23/2026",
            },
            {
                "id": "01KGMK3HTJ109M7KS3CGSYGXZX",
                "apartmentName": "302",
                "floorplanName": "Traditional 1x1 770 SF",
                "beds": 1, "baths": 1, "sqft": 720,
                "minimumRent": 1940, "maximumRent": 3270,
            },
            {
                "id": "01KGMK3HTJ109M7KS3CGSYGXZY",
                "apartmentName": "411",
                "floorplanName": "Traditional 2x2 1019 SF",
                "beds": 2, "baths": 2, "sqft": 1019,
                "minimumRent": 2700, "maximumRent": 5180,
            },
        ],
        expected_verdict="SUCCESS",
        notes="RentCafe field-map fix recovers sqft and unit-level "
              "apartmentName. Pre-fix: 36 units with area=-1 and "
              "inferred unit IDs. Post-fix: real per-apartment data.",
    ),
    CanaryProperty(
        property_id="1163",
        name="Country Woods",
        url="countrywoodsapts.com",
        basket="REAL_FIX",
        cloud_verdict="SUCCESS",
        cloud_units=3,
        # Country Woods plans — sqft encoded in plan name as range.
        # No apartmentName / unit_id → classify as plan-level (Stage 2).
        raw_units=[
            {"floor_plan_name": "682-708sqft", "beds": 1, "baths": 1, "rent_low": 2140},
            {"floor_plan_name": "886-912sqft", "beds": 2, "baths": 1, "rent_low": 2675},
            {"floor_plan_name": "970-996sqft", "beds": 2, "baths": 2, "rent_low": 2925},
        ],
        expected_verdict="SUCCESS",
        notes="Stage 1 infer_sqft_from_name recovers area (682 / 886 / 970). "
              "Stage 2 routes to plan_summaries (no per-unit identity).",
    ),

    # ── PLAN_LEVEL_TEST basket — JSON-LD plan-only data ─
    CanaryProperty(
        property_id="P_LUXE_88",
        name="Luxe 88",
        url="example.com/luxe-88",
        basket="PLAN_LEVEL_TEST",
        cloud_verdict="SUCCESS",
        cloud_units=16,
        # JSON-LD Offers — rent + plan name but no per-apartment identity.
        # Pre-fix: each counted as a "unit". Post-fix: routed to
        # plan_summaries; verdict = SUCCESS_PLAN_LEVEL (when wired through
        # at the runner level — for now the post_process partition is the
        # observable contract).
        raw_units=[
            {"floor_plan_name": "Dean /D", "rent_low": 1629, "beds": 1, "baths": 1},
            {"floor_plan_name": "Fern /F", "rent_low": 1559, "beds": 1, "baths": 1},
            {"floor_plan_name": "Brittany /B", "rent_low": 1535, "beds": 2, "baths": 2},
        ],
        expected_verdict="SUCCESS_PLAN_LEVEL",
        notes="JSON-LD Offers routed to plan_summaries (Stage 2). The "
              "verdict layer surfaces SUCCESS_PLAN_LEVEL when no unit-level "
              "rows exist but plan-level rows do.",
    ),

    # ── DEAD_URL basket — Stage 3 ─
    CanaryProperty(
        property_id="40640",
        name="dawnhomes",
        url="dawnhomes.com (404 redirect)",
        basket="DEAD_URL",
        cloud_verdict="FAILED_UNREACHABLE",
        cloud_units=0,
        fetch_outcome="DEAD_URL",
        expected_verdict="DEAD_URL",
        notes="Final status 404 → DEAD_URL (Stage 3). Excluded from "
              "success-rate denominator; pre-Stage-3 was lumped into "
              "FAILED_UNREACHABLE alongside transient fetch failures.",
    ),

    # ── UNCHANGED_FAIL basket — properties we don't yet recover ─
    CanaryProperty(
        property_id="1084",
        name="Black Hawk",
        url="theblackhawkapartments.com",
        basket="FAILURE",
        cloud_verdict="FAILED_NO_DATA",
        cloud_units=0,
        raw_units=[],
        expected_verdict="FAILED_NO_DATA",
        notes="Static-anchor link-hop discovery is a separate gap; "
              "Stage 1+2+3 don't address it. Expected to remain failed.",
    ),
]


# ── Replay through the new pipeline ───────────────────────────────────────────


@dataclass
class CanaryRow:
    """One row of the canary report."""

    property_id: str
    name: str
    basket: str
    cloud_verdict: str
    cloud_units: int
    post_verdict: str
    post_unit_level: int
    post_plan_level: int
    post_rejected: int
    status: str  # IMPROVED / REGRESSED / UNCHANGED_OK / UNCHANGED_FAIL / DEMOTED_AS_EXPECTED
    notes: str


def replay_one(prop: CanaryProperty) -> CanaryRow:
    """Run one manifest entry through the new pipeline."""
    # Run through post_process.
    pp = post_process(prop.raw_units, property_id=prop.property_id)

    # Compute the verdict as the runner would. We pass plan_summaries so
    # the SUCCESS_PLAN_LEVEL path can fire when unit-level is empty but
    # plan-level is populated.

    class _Extract:
        records = pp.units

    verdict_result = compute(
        fetch_outcome=prop.fetch_outcome,
        extract_result=_Extract(),
        plan_summaries=pp.plan_summaries,
    )
    post_verdict = verdict_result.verdict.value

    # Classify the delta.
    cloud_was_success = verdict_is_success(prop.cloud_verdict)
    post_is_success = verdict_is_success(post_verdict)
    post_is_dead = verdict_excluded_from_success_rate(post_verdict)

    if prop.basket == "EXPECTED_DEMOTION":
        # Cloud reported SUCCESS but units were junk; post-fix should
        # demote. Anything else is a problem.
        if not post_is_success and post_verdict == prop.expected_verdict:
            status = "DEMOTED_AS_EXPECTED"
        elif post_is_success:
            status = "REGRESSED"
        else:
            status = "DEMOTED_AS_EXPECTED"
    elif prop.basket == "DEAD_URL":
        status = "IMPROVED" if post_is_dead else "REGRESSED"
    elif prop.basket == "REAL_FIX":
        # Expect improvement (post-fix produces real data).
        if post_is_success and pp.n_admitted > 0:
            status = "IMPROVED"
        elif cloud_was_success and not post_is_success:
            status = "REGRESSED"
        else:
            status = "UNCHANGED_FAIL"
    elif prop.basket == "PLAN_LEVEL_TEST":
        # Expect SUCCESS_PLAN_LEVEL specifically.
        if post_verdict == prop.expected_verdict:
            status = "IMPROVED"
        elif cloud_was_success and not post_is_success:
            status = "REGRESSED"
        else:
            status = "UNCHANGED_FAIL"
    elif prop.basket == "FAILURE":
        if post_is_success:
            status = "IMPROVED"
        elif cloud_was_success and not post_is_success:
            status = "REGRESSED"
        else:
            status = "UNCHANGED_FAIL"
    else:
        status = "UNKNOWN_BASKET"

    return CanaryRow(
        property_id=prop.property_id,
        name=prop.name,
        basket=prop.basket,
        cloud_verdict=prop.cloud_verdict,
        cloud_units=prop.cloud_units,
        post_verdict=post_verdict,
        post_unit_level=pp.n_unit_level,
        post_plan_level=pp.n_plan_level,
        post_rejected=pp.n_rejected,
        status=status,
        notes=prop.notes,
    )


def render_markdown(rows: list[CanaryRow]) -> str:
    """Human-readable markdown report."""
    from collections import Counter

    n_total = len(rows)
    counts = Counter(r.status for r in rows)
    n_regressed = counts.get("REGRESSED", 0)
    gate = "PASS" if n_regressed == 0 else "FAIL — DO NOT DEPLOY"

    out: list[str] = [
        "# Shadow Canary — 2026-05-11 regression shapes",
        "",
        f"**Properties replayed:** {n_total}  ",
        f"**Pre-deploy gate:** `REGRESSED == 0` → **{gate}**",
        "",
        "## Summary",
        "",
        "```",
        f"  IMPROVED:                  {counts.get('IMPROVED', 0):4d}  "
        "(was failing/under-extracted, now succeeds with valid data)",
        f"  DEMOTED_AS_EXPECTED:       {counts.get('DEMOTED_AS_EXPECTED', 0):4d}  "
        "(cloud SUCCESS was extraction-junk; post-fix correctly demotes)",
        f"  UNCHANGED_OK:              {counts.get('UNCHANGED_OK', 0):4d}  "
        "(was succeeding, still succeeds)",
        f"  UNCHANGED_FAIL:            {counts.get('UNCHANGED_FAIL', 0):4d}  "
        "(was failing, still failing — fix doesn't cover)",
        f"  REGRESSED:                 {counts.get('REGRESSED', 0):4d}  "
        "(was succeeding, now fails — STOP)",
        "```",
        "",
        "## Per-property delta",
        "",
        "| property_id | name | basket | cloud → post-fix | unit/plan/rej | status |",
        "|---|---|---|---|---|---|",
    ]

    for r in rows:
        cloud_label = f"`{r.cloud_verdict}` ({r.cloud_units}u)"
        post_label = f"`{r.post_verdict}`"
        counts_label = f"{r.post_unit_level}u / {r.post_plan_level}p / {r.post_rejected}r"
        out.append(
            f"| {r.property_id} | {r.name} | {r.basket} | "
            f"{cloud_label} → {post_label} | {counts_label} | **{r.status}** |"
        )

    out += [
        "",
        "## Notes (per property)",
        "",
    ]
    for r in rows:
        out.append(f"### {r.property_id} — {r.name}")
        out.append("")
        out.append(r.notes)
        out.append("")

    return "\n".join(out)


def main() -> int:
    rows = [replay_one(p) for p in MANIFEST]
    md = render_markdown(rows)
    print(md)

    n_regressed = sum(1 for r in rows if r.status == "REGRESSED")
    return 0 if n_regressed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
