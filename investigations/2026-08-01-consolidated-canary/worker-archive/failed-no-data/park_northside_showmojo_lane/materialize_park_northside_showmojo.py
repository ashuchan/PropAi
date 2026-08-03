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
from urllib.parse import parse_qs, urljoin, urlparse

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
LANE = ROOT / "park_northside_showmojo_lane"
RAW = LANE / "raw"
EVIDENCE = LANE / "evidence_park_northside_38378_showmojo_discovery.json"
PROPOSAL = LANE / "park_northside_showmojo_implementation_proposal.md"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
SUMMARY = ROOT / "strict_recovery_ledger_current_summary.json"
PROPERTIES = Path("ma_poc/config/properties.csv")
HARNESS = ROOT / "appfolio_wix_residual_lane" / "run_current_full_e2e.py"

PROPERTY_ID = "38378"
PROPERTY_NAME = "Park Northside"
CANONICAL_ADDRESS = "1601 Roane St"
CANONICAL_CITY = "Richmond"
CANONICAL_STATE = "VA"
CANONICAL_ZIP = "23222"
CONFIGURED_URL = "https://www.parknorthsiderva.com/"
FLOORPLANS_URL = "https://www.parknorthsiderva.com/floorplans"
MANAGER_URL = "https://dobrinpropertymanagement.com/"
MANAGER_LISTINGS_URL = (
    "https://dobrinpropertymanagement.com/richmond-va-property-listings/"
)
SHOWMOJO_EMBED_URL = "https://showmojo.com/fea92db007/listings/mapsearch"
SHOWMOJO_ACCOUNT = "fea92db007"
APPLICATION_SITE_ID = "44261A"
CONTROL_IDS = {
    "2ae5ea2026": "wrong_brand_and_zip_graystone",
    "097b680090": "wrong_brand_and_zip_lakeview",
    "e3afa4f0bf": "park_name_template_spill_wrong_zip",
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
    Path("ma_poc/pms/resolver.py"),
    Path("ma_poc/pms/adapters/rentmanager.py"),
    Path("ma_poc/pms/adapters/wix_nopms.py"),
    Path("ma_poc/pms/adapters/_pms_portal_hop.py"),
    Path("ma_poc/pms/adapters/_universal_recovery.py"),
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def clean_text(value: str) -> str:
    return " ".join(str(value or "").split())


def node_text(node) -> str:
    return clean_text(node.get_text(" ", strip=True)) if node else ""


def parse_rent(value: str) -> int:
    match = re.search(r"\$\s*([0-9][0-9,]*)", value or "")
    return int(match.group(1).replace(",", "")) if match else 0


def normalized_host(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def site_id(url: str) -> str:
    return str(parse_qs(urlparse(url).query).get("siteID", [""])[0])


def links(soup: BeautifulSoup, base_url: str) -> list[str]:
    return [
        urljoin(base_url, node.get("href") or node.get("src") or "")
        for node in soup.select("a[href], iframe[src]")
    ]


def source_snapshot() -> dict:
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


def cohort_snapshot() -> dict:
    return {
        "ledger_sha256": sha256_file(LEDGER),
        "summary_sha256": sha256_file(SUMMARY),
        "remaining_sha256": sha256_file(REMAINING),
        "ledger_rows": len(read_csv(LEDGER)),
        "remaining_rows": len(read_csv(REMAINING)),
    }


class DirectClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.trust_env = False
        self.captures: dict[str, dict] = {}

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
                f"fetch failed label={label} status={response.status_code} "
                f"bytes={len(body)} final={response.url}"
            )
        return response, BeautifulSoup(body.decode("utf-8", "replace"), "html.parser")


def parse_card(card, page_number: int) -> dict:
    uid = clean_text(card.get("data-listing-uid") or "")
    address_nodes = card.select(".address p")
    address = node_text(address_nodes[0]) if address_nodes else ""
    city_state_zip = node_text(address_nodes[1]) if len(address_nodes) > 1 else ""
    rent_text = node_text(card.select_one("li.rent"))
    options = [node_text(node) for node in card.select("ul.options > li")]
    schedule = card.select_one(".ss_btn a[href]")
    detail_url = (
        urljoin("https://showmojo.com", schedule.get("href") or "")
        if schedule
        else ""
    )
    apply = card.select_one("a.apply_btn[href]")
    apply_url = (apply.get("href") or "") if apply else ""
    full_text = node_text(card)
    bedrooms_text = node_text(card.select_one("ul.price_rooms li.br"))
    bathrooms_text = node_text(card.select_one("ul.price_rooms li.ba"))
    sqft_text = ""
    for node in card.select("ul.price_rooms > li"):
        candidate = node_text(node)
        if re.fullmatch(r"[0-9,]+\s+SF", candidate, re.IGNORECASE):
            sqft_text = candidate
            break
    return {
        "provider_listing_uid": uid,
        "provider_unit_address": address,
        "city_state_zip": city_state_zip,
        "rent_text": rent_text,
        "rent": parse_rent(rent_text),
        "availability_text": options[0] if options else "",
        "housing_type": options[2] if len(options) > 2 else "",
        "bedrooms_text": bedrooms_text,
        "bathrooms_text": bathrooms_text,
        "sqft_text": sqft_text,
        "detail_url": detail_url,
        "apply_url": apply_url,
        "application_site_id": site_id(apply_url),
        "highlights": node_text(card.select_one(".listing_highlights")),
        "mentions_park_northside": bool(
            re.search(r"\bPark\s+Northside\b", full_text, re.IGNORECASE)
        ),
        "map_page": page_number,
    }


def detail_value(soup: BeautifulSoup, selector: str) -> str:
    return node_text(soup.select_one(selector))


def parse_detail(client: DirectClient, row: dict, label_prefix: str) -> dict:
    uid = row["provider_listing_uid"]
    response, soup = client.get(f"{label_prefix}_{uid}", row["detail_url"])
    addresses = soup.select(".listing-address")
    address_text = node_text(addresses[0]) if addresses else ""
    apply = soup.select_one('a[href*="ApplyNowRHR"]')
    apply_url = (apply.get("href") or "") if apply else ""
    detail = {
        "status": int(response.status_code),
        "final_url": str(response.url),
        "title": node_text(soup.title),
        "provider_listing_uid": uid,
        "detail_address_text": address_text,
        "rent_text": detail_value(soup, ".listing-price"),
        "rent": parse_rent(detail_value(soup, ".listing-price")),
        "housing_type": detail_value(soup, ".listing-housing-type"),
        "bedrooms_text": detail_value(soup, ".listing-bedrooms"),
        "full_bathrooms_text": detail_value(soup, ".listing-full-bathrooms"),
        "half_bathrooms_text": detail_value(soup, ".listing-half-bathrooms"),
        "sqft_text": detail_value(soup, ".listing-footage"),
        "availability_text": detail_value(soup, ".listing-availability"),
        "application_site_id": site_id(apply_url),
        "mentions_park_northside": bool(
            re.search(r"\bPark\s+Northside\b", node_text(soup), re.IGNORECASE)
        ),
    }
    detail["checks"] = {
        "showmojo_host": normalized_host(detail["final_url"]) == "showmojo.com",
        "uid_path_binding": urlparse(detail["final_url"]).path.startswith(f"/l/{uid}"),
        "native_form_uid_binding": soup.select_one(f'form[action="/l/{uid}"]')
        is not None,
        "dobrin_brand_title": "dobrin property management" in detail["title"].casefold(),
        "unit_address_exact": row["provider_unit_address"].casefold()
        in address_text.casefold(),
        "city_state_zip_exact": row["city_state_zip"].casefold()
        in address_text.casefold(),
        "positive_rent_exact": detail["rent"] == row["rent"] > 0,
        "availability_exact": detail["availability_text"].casefold()
        == row["availability_text"].casefold(),
        "application_site_id_exact": detail["application_site_id"]
        == APPLICATION_SITE_ID,
    }
    return detail


def load_harness():
    spec = importlib.util.spec_from_file_location("park_northside_e2e", HARNESS)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load current full E2E harness")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def current_e2e(residual: dict, metadata: dict) -> dict:
    harness = load_harness()
    harness.fetch_mod.fetch = harness.direct_fetch
    reset_web_unlocker_call_count()
    reset_hyperbrowser_property_counts()
    repeats = []
    for repeat_index in range(1, 4):
        row = await harness.one(residual, metadata)
        row["repeat_index"] = repeat_index
        repeats.append(row)
        print(
            json.dumps(
                {
                    "repeat": repeat_index,
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
            f"forbidden backend observed hb={hb_calls} unlocker={unlocker_calls}"
        )
    counts = [int(row.get("strict_native_positive_rent_rows") or 0) for row in repeats]
    return {
        "repeats": repeats,
        "all_configured_fetch_ok": all(
            (row.get("configured_fetch") or {}).get("outcome") == "OK"
            and (row.get("configured_fetch") or {}).get("status") == 200
            for row in repeats
        ),
        "strict_native_positive_rent_counts": counts,
        "current_pipeline_strict_pass": all(value > 0 for value in counts),
        "web_unlocker_call_count": unlocker_calls,
        "hyperbrowser_call_count": hb_calls,
    }


def proposal_text(unit_count: int) -> str:
    return f"""# Park Northside / ShowMojo narrow production proposal

Status: discovery only; no repository source was edited and no ledger admission was made.

## Observed current path

`parknorthsiderva.com` identifies Park Northside at 1601 Roane St, Richmond VA
23222 and names Dobrin as manager. Dobrin's official `All Properties` page
embeds `showmojo.com/{SHOWMOJO_ACCOUNT}/listings/mapsearch`. That current
provider roster contains {unit_count} rows which pass every fail-closed boundary
below. The configured production scrape currently emits zero units, so this is
a navigation/adapter gap, not an authoritative recovery yet.

## Narrow implementation

1. Add a small public-HTML helper such as `_showmojo_public.py`; do not make
   ShowMojo a portfolio-wide generic parser.
2. Trigger it only after an official chain is proven: exact configured property
   identity -> explicit `Managed by` manager link -> same-manager listings page
   -> one published ShowMojo iframe/account.
3. Fetch only that published account's `listings/mapsearch` pages, bounded to
   five pages, ordinary direct GET, no render, proxy, unlocker, CAPTCHA solving,
   fingerprint rotation, or LLM.
4. Require every row to have one 10-hex ShowMojo UID, detail/form UID agreement,
   positive provider rent, explicit provider availability text, exact canonical
   city/state/ZIP, and the canonical property name in the row description.
5. Fail closed on any mixed account/iframe/manager chain. Never fall back to all
   same-manager rows. Deduplicate by UID.
6. Emit the full provider street address as native unit identity and UID as
   `source_ids.showmojo_listing_uid`. Preserve availability text. Leave
   `floor_plan_name` blank because ShowMojo does not publish it; do not infer a
   plan name from square footage.
7. Test three same-account controls: Graystone (wrong brand/ZIP), Lakeview
   (wrong brand/ZIP), and Thomas St (Park Northside template spill, wrong ZIP).
8. Add a configured-route E2E asserting the official hop telemetry and exactly
   {unit_count} native/priced/availability-qualified rows with zero portfolio
   contamination.

Suggested tier: `TIER_1_PUBLIC_SHOWMOJO_OFFICIAL_MANAGER_CHAIN`.
"""


async def main() -> None:
    for name, expected in EXPECTED_ENV.items():
        actual = os.environ.get(name, "").casefold()
        if actual != expected:
            raise RuntimeError(f"{name}={actual!r}; expected {expected!r}")
    RAW.mkdir(parents=True, exist_ok=True)
    source_before = source_snapshot()
    cohort_before = cohort_snapshot()
    remaining_rows = read_csv(REMAINING)
    residual = next(
        (row for row in remaining_rows if row["property_id"] == PROPERTY_ID), None
    )
    metadata = next(
        (row for row in read_csv(PROPERTIES) if row.get("apartmentid") == PROPERTY_ID),
        None,
    )
    ledger_ids = {row["property_id"] for row in read_csv(LEDGER)}
    if residual is None or metadata is None or PROPERTY_ID in ledger_ids:
        raise RuntimeError("PID 38378 is not an untouched remaining candidate")
    metadata_checks = {
        "name": metadata.get("name") == PROPERTY_NAME,
        "address": metadata.get("address") == CANONICAL_ADDRESS,
        "city": metadata.get("city") == CANONICAL_CITY,
        "state": metadata.get("state") == CANONICAL_STATE,
        "zip": metadata.get("zip") == CANONICAL_ZIP,
        "website": normalized_host(metadata.get("website") or "")
        == "parknorthsiderva.com",
    }
    if not all(metadata_checks.values()):
        raise RuntimeError(f"canonical metadata mismatch: {metadata_checks}")

    client = DirectClient()
    _, configured = client.get("configured_root", CONFIGURED_URL)
    _, floorplans = client.get("configured_floorplans", FLOORPLANS_URL)
    _, manager = client.get("manager_root", MANAGER_URL)
    _, manager_listings = client.get("manager_listings", MANAGER_LISTINGS_URL)
    configured_text = node_text(configured)
    floorplans_text = node_text(floorplans)
    configured_links = links(configured, CONFIGURED_URL)
    floorplan_links = links(floorplans, FLOORPLANS_URL)
    manager_links = links(manager, MANAGER_URL)
    listing_links = links(manager_listings, MANAGER_LISTINGS_URL)
    chain_checks = {
        "configured_brand": PROPERTY_NAME.casefold() in configured_text.casefold(),
        "configured_address": "1601 roane st" in configured_text.casefold(),
        "configured_zip": CANONICAL_ZIP in configured_text,
        "configured_names_manager": "managed by dobrin" in configured_text.casefold(),
        "configured_links_manager": any(
            normalized_host(value) == "dobrinpropertymanagement.com"
            for value in configured_links
        ),
        "floorplans_brand": PROPERTY_NAME.casefold() in floorplans_text.casefold(),
        "floorplans_address": "1601 roane st" in floorplans_text.casefold(),
        "floorplans_zip": CANONICAL_ZIP in floorplans_text,
        "floorplans_apply_site_id": any(
            site_id(value) == APPLICATION_SITE_ID for value in floorplan_links
        ),
        "manager_links_property": any(
            normalized_host(value) == "parknorthsiderva.com" for value in manager_links
        ),
        "manager_links_listings": any(
            normalized_host(value) == "dobrinpropertymanagement.com"
            and urlparse(value).path.rstrip("/") == "/richmond-va-property-listings"
            for value in manager_links
        ),
        "listings_links_property": any(
            normalized_host(value) == "parknorthsiderva.com" for value in listing_links
        ),
        "listings_embed_exact_account": any(
            normalized_host(value) == "showmojo.com"
            and urlparse(value).path.rstrip("/")
            == f"/{SHOWMOJO_ACCOUNT}/listings/mapsearch"
            for value in listing_links
        ),
    }
    if not all(chain_checks.values()):
        raise RuntimeError(f"official chain failed: {chain_checks}")

    portfolio: list[dict] = []
    roster_pages = []
    seen: set[str] = set()
    for page_number in range(1, 9):
        page_url = f"{SHOWMOJO_EMBED_URL}?page={page_number}"
        _, soup = client.get(f"showmojo_map_page_{page_number}", page_url)
        cards = soup.select("div.cnt_box[data-listing-uid]")
        roster_pages.append({"page": page_number, "url": page_url, "cards": len(cards)})
        if not cards:
            break
        for card in cards:
            row = parse_card(card, page_number)
            uid = row["provider_listing_uid"]
            if uid in seen:
                raise RuntimeError(f"duplicate UID across pages: {uid}")
            seen.add(uid)
            portfolio.append(row)
    if not roster_pages or roster_pages[-1]["cards"] != 0:
        raise RuntimeError("pagination did not reach a bounded empty page")

    for row in portfolio:
        reasons = []
        if not re.fullmatch(r"[0-9a-f]{10}", row["provider_listing_uid"]):
            reasons.append("invalid_native_uid")
        if not row["mentions_park_northside"]:
            reasons.append("canonical_property_name_absent")
        if row["city_state_zip"].casefold() != "richmond, va 23222":
            reasons.append("canonical_city_state_zip_mismatch")
        if row["rent"] <= 0:
            reasons.append("no_positive_rent")
        if not row["availability_text"]:
            reasons.append("no_explicit_provider_availability")
        if row["application_site_id"] != APPLICATION_SITE_ID:
            reasons.append("application_site_id_mismatch")
        if not urlparse(row["detail_url"]).path.startswith(
            f"/l/{row['provider_listing_uid']}/"
        ):
            reasons.append("detail_uid_path_mismatch")
        row["boundary_rejections"] = reasons
        row["provider_direct_candidate"] = not reasons

    accepted = [row for row in portfolio if row["provider_direct_candidate"]]
    if not accepted or len({row["provider_listing_uid"] for row in accepted}) != len(
        accepted
    ):
        raise RuntimeError("no unique provider-direct rows")
    units = []
    for row in accepted:
        units.append(
            {
                "provider_unit_id": row["provider_listing_uid"],
                "unit_number": row["provider_unit_address"],
                "provider_unit_address": row["provider_unit_address"],
                "city": CANONICAL_CITY,
                "state": CANONICAL_STATE,
                "zip": CANONICAL_ZIP,
                "rent": row["rent"],
                "availability_text": row["availability_text"],
                "availability_date": "",
                "availability_date_provenance": (
                    "provider publishes relative/raw text only; no date invented"
                ),
                "floor_plan_name": "",
                "floor_plan_name_provenance": (
                    "ShowMojo does not publish a plan name; no sqft inference"
                ),
                "bedrooms_text": row["bedrooms_text"],
                "bathrooms_text": row["bathrooms_text"],
                "sqft_text": row["sqft_text"],
                "source_url": row["detail_url"],
                "source_ids": {
                    "showmojo_account": SHOWMOJO_ACCOUNT,
                    "showmojo_listing_uid": row["provider_listing_uid"],
                    "rhr_application_site_id": APPLICATION_SITE_ID,
                },
            }
        )

    by_uid = {row["provider_listing_uid"]: row for row in portfolio}
    if not CONTROL_IDS.keys() <= by_uid.keys():
        raise RuntimeError(f"missing controls: {sorted(CONTROL_IDS.keys() - by_uid.keys())}")
    controls = []
    for uid, expected_control in CONTROL_IDS.items():
        row = by_uid[uid]
        if row["provider_direct_candidate"]:
            raise RuntimeError(f"control unexpectedly accepted: {uid}")
        controls.append(
            {
                "expected_control": expected_control,
                "record": row,
                "excluded": True,
            }
        )

    e2e = await current_e2e(residual, metadata)
    if not e2e["all_configured_fetch_ok"]:
        raise RuntimeError("configured E2E fetch failed")
    source_after = source_snapshot()
    cohort_after = cohort_snapshot()
    if source_before["git_head"] != source_after["git_head"]:
        raise RuntimeError("git HEAD changed; rerun required")
    if source_before["critical_file_sha256"] != source_after["critical_file_sha256"]:
        raise RuntimeError("critical source changed; rerun required")
    if cohort_before != cohort_after:
        raise RuntimeError("ledger/remaining changed; rerun required")

    excluded = [row for row in portfolio if not row["provider_direct_candidate"]]
    blank_availability = [
        row
        for row in excluded
        if row["mentions_park_northside"]
        and row["city_state_zip"].casefold() == "richmond, va 23222"
        and row["rent"] > 0
        and "no_explicit_provider_availability" in row["boundary_rejections"]
    ]
    direct_pass = bool(
        units
        and len(units) == len(accepted)
        and len({row["provider_unit_id"] for row in units}) == len(units)
        and all(row["rent"] > 0 and row["availability_text"] for row in units)
        and len(controls) == 3
        and all(control["excluded"] for control in controls)
    )
    if not direct_pass:
        raise RuntimeError("provider-direct gate failed")
    PROPOSAL.write_text(proposal_text(len(units)), encoding="utf-8")
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "lane": "park_northside_showmojo_official_manager_chain_discovery",
        "property_id": int(PROPERTY_ID),
        "property_name": PROPERTY_NAME,
        "canonical_identity": {
            "address": CANONICAL_ADDRESS,
            "city": CANONICAL_CITY,
            "state": CANONICAL_STATE,
            "zip": CANONICAL_ZIP,
            "configured_url": CONFIGURED_URL,
            "metadata_checks": metadata_checks,
        },
        "cohort": {
            "boundary": "exact_2026-07-31_FAILED_NO_DATA_344",
            "source_adapter_0731": residual.get("source_adapter_0731") or "",
            "current_detected_adapter": residual.get("current_detected_adapter") or "",
            "rp_oracle_native_unit_rows": int(
                residual.get("rp_oracle_native_unit_rows") or 0
            ),
            "state_before": cohort_before,
            "state_after": cohort_after,
            "confirmed_remaining_not_ledger": True,
        },
        "official_chain": {
            "configured_url": CONFIGURED_URL,
            "floorplans_url": FLOORPLANS_URL,
            "manager_url": MANAGER_URL,
            "manager_listings_url": MANAGER_LISTINGS_URL,
            "showmojo_embed_url": SHOWMOJO_EMBED_URL,
            "checks": chain_checks,
            "strict_chain_pass": all(chain_checks.values()),
        },
        "provider_roster": {
            "pages": roster_pages,
            "total_portfolio_rows": len(portfolio),
            "provider_direct_candidate_rows": len(accepted),
            "excluded_portfolio_rows": len(excluded),
            "excluded_matching_property_without_explicit_availability": len(
                blank_availability
            ),
            "native_listing_ids": [row["provider_listing_uid"] for row in accepted],
            "all_rows": portfolio,
        },
        "boundary_controls": controls,
        "detail_replay": {
            "performed": False,
            "reason": (
                "The officially embedded mapsearch SSR already publishes native UID, "
                "unit address, rent, availability, description, and application binding. "
                "Detail replay is proposed as an implementation gate, not claimed here."
            ),
        },
        "units": units,
        "native_identity_rows": len(units),
        "native_positive_rent_rows": len(units),
        "native_explicit_availability_rows": len(units),
        "provider_direct_strict_pass": direct_pass,
        "strict_verdict": (
            "discovery_pass_exact_official_manager_showmojo_provider_boundary_"
            "but_current_production_pipeline_gap"
        ),
        "current_full_configured_pipeline": e2e,
        "current_production_e2e_strict_pass": e2e["current_pipeline_strict_pass"],
        "authoritative_ledger_eligible": False,
        "authoritative_ledger_hold_reason": (
            "No current ShowMojo implementation; parent must independently replay "
            "and only admit after configured-route E2E emits the same bounded rows."
        ),
        "production_implementation_proposal": str(PROPOSAL),
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
                "proposal": str(PROPOSAL),
                "proposal_sha256": sha256_file(PROPOSAL),
                "provider_direct_rows": len(units),
                "provider_direct_strict_pass": direct_pass,
                "current_pipeline_counts": e2e[
                    "strict_native_positive_rent_counts"
                ],
                "authoritative_ledger_eligible": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
