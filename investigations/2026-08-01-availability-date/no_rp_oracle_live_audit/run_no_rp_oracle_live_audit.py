#!/usr/bin/env python3
"""Build a separate current-live audit for July-31 capture-date-only tiers.

This investigation deliberately does *not* use the RealPage comparison file as
an oracle.  Its historical denominator is the exact set of July-31 SurgeX rows
from these source families:

* ``TIER_1_API_REALPAGE_OLL``
* ``TIER_1_API_ENTRATA``
* ``TIER_1_API_ONESITE``
* ``TIER_1_API_ONESITE_WORKFLOW``
* every configured ``aspensquare.com`` property (the operator spans tiers)
* ``TIER_1_DOM_SQUARESPACE_UNIT_BLOCK``

The expected denominator is 573 rows across 78 mutually-exclusive properties.
It is intentionally separate from the RP-matched cohort and from the exact
SecureCafe audit in the sibling directory.

The live lane probes representative current public sources and records exact
raw availability values.  It never solves CAPTCHAs.  Direct requests use
``curl_cffi`` browser impersonation.  The optional ``--allow-hyperbrowser``
flag permits a clean residential render (CAPTCHA solving remains hard-disabled
in the repository backend) only when an Entrata conventional page is blocked.
No adapter or production file is modified.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import re
import subprocess
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ma_poc.pms.adapters.knock import find_knock_community_hash  # noqa: E402
from ma_poc.core.schema_v2 import _format_v2_unit  # noqa: E402
from ma_poc.pms.adapters._api_parser import (  # noqa: E402
    realpage_units_to_adapter_shape,
)
from ma_poc.pms.adapters._avail_table_recovery import (  # noqa: E402
    parse_squarespace_unit_blocks,
)
from ma_poc.pms.adapters.entrata import (  # noqa: E402
    parse_entrata_prospectportal_html,
)
from ma_poc.pms.adapters.knock import parse_knock_units  # noqa: E402
from ma_poc.pms.adapters.onesite import (  # noqa: E402
    _XYZ_IMPERSONATE_CHAIN,
    _XYZ_USER_AGENT,
    _generate_xyz_token,
    _onesite_workflowstartup_url,
    parse_onesite_workflowstartup,
)


RESULT_TYPE = "local_current_live_no_rp_oracle_audit_not_canary"
JULY_CAPTURE_DATE = date(2026, 7, 31)
LIVE_CAPTURE_DATE = date(2026, 8, 1)
EXPECTED_PROPERTIES = 78
EXPECTED_ROWS = 573

CATEGORY_BY_TIER = {
    "TIER_1_API_REALPAGE_OLL": "REALPAGE_OLL",
    "TIER_1_API_ENTRATA": "ENTRATA_API",
    "TIER_1_API_ONESITE": "ONESITE_API",
    "TIER_1_API_ONESITE_WORKFLOW": "ONESITE_WORKFLOW",
    "TIER_1_DOM_SQUARESPACE_UNIT_BLOCK": "SQUARESPACE_UNIT_BLOCK",
}

# Probe >=3 per category where the historical fleet permits it.  The two
# smaller categories contain only 2 and 1 properties respectively, so the
# complete category is probed and the limitation is explicit in the summary.
PROBE_SELECTION: dict[str, tuple[str, ...]] = {
    "REALPAGE_OLL": ("2114", "263789", "263081", "72054"),
    "ENTRATA_API": ("257301", "293343", "67722"),
    "ONESITE_API": ("25395", "15079", "7886", "76982", "269984"),
    "ONESITE_WORKFLOW": ("15704", "283561"),
    "ASPENSQUARE_OPERATOR": ("6526", "16186", "4079"),
    "SQUARESPACE_UNIT_BLOCK": ("241432",),
}

FIVE_FAMILY_CATEGORIES: dict[str, tuple[str, ...]] = {
    "REALPAGE_OLL_API": ("REALPAGE_OLL",),
    "ENTRATA_API": ("ENTRATA_API",),
    "ONESITE_API_AND_WORKFLOW": ("ONESITE_API", "ONESITE_WORKFLOW"),
    "ASPENSQUARE": ("ASPENSQUARE_OPERATOR",),
    "SQUARESPACE": ("SQUARESPACE_UNIT_BLOCK",),
}

REALPAGE_PROPERTY_ID_RE = re.compile(
    r"propertyId\s*=\s*['\"]?(\d+)", re.IGNORECASE
)
REALPAGE_API_KEY_RE = re.compile(
    r"apiKey\s*:\s*['\"]([^'\"]+)", re.IGNORECASE
)
ONESITE_PORTAL_RE = re.compile(
    r"https?://[a-z0-9-]+\.onlineleasing\.realpage\.com/?",
    re.IGNORECASE,
)
ONESITE_SITE_ID_RE = re.compile(
    r"(?:widgetLoader\.js\?[^\"'<>]*?siteId=|"
    r"onesite\.realpage\.com/welcomehome/?\?[^\"'<>]*?siteID=)(\d+)",
    re.IGNORECASE,
)
MONEY_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{2})?)")
UNIT_BLOCK_RE = re.compile(r"^\s*Unit\s+(.+?)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class HttpResult:
    requested_url: str
    final_url: str
    status: int
    text: str
    error: str
    impersonation: str
    attempts: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--units",
        type=Path,
        default=Path("/Users/ankur/Downloads/scraped_units_2026-07-31.csv"),
        help="July-31 SurgeX unit output.",
    )
    parser.add_argument(
        "--properties",
        type=Path,
        default=REPO_ROOT / "properties.csv",
        help="Configured property metadata.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=SCRIPT_PATH.parent,
        help="Destination for generated CSV/JSON evidence.",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--allow-hyperbrowser",
        action="store_true",
        help=(
            "Allow one clean Hyperbrowser render per selected Entrata property "
            "when its conventional page is blocked. CAPTCHA solving is disabled."
        ),
    )
    parser.add_argument(
        "--allow-cohort-drift",
        action="store_true",
        help="Permit an input other than the expected 573 rows / 78 properties.",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_value(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def text_bool(value: bool | None) -> str:
    if value is None:
        return ""
    return "true" if value else "false"


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def compact_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def name_match(expected: str, observed: str) -> bool:
    left = normalize_text(expected)
    right = normalize_text(observed)
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    stop = {
        "apartments",
        "apartment",
        "homes",
        "home",
        "at",
        "the",
        "residences",
        "community",
    }
    l_tokens = {token for token in left.split() if token not in stop}
    r_tokens = {token for token in right.split() if token not in stop}
    overlap = len(l_tokens & r_tokens) / max(1, len(l_tokens))
    return overlap >= 0.65 or SequenceMatcher(None, left, right).ratio() >= 0.72


def address_match(expected: str, observed: str) -> bool:
    left = normalize_text(expected)
    right = normalize_text(observed)
    if not left or not right:
        return False
    number = next((token for token in left.split() if token.isdigit()), "")
    street_tokens = [
        token
        for token in left.split()
        if token not in {number, "n", "s", "e", "w", "north", "south", "east", "west"}
        and len(token) >= 3
    ]
    return bool(number and number in right and any(token in right for token in street_tokens))


def identity_verdict(
    expected_name: str,
    expected_address: str,
    observed_name: str,
    observed_address: str,
    page_text: str = "",
) -> tuple[bool, str]:
    n_ok = name_match(expected_name, observed_name)
    a_ok = address_match(expected_address, observed_address)
    page_name_ok = normalize_text(expected_name) in normalize_text(page_text)
    page_a_ok = address_match(expected_address, page_text)
    if (n_ok or page_name_ok) and (a_ok or page_a_ok):
        if n_ok and a_ok:
            return True, "structured_name_and_address_match"
        return True, "configured_name_and_address_visible_on_property_page"
    if n_ok and not expected_address:
        return True, "name_match_no_configured_address"
    return False, (
        f"name_match={text_bool(n_ok)};name_visible={text_bool(page_name_ok)};"
        f"address_match={text_bool(a_ok)};address_visible={text_bool(page_a_ok)}"
    )


def fetch_get(
    url: str,
    timeout: float,
    *,
    headers: dict[str, str] | None = None,
    impersonations: tuple[str, ...] = ("chrome120", "chrome124"),
) -> HttpResult:
    best: HttpResult | None = None
    for attempt, impersonation in enumerate(impersonations, start=1):
        try:
            response = requests.get(
                url,
                headers=headers,
                impersonate=impersonation,
                timeout=timeout,
                allow_redirects=True,
            )
            current = HttpResult(
                requested_url=url,
                final_url=str(response.url),
                status=int(response.status_code),
                text=response.text or "",
                error="",
                impersonation=impersonation,
                attempts=attempt,
            )
        except Exception as exc:
            current = HttpResult(
                requested_url=url,
                final_url=url,
                status=0,
                text="",
                error=f"{type(exc).__name__}: {str(exc)[:160]}",
                impersonation=impersonation,
                attempts=attempt,
            )
        best = current
        if current.status == 200 and current.text:
            return current
    assert best is not None
    return best


def first_json_response(body: Any) -> Any:
    if isinstance(body, dict):
        return body.get("response")
    return None


def parse_isoish_date(raw: Any, capture_date: date) -> date | None:
    value = compact_text(raw)
    if not value:
        return None
    value = re.sub(r"^available\s+", "", value, flags=re.IGNORECASE)
    value = value.replace("Available on ", "").strip()
    match = re.search(r"(20\d{2})-(\d{2})-(\d{2})", value)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    for fmt in (
        "%m/%d/%Y",
        "%m-%d-%Y",
        "%m/%d/%y",
        "%m-%d-%y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%b %d %Y",
        "%B %d %Y",
    ):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    short = re.fullmatch(r"(\d{1,2})/(\d{1,2})", value)
    if short:
        try:
            return date(capture_date.year, int(short.group(1)), int(short.group(2)))
        except ValueError:
            return None
    return None


def availability_semantic(raw: Any, capture_date: date) -> tuple[str, str]:
    value = compact_text(raw)
    low = value.lower()
    if not value:
        return "source_blank", ""
    if re.search(r"\bnot\s+available\b|\bwaitlist\b|get notified", low):
        return "unavailable", ""
    if re.search(r"\bavailable\s+now\b|\bimmediate(?:ly)?\b", low):
        return "available_now", capture_date.isoformat()
    parsed = parse_isoish_date(value, capture_date)
    if parsed is not None:
        if parsed > capture_date:
            if (parsed - capture_date).days > 5 * 365:
                return "sentinel_future", parsed.isoformat()
            return "explicit_future", parsed.isoformat()
        if parsed == capture_date:
            return "explicit_capture_date", parsed.isoformat()
        if (capture_date - parsed).days > 5 * 365:
            return "historical_sentinel", parsed.isoformat()
        return "historical_embedded", parsed.isoformat()
    if re.search(r"\bavailable\b|\bvacant\b", low):
        return "available_state_no_date", ""
    return "unparsed_availability_text", ""


def safe_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def source_subset(value: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    return {key: value.get(key) for key in keys if key in value}


ADAPTER_DATE_KEYS = (
    "available_date",
    "availability_date",
    "internalAvailableDate",
    "availableDate",
    "date_available",
    "dateAvailable",
)


def adapter_availability_value(row: dict[str, Any] | None) -> tuple[str, str]:
    if not row:
        return "", ""
    for key in ADAPTER_DATE_KEYS:
        value = compact_text(row.get(key))
        if value:
            return key, value
    return "", ""


def adapter_row_queues(
    rows: Iterable[dict[str, Any]], *anchor_keys: str
) -> dict[str, list[dict[str, Any]]]:
    queues: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        anchor = next(
            (
                normalize_text(row.get(key))
                for key in anchor_keys
                if normalize_text(row.get(key))
            ),
            "",
        )
        if anchor:
            queues[anchor].append(row)
    return queues


def take_adapter_row(
    queues: dict[str, list[dict[str, Any]]], anchor: Any
) -> dict[str, Any] | None:
    values = queues.get(normalize_text(anchor)) or []
    return values.pop(0) if values else None


def formatter_trace(
    adapter_row: dict[str, Any] | None,
    *,
    captured_at: str,
    property_id: str,
) -> tuple[dict[str, Any], str]:
    if adapter_row is None:
        return {}, ""
    try:
        scrape_ts = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        unit_anchor = compact_text(
            adapter_row.get("unit_id") or adapter_row.get("unit_number")
        )
        return (
            _format_v2_unit(
                dict(adapter_row),
                scrape_ts,
                property_id=property_id,
                property_plan_level=not bool(unit_anchor),
            ),
            "",
        )
    except Exception as exc:  # audit trace must retain the native evidence
        return {}, f"{type(exc).__name__}: {str(exc)[:180]}"


def pipeline_classification(
    *,
    source_semantic: str,
    source_normalized: str,
    source_field: str,
    adapter_row_present: bool,
    adapter_route_selected: bool,
    adapter_row_absence_reason: str,
    adapter_value: str,
    adapter_normalized: str,
    formatter_date: str,
    formatter_provenance: str,
    formatter_error: str,
) -> tuple[str, str, str]:
    """Return ``(pipeline_outcome, loss_classification, confidence)``.

    This is deliberately semantic, not distributional: a capture-date output
    is only called a loss when the exact native row carried a concrete future
    date and the current parser/formatter trace failed to preserve it.
    """
    if formatter_error:
        return "FORMATTER_TRACE_ERROR", "formatter_trace_error", "LOW"
    if not adapter_route_selected:
        if source_semantic == "explicit_future":
            return (
                "NATIVE_FUTURE_ON_UNWIRED_ROUTE",
                "adapter_route_selection_loss",
                "HIGH",
            )
        return "NATIVE_SOURCE_ROUTE_NOT_WIRED", "adapter_route_selection_gap", "HIGH"
    if not adapter_row_present:
        if source_semantic == "explicit_future":
            if adapter_row_absence_reason == "unsupported_response_units_envelope":
                return (
                    "NATIVE_FUTURE_DROPPED_WITH_RESPONSE_ENVELOPE",
                    "adapter_response_shape_loss",
                    "HIGH",
                )
            return "NATIVE_FUTURE_ROW_DROPPED", "adapter_row_selection_loss", "HIGH"
        return "NATIVE_ROW_DROPPED", "adapter_row_selection_loss", "MEDIUM"

    if source_semantic == "explicit_future":
        if adapter_normalized != source_normalized:
            if source_field == "internalAvailableDate" and not adapter_value:
                return "NATIVE_FUTURE_DROPPED_BY_ADAPTER", "adapter_key_alias_loss", "HIGH"
            if not adapter_value:
                return (
                    "NATIVE_FUTURE_DROPPED_BY_ADAPTER",
                    "adapter_date_extraction_loss",
                    "HIGH",
                )
            return (
                "NATIVE_FUTURE_CHANGED_BY_ADAPTER",
                "adapter_date_normalization_mismatch",
                "HIGH",
            )
        if formatter_date != source_normalized:
            return (
                "ADAPTER_FUTURE_DROPPED_BY_FORMATTER",
                "formatter_date_loss_or_change",
                "HIGH",
            )
        return "EXPLICIT_FUTURE_PRESERVED_END_TO_END", "none", "HIGH"

    if source_semantic == "available_now":
        if formatter_date == LIVE_CAPTURE_DATE.isoformat():
            if formatter_provenance == "available_now":
                return "AVAILABLE_NOW_PRESERVED_END_TO_END", "none", "HIGH"
            return (
                "AVAILABLE_NOW_DATE_PRESERVED_BUT_SOURCE_TOKEN_DROPPED",
                "adapter_available_now_provenance_loss",
                "HIGH",
            )
        return "AVAILABLE_NOW_NOT_NORMALIZED_TO_CAPTURE_DATE", "normalization_loss", "HIGH"

    if source_semantic in {"source_blank", "available_state_no_date"}:
        if formatter_date == LIVE_CAPTURE_DATE.isoformat():
            return (
                "SOURCE_NO_DATE_NORMALIZED_TO_CAPTURE_DATE",
                "source_no_date_capture_date_default",
                "HIGH",
            )
        return "SOURCE_NO_DATE_REMAINS_UNKNOWN", "none", "HIGH"

    if source_semantic == "explicit_capture_date":
        if formatter_date == source_normalized:
            return "EXPLICIT_CAPTURE_DATE_PRESERVED", "none", "HIGH"
        return "EXPLICIT_CAPTURE_DATE_CHANGED", "normalization_loss", "HIGH"

    if source_semantic == "historical_embedded":
        if formatter_date == source_normalized:
            return "HISTORICAL_SOURCE_DATE_PRESERVED", "none", "HIGH"
        if formatter_provenance == "sentinel_clamped":
            return "HISTORICAL_SENTINEL_CLAMPED", "intentional_sanity_clamp", "HIGH"
        return "HISTORICAL_SOURCE_DATE_CHANGED", "normalization_loss", "HIGH"

    if source_semantic in {"sentinel_future", "historical_sentinel"}:
        return "OUT_OF_RANGE_SOURCE_SENTINEL", "source_sentinel_date", "HIGH"

    if source_semantic == "unavailable":
        return "SOURCE_UNAVAILABLE", "none", "HIGH"
    return "UNPARSED_NATIVE_AVAILABILITY", "unparsed_source_text", "MEDIUM"


def evidence_row(
    *,
    captured_at: str,
    category: str,
    property_id: str,
    configured_name: str,
    source_kind: str,
    evidence_url: str,
    source_row_id: str,
    floor_plan_name: str,
    raw_availability: str,
    source_field: str,
    raw_evidence: dict[str, Any],
    primary: bool = True,
    adapter_parser: str = "",
    adapter_trace_method: str = "exact_current_parser",
    adapter_row: dict[str, Any] | None = None,
    adapter_route_selected: bool = True,
    adapter_row_absence_reason: str = "",
    diagnostic_adapter_row: dict[str, Any] | None = None,
    diagnostic_trace_method: str = "",
    trace_note: str = "",
) -> dict[str, Any]:
    semantic, normalized = availability_semantic(raw_availability, LIVE_CAPTURE_DATE)
    adapter_key, adapter_value = adapter_availability_value(adapter_row)
    adapter_semantic, adapter_normalized = availability_semantic(
        adapter_value, LIVE_CAPTURE_DATE
    )
    formatter, formatter_error = formatter_trace(
        adapter_row, captured_at=captured_at, property_id=property_id
    )
    formatter_date = compact_text(formatter.get("available_date"))
    formatter_provenance = compact_text(
        formatter.get("availability_date_provenance")
    )
    formatter_raw = compact_text(formatter.get("_available_date_raw"))
    outcome, loss, confidence = pipeline_classification(
        source_semantic=semantic,
        source_normalized=normalized,
        source_field=source_field,
        adapter_row_present=adapter_row is not None,
        adapter_route_selected=adapter_route_selected,
        adapter_row_absence_reason=adapter_row_absence_reason,
        adapter_value=adapter_value,
        adapter_normalized=adapter_normalized,
        formatter_date=formatter_date,
        formatter_provenance=formatter_provenance,
        formatter_error=formatter_error,
    )
    adapter_alias_miss = loss == "adapter_key_alias_loss"
    diagnostic_key, diagnostic_value = adapter_availability_value(
        diagnostic_adapter_row
    )
    _, diagnostic_normalized = availability_semantic(
        diagnostic_value, LIVE_CAPTURE_DATE
    )
    diagnostic_formatter, diagnostic_formatter_error = formatter_trace(
        diagnostic_adapter_row,
        captured_at=captured_at,
        property_id=property_id,
    )
    diagnostic_formatter_date = compact_text(
        diagnostic_formatter.get("available_date")
    )
    diagnostic_formatter_provenance = compact_text(
        diagnostic_formatter.get("availability_date_provenance")
    )
    diagnostic_gap = ""
    if diagnostic_adapter_row is not None and semantic == "explicit_future":
        if source_field == "internalAvailableDate" and not diagnostic_value:
            diagnostic_gap = "adapter_key_alias_loss_after_response_unwrap"
        elif diagnostic_normalized != normalized:
            diagnostic_gap = "adapter_date_loss_after_response_unwrap"
        elif diagnostic_formatter_date != normalized:
            diagnostic_gap = "formatter_loss_after_response_unwrap"
        else:
            diagnostic_gap = "explicit_future_preserved_after_response_unwrap"
    return {
        "result_type": RESULT_TYPE,
        "capture_timestamp_utc": captured_at,
        "capture_date": LIVE_CAPTURE_DATE.isoformat(),
        "historical_run_date": JULY_CAPTURE_DATE.isoformat(),
        "denominator_scope": "separate_no_rp_oracle_573_rows_78_properties",
        "category": category,
        "property_id": property_id,
        "configured_name": configured_name,
        "source_kind": source_kind,
        "primary_property_evidence": text_bool(primary),
        "evidence_url": evidence_url,
        "source_row_id": source_row_id,
        "floor_plan_name": floor_plan_name,
        "raw_availability_value": compact_text(raw_availability),
        "normalized_availability_date": normalized,
        "availability_semantic": semantic,
        "explicit_future": text_bool(semantic == "explicit_future"),
        "source_field": source_field,
        "adapter_parser": adapter_parser,
        "adapter_trace_method": adapter_trace_method,
        "adapter_route_selected": text_bool(adapter_route_selected),
        "adapter_row_present": text_bool(adapter_row is not None),
        "adapter_row_absence_reason": adapter_row_absence_reason,
        "adapter_availability_key": adapter_key,
        "adapter_availability_value": adapter_value,
        "adapter_normalized_availability_date": adapter_normalized,
        "adapter_availability_semantic": adapter_semantic,
        "adapter_date_alias_hit": text_bool(bool(adapter_value)),
        "missed_future_by_adapter_alias": text_bool(adapter_alias_miss),
        "formatter_available_date": formatter_date,
        "formatter_available_date_raw": formatter_raw,
        "formatter_availability_date_provenance": formatter_provenance,
        "formatter_error": formatter_error,
        "explicit_future_preserved_by_adapter": text_bool(
            semantic == "explicit_future" and adapter_normalized == normalized
        ),
        "explicit_future_preserved_by_formatter": text_bool(
            semantic == "explicit_future" and formatter_date == normalized
        ),
        "formatter_capture_date_default": text_bool(
            formatter_provenance == "capture_date_default"
        ),
        "pipeline_outcome": outcome,
        "loss_classification": loss,
        "confidence": confidence,
        "trace_note": trace_note,
        "adapter_row_json": safe_json(adapter_row or {}),
        "formatter_row_json": safe_json(formatter),
        "diagnostic_trace_method": diagnostic_trace_method,
        "diagnostic_adapter_row_present": text_bool(
            diagnostic_adapter_row is not None
        ),
        "diagnostic_adapter_availability_key": diagnostic_key,
        "diagnostic_adapter_availability_value": diagnostic_value,
        "diagnostic_adapter_normalized_availability_date": diagnostic_normalized,
        "diagnostic_formatter_available_date": diagnostic_formatter_date,
        "diagnostic_formatter_availability_date_provenance": (
            diagnostic_formatter_provenance
        ),
        "diagnostic_formatter_error": diagnostic_formatter_error,
        "diagnostic_gap_classification": diagnostic_gap,
        "diagnostic_adapter_row_json": safe_json(diagnostic_adapter_row or {}),
        "diagnostic_formatter_row_json": safe_json(diagnostic_formatter),
        "raw_evidence_json": safe_json(raw_evidence),
    }


def jsonld_identity(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html or "", "lxml")
    candidates: list[dict[str, Any]] = []
    for block in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(block.get_text(strip=True))
        except Exception:
            continue
        if isinstance(value, dict):
            if isinstance(value.get("@graph"), list):
                candidates.extend(x for x in value["@graph"] if isinstance(x, dict))
            candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(x for x in value if isinstance(x, dict))
    wanted = {
        "ApartmentComplex",
        "LocalBusiness",
        "Residence",
        "Apartment",
        "Organization",
    }
    node = next(
        (
            item
            for item in candidates
            if (
                item.get("@type") in wanted
                or (
                    isinstance(item.get("@type"), list)
                    and wanted.intersection(set(item["@type"]))
                )
            )
        ),
        candidates[0] if candidates else {},
    )
    name = compact_text(node.get("name")) if isinstance(node, dict) else ""
    addr = node.get("address") if isinstance(node, dict) else None
    if isinstance(addr, dict):
        observed_address = compact_text(
            " ".join(
                str(addr.get(key) or "")
                for key in (
                    "streetAddress",
                    "address1",
                    "addressLocality",
                    "city",
                    "addressRegion",
                    "state",
                    "postalCode",
                    "zip",
                )
            )
        )
    else:
        observed_address = compact_text(addr)
    if not name:
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        name = compact_text(title.split("|")[0].split("—")[0])
    return name, observed_address


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def category_for(row: dict[str, str], metadata: dict[str, dict[str, str]]) -> str:
    property_id = str(row.get("property_id") or "")
    website = str((metadata.get(property_id) or {}).get("website") or "").lower()
    if "aspensquare.com" in website:
        return "ASPENSQUARE_OPERATOR"
    return CATEGORY_BY_TIER.get(str(row.get("extraction_tier") or ""), "")


def build_july_property_ledger(
    unit_rows: list[dict[str, str]], metadata: dict[str, dict[str, str]]
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in unit_rows:
        category = category_for(row, metadata)
        if category:
            grouped[(str(row["property_id"]), category)].append(row)

    ledger: list[dict[str, Any]] = []
    for (property_id, category), rows in sorted(
        grouped.items(), key=lambda item: (item[0][1], int(item[0][0]))
    ):
        meta = metadata.get(property_id, {})
        dates = [compact_text(row.get("available_date")) for row in rows]
        selected = property_id in PROBE_SELECTION.get(category, ())
        ledger.append(
            {
                "result_type": "historical_2026_07_31_separate_denominator",
                "historical_run_date": JULY_CAPTURE_DATE.isoformat(),
                "denominator_scope": "separate_no_rp_oracle_573_rows_78_properties",
                "category": category,
                "property_id": property_id,
                "property_name": str(meta.get("name") or rows[0].get("property_name") or ""),
                "configured_address": str(meta.get("address") or ""),
                "configured_city": str(meta.get("city") or ""),
                "configured_state": str(meta.get("state") or ""),
                "website": str(meta.get("website") or ""),
                "july_rows": len(rows),
                "july_unit_rows": sum(
                    str(row.get("is_floor_plan_level") or "").upper() == "FALSE"
                    for row in rows
                ),
                "july_plan_rows": sum(
                    str(row.get("is_floor_plan_level") or "").upper() == "TRUE"
                    for row in rows
                ),
                "july_capture_date_rows": sum(
                    value == JULY_CAPTURE_DATE.isoformat() for value in dates
                ),
                "july_blank_date_rows": sum(not value for value in dates),
                "july_other_date_rows": sum(
                    bool(value) and value != JULY_CAPTURE_DATE.isoformat()
                    for value in dates
                ),
                "july_extraction_tiers": ";".join(
                    sorted({str(row.get("extraction_tier") or "") for row in rows})
                ),
                "selected_for_current_live_probe": text_bool(selected),
            }
        )
    return ledger


def realpage_identity(payload: Any) -> tuple[str, str]:
    response = first_json_response(payload)
    if not isinstance(response, dict):
        return "", ""
    observed_name = compact_text(response.get("name"))
    address = response.get("address")
    if isinstance(address, dict):
        observed_address = compact_text(
            " ".join(
                str(address.get(key) or "")
                for key in (
                    "address1",
                    "address2",
                    "cityName",
                    "stateCode",
                    "postalCode",
                )
            )
        )
    else:
        observed_address = compact_text(address)
    return observed_name, observed_address


def realpage_units(payload: Any) -> list[dict[str, Any]]:
    response = first_json_response(payload)
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if isinstance(response, dict) and isinstance(response.get("units"), list):
        return [item for item in response["units"] if isinstance(item, dict)]
    return []


def probe_realpage_widget(
    meta: dict[str, str],
    category: str,
    captured_at: str,
    timeout: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    property_id = str(meta["apartmentid"])
    homepage = fetch_get(str(meta["website"]), timeout)
    prop_match = REALPAGE_PROPERTY_ID_RE.search(homepage.text)
    key_match = REALPAGE_API_KEY_RE.search(homepage.text)
    if homepage.status != 200 or not prop_match or not key_match:
        return (
            {
                "status": "FETCH_FAILED",
                "error": (
                    f"marketing_status={homepage.status};property_id={bool(prop_match)};"
                    f"api_key={bool(key_match)};error={homepage.error}"
                ),
                "identity_name": "",
                "identity_address": "",
                "identity_match": False,
                "identity_reason": "widget_configuration_not_found",
                "evidence_urls": [homepage.final_url],
                "access_path": "curl_cffi_current_live",
                "source_items": 0,
            },
            [],
        )
    rp_property_id = prop_match.group(1)
    api_key = key_match.group(1)
    origin = f"{urlparse(homepage.final_url).scheme}://{urlparse(homepage.final_url).netloc}"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": origin,
        "Referer": homepage.final_url,
        "x-ws-authkey": api_key,
    }
    api_base = f"https://api.ws.realpage.com/v2/property/{rp_property_id}"
    details_url = f"{api_base}/PropertyDetails"
    units_url = f"{api_base}/units?available=true&honordisplayorder=true"
    details = fetch_get(details_url, timeout, headers=headers)
    units_response = fetch_get(units_url, timeout, headers=headers)
    details_body: Any = {}
    units_body: Any = {}
    try:
        details_body = json.loads(details.text)
    except Exception:
        pass
    try:
        units_body = json.loads(units_response.text)
    except Exception:
        pass
    observed_name, observed_address = realpage_identity(details_body)
    identity_ok, identity_reason = identity_verdict(
        str(meta.get("name") or ""),
        str(meta.get("address") or ""),
        observed_name,
        observed_address,
        homepage.text,
    )
    raw_units = realpage_units(units_body)
    response_payload = first_json_response(units_body)
    response_units_envelope = bool(
        isinstance(response_payload, dict)
        and isinstance(response_payload.get("units"), list)
    )
    adapter_rows = realpage_units_to_adapter_shape(units_body, units_url)
    adapter_by_unit = adapter_row_queues(adapter_rows, "unit_number", "unit_id")
    diagnostic_rows = (
        realpage_units_to_adapter_shape({"response": raw_units}, units_url)
        if response_units_envelope
        else []
    )
    diagnostic_by_unit = adapter_row_queues(
        diagnostic_rows, "unit_number", "unit_id"
    )
    rows: list[dict[str, Any]] = []
    for unit in raw_units:
        source_field = next(
            (
                field
                for field in (
                    "internalAvailableDate",
                    "availableDate",
                    "available_date",
                    "vacantDate",
                )
                if unit.get(field) not in (None, "")
            ),
            "",
        )
        raw_date = compact_text(unit.get(source_field)) if source_field else ""
        unit_anchor = compact_text(
            unit.get("unitNumber") or unit.get("name") or unit.get("id")
        )
        adapter_row = take_adapter_row(adapter_by_unit, unit_anchor)
        diagnostic_adapter_row = take_adapter_row(diagnostic_by_unit, unit_anchor)
        rows.append(
            evidence_row(
                captured_at=captured_at,
                category=category,
                property_id=property_id,
                configured_name=str(meta.get("name") or ""),
                source_kind="realpage_public_widget_units_api",
                evidence_url=units_url,
                source_row_id=unit_anchor,
                floor_plan_name=compact_text(
                    unit.get("floorPlanName") or unit.get("floorplanName")
                ),
                raw_availability=raw_date,
                source_field=source_field or "no_date_field",
                adapter_parser="_api_parser.realpage_units_to_adapter_shape",
                adapter_row=adapter_row,
                adapter_row_absence_reason=(
                    "unsupported_response_units_envelope"
                    if response_units_envelope and adapter_row is None
                    else ""
                ),
                diagnostic_adapter_row=diagnostic_adapter_row,
                diagnostic_trace_method=(
                    "controlled_single_change_replay_unwrap_response_units"
                    if response_units_envelope
                    else ""
                ),
                trace_note=(
                    "Primary trace replays the exact public /units envelope. When "
                    "that envelope is unsupported, the diagnostic trace changes "
                    "only response.units -> response list to isolate the next loss."
                ),
                raw_evidence=source_subset(
                    unit,
                    (
                        "id",
                        "name",
                        "unitNumber",
                        "floorplanId",
                        "floorPlanName",
                        "leaseStatus",
                        "internalAvailableDate",
                        "availableDate",
                        "available_date",
                        "vacantDate",
                        "rent",
                    ),
                ),
            )
        )
    status = "PROBED_DATA" if rows else "PROBED_NO_CURRENT_INVENTORY"
    if units_response.status != 200:
        status = "FETCH_FAILED"
    return (
        {
            "status": status,
            "error": "" if units_response.status == 200 else units_response.error,
            "identity_name": observed_name,
            "identity_address": observed_address,
            "identity_match": identity_ok,
            "identity_reason": identity_reason,
            "evidence_urls": [homepage.final_url, details_url, units_url],
            "access_path": "curl_cffi_current_live_public_widget_api",
            "source_items": len(raw_units),
            "source_detail": (
                f"marketing_property_id={rp_property_id};"
                f"details_http={details.status};units_http={units_response.status};"
                f"units_response_shape={'response.units' if response_units_envelope else type(response_payload).__name__}"
            ),
        },
        rows,
    )


def conventional_url(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for anchor in soup.find_all("a", href=True):
        candidate = urljoin(base_url, str(anchor.get("href") or ""))
        if "/conventional/" in candidate:
            return candidate
    return ""


async def hb_fetch(url: str, property_id: str) -> HttpResult:
    from ma_poc.fetch.hyperbrowser_backend import hb_raw_get

    status, body = await hb_raw_get(url, property_id)
    return HttpResult(
        requested_url=url,
        final_url=url,
        status=int(status),
        text=body or "",
        error="" if status == 200 else f"hyperbrowser_http_{status}",
        impersonation="hyperbrowser_clean_residential_no_captcha",
        attempts=1,
    )


async def probe_entrata(
    meta: dict[str, str],
    captured_at: str,
    timeout: float,
    allow_hyperbrowser: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    property_id = str(meta["apartmentid"])
    homepage = fetch_get(str(meta["website"]), timeout)
    route = conventional_url(homepage.text, homepage.final_url)
    if not route:
        return (
            {
                "status": "FETCH_FAILED",
                "error": f"no_conventional_route;marketing_http={homepage.status}",
                "identity_name": "",
                "identity_address": "",
                "identity_match": False,
                "identity_reason": "no_conventional_route",
                "evidence_urls": [homepage.final_url],
                "access_path": "curl_cffi_current_live",
                "source_items": 0,
            },
            [],
        )
    page = fetch_get(route, timeout, impersonations=("chrome120",))
    access_path = "curl_cffi_current_live"
    if (page.status != 200 or "fp-card" not in page.text) and allow_hyperbrowser:
        page = await hb_fetch(route, property_id)
        access_path = "hyperbrowser_clean_residential_render_no_captcha"
    soup = BeautifulSoup(page.text or "", "lxml")
    cards = soup.select(".fp-card")
    observed_name, observed_address = jsonld_identity(page.text or homepage.text)
    identity_ok, identity_reason = identity_verdict(
        str(meta.get("name") or ""),
        str(meta.get("address") or ""),
        observed_name,
        observed_address,
        (page.text or homepage.text),
    )
    adapter_rows = parse_entrata_prospectportal_html(page.text, route)
    adapter_by_plan = adapter_row_queues(
        adapter_rows, "floor_plan_name", "floorplan_name"
    )
    rows: list[dict[str, Any]] = []
    for index, card in enumerate(cards, start=1):
        name_el = card.select_one(".fp-title, .fp-name")
        availability_el = card.select_one(".availability")
        plan_name = name_el.get_text(" ", strip=True) if name_el else ""
        raw_availability = (
            availability_el.get_text(" ", strip=True) if availability_el else ""
        )
        adapter_row = take_adapter_row(adapter_by_plan, plan_name)
        rows.append(
            evidence_row(
                captured_at=captured_at,
                category="ENTRATA_API",
                property_id=property_id,
                configured_name=str(meta.get("name") or ""),
                source_kind="entrata_visible_conventional_floorplan_card",
                evidence_url=route,
                source_row_id=f"card-{index}",
                floor_plan_name=plan_name,
                raw_availability=raw_availability,
                source_field="visible .fp-card .availability",
                adapter_parser="entrata.parse_entrata_prospectportal_html",
                adapter_row=adapter_row,
                trace_note=(
                    "The exact current SSR HTML was parsed by the current Entrata "
                    "fallback parser, then the matched plan row was formatted."
                ),
                raw_evidence={
                    "floor_plan_name": plan_name,
                    "availability_text": raw_availability,
                    "card_text": compact_text(card.get_text(" ", strip=True))[:800],
                },
            )
        )
    status = "PROBED_DATA" if rows else "FETCH_FAILED"
    return (
        {
            "status": status,
            "error": "" if rows else f"conventional_http={page.status};{page.error}",
            "identity_name": observed_name,
            "identity_address": observed_address,
            "identity_match": identity_ok,
            "identity_reason": identity_reason,
            "evidence_urls": [homepage.final_url, route],
            "access_path": access_path,
            "source_items": len(cards),
            "source_detail": (
                "Current visible SSR plan cards; historical July output came from "
                "the Entrata API floorplan tier."
            ),
        },
        rows,
    )


def nested_floorplans(payload: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            floorplans = value.get("Floorplans")
            if isinstance(floorplans, list):
                out.extend(item for item in floorplans if isinstance(item, dict))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in out:
        key = compact_text(item.get("Id") or item.get("MarketingId") or item.get("Name"))
        if key and key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def fetch_workflowstartup(
    site_id: str, origin: str, timeout: float
) -> tuple[HttpResult, Any]:
    url = _onesite_workflowstartup_url(site_id)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": origin or "https://example.com",
        "Referer": (origin.rstrip("/") + "/") if origin else "https://example.com/",
        "User-Agent": _XYZ_USER_AGENT,
        "xyz": _generate_xyz_token(site_id, _XYZ_USER_AGENT),
        "X-AuthToken": "",
        "X-Phased": "",
    }
    response = fetch_get(
        url,
        timeout,
        headers=headers,
        impersonations=tuple(_XYZ_IMPERSONATE_CHAIN),
    )
    payload: Any = {}
    try:
        payload = json.loads(response.text)
    except Exception:
        pass
    return response, payload


def wordpress_946_units(
    captured_at: str, meta: dict[str, str], timeout: float
) -> tuple[list[dict[str, Any]], list[str], str]:
    property_id = str(meta["apartmentid"])
    page_url = "https://www.946mlk.com/floorplans/"
    session = requests.Session(impersonate="chrome120")
    try:
        session.get(page_url, timeout=timeout, allow_redirects=True)
        api_url = "https://www.946mlk.com/wp-admin/admin-ajax.php"
        headers = {
            "Referer": page_url,
            "Origin": "https://www.946mlk.com",
            "X-Requested-With": "XMLHttpRequest",
        }
        listing = session.post(
            api_url,
            data={
                "action": "swifty_floorplan_section_details_with_ajax",
                "nocache": str(int(time.time() * 1000)),
                "pageType": "floorplans",
            },
            headers=headers,
            timeout=timeout,
        )
    except Exception as exc:
        return [], [page_url], f"{type(exc).__name__}: {str(exc)[:160]}"
    soup = BeautifulSoup(listing.text or "", "lxml")
    rows: list[dict[str, Any]] = []
    plans = soup.select(".single-floorplan[data_id]")
    for plan in plans:
        plan_id = compact_text(plan.get("data_id"))
        plan_link = plan.select_one("[data-name]")
        plan_name = compact_text(plan_link.get("data-name")) if plan_link else ""
        plan_text = compact_text(plan.get_text(" ", strip=True))
        if not plan_id or re.search(r"no\s+vacant\s+unit", plan_text, re.IGNORECASE):
            continue
        try:
            unit_response = session.post(
                api_url,
                data={
                    "action": "swifty_load_available_units",
                    "flp_id": plan_id,
                    "nocache": str(int(time.time() * 1000)),
                },
                headers=headers,
                timeout=timeout,
            )
        except Exception:
            continue
        unit_soup = BeautifulSoup(unit_response.text or "", "lxml")
        for unit_row in unit_soup.select("tr.single-flp-unit-row"):
            cells = [cell.get_text(" ", strip=True) for cell in unit_row.select("td")]
            if len(cells) < 4:
                continue
            raw_availability = cells[3]
            rows.append(
                evidence_row(
                    captured_at=captured_at,
                    category="ONESITE_WORKFLOW",
                    property_id=property_id,
                    configured_name=str(meta.get("name") or ""),
                    source_kind="onesite_property_marketing_visible_unit_ajax",
                    evidence_url=page_url,
                    source_row_id=cells[0],
                    floor_plan_name=plan_name,
                    raw_availability=raw_availability,
                    source_field="visible unit-table Availability column",
                    adapter_parser="NO_CURRENT_ADAPTER_FOR_MARKETING_AJAX_ROUTE",
                    adapter_trace_method="exact_native_route_not_selected",
                    adapter_row=None,
                    adapter_route_selected=False,
                    trace_note=(
                        "946 MLK's own visible WordPress AJAX unit table publishes "
                        "this date, but the current OneSite workflow path does not "
                        "select or parse that alternate route."
                    ),
                    raw_evidence={
                        "floorplan_id": plan_id,
                        "floor_plan_name": plan_name,
                        "unit_number": cells[0],
                        "price": cells[1],
                        "floor": cells[2],
                        "availability": raw_availability,
                        "ajax_endpoint": api_url,
                        "plan_card_date": compact_text(plan.get("flp-date")),
                    },
                )
            )
    return rows, [page_url, api_url], "" if rows else f"listing_http={listing.status_code}"


def probe_onesite_workflow(
    meta: dict[str, str], captured_at: str, timeout: float
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    property_id = str(meta["apartmentid"])
    homepage = fetch_get(str(meta["website"]), timeout)
    normalized_html = homepage.text.replace("\\/", "/")
    portals = list(dict.fromkeys(ONESITE_PORTAL_RE.findall(normalized_html)))
    site_ids = list(dict.fromkeys(ONESITE_SITE_ID_RE.findall(normalized_html)))
    portal_result: HttpResult | None = None
    if portals:
        portal_result = fetch_get(
            portals[0],
            timeout,
            impersonations=("chrome116", "edge99", "safari17_0"),
        )
        site_ids.extend(
            value
            for value in ONESITE_SITE_ID_RE.findall(portal_result.text)
            if value not in site_ids
        )
    observed_name, observed_address = jsonld_identity(homepage.text)
    identity_ok, identity_reason = identity_verdict(
        str(meta.get("name") or ""),
        str(meta.get("address") or ""),
        observed_name,
        observed_address,
        homepage.text,
    )
    workflow_response: HttpResult | None = None
    payload: Any = {}
    floorplans: list[dict[str, Any]] = []
    workflow_url = ""
    if site_ids:
        parsed = urlparse(homepage.final_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        workflow_response, payload = fetch_workflowstartup(site_ids[0], origin, timeout)
        workflow_url = workflow_response.requested_url
        floorplans = nested_floorplans(payload)
    adapter_rows = parse_onesite_workflowstartup(payload, workflow_url)
    adapter_by_unit = adapter_row_queues(adapter_rows, "unit_number", "unit_id")
    workflow_rows: list[dict[str, Any]] = []
    for floorplan in floorplans:
        unit_ids = [
            compact_text(value)
            for value in (floorplan.get("UnitIds") or [])
            if compact_text(value)
        ]
        for unit_id in unit_ids:
            adapter_row = take_adapter_row(adapter_by_unit, unit_id)
            workflow_rows.append(
                evidence_row(
                    captured_at=captured_at,
                    category="ONESITE_WORKFLOW",
                    property_id=property_id,
                    configured_name=str(meta.get("name") or ""),
                    source_kind="onesite_workflowstartup_raw_floorplan_unitids",
                    evidence_url=workflow_url,
                    source_row_id=unit_id,
                    floor_plan_name=compact_text(floorplan.get("Name")),
                    raw_availability="",
                    source_field="workflowstartup Floorplans has no date field",
                    adapter_parser="onesite.parse_onesite_workflowstartup",
                    adapter_row=adapter_row,
                    trace_note=(
                        "Exact workflowstartup JSON was replayed through the current "
                        "OneSite parser. The native floorplan object has UnitIds but "
                        "no availability-date field."
                    ),
                    raw_evidence=source_subset(
                        floorplan,
                        (
                            "Id",
                            "MarketingId",
                            "Name",
                            "Bedrooms",
                            "Bathrooms",
                            "AvailableUnits",
                            "UnitIds",
                            "MinPriceRange",
                            "MaxPriceRange",
                        ),
                    ),
                )
            )

    evidence_urls = [homepage.final_url]
    if portal_result:
        evidence_urls.append(portal_result.final_url)
    if workflow_url:
        evidence_urls.append(workflow_url)

    primary_rows = workflow_rows
    error = ""
    source_detail = "workflowstartup publishes UnitIds but no availability-date field"
    if property_id == "283561":
        marketing_rows, marketing_urls, marketing_error = wordpress_946_units(
            captured_at, meta, timeout
        )
        # Keep workflow rows as explicit supplemental evidence of the missing
        # raw field, while the public marketing unit table is the current
        # visible primary source for this property.
        for row in workflow_rows:
            row["primary_property_evidence"] = "false"
        primary_rows = marketing_rows
        evidence_urls.extend(marketing_urls)
        error = marketing_error
        source_detail = (
            "Primary: current property marketing availability table. Supplemental: "
            "workflowstartup currently publishes no date fields."
        )
    all_rows = primary_rows + ([] if primary_rows is workflow_rows else workflow_rows)
    if primary_rows:
        status = "PROBED_DATA"
    elif workflow_response and workflow_response.status == 200:
        status = "PROBED_NO_CURRENT_INVENTORY"
    else:
        status = "FETCH_FAILED"
    return (
        {
            "status": status,
            "error": error
            or (
                ""
                if workflow_response and workflow_response.status == 200
                else "workflowstartup_unavailable"
            ),
            "identity_name": observed_name,
            "identity_address": observed_address,
            "identity_match": identity_ok,
            "identity_reason": identity_reason,
            "evidence_urls": list(dict.fromkeys(evidence_urls)),
            "access_path": "curl_cffi_current_live_workflow_and_visible_marketing",
            "source_items": len(primary_rows),
            "source_detail": (
                f"site_id={site_ids[0] if site_ids else ''};"
                f"workflow_floorplans={len(floorplans)};{source_detail}"
            ),
        },
        all_rows,
    )


def probe_aspensquare(
    meta: dict[str, str], captured_at: str, timeout: float
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    property_id = str(meta["apartmentid"])
    homepage = fetch_get(str(meta["website"]), timeout)
    community_hash = find_knock_community_hash(homepage.text)
    if not community_hash:
        return (
            {
                "status": "FETCH_FAILED",
                "error": f"community_hash_not_found;marketing_http={homepage.status}",
                "identity_name": "",
                "identity_address": "",
                "identity_match": False,
                "identity_reason": "community_hash_not_found",
                "evidence_urls": [homepage.final_url],
                "access_path": "curl_cffi_current_live",
                "source_items": 0,
            },
            [],
        )
    headers = {
        "Origin": "https://doorway.knck.io",
        "Accept": "application/json",
    }
    community_url = (
        "https://doorway-api.knockrentals.com/v1/property/community/"
        f"{community_hash}"
    )
    community_response = fetch_get(community_url, timeout, headers=headers)
    community_body: Any = {}
    try:
        community_body = json.loads(community_response.text)
    except Exception:
        pass
    property_obj = (
        community_body.get("property") if isinstance(community_body, dict) else {}
    ) or {}
    location = ((property_obj.get("data") or {}).get("location") or {})
    observed_name = compact_text(location.get("name"))
    addr = location.get("address") or {}
    observed_address = compact_text(
        " ".join(
            str(addr.get(key) or "")
            for key in ("street", "address", "line1", "city", "state", "zip")
        )
    )
    # Some Knock locations publish the street under a less common key.
    if not address_match(str(meta.get("address") or ""), observed_address):
        observed_address = compact_text(safe_json(addr))
    identity_ok, identity_reason = identity_verdict(
        str(meta.get("name") or ""),
        str(meta.get("address") or ""),
        observed_name,
        observed_address,
        homepage.text,
    )
    numeric_id = compact_text(property_obj.get("id"))
    units_url = (
        f"https://doorway-api.knockrentals.com/v1/property/{numeric_id}/units"
        if numeric_id
        else ""
    )
    units_response = (
        fetch_get(units_url, timeout, headers=headers)
        if units_url
        else HttpResult("", "", 0, "", "no_numeric_property_id", "", 0)
    )
    units_body: Any = {}
    try:
        units_body = json.loads(units_response.text)
    except Exception:
        pass
    units_data = (
        units_body.get("units_data") if isinstance(units_body, dict) else {}
    ) or {}
    raw_units = [
        unit
        for unit in (units_data.get("units") or [])
        if isinstance(unit, dict)
        and not unit.get("hidden")
        and not unit.get("leased")
        and not unit.get("reserved")
    ]
    adapter_rows = parse_knock_units(units_body)
    adapter_by_unit = adapter_row_queues(adapter_rows, "unit_number", "unit_id")
    rows: list[dict[str, Any]] = []
    for unit in raw_units:
        raw_date = compact_text(
            unit.get("availableOn")
            or unit.get("available_on")
            or unit.get("ready_date")
        )
        unit_anchor = compact_text(
            unit.get("name") or unit.get("unit_number") or unit.get("id")
        )
        adapter_row = take_adapter_row(adapter_by_unit, unit_anchor)
        rows.append(
            evidence_row(
                captured_at=captured_at,
                category="ASPENSQUARE_OPERATOR",
                property_id=property_id,
                configured_name=str(meta.get("name") or ""),
                source_kind="aspensquare_public_knock_units_api",
                evidence_url=units_url,
                source_row_id=unit_anchor,
                floor_plan_name=compact_text(unit.get("layoutName")),
                raw_availability=raw_date,
                source_field=(
                    "availableOn"
                    if unit.get("availableOn") not in (None, "")
                    else "available_on/ready_date"
                ),
                adapter_parser="knock.parse_knock_units",
                adapter_row=adapter_row,
                trace_note=(
                    "Exact current Knock units payload was replayed through the "
                    "current adapter parser, then the matched unit was formatted."
                ),
                raw_evidence=source_subset(
                    unit,
                    (
                        "id",
                        "name",
                        "layoutId",
                        "layoutName",
                        "available",
                        "availableRaw",
                        "availableOn",
                        "available_on",
                        "ready_date",
                        "hidden",
                        "leased",
                        "reserved",
                        "occupied",
                        "price",
                        "displayPrice",
                    ),
                ),
            )
        )
    status = "PROBED_DATA" if rows else "PROBED_NO_CURRENT_INVENTORY"
    if units_response.status != 200:
        status = "FETCH_FAILED"
    return (
        {
            "status": status,
            "error": "" if units_response.status == 200 else units_response.error,
            "identity_name": observed_name,
            "identity_address": observed_address,
            "identity_match": identity_ok,
            "identity_reason": identity_reason,
            "evidence_urls": [homepage.final_url, community_url, units_url],
            "access_path": "curl_cffi_current_live_public_knock_api",
            "source_items": len(raw_units),
            "source_detail": (
                f"community_hash={community_hash};numeric_property_id={numeric_id};"
                f"community_http={community_response.status};units_http={units_response.status}"
            ),
        },
        rows,
    )


def squarespace_unit_blocks(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html or "", "lxml")
    out: list[dict[str, str]] = []
    for paragraph in soup.select("div.sqs-html-content p"):
        segments = [
            compact_text(segment)
            for segment in paragraph.get_text("\n", strip=True).split("\n")
            if compact_text(segment)
        ]
        if not segments:
            continue
        unit_match = UNIT_BLOCK_RE.match(segments[0])
        if not unit_match or not any(MONEY_RE.search(segment) for segment in segments):
            continue
        availability = next(
            (segment for segment in segments if re.match(r"Available\b", segment, re.I)),
            "",
        )
        rent = next((segment for segment in segments if MONEY_RE.search(segment)), "")
        plan = segments[1] if len(segments) > 1 else ""
        out.append(
            {
                "unit_number": compact_text(unit_match.group(1)),
                "floor_plan_name": plan,
                "rent": rent,
                "availability": availability,
                "visible_block": " | ".join(segments),
            }
        )
    return out


def probe_squarespace(
    meta: dict[str, str], captured_at: str, timeout: float
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    property_id = str(meta["apartmentid"])
    homepage = fetch_get(str(meta["website"]), timeout)
    soup = BeautifulSoup(homepage.text or "", "lxml")
    availability_url = ""
    for anchor in soup.find_all("a", href=True):
        if "availability" in str(anchor.get("href") or "").lower():
            availability_url = urljoin(homepage.final_url, str(anchor["href"]))
            break
    if not availability_url:
        availability_url = urljoin(homepage.final_url, "/availability")
    page = fetch_get(availability_url, timeout)
    observed_name, observed_address = jsonld_identity(page.text or homepage.text)
    identity_ok, identity_reason = identity_verdict(
        str(meta.get("name") or ""),
        str(meta.get("address") or ""),
        observed_name,
        observed_address,
        (page.text or homepage.text),
    )
    blocks = squarespace_unit_blocks(page.text)
    adapter_rows = parse_squarespace_unit_blocks(page.text, page.final_url)
    adapter_by_unit = adapter_row_queues(adapter_rows, "unit_number", "unit_id")
    rows: list[dict[str, Any]] = []
    for block in blocks:
        adapter_row = take_adapter_row(adapter_by_unit, block["unit_number"])
        rows.append(
            evidence_row(
                captured_at=captured_at,
                category="SQUARESPACE_UNIT_BLOCK",
                property_id=property_id,
                configured_name=str(meta.get("name") or ""),
                source_kind="squarespace_visible_unit_block",
                evidence_url=page.final_url,
                source_row_id=block["unit_number"],
                floor_plan_name=block["floor_plan_name"],
                raw_availability=block["availability"],
                source_field="visible unit block fourth line",
                adapter_parser="_avail_table_recovery.parse_squarespace_unit_blocks",
                adapter_row=adapter_row,
                trace_note=(
                    "Exact current /availability HTML was replayed through the "
                    "current Squarespace unit-block parser, then formatted."
                ),
                raw_evidence=block,
            )
        )
    return (
        {
            "status": "PROBED_DATA" if rows else "FETCH_FAILED",
            "error": "" if rows else f"availability_http={page.status};{page.error}",
            "identity_name": observed_name,
            "identity_address": observed_address,
            "identity_match": identity_ok,
            "identity_reason": identity_reason,
            "evidence_urls": [homepage.final_url, page.final_url],
            "access_path": "curl_cffi_current_live_visible_squarespace_dom",
            "source_items": len(blocks),
            "source_detail": "Current visible per-unit blocks on /availability.",
        },
        rows,
    )


def probe_property_record(
    *,
    meta: dict[str, str],
    category: str,
    result: dict[str, Any],
    rows: list[dict[str, Any]],
    july_row: dict[str, Any],
    captured_at: str,
) -> dict[str, Any]:
    primary_rows = [row for row in rows if row["primary_property_evidence"] == "true"]
    semantic_counts = Counter(row["availability_semantic"] for row in primary_rows)
    pipeline_counts = Counter(row["pipeline_outcome"] for row in primary_rows)
    loss_counts = Counter(row["loss_classification"] for row in primary_rows)
    future_count = semantic_counts["explicit_future"]
    future_adapter_preserved = sum(
        row["explicit_future_preserved_by_adapter"] == "true" for row in primary_rows
    )
    future_formatter_preserved = sum(
        row["explicit_future_preserved_by_formatter"] == "true"
        for row in primary_rows
    )
    alias_miss_count = sum(
        row["missed_future_by_adapter_alias"] == "true" for row in primary_rows
    )
    diagnostic_alias_miss_count = sum(
        row["diagnostic_gap_classification"]
        == "adapter_key_alias_loss_after_response_unwrap"
        for row in primary_rows
    )
    july_capture = int(july_row["july_capture_date_rows"])
    july_rows = int(july_row["july_rows"])
    if future_count and july_capture == july_rows:
        finding = "CURRENT_SOURCE_HAS_EXPLICIT_FUTURE_WHILE_JULY_OUTPUT_WAS_CAPTURE_DATE"
    elif result["status"] == "PROBED_NO_CURRENT_INVENTORY":
        finding = "CURRENT_SOURCE_HAS_NO_CURRENT_INVENTORY"
    elif primary_rows and semantic_counts["source_blank"] == len(primary_rows):
        finding = "CURRENT_SOURCE_PUBLISHES_INVENTORY_WITHOUT_DATE_FIELD"
    elif primary_rows:
        finding = "CURRENT_SOURCE_PROBED_NO_EXPLICIT_FUTURE_IN_SAMPLE"
    else:
        finding = "LIVE_PROBE_FAILED_OR_EMPTY"
    if future_count and future_formatter_preserved == future_count:
        pipeline_finding = "ALL_NATIVE_EXPLICIT_FUTURES_PRESERVED_END_TO_END"
    elif future_count and future_formatter_preserved:
        pipeline_finding = "SOME_NATIVE_EXPLICIT_FUTURES_LOST_IN_PIPELINE"
    elif future_count:
        pipeline_finding = "NATIVE_EXPLICIT_FUTURES_NOT_PRESERVED"
    elif result["status"] == "PROBED_NO_CURRENT_INVENTORY":
        pipeline_finding = "CURRENT_NO_INVENTORY_NO_DATE_CONCLUSION"
    elif primary_rows:
        pipeline_finding = "NO_NATIVE_EXPLICIT_FUTURE_IN_CURRENT_SAMPLE"
    else:
        pipeline_finding = "PIPELINE_NOT_TRACED"
    trace_confidence = (
        "HIGH"
        if result.get("identity_match")
        and result["status"] in {"PROBED_DATA", "PROBED_NO_CURRENT_INVENTORY"}
        and not any(row["formatter_error"] for row in primary_rows)
        else "LOW"
    )
    return {
        "result_type": RESULT_TYPE,
        "capture_timestamp_utc": captured_at,
        "capture_date": LIVE_CAPTURE_DATE.isoformat(),
        "historical_run_date": JULY_CAPTURE_DATE.isoformat(),
        "denominator_scope": "separate_no_rp_oracle_573_rows_78_properties",
        "category": category,
        "property_id": str(meta["apartmentid"]),
        "property_name": str(meta.get("name") or ""),
        "configured_address": str(meta.get("address") or ""),
        "website": str(meta.get("website") or ""),
        "probe_status": result["status"],
        "access_path": result.get("access_path", ""),
        "identity_name": result.get("identity_name", ""),
        "identity_address": result.get("identity_address", ""),
        "identity_match": text_bool(bool(result.get("identity_match"))),
        "identity_reason": result.get("identity_reason", ""),
        "evidence_urls": ";".join(result.get("evidence_urls") or []),
        "source_items": int(result.get("source_items") or 0),
        "primary_evidence_rows": len(primary_rows),
        "supplemental_evidence_rows": len(rows) - len(primary_rows),
        "current_explicit_future_rows": semantic_counts["explicit_future"],
        "current_available_now_rows": semantic_counts["available_now"],
        "current_explicit_capture_date_rows": semantic_counts["explicit_capture_date"],
        "current_historical_embedded_rows": semantic_counts["historical_embedded"],
        "current_sentinel_future_rows": semantic_counts["sentinel_future"],
        "current_historical_sentinel_rows": semantic_counts["historical_sentinel"],
        "current_available_state_no_date_rows": semantic_counts[
            "available_state_no_date"
        ],
        "current_source_blank_rows": semantic_counts["source_blank"],
        "current_unavailable_rows": semantic_counts["unavailable"],
        "current_unparsed_rows": semantic_counts["unparsed_availability_text"],
        "current_future_missed_by_adapter_alias_rows": alias_miss_count,
        "current_explicit_future_preserved_by_adapter_rows": future_adapter_preserved,
        "current_explicit_future_preserved_by_formatter_rows": future_formatter_preserved,
        "current_explicit_future_lost_by_adapter_rows": max(
            0, future_count - future_adapter_preserved
        ),
        "current_explicit_future_lost_by_formatter_rows": max(
            0, future_adapter_preserved - future_formatter_preserved
        ),
        "current_formatter_capture_date_default_rows": sum(
            row["formatter_capture_date_default"] == "true" for row in primary_rows
        ),
        "current_capture_defaults_from_source_blank_rows": sum(
            row["availability_semantic"] == "source_blank"
            and row["formatter_capture_date_default"] == "true"
            for row in primary_rows
        ),
        "current_capture_defaults_from_available_now_rows": sum(
            row["availability_semantic"] == "available_now"
            and row["formatter_available_date"] == LIVE_CAPTURE_DATE.isoformat()
            for row in primary_rows
        ),
        "current_adapter_route_selection_loss_rows": loss_counts[
            "adapter_route_selection_loss"
        ],
        "current_adapter_key_alias_loss_rows": loss_counts["adapter_key_alias_loss"],
        "current_adapter_response_shape_loss_rows": loss_counts[
            "adapter_response_shape_loss"
        ],
        "current_diagnostic_alias_loss_after_unwrap_rows": (
            diagnostic_alias_miss_count
        ),
        "current_diagnostic_capture_defaults_after_unwrap_rows": sum(
            row["diagnostic_gap_classification"]
            == "adapter_key_alias_loss_after_response_unwrap"
            and row["diagnostic_formatter_available_date"]
            == LIVE_CAPTURE_DATE.isoformat()
            for row in primary_rows
        ),
        "current_normalization_loss_rows": loss_counts["normalization_loss"]
        + loss_counts["formatter_date_loss_or_change"],
        "pipeline_outcomes_json": safe_json(dict(sorted(pipeline_counts.items()))),
        "loss_classifications_json": safe_json(dict(sorted(loss_counts.items()))),
        "pipeline_finding": pipeline_finding,
        "trace_confidence": trace_confidence,
        "july_rows": july_rows,
        "july_capture_date_rows": july_capture,
        "july_blank_date_rows": int(july_row["july_blank_date_rows"]),
        "finding": finding,
        "source_detail": result.get("source_detail", ""),
        "error": result.get("error", ""),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty ledger: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def tier_summary(
    july_ledger: list[dict[str, Any]], property_audit: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    categories = sorted({row["category"] for row in july_ledger})
    for category in categories:
        july = [row for row in july_ledger if row["category"] == category]
        live = [row for row in property_audit if row["category"] == category]
        out.append(
            {
                "result_type": RESULT_TYPE,
                "category": category,
                "historical_properties": len(july),
                "historical_rows": sum(int(row["july_rows"]) for row in july),
                "historical_capture_date_rows": sum(
                    int(row["july_capture_date_rows"]) for row in july
                ),
                "historical_blank_date_rows": sum(
                    int(row["july_blank_date_rows"]) for row in july
                ),
                "live_sample_requested": len(PROBE_SELECTION[category]),
                "live_sample_probed_data": sum(
                    row["probe_status"] == "PROBED_DATA" for row in live
                ),
                "live_sample_no_inventory": sum(
                    row["probe_status"] == "PROBED_NO_CURRENT_INVENTORY"
                    for row in live
                ),
                "live_sample_failed": sum(
                    row["probe_status"] == "FETCH_FAILED" for row in live
                ),
                "live_identity_match_properties": sum(
                    row["identity_match"] == "true" for row in live
                ),
                "live_properties_with_explicit_future": sum(
                    int(row["current_explicit_future_rows"]) > 0 for row in live
                ),
                "live_source_sentinel_rows": sum(
                    int(row["current_sentinel_future_rows"])
                    + int(row["current_historical_sentinel_rows"])
                    for row in live
                ),
                "live_explicit_future_rows": sum(
                    int(row["current_explicit_future_rows"]) for row in live
                ),
                "live_explicit_future_preserved_by_adapter_rows": sum(
                    int(row["current_explicit_future_preserved_by_adapter_rows"])
                    for row in live
                ),
                "live_explicit_future_preserved_by_formatter_rows": sum(
                    int(row["current_explicit_future_preserved_by_formatter_rows"])
                    for row in live
                ),
                "live_explicit_future_lost_by_adapter_rows": sum(
                    int(row["current_explicit_future_lost_by_adapter_rows"])
                    for row in live
                ),
                "live_explicit_future_lost_by_formatter_rows": sum(
                    int(row["current_explicit_future_lost_by_formatter_rows"])
                    for row in live
                ),
                "live_future_missed_by_adapter_alias_rows": sum(
                    int(row["current_future_missed_by_adapter_alias_rows"])
                    for row in live
                ),
                "live_formatter_capture_date_default_rows": sum(
                    int(row["current_formatter_capture_date_default_rows"])
                    for row in live
                ),
                "live_capture_defaults_from_source_blank_rows": sum(
                    int(row["current_capture_defaults_from_source_blank_rows"])
                    for row in live
                ),
                "live_capture_defaults_from_available_now_rows": sum(
                    int(row["current_capture_defaults_from_available_now_rows"])
                    for row in live
                ),
                "live_adapter_route_selection_loss_rows": sum(
                    int(row["current_adapter_route_selection_loss_rows"])
                    for row in live
                ),
                "live_adapter_key_alias_loss_rows": sum(
                    int(row["current_adapter_key_alias_loss_rows"])
                    for row in live
                ),
                "live_adapter_response_shape_loss_rows": sum(
                    int(row["current_adapter_response_shape_loss_rows"])
                    for row in live
                ),
                "live_diagnostic_alias_loss_after_unwrap_rows": sum(
                    int(row["current_diagnostic_alias_loss_after_unwrap_rows"])
                    for row in live
                ),
                "live_diagnostic_capture_defaults_after_unwrap_rows": sum(
                    int(
                        row[
                            "current_diagnostic_capture_defaults_after_unwrap_rows"
                        ]
                    )
                    for row in live
                ),
                "live_normalization_loss_rows": sum(
                    int(row["current_normalization_loss_rows"])
                    for row in live
                ),
                "high_confidence_properties": sum(
                    row["trace_confidence"] == "HIGH" for row in live
                ),
                "sample_limit": (
                    "full category; fewer than 3 historical properties"
                    if len(july) < 3
                    else "at least 3 representative properties"
                ),
            }
        )
    return out


def five_family_summary(
    july_ledger: list[dict[str, Any]], property_audit: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for family, categories in FIVE_FAMILY_CATEGORIES.items():
        july = [row for row in july_ledger if row["category"] in categories]
        live = [row for row in property_audit if row["category"] in categories]
        requested = sum(len(PROBE_SELECTION[category]) for category in categories)
        future_rows = sum(int(row["current_explicit_future_rows"]) for row in live)
        preserved_rows = sum(
            int(row["current_explicit_future_preserved_by_formatter_rows"])
            for row in live
        )
        out.append(
            {
                "result_type": RESULT_TYPE,
                "family": family,
                "included_subcategories": ";".join(categories),
                "historical_properties": len(july),
                "historical_rows": sum(int(row["july_rows"]) for row in july),
                "historical_capture_date_rows": sum(
                    int(row["july_capture_date_rows"]) for row in july
                ),
                "historical_blank_date_rows": sum(
                    int(row["july_blank_date_rows"]) for row in july
                ),
                "live_sample_requested": requested,
                "live_sample_with_data": sum(
                    row["probe_status"] == "PROBED_DATA" for row in live
                ),
                "live_sample_current_no_inventory": sum(
                    row["probe_status"] == "PROBED_NO_CURRENT_INVENTORY"
                    for row in live
                ),
                "live_sample_failed": sum(
                    row["probe_status"] == "FETCH_FAILED" for row in live
                ),
                "exact_identity_matches": sum(
                    row["identity_match"] == "true" for row in live
                ),
                "high_confidence_properties": sum(
                    row["trace_confidence"] == "HIGH" for row in live
                ),
                "properties_with_native_explicit_future": sum(
                    int(row["current_explicit_future_rows"]) > 0 for row in live
                ),
                "native_source_sentinel_rows": sum(
                    int(row["current_sentinel_future_rows"])
                    + int(row["current_historical_sentinel_rows"])
                    for row in live
                ),
                "native_explicit_future_rows": future_rows,
                "explicit_future_preserved_by_adapter_rows": sum(
                    int(row["current_explicit_future_preserved_by_adapter_rows"])
                    for row in live
                ),
                "explicit_future_preserved_by_formatter_rows": preserved_rows,
                "explicit_future_missed_rows": max(0, future_rows - preserved_rows),
                "adapter_key_alias_loss_rows": sum(
                    int(row["current_adapter_key_alias_loss_rows"])
                    for row in live
                ),
                "adapter_response_shape_loss_rows": sum(
                    int(row["current_adapter_response_shape_loss_rows"])
                    for row in live
                ),
                "diagnostic_alias_loss_after_response_unwrap_rows": sum(
                    int(row["current_diagnostic_alias_loss_after_unwrap_rows"])
                    for row in live
                ),
                "diagnostic_capture_defaults_after_response_unwrap_rows": sum(
                    int(
                        row[
                            "current_diagnostic_capture_defaults_after_unwrap_rows"
                        ]
                    )
                    for row in live
                ),
                "adapter_route_selection_loss_rows": sum(
                    int(row["current_adapter_route_selection_loss_rows"])
                    for row in live
                ),
                "normalization_loss_rows": sum(
                    int(row["current_normalization_loss_rows"])
                    for row in live
                ),
                "capture_date_defaults": sum(
                    int(row["current_formatter_capture_date_default_rows"])
                    for row in live
                ),
                "capture_defaults_from_source_no_date": sum(
                    int(row["current_capture_defaults_from_source_blank_rows"])
                    for row in live
                ),
                "capture_defaults_from_visible_available_now": sum(
                    int(row["current_capture_defaults_from_available_now_rows"])
                    for row in live
                ),
                "confidence": (
                    "HIGH"
                    if live
                    and all(row["trace_confidence"] == "HIGH" for row in live)
                    else "MIXED"
                ),
                "sample_limit": (
                    "complete family population; fewer than 3 properties exist"
                    if len(july) < 3
                    else "at least 3 exact properties"
                ),
            }
        )
    return out


async def main() -> None:
    args = parse_args()
    captured_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    unit_rows = load_csv(args.units)
    properties = load_csv(args.properties)
    metadata = {str(row["apartmentid"]): row for row in properties}
    july_ledger = build_july_property_ledger(unit_rows, metadata)
    july_total_rows = sum(int(row["july_rows"]) for row in july_ledger)
    if not args.allow_cohort_drift and (
        len(july_ledger) != EXPECTED_PROPERTIES or july_total_rows != EXPECTED_ROWS
    ):
        raise SystemExit(
            f"cohort drift: expected {EXPECTED_ROWS}/{EXPECTED_PROPERTIES}, "
            f"observed {july_total_rows}/{len(july_ledger)}"
        )
    july_by_id = {str(row["property_id"]): row for row in july_ledger}

    evidence: list[dict[str, Any]] = []
    property_audit: list[dict[str, Any]] = []
    for category, property_ids in PROBE_SELECTION.items():
        for property_id in property_ids:
            meta = metadata[property_id]
            if category in {"REALPAGE_OLL", "ONESITE_API"}:
                result, rows = probe_realpage_widget(
                    meta, category, captured_at, args.timeout
                )
            elif category == "ENTRATA_API":
                result, rows = await probe_entrata(
                    meta,
                    captured_at,
                    args.timeout,
                    args.allow_hyperbrowser,
                )
            elif category == "ONESITE_WORKFLOW":
                result, rows = probe_onesite_workflow(meta, captured_at, args.timeout)
            elif category == "ASPENSQUARE_OPERATOR":
                result, rows = probe_aspensquare(meta, captured_at, args.timeout)
            elif category == "SQUARESPACE_UNIT_BLOCK":
                result, rows = probe_squarespace(meta, captured_at, args.timeout)
            else:
                raise AssertionError(category)
            evidence.extend(rows)
            property_audit.append(
                probe_property_record(
                    meta=meta,
                    category=category,
                    result=result,
                    rows=rows,
                    july_row=july_by_id[property_id],
                    captured_at=captured_at,
                )
            )

    tier_rows = tier_summary(july_ledger, property_audit)
    family_rows = five_family_summary(july_ledger, property_audit)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    july_path = output_dir / "july31_separate_denominator_property_ledger.csv"
    property_path = output_dir / "current_live_property_audit.csv"
    evidence_path = output_dir / "current_live_unit_evidence.csv"
    tier_path = output_dir / "tier_summary.csv"
    family_path = output_dir / "five_family_pipeline_summary.csv"
    write_csv(july_path, july_ledger)
    write_csv(property_path, property_audit)
    write_csv(evidence_path, evidence)
    write_csv(tier_path, tier_rows)
    write_csv(family_path, family_rows)

    primary_evidence = [
        row for row in evidence if row["primary_property_evidence"] == "true"
    ]
    summary = {
        "result_type": RESULT_TYPE,
        "capture_timestamp_utc": captured_at,
        "capture_date": LIVE_CAPTURE_DATE.isoformat(),
        "historical_run_date": JULY_CAPTURE_DATE.isoformat(),
        "scope": {
            "denominator": "separate_no_rp_oracle_cohort",
            "historical_rows": july_total_rows,
            "historical_properties": len(july_ledger),
            "historical_capture_date_rows": sum(
                int(row["july_capture_date_rows"]) for row in july_ledger
            ),
            "historical_blank_date_rows": sum(
                int(row["july_blank_date_rows"]) for row in july_ledger
            ),
            "rp_matched_cohort_included": False,
            "securecafe_exact_cohort_included": False,
        },
        "live_probe": {
            "properties_requested": sum(len(values) for values in PROBE_SELECTION.values()),
            "properties_with_data": sum(
                row["probe_status"] == "PROBED_DATA" for row in property_audit
            ),
            "properties_no_current_inventory": sum(
                row["probe_status"] == "PROBED_NO_CURRENT_INVENTORY"
                for row in property_audit
            ),
            "properties_failed": sum(
                row["probe_status"] == "FETCH_FAILED" for row in property_audit
            ),
            "properties_identity_matched": sum(
                row["identity_match"] == "true" for row in property_audit
            ),
            "primary_evidence_rows": len(primary_evidence),
            "supplemental_evidence_rows": len(evidence) - len(primary_evidence),
            "semantic_counts": dict(
                sorted(Counter(row["availability_semantic"] for row in primary_evidence).items())
            ),
            "properties_with_explicit_future": sum(
                int(row["current_explicit_future_rows"]) > 0
                for row in property_audit
            ),
            "explicit_future_rows": sum(
                row["availability_semantic"] == "explicit_future"
                for row in primary_evidence
            ),
            "future_rows_missed_by_realpage_adapter_alias": sum(
                row["missed_future_by_adapter_alias"] == "true"
                for row in primary_evidence
            ),
            "future_rows_dropped_by_realpage_response_shape": sum(
                row["availability_semantic"] == "explicit_future"
                and row["loss_classification"] == "adapter_response_shape_loss"
                for row in primary_evidence
            ),
            "future_rows_with_diagnostic_alias_loss_after_unwrap": sum(
                row["diagnostic_gap_classification"]
                == "adapter_key_alias_loss_after_response_unwrap"
                for row in primary_evidence
            ),
            "future_rows_diagnostic_capture_default_after_unwrap": sum(
                row["diagnostic_gap_classification"]
                == "adapter_key_alias_loss_after_response_unwrap"
                and row["diagnostic_formatter_available_date"]
                == LIVE_CAPTURE_DATE.isoformat()
                for row in primary_evidence
            ),
            "explicit_future_rows_preserved_by_adapter": sum(
                row["explicit_future_preserved_by_adapter"] == "true"
                for row in primary_evidence
            ),
            "explicit_future_rows_preserved_by_formatter": sum(
                row["explicit_future_preserved_by_formatter"] == "true"
                for row in primary_evidence
            ),
            "explicit_future_rows_missed_end_to_end": sum(
                row["availability_semantic"] == "explicit_future"
                and row["explicit_future_preserved_by_formatter"] != "true"
                for row in primary_evidence
            ),
            "formatter_capture_date_default_rows": sum(
                row["formatter_capture_date_default"] == "true"
                for row in primary_evidence
            ),
            "pipeline_outcome_counts": dict(
                sorted(Counter(row["pipeline_outcome"] for row in primary_evidence).items())
            ),
            "loss_classification_counts": dict(
                sorted(
                    Counter(
                        row["loss_classification"] for row in primary_evidence
                    ).items()
                )
            ),
        },
        "tier_summary": tier_rows,
        "five_family_pipeline_summary": family_rows,
        "repeatable_gaps": {
            "realpage_response_units_envelope": {
                "demonstrated": sum(
                    int(row["current_adapter_response_shape_loss_rows"]) > 0
                    for row in property_audit
                    if row["category"] == "REALPAGE_OLL"
                )
                >= 3,
                "properties": [
                    row["property_id"]
                    for row in property_audit
                    if row["category"] == "REALPAGE_OLL"
                    and int(row["current_adapter_response_shape_loss_rows"]) > 0
                ],
                "note": (
                    "The live public /units endpoint returns response.units, while "
                    "_api_parser.realpage_units_from_body currently accepts a list "
                    "directly under response. Exact current-parser replay emits no "
                    "rows for this envelope."
                ),
            },
            "realpage_internal_available_date_alias_after_unwrap": {
                "demonstrated": sum(
                    int(row["current_diagnostic_alias_loss_after_unwrap_rows"]) > 0
                    for row in property_audit
                    if row["category"] == "REALPAGE_OLL"
                )
                >= 3,
                "properties": [
                    row["property_id"]
                    for row in property_audit
                    if row["category"] == "REALPAGE_OLL"
                    and int(row["current_diagnostic_alias_loss_after_unwrap_rows"]) > 0
                ],
                "note": (
                    "Controlled one-change replay unwraps response.units without "
                    "changing any native row. The parser then emits units but drops "
                    "internalAvailableDate because it reads only availableDate / "
                    "available_date; the formatter consequently defaults to capture "
                    "date. This is diagnostic evidence, not the primary adapter path."
                ),
            },
            "other_source_families": (
                "Every native row is traced through the current family parser and "
                "directly through schema_v2._format_v2_unit. Alternate visible "
                "routes with no current adapter are explicitly labelled as route "
                "selection gaps. No adapter edit was made in this lane."
            ),
        },
        "guardrails": {
            "adapter_edits": False,
            "production_edits": False,
            "paid_canary": False,
            "llm_enabled": False,
            "captcha_solving": False,
            "hyperbrowser_allowed": bool(args.allow_hyperbrowser),
            "hyperbrowser_sessions": sum(
                str(row["access_path"]).startswith("hyperbrowser_")
                for row in property_audit
            ),
            "hyperbrowser_scope": (
                "Entrata conventional page fallback only; clean residential render; "
                "repository solveCaptchas hard-disabled"
            ),
        },
        "inputs": {
            "units": str(args.units.resolve()),
            "units_sha256": sha256_file(args.units),
            "properties": str(args.properties.resolve()),
            "properties_sha256": sha256_file(args.properties),
            "git_branch": git_value("branch", "--show-current"),
            "git_head": git_value("rev-parse", "HEAD"),
            "git_worktree_dirty": bool(git_value("status", "--short")),
            "trace_code_snapshot": (
                "current_worktree_including_uncommitted_parent_availability_changes"
            ),
            "traced_code_sha256": {
                "schema_v2": sha256_file(REPO_ROOT / "ma_poc/core/schema_v2.py"),
                "api_parser": sha256_file(
                    REPO_ROOT / "ma_poc/pms/adapters/_api_parser.py"
                ),
                "entrata": sha256_file(REPO_ROOT / "ma_poc/pms/adapters/entrata.py"),
                "onesite": sha256_file(REPO_ROOT / "ma_poc/pms/adapters/onesite.py"),
                "knock": sha256_file(REPO_ROOT / "ma_poc/pms/adapters/knock.py"),
                "availability_table_recovery": sha256_file(
                    REPO_ROOT / "ma_poc/pms/adapters/_avail_table_recovery.py"
                ),
            },
        },
        "artifacts": {
            "july_property_ledger": july_path.name,
            "current_property_audit": property_path.name,
            "current_unit_evidence": evidence_path.name,
            "tier_summary": tier_path.name,
            "five_family_pipeline_summary": family_path.name,
        },
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
