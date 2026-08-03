#!/usr/bin/env python3
"""Build the zero-cost, deterministic affected-property canary manifest.

This script only reads repository files and writes local artifacts.  It never
builds an image, contacts GCP, uploads a CSV, or starts a job.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DEFAULT_OUTPUT = HERE / "affected-property-manifest-v1"
PROPERTIES_CSV = REPO_ROOT / "ma_poc/config/properties.csv"
FINDINGS_MD = HERE / "ADAPTER_DATA_QUALITY_FINDINGS.md"
MANIFEST_VERSION = "affected-property-manifest-v1"
AUDIT_SNAPSHOT_DATE = "2026-08-02"
PROPERTY_COLUMNS = ("apartmentid", "name", "address", "city", "state", "zip", "website")
GENERATED_FILES = (
    "affected_properties.jsonl",
    "affected_properties.csv",
    "finding_coverage.json",
    "future_launch_contract.json",
    "launch_index.csv",
    "launch_properties.csv",
    "manifest_summary.json",
    "README.md",
    "SHA256SUMS.json",
)


def _ids(value: str) -> tuple[str, ...]:
    return tuple(part for part in value.replace("\n", " ").split() if part)


UDR_20 = _ids(
    "2709 2958 3749 4752 8179 12355 24366 26094 30751 36972 37716 40566 "
    "66998 72593 95149 228582 230081 40989 2957 36925"
)
IRVINE_13 = _ids(
    "738 16387 16758 16796 17102 17178 17320 26901 32561 34548 230542 231107 263158"
)
ONSITE_JULY_SUPERSET = _ids(
    "1165 4724 12927 15361 16101 16198 16953 17161 17541 19108 20349 "
    "21169 23306 26946 27161 28372 28386 28538 28696 28728 33318 34275 "
    "37577 37712 39387 41973 44765 46561 52967 55194 57918 58341 62178 "
    "63933 64618 65134 69323 71651 72192 72374 74193 78373 224538 "
    "227366 246304 250342 253383 256972 257779 261648 262518 265383 "
    "266348 274909 278167 286725 290807 291337 294552"
)
EQUITY_25 = _ids(
    "2955 7797 8418 13153 24542 24998 27574 30657 32336 32995 33986 "
    "35540 35919 37919 37920 38733 40710 71558 118445 221098 228359 "
    "232480 245501 255578 265281"
)
ESSEX_27 = _ids(
    "12586 24180 12985 21149 3748 229748 27577 2405 63770 2587 24315 "
    "2537 77713 13181 21381 17984 226350 2282 20050 94295 19995 63771 "
    "31197 12963 15579 37721 23143"
)
FORTRESSTECH_10 = _ids(
    "1752 50746 58156 61951 67516 220976 234936 269985 291150 296916"
)
RS365_10 = _ids("16196 34909 39573 60939 63462 217605 52348 16377 1777 246152")
ASPEN_8 = _ids("16186 6526 14907 47710 4079 14934 24049 48514")
MARKETAPTS_29 = _ids(
    "3379 3535 5119 13305 13756 14336 14596 16961 18752 19940 23549 "
    "26194 27999 28391 30723 46986 54769 56750 68991 69928 73916 "
    "217956 224788 225934 242344 255737 269136 284598 49248"
)
MRI_DIRECT_8 = _ids("264397 260340 19396 9133 48769 29583 235871 12370")
CAMDEN_16 = _ids(
    "1740 7383 14062 15064 15332 24648 26730 30997 35256 37545 38541 "
    "62512 78724 227989 229295 234916"
)


@dataclass(frozen=True)
class FindingSpec:
    finding_id: int
    adapter: str
    affected_ids: tuple[str, ...]
    control_ids: tuple[str, ...]
    tests: tuple[str, ...]
    acceptance_contract: str
    coverage_mode: str = "explicit_audit_scope"
    disposition: str = "confirmed_remediated"


def _spec(
    finding_id: int,
    adapter: str,
    affected: str | Iterable[str],
    test: str | Iterable[str],
    acceptance: str,
    *,
    controls: str | Iterable[str] = (),
    coverage: str = "explicit_audit_scope",
    disposition: str = "confirmed_remediated",
) -> FindingSpec:
    def as_tuple(value: str | Iterable[str]) -> tuple[str, ...]:
        return _ids(value) if isinstance(value, str) else tuple(value)

    tests = (test,) if isinstance(test, str) else tuple(test)
    return FindingSpec(
        finding_id=finding_id,
        adapter=adapter,
        affected_ids=as_tuple(affected),
        control_ids=as_tuple(controls),
        tests=tests,
        acceptance_contract=acceptance,
        coverage_mode=coverage,
        disposition=disposition,
    )


SPECS = (
    _spec(
        1,
        "rentcafe_applicant",
        "234581 239094 240595",
        "ma_poc/tests/pms/adapters/test_rentcafe_applicant_recovery.py",
        "Inquiry-only plans remain plan-level UNKNOWN and never become available units.",
    ),
    _spec(
        2,
        "avalonbay",
        "26892 36964 262540",
        "ma_poc/tests/pms/adapters/test_avalonbay_html.py",
        "Every native Avalon apartment ID survives even when visible unit labels repeat.",
    ),
    _spec(
        3,
        "entrata",
        "19299 34482 257328 36173",
        "ma_poc/tests/pms/adapters/test_entrata_pp_building_identity.py",
        "Building/address and native apartment identity disambiguate repeated visible numbers.",
    ),
    _spec(
        4,
        "udr",
        (),
        "ma_poc/tests/pms/adapters/test_udr_jsonld.py",
        "Current UDR identity remains collision-free.",
        controls="2958 8179 30751",
        disposition="cleared_control",
    ),
    _spec(
        5,
        "knock",
        (),
        "ma_poc/tests/pms/adapters/test_knock.py",
        "Current Knock UUID identity remains collision-free.",
        controls="1783 221319 281928 2305",
        disposition="cleared_control",
    ),
    _spec(
        6,
        "appfolio",
        "241643",
        "ma_poc/tests/pms/adapters/test_appfolio_wisconsin_grid_identity.py",
        "Wisconsin grid addresses and native listing IDs keep all physical apartments distinct.",
    ),
    _spec(
        7,
        "venterra",
        "48177 30237 14524 33327",
        "ma_poc/tests/pms/adapters/test_venterra.py",
        "Native unit code is canonical; the short public label remains display metadata.",
    ),
    _spec(
        8,
        "cortland",
        "3181 255134 34500",
        "ma_poc/tests/pms/adapters/test_cortland_cards.py",
        "Native apartment and building identity survive both modern and legacy Cortland cards.",
    ),
    _spec(
        9,
        "cortland",
        "3181 34500 2982 62782",
        "ma_poc/tests/pms/adapters/test_cortland_cards.py",
        "Base rent remains separate from fee-inclusive total monthly rent.",
    ),
    _spec(
        10,
        "g5",
        "3785 35934 33267",
        "ma_poc/tests/pms/adapters/test_g5.py",
        "Native apartment identity wins over repeated floor-plan type labels.",
    ),
    _spec(
        11,
        "harbor_group",
        "67524",
        (
            "ma_poc/tests/pms/adapters/test_harbor_group.py",
            "ma_poc/tests/scripts/test_availability_date_contract.py",
        ),
        "Every explicit Harbor future date survives the production formatter.",
        controls="30734 4944",
    ),
    _spec(
        12,
        "onesite_workflow",
        "14538 16078 14155",
        "ma_poc/tests/pms/adapters/test_mark_taylor.py",
        "Only the configured Mark-Taylor property roster may be admitted.",
    ),
    _spec(
        13,
        "securecafe",
        "43097 54743 239318 60386",
        "ma_poc/tests/pms/adapters/test_rentcafe_rc2.py",
        "Native SecureCafe UnitID is canonical when published.",
        controls="232538",
    ),
    _spec(
        14,
        "generic_spherexx",
        "70255 259386 281149",
        "ma_poc/tests/pms/adapters/test_spherexx.py",
        "Building/native identity is applied before deduplication.",
    ),
    _spec(
        15,
        "shared_formatter",
        "37143 56166 14581 258143",
        "ma_poc/tests/pms/adapters/test_api_parser_available_flag.py",
        "Explicitly unavailable rows without a date never receive the capture date.",
    ),
    _spec(
        16,
        "udr",
        UDR_20,
        (
            "ma_poc/tests/pms/adapters/test_udr_jsonld.py",
            "ma_poc/tests/scripts/test_availability_date_contract.py",
        ),
        "Join UDR dates by native apartment ID first; preserve all source dates and existing canonical IDs.",
        coverage="complete_20_property_cohort",
    ),
    _spec(
        17,
        "g5",
        "238944",
        "ma_poc/tests/pms/adapters/test_g5.py",
        "Repair zero dimensions only when explicit non-studio plan tokens prove the value.",
        controls="37983 37972",
    ),
    _spec(
        18,
        "securecafe",
        (),
        "ma_poc/tests/pms/adapters/test_rentcafe_unit_roster.py",
        "Do not invent future dates absent from the current public source.",
        controls="261530 250124 60750 5974 4904",
        disposition="cleared_control",
    ),
    _spec(
        19,
        "jonah_ssr",
        "230770",
        "ma_poc/tests/pms/adapters/test_jonah_ssr_recovery.py",
        "Equivalent URL spellings are crawled once and merge by immutable apartment identity.",
    ),
    _spec(
        20,
        "rentmanager_iloveleasing",
        "78381",
        "ma_poc/tests/pms/adapters/test_iloveleasing_table.py",
        "Retain native detail ID and street-qualified identity for every Rose Park row.",
    ),
    _spec(
        21,
        "rentvision",
        "75722",
        "ma_poc/tests/pms/adapters/test_rentvision_building_identity.py",
        "Retain Apply UnitId and building; repeated short apartment labels remain distinct.",
    ),
    _spec(
        22,
        "resman",
        "37143 56151 243936",
        "ma_poc/tests/pms/adapters/test_resman.py",
        "Join full roster and availability surfaces by immutable identity, never mutable rent.",
    ),
    _spec(
        23,
        "sightmap",
        (),
        "ma_poc/tests/pms/adapters/test_sightmap_direct_probe.py",
        "Direct SightMap source-to-final identity and value mapping remains exact.",
        controls="279758 32746 288891",
        disposition="cleared_control",
    ),
    _spec(
        24,
        "apts247",
        "64390 9168 31564 68313",
        "ma_poc/tests/pms/adapters/test_apts247.py",
        "Stable native PMS identity is canonical for numbered apartments.",
    ),
    _spec(
        25,
        "funnel_spaces",
        "119144 58969 26967",
        "ma_poc/tests/pms/adapters/test_funnel_spaces_identity.py",
        "Native property, plan, and unit IDs survive final formatting.",
    ),
    _spec(
        26,
        "realpage_getunits",
        (),
        "ma_poc/tests/pms/adapters/test_realpage_onlineleasing_getunits.py",
        "Native IDs and explicit dates on public GetUnits routes remain exact.",
        controls="25781 11317 279103 39346 293332 258135",
        disposition="cleared_control",
    ),
    _spec(
        27,
        "repli360",
        "21347 2598 2594 16969 14117 38525",
        "ma_poc/tests/pms/adapters/test_repli360.py",
        "Waitlist sentinels are excluded and native unit IDs are retained.",
    ),
    _spec(
        28,
        "maac",
        "6194 52140 12525 232992 218985 54550",
        "ma_poc/tests/pms/adapters/test_maac_html.py",
        "MAAC native identity survives without changing exact values or dates.",
    ),
    _spec(
        29,
        "encore_jonah",
        "276734 288502 295254 253388 274384 278113",
        "ma_poc/tests/pms/adapters/test_encoreskyline_units.py",
        "SSR and resource routes preserve the same property-bound native identity.",
    ),
    _spec(
        30,
        "irvine",
        IRVINE_13,
        "ma_poc/tests/pms/adapters/test_irvine_identity.py",
        "Community-qualified native unit identity prevents cross-community collisions.",
        coverage="complete_13_property_cohort",
    ),
    _spec(
        31,
        "amli",
        "261770 37386 62778 54553 237704 61548 239952 242191 68148 66940 40193",
        "ma_poc/tests/pms/adapters/test_amli_property_binding.py",
        "Only target-property AMLI units are admitted and exact unit fields survive.",
        coverage="complete_11_property_audit_cohort",
    ),
    _spec(
        32,
        "onsite_apply",
        ONSITE_JULY_SUPERSET,
        "ma_poc/tests/pms/adapters/test_onsite_apply.py",
        "Native On-Site ID, baths, exact proven plan, dates, and property binding survive.",
        controls="14956 32793",
        coverage="deterministic_july_success_superset_plus_current_no_link_controls",
    ),
    _spec(
        33,
        "equity",
        EQUITY_25,
        "ma_poc/tests/pms/adapters/test_equity.py",
        "Property-scoped buildingId:unitId is canonical and all source values remain exact.",
        controls="218378",
        coverage="complete_26_property_current_cohort",
    ),
    _spec(
        34,
        "essex",
        ESSEX_27,
        "ma_poc/tests/pms/adapters/test_essex.py",
        "Native unit/floor-plan IDs and property request binding survive; empty exits are observable.",
        coverage="complete_27_property_current_cohort",
    ),
    _spec(
        35,
        "fortresstech",
        "296916",
        "ma_poc/tests/pms/adapters/test_fortresstech.py",
        "Trusted structured 282-sq-ft studio area survives without weakening general clamps.",
        controls=tuple(pid for pid in FORTRESSTECH_10 if pid != "296916"),
        coverage="complete_10_property_cohort",
    ),
    _spec(
        36,
        "residentservices365",
        RS365_10,
        "ma_poc/tests/pms/adapters/test_residentservices365.py",
        "Exact plan/floor/term and coherent Best Value tuple survive; visible Now overrides stale epochs.",
        coverage="complete_10_property_cohort",
    ),
    _spec(
        37,
        "rentaladdress",
        "218586",
        "ma_poc/tests/pms/adapters/test_rentaladdress.py",
        "Inquiry-only plan cards retain exact values but no fabricated availability date.",
    ),
    _spec(
        38,
        "aspensquare",
        ASPEN_8,
        "ma_poc/tests/pms/adapters/test_aspensquare.py",
        "Stable UUID plus public label/building/human plan survive and future offerings stay available.",
        coverage="complete_8_property_cohort",
    ),
    _spec(
        39,
        "edificecms",
        "12377 19357 20977 222652 37388",
        "ma_poc/tests/pms/adapters/test_edificecms.py",
        "Future on-notice rows remain available and only property-bound plan catalogues merge.",
        coverage="complete_5_property_cohort",
    ),
    _spec(
        40,
        "marketapts",
        "13756 73916",
        "ma_poc/tests/pms/adapters/test_marketapts.py",
        "Authoritative MarketApts plan channel suppresses degraded generic rows; deposits never become rent.",
        controls=tuple(pid for pid in MARKETAPTS_29 if pid not in {"13756", "73916"}),
        coverage="complete_29_property_cohort",
    ),
    _spec(
        41,
        "mri_prospectconnect",
        "235871 540",
        "ma_poc/tests/pms/adapters/test_mri_prospectconnect.py",
        "Preserve both rent-range endpoints and prefer an exactly property-bound MRI route.",
        controls=tuple(pid for pid in MRI_DIRECT_8 if pid != "235871"),
        coverage="complete_9_property_attributed_cohort",
    ),
    _spec(
        42,
        "rentcafe_layout_tab",
        "11805 6281 281751 37360 268592 1156 23367 254167",
        "ma_poc/tests/pms/adapters/test_rentcafe_layout_tab.py",
        "Union exact plan drills by native apartment identity and let exact plan semantics win.",
        controls="1084 3811 27125 253774",
        coverage="complete_12_property_cohort",
    ),
    _spec(
        43,
        "wix_floor_plans",
        "47909 262964",
        "ma_poc/tests/pms/adapters/test_wix_floor_plans.py",
        "Recover authored Wix plans and only explicitly linked, identity-bound physical rosters.",
        controls="220345",
    ),
    _spec(
        44,
        "camden",
        CAMDEN_16,
        "ma_poc/tests/pms/adapters/test_camden.py",
        "Exact catalogue/plan drills produce every community-qualified unit; suggestion cross-products are forbidden.",
        coverage="complete_16_property_cohort",
    ),
    _spec(
        45,
        "squarespace_nopms",
        "241432 56903 68505 61950",
        "ma_poc/tests/pms/adapters/test_squarespace_authored_routes.py",
        "Recover authored property-bound rosters and reject provider placeholders.",
        controls="57195 280355",
        coverage="complete_6_property_cohort",
    ),
    _spec(
        46,
        "thinkreside",
        "271195 51921",
        "ma_poc/tests/pms/adapters/test_thinkreside.py",
        "Current towncommunity shapes remain complete; visible Now provenance and exact plan cards survive.",
    ),
    _spec(
        47,
        "wix_nopms",
        "19538 67150 69203 217343 240745 254556 276351 23963 34523 36268 37805 23494 71345 282696 271721",
        "ma_poc/tests/pms/adapters/test_wix_iframe_walker.py",
        "Provider routes stay identity-bound; recoverable plan/unit routes emit exact rows without waitlist or placeholder records.",
        controls="46179 118965 263732",
        coverage="complete_18_property_cohort",
    ),
    _spec(
        48,
        "yotta",
        "35349 15049 34785",
        "ma_poc/tests/pms/adapters/test_yotta.py",
        "Provider plan IDs, floor ordinals, literal Today provenance, future dates, and response lineage survive.",
    ),
    _spec(
        49,
        "non_registry",
        "55709 1765 38378 261580",
        (
            "ma_poc/tests/pms/adapters/test_betternoi_public.py",
            "ma_poc/tests/pms/adapters/test_nesthub_public.py",
            "ma_poc/tests/pms/adapters/test_showmojo_public.py",
            "ma_poc/tests/pms/adapters/test_static_residence_table.py",
        ),
        "Each exact non-registry route stays property-bound and preserves physical rows, future dates, and authored plans.",
    ),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_properties() -> dict[str, dict[str, str]]:
    with PROPERTIES_CSV.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if tuple(rows[0]) != PROPERTY_COLUMNS:
        raise ValueError(f"unexpected property columns: {tuple(rows[0])}")
    result = {row["apartmentid"]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("properties.csv has duplicate apartmentid values")
    return result


def _load_headings() -> dict[int, tuple[str, int]]:
    headings: dict[int, tuple[str, int]] = {}
    for line_number, line in enumerate(
        FINDINGS_MD.read_text(encoding="utf-8").splitlines(), start=1
    ):
        match = re.match(r"^## (\d+)\. (.+)$", line)
        if match:
            headings[int(match.group(1))] = (match.group(2), line_number)
    return headings


def _validate(
    properties: dict[str, dict[str, str]], headings: dict[int, tuple[str, int]]
) -> None:
    expected = set(range(1, 50))
    actual = {spec.finding_id for spec in SPECS}
    if actual != expected or len(SPECS) != 49:
        raise ValueError(
            f"finding coverage mismatch: missing={sorted(expected - actual)} extra={sorted(actual - expected)}"
        )
    if set(headings) != expected:
        raise ValueError("findings markdown does not contain exactly headings 1..49")
    for spec in SPECS:
        ids = (*spec.affected_ids, *spec.control_ids)
        if not ids:
            raise ValueError(f"finding {spec.finding_id} has no deterministic property")
        if len(ids) != len(set(ids)):
            raise ValueError(f"finding {spec.finding_id} repeats a property ID")
        missing = sorted(set(ids) - set(properties), key=int)
        if missing:
            raise ValueError(
                f"finding {spec.finding_id} has IDs absent from properties.csv: {missing}"
            )
        for selector in spec.tests:
            if not (REPO_ROOT / selector).is_file():
                raise ValueError(
                    f"finding {spec.finding_id} test selector does not exist: {selector}"
                )


def _manifest_rows(
    properties: dict[str, dict[str, str]], headings: dict[int, tuple[str, int]]
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    evidence_file = str(FINDINGS_MD.relative_to(REPO_ROOT))
    for spec in SPECS:
        title, line = headings[spec.finding_id]
        for role, property_ids in (
            ("affected", spec.affected_ids),
            ("regression_control", spec.control_ids),
        ):
            for property_id in sorted(property_ids, key=int):
                property_row = properties[property_id]
                rows.append(
                    {
                        "finding_id": spec.finding_id,
                        "finding_title": title,
                        "disposition": spec.disposition,
                        "coverage_mode": spec.coverage_mode,
                        "property_role": role,
                        "adapter": spec.adapter,
                        "canonical_id": property_id,
                        "name": property_row["name"],
                        "address": property_row["address"],
                        "city": property_row["city"],
                        "state": property_row["state"],
                        "zip": property_row["zip"],
                        "website": property_row["website"],
                        "acceptance_contract": spec.acceptance_contract,
                        "evidence_file": evidence_file,
                        "evidence_line": line,
                        "test_selectors": list(spec.tests),
                    }
                )
    return rows


def _write_csv(
    path: Path, rows: list[dict[str, object]], fieldnames: Iterable[str]
) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=tuple(fieldnames),
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            encoded = dict(row)
            if isinstance(encoded.get("test_selectors"), list):
                encoded["test_selectors"] = "|".join(encoded["test_selectors"])
            writer.writerow(encoded)


def build(output_dir: Path) -> dict[str, object]:
    properties = _load_properties()
    headings = _load_headings()
    _validate(properties, headings)
    rows = _manifest_rows(properties, headings)
    output_dir.mkdir(parents=True, exist_ok=True)

    jsonl_path = output_dir / "affected_properties.jsonl"
    jsonl_path.write_text(
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    manifest_fields = (
        "finding_id",
        "finding_title",
        "disposition",
        "coverage_mode",
        "property_role",
        "adapter",
        "canonical_id",
        "name",
        "address",
        "city",
        "state",
        "zip",
        "website",
        "acceptance_contract",
        "evidence_file",
        "evidence_line",
        "test_selectors",
    )
    _write_csv(output_dir / "affected_properties.csv", rows, manifest_fields)

    by_property: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_property[str(row["canonical_id"])].append(row)
    property_ids = sorted(by_property, key=int)
    launch_rows = [properties[property_id] for property_id in property_ids]
    _write_csv(output_dir / "launch_properties.csv", launch_rows, PROPERTY_COLUMNS)

    index_rows: list[dict[str, object]] = []
    for property_id in property_ids:
        linked = by_property[property_id]
        index_rows.append(
            {
                **properties[property_id],
                "finding_ids": "|".join(str(row["finding_id"]) for row in linked),
                "property_roles": "|".join(str(row["property_role"]) for row in linked),
                "adapters": "|".join(
                    dict.fromkeys(str(row["adapter"]) for row in linked)
                ),
            }
        )
    _write_csv(
        output_dir / "launch_index.csv",
        index_rows,
        (*PROPERTY_COLUMNS, "finding_ids", "property_roles", "adapters"),
    )

    coverage_rows = []
    for spec in SPECS:
        title, line = headings[spec.finding_id]
        coverage_rows.append(
            {
                "finding_id": spec.finding_id,
                "title": title,
                "disposition": spec.disposition,
                "coverage_mode": spec.coverage_mode,
                "adapter": spec.adapter,
                "affected_property_count": len(spec.affected_ids),
                "control_property_count": len(spec.control_ids),
                "evidence_line": line,
                "test_selectors": list(spec.tests),
                "acceptance_contract": spec.acceptance_contract,
            }
        )
    coverage = {
        "manifest_version": MANIFEST_VERSION,
        "finding_ids": list(range(1, 50)),
        "all_findings_represented": True,
        "findings": coverage_rows,
    }
    (output_dir / "finding_coverage.json").write_text(
        json.dumps(coverage, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    # Freeze configuration only. This file is deliberately not a deployable
    # Cloud Run job definition, and the builder never contacts GCP. Reserving
    # one of the existing three HB calls for a property-bound route does not
    # increase the per-property paid-session ceiling.
    future_launch_contract = {
        "manifest_version": MANIFEST_VERSION,
        "launch_authorized": False,
        "build_or_deploy_performed": False,
        "job_started": False,
        "property_input": "launch_properties.csv",
        "property_count": len(property_ids),
        "profile_gate": "strict_profile_materializer_v3_identity_ADMIT_only",
        "environment": {
            "COMPLIANCE_MODE": "1",
            "ENABLE_UNLOCKER_TIER": "false",
            "FETCH_BACKEND": "hyperbrowser",
            "HYPERBROWSER_MAX_CALLS_PER_PROPERTY": "3",
            "HYPERBROWSER_RESERVED_PRIORITY_CALLS": "1",
        },
        "required_outputs": [
            "raw_source_count",
            "parser_count",
            "formatted_count",
            "final_admitted_count",
            "canonical_id_uniqueness",
            "property_identity_verdict",
            "availability_date_provenance",
            "unit_source_provenance",
        ],
    }
    (output_dir / "future_launch_contract.json").write_text(
        json.dumps(future_launch_contract, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    summary = {
        "manifest_version": MANIFEST_VERSION,
        "audit_snapshot_date": AUDIT_SNAPSHOT_DATE,
        "zero_cost_local_only": True,
        "build_or_deploy_performed": False,
        "job_started": False,
        "finding_count": len(SPECS),
        "finding_ids": list(range(1, 50)),
        "all_findings_represented": True,
        "manifest_row_count": len(rows),
        "unique_launch_property_count": len(property_ids),
        "affected_row_count": sum(row["property_role"] == "affected" for row in rows),
        "control_row_count": sum(
            row["property_role"] == "regression_control" for row in rows
        ),
        "coverage_modes": dict(
            sorted(Counter(spec.coverage_mode for spec in SPECS).items())
        ),
        "dispositions": dict(
            sorted(Counter(spec.disposition for spec in SPECS).items())
        ),
        "inputs": {
            str(PROPERTIES_CSV.relative_to(REPO_ROOT)): _sha256(PROPERTIES_CSV),
            str(FINDINGS_MD.relative_to(REPO_ROOT)): _sha256(FINDINGS_MD),
            str(Path(__file__).resolve().relative_to(REPO_ROOT)): _sha256(
                Path(__file__).resolve()
            ),
        },
    }
    (output_dir / "manifest_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    readme = f"""# Deterministic affected-property manifest

This is a local, zero-cost launch input generated from the 2026-08-02 audit.
It contains **{len(property_ids)} unique properties** and **{len(rows)} property/finding rows**.
All 49 findings are represented, including cleared findings through explicit
regression controls.

- `launch_properties.csv` is the exact seven-column future job input.
- `launch_index.csv` explains each launch property's linked findings and roles.
- `affected_properties.jsonl` and `.csv` are the traceable property/finding ledger.
- `finding_coverage.json` proves findings 1-49 are represented and records the
  acceptance contract, evidence line, and local test selectors.
- `future_launch_contract.json` freezes the no-launch state, compliance flags,
  three-call Hyperbrowser ceiling, and one reserved exact-route slot.
- `manifest_summary.json` pins all source hashes and confirms no build, deploy,
  upload, or job launch occurred.
- `SHA256SUMS.json` pins every generated artifact except itself.

Finding 32 deliberately uses the deterministic July On-Site success superset
plus the two named current no-link controls. The live audit measured a moving
49-property attribution set but did not save that exact scan ledger; using the
superset avoids silently dropping a previously affected property.

Rebuild locally:

```bash
python investigations/2026-08-01-consolidated-canary/build_affected_property_manifest.py
```

Verify byte-for-byte determinism:

```bash
python investigations/2026-08-01-consolidated-canary/build_affected_property_manifest.py --check
```

Neither command contacts GCP or any property website.
"""
    (output_dir / "README.md").write_text(readme, encoding="utf-8")

    checksum_names = [name for name in GENERATED_FILES if name != "SHA256SUMS.json"]
    checksums = {name: _sha256(output_dir / name) for name in checksum_names}
    (output_dir / "SHA256SUMS.json").write_text(
        json.dumps(checksums, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def check(output_dir: Path) -> None:
    missing = [name for name in GENERATED_FILES if not (output_dir / name).is_file()]
    if missing:
        raise SystemExit(f"manifest is incomplete; missing {missing}")
    with tempfile.TemporaryDirectory(prefix="affected-property-manifest-") as temp:
        candidate = Path(temp)
        build(candidate)
        changed = [
            name
            for name in GENERATED_FILES
            if (candidate / name).read_bytes() != (output_dir / name).read_bytes()
        ]
    if changed:
        raise SystemExit(f"manifest is stale or non-deterministic: {changed}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        check(args.output_dir)
        print(f"manifest verified: {args.output_dir}")
        return 0
    summary = build(args.output_dir)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
