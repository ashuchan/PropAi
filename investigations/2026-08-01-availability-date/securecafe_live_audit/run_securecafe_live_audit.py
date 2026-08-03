#!/usr/bin/env python3
"""Reproduce the current-live SecureCafe availability-date audit.

This investigation is deliberately local and read-only with respect to the
public sites it probes.  It is not a GCP canary and does not use CAPTCHA
solving, a paid browser, or a web unlocker.  Network requests use curl_cffi's
Chrome TLS impersonation, matching the direct public-fetch path used by the
RentCafe adapter.

The historical cohort is the exact July-31 matched-native-unit slice where:

* extraction_tier == TIER_1_API_RENTCAFE_SECURECAFE
* comparison == rp_future_to_capture_date

The expected cohort is 567 units across 65 properties.  For each property the
script discovers every SecureCafe base published by its current marketing
page, fetches each public ``availableunits.aspx`` page, runs the repository's
patched parser, and chooses a portal by maximum overlap with that property's
target unit IDs.  This prevents a portfolio's first sibling-property link from
being mistaken for the target property.

Outputs are CSV/JSON only and are written beside this script.  No fetched HTML
is persisted.
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import re
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, unquote_plus, urljoin, urlparse

import pandas as pd
from curl_cffi import requests


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ma_poc.core.schema_v2 import _format_v2_unit  # noqa: E402
from ma_poc.pms.adapters.rentcafe import (  # noqa: E402
    parse_securecafe_availableunits,
)


EXPECTED_UNITS = 567
EXPECTED_PROPERTIES = 65
TARGET_TIER = "TIER_1_API_RENTCAFE_SECURECAFE"
TARGET_COMPARISON = "rp_future_to_capture_date"
RESULT_TYPE = "local_current_live_audit_not_canary"
MANUAL_PROPERTY_IDS = ("296175", "265098", "268338")
GENERIC_AVAILABLE_RE = re.compile(
    r"(?:available(?:\s+now)?|now|immediate(?:ly)?)", re.IGNORECASE
)

SECURECAFE_URL_RE = re.compile(
    r"(?:https?:)?//"
    r"(?P<sub>[a-z0-9][a-z0-9-]*)\."
    r"(?P<domain>securecafe(?:net)?)\.com/"
    r"(?:onlineleasing|residentservices)/"
    r"(?P<slug>[a-z0-9][a-z0-9-]*)",
    re.IGNORECASE,
)
HREF_RE = re.compile(
    r"\bhref\s*=\s*(?:\"(?P<double>[^\"]+)\"|'(?P<single>[^']+)')",
    re.IGNORECASE,
)
LEASING_LINK_RE = re.compile(
    r"(?:availability|available|floor[-_ ]?plans?|apartments?|apply|lease|rent)",
    re.IGNORECASE,
)
ROW_RE = re.compile(
    r"<tr[^>]*\bclass\s*=\s*['\"][^'\"]*\bAvailUnitRow\b[^'\"]*['\"][^>]*>"
    r".*?</tr>",
    re.IGNORECASE | re.DOTALL,
)
ROW_APARTMENT_RE = re.compile(
    r"data-label\s*=\s*['\"]?Apartment['\"]?[^>]*>\s*#?\s*([A-Za-z0-9-]+)",
    re.IGNORECASE,
)
ROW_MOVE_IN_RE = re.compile(
    r"(?:[?&]|&amp;)MoveInDate=([^&'\"<>\s]+)", re.IGNORECASE
)
ROW_VISIBLE_DATE_RE = re.compile(
    r"data-label\s*=\s*['\"]?Date Available['\"]?[^>]*>(.*?)</td>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    final_url: str
    status: int
    text: str
    error: str
    elapsed_ms: int
    challenge_shell: bool
    attempts: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path(
            "/private/tmp/propai-availability-fix-projection-0731/"
            "projection_unit_ledger.csv"
        ),
        help="July-31 exact matched-unit projection ledger.",
    )
    parser.add_argument(
        "--properties",
        type=Path,
        default=REPO_ROOT / "properties.csv",
        help="Property configuration CSV containing apartmentid and website.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=SCRIPT_PATH.parent,
        help="Destination for CSV/JSON artifacts.",
    )
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--capture-time",
        help="Optional ISO timestamp used by the canonical formatter.",
    )
    parser.add_argument(
        "--allow-cohort-drift",
        action="store_true",
        help="Permit an input other than the expected 567 units / 65 properties.",
    )
    return parser.parse_args()


def utc_capture_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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


def bool_text(value: bool | None) -> str:
    if value is None:
        return ""
    return "true" if value else "false"


def normalize_unit_id(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"^(?:unit|apt|apartment)\s*#?\s*", "", text, flags=re.I)
    text = text.lstrip("#").strip().upper()
    return re.sub(r"\s+", "", text)


def normalize_discovery_text(value: str) -> str:
    text = value or ""
    for _ in range(2):
        text = html_lib.unescape(text)
    replacements = {
        r"\/": "/",
        r"\u002F": "/",
        r"\u002f": "/",
        r"\x2F": "/",
        r"\x2f": "/",
        r"\u003A": ":",
        r"\u003a": ":",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def securecafe_bases(value: str) -> list[str]:
    normalized = normalize_discovery_text(value)
    seen: set[str] = set()
    bases: list[str] = []
    for match in SECURECAFE_URL_RE.finditer(normalized):
        base = (
            f"https://{match.group('sub').lower()}.securecafe.com/onlineleasing/"
            f"{match.group('slug').lower()}"
        )
        if base not in seen:
            seen.add(base)
            bases.append(base)
    # Some CMSes percent-encode an entire SecureCafe URL in a redirect query.
    decoded = unquote(normalized)
    if decoded != normalized:
        for match in SECURECAFE_URL_RE.finditer(decoded):
            base = (
                f"https://{match.group('sub').lower()}.securecafe.com/onlineleasing/"
                f"{match.group('slug').lower()}"
            )
            if base not in seen:
                seen.add(base)
                bases.append(base)
    return bases


def challenge_shell(text: str) -> bool:
    sample = (text or "")[:120_000].lower()
    markers = (
        "cf-chl-",
        "just a moment...",
        "challenges.cloudflare.com",
        "enable javascript and cookies to continue",
    )
    return any(marker in sample for marker in markers)


def fetch_public(url: str, timeout: float) -> FetchResult:
    started = time.monotonic()
    best: FetchResult | None = None
    for attempt, impersonation in enumerate(("chrome120", "chrome124"), start=1):
        try:
            response = requests.get(
                url,
                impersonate=impersonation,
                timeout=timeout,
                allow_redirects=True,
            )
            text = response.text or ""
            result = FetchResult(
                requested_url=url,
                final_url=str(response.url),
                status=int(response.status_code),
                text=text,
                error="",
                elapsed_ms=round((time.monotonic() - started) * 1000),
                challenge_shell=challenge_shell(text),
                attempts=attempt,
            )
        except Exception as exc:
            result = FetchResult(
                requested_url=url,
                final_url="",
                status=0,
                text="",
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=round((time.monotonic() - started) * 1000),
                challenge_shell=False,
                attempts=attempt,
            )
        if result.status == 200 and result.text and not result.challenge_shell:
            return result
        if best is None or (result.status == 200 and best.status != 200):
            best = result
    assert best is not None
    return best


def leasing_links(page_url: str, text: str, limit: int = 6) -> list[str]:
    parsed_page = urlparse(page_url)
    origin_host = parsed_page.netloc.lower().split(":", 1)[0]
    seen: set[str] = set()
    links: list[str] = []
    for match in HREF_RE.finditer(normalize_discovery_text(text)):
        raw = html_lib.unescape(match.group("double") or match.group("single") or "")
        if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(page_url, raw)
        parsed = urlparse(absolute)
        host = parsed.netloc.lower().split(":", 1)[0]
        if host != origin_host or not LEASING_LINK_RE.search(parsed.path):
            continue
        clean = parsed._replace(fragment="").geturl()
        if clean not in seen and clean != page_url:
            seen.add(clean)
            links.append(clean)
        if len(links) >= limit:
            break
    return links


def discover_property(
    property_row: dict[str, str], timeout: float
) -> dict[str, Any]:
    website = property_row["website"]
    homepage = fetch_public(website, timeout)
    pages = [homepage]
    bases: list[str] = []
    sources: dict[str, list[str]] = {}

    def add_from_page(page: FetchResult) -> None:
        found = securecafe_bases(page.final_url) + securecafe_bases(page.text)
        for base in found:
            if base not in bases:
                bases.append(base)
            sources.setdefault(base, [])
            source_url = page.final_url or page.requested_url
            if source_url not in sources[base]:
                sources[base].append(source_url)

    add_from_page(homepage)
    # The exact cohort normally publishes the portal on its homepage.  Follow
    # a bounded set of same-origin leasing links only when it does not.
    if not bases and homepage.status == 200 and homepage.text:
        for link in leasing_links(homepage.final_url or website, homepage.text):
            page = fetch_public(link, timeout)
            pages.append(page)
            add_from_page(page)
            if bases:
                break

    return {
        "property_id": property_row["property_id"],
        "property_name": property_row["property_name"],
        "website": website,
        "marketing_status": homepage.status,
        "marketing_final_url": homepage.final_url,
        "marketing_error": homepage.error,
        "marketing_challenge_shell": homepage.challenge_shell,
        "marketing_pages_fetched": len(pages),
        "marketing_attempts": homepage.attempts,
        "candidate_bases": bases,
        "candidate_sources": sources,
    }


def portal_url(base: str) -> str:
    return f"{base.rstrip('/')}/availableunits.aspx"


def fetch_portal(base: str, timeout: float) -> dict[str, Any]:
    url = portal_url(base)
    fetched = fetch_public(url, timeout)
    units = (
        parse_securecafe_availableunits(fetched.text, url)
        if fetched.status == 200 and not fetched.challenge_shell
        else []
    )
    by_id: dict[str, list[dict[str, Any]]] = {}
    for unit in units:
        key = normalize_unit_id(unit.get("unit_number") or unit.get("unit_id"))
        if key:
            by_id.setdefault(key, []).append(unit)
    return {
        "base": base,
        "url": url,
        "fetch": fetched,
        "units": units,
        "by_id": by_id,
    }


def choose_portal(
    discovery: dict[str, Any], target_ids: set[str], portals: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
    scored: list[dict[str, Any]] = []
    for position, base in enumerate(discovery["candidate_bases"]):
        portal = portals[base]
        overlap = sorted(target_ids & set(portal["by_id"]))
        fetched: FetchResult = portal["fetch"]
        scored.append(
            {
                "position": position,
                "base": base,
                "url": portal["url"],
                "status": fetched.status,
                "final_url": fetched.final_url,
                "error": fetched.error,
                "challenge_shell": fetched.challenge_shell,
                "parsed_units": len(portal["units"]),
                "target_overlap_count": len(overlap),
                "target_overlap_units": overlap,
            }
        )
    ranked = sorted(
        scored,
        key=lambda item: (
            -item["target_overlap_count"],
            -item["parsed_units"],
            item["position"],
        ),
    )
    if not ranked:
        return None, "no_candidate_portal", scored
    top_overlap = ranked[0]["target_overlap_count"]
    if top_overlap <= 0:
        if all(item["status"] != 200 for item in ranked):
            return None, "all_candidate_fetches_failed", scored
        if all(item["parsed_units"] == 0 for item in ranked):
            return None, "all_candidate_parsers_empty", scored
        return None, "no_target_unit_overlap", scored

    tied = [item for item in ranked if item["target_overlap_count"] == top_overlap]
    selected_score = tied[0]
    reason = "unique_max_target_unit_overlap"
    if len(tied) > 1:
        inventories = [set(portals[item["base"]]["by_id"]) for item in tied]
        overlaps = [set(item["target_overlap_units"]) for item in tied]
        if all(inv == inventories[0] for inv in inventories[1:]) and all(
            overlap == overlaps[0] for overlap in overlaps[1:]
        ):
            reason = "target_overlap_tie_identical_inventory_first_published"
        else:
            return None, "ambiguous_max_target_unit_overlap", scored
    return portals[selected_score["base"]], reason, scored


def parse_iso_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def compare_dates(current: str, rp_date: str, capture_date: str) -> tuple[str, int | None, bool | None]:
    current_d = parse_iso_date(current)
    rp_d = parse_iso_date(rp_date)
    capture_d = parse_iso_date(capture_date)
    if current_d is None:
        return "current_date_missing", None, None
    if rp_d is None:
        return "rp_date_missing", None, None
    delta = (current_d - rp_d).days
    if delta == 0:
        label = "current_exact"
    elif delta == 1:
        label = "current_plus_one_day"
    elif delta == -1:
        label = "current_minus_one_day"
    elif capture_d is not None and current_d > capture_d:
        label = "current_other_future"
    else:
        label = "current_nonfuture"
    return label, delta, abs(delta) <= 1


def independent_row_evidence(portal_html: str, target_unit_id: str) -> dict[str, str]:
    target = normalize_unit_id(target_unit_id)
    for row in ROW_RE.findall(portal_html):
        apt = ROW_APARTMENT_RE.search(row)
        if not apt or normalize_unit_id(apt.group(1)) != target:
            continue
        visible_match = ROW_VISIBLE_DATE_RE.search(row)
        visible = ""
        if visible_match:
            visible = re.sub(r"<[^>]+>", " ", visible_match.group(1))
            visible = re.sub(r"\s+", " ", visible).strip()
        move_match = ROW_MOVE_IN_RE.search(row)
        move_in = ""
        if move_match:
            move_in = unquote_plus(html_lib.unescape(move_match.group(1))).strip()
        return {
            "independent_row_unit": apt.group(1),
            "independent_visible_date": visible,
            "independent_apply_move_in_date": move_in,
        }
    return {
        "independent_row_unit": "",
        "independent_visible_date": "",
        "independent_apply_move_in_date": "",
    }


def source_date_origin(evidence: dict[str, str], parser_raw_date: str) -> str:
    visible = evidence["independent_visible_date"].strip()
    action = evidence["independent_apply_move_in_date"].strip()
    if visible and not GENERIC_AVAILABLE_RE.fullmatch(visible):
        return "visible_concrete_date"
    if action and parser_raw_date == action:
        return "apply_move_in_fallback"
    if visible and GENERIC_AVAILABLE_RE.fullmatch(visible):
        return "visible_relative_availability"
    if parser_raw_date:
        return "other_parser_source"
    return "source_date_blank"


def json_cell(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def load_cohort(ledger_path: Path, properties_path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    ledger = pd.read_csv(ledger_path, dtype=str, keep_default_na=False)
    cohort = ledger.loc[
        ledger["extraction_tier"].eq(TARGET_TIER)
        & ledger["comparison"].eq(TARGET_COMPARISON)
    ].copy()
    cohort["property_id"] = cohort["property_id"].astype(str)
    cohort["unit_key"] = cohort["unit_id"].map(normalize_unit_id)
    if cohort[["property_id", "unit_key"]].duplicated().any():
        duplicates = cohort.loc[
            cohort[["property_id", "unit_key"]].duplicated(False),
            ["property_id", "unit_id"],
        ]
        raise RuntimeError(f"Target cohort has duplicate keys:\n{duplicates.to_string(index=False)}")

    properties = pd.read_csv(properties_path, dtype=str, keep_default_na=False)
    properties = properties.rename(
        columns={"apartmentid": "property_id", "name": "configured_property_name"}
    )
    keep = properties[["property_id", "configured_property_name", "website"]].copy()
    merged = cohort.merge(keep, how="left", on="property_id", validate="many_to_one")
    if merged["website"].eq("").any():
        missing = sorted(merged.loc[merged["website"].eq(""), "property_id"].unique())
        raise RuntimeError(f"Missing configured website for properties: {missing}")
    return cohort, merged


def counts_by(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def main() -> None:
    args = parse_args()
    capture_ts = utc_capture_time(args.capture_time)
    capture_iso = capture_ts.isoformat()
    capture_date = capture_ts.date().isoformat()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    cohort, enriched = load_cohort(args.ledger, args.properties)
    property_count = int(cohort["property_id"].nunique())
    if not args.allow_cohort_drift and (
        len(cohort) != EXPECTED_UNITS or property_count != EXPECTED_PROPERTIES
    ):
        raise RuntimeError(
            f"Cohort drift: got {len(cohort)} units/{property_count} properties; "
            f"expected {EXPECTED_UNITS}/{EXPECTED_PROPERTIES}"
        )

    cohort_snapshot = enriched[
        [
            "property_id",
            "property_name",
            "website",
            "unit_id",
            "unit_key",
            "rp_date",
            "sx_date",
            "sx_date_provenance",
            "extraction_tier",
            "comparison",
        ]
    ].sort_values(["property_id", "unit_key"])
    cohort_snapshot.to_csv(
        args.output_dir / "july31_securecafe_target_cohort.csv", index=False
    )

    property_rows: list[dict[str, str]] = []
    for property_id, group in enriched.groupby("property_id", sort=True):
        first = group.iloc[0]
        property_rows.append(
            {
                "property_id": str(property_id),
                "property_name": str(first["property_name"]),
                "website": str(first["website"]),
            }
        )

    discoveries: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {
            executor.submit(discover_property, row, args.timeout): row["property_id"]
            for row in property_rows
        }
        for future in as_completed(future_map):
            result = future.result()
            discoveries[result["property_id"]] = result

    all_bases = sorted(
        {
            base
            for discovery in discoveries.values()
            for base in discovery["candidate_bases"]
        }
    )
    portals: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {
            executor.submit(fetch_portal, base, args.timeout): base for base in all_bases
        }
        for future in as_completed(future_map):
            base = future_map[future]
            portals[base] = future.result()

    selected_by_property: dict[str, dict[str, Any] | None] = {}
    selection_reason_by_property: dict[str, str] = {}
    candidate_scores_by_property: dict[str, list[dict[str, Any]]] = {}
    property_audit_rows: list[dict[str, Any]] = []

    for row in property_rows:
        property_id = row["property_id"]
        group = enriched.loc[enriched["property_id"].eq(property_id)]
        targets = set(group["unit_key"])
        discovery = discoveries[property_id]
        selected, selection_reason, scores = choose_portal(
            discovery, targets, portals
        )
        selected_by_property[property_id] = selected
        selection_reason_by_property[property_id] = selection_reason
        candidate_scores_by_property[property_id] = scores

        selected_fetch: FetchResult | None = selected["fetch"] if selected else None
        selected_overlap = (
            sorted(targets & set(selected["by_id"])) if selected else []
        )
        property_audit_rows.append(
            {
                "result_type": RESULT_TYPE,
                "capture_time_utc": capture_iso,
                "property_id": property_id,
                "property_name": row["property_name"],
                "website": row["website"],
                "target_units": len(targets),
                "marketing_status": discovery["marketing_status"],
                "marketing_final_url": discovery["marketing_final_url"],
                "marketing_error": discovery["marketing_error"],
                "marketing_challenge_shell": bool_text(
                    discovery["marketing_challenge_shell"]
                ),
                "marketing_pages_fetched": discovery["marketing_pages_fetched"],
                "marketing_attempts": discovery["marketing_attempts"],
                "candidate_count": len(discovery["candidate_bases"]),
                "candidate_bases_json": json_cell(discovery["candidate_bases"]),
                "candidate_scores_json": json_cell(scores),
                "selection_reason": selection_reason,
                "selected_portal_url": selected["url"] if selected else "",
                "selected_portal_final_url": (
                    selected_fetch.final_url if selected_fetch else ""
                ),
                "selected_portal_status": selected_fetch.status if selected_fetch else "",
                "selected_portal_error": selected_fetch.error if selected_fetch else "",
                "selected_portal_challenge_shell": (
                    bool_text(selected_fetch.challenge_shell) if selected_fetch else ""
                ),
                "selected_portal_attempts": (
                    selected_fetch.attempts if selected_fetch else ""
                ),
                "selected_parser_units": len(selected["units"]) if selected else 0,
                "selected_target_overlap_units": len(selected_overlap),
                "selected_target_overlap_ratio": (
                    len(selected_overlap) / len(targets) if targets else 0.0
                ),
                "selected_overlap_ids_json": json_cell(selected_overlap),
            }
        )

    manifest_rows: list[dict[str, Any]] = []
    manual_validation_rows: list[dict[str, Any]] = []
    for _, target in enriched.sort_values(["property_id", "unit_key"]).iterrows():
        property_id = str(target["property_id"])
        unit_key = str(target["unit_key"])
        selected = selected_by_property[property_id]
        selection_reason = selection_reason_by_property[property_id]
        current_units = selected["by_id"].get(unit_key, []) if selected else []
        current_unit = current_units[0] if current_units else None
        raw_date = str(current_unit.get("availability_date") or "") if current_unit else ""
        independent = (
            independent_row_evidence(selected["fetch"].text, str(target["unit_id"]))
            if selected and current_unit
            else {
                "independent_row_unit": "",
                "independent_visible_date": "",
                "independent_apply_move_in_date": "",
            }
        )
        raw_date_origin = source_date_origin(independent, raw_date)
        normalized: dict[str, Any] = {}
        if current_unit:
            normalized = _format_v2_unit(
                dict(current_unit), capture_ts, property_id=property_id
            )
        normalized_date = str(normalized.get("available_date") or "")
        provenance = str(normalized.get("availability_date_provenance") or "")
        normalized_d = parse_iso_date(normalized_date)
        current_future = (
            normalized_d > capture_ts.date() if normalized_d is not None else None
        )
        comparison, delta_days, within_one = compare_dates(
            normalized_date, str(target["rp_date"]), capture_date
        )
        if not current_unit:
            comparison = "current_unit_not_in_selected_inventory"
            delta_days = None
            within_one = None
        recovered_future = bool(
            current_unit
            and current_future
            and provenance == "explicit_future"
            and raw_date
        )
        if not selected:
            failure = selection_reason
        elif not current_unit:
            failure = "target_unit_not_in_current_inventory"
        elif not raw_date:
            failure = "current_source_date_blank"
        elif not recovered_future:
            failure = f"current_date_not_explicit_future:{provenance or 'missing'}"
        else:
            failure = ""

        row = {
            "result_type": RESULT_TYPE,
            "capture_time_utc": capture_iso,
            "capture_date_utc": capture_date,
            "historical_or_current": "current_live_compared_to_july31_rp",
            "property_id": property_id,
            "property_name": str(target["property_name"]),
            "website": str(target["website"]),
            "target_unit_id": str(target["unit_id"]),
            "target_unit_key": unit_key,
            "july31_rp_date": str(target["rp_date"]),
            "july31_surgex_date": str(target["sx_date"]),
            "july31_surgex_provenance": str(target["sx_date_provenance"]),
            "portal_selection_reason": selection_reason,
            "selected_portal_url": selected["url"] if selected else "",
            "selected_portal_status": selected["fetch"].status if selected else "",
            "selected_parser_inventory_units": len(selected["units"]) if selected else 0,
            "current_unit_present": bool_text(bool(current_unit)),
            "current_unit_match_count": len(current_units),
            "current_parser_unit_number": (
                str(current_unit.get("unit_number") or "") if current_unit else ""
            ),
            "current_raw_source_date": raw_date,
            "current_raw_date_origin": raw_date_origin,
            "current_visible_source_date": independent["independent_visible_date"],
            "current_apply_move_in_date": independent[
                "independent_apply_move_in_date"
            ],
            "current_normalized_date": normalized_date,
            "current_normalized_provenance": provenance,
            "current_source_date_present": bool_text(bool(raw_date)),
            "current_future_presence": bool_text(current_future),
            "current_explicit_future_recovered": bool_text(recovered_future),
            "current_vs_rp_comparison": comparison,
            "current_minus_rp_days": "" if delta_days is None else delta_days,
            "current_within_one_day_of_rp": bool_text(within_one),
            "failure": failure,
        }
        manifest_rows.append(row)

        if property_id in MANUAL_PROPERTY_IDS and selected and current_unit:
            if not any(
                existing["property_id"] == property_id
                for existing in manual_validation_rows
            ):
                manual_validation_rows.append(
                    {
                        "result_type": RESULT_TYPE,
                        "capture_time_utc": capture_iso,
                        "property_id": property_id,
                        "property_name": str(target["property_name"]),
                        "portal_url": selected["url"],
                        "target_unit_id": str(target["unit_id"]),
                        **independent,
                        "patched_parser_raw_date": raw_date,
                        "canonical_date": normalized_date,
                        "canonical_provenance": provenance,
                        "independent_action_matches_parser": bool_text(
                            bool(independent["independent_apply_move_in_date"])
                            and independent["independent_apply_move_in_date"]
                            == raw_date
                        ),
                    }
                )

    manifest = pd.DataFrame(manifest_rows)
    property_audit = pd.DataFrame(property_audit_rows).sort_values("property_id")
    manual_validation = pd.DataFrame(manual_validation_rows).sort_values("property_id")
    manifest.to_csv(args.output_dir / "current_live_unit_manifest.csv", index=False)
    property_audit.to_csv(args.output_dir / "current_live_property_audit.csv", index=False)
    manual_validation.to_csv(
        args.output_dir / "manual_three_property_validation.csv", index=False
    )

    recovered_mask = manifest["current_explicit_future_recovered"].eq("true")
    action_fallback_mask = (
        recovered_mask
        & manifest["current_raw_date_origin"].eq("apply_move_in_fallback")
    )
    present_mask = manifest["current_unit_present"].eq("true")
    raw_present_mask = manifest["current_source_date_present"].eq("true")
    within_mask = manifest["current_within_one_day_of_rp"].eq("true")
    selected_mask = property_audit["selected_portal_url"].ne("")
    summary = {
        "result_type": RESULT_TYPE,
        "labels": {
            "historical_oracle": "RealPage dates captured July 31, 2026",
            "source_evidence": "current-live public marketing and SecureCafe pages",
            "execution": "local investigation; not GCP; not canary; no CAPTCHA solving",
        },
        "capture_time_utc": capture_iso,
        "capture_date_utc": capture_date,
        "scope": {
            "filter": {
                "extraction_tier": TARGET_TIER,
                "comparison": TARGET_COMPARISON,
            },
            "target_units": int(len(manifest)),
            "target_properties": int(manifest["property_id"].nunique()),
            "expected_target_units": EXPECTED_UNITS,
            "expected_target_properties": EXPECTED_PROPERTIES,
        },
        "discovery_and_selection": {
            "marketing_http_200_properties": int(
                property_audit["marketing_status"].eq(200).sum()
            ),
            "properties_with_candidate_base": int(
                property_audit["candidate_count"].gt(0).sum()
            ),
            "distinct_candidate_bases": len(all_bases),
            "candidate_portal_http_status_counts": counts_by(
                str(portals[base]["fetch"].status) for base in all_bases
            ),
            "properties_with_selected_unit_matched_portal": int(selected_mask.sum()),
            "selection_reason_counts": counts_by(
                property_audit["selection_reason"].astype(str)
            ),
        },
        "current_live_results": {
            "target_units_present_in_current_inventory": int(present_mask.sum()),
            "target_properties_with_any_current_unit": int(
                manifest.loc[present_mask, "property_id"].nunique()
            ),
            "target_units_with_raw_source_date": int(raw_present_mask.sum()),
            "target_properties_with_any_raw_source_date": int(
                manifest.loc[raw_present_mask, "property_id"].nunique()
            ),
            "explicit_future_recovered_units": int(recovered_mask.sum()),
            "explicit_future_recovered_properties": int(
                manifest.loc[recovered_mask, "property_id"].nunique()
            ),
            "apply_move_in_fallback_recovered_units": int(
                action_fallback_mask.sum()
            ),
            "apply_move_in_fallback_recovered_properties": int(
                manifest.loc[action_fallback_mask, "property_id"].nunique()
            ),
            "within_one_day_of_july31_rp_units": int(within_mask.sum()),
            "within_one_day_of_july31_rp_properties": int(
                manifest.loc[within_mask, "property_id"].nunique()
            ),
            "comparison_counts": counts_by(
                manifest["current_vs_rp_comparison"].astype(str)
            ),
            "provenance_counts": counts_by(
                manifest["current_normalized_provenance"].astype(str)
            ),
            "raw_source_origin_counts": counts_by(
                manifest["current_raw_date_origin"].astype(str)
            ),
            "failure_counts": counts_by(
                value if value else "recovered"
                for value in manifest["failure"].astype(str)
            ),
        },
        "manual_three_property_validation": manual_validation.to_dict("records"),
        "reproducibility": {
            "git_head": git_value("rev-parse", "HEAD"),
            "git_branch": git_value("branch", "--show-current"),
            "ledger_path": str(args.ledger.resolve()),
            "ledger_sha256": sha256_file(args.ledger),
            "properties_path": str(args.properties.resolve()),
            "properties_sha256": sha256_file(args.properties),
            "script_path": str(SCRIPT_PATH),
            "script_sha256": sha256_file(SCRIPT_PATH),
            "parser_path": str(
                REPO_ROOT / "ma_poc/pms/adapters/rentcafe.py"
            ),
            "parser_sha256": sha256_file(
                REPO_ROOT / "ma_poc/pms/adapters/rentcafe.py"
            ),
            "formatter_path": str(REPO_ROOT / "ma_poc/core/schema_v2.py"),
            "formatter_sha256": sha256_file(
                REPO_ROOT / "ma_poc/core/schema_v2.py"
            ),
            "curl_impersonation_attempts": ["chrome120", "chrome124"],
            "workers": args.workers,
            "timeout_seconds": args.timeout,
        },
        "limitations": [
            "Current-live inventory is compared with a July-31 RealPage oracle; units can legitimately lease, appear, or change dates between captures.",
            "A one-day difference is reported as agreement for this audit because the two systems were captured on different days.",
            "Properties whose public legacy portal is gone, blocked, empty, or no longer lists a July-31 target unit are reported as failures rather than inferred recoveries.",
            "This is local evidence from direct public fetches, not a paid GCP canary or production run.",
        ],
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "manual_three_property_validation.json").write_text(
        json.dumps(manual_validation.to_dict("records"), indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
