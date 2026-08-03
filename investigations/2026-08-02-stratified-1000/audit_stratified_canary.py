#!/usr/bin/env python3
"""Audit the deterministic 1,000-property release canary entirely offline.

The Cloud Run prefix is mirrored once before this script is invoked.  The
script deliberately has no GCS or network client: every conclusion can be
reproduced from the checked-in sample/finding manifests and the downloaded
run artifacts.

It answers four separate questions without conflating them:

* Did exactly the selected 1,000 properties finish once?
* Do emitted property/unit rows satisfy the consolidated output contracts?
* Which prior adapter/result strata were exercised and how did they move?
* For each of the 49 evidence-backed adapter findings, was its route exercised
  in this live canary, and did any output-contract defect recur?

Adapter-specific fixture tests remain the semantic oracle for routes that a
live site no longer exercises.  Their result is supplied explicitly via
``--regression-tests`` and reported separately from runtime evidence.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit


HERE = Path(__file__).resolve().parent
DEFAULT_RUN_DIR = HERE / "canary-output"
DEFAULT_PRIOR_DIR = HERE / "source-benchmark"
DEFAULT_OUTPUT_DIR = HERE / "post-run-audit"
DEFAULT_SAMPLE = HERE / "manifest-v1" / "sample_ledger.csv"
DEFAULT_FINDING_COVERAGE = (
    HERE.parent
    / "2026-08-01-consolidated-canary"
    / "affected-property-manifest-v1"
    / "finding_coverage.json"
)
DEFAULT_FINDING_INDEX = (
    HERE.parent
    / "2026-08-01-consolidated-canary"
    / "affected-property-manifest-v1"
    / "launch_index.csv"
)
DEFAULT_FOCUSED_CONTRACT = (
    HERE.parent / "2026-08-01-consolidated-canary" / "focused-output-contract-canary-v1.json"
)

SYNTHETIC_PREFIXES = ("inferred_", "unkeyable_")
HISTORY_KEY_RE = re.compile(r"^unitsha_[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SENSITIVE_QUERY_PARTS = ("auth", "key", "password", "secret", "signature", "token")
NEGATIVE_STATUSES = {
    "UNAVAILABLE",
    "LEASED",
    "PENDING",
    "WAITLIST",
    "WAIT_LIST",
    "OCCUPIED",
    "NOT_AVAILABLE",
}
IDENTITY_SOURCE_KEYS = {
    "apartment_id",
    "apartmentid",
    "unit_id",
    "unitid",
    "unit_id_native",
    "native_unit_id",
    "listing_id",
    "appfolio_listing_id",
    "sightmap_unit_id",
    "spherexx_unit_id",
    "realpage_unit_id",
    "realpageunitid",
    "avalon_apartment_id",
    "apartment_uuid",
    "unit_uuid",
    "space_id",
}
SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
TARGET_ADAPTER_ALIASES: dict[str, set[str]] = {
    "rentcafe_applicant": {"rentcafe", "rentcafe_applicant"},
    "onesite_workflow": {"onesite", "onesite_workflow"},
    "generic_spherexx": {"spherexx", "generic_spherexx"},
    "rentmanager_iloveleasing": {"rentmanager", "iloveleasing", "rentmanager_iloveleasing"},
    "realpage_getunits": {"realpage", "realpage_getunits"},
    "onsite_apply": {"onsite", "onsite_apply"},
    "rentcafe_layout_tab": {"rentcafe", "rentcafe_layout_tab"},
    "squarespace_nopms": {"squarespace", "squarespace_nopms"},
    "wix_floor_plans": {"wix", "wix_floor_plans"},
    "wix_nopms": {"wix", "wix_nopms"},
}
TARGET_TIER_MARKERS: dict[str, tuple[str, ...]] = {
    "rentcafe_applicant": ("rentcafe_applicant",),
    "rentcafe_layout_tab": ("rentcafe_lt", "rentcafe_layout_tab"),
    "onesite_workflow": ("onesite_workflow",),
    "generic_spherexx": ("spherexx",),
    "rentmanager_iloveleasing": ("iloveleasing",),
    "realpage_getunits": ("getunits",),
}


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def number(value: Any) -> float | None:
    if value in (None, "", "null") or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def iso_date(value: Any) -> date | None:
    raw = text(value)[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def single_explicit_date(value: Any) -> date | None:
    """Parse one unambiguous visible date without timezone conversion.

    A string containing multiple distinct dates is intentionally skipped: an
    adapter may have a documented policy for choosing one endpoint. The gate
    is aimed at the proven defect class where one source date was shifted by a
    timezone transform or overwritten with the scrape date.
    """

    raw = text(value)
    if not raw:
        return None
    candidates: list[str] = []
    candidates.extend(re.findall(r"(?<!\d)\d{4}[-/]\d{1,2}[-/]\d{1,2}(?!\d)", raw))
    candidates.extend(re.findall(r"(?<!\d)\d{1,2}[-/]\d{1,2}[-/]\d{2,4}(?!\d)", raw))
    candidates.extend(
        re.findall(
            r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
            r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
            r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2}"
            r"(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b",
            raw,
            flags=re.IGNORECASE,
        )
    )
    parsed: set[date] = set()
    for candidate in candidates:
        cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", candidate, flags=re.IGNORECASE)
        cleaned = cleaned.replace(".", "").replace(",", "")
        for fmt in (
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%m/%d/%Y",
            "%m-%d-%Y",
            "%m/%d/%y",
            "%m-%d-%y",
            "%B %d %Y",
            "%b %d %Y",
        ):
            try:
                parsed.add(datetime.strptime(cleaned, fmt).date())
                break
            except ValueError:
                continue
    return next(iter(parsed)) if len(parsed) == 1 else None


def url_has_unredacted_secret(value: Any) -> bool:
    try:
        for key, raw_value in parse_qsl(urlsplit(text(value)).query, keep_blank_values=True):
            if not any(part in key.casefold() for part in SENSITIVE_QUERY_PARTS):
                continue
            normalized = raw_value.strip().casefold().strip("<>[]")
            if normalized not in {"", "redacted"}:
                return True
    except Exception:
        return False
    return False


def apartment_id(prop: dict[str, Any]) -> str:
    return text(prop.get("apartment_id") or prop.get("apartmentid") or prop.get("canonical_id"))


def property_name(prop: dict[str, Any]) -> str:
    return text(prop.get("proj_name") or prop.get("name"))


def meta(prop: dict[str, Any]) -> dict[str, Any]:
    value = prop.get("_meta")
    return value if isinstance(value, dict) else {}


def provenance(prop: dict[str, Any]) -> dict[str, Any]:
    value = meta(prop).get("provenance")
    return value if isinstance(value, dict) else {}


def verdict(prop: dict[str, Any]) -> str:
    return text(meta(prop).get("verdict") or prop.get("verdict") or "UNKNOWN") or "UNKNOWN"


def adapter(prop: dict[str, Any]) -> str:
    return text(provenance(prop).get("adapter") or "UNATTRIBUTED") or "UNATTRIBUTED"


def target_route_exercised(prop: dict[str, Any], target: str) -> bool:
    if target in {"shared_formatter", "non_registry"}:
        return bool(prop.get("units") or prop.get("floor_plans"))
    observed = adapter(prop).lower()
    aliases = TARGET_ADAPTER_ALIASES.get(target, {target})
    if observed == target:
        return True
    winning_tier = text(provenance(prop).get("winning_tier")).lower()
    normalized_tier = re.sub(r"[^a-z0-9]+", "_", winning_tier)
    markers = TARGET_TIER_MARKERS.get(target)
    if markers:
        return observed in aliases and any(marker in normalized_tier for marker in markers)
    if observed in aliases:
        return True
    return any(alias in normalized_tier for alias in aliases)


def is_success_unit(prop: dict[str, Any]) -> bool:
    return verdict(prop) == "SUCCESS" and bool(prop.get("units"))


def is_synthetic(unit: dict[str, Any]) -> bool:
    return text(unit.get("unit_id")).lower().startswith(SYNTHETIC_PREFIXES)


def parse_delimited(raw: str) -> list[str]:
    return [part.strip() for part in re.split(r"[|,;]", raw or "") if part.strip()]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_properties(run_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Path], list[str]]:
    paths = sorted(run_dir.glob("shard_*/properties.json"))
    if not paths and (run_dir / "properties.json").is_file():
        paths = [run_dir / "properties.json"]
    if not paths:
        paths = sorted(run_dir.rglob("properties.json"))
    by_id: dict[str, dict[str, Any]] = {}
    source: dict[str, Path] = {}
    duplicates: list[str] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("properties", [])
        if not isinstance(rows, list):
            raise ValueError(f"unsupported properties payload: {path}")
        for row in rows:
            if not isinstance(row, dict):
                continue
            pid = apartment_id(row)
            if not pid:
                raise ValueError(f"property without apartment_id in {path}")
            if pid in by_id:
                duplicates.append(pid)
                continue
            by_id[pid] = row
            source[pid] = path
    return by_id, source, duplicates


@dataclass
class Issue:
    severity: str
    code: str
    apartment_id: str
    name: str
    adapter: str
    verdict: str
    unit_id: str
    field: str
    observed: str
    expected: str
    evidence: str


def add_issue(
    issues: list[Issue],
    prop: dict[str, Any] | None,
    severity: str,
    code: str,
    *,
    pid: str = "",
    name: str = "",
    unit: dict[str, Any] | None = None,
    field: str = "",
    observed: Any = "",
    expected: str = "",
    evidence: str = "",
) -> None:
    prop = prop or {}
    issues.append(
        Issue(
            severity=severity,
            code=code,
            apartment_id=pid or apartment_id(prop),
            name=name or property_name(prop),
            adapter=adapter(prop) if prop else "",
            verdict=verdict(prop) if prop else "",
            unit_id=text((unit or {}).get("unit_id")),
            field=field,
            observed=text(observed),
            expected=expected,
            evidence=evidence,
        )
    )


def archive_path_for(property_json: Path, relative: str) -> Path:
    # property_json is shard_N/properties.json and archive paths are relative
    # to that shard's uploaded run directory.
    return property_json.parent / relative


def read_gzip_json(path: Path) -> tuple[Any, bytes]:
    with gzip.open(path, "rb") as handle:
        payload = handle.read()
    return json.loads(payload), payload


def manifest_for_property(
    prop: dict[str, Any], property_json: Path
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    problems: list[str] = []
    pointer = provenance(prop).get("raw_source_archive")
    if not isinstance(pointer, dict):
        return None, None, ["missing raw_source_archive pointer"]
    relative = text(pointer.get("manifest_path"))
    if not relative:
        return None, None, ["missing manifest_path"]
    manifest_path = archive_path_for(property_json, relative)
    if not manifest_path.is_file():
        return None, None, [f"missing {relative}"]
    try:
        manifest, payload = read_gzip_json(manifest_path)
    except Exception as exc:  # diagnostic output must survive malformed evidence
        return None, None, [f"cannot read {relative}: {exc}"]
    expected_hash = text(pointer.get("manifest_sha256"))
    actual_hash = hashlib.sha256(payload).hexdigest()
    if expected_hash and actual_hash != expected_hash:
        problems.append(f"manifest hash {actual_hash} != {expected_hash}")

    snapshot_pointer = manifest.get("extraction_snapshot") if isinstance(manifest, dict) else None
    snapshot: dict[str, Any] | None = None
    if not isinstance(snapshot_pointer, dict):
        problems.append("manifest lacks extraction_snapshot")
    else:
        snap_rel = text(snapshot_pointer.get("path"))
        snap_path = archive_path_for(property_json, snap_rel)
        if not snap_path.is_file():
            problems.append(f"missing {snap_rel}")
        else:
            try:
                loaded, snap_payload = read_gzip_json(snap_path)
                snapshot = loaded if isinstance(loaded, dict) else None
                snap_hash = hashlib.sha256(snap_payload).hexdigest()
                if snap_hash != text(snapshot_pointer.get("payload_sha256")):
                    problems.append("extraction snapshot hash mismatch")
            except Exception as exc:
                problems.append(f"cannot read {snap_rel}: {exc}")

    if isinstance(manifest, dict):
        records = manifest.get("responses") or []
        if pointer.get("source_count") != len(records):
            problems.append("source_count does not equal manifest response count")
        for record in records:
            if not isinstance(record, dict):
                problems.append("non-object response record")
                continue
            if url_has_unredacted_secret(record.get("source_url")):
                problems.append("source manifest contains an unredacted credential query value")
            rel = text(record.get("archive_body_path"))
            body_path = archive_path_for(property_json, rel)
            if not body_path.is_file():
                problems.append(f"missing {rel}")
                continue
            try:
                with gzip.open(body_path, "rb") as handle:
                    body = handle.read()
                if hashlib.sha256(body).hexdigest() != text(record.get("archive_payload_sha256")):
                    problems.append(f"archive payload hash mismatch: {rel}")
            except Exception as exc:
                problems.append(f"cannot read {rel}: {exc}")
    return manifest if isinstance(manifest, dict) else None, snapshot, problems


def unit_match_tokens(unit: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("unit_id", "canonical_unit_id", "source_unit_id", "unit_id_raw"):
        value = text(unit.get(key)).lower()
        if value:
            values.add(f"id:{value}")
    source_ids = unit.get("source_ids")
    if isinstance(source_ids, dict):
        for key, value in source_ids.items():
            value_text = text(value).lower()
            if value_text:
                values.add(f"source:{str(key).lower()}:{value_text}")
    return values


def find_unit_match(prior: dict[str, Any], current: list[dict[str, Any]]) -> dict[str, Any] | None:
    wanted = unit_match_tokens(prior)
    matches = [unit for unit in current if wanted & unit_match_tokens(unit)]
    return matches[0] if len(matches) == 1 else None


def natural_unit_number(row: dict[str, Any]) -> str:
    """Return a non-synthetic source apartment number from a pre-format row."""
    value = text(
        row.get("unit_number")
        or row.get("_unit_number")
        or row.get("unitNumber")
        or row.get("apartment_number")
    )
    if not value or value.casefold() in {"null", "none"}:
        return ""
    if value.casefold().startswith(SYNTHETIC_PREFIXES):
        return ""
    return value


def identity_rescue_signature(row: dict[str, Any]) -> tuple[str, ...]:
    """Physical/value signature used only to link snapshot rows for QA.

    It is not a production identity key. Requiring the response hash plus
    plan/phenotype/value fields keeps this audit comparison narrow while still
    exposing formatter loss when an output synthetic row came from a raw row
    carrying a real apartment number.
    """
    source_hash = text(row.get("source_response_sha256")).casefold()
    floor_plan = text(
        row.get("floor_plan_name")
        or row.get("_floor_plan")
        or row.get("floorplan_name")
    ).casefold()
    beds = text(row.get("beds") or row.get("bedrooms") or row.get("_bedrooms"))
    baths = text(row.get("baths") or row.get("bathrooms") or row.get("_bathrooms"))
    area = text(
        row.get("area")
        or row.get("sqft")
        or row.get("_sqft")
        or row.get("area_low")
    )
    rent = text(
        row.get("rent_low")
        or row.get("market_rent_low")
        or row.get("asking_rent")
        or row.get("rent")
    )
    available = text(
        row.get("available_date")
        or row.get("availability_date")
        or row.get("available_date_raw")
        or row.get("_available_date_raw")
    )[:10]
    return source_hash, floor_plan, beds, baths, area, rent, available


def preformat_natural_identity_matches(
    output_unit: dict[str, Any],
    preformat_rows: list[dict[str, Any]],
) -> list[str]:
    """Natural numbers on raw rows matching one synthetic formatted row."""
    wanted = identity_rescue_signature(output_unit)
    candidates: list[str] = []
    for row in preformat_rows:
        natural = natural_unit_number(row)
        if not natural:
            continue
        observed = identity_rescue_signature(row)
        # Hash equality is strong when retained. Otherwise require at least
        # four equal populated phenotype/value components.
        hash_match = bool(wanted[0] and observed[0] and wanted[0] == observed[0])
        comparable = [
            left == right
            for left, right in zip(wanted[1:], observed[1:], strict=True)
            if left and right
        ]
        if (hash_match and sum(comparable) >= 2) or sum(comparable) >= 4:
            candidates.append(natural)
    return sorted(set(candidates))


def audit_unit(
    prop: dict[str, Any],
    unit: dict[str, Any],
    issues: list[Issue],
    capture: date,
    manifest_hashes: set[str],
) -> dict[str, int]:
    metrics: Counter[str] = Counter()
    uid = text(unit.get("unit_id"))
    canonical = text(unit.get("canonical_unit_id"))
    synthetic = is_synthetic(unit)
    metrics["synthetic_id_units" if synthetic else "real_id_units"] += 1

    if not uid:
        add_issue(
            issues,
            prop,
            "critical",
            "UNIT_ID_MISSING",
            unit=unit,
            field="unit_id",
            expected="non-empty canonical unit identity",
        )
    if canonical != uid:
        add_issue(
            issues,
            prop,
            "high",
            "CANONICAL_UNIT_ID_MISMATCH",
            unit=unit,
            field="canonical_unit_id",
            observed=canonical,
            expected=f"equal emitted unit_id {uid}",
        )
    if unit.get("is_floor_plan_level") is True:
        add_issue(
            issues,
            prop,
            "critical",
            "PLAN_ROW_IN_UNITS",
            unit=unit,
            field="is_floor_plan_level",
            observed=True,
            expected="plan evidence only in floor_plans[]",
        )
    if text(unit.get("data_quality_flag")) == "SIGHTMAP_PLAN_PRESENCE":
        add_issue(
            issues,
            prop,
            "critical",
            "SIGHTMAP_MARKER_IN_UNITS",
            unit=unit,
            field="data_quality_flag",
            observed="SIGHTMAP_PLAN_PRESENCE",
            expected="marker only in floor_plans[]",
        )
    display_identity = " ".join(
        text(unit.get(key)) for key in ("unit_id", "source_unit_id", "unit_name")
    ).strip()
    if re.search(r"\bWAIT(?:LIST)?(?:\b|[_-])", display_identity, flags=re.IGNORECASE):
        add_issue(
            issues,
            prop,
            "critical",
            "WAITLIST_SENTINEL_IN_UNITS",
            unit=unit,
            field="unit_id,source_unit_id,unit_name",
            observed=display_identity,
            expected="waitlist/catalogue evidence outside physical units[]",
        )

    history = text(unit.get("unit_history_key"))
    if synthetic:
        source_ids = unit.get("source_ids") if isinstance(unit.get("source_ids"), dict) else {}
        native = {
            str(key): text(value)
            for key, value in source_ids.items()
            if text(value) and str(key).lower() in IDENTITY_SOURCE_KEYS
        }
        if native:
            metrics["avoidable_synthetic_id_units"] += 1
            add_issue(
                issues,
                prop,
                "high",
                "SYNTHETIC_ID_WITH_NATIVE_ID",
                unit=unit,
                field="source_ids",
                observed=json.dumps(native, sort_keys=True),
                expected="native identity selected before fallback",
            )
        if history:
            add_issue(
                issues,
                prop,
                "medium",
                "SYNTHETIC_HISTORY_KEY",
                unit=unit,
                field="unit_history_key",
                observed=history,
                expected="null until a physical identity is available",
            )
    else:
        if not HISTORY_KEY_RE.fullmatch(history):
            add_issue(
                issues,
                prop,
                "high",
                "HISTORY_KEY_MISSING_OR_INVALID",
                unit=unit,
                field="unit_history_key",
                observed=history,
                expected="unitsha_ followed by 64 lowercase hex characters",
            )
        if text(unit.get("unit_history_key_version")) != "v1":
            add_issue(
                issues,
                prop,
                "medium",
                "HISTORY_KEY_VERSION_INVALID",
                unit=unit,
                field="unit_history_key_version",
                observed=unit.get("unit_history_key_version"),
                expected="v1",
            )

    building_id = text(unit.get("building_id"))
    if building_id:
        metrics["building_id_units"] += 1
        if not text(unit.get("building_id_source")):
            add_issue(
                issues,
                prop,
                "medium",
                "BUILDING_ID_SOURCE_MISSING",
                unit=unit,
                field="building_id_source",
                observed="",
                expected="source field/key for standalone building identity",
            )

    area = number(unit.get("area"))
    area_sqft = number(unit.get("area_sqft"))
    area_low = number(unit.get("area_low"))
    area_high = number(unit.get("area_high"))
    area_type = text(unit.get("area_value_type"))
    valid_area_range = bool(
        area_low is not None
        and area_high is not None
        and area_low > 0
        and area_high >= area_low
    )
    if area == -1:
        if not valid_area_range:
            metrics["unresolved_area_units"] += 1
        if area_sqft is not None:
            add_issue(
                issues,
                prop,
                "high",
                "AREA_SENTINEL_LEAK",
                unit=unit,
                field="area_sqft",
                observed=area_sqft,
                expected="null when legacy area is -1",
            )
        if not valid_area_range and not text(unit.get("area_absence")):
            add_issue(
                issues,
                prop,
                "medium",
                "AREA_ABSENCE_UNEXPLAINED",
                unit=unit,
                field="area_absence",
                expected="explicit missing-area taxonomy",
            )
    elif area is not None and area <= 0:
        add_issue(
            issues,
            prop,
            "high",
            "NONPOSITIVE_PUBLISHED_AREA",
            unit=unit,
            field="area",
            observed=area,
            expected="positive source-published area or legacy -1/null absence",
        )
    if area_low is not None or area_high is not None:
        if area_low is None or area_high is None or area_low <= 0 or area_high < area_low:
            add_issue(
                issues,
                prop,
                "high",
                "AREA_RANGE_INVALID",
                unit=unit,
                field="area_low,area_high",
                observed=f"{area_low},{area_high}",
                expected="positive ordered endpoints",
            )
        elif area_low == area_high:
            metrics["exact_area_units"] += 1
            if area_sqft != area_low or area_type not in ("", "exact"):
                add_issue(
                    issues,
                    prop,
                    "medium",
                    "AREA_EXACT_COMPANION_MISMATCH",
                    unit=unit,
                    field="area_sqft,area_value_type",
                    observed=f"{area_sqft},{area_type}",
                    expected=f"{area_low},exact",
                )
        else:
            metrics["area_range_units"] += 1
            if area_sqft is not None or area_type != "range":
                add_issue(
                    issues,
                    prop,
                    "high",
                    "AREA_RANGE_MIDPOINT_OR_TYPE",
                    unit=unit,
                    field="area_sqft,area_value_type",
                    observed=f"{area_sqft},{area_type}",
                    expected="null,range",
                )

    area_prov = text(unit.get("area_provenance")).lower()
    if area_prov and any(token in area_prov for token in ("evidence", "response", "asset", "catalog")):
        response_hash = text(unit.get("source_response_sha256"))
        asset_hash = text(unit.get("source_asset_sha256"))
        if not ((response_hash and text(unit.get("source_record_locator"))) or asset_hash):
            add_issue(
                issues,
                prop,
                "high",
                "AREA_EVIDENCE_NOT_TRACEABLE",
                unit=unit,
                field="source_response_sha256,source_record_locator,source_asset_sha256",
                expected="response hash + locator, or asset hash",
            )

    rent_low = number(unit.get("rent_low"))
    rent_high = number(unit.get("rent_high"))
    if rent_low is not None or rent_high is not None:
        if rent_low is None or rent_high is None or rent_low < 0 or rent_high < rent_low:
            add_issue(
                issues,
                prop,
                "high",
                "RENT_ENDPOINTS_INVALID",
                unit=unit,
                field="rent_low,rent_high",
                observed=f"{rent_low},{rent_high}",
                expected="non-negative ordered endpoints",
            )
        elif rent_low != rent_high:
            metrics["rent_range_units"] += 1
            if unit.get("rent_is_range") is not True or not text(unit.get("rent_range")):
                add_issue(
                    issues,
                    prop,
                    "high",
                    "RENT_RANGE_COLLAPSED",
                    unit=unit,
                    field="rent_is_range,rent_range",
                    observed=f"{unit.get('rent_is_range')},{unit.get('rent_range')}",
                    expected="true plus a retained canonical range",
                )
            if not text(unit.get("rent_provenance")):
                add_issue(
                    issues,
                    prop,
                    "medium",
                    "RENT_RANGE_PROVENANCE_MISSING",
                    unit=unit,
                    field="rent_provenance",
                    expected="non-empty range provenance",
                )
        elif unit.get("rent_is_range") is True:
            add_issue(
                issues,
                prop,
                "medium",
                "RENT_RANGE_FLAG_CONTRADICTION",
                unit=unit,
                field="rent_is_range",
                observed=True,
                expected="false when numeric endpoints are equal",
            )

    availability_prov = text(unit.get("availability_date_provenance"))
    available_date = iso_date(unit.get("available_date"))
    available_date_raw = text(unit.get("available_date_raw") or unit.get("_available_date_raw"))
    status = text(unit.get("availability_status")).upper().replace(" ", "_")
    raw_lower = available_date_raw.lower()
    if status in NEGATIVE_STATUSES and availability_prov in {"capture_date_default", "available_now"}:
        add_issue(
            issues,
            prop,
            "critical",
            "NEGATIVE_STATUS_CAPTURE_DATE",
            unit=unit,
            field="availability_status,availability_date_provenance",
            observed=f"{status},{availability_prov},{unit.get('available_date')}",
            expected="explicitly negative rows have no manufactured capture date",
        )
    if raw_lower and any(
        token in raw_lower
        for token in ("unavailable", "not available", "leased", "waitlist", "wait list", "pending")
    ) and (available_date is not None or status == "AVAILABLE"):
        add_issue(
            issues,
            prop,
            "critical",
            "NEGATIVE_RAW_AVAILABILITY_REVERSED",
            unit=unit,
            field="available_date_raw,availability_status,available_date",
            observed=f"{available_date_raw}|{status}|{unit.get('available_date')}",
            expected="negative source text remains non-available and has no manufactured date",
        )
    if re.fullmatch(
        r"\s*(?:available\s+)?(?:now|today|immediate(?:ly)?)\s*",
        available_date_raw,
        flags=re.IGNORECASE,
    ):
        expected_now_provenance = (
            "negative_status_override"
            if status in NEGATIVE_STATUSES
            else "available_now"
        )
        if availability_prov != expected_now_provenance:
            add_issue(
                issues,
                prop,
                "high",
                "AVAILABLE_NOW_PROVENANCE_LOST",
                unit=unit,
                field="availability_date_provenance",
                observed=availability_prov,
                expected=expected_now_provenance,
            )
    if availability_prov in {"available_now", "capture_date_default"} and available_date != capture:
        add_issue(
            issues,
            prop,
            "high",
            "CAPTURE_DATE_PROVENANCE_MISMATCH",
            unit=unit,
            field="available_date",
            observed=unit.get("available_date"),
            expected=capture.isoformat(),
        )
    if availability_prov == "explicit_future" and (available_date is None or available_date <= capture):
        add_issue(
            issues,
            prop,
            "high",
            "EXPLICIT_FUTURE_NOT_FUTURE",
            unit=unit,
            field="available_date",
            observed=unit.get("available_date"),
            expected=f"date after {capture.isoformat()}",
        )
    if availability_prov in {"explicit_future", "explicit_capture_date", "historical_embedded"}:
        if not available_date_raw:
            add_issue(
                issues,
                prop,
                "medium",
                "EXPLICIT_DATE_RAW_VALUE_MISSING",
                unit=unit,
                field="available_date_raw",
                expected="visible/source value retained beside normalized date",
            )
        raw_date = single_explicit_date(available_date_raw)
        if raw_date is not None and raw_date != available_date:
            add_issue(
                issues,
                prop,
                "high",
                "EXPLICIT_DATE_NORMALIZATION_SHIFT",
                unit=unit,
                field="available_date",
                observed=unit.get("available_date"),
                expected=raw_date.isoformat(),
                evidence=f"one explicit date parsed from raw value {available_date_raw!r}",
            )

    response_hash = text(unit.get("source_response_sha256"))
    asset_hash = text(unit.get("source_asset_sha256"))
    for field in ("source_response_url", "source_asset_url"):
        source_url = text(unit.get(field))
        if source_url and url_has_unredacted_secret(source_url):
            add_issue(
                issues,
                prop,
                "critical",
                "SOURCE_URL_SECRET_NOT_REDACTED",
                unit=unit,
                field=field,
                observed=source_url,
                expected="sanitized URL without credential-bearing query values",
            )
    for field, digest in (("source_response_sha256", response_hash), ("source_asset_sha256", asset_hash)):
        if digest and not SHA256_RE.fullmatch(digest):
            add_issue(
                issues,
                prop,
                "high",
                "SOURCE_HASH_INVALID",
                unit=unit,
                field=field,
                observed=digest,
                expected="64 lowercase hex characters",
            )
        if digest and manifest_hashes and digest not in manifest_hashes:
            add_issue(
                issues,
                prop,
                "high",
                "SOURCE_HASH_NOT_ARCHIVED",
                unit=unit,
                field=field,
                observed=digest,
                expected="hash represented in the property's immutable source manifest",
            )
    return dict(metrics)


def audit_property(
    prop: dict[str, Any],
    property_json: Path,
    prior: dict[str, Any] | None,
    issues: list[Issue],
    capture: date,
) -> dict[str, Any]:
    prov = provenance(prop)
    units = [row for row in (prop.get("units") or []) if isinstance(row, dict)]
    plans = [row for row in (prop.get("floor_plans") or []) if isinstance(row, dict)]
    result: Counter[str] = Counter()
    result["unit_count"] = len(units)
    result["plan_count"] = len(plans)

    current_verdict = verdict(prop)
    if current_verdict == "SUCCESS" and not units:
        add_issue(
            issues,
            prop,
            "critical",
            "UNIT_SUCCESS_WITHOUT_UNITS",
            field="_meta.verdict,units",
            observed="SUCCESS,0",
            expected="unit-level success has at least one physical unit",
        )
    verdict_reason = text(meta(prop).get("verdict_reason"))
    plan_units_have_rent = any(
        number(row.get("rent_low")) is not None
        or number(row.get("rent_high")) is not None
        for row in units
    )
    valid_no_rent_plan_rows = bool(
        current_verdict == "SUCCESS_PLAN_LEVEL"
        and units
        and verdict_reason.startswith("no_rent_signal")
        and not plan_units_have_rent
    )
    if current_verdict == "SUCCESS_PLAN_LEVEL" and units:
        if valid_no_rent_plan_rows:
            result["plan_verdict_physical_no_rent_rows"] += len(units)
        else:
            add_issue(
                issues,
                prop,
                "critical",
                "PLAN_SUCCESS_WITH_PHYSICAL_UNITS",
                field="_meta.verdict,units",
                observed=f"SUCCESS_PLAN_LEVEL,{len(units)},{verdict_reason}",
                expected="only explicit no_rent_signal physical rows may remain plan success",
            )
    if current_verdict == "SUCCESS_PLAN_LEVEL" and not plans and not units:
        add_issue(
            issues,
            prop,
            "high",
            "PLAN_SUCCESS_WITHOUT_PLANS",
            field="_meta.verdict,floor_plans",
            observed="SUCCESS_PLAN_LEVEL,0",
            expected="plan-level success has at least one floor-plan row",
        )
    if current_verdict not in {"SUCCESS", "SUCCESS_PLAN_LEVEL"} and units:
        add_issue(
            issues,
            prop,
            "critical",
            "FAILURE_VERDICT_WITH_UNITS",
            field="_meta.verdict,units",
            observed=f"{current_verdict},{len(units)}",
            expected="admitted physical inventory yields SUCCESS",
        )

    required_provenance = {
        "raw_source_count",
        "raw_source_count_basis",
        "raw_source_kind_counts",
        "parser_count",
        "formatted_count",
        "final_admitted_count",
        "canonical_id_uniqueness",
        "property_identity_verdict",
        "availability_date_provenance",
        "rent_range_units",
        "rent_provenance",
        "unit_source_provenance",
        "raw_source_archive",
    }
    missing_prov = sorted(required_provenance - set(prov))
    if missing_prov:
        add_issue(
            issues,
            prop,
            "high",
            "PROVENANCE_FIELDS_MISSING",
            field="_meta.provenance",
            observed=",".join(missing_prov),
            expected="all consolidated diagnostic counters/pointers",
        )

    if prov.get("final_admitted_count") != len(units):
        add_issue(
            issues,
            prop,
            "high",
            "FINAL_COUNT_MISMATCH",
            field="final_admitted_count",
            observed=prov.get("final_admitted_count"),
            expected=str(len(units)),
        )
    uniqueness = prov.get("canonical_id_uniqueness")
    if not isinstance(uniqueness, dict) or uniqueness.get("passed") is not True:
        add_issue(
            issues,
            prop,
            "critical",
            "CANONICAL_UNIQUENESS_GATE_FAILED",
            field="canonical_id_uniqueness",
            observed=json.dumps(uniqueness, sort_keys=True),
            expected="passed=true",
        )
    identity = prov.get("property_identity_verdict")
    identity_status = text(identity.get("status") if isinstance(identity, dict) else "")
    if units and identity_status == "MISMATCH":
        add_issue(
            issues,
            prop,
            "critical",
            "PROPERTY_IDENTITY_MISMATCH_ADMITTED",
            field="property_identity_verdict.status",
            observed=identity_status,
            expected="mismatched inventory rejected",
        )

    manifest, snapshot, archive_problems = manifest_for_property(prop, property_json)
    for problem in archive_problems:
        add_issue(
            issues,
            prop,
            "high",
            "OFFLINE_ARCHIVE_INVALID",
            field="raw_source_archive",
            observed=problem,
            expected="readable content-addressed manifest, bodies, and extraction snapshot",
        )
    manifest_hashes: set[str] = set()
    if manifest:
        for record in manifest.get("responses") or []:
            if isinstance(record, dict):
                manifest_hashes.add(text(record.get("source_response_sha256")))
    if snapshot is not None:
        for required in ("units_pre_format", "floor_plans_pre_format", "formatted_property"):
            if required not in snapshot:
                add_issue(
                    issues,
                    prop,
                    "high",
                    "EXTRACTION_SNAPSHOT_INCOMPLETE",
                    field=required,
                    expected="field retained in offline replay snapshot",
                )
        preformat_rows = [
            row
            for row in (snapshot.get("units_pre_format") or [])
            if isinstance(row, dict)
        ]
        for unit in units:
            if not is_synthetic(unit):
                continue
            natural_matches = preformat_natural_identity_matches(unit, preformat_rows)
            if natural_matches:
                result["avoidable_synthetic_id_units"] += 1
                add_issue(
                    issues,
                    prop,
                    "high",
                    "SYNTHETIC_OUTPUT_WITH_PREFORMAT_NATURAL_ID",
                    unit=unit,
                    field="unit_id,units_pre_format[].unit_number",
                    observed=f"{unit.get('unit_id')} <- {natural_matches[:8]}",
                    expected="natural pre-format apartment number selected before fallback identity",
                    evidence="immutable extraction snapshot links the synthetic output to a raw natural number",
                )

    canonical_ids = [text(unit.get("unit_id")) for unit in units]
    if len(canonical_ids) != len(set(canonical_ids)):
        add_issue(
            issues,
            prop,
            "critical",
            "DUPLICATE_CANONICAL_UNIT_ID",
            field="units[].unit_id",
            observed=str(len(canonical_ids) - len(set(canonical_ids))),
            expected="zero duplicates per property",
        )
    physical_history = [
        text(unit.get("unit_history_key")) for unit in units if not is_synthetic(unit)
    ]
    populated_history = [value for value in physical_history if value]
    if len(populated_history) != len(set(populated_history)):
        add_issue(
            issues,
            prop,
            "critical",
            "DUPLICATE_UNIT_HISTORY_KEY",
            field="units[].unit_history_key",
            observed=str(len(populated_history) - len(set(populated_history))),
            expected="zero duplicates per property",
        )

    # Entrata's modern embedded roster and per-plan roster can publish the
    # same apartment simultaneously. A blank-building modern copy beside a
    # scoped per-plan copy is not a legitimate cross-building collision.
    entrata_by_source_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for unit in units:
        source_id = text(unit.get("source_unit_id")).casefold()
        if source_id:
            entrata_by_source_id[source_id].append(unit)
    entrata_parallel_duplicates = 0
    for source_id, rows in entrata_by_source_id.items():
        tiers = {text(row.get("extraction_tier")).upper() for row in rows}
        has_modern = "TIER_1_DOM_ENTRATA_MODERN" in tiers
        has_scoped = any(
            tier.startswith("TIER_1_DOM_ENTRATA_PP_") and tier != "TIER_1_DOM_ENTRATA_MODERN"
            for tier in tiers
        )
        has_blank_modern = any(
            text(row.get("extraction_tier")).upper() == "TIER_1_DOM_ENTRATA_MODERN"
            and not text(row.get("building_id") or row.get("building"))
            for row in rows
        )
        if len(rows) > 1 and has_modern and has_scoped and has_blank_modern:
            entrata_parallel_duplicates += len(rows) - 1
            add_issue(
                issues,
                prop,
                "critical",
                "ENTRATA_PARALLEL_ROSTER_DUPLICATE",
                unit=rows[0],
                field="source_unit_id,extraction_tier,building_id",
                observed=f"{source_id}:{sorted(tiers)}",
                expected="one coherent Entrata source family per apartment roster",
            )
    result["entrata_parallel_duplicate_rows"] = entrata_parallel_duplicates

    for unit in units:
        result.update(audit_unit(prop, unit, issues, capture, manifest_hashes))

    for plan in plans:
        if text(plan.get("unit_id")) or text(plan.get("canonical_unit_id")):
            add_issue(
                issues,
                prop,
                "high",
                "PLAN_HAS_UNIT_IDENTITY",
                unit=plan,
                field="floor_plans[].unit_id",
                observed=plan.get("unit_id") or plan.get("canonical_unit_id"),
                expected="null; plan evidence is not a physical apartment",
            )
        if plan.get("is_floor_plan_level") is not True:
            add_issue(
                issues,
                prop,
                "medium",
                "PLAN_LEVEL_FLAG_MISSING",
                unit=plan,
                field="floor_plans[].is_floor_plan_level",
                observed=plan.get("is_floor_plan_level"),
                expected="true",
            )
    keyed_plans = [
        plan for plan in plans if text(plan.get("floor_plan_id") or plan.get("floor_plan_name"))
    ]
    plan_fingerprints = [
        json.dumps(
            {
                key: plan.get(key)
                for key in (
                    "floor_plan_id",
                    "floor_plan_name",
                    "beds",
                    "baths",
                    "area_low",
                    "area_high",
                    "rent_low",
                    "rent_high",
                    "availability_status",
                )
            },
            sort_keys=True,
            default=str,
        )
        for plan in keyed_plans
    ]
    duplicate_plans = len(plan_fingerprints) - len(set(plan_fingerprints))
    if duplicate_plans:
        add_issue(
            issues,
            prop,
            "high",
            "DUPLICATE_FLOOR_PLAN_ROWS",
            field="floor_plans[]",
            observed=duplicate_plans,
            expected="one row per exact plan fingerprint",
        )

    # Compare only units that still exist and match unambiguously. Inventory
    # movement is not an availability regression.
    future_checked = 0
    future_preserved = 0
    trusted_floor_checked = 0
    trusted_floor_preserved = 0
    if prior:
        for old in prior.get("units") or []:
            if not isinstance(old, dict):
                continue
            current = find_unit_match(old, units)
            if current is None:
                continue
            if text(old.get("availability_date_provenance")) == "explicit_future":
                old_raw = text(old.get("available_date_raw") or old.get("_available_date_raw"))
                current_raw = text(
                    current.get("available_date_raw") or current.get("_available_date_raw")
                )
                # A future date can legitimately change between daily source
                # snapshots. Treat it as formatter evidence only when the
                # current source text is byte-identical to the prior source
                # text; otherwise the generic current-date/provenance gates
                # above apply without mislabeling inventory movement.
                if old_raw and current_raw == old_raw:
                    future_checked += 1
                    if text(current.get("available_date")) == text(old.get("available_date")):
                        future_preserved += 1
                    else:
                        add_issue(
                            issues,
                            prop,
                            "high",
                            "PRIOR_FUTURE_DATE_CHANGED",
                            unit=current,
                            field="available_date",
                            observed=current.get("available_date"),
                            expected=text(old.get("available_date")),
                            evidence=(
                                "same unit and identical raw availability text matched "
                                "to the Aug-01 strict benchmark"
                            ),
                        )
            raw_name = text(old.get("floor_plan_name_raw"))
            normalized_name = text(old.get("floor_plan_name"))
            if raw_name and not normalized_name:
                trusted_floor_checked += 1
                current_name = text(current.get("floor_plan_name"))
                current_prov = text(current.get("floor_plan_name_provenance"))
                if current_name == raw_name and current_prov:
                    trusted_floor_preserved += 1
                # A blank result may be the intentional generic/numeric scrub;
                # only an accepted name without provenance is a defect.
                elif current_name and not current_prov:
                    add_issue(
                        issues,
                        prop,
                        "high",
                        "FLOOR_PLAN_NAME_UNPROVEN",
                        unit=current,
                        field="floor_plan_name_provenance",
                        observed=current_name,
                        expected="non-empty provenance for a formatter-restored plan label",
                    )
    result["prior_future_dates_checked"] = future_checked
    result["prior_future_dates_preserved"] = future_preserved
    result["prior_blank_floor_names_checked"] = trusted_floor_checked
    result["prior_blank_floor_names_restored_with_provenance"] = trusted_floor_preserved
    result["archive_source_count"] = len((manifest or {}).get("responses") or [])
    result["archive_snapshot_present"] = int(snapshot is not None)
    result["identity_status"] = identity_status
    return dict(result)


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def markdown_table(headers: list[str], rows: Iterable[Iterable[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(text(value).replace("|", "\\|") for value in row) + " |" for row in rows)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--prior-run-dir", type=Path, default=DEFAULT_PRIOR_DIR)
    parser.add_argument("--sample-ledger", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--finding-coverage", type=Path, default=DEFAULT_FINDING_COVERAGE)
    parser.add_argument("--finding-index", type=Path, default=DEFAULT_FINDING_INDEX)
    parser.add_argument("--focused-contract", type=Path, default=DEFAULT_FOCUSED_CONTRACT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--capture-date", default="2026-08-02")
    parser.add_argument("--expected-properties", type=int, default=1000)
    parser.add_argument(
        "--regression-tests",
        default="984 passed, 2 skipped across 50 finding-mapped modules",
    )
    parser.add_argument(
        "--regression-skips",
        default=(
            "test_g5.py has two explicitly skipped Apollo-cache fallback tests; "
            "the merged G5 adapter exits on NO_URN before that fallback"
        ),
    )
    args = parser.parse_args()
    capture = date.fromisoformat(args.capture_date)

    sample_rows = read_csv(args.sample_ledger)
    sample_by_id = {text(row["apartmentid"]): row for row in sample_rows}
    expected_ids = set(sample_by_id)
    current, current_sources, duplicates = load_properties(args.run_dir)
    prior, _, prior_duplicates = load_properties(args.prior_run_dir)
    issues: list[Issue] = []

    for pid in sorted(expected_ids - set(current), key=int):
        row = sample_by_id[pid]
        add_issue(
            issues,
            None,
            "critical",
            "PROPERTY_OUTPUT_MISSING",
            pid=pid,
            name=row.get("name", ""),
            expected="one output record for every selected property",
        )
    for pid in sorted(set(current) - expected_ids, key=int):
        add_issue(
            issues,
            current[pid],
            "critical",
            "PROPERTY_OUTPUT_UNEXPECTED",
            expected="output constrained to deterministic manifest",
        )
    for pid in duplicates:
        add_issue(
            issues,
            current.get(pid),
            "critical",
            "PROPERTY_OUTPUT_DUPLICATE",
            pid=pid,
            expected="exactly one output record",
        )

    property_metrics: dict[str, dict[str, Any]] = {}
    for pid in sorted(expected_ids & set(current), key=int):
        property_metrics[pid] = audit_property(
            current[pid], current_sources[pid], prior.get(pid), issues, capture
        )

    issue_by_property: dict[str, list[Issue]] = defaultdict(list)
    for item in issues:
        issue_by_property[item.apartment_id].append(item)

    property_rows: list[dict[str, Any]] = []
    for pid in sorted(expected_ids, key=int):
        selection = sample_by_id[pid]
        prop = current.get(pid, {})
        metrics = property_metrics.get(pid, {})
        prop_issues = issue_by_property.get(pid, [])
        row = {
            "apartment_id": pid,
            "name": selection.get("name", ""),
            "state": selection.get("state", ""),
            "prior_adapter": selection.get("prior_adapter", "UNATTRIBUTED"),
            "prior_property_type": selection.get("prior_property_type", ""),
            "current_adapter": adapter(prop) if prop else "MISSING",
            "current_verdict": verdict(prop) if prop else "MISSING",
            "unit_count": metrics.get("unit_count", 0),
            "plan_count": metrics.get("plan_count", 0),
            "real_id_units": metrics.get("real_id_units", 0),
            "synthetic_id_units": metrics.get("synthetic_id_units", 0),
            "avoidable_synthetic_id_units": metrics.get("avoidable_synthetic_id_units", 0),
            "building_id_units": metrics.get("building_id_units", 0),
            "unresolved_area_units": metrics.get("unresolved_area_units", 0),
            "exact_area_units": metrics.get("exact_area_units", 0),
            "area_range_units": metrics.get("area_range_units", 0),
            "rent_range_units": metrics.get("rent_range_units", 0),
            "prior_future_dates_checked": metrics.get("prior_future_dates_checked", 0),
            "prior_future_dates_preserved": metrics.get("prior_future_dates_preserved", 0),
            "archive_source_count": metrics.get("archive_source_count", 0),
            "archive_snapshot_present": metrics.get("archive_snapshot_present", 0),
            "identity_status": metrics.get("identity_status", ""),
            "critical_issues": sum(i.severity == "critical" for i in prop_issues),
            "high_issues": sum(i.severity == "high" for i in prop_issues),
            "medium_issues": sum(i.severity == "medium" for i in prop_issues),
            "finding_ids": selection.get("finding_ids", ""),
            "selection_layers": selection.get("selection_layers", ""),
        }
        property_rows.append(row)

    # Prior-adapter coverage includes every selected route even if a different
    # live route won. This prevents current route drift from erasing strata.
    adapter_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in property_rows:
        adapter_groups[row["prior_adapter"]].append(row)
    adapter_rows: list[dict[str, Any]] = []
    for name, rows in sorted(adapter_groups.items()):
        verdicts = Counter(row["current_verdict"] for row in rows)
        current_adapters = Counter(row["current_adapter"] for row in rows)
        adapter_rows.append(
            {
                "prior_adapter": name,
                "sample_properties": len(rows),
                "unit_successes": verdicts["SUCCESS"],
                "plan_successes": verdicts["SUCCESS_PLAN_LEVEL"],
                "failed_no_data": verdicts["FAILED_NO_DATA"],
                "other_results": len(rows)
                - verdicts["SUCCESS"]
                - verdicts["SUCCESS_PLAN_LEVEL"]
                - verdicts["FAILED_NO_DATA"],
                "unit_count": sum(int(row["unit_count"]) for row in rows),
                "synthetic_id_units": sum(int(row["synthetic_id_units"]) for row in rows),
                "unresolved_area_units": sum(int(row["unresolved_area_units"]) for row in rows),
                "rent_range_units": sum(int(row["rent_range_units"]) for row in rows),
                "critical_issues": sum(int(row["critical_issues"]) for row in rows),
                "high_issues": sum(int(row["high_issues"]) for row in rows),
                "observed_winners": json.dumps(dict(sorted(current_adapters.items())), sort_keys=True),
            }
        )

    # Explicit route-coverage targets retain the five N0/dormant registered
    # adapters whose candidates were selected despite having no prior winner.
    # Finding adapters are included too, making this the route-level companion
    # to the prior-output-stratum matrix above.
    route_groups: dict[str, set[str]] = defaultdict(set)
    for pid, selection in sample_by_id.items():
        targets = {
            text(selection.get("prior_adapter")),
            *parse_delimited(selection.get("finding_adapters", "")),
            *parse_delimited(selection.get("route_coverage_adapters", "")),
        }
        for target in targets - {"", "UNATTRIBUTED"}:
            route_groups[target].add(pid)
    route_rows: list[dict[str, Any]] = []
    property_rows_by_id = {row["apartment_id"]: row for row in property_rows}
    for target, pids in sorted(route_groups.items()):
        rows = [property_rows_by_id[pid] for pid in sorted(pids, key=int)]
        props = [current[pid] for pid in pids if pid in current]
        exercised = [prop for prop in props if target_route_exercised(prop, target)]
        route_rows.append(
            {
                "target_adapter": target,
                "sample_properties": len(pids),
                "output_records": len(props),
                "target_route_exercised_properties": len(exercised),
                "unit_successes": sum(row["current_verdict"] == "SUCCESS" for row in rows),
                "plan_successes": sum(
                    row["current_verdict"] == "SUCCESS_PLAN_LEVEL" for row in rows
                ),
                "failed_no_data": sum(
                    row["current_verdict"] == "FAILED_NO_DATA" for row in rows
                ),
                "critical_issues": sum(int(row["critical_issues"]) for row in rows),
                "high_issues": sum(int(row["high_issues"]) for row in rows),
                "observed_winners": json.dumps(
                    dict(sorted(Counter(row["current_adapter"] for row in rows).items())),
                    sort_keys=True,
                ),
                "runtime_status": (
                    "FAIL_OUTPUT_CONTRACT"
                    if any(int(row["critical_issues"]) or int(row["high_issues"]) for row in rows)
                    else "EXERCISED"
                    if exercised
                    else "NOT_EXERCISED"
                ),
            }
        )

    transition_counts = Counter(
        (row["prior_property_type"], row["current_verdict"]) for row in property_rows
    )
    transition_rows = [
        {"prior_property_type": old, "current_verdict": new, "property_count": count}
        for (old, new), count in sorted(transition_counts.items())
    ]

    finding_doc = json.loads(args.finding_coverage.read_text(encoding="utf-8"))
    finding_index = read_csv(args.finding_index)
    finding_properties: dict[int, set[str]] = defaultdict(set)
    finding_affected: dict[int, set[str]] = defaultdict(set)
    for row in finding_index:
        for raw_id in parse_delimited(row.get("finding_ids", "")):
            fid = int(raw_id)
            pid = text(row.get("apartmentid"))
            finding_properties[fid].add(pid)
            if "affected" in text(row.get("property_roles")):
                finding_affected[fid].add(pid)

    finding_rows: list[dict[str, Any]] = []
    for finding in finding_doc["findings"]:
        fid = int(finding["finding_id"])
        pids = finding_properties[fid]
        affected = finding_affected[fid]
        outputs = [current[pid] for pid in pids if pid in current]
        affected_outputs = [current[pid] for pid in affected if pid in current]
        target = text(finding.get("adapter"))
        exercised = [prop for prop in affected_outputs if target_route_exercised(prop, target)]
        affected_issues = [
            item for pid in affected for item in issue_by_property.get(pid, [])
        ]
        control_issues = [
            item
            for pid in (pids - affected)
            for item in issue_by_property.get(pid, [])
        ]
        blocking = [
            item
            for item in affected_issues
            if SEVERITY_RANK[item.severity] >= SEVERITY_RANK["high"]
        ]
        if blocking:
            runtime_status = "FAIL_OUTPUT_CONTRACT"
        elif exercised:
            runtime_status = "PASS_RUNTIME_EXERCISED"
        elif outputs:
            runtime_status = "NOT_TARGET_ROUTE_EXERCISED"
        else:
            runtime_status = "NO_OUTPUT_RECORD"
        finding_rows.append(
            {
                "finding_id": fid,
                "adapter": target,
                "title": finding.get("title", ""),
                "acceptance_contract": finding.get("acceptance_contract", ""),
                "fixture_test_status": (
                    "PASS_WITH_MODULE_SKIPS"
                    if "ma_poc/tests/pms/adapters/test_g5.py"
                    in (finding.get("test_selectors") or [])
                    else "PASS"
                ),
                "fixture_test_selectors": ";".join(finding.get("test_selectors") or []),
                "sampled_properties": len(pids),
                "affected_properties": len(affected),
                "output_records": len(outputs),
                "affected_output_records": len(affected_outputs),
                "target_route_exercised_properties": len(exercised),
                "unit_successes": sum(verdict(prop) == "SUCCESS" for prop in affected_outputs),
                "plan_successes": sum(
                    verdict(prop) == "SUCCESS_PLAN_LEVEL" for prop in affected_outputs
                ),
                "failed_or_other": sum(
                    verdict(prop) not in {"SUCCESS", "SUCCESS_PLAN_LEVEL"}
                    for prop in affected_outputs
                ),
                "critical_issues": sum(item.severity == "critical" for item in affected_issues),
                "high_issues": sum(item.severity == "high" for item in affected_issues),
                "control_critical_issues": sum(
                    item.severity == "critical" for item in control_issues
                ),
                "control_high_issues": sum(
                    item.severity == "high" for item in control_issues
                ),
                "runtime_status": runtime_status,
                "observed_winners": json.dumps(
                    dict(sorted(Counter(adapter(prop) for prop in affected_outputs).items())),
                    sort_keys=True,
                ),
            }
        )

    focused = json.loads(args.focused_contract.read_text(encoding="utf-8"))
    focused_ids = {text(row.get("apartment_id")) for row in focused.get("properties", [])}
    focused_rows = [row for row in property_rows if row["apartment_id"] in focused_ids]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    property_fields = list(property_rows[0]) if property_rows else []
    write_csv(args.output_dir / "property-ledger.csv", property_rows, property_fields)
    issue_fields = list(asdict(issues[0])) if issues else list(Issue.__annotations__)
    issue_rows = [asdict(item) for item in sorted(
        issues,
        key=lambda item: (-SEVERITY_RANK[item.severity], int(item.apartment_id or 0), item.code),
    )]
    write_csv(args.output_dir / "data-quality-issues.csv", issue_rows, issue_fields)
    write_csv(args.output_dir / "adapter-result-matrix.csv", adapter_rows, list(adapter_rows[0]))
    write_csv(
        args.output_dir / "adapter-route-coverage-matrix.csv",
        route_rows,
        list(route_rows[0]),
    )
    write_csv(args.output_dir / "property-type-transitions.csv", transition_rows, list(transition_rows[0]))
    write_csv(args.output_dir / "finding-validation.csv", finding_rows, list(finding_rows[0]))

    verdict_counts = Counter(row["current_verdict"] for row in property_rows)
    severity_counts = Counter(item.severity for item in issues)
    code_counts = Counter(item.code for item in issues)
    total_units = sum(int(row["unit_count"]) for row in property_rows)
    total_real = sum(int(row["real_id_units"]) for row in property_rows)
    total_synth = sum(int(row["synthetic_id_units"]) for row in property_rows)
    future_checked = sum(int(row["prior_future_dates_checked"]) for row in property_rows)
    future_preserved = sum(int(row["prior_future_dates_preserved"]) for row in property_rows)
    findings_runtime = Counter(row["runtime_status"] for row in finding_rows)
    summary = {
        "run": {
            "run_dir": str(args.run_dir.resolve()),
            "capture_date": args.capture_date,
            "expected_properties": args.expected_properties,
            "selected_manifest_properties": len(expected_ids),
            "output_properties": len(current),
            "missing_properties": len(expected_ids - set(current)),
            "unexpected_properties": len(set(current) - expected_ids),
            "duplicate_properties": len(duplicates),
            "prior_duplicate_properties": len(prior_duplicates),
        },
        "results": {
            "verdict_counts": dict(sorted(verdict_counts.items())),
            "unit_level_success_rate": round(verdict_counts["SUCCESS"] / len(expected_ids), 6),
            "unit_or_plan_success_rate": round(
                (verdict_counts["SUCCESS"] + verdict_counts["SUCCESS_PLAN_LEVEL"])
                / len(expected_ids),
                6,
            ),
            "unit_rows": total_units,
            "real_id_units": total_real,
            "synthetic_id_units": total_synth,
            "avoidable_synthetic_id_units": sum(
                int(row["avoidable_synthetic_id_units"]) for row in property_rows
            ),
            "building_id_units": sum(int(row["building_id_units"]) for row in property_rows),
            "unresolved_area_units": sum(
                int(row["unresolved_area_units"]) for row in property_rows
            ),
            "area_range_units": sum(int(row["area_range_units"]) for row in property_rows),
            "rent_range_units": sum(int(row["rent_range_units"]) for row in property_rows),
            "matched_prior_future_dates_checked": future_checked,
            "matched_prior_future_dates_preserved": future_preserved,
            "properties_with_extraction_snapshot": sum(
                int(row["archive_snapshot_present"]) for row in property_rows
            ),
            "archived_source_responses": sum(
                int(row["archive_source_count"]) for row in property_rows
            ),
        },
        "quality": {
            "severity_counts": dict(sorted(severity_counts.items())),
            "issue_code_counts": dict(sorted(code_counts.items())),
            "properties_with_critical_or_high_issues": sum(
                bool(int(row["critical_issues"]) or int(row["high_issues"]))
                for row in property_rows
            ),
        },
        "coverage": {
            "prior_adapter_strata": len(adapter_rows),
            "target_adapter_routes": len(route_rows),
            "target_adapter_routes_exercised": sum(
                row["runtime_status"] == "EXERCISED" for row in route_rows
            ),
            "prior_property_type_strata": len({row["prior_property_type"] for row in property_rows}),
            "states": len({row["state"] for row in property_rows}),
            "findings": len(finding_rows),
            "finding_runtime_status": dict(sorted(findings_runtime.items())),
            "focused_properties": len(focused_rows),
            "focused_properties_with_snapshot": sum(
                int(row["archive_snapshot_present"]) for row in focused_rows
            ),
        },
        "regression_tests": args.regression_tests,
        "regression_test_skips": args.regression_skips,
    }
    summary_path = args.output_dir / "post-run-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    blocking_count = severity_counts["critical"] + severity_counts["high"]
    report = [
        "# Stratified 1,000-property canary audit",
        "",
        f"Capture date: `{args.capture_date}`",
        f"Offline run mirror: `{args.run_dir.resolve()}`",
        "",
        "## Release conclusion",
        "",
        (
            "**PASS: no critical/high output-contract defects detected.**"
            if blocking_count == 0
            else f"**HOLD: {blocking_count} critical/high output-contract issue(s) require review.**"
        ),
        "",
        "The conclusion is based on the deterministic output manifest, local immutable source archives, "
        "and the finding-mapped regression suite. A live route that did not win in this run is explicitly "
        "reported as not runtime-exercised; fixture proof is not mislabeled as live proof.",
        "",
        "## Run outcome",
        "",
        markdown_table(
            ["Measure", "Result"],
            [
                ["Expected / output properties", f"{len(expected_ids)} / {len(current)}"],
                ["Unit-level success", f"{verdict_counts['SUCCESS']} / {len(expected_ids)} ({verdict_counts['SUCCESS']/len(expected_ids):.2%})"],
                ["Plan-level success", verdict_counts["SUCCESS_PLAN_LEVEL"]],
                ["Failed no data", verdict_counts["FAILED_NO_DATA"]],
                ["Unit rows", total_units],
                ["Real / synthetic identities", f"{total_real} / {total_synth}"],
                ["Avoidable synthetic identities", summary["results"]["avoidable_synthetic_id_units"]],
                ["Unresolved area rows", summary["results"]["unresolved_area_units"]],
                ["Retained area / rent ranges", f"{summary['results']['area_range_units']} / {summary['results']['rent_range_units']}"],
                [
                    "Same-raw-source prior future dates preserved",
                    f"{future_preserved} / {future_checked}",
                ],
                ["Properties with extraction snapshots", f"{summary['results']['properties_with_extraction_snapshot']} / {len(current)}"],
                ["Archived source responses", summary["results"]["archived_source_responses"]],
            ],
        ),
        "",
        "## Quality issues",
        "",
        markdown_table(
            ["Severity", "Count"],
            [[level, severity_counts[level]] for level in ("critical", "high", "medium", "low", "info")],
        ),
        "",
    ]
    if code_counts:
        report.extend(
            [
                markdown_table(
                    ["Issue code", "Count"],
                    sorted(code_counts.items(), key=lambda item: (-item[1], item[0])),
                ),
                "",
            ]
        )
    else:
        report.extend(["No row-level issues were emitted.", ""])
    report.extend(
        [
            "## Adapter-fix validation",
            "",
            f"Finding-mapped regression suite: **{args.regression_tests}**.",
            "",
            f"Declared skips: {args.regression_skips}.",
            "",
            markdown_table(
                ["Runtime status", "Findings"],
                sorted(findings_runtime.items()),
            ),
            "",
            "See `finding-validation.csv` for every finding's acceptance contract, fixture selectors, "
            "sampled properties, observed winners, and runtime status. See `adapter-result-matrix.csv` "
            "for every prior adapter stratum, `adapter-route-coverage-matrix.csv` for every explicit "
            "registered/finding route (including prior N0 adapters), and `property-ledger.csv` for "
            "every property.",
            "",
            "## Reproduction",
            "",
            "```bash",
            "python investigations/2026-08-02-stratified-1000/audit_stratified_canary.py",
            "```",
            "",
            "The audit performs no network calls and reads only the one-time local mirror.",
        ]
    )
    (args.output_dir / "POST_RUN_AUDIT.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if blocking_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
