from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import importlib.util
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ma_poc.fetch.hyperbrowser_backend import (
    hyperbrowser_property_call_count,
    reset_hyperbrowser_property_counts,
)
from ma_poc.pms.adapters._probe import (
    reset_web_unlocker_call_count,
    web_unlocker_call_count,
)


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "annaberg_nesthub_lane"
RAW = LANE / "raw"
EVIDENCE = LANE / "evidence_annaberg_1765_nesthub_discovery.json"
PROPOSAL = LANE / "annaberg_1765_nesthub_implementation_proposal.md"
MATERIALIZER = LANE / "materialize_annaberg_nesthub_discovery.py"
HARNESS = ROOT / "appfolio_wix_residual_lane" / "run_current_full_e2e.py"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
SUMMARY = ROOT / "strict_recovery_ledger_current_summary.json"
PROPERTIES = Path("ma_poc/config/properties.csv")

PROPERTY_ID = "1765"
PROPERTY_NAME = "Annaberg"
PROPERTY_PUBLIC_NAME = "Annaberg Apartments"
ADDRESS = "2905 Arrowhead Dr"
CITY = "Augusta"
STATE = "GA"
ZIP_CODE = "30909"
CONFIGURED_ID = "56"
CURRENT_ID = "602"
CONFIGURED_URL = (
    "https://www.augustarentalhomes.net/_system/listings/56/"
    "2905-Arrowhead-Drive---D3-Augusta-GA-30909-US"
)
COMMUNITY_URL = "https://www.augustarentalhomes.net/annabergs"
ROSTER_URL = "https://www.augustarentalhomes.net/augusta-homes-for-rent"
CURRENT_URL = (
    "https://www.augustarentalhomes.net/_system/listings/602/"
    "2905-Arrowhead-Drive---E7-Augusta-GA-30909-US"
)
CONTROL_URLS = {
    "configured_stale_56": CONFIGURED_URL,
    "same_zip_wrong_street_601": (
        "https://www.augustarentalhomes.net/_system/listings/601/"
        "1531-Abby-Way---1-Augusta-GA-30909-US"
    ),
    "wrong_property_city_zip_606": (
        "https://www.augustarentalhomes.net/_system/listings/606/"
        "104-Canary-Street-Thomson-GA-30824-US"
    ),
}

EXPECTED_ENV = {
    "COMPLIANCE_MODE": "1",
    "ENABLE_TIER4_LLM": "false",
    "ENABLE_TIER_ESCALATION": "false",
    "ENABLE_DC_PROXY_TIER": "false",
    "ENABLE_RESIDENTIAL_TIER": "false",
    "ENABLE_RESIDENTIAL_RENDER_TIER": "false",
    "ENABLE_UNLOCKER_TIER": "false",
    "ENABLE_FLARESOLVERR_TIER": "false",
    "FETCH_BACKEND": "brightdata",
    "RENDER_BACKEND": "local",
    "PROBE_PROXY_URL": "",
    "PROXY_POOL_URLS": "",
    "ENABLE_RENDER_ON_EMPTY": "false",
    "ENABLE_PLAN_UNIT_RENDER": "false",
    "ENABLE_ENTRATA_PLAN_RENDER": "false",
    "ENABLE_BODY_RESOLVER": "false",
    "ENABLE_CRAWL_GET_GATE": "false",
}

CRITICAL_SOURCE_FILES = (
    Path("ma_poc/pms/scraper.py"),
    Path("ma_poc/pms/detector.py"),
    Path("ma_poc/pms/adapters/generic_plan_text.py"),
    Path("ma_poc/pms/adapters/_universal_recovery.py"),
    Path("ma_poc/pms/adapters/_probe.py"),
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def clean(value: object) -> str:
    return " ".join(str(value or "").split())


def norm(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


ADDRESS_ALIASES = {
    "avenue": "ave",
    "boulevard": "blvd",
    "court": "ct",
    "drive": "dr",
    "highway": "hwy",
    "lane": "ln",
    "parkway": "pkwy",
    "place": "pl",
    "road": "rd",
    "street": "st",
}


def norm_address(value: object) -> str:
    return " ".join(ADDRESS_ALIASES.get(token, token) for token in norm(value).split())


def host(url: str) -> str:
    value = (urlparse(url).hostname or "").casefold().rstrip(".")
    return value[4:] if value.startswith("www.") else value


def source_snapshot() -> dict[str, object]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--short"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return {
        "git_head": head,
        "dirty": bool(status),
        "git_status_short": status,
        "critical_file_sha256": {
            str(path): sha256_file(path) for path in CRITICAL_SOURCE_FILES
        },
    }


def cohort_snapshot() -> dict[str, object]:
    ledger = read_csv(LEDGER)
    remaining = read_csv(REMAINING)
    return {
        "ledger_sha256": sha256_file(LEDGER),
        "remaining_sha256": sha256_file(REMAINING),
        "summary_sha256": sha256_file(SUMMARY),
        "ledger_rows": len(ledger),
        "remaining_rows": len(remaining),
        "property_in_ledger": any(row["property_id"] == PROPERTY_ID for row in ledger),
        "property_in_remaining": any(
            row["property_id"] == PROPERTY_ID for row in remaining
        ),
    }


class DirectClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.trust_env = False
        self.captures: dict[str, dict[str, object]] = {}

    def get(self, label: str, url: str) -> tuple[requests.Response, BeautifulSoup]:
        response = self.session.get(
            url,
            timeout=30,
            allow_redirects=True,
            proxies={},
            headers={"User-Agent": "Mozilla/5.0 (compatible; PropAiStrictAudit/1.0)"},
        )
        body = response.content or b""
        raw_path = RAW / f"{label}.html.gz"
        raw_path.write_bytes(gzip.compress(body, compresslevel=9, mtime=0))
        self.captures[label] = {
            "requested_url": url,
            "final_url": str(response.url),
            "status": int(response.status_code),
            "body_bytes": len(body),
            "body_sha256": sha256_bytes(body),
            "raw_gzip": str(raw_path),
            "raw_gzip_sha256": sha256_file(raw_path),
        }
        if response.status_code != 200 or not body:
            raise RuntimeError(
                f"direct fetch failed label={label} status={response.status_code} "
                f"bytes={len(body)} final={response.url}"
            )
        return response, BeautifulSoup(body.decode("utf-8", "replace"), "html.parser")


def text(node) -> str:
    return clean(node.get_text(" ", strip=True)) if node else ""


def parse_money(value: str) -> int:
    match = re.search(r"\$\s*([0-9][0-9,]*)", value or "")
    return int(match.group(1).replace(",", "")) if match else 0


def listing_id_from_url(url: str) -> str:
    match = re.search(r"/_system/listings/(\d+)(?:/|$)", urlparse(url).path)
    return match.group(1) if match else ""


def field_value(soup: BeautifulSoup, label: str) -> str:
    wanted = norm(label)
    for row in soup.select(".sub-detail"):
        key = norm(text(row.select_one(".sub-detail__label"))).rstrip()
        if key == wanted:
            return text(row.select_one(".sub-detail__value"))
    return ""


def detail_record(soup: BeautifulSoup, final_url: str) -> dict[str, object]:
    canonical = soup.select_one('link[rel="canonical"][href]')
    description = text(soup.select_one(".description"))
    plan_match = re.search(
        r"\bThe\s+([A-Z][A-Za-z0-9' -]{1,40}?)\s+is\s+a\s+"
        r"\d+(?:\.\d+)?\s+bedroom\b",
        description,
    )
    return {
        "listing_id": listing_id_from_url(final_url),
        "canonical_url": str(canonical.get("href") or "") if canonical else "",
        "title": text(soup.title),
        "street_heading": text(soup.select_one("h1")),
        "city_state_zip_heading": text(soup.select_one("h2")),
        "rent": parse_money(text(soup.select_one(".key-detail.price .value"))),
        "bedrooms": text(soup.select_one(".key-detail.bedrooms .value")),
        "bathrooms": text(soup.select_one(".key-detail.bathrooms .value")),
        "sqft": text(soup.select_one(".key-detail.sqft .value")),
        "status": text(soup.select_one(".key-detail.rent .label")),
        "availability_date": field_value(soup, "Date Available"),
        "description": description,
        "description_mentions_property": PROPERTY_PUBLIC_NAME.casefold()
        in description.casefold(),
        "description_mentions_base_address": norm_address(ADDRESS)
        in norm_address(description),
        "floor_plan_name": clean(plan_match.group(1)) if plan_match else "",
    }


def parse_roster(soup: BeautifulSoup, base_url: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in soup.select(".nhw-list__item"):
        anchor = item.select_one("a[href][data-id]")
        if anchor is None:
            continue
        href = urljoin(base_url, str(anchor.get("href") or ""))
        listing_id = clean(anchor.get("data-id") or "")
        location = text(item.select_one(".nhw-list__location"))
        parts = [part.strip() for part in location.split(",")]
        street = parts[0] if parts else ""
        city = parts[1] if len(parts) > 1 else ""
        state_zip = parts[2] if len(parts) > 2 else ""
        canonical_street = norm_address(ADDRESS)
        normalized_street = norm_address(street)
        tail = normalized_street[len(canonical_street) :].strip(
        ) if normalized_street.startswith(canonical_street) else ""
        exact_property_address = bool(
            normalized_street.startswith(canonical_street)
            and (not tail or re.fullmatch(r"[a-z0-9-]{1,16}", tail.replace(" ", "")))
            and norm(city) == norm(CITY)
            and norm(state_zip) == norm(f"{STATE} {ZIP_CODE}")
        )
        rows.append(
            {
                "listing_id": listing_id,
                "path_listing_id": listing_id_from_url(href),
                "detail_url": href,
                "rent": parse_money(text(item.select_one(".nhw-list__price"))),
                "details": text(item.select_one(".nhw-list__details")),
                "location": location,
                "street": street,
                "city": city,
                "state_zip": state_zip,
                "property_type": text(item.select_one(".nhw-list__prop-type")),
                "availability": text(item.select_one(".nhw-list__availability")),
                "exact_property_address": exact_property_address,
            }
        )
    return rows


def load_harness():
    spec = importlib.util.spec_from_file_location("annaberg_e2e", HARNESS)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load current full configured E2E harness")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def current_e2e(residual: dict[str, str], metadata: dict[str, str]) -> dict[str, object]:
    harness = load_harness()
    reset_web_unlocker_call_count()
    reset_hyperbrowser_property_counts()
    repeats = []
    for repeat in range(1, 4):
        row = await harness.one(residual, metadata)
        repeats.append(
            {
                "repeat": repeat,
                "configured_fetch": row.get("configured_fetch") or {},
                "adapter": row.get("adapter") or "",
                "tier": row.get("tier") or "",
                "unit_rows": int(row.get("emitted_unit_rows") or 0),
                "plan_rows": int(row.get("plan_rows") or 0),
                "native_rows": int(row.get("native_identity_rows") or 0),
                "strict_native_positive_rent_rows": int(
                    row.get("strict_native_positive_rent_rows") or 0
                ),
                "plan_samples": row.get("plan_samples") or [],
                "fallback_chain": row.get("fallback_chain") or [],
                "llm_interactions": row.get("llm_interactions") or [],
                "errors": row.get("errors") or [],
                "strict_rejection": row.get("strict_rejection") or "",
            }
        )
        print(
            json.dumps(
                {
                    "repeat": repeat,
                    "adapter": row.get("adapter") or "",
                    "tier": row.get("tier") or "",
                    "units": int(row.get("emitted_unit_rows") or 0),
                    "plans": int(row.get("plan_rows") or 0),
                    "strict": int(row.get("strict_native_positive_rent_rows") or 0),
                }
            ),
            flush=True,
        )
    hb_calls = hyperbrowser_property_call_count(PROPERTY_ID)
    unlocker_calls = web_unlocker_call_count()
    if hb_calls or unlocker_calls:
        raise RuntimeError(
            f"restricted backend observed hb={hb_calls} unlocker={unlocker_calls}"
        )
    expected_gap = all(
        row["configured_fetch"].get("status") == 200
        and row["adapter"] == "generic_plan_text"
        and row["unit_rows"] == 0
        and row["plan_rows"] == 1
        and row["native_rows"] == 0
        and row["strict_native_positive_rent_rows"] == 0
        and not row["llm_interactions"]
        for row in repeats
    )
    if not expected_gap:
        raise RuntimeError(f"configured-route gap changed: {repeats}")
    return {
        "repeats": repeats,
        "all_repeats_confirm_current_gap": expected_gap,
        "web_unlocker_call_count": unlocker_calls,
        "hyperbrowser_call_count": hb_calls,
    }


def proposal_text() -> str:
    return """# Annaberg / NestHub narrow production proposal

Status: discovery only. No repository source, strict ledger, or canary state was changed.

## Current gap

The configured URL is native NestHub listing 56 at Annaberg's exact address,
but the provider explicitly marks that apartment unavailable. The configured
pipeline repeats as zero unit rows plus one synthetic plan row whose $500 is
the deposit, not rent. The same official host currently publishes native
listing 602 (unit E7) in its Available Rentals SSR roster and detail page.

## Proposed fail-closed recovery

1. Add a narrow helper such as `_nesthub_public.py`; do not parse arbitrary
   property-manager portfolios.
2. Trigger only on a configured same-host `/_system/listings/{numeric_id}/...`
   NestHub detail with `resources.nesthub.com`, `.nhw-details`, exact canonical
   property identity in the scoped description/address, and an explicit
   unavailable status or otherwise-empty primary extraction.
3. Follow the exact property's same-host community link by name. Require its
   h1/address to match the configured property and its `#nh-props` widget to
   publish `data-ion=listing-widget` plus one non-empty `data-hard-filters`.
4. Follow the same-host, page-published Available Rentals link. Parse only the
   bounded SSR `.nhw-list__item > a[data-id]` roster; reject an oversized or
   non-NestHub response.
5. Select only rows whose normalized base street plus city/state/ZIP exactly
   match the configured property. Never accept all same-manager rows.
6. Fetch each selected same-host detail and require card/path/canonical native
   ID agreement, exact address, property name in the scoped `.description`,
   `For Rent`, positive card/detail-equal rent, explicit availability date,
   bedroom/bath/sqft, and a unique visible unit suffix.
7. Preserve provider floor-plan names only from the scoped sentence shape
   `The {name} is a {n} bedroom...`; for unit 602 this is exactly
   `The Chesapeake`. Do not infer a name from dimensions or URL slug.
8. Emit visible unit suffix `E7` as unit_number, the full provider address as
   `provider_unit_address`, and native ID 602 as a pending provenance key
   (e.g. `nesthub_listing_id`; do not claim cross-run stability yet).
9. Test the exact-property unavailable listing 56, same-ZIP wrong-street
   listing 601, and wrong-property/city/ZIP listing 606 as mandatory controls.
10. Use ordinary direct GET only. No LLM, render, Hyperbrowser, unlocker,
    CAPTCHA solving, FlareSolverr, proxy, or fingerprint rotation is needed.

Suggested tier: `TIER_1_PUBLIC_NESTHUB_SSR_EXACT_PROPERTY`.
"""


async def main() -> None:
    for name, expected in EXPECTED_ENV.items():
        actual = os.environ.get(name, "").casefold()
        if actual != expected:
            raise RuntimeError(f"{name}={actual!r}; expected {expected!r}")

    RAW.mkdir(parents=True, exist_ok=True)
    source_before = source_snapshot()
    cohort_before = cohort_snapshot()
    if cohort_before["property_in_ledger"] or not cohort_before["property_in_remaining"]:
        raise RuntimeError("PID 1765 is not an untouched current remainder")

    residual = next(
        row for row in read_csv(REMAINING) if row["property_id"] == PROPERTY_ID
    )
    metadata = next(
        row for row in read_csv(PROPERTIES) if row.get("apartmentid") == PROPERTY_ID
    )
    metadata_checks = {
        "name": metadata.get("name") == PROPERTY_NAME,
        "address": metadata.get("address") == ADDRESS,
        "city": metadata.get("city") == CITY,
        "state": metadata.get("state") == STATE,
        "zip": metadata.get("zip") == ZIP_CODE,
        "configured_url": metadata.get("website") == CONFIGURED_URL,
    }
    if not all(metadata_checks.values()):
        raise RuntimeError(f"metadata mismatch: {metadata_checks}")

    client = DirectClient()
    configured_response, configured_soup = client.get("configured_56", CONFIGURED_URL)
    community_response, community_soup = client.get("community_annaberg", COMMUNITY_URL)
    roster_response, roster_soup = client.get("available_roster", ROSTER_URL)
    current_response, current_soup = client.get("current_602", CURRENT_URL)
    controls: dict[str, dict[str, object]] = {}
    for label, url in CONTROL_URLS.items():
        response, soup = client.get(f"control_{label}", url)
        controls[label] = detail_record(soup, str(response.url))

    configured = detail_record(configured_soup, str(configured_response.url))
    community_address = text(community_soup.select_one("address"))
    community_widget = community_soup.select_one(
        '#nh-props[data-ion="listing-widget"][data-hard-filters]'
    )
    community_checks = {
        "same_host": host(str(community_response.url)) == host(CONFIGURED_URL),
        "canonical_path": urlparse(str(community_response.url)).path == "/annabergs",
        "property_name": PROPERTY_PUBLIC_NAME.casefold()
        in text(community_soup.select_one("h1")).casefold(),
        "exact_address": norm_address(f"{ADDRESS} {CITY} {STATE} {ZIP_CODE}")
        in norm_address(community_address),
        "nesthub_resources": "resources.nesthub.com" in str(community_soup),
        "property_widget": community_widget is not None,
        "hard_filter_exact": clean(
            community_widget.get("data-hard-filters") if community_widget else ""
        )
        == "search=ANNBRG",
        "available_units_heading": "available units"
        in norm(text(community_soup)).casefold(),
    }
    if not all(community_checks.values()):
        raise RuntimeError(f"community boundary failed: {community_checks}")

    configured_checks = {
        "same_host": host(str(configured_response.url)) == host(CONFIGURED_URL),
        "native_id": configured["listing_id"] == CONFIGURED_ID,
        "exact_street": norm_address(ADDRESS)
        in norm_address(configured["street_heading"]),
        "exact_city_state_zip": norm(configured["city_state_zip_heading"])
        == norm(f"{CITY} {STATE} {ZIP_CODE}"),
        "scoped_property_name": configured["description_mentions_property"],
        "explicitly_unavailable": configured["status"]
        == "This Property Is Not Available",
    }
    if not all(configured_checks.values()):
        raise RuntimeError(f"configured listing boundary failed: {configured_checks}")

    roster_marker = roster_soup.select_one(
        '#nesthub-property-list-view[data-ion="listing-list"]'
    )
    roster = parse_roster(roster_soup, str(roster_response.url))
    roster_checks = {
        "same_host": host(str(roster_response.url)) == host(CONFIGURED_URL),
        "canonical_path": urlparse(str(roster_response.url)).path
        == "/augusta-homes-for-rent",
        "nesthub_marker": roster_marker is not None,
        "bounded_nonempty_roster": 1 <= len(roster) <= 100,
        "all_native_ids_agree": all(
            row["listing_id"] and row["listing_id"] == row["path_listing_id"]
            for row in roster
        ),
    }
    if not all(roster_checks.values()):
        raise RuntimeError(f"roster boundary failed: {roster_checks}")
    candidates = [row for row in roster if row["exact_property_address"]]
    if [row["listing_id"] for row in candidates] != [CURRENT_ID]:
        raise RuntimeError(f"unexpected exact-address candidates: {candidates}")

    target_card = candidates[0]
    current = detail_record(current_soup, str(current_response.url))
    current_checks = {
        "same_host": host(str(current_response.url)) == host(CONFIGURED_URL),
        "card_detail_url_exact": target_card["detail_url"] == CURRENT_URL,
        "card_detail_native_id_exact": target_card["listing_id"]
        == current["listing_id"]
        == CURRENT_ID,
        "canonical_native_id_exact": listing_id_from_url(
            str(current["canonical_url"])
        )
        == CURRENT_ID,
        "exact_street_and_unit": norm_address("2905 Arrowhead Dr E7")
        == norm_address(current["street_heading"]),
        "exact_city_state_zip": norm(current["city_state_zip_heading"])
        == norm(f"{CITY} {STATE} {ZIP_CODE}"),
        "scoped_property_name": current["description_mentions_property"],
        "scoped_base_address": current["description_mentions_base_address"],
        "status_for_rent": current["status"] == "For Rent",
        "positive_rent": current["rent"] == target_card["rent"] == 1160,
        "dimensions": current["bedrooms"] == "2"
        and current["bathrooms"] == "2.5"
        and current["sqft"] == "1268",
        "availability_card_detail_exact": target_card["availability"]
        == "Available: 08-19-2026"
        and current["availability_date"] == "08-19-2026",
        "provider_floor_plan_name_exact": current["floor_plan_name"]
        == "Chesapeake",
    }
    if not all(current_checks.values()):
        raise RuntimeError(f"current detail gate failed: {current_checks}")

    control_assertions = {
        "configured_stale_56": {
            "excluded": controls["configured_stale_56"]["status"]
            == "This Property Is Not Available",
            "reason": "exact_property_but_explicitly_unavailable",
        },
        "same_zip_wrong_street_601": {
            "excluded": norm_address(ADDRESS)
            not in norm_address(controls["same_zip_wrong_street_601"]["street_heading"])
            and not controls["same_zip_wrong_street_601"][
                "description_mentions_property"
            ],
            "reason": "same_city_state_zip_but_wrong_street_and_scoped_property_name",
        },
        "wrong_property_city_zip_606": {
            "excluded": norm_address(ADDRESS)
            not in norm_address(controls["wrong_property_city_zip_606"]["street_heading"])
            and norm(controls["wrong_property_city_zip_606"]["city_state_zip_heading"])
            != norm(f"{CITY} {STATE} {ZIP_CODE}")
            and not controls["wrong_property_city_zip_606"][
                "description_mentions_property"
            ],
            "reason": "wrong_property_street_city_zip_and_scoped_property_name",
        },
    }
    if len(control_assertions) < 3 or not all(
        bool(value["excluded"]) for value in control_assertions.values()
    ):
        raise RuntimeError(f"control exclusion failed: {control_assertions}")

    e2e = await current_e2e(residual, metadata)
    source_after = source_snapshot()
    cohort_after = cohort_snapshot()
    if source_before["git_head"] != source_after["git_head"]:
        raise RuntimeError("git HEAD changed during discovery")
    if source_before["critical_file_sha256"] != source_after["critical_file_sha256"]:
        raise RuntimeError("critical source changed during discovery")
    # Other isolated recovery lanes may be admitted by the parent while this
    # three-replay materializer is running. That is expected shared-state
    # movement, not a mutation by this lane. Fail only if *this* candidate's
    # untouched status changed; retain both global snapshots for provenance.
    if cohort_after["property_in_ledger"] or not cohort_after["property_in_remaining"]:
        raise RuntimeError("PID 1765 cohort status changed during discovery")

    unit = {
        "provider": "NestHub",
        "native_listing_id": CURRENT_ID,
        "unit_number": "E7",
        "provider_unit_address": "2905 Arrowhead Drive - E7",
        "floor_plan_name": "Chesapeake",
        "bedrooms": "2",
        "bathrooms": "2.5",
        "sqft": "1268",
        "rent": 1160,
        "availability_status": "AVAILABLE",
        "availability_date": "08-19-2026",
        "source_url": CURRENT_URL,
        "source_ids_proposal": {"nesthub_listing_id": CURRENT_ID},
        "source_id_scope_proposal": "UNIT_PENDING",
    }
    strict_direct_pass = bool(
        all(configured_checks.values())
        and all(community_checks.values())
        and all(roster_checks.values())
        and all(current_checks.values())
        and all(bool(value["excluded"]) for value in control_assertions.values())
        and unit["rent"] > 0
        and unit["unit_number"]
        and unit["availability_date"]
        and unit["floor_plan_name"]
    )
    if not strict_direct_pass:
        raise RuntimeError("strict provider-direct gate failed")

    PROPOSAL.write_text(proposal_text(), encoding="utf-8")
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "lane": "annaberg_nesthub_exact_property_ssr_discovery",
        "property_id": int(PROPERTY_ID),
        "property_name": PROPERTY_NAME,
        "canonical_identity": {
            "address": ADDRESS,
            "city": CITY,
            "state": STATE,
            "zip": ZIP_CODE,
            "configured_url": CONFIGURED_URL,
            "metadata_checks": metadata_checks,
        },
        "cohort": {
            "boundary": "exact_2026-07-31_FAILED_NO_DATA_344",
            "source_adapter_0731": residual.get("source_adapter_0731") or "",
            "current_detected_adapter": residual.get("current_detected_adapter") or "",
            "state_before": cohort_before,
            "state_after": cohort_after,
            "global_cohort_changed_during_run": cohort_before != cohort_after,
            "confirmed_remaining_not_ledger": True,
        },
        "configured_stale_listing": {
            "record": configured,
            "checks": configured_checks,
            "must_never_emit": True,
        },
        "official_property_chain": {
            "community_url": COMMUNITY_URL,
            "community_checks": community_checks,
            "roster_url": ROSTER_URL,
            "roster_checks": roster_checks,
            "strict_chain_pass": all(community_checks.values())
            and all(roster_checks.values()),
        },
        "provider_roster": {
            "total_current_rows": len(roster),
            "exact_property_address_rows": len(candidates),
            "rows": roster,
        },
        "current_provider_detail": {
            "record": current,
            "checks": current_checks,
        },
        "boundary_controls": {
            label: {"record": controls[label], **assertion}
            for label, assertion in control_assertions.items()
        },
        "unit": unit,
        "native_identity_rows": 1,
        "native_positive_rent_rows": 1,
        "native_explicit_availability_date_rows": 1,
        "native_provider_floor_plan_name_rows": 1,
        "provider_direct_strict_pass": strict_direct_pass,
        "current_full_configured_pipeline": e2e,
        "current_production_e2e_strict_pass": False,
        "strict_verdict": (
            "discovery_pass_exact_first_party_nesthub_property_boundary_"
            "but_current_production_pipeline_gap"
        ),
        "authoritative_ledger_eligible": False,
        "authoritative_ledger_hold_reason": (
            "No current NestHub recovery implementation; parent must independently "
            "replay and only admit after configured-route E2E emits the same bounded row."
        ),
        "production_implementation_proposal": str(PROPOSAL),
        "materializer": str(MATERIALIZER),
        "source_snapshot_before": source_before,
        "source_snapshot_after": source_after,
        "guardrails": {
            "compliance_mode": True,
            "llm": False,
            "paid_canary": False,
            "proxy": False,
            "web_unlocker": False,
            "hyperbrowser": False,
            "captcha_solving": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "environment": EXPECTED_ENV,
        },
        "raw_captures": client.captures,
        "repo_mutation": "none",
        "ledger_mutation": "none",
    }
    EVIDENCE.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "artifact": str(EVIDENCE),
                "artifact_sha256": sha256_file(EVIDENCE),
                "materializer_sha256": sha256_file(MATERIALIZER),
                "proposal": str(PROPOSAL),
                "proposal_sha256": sha256_file(PROPOSAL),
                "provider_direct_rows": 1,
                "provider_direct_strict_pass": strict_direct_pass,
                "current_pipeline_counts": [
                    row["strict_native_positive_rent_rows"]
                    for row in e2e["repeats"]
                ],
                "authoritative_ledger_eligible": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
