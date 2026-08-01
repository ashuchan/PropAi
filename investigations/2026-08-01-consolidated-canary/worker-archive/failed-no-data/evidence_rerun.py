from __future__ import annotations

import asyncio
import csv
import gzip
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import ma_poc.pms.adapters  # noqa: F401
from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._parsing import contains_street_address
from ma_poc.pms.adapters.appfolio import (
    _extract_zip as _appfolio_extract_zip,
    _normalize_street as _appfolio_normalize_street,
    _street_candidates as _appfolio_street_candidates,
)


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LEDGER_PATH = ROOT / "strict99_authoritative_ledger.csv"
LEDGER_LEVEL = os.environ.get("LEDGER_LEVEL", "").strip()
EVIDENCE_LANE = os.environ.get("EVIDENCE_LANE", "").strip()
SOURCE_ADAPTER = os.environ.get("SOURCE_ADAPTER", "").strip()
PROPERTY_IDS = {
    int(item) for item in os.environ.get("PROPERTY_IDS", "").split(",") if item.strip()
}
BATCH_LABEL = os.environ.get("BATCH_LABEL", "evidence-rerun").strip()
OUTPUT_PATH = Path(os.environ["OUTPUT_PATH"])
CONCURRENCY = int(os.environ.get("AUDIT_CONCURRENCY", "5"))
TIMEOUT_SECONDS = int(os.environ.get("AUDIT_TIMEOUT_SECONDS", "140"))
PROPERTIES_PATH = Path("ma_poc/config/properties.csv")

_IDENTITY_KEYS = (
    "unit_number",
    "unit_id",
    "unitid",
    "native_unit_id",
    "source_unit_id",
    "_source_native_id",
    "source_id",
    "floor_plan_id",
    "apartmentid",
)
_ADDRESS_KEYS = ("address", "street_address", "property_address")


def canonical_metadata() -> dict[int, dict[str, str]]:
    metadata: dict[int, dict[str, str]] = {}
    with PROPERTIES_PATH.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                property_id = int(row.get("apartmentid") or "")
            except ValueError:
                continue
            metadata[property_id] = row
    return metadata


def with_canonical_metadata(record: dict, metadata: dict[int, dict[str, str]]) -> dict:
    """Repair reconstruction-only blank fields without changing cohort URL."""
    property_id = int(record["property_id"])
    canonical = metadata.get(property_id, {})
    merged = dict(record)
    merged["proj_name"] = str(record.get("proj_name") or canonical.get("name") or "")
    merged["address"] = str(record.get("address") or canonical.get("address") or "")
    merged["city"] = str(record.get("city") or canonical.get("city") or "")
    merged["state"] = str(record.get("state") or canonical.get("state") or "")
    merged["zip_code"] = str(record.get("zip_code") or canonical.get("zip") or "")
    merged["_canonical_metadata_joined"] = any(
        not str(record.get(key) or "").strip() and bool(merged.get(key))
        for key in ("proj_name", "address", "city", "state", "zip_code")
    )
    return merged


def fetch_for(record: dict) -> FetchResult | None:
    property_id = str(record["property_id"])
    path = ROOT / "raw_all" / f"{property_id}.html.gz"
    if not path.exists():
        return None
    body = gzip.open(path, "rb").read()
    url = str(record.get("website") or "")
    return FetchResult(
        url=url,
        outcome=FetchOutcome.OK,
        status=200,
        body=body,
        headers={},
        render_mode=RenderMode.RENDER,
        final_url=url,
        attempts=1,
        elapsed_ms=0,
    )


def _norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _property_name_key(value: object) -> str:
    tokens = re.findall(r"[a-z0-9]+", str(value or "").casefold().replace("'", ""))
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    generic = {
        "apartment",
        "apartments",
        "home",
        "homes",
        "townhome",
        "townhomes",
        "community",
    }
    return "".join(token for token in tokens if token not in generic)


def _identity_sample(row: dict) -> dict:
    identities = {
        key: str(row.get(key))
        for key in _IDENTITY_KEYS
        if row.get(key) not in (None, "")
    }
    return {
        "identity": identities,
        "source_ids": (
            dict(row.get("source_ids") or {})
            if isinstance(row.get("source_ids"), dict)
            else {}
        ),
        "source_api_url": str(row.get("source_api_url") or ""),
        "floor_plan_name": str(row.get("floor_plan_name") or ""),
        "unit_name": str(row.get("unit_name") or ""),
        "availability_date": str(row.get("availability_date") or ""),
        "source_property_id": str(row.get("source_property_id") or ""),
        "source_property_name": str(row.get("source_property_name") or ""),
        "source_property_provenance": str(row.get("source_property_provenance") or ""),
        "source_portal_url": str(row.get("source_portal_url") or ""),
        "positive_rent_evidence": {
            key: row.get(key)
            for key in (
                "market_rent_low",
                "market_rent_high",
                "rent_low",
                "rent_high",
                "asking_rent",
                "rent",
            )
            if isinstance(row.get(key), (int, float))
            and not isinstance(row.get(key), bool)
            and row.get(key) > 0
        },
    }


def _has_positive_rent(row: dict) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and row.get(key) > 0
        for key in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "asking_rent",
            "rent",
        )
    )


def _identity_verdict(
    record: dict,
    units: list[dict],
    archived_html: str,
) -> tuple[str, dict]:
    website_host = (urlparse(str(record.get("website") or "")).hostname or "").lower()
    source_urls = sorted(
        {
            str(row.get("source_api_url") or "").strip()
            for row in units
            if str(row.get("source_api_url") or "").strip()
        }
    )
    source_hosts = sorted(
        {(urlparse(url).hostname or "").lower() for url in source_urls if url}
    )
    native_rows = [row for row in units if unit_has_real_anchor(row)]
    qualified_rows = [row for row in native_rows if _has_positive_rent(row)]
    unit_numbers = {
        str(row.get("unit_number") or "").strip()
        for row in units
        if str(row.get("unit_number") or "").strip()
    }
    record_addr = _norm(record.get("address"))
    row_addresses = {
        str(row.get(key) or "").strip()
        for row in units
        for key in _ADDRESS_KEYS
        if str(row.get(key) or "").strip()
    }
    row_addresses.update(
        str(row.get("unit_name") or "").strip()
        for row in units
        if contains_street_address(str(row.get("unit_name") or ""))
    )
    normalized_addresses = {_norm(value) for value in row_addresses if _norm(value)}
    loose_address_matches = bool(
        record_addr
        and any(
            record_addr in value or value in record_addr
            for value in normalized_addresses
        )
    )
    target_street = _appfolio_normalize_street(str(record.get("address") or ""))
    target_zip = _appfolio_extract_zip(str(record.get("zip_code") or ""))
    exact_street_matches = {
        value
        for value in row_addresses
        if target_street
        and any(
            _appfolio_normalize_street(candidate) == target_street
            for candidate in _appfolio_street_candidates(value)
        )
        and (
            not target_zip
            or _appfolio_extract_zip(value, prefer_last=True) == target_zip
        )
    }
    address_matches = loose_address_matches or bool(exact_street_matches)
    address_conflict = bool(
        record_addr
        and normalized_addresses
        and not address_matches
        and len(normalized_addresses) > 1
    )
    same_host = bool(
        website_host
        and any(
            host == website_host
            or host.endswith("." + website_host)
            or website_host.endswith("." + host)
            for host in source_hosts
            if host
        )
    )
    stopwords = {
        "apartment",
        "apartments",
        "at",
        "community",
        "homes",
        "ii",
        "iii",
        "on",
        "the",
        "village",
    }
    property_tokens = [
        token
        for token in re.findall(
            r"[a-z0-9]+", str(record.get("proj_name") or "").lower()
        )
        if len(token) >= 3 and token not in stopwords
    ]
    source_identity = _norm(" ".join([*source_hosts, *source_urls]))
    property_name_consistent = bool(
        property_tokens
        and all(_norm(token) in source_identity for token in property_tokens)
    )
    normalized_archived = (archived_html or "").replace("\\/", "/").lower()
    securecafe_hosts = [
        host for host in source_hosts if host.endswith("securecafe.com")
    ]
    securecafe_exact_base_harvested = bool(
        securecafe_hosts
        and all(host in normalized_archived for host in securecafe_hosts)
    )
    securecafe_property_match = bool(
        securecafe_exact_base_harvested and property_name_consistent
    )
    applicant_hosts = [
        host for host in source_hosts if host.endswith(".securecafeapplicant.com")
    ]
    applicant_subdomains = {
        host.removesuffix(".securecafeapplicant.com") for host in applicant_hosts
    }
    applicant_query_ids = {
        value
        for url in source_urls
        for value in parse_qs(urlparse(url).query).get("propertyId", [])
    }
    row_property_ids = {
        str(row.get("source_property_id") or "").strip()
        for row in units
        if str(row.get("source_property_id") or "").strip()
    }
    row_property_names = {
        _property_name_key(row.get("source_property_name"))
        for row in units
        if _property_name_key(row.get("source_property_name"))
    }
    onesite_api_ids = {
        match.group(1)
        for url in source_urls
        if (
            match := re.search(
                r"/workflowstartup/v1/(\d+)/English(?:[/?]|$)",
                url,
                re.IGNORECASE,
            )
        )
    }
    onesite_provenance = {
        str(row.get("source_property_provenance") or "").strip()
        for row in units
        if str(row.get("source_property_provenance") or "").strip()
    }
    onesite_portal_urls = {
        str(row.get("source_portal_url") or "").strip()
        for row in units
        if str(row.get("source_portal_url") or "").strip()
    }
    onesite_portal_hosts = {
        (urlparse(url).hostname or "").lower() for url in onesite_portal_urls
    }
    archived_onesite_portal_hosts = set(
        re.findall(
            r"https?://([\w-]+\.onlineleasing\.realpage\.com)",
            normalized_archived,
            re.IGNORECASE,
        )
    )
    onesite_published_portal_proof = bool(
        onesite_provenance == {"published_portal_shell"}
        and len(onesite_portal_hosts) == 1
        and onesite_portal_hosts == archived_onesite_portal_hosts
    )
    onesite_published_id_proof = bool(
        onesite_provenance == {"marketing_page_site_id"}
        and onesite_api_ids
        and all(
            re.search(
                rf"(?:widgetloader\.js\?siteid=|siteid=){re.escape(native_id)}\b",
                normalized_archived,
                re.IGNORECASE,
            )
            for native_id in onesite_api_ids
        )
    )
    title_match = re.search(
        r"<title[^>]*>(.*?)</title>", archived_html or "", re.IGNORECASE | re.DOTALL
    )
    archived_title = (
        re.sub(r"<[^>]+>", " ", title_match.group(1)) if title_match else ""
    )
    target_property_name_key = _property_name_key(record.get("proj_name"))
    target_property_name_variants = {
        target_property_name_key,
        target_property_name_key.replace("boulevard", "blvd"),
    }
    target_property_name_variants.discard("")
    marketing_identity_key = _property_name_key(
        " ".join(
            [
                str(record.get("website") or ""),
                website_host,
                archived_title,
            ]
        )
    )
    onesite_target_tokens = {
        token
        for token in re.findall(
            r"[a-z0-9]+", str(record.get("proj_name") or "").casefold()
        )
        if len(token) >= 2
        and token
        not in {
            "apartment",
            "apartments",
            "at",
            "community",
            "homes",
            "station",
            "the",
        }
    }
    marketing_identity_raw = _norm(
        " ".join(
            [
                str(record.get("website") or ""),
                website_host,
                archived_title,
            ]
        )
    )
    onesite_relaxed_name_match = bool(
        len(onesite_target_tokens) >= 2
        and all(
            _norm(token) in marketing_identity_raw for token in onesite_target_tokens
        )
    )
    onesite_marketing_property_match = (
        bool(
            target_property_name_variants
            and any(
                variant in marketing_identity_key
                for variant in target_property_name_variants
            )
        )
        or onesite_relaxed_name_match
    )
    onesite_property_match = bool(
        source_hosts == ["leasing.realpage.com"]
        and len(onesite_api_ids) == 1
        and onesite_api_ids == row_property_ids
        and (onesite_published_portal_proof or onesite_published_id_proof)
        and onesite_marketing_property_match
    )
    applicant_tenant_link_harvested = bool(
        applicant_subdomains
        and all(
            re.search(
                rf"https?://{re.escape(sub)}\.securecafe(?:net)?\.com/"
                r"(?:onlineleasing|residentservices)/",
                normalized_archived,
            )
            for sub in applicant_subdomains
        )
    )
    applicant_provenance = {
        str(row.get("source_property_provenance") or "").strip()
        for row in units
        if str(row.get("source_property_provenance") or "").strip()
    }
    applicant_portal_urls = {
        str(row.get("source_portal_url") or "").strip()
        for row in units
        if str(row.get("source_portal_url") or "").strip()
    }
    applicant_portal_hosts = {
        (urlparse(url).hostname or "").lower() for url in applicant_portal_urls
    }
    applicant_shell_exact = bool(
        re.sub(r"\s+", " ", archived_title).strip().casefold()
        == "applicant portal | rentcafe"
        and re.search(r"/applicant/(?:js|chunks)/", normalized_archived)
    )
    vanity_host = website_host.removeprefix("www.")
    vanity_labels = vanity_host.split(".")
    applicant_vanity_subdomain_match = bool(
        len(vanity_labels) == 2
        and all(vanity_labels)
        and applicant_subdomains == {vanity_labels[0]}
    )
    applicant_vanity_shell_proof = bool(
        applicant_shell_exact
        and applicant_vanity_subdomain_match
        and applicant_provenance == {"vanity_applicant_shell"}
        and applicant_portal_hosts == {website_host}
    )
    applicant_base_harvested = bool(
        applicant_subdomains
        and (
            applicant_tenant_link_harvested
            or applicant_vanity_shell_proof
            or all(
                f"{sub}.securecafe.com/onlineleasing/" in normalized_archived
                for sub in applicant_subdomains
            )
            or (
                all(host in normalized_archived for host in applicant_hosts)
                and applicant_query_ids
                and all(
                    f"myolepropertyid={native_id}" in normalized_archived
                    or f"propertyid={native_id}" in normalized_archived
                    for native_id in applicant_query_ids
                )
            )
        )
    )
    applicant_property_match = bool(
        applicant_base_harvested
        and len(applicant_query_ids) == 1
        and applicant_query_ids == row_property_ids
        and row_property_names == {_property_name_key(record.get("proj_name"))}
    )
    spherexx_provenance = {
        str(row.get("source_property_provenance") or "").strip()
        for row in units
        if str(row.get("source_property_provenance") or "").strip()
    }
    spherexx_portal_urls = {
        str(row.get("source_portal_url") or "").strip()
        for row in units
        if str(row.get("source_portal_url") or "").strip()
    }
    spherexx_portal_hosts = {
        (urlparse(url).hostname or "").lower() for url in spherexx_portal_urls
    }
    spherexx_native_rows = [
        row
        for row in units
        if isinstance(row.get("source_ids"), dict)
        and str(row["source_ids"].get("spherexx_unit_id") or "").strip()
    ]
    spherexx_published_portal_proof = bool(
        source_hosts == ["clients.spherexx.com"]
        and spherexx_provenance == {"published_spherexx_availability_iframe"}
        and len(spherexx_portal_urls) == 1
        and all(url.casefold() in normalized_archived for url in spherexx_portal_urls)
        and all(
            host == website_host
            or host.removeprefix("www.") == website_host.removeprefix("www.")
            for host in spherexx_portal_hosts
        )
    )
    spherexx_property_match = bool(
        spherexx_published_portal_proof
        and len(spherexx_native_rows) == len(units)
        and row_property_names == {_property_name_key(record.get("proj_name"))}
    )
    evidence = {
        "website_host": website_host,
        "source_hosts": source_hosts,
        "source_urls": source_urls[:5],
        "rows_with_native_identity": len(native_rows),
        "rows_with_native_identity_and_positive_rent": len(qualified_rows),
        "distinct_unit_numbers": len(unit_numbers),
        "row_addresses": sorted(row_addresses)[:10],
        "same_host": same_host,
        "address_matches": address_matches,
        "exact_street_matches": sorted(exact_street_matches)[:10],
        "address_conflict": address_conflict,
        "property_name_consistent": property_name_consistent,
        "securecafe_exact_base_harvested": securecafe_exact_base_harvested,
        "securecafe_property_match": securecafe_property_match,
        "applicant_base_harvested": applicant_base_harvested,
        "applicant_tenant_link_harvested": applicant_tenant_link_harvested,
        "applicant_vanity_shell_proof": applicant_vanity_shell_proof,
        "applicant_vanity_subdomain_match": applicant_vanity_subdomain_match,
        "applicant_provenance": sorted(applicant_provenance),
        "applicant_portal_hosts": sorted(applicant_portal_hosts),
        "applicant_property_match": applicant_property_match,
        "applicant_query_ids": sorted(applicant_query_ids),
        "row_property_ids": sorted(row_property_ids),
        "row_property_names": sorted(row_property_names),
        "onesite_api_ids": sorted(onesite_api_ids),
        "onesite_provenance": sorted(onesite_provenance),
        "onesite_portal_hosts": sorted(onesite_portal_hosts),
        "onesite_published_portal_proof": onesite_published_portal_proof,
        "onesite_published_id_proof": onesite_published_id_proof,
        "onesite_marketing_property_match": onesite_marketing_property_match,
        "onesite_relaxed_name_match": onesite_relaxed_name_match,
        "onesite_property_match": onesite_property_match,
        "spherexx_provenance": sorted(spherexx_provenance),
        "spherexx_portal_hosts": sorted(spherexx_portal_hosts),
        "spherexx_native_identity_rows": len(spherexx_native_rows),
        "spherexx_published_portal_proof": spherexx_published_portal_proof,
        "spherexx_property_match": spherexx_property_match,
    }
    if not units:
        verdict = "no_native_units"
    elif address_conflict:
        verdict = "contaminated_address_conflict"
    elif not native_rows:
        verdict = "unverified_no_real_native_anchor"
    elif not qualified_rows:
        verdict = "unverified_no_positive_rent"
    elif (
        same_host
        or address_matches
        or securecafe_property_match
        or applicant_property_match
        or onesite_property_match
        or spherexx_property_match
    ):
        verdict = "pass_property_scoped_native_identity"
    else:
        verdict = "unverified_property_boundary"
    return verdict, evidence


async def run_one(record: dict, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        property_id = str(record["property_id"])
        fetch_result = fetch_for(record)
        if fetch_result is None:
            return {"property_id": int(property_id), "outcome": "NO_ARCHIVED_BODY"}
        profile_path = ROOT / "profiles" / f"{property_id}.json"
        try:
            profile = (
                ScrapeProfile.model_validate_json(profile_path.read_text())
                if profile_path.exists()
                else None
            )
        except Exception:
            profile = None
        csv_row = {
            "apartmentid": property_id,
            "name": record.get("proj_name") or "",
            "address": record.get("address") or "",
            "city": record.get("city") or "",
            "state": record.get("state") or "",
            "zip": record.get("zip_code") or "",
            "website": record.get("website") or "",
        }
        budget = {
            "llm_api_calls": 0,
            "llm_dom_calls": 0,
            "llm_monolithic": 0,
            "link_hop": 0,
            "_cost_cap_usd": 0,
        }
        try:
            result = await asyncio.wait_for(
                scraper_mod.scrape(
                    record.get("website") or "",
                    profile=profile,
                    page=None,
                    fetch_result=fetch_result,
                    csv_row=csv_row,
                    property_id=property_id,
                    shared_budget=budget,
                ),
                timeout=TIMEOUT_SECONDS,
            )
        except Exception as exc:
            return {
                "property_id": int(property_id),
                "property_name": str(record.get("proj_name") or ""),
                "outcome": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
            }
        units = list(result.get("units") or [])
        plans = list(result.get("plan_summaries") or [])
        archived_html = (
            fetch_result.body.decode("utf-8", "replace")
            if isinstance(fetch_result.body, bytes)
            else str(fetch_result.body or "")
        )
        verdict, identity_evidence = _identity_verdict(
            record,
            units,
            archived_html,
        )
        raw_outcome = "UNITS" if units else "PLANS" if plans else "EMPTY"
        qualified = bool(units and verdict.startswith("pass_"))
        return {
            "property_id": int(property_id),
            "property_name": str(record.get("proj_name") or ""),
            "website": str(record.get("website") or ""),
            "batch_label": BATCH_LABEL,
            "canonical_metadata_joined": bool(record.get("_canonical_metadata_joined")),
            "raw_extractor_outcome": raw_outcome,
            "outcome": (
                "UNIT_QUALIFIED"
                if qualified
                else "UNIT_UNVERIFIED"
                if units
                else "PLAN_ONLY"
                if plans
                else "EMPTY"
            ),
            "adapter": result.get("_adapter_used"),
            "tier": result.get("extraction_tier_used"),
            "units": len(units),
            "plans": len(plans),
            "identity_samples": [_identity_sample(row) for row in units[:2]],
            "identity_evidence": identity_evidence,
            "property_identity_match": verdict.startswith("pass_"),
            "contamination_verdict": verdict,
            "sample_plan_names": sorted(
                {
                    str(row.get("floor_plan_name") or "").strip()
                    for row in [*units, *plans]
                    if str(row.get("floor_plan_name") or "").strip()
                }
            )[:8],
            "errors": list(result.get("errors") or [])[-5:],
        }


async def main() -> None:
    records = json.loads((ROOT / "failed344.json").read_text())
    metadata = canonical_metadata()
    by_id = {
        int(row["property_id"]): with_canonical_metadata(row, metadata)
        for row in records
    }
    selected = []
    if PROPERTY_IDS:
        selected = [by_id[property_id] for property_id in sorted(PROPERTY_IDS)]
    else:
        with LEDGER_PATH.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if LEDGER_LEVEL and row["recovery_level"] != LEDGER_LEVEL:
                    continue
                if EVIDENCE_LANE and row["evidence_lane"] != EVIDENCE_LANE:
                    continue
                if (
                    SOURCE_ADAPTER
                    and (row["source_adapter_0731"] or "") != SOURCE_ADAPTER
                ):
                    continue
                selected.append(by_id[int(row["property_id"])])
    print(
        json.dumps(
            {
                "batch_label": BATCH_LABEL,
                "candidate_count": len(selected),
                "property_ids": [int(row["property_id"]) for row in selected],
            }
        ),
        flush=True,
    )
    semaphore = asyncio.Semaphore(CONCURRENCY)
    results = []
    tasks = [asyncio.create_task(run_one(row, semaphore)) for row in selected]
    for task in asyncio.as_completed(tasks):
        row = await task
        results.append(row)
        print(json.dumps(row), flush=True)
    results.sort(key=lambda row: row["property_id"])
    OUTPUT_PATH.write_text(
        json.dumps(
            {
                "batch_label": BATCH_LABEL,
                "filters": {
                    "ledger_level": LEDGER_LEVEL,
                    "evidence_lane": EVIDENCE_LANE,
                    "source_adapter": SOURCE_ADAPTER,
                    "property_ids": sorted(PROPERTY_IDS),
                },
                "results": results,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "summary": dict(
                    __import__("collections").Counter(
                        row.get("outcome") for row in results
                    )
                ),
                "output_path": str(OUTPUT_PATH),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
