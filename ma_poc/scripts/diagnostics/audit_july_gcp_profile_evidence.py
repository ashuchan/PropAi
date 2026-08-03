"""Reconcile frozen warm profiles with the archived July GCP canary evidence.

The command is read-only with respect to GCS and profile stores.  It downloads
per-property reports and optional API samples, identifies the exact historical
winning source, compares that source with every reusable profile route, and
joins the result to the live vendor-metadata route ledger.

Security boundary: output records never contain endpoint URLs or response
bodies.  Routes are represented by provider, hostname, a non-secret vendor
locator where one exists, and a SHA-256 fingerprint.  Report/API bodies are
parsed in memory and represented only by hashes and extracted property
identity.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import html
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from google.cloud import storage  # type: ignore[import-untyped]

from ma_poc.pms.property_identity import (
    MATCH,
    MISMATCH,
    UNKNOWN,
    evaluate_observed_from_csv,
    knock_observed_identity,
    sightmap_observed_identity,
)
from ma_poc.pms.source_provenance import response_sha256

_AUDIT_VERSION = "july-gcp-profile-evidence-v1"
_URL_FIELDS = (
    "winning_page_url",
    "availability_links",
    "known_endpoints",
    "widget_endpoints",
    "llm_field_mappings",
    "field_patches",
)
_IDENTITY_QUERY_KEYS = frozenset(
    {
        "communityid",
        "id",
        "p",
        "property",
        "propertyid",
        "property_id",
        "siteid",
    }
)


@dataclass(frozen=True, slots=True)
class ProfileRoute:
    property_id: str
    source: str
    url: str

    @property
    def canonical(self) -> str:
        return canonical_url(self.url)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical.encode("utf-8")).hexdigest()


def _absolute_url(value: Any, base_url: str = "") -> str:
    raw = html.unescape(str(value or "")).strip().strip("`").replace("\\/", "/")
    if not raw:
        return ""
    if raw.startswith("/") and base_url:
        return urljoin(base_url, raw)
    if "://" not in raw and "." in raw.split("/", 1)[0]:
        return "https://" + raw
    return raw


def canonical_url(value: Any) -> str:
    """Stable private comparison form; never persist this value."""

    raw = _absolute_url(value)
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw.casefold()
    host = (parsed.hostname or "").casefold().rstrip(".")
    path = re.sub(r"/+", "/", unquote(parsed.path or "/")).rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit(("https", host, path, query, ""))


def route_equivalent(left: Any, right: Any) -> bool:
    a, b = canonical_url(left), canonical_url(right)
    if not a or not b:
        return False
    if a == b:
        return True
    pa, pb = urlsplit(a), urlsplit(b)
    if (pa.hostname, pa.path) != (pb.hostname, pb.path):
        return False
    qa = {key.casefold(): value for key, value in parse_qsl(pa.query, keep_blank_values=True)}
    qb = {key.casefold(): value for key, value in parse_qsl(pb.query, keep_blank_values=True)}
    for key in _IDENTITY_QUERY_KEYS & qa.keys() & qb.keys():
        if qa[key] != qb[key]:
            return False
    return True


def provider_for_url(url: str, fallback: str = "") -> str:
    host = (urlsplit(_absolute_url(url)).hostname or "").casefold()
    value = f"{host}{urlsplit(_absolute_url(url)).path.casefold()}"
    if "sightmap.com" in host:
        return "sightmap"
    if "knockrentals.com" in host:
        return "knock"
    if "edificecms.com" in host:
        return "edifice"
    if "securecafe.com" in host or "rentcafe.com" in host:
        return "rentcafe"
    if "appfolio.com" in host:
        return "appfolio"
    if "realpage.com" in host or "realpage" in value:
        return "realpage"
    if "entrata" in host or "prospectportal.com" in host:
        return "entrata"
    if "myresman.com" in host or "resman" in value:
        return "resman"
    if "onesite" in value:
        return "onesite"
    return fallback or host or "unknown"


def safe_locator(url: str) -> str:
    raw = _absolute_url(url)
    patterns = (
        (r"sightmaps/(\d+)", "asset"),
        (r"/property/community/([a-z0-9]+)", "community"),
        (r"/property/(\d+)(?:/units)?", "property"),
        (r"[?&]property_id=([0-9a-f-]{36})", "property"),
    )
    for pattern, label in patterns:
        match = re.search(pattern, raw, re.IGNORECASE)
        if match:
            return f"{label}:{match.group(1).casefold()}"
    return ""


def safe_route_record(route: ProfileRoute, *, winner: bool, live_status: str = "") -> dict[str, Any]:
    parsed = urlsplit(route.canonical)
    return {
        "source": route.source,
        "provider": provider_for_url(route.url),
        "host": parsed.hostname or None,
        "locator": safe_locator(route.url) or None,
        "route_sha256": route.sha256,
        "historical_winner": winner,
        "live_status": live_status or None,
    }


def captured_body_for_url(
    url: str,
    report: dict[str, Any],
    sample_payload: Any,
    sample_provider: str = "",
) -> tuple[str, Any, str, str]:
    """Return raw body, parsed body, evidence source, and provider for *url*.

    API samples are preferred because their bodies are capped at 250 KB rather
    than the report's 3 KB preview.  The return value is deliberately kept in
    memory; callers persist only hashes and extracted identity.
    """

    sample_candidate: tuple[str, Any, str, str] | None = None
    if isinstance(sample_payload, list):
        for item in sample_payload:
            if not isinstance(item, dict) or not route_equivalent(item.get("url"), url):
                continue
            body_raw = str(item.get("body") or "")
            sample_candidate = (
                body_raw,
                parse_structured_body(body_raw),
                "api_sample",
                provider_for_url(str(item.get("url") or ""), sample_provider),
            )
            if sample_candidate[1] is not None:
                return sample_candidate
            break

    api_index = next(
        (index for index, item_url in report["api_urls"].items() if route_equivalent(item_url, url)),
        None,
    )
    if api_index in report["body_snippets"]:
        body_raw = report["body_snippets"][api_index]
        report_candidate = (
            body_raw,
            parse_structured_body(body_raw),
            "report_snippet",
            provider_for_url(url),
        )
        if report_candidate[1] is not None or sample_candidate is None:
            return report_candidate
    if sample_candidate is not None:
        return sample_candidate
    return "", None, "", provider_for_url(url)


def profile_routes(property_id: str, profile: dict[str, Any]) -> list[ProfileRoute]:
    navigation = profile.get("navigation") or {}
    api = profile.get("api_hints") or {}
    entry = _absolute_url(navigation.get("entry_url"))
    values: list[tuple[str, Any]] = []
    values.append(("navigation.winning_page_url", navigation.get("winning_page_url")))
    availability_path = navigation.get("availability_page_path")
    if availability_path:
        values.append(("navigation.availability_page_path", _absolute_url(availability_path, entry)))
    values.extend(
        ("navigation.availability_links", value) for value in navigation.get("availability_links") or []
    )
    values.extend(("api_hints.widget_endpoints", value) for value in api.get("widget_endpoints") or [])
    for item in api.get("known_endpoints") or []:
        if isinstance(item, dict):
            values.append(("api_hints.known_endpoints", item.get("url_pattern") or item.get("url")))
    for field in ("llm_field_mappings", "field_patches"):
        for item in api.get(field) or []:
            if isinstance(item, dict):
                values.append((f"api_hints.{field}", item.get("api_url_pattern")))

    grouped: dict[str, ProfileRoute] = {}
    for source, value in values:
        url = _absolute_url(value, entry)
        canonical = canonical_url(url)
        if not canonical:
            continue
        grouped.setdefault(canonical, ProfileRoute(property_id, source, url))
    return list(grouped.values())


def parse_report(text: str) -> dict[str, Any]:
    def match(pattern: str) -> str:
        found = re.search(pattern, text, re.MULTILINE)
        return found.group(1).strip() if found else ""

    winner = match(r"^\*\*Winning Source:\*\*\s*(.*?)\s*$").strip(" `")
    if winner.casefold() in {"n/a", "none", "unknown"}:
        winner = ""
    tier = match(r"^\*\*Extraction Tier:\*\*\s*`?([^`\n]+)")
    api_count_raw = match(r"^\*\*API responses captured:\*\*\s*(\d+)")

    api_urls: dict[int, str] = {}
    section = re.search(
        r"^### APIs Captured \(from homepage load\)\s*$([\s\S]*?)(?=^## |^### |\Z)",
        text,
        re.MULTILINE,
    )
    if section:
        for found in re.finditer(r"^(\d+)\.\s+`([^`]+)`", section.group(1), re.MULTILINE):
            api_urls[int(found.group(1))] = found.group(2).strip()

    body_snippets: dict[int, str] = {}
    for found in re.finditer(
        r"<summary>API\s+(\d+):[\s\S]*?</summary>\s*```json\s*([\s\S]*?)\s*```",
        text,
        re.MULTILINE,
    ):
        body_snippets[int(found.group(1))] = found.group(2)

    return {
        "winner": _absolute_url(winner),
        "tier": tier,
        "api_count": int(api_count_raw or 0),
        "api_urls": api_urls,
        "body_snippets": body_snippets,
    }


def parse_structured_body(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    raw = value.strip()
    if not raw:
        return None
    for parser in (json.loads, ast.literal_eval):
        try:
            return parser(raw)
        except (ValueError, SyntaxError, TypeError, json.JSONDecodeError):
            continue
    return None


def _mapping_text(value: Any, *keys: str) -> str:
    if not isinstance(value, dict):
        return ""
    lower = {str(key).casefold(): item for key, item in value.items()}
    for key in keys:
        item = lower.get(key.casefold())
        if isinstance(item, (str, int, float)) and str(item).strip():
            return str(item).strip()
    return ""


def _address_mapping(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        return {"address": value, "city": "", "state": "", "zip": ""}
    if not isinstance(value, dict):
        return {"address": "", "city": "", "state": "", "zip": ""}
    nested = value.get("address")
    if isinstance(nested, dict):
        value = nested
    return {
        "address": _mapping_text(value, "street", "streetAddress", "address1", "address_1", "line1"),
        "city": _mapping_text(value, "city", "locality"),
        "state": _mapping_text(value, "state", "region", "stateCode"),
        "zip": _mapping_text(value, "zip", "zipcode", "zipCode", "postalCode"),
    }


def html_identity_candidates(body: str) -> list[dict[str, Any]]:
    """Extract conservative property identity from an HTML route response.

    A title or heading can positively corroborate a configured name, but it is
    not strong enough to condemn a route when it differs (corporate shells and
    generic leasing titles are common).  Structured data carrying both a name
    and street address is marked strong enough for a negative decision.
    """

    if "<" not in body or ">" not in body:
        return []
    soup = BeautifulSoup(body, "html.parser")
    candidates: list[dict[str, Any]] = []

    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text(" ", strip=True)
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        queue = payload if isinstance(payload, list) else [payload]
        for item in queue:
            if not isinstance(item, dict):
                continue
            nested = item.get("@graph")
            if isinstance(nested, list):
                queue.extend(value for value in nested if isinstance(value, dict))
            name = _mapping_text(item, "name", "propertyName", "communityName")
            address = _address_mapping(item.get("address") or item.get("location") or {})
            if name:
                candidates.append(
                    {
                        "name": html.unescape(name),
                        **address,
                        "_strict_mismatch": bool(address["address"]),
                    }
                )

    street = soup.find(attrs={"itemprop": re.compile(r"streetAddress", re.I)})
    city = soup.find(attrs={"itemprop": re.compile(r"addressLocality", re.I)})
    state = soup.find(attrs={"itemprop": re.compile(r"addressRegion", re.I)})
    postal = soup.find(attrs={"itemprop": re.compile(r"postalCode", re.I)})
    itemprop_address = {
        "address": street.get_text(" ", strip=True) if street else "",
        "city": city.get_text(" ", strip=True) if city else "",
        "state": state.get_text(" ", strip=True) if state else "",
        "zip": postal.get_text(" ", strip=True) if postal else "",
    }

    names: list[str] = []
    for selector, attr in (
        ('meta[property="og:site_name"]', "content"),
        ('meta[property="og:title"]', "content"),
        ("title", ""),
        ("h1", ""),
    ):
        node = soup.select_one(selector)
        if node is None:
            continue
        value = str(node.get(attr) if attr else node.get_text(" ", strip=True) or "").strip()
        if not value:
            continue
        names.append(html.unescape(value))
        names.extend(
            segment.strip()
            for segment in re.split(r"\s+[|\u2013\u2014-]\s+", html.unescape(value))
            if segment.strip()
        )

    for name in names:
        candidates.append(
            {
                "name": name,
                **itemprop_address,
                "_strict_mismatch": bool(itemprop_address["address"]),
            }
        )

    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for item in candidates:
        key = tuple(
            str(item.get(field) or "").casefold() for field in ("name", "address", "city", "state", "zip")
        )
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:20]


def generic_identity_candidates(body: Any) -> list[dict[str, str]]:
    """Extract only mappings explicitly shaped as property/community identity."""

    candidates: list[dict[str, str]] = []

    def visit(value: Any, path: tuple[str, ...], depth: int) -> None:
        if depth > 5:
            return
        if isinstance(value, list):
            for item in value[:20]:
                visit(item, path + ("[]",), depth + 1)
            return
        if not isinstance(value, dict):
            return

        strong_name = _mapping_text(
            value,
            "propertyName",
            "property_name",
            "communityName",
            "community_name",
            "siteName",
            "site_name",
            "assetName",
            "asset_name",
        )
        path_hint = any(
            token in {"property", "community", "asset", "site", "location", "response"} for token in path[-2:]
        )
        name = strong_name or (_mapping_text(value, "name", "title") if path_hint else "")
        location = value.get("location") or value.get("address") or value
        address = _address_mapping(location)
        if name and (strong_name or path_hint):
            candidates.append({"name": name, **address})

        for key, item in value.items():
            if isinstance(item, (dict, list)):
                visit(item, path + (str(key).casefold(),), depth + 1)

    visit(body, (), 0)
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    for item in candidates:
        key = tuple(item[field].casefold() for field in ("name", "address", "city", "state", "zip"))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:20]


def observed_candidates(provider: str, body: Any) -> list[dict[str, str]]:
    if isinstance(body, str):
        return html_identity_candidates(body)
    if provider == "sightmap":
        return [sightmap_observed_identity(body)]
    if provider == "knock":
        return [knock_observed_identity(body)]
    if provider == "edifice" and isinstance(body, dict):
        value = body.get("property")
        if isinstance(value, dict):
            return [{"name": _mapping_text(value, "name", "title"), **_address_mapping(value)}]
        return [{"name": str(value or "").strip(), "address": "", "city": "", "state": "", "zip": ""}]
    if provider == "onesite" and isinstance(body, dict):
        response = body.get("response") if isinstance(body.get("response"), dict) else body
        return [{"name": _mapping_text(response, "name", "propertyName"), **_address_mapping(response)}]
    return generic_identity_candidates(body)


def decide_candidates(configured: dict[str, str], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = [evaluate_observed_from_csv(configured, candidate) for candidate in candidates]
    matches = [decision for decision in decisions if decision.status == MATCH]
    if matches:
        return matches[0].to_dict()
    informative = [
        decision
        for candidate, decision in zip(candidates, decisions, strict=True)
        if decision.status != UNKNOWN and candidate.get("_strict_mismatch", True)
    ]
    # Multiple property-shaped mismatches indicate a portfolio response; do
    # not call one of them the configured property's definitive identity.
    if len(informative) == 1:
        return informative[0].to_dict()
    return {
        "status": UNKNOWN,
        "evidence": ["no_property_identity" if not candidates else "multiple_or_ambiguous_identities"],
        "configured_name": configured.get("name") or None,
        "observed_name": None,
        "configured_address": configured.get("address") or None,
        "observed_address": None,
    }


def load_live_ledger(path: Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = (str(row.get("property_id")), str(row.get("provider")), str(row.get("route_locator")))
        result[key] = row
    return result


def live_status_for(property_id: str, url: str, live: dict[tuple[str, str, str], dict[str, Any]]) -> str:
    provider = provider_for_url(url)
    locator = safe_locator(url)
    row = live.get((property_id, provider, locator))
    return str((row or {}).get("decision", {}).get("status") or "")


def identity_for_url(
    property_id: str,
    url: str,
    configured: dict[str, dict[str, str]],
    live: dict[tuple[str, str, str], dict[str, Any]],
    report: dict[str, Any],
    sample_payload: Any,
    sample_provider: str,
) -> tuple[dict[str, Any], str, str, Any]:
    body_raw, body_value, body_source, body_provider = captured_body_for_url(
        url, report, sample_payload, sample_provider
    )
    candidates = observed_candidates(body_provider, body_value) if body_value is not None else []
    identity = decide_candidates(configured[property_id], candidates)
    live_status = live_status_for(property_id, url, live)
    if live_status in {MATCH, MISMATCH}:
        live_row = live.get((property_id, provider_for_url(url), safe_locator(url))) or {}
        identity = (live_row.get("decision") or identity).copy()
        identity["evidence_source"] = "live_route_metadata"
    elif body_source:
        identity["evidence_source"] = body_source
    else:
        identity["evidence_source"] = "none"
    return identity, body_source, body_raw, body_value


def cohort_rows(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return {
        str(row.get("apartmentid") or row.get("apartment_id") or row.get("property_id")): row for row in rows
    }


def _parse_gcs_prefix(uri: str) -> tuple[str, str]:
    parsed = urlsplit(uri)
    if parsed.scheme != "gs" or not parsed.netloc:
        raise ValueError(f"not_gcs_uri:{uri}")
    return parsed.netloc, parsed.path.lstrip("/").rstrip("/") + "/"


def run(args: argparse.Namespace) -> dict[str, Any]:
    profiles = {path.stem: path for path in args.profiles_dir.glob("*.json")}
    configured = cohort_rows(args.cohort_csv)
    live = load_live_ledger(args.live_route_ledger)
    bucket_name, prefix = _parse_gcs_prefix(args.gcs_prefix)
    client = storage.Client()

    report_blobs: dict[str, Any] = {}
    raw_html_ids: set[str] = set()
    api_sample_blobs: dict[str, Any] = {}
    for blob in client.list_blobs(bucket_name, prefix=prefix):
        name = blob.name
        property_id = name.rsplit("/", 1)[-1].split(".", 1)[0]
        if property_id not in profiles:
            continue
        if "/property_reports/" in name and name.endswith(".md"):
            report_blobs[property_id] = blob
        elif "/raw_html/" in name and name.endswith(".html.gz"):
            raw_html_ids.add(property_id)
        elif "/api_samples/" in name and name.endswith(".json"):
            api_sample_blobs[property_id] = blob

    missing_reports = sorted(set(profiles) - set(report_blobs), key=int)
    if missing_reports:
        raise RuntimeError(f"missing_property_reports:{len(missing_reports)}")

    def read_report(item: tuple[str, Any]) -> tuple[str, bytes, dict[str, Any], int]:
        property_id, blob = item
        raw = blob.download_as_bytes()
        return (
            property_id,
            raw,
            parse_report(raw.decode("utf-8", errors="replace")),
            int(blob.generation or 0),
        )

    def read_sample(item: tuple[str, Any]) -> tuple[str, bytes, Any, int, str]:
        property_id, blob = item
        raw = blob.download_as_bytes()
        provider_match = re.search(r"/api_samples/([^/]+)/", blob.name)
        provider = provider_match.group(1) if provider_match else "unknown"
        return property_id, raw, json.loads(raw), int(blob.generation or 0), provider

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        report_data = {
            property_id: (raw, parsed, generation)
            for property_id, raw, parsed, generation in executor.map(read_report, report_blobs.items())
        }
        sample_data = {
            property_id: (raw, payload, generation, provider)
            for property_id, raw, payload, generation, provider in executor.map(
                read_sample, api_sample_blobs.items()
            )
        }

    records: list[dict[str, Any]] = []
    for property_id in sorted(profiles, key=int):
        profile = json.loads(profiles[property_id].read_text(encoding="utf-8"))
        routes = profile_routes(property_id, profile)
        report_raw, report, report_generation = report_data[property_id]
        winner = report["winner"]
        winner_matches = [route for route in routes if winner and route_equivalent(route.url, winner)]
        sample_payload: Any = None
        sample_provider = ""
        sample_generation = 0
        if property_id in sample_data:
            _sample_raw, sample_payload, sample_generation, sample_provider = sample_data[property_id]

        winner_api_index = next(
            (index for index, url in report["api_urls"].items() if winner and route_equivalent(url, winner)),
            None,
        )

        route_records: list[dict[str, Any]] = []
        for route in routes:
            live_status = live_status_for(property_id, route.url, live)
            route_identity, route_body_source, route_body_raw, route_body_value = identity_for_url(
                property_id,
                route.url,
                configured,
                live,
                report,
                sample_payload,
                sample_provider,
            )
            route_record = safe_route_record(
                route,
                winner=any(route_equivalent(route.url, item.url) for item in winner_matches),
                live_status=live_status,
            )
            route_record.update(
                {
                    "identity": route_identity,
                    "body_source": route_body_source or None,
                    "body_sha256": response_sha256(route_body_raw) if route_body_source else None,
                    "body_parseable": route_body_value is not None,
                }
            )
            route_records.append(route_record)

        body_source = ""
        body_raw = ""
        body_value: Any = None
        if winner:
            identity, body_source, body_raw, body_value = identity_for_url(
                property_id,
                winner,
                configured,
                live,
                report,
                sample_payload,
                sample_provider,
            )
        else:
            identity = decide_candidates(configured[property_id], [])
            identity["evidence_source"] = "none"

        records.append(
            {
                "audit_version": _AUDIT_VERSION,
                "property_id": property_id,
                "configured": {
                    "name": configured[property_id].get("name") or "",
                    "address": configured[property_id].get("address") or "",
                    "city": configured[property_id].get("city") or "",
                    "state": configured[property_id].get("state") or "",
                    "zip": configured[property_id].get("zip") or "",
                },
                "historical": {
                    "tier": report["tier"] or None,
                    "winner_present": bool(winner),
                    "winner_provider": provider_for_url(winner) if winner else None,
                    "winner_host": (urlsplit(canonical_url(winner)).hostname or None) if winner else None,
                    "winner_locator": safe_locator(winner) or None,
                    "winner_sha256": hashlib.sha256(canonical_url(winner).encode("utf-8")).hexdigest()
                    if winner
                    else None,
                    "winner_in_profile": bool(winner_matches),
                    "captured_api_count": report["api_count"],
                    "winner_api_index": winner_api_index,
                },
                "identity": identity,
                "profile_routes": route_records,
                "artifacts": {
                    "report_generation": report_generation,
                    "report_sha256": response_sha256(report_raw),
                    "raw_html_present": property_id in raw_html_ids,
                    "api_sample_present": property_id in sample_data,
                    "api_sample_generation": sample_generation or None,
                    "winner_body_source": body_source or None,
                    "winner_body_sha256": response_sha256(body_raw) if body_source else None,
                    "winner_body_parseable": body_value is not None,
                },
            }
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ledger = args.output_dir / "archive-evidence-ledger.jsonl"
    ledger.write_text(
        "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )

    identity_counts = Counter(str(record["identity"]["status"]) for record in records)
    tier_counts = Counter(str(record["historical"]["tier"] or "UNKNOWN") for record in records)
    summary = {
        "audit_version": _AUDIT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "profiles": len(records),
            "gcs_prefix": args.gcs_prefix,
            "reports": len(report_data),
            "raw_html": len(raw_html_ids),
            "api_samples": len(sample_data),
        },
        "historical": {
            "winning_source_present": sum(record["historical"]["winner_present"] for record in records),
            "winner_in_profile": sum(record["historical"]["winner_in_profile"] for record in records),
            "winner_body_available": sum(
                bool(record["artifacts"]["winner_body_source"]) for record in records
            ),
            "winner_body_parseable": sum(record["artifacts"]["winner_body_parseable"] for record in records),
        },
        "identity_status_counts": dict(sorted(identity_counts.items())),
        "tier_counts": dict(sorted(tier_counts.items())),
        "live_route_status_counts": dict(
            sorted(
                Counter(
                    route["live_status"]
                    for record in records
                    for route in record["profile_routes"]
                    if route["live_status"]
                ).items()
            )
        ),
        "profile_route_identity_status_counts": dict(
            sorted(
                Counter(
                    route["identity"]["status"] for record in records for route in record["profile_routes"]
                ).items()
            )
        ),
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles-dir", type=Path, required=True)
    parser.add_argument("--cohort-csv", type=Path, required=True)
    parser.add_argument("--live-route-ledger", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--gcs-prefix", required=True)
    parser.add_argument("--workers", type=int, default=24)
    return parser.parse_args()


def main() -> None:
    summary = run(parse_args())
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
