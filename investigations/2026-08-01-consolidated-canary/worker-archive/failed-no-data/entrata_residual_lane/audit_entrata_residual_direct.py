#!/usr/bin/env python3
"""Current direct/static audit of the strict Entrata residual cohort.

The audit is property-scoped and fail-closed.  Candidates must be exact
``/conventional/`` routes published in the archived property body or already
accepted by a prior one-session strict sweep.  Every inventory request stays
on the accepted property origin.  No Hyperbrowser session is created here.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters.entrata import (
    _PP_CONVENTIONAL_RE,
    _extract_vus_urls,
    _find_pp_conventional_index,
    find_entrata_pp_plan_links,
    parse_entrata_modern_units_data,
    parse_entrata_pp_jd_fp_cards,
    parse_entrata_pp_unit_cards,
    parse_entrata_prospectportal_html,
    parse_prospectportal_unit_spaces,
)


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUT = ROOT / "entrata_residual_lane"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
OUTPUT = OUT / "evidence_entrata_residual_current_direct_audit.json"
MAX_WORKERS = 3
MAX_LINKS = 30
STOPWORDS = {
    "apartment",
    "apartments",
    "apt",
    "apts",
    "at",
    "community",
    "home",
    "homes",
    "of",
    "on",
    "residence",
    "residences",
    "the",
    "townhome",
    "townhomes",
    "i",
    "ii",
    "iii",
    "iv",
}
RENT_FIELDS = (
    "market_rent_low",
    "market_rent_high",
    "rent_low",
    "rent_high",
    "asking_rent",
    "rent",
)
INDEX_MARKERS = (
    "fp-card",
    "fp-group-item",
    "unit-item",
    "view_unit_spaces",
    "unitsdata",
    "jd-fp-unit-card",
)
DETAIL_MARKERS = (
    "unit-details",
    "unit-item",
    "unitsdata",
    "jd-fp-unit-card",
    "available-unit",
)


def json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def name_tokens(value: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if token not in STOPWORDS
    ]


def normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def safe_conventional_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme.casefold() in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and port is None
        and not parsed.query
        and not parsed.fragment
        and re.fullmatch(
            r"/[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*/conventional/?",
            parsed.path,
            re.IGNORECASE,
        )
    )


def url_name_match(url: str, property_name: str) -> bool:
    wanted = name_tokens(property_name)
    if not wanted:
        return False
    parsed = urlsplit(url)
    identity = f"{parsed.hostname or ''} {parsed.path}".casefold()
    observed = set(name_tokens(identity))
    compact = re.sub(r"[^a-z0-9]+", "", identity)
    return all(
        token in observed or (len(token) >= 4 and token in compact)
        for token in wanted
    )


def archived_html(property_id: str) -> str:
    path = ROOT / "raw_all" / f"{property_id}.html.gz"
    if not path.exists():
        return ""
    return gzip.open(path, "rb").read().decode("utf-8", "replace")


def canonical_rows() -> dict[str, dict[str, str]]:
    with PROPERTIES.open(encoding="utf-8-sig", newline="") as handle:
        return {
            str(row.get("apartmentid") or "").strip(): row
            for row in csv.DictReader(handle)
        }


def residual_rows() -> list[dict[str, str]]:
    canonical = canonical_rows()
    with REMAINING.open(encoding="utf-8-sig", newline="") as handle:
        rows = [
            row
            for row in csv.DictReader(handle)
            if row.get("current_detected_adapter") == "entrata"
        ]
    for row in rows:
        meta = canonical.get(row["property_id"], {})
        row["property_name"] = row.get("property_name") or meta.get("name") or ""
        row["address"] = meta.get("address") or ""
        row["city"] = meta.get("city") or ""
        row["state"] = meta.get("state") or ""
        row["zip"] = meta.get("zip") or ""
    return rows


def prior_hb_rows() -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(ROOT.glob("hb_entrata_sweep_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in payload.get("results", []):
            pid = str(row.get("property_id") or "")
            if not pid:
                continue
            copy = dict(row)
            copy["artifact"] = str(path)
            out.setdefault(pid, []).append(copy)
    return out


def candidate_url(
    row: dict[str, str],
    html: str,
    prior: list[dict[str, Any]],
) -> tuple[str, str, list[str]]:
    name = row.get("property_name") or ""
    observed: list[str] = []

    # A prior strict sweep route already passed the archived exact-property
    # candidate gate.  Keep it as an evidence source, not as proof of current
    # unit inventory.
    for item in prior:
        candidate = str(item.get("matched_url") or "")
        if safe_conventional_url(candidate) and candidate not in observed:
            observed.append(candidate)

    for candidate in _find_pp_conventional_index(html, row.get("website") or ""):
        normalized_url = candidate if candidate.endswith("/") else candidate + "/"
        if safe_conventional_url(normalized_url) and normalized_url not in observed:
            observed.append(normalized_url)
    for match in _PP_CONVENTIONAL_RE.finditer(html):
        candidate = urljoin(row.get("website") or "", match.group(1))
        normalized_url = candidate if candidate.endswith("/") else candidate + "/"
        if safe_conventional_url(normalized_url) and normalized_url not in observed:
            observed.append(normalized_url)

    name_matches = [url for url in observed if url_name_match(url, name)]
    if len(name_matches) == 1:
        return name_matches[0], "unique_published_property_name_match", observed
    prior_matches = [
        str(item.get("matched_url") or "")
        for item in prior
        if safe_conventional_url(str(item.get("matched_url") or ""))
        and item.get("property_identity_match") is True
    ]
    prior_matches = list(dict.fromkeys(prior_matches))
    if len(prior_matches) == 1:
        return prior_matches[0], "prior_strict_identity_matched_route", observed
    return "", "no_unique_exact_property_route", observed


def response_is_challenge(html: str) -> bool:
    low = html.casefold()
    return any(
        token in low
        for token in ("just a moment", "verify you are human", "cf-chl-")
    )


def bounded_get(
    session: requests.Session,
    url: str,
    *,
    predicate: Callable[[str], bool],
    referer: str = "",
    xhr: bool = False,
) -> tuple[str, str, list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, 3):
        headers: dict[str, str] = {}
        if referer:
            headers["Referer"] = referer
        if xhr:
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers["Accept"] = "text/html, */*; q=0.01"
        try:
            response = session.get(
                url,
                headers=headers or None,
                timeout=45,
                allow_redirects=True,
            )
            body = response.text
            accepted = (
                response.status_code == 200
                and not response_is_challenge(body)
                and predicate(body)
            )
            attempts.append(
                {
                    "attempt": attempt,
                    "status_code": response.status_code,
                    "final_url": str(response.url),
                    "body_bytes": len(response.content),
                    "body_sha256": hash_bytes(response.content),
                    "accepted": accepted,
                }
            )
            if accepted:
                return body, str(response.url), attempts
        except Exception as exc:
            attempts.append(
                {
                    "attempt": attempt,
                    "error": type(exc).__name__,
                    "accepted": False,
                }
            )
    return "", "", attempts


def same_origin(url: str, origin: str) -> bool:
    try:
        left = urlsplit(url)
        right = urlsplit(origin)
    except (TypeError, ValueError):
        return False
    return bool(
        left.scheme.casefold() == right.scheme.casefold()
        and (left.hostname or "").casefold() == (right.hostname or "").casefold()
        and left.port == right.port
        and left.username is None
        and left.password is None
    )


def positive_rent(row: dict[str, Any]) -> bool:
    for key in RENT_FIELDS:
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(float(value)) and float(value) > 0:
            return True
    return False


def strict_units(rows: list[dict[str, Any]], origin: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        unit = str(row.get("unit_number") or "").strip()
        building = str(row.get("building") or "").strip().casefold()
        source = str(row.get("source_api_url") or "")
        key = (building, unit.casefold())
        if (
            not unit
            or key in seen
            or not unit_has_real_anchor(row)
            or not positive_rent(row)
            or not source
            or not same_origin(source, origin)
        ):
            continue
        seen.add(key)
        out.append(row)
    return out


def body_identity(
    html: str,
    row: dict[str, str],
    accepted_url: str,
    candidate_basis: str,
) -> dict[str, Any]:
    text = normalized(html)
    tokens = name_tokens(row.get("property_name") or "")
    name_match = bool(tokens) and all(token in set(text.split()) for token in tokens)
    address = normalized(row.get("address") or "")
    address_match = bool(address) and address in text
    city_zip = normalized(
        f"{row.get('city') or ''} {row.get('state') or ''} {row.get('zip') or ''}"
    )
    city_zip_match = bool(city_zip) and city_zip in text
    url_match = url_name_match(accepted_url, row.get("property_name") or "")
    prior_strict = candidate_basis == "prior_strict_identity_matched_route"
    return {
        "name_match": name_match,
        "address_match": address_match,
        "city_state_zip_match": city_zip_match,
        "url_name_match": url_match,
        "prior_strict_route": prior_strict,
        "pass": bool(name_match and (url_match or address_match or prior_strict)),
    }


def audit_one(
    row: dict[str, str],
    prior_map: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    pid = row["property_id"]
    if pid == "1375":
        return {
            "property_id": 1375,
            "property_name": row["property_name"],
            "outcome": "MATERIALIZED_SEPARATELY",
            "artifact": str(
                OUT / "evidence_entrata_1375_lakewood_current_strict.json"
            ),
        }
    html = archived_html(pid)
    prior = prior_map.get(pid, [])
    candidate, basis, observed = candidate_url(row, html, prior)
    base = {
        "property_id": int(pid),
        "property_name": row.get("property_name") or "",
        "website": row.get("website") or "",
        "address": row.get("address") or "",
        "rp_oracle_native_unit_rows": int(row.get("rp_oracle_native_unit_rows") or 0),
        "prior_disposition": row.get("prior_disposition") or "",
        "archived_body_bytes": len(html),
        "candidate_url": candidate,
        "candidate_basis": basis,
        "observed_conventional_urls": observed,
        "prior_hb_evidence": prior,
        "hyperbrowser_sessions_used": 0,
    }
    if not candidate:
        return {**base, "outcome": "NO_UNIQUE_EXACT_ROUTE", "units": 0}

    session = requests.Session(impersonate="chrome120")
    index_html, final_url, index_attempts = bounded_get(
        session,
        candidate,
        predicate=lambda body: any(
            marker in body.casefold() for marker in INDEX_MARKERS
        )
        or bool(find_entrata_pp_plan_links(body, candidate))
        or bool(_extract_vus_urls([(candidate, body)], candidate)),
    )
    if not index_html:
        return {
            **base,
            "outcome": "DIRECT_INDEX_BLOCKED_OR_EMPTY",
            "units": 0,
            "index_attempts": index_attempts,
        }
    if not same_origin(final_url, candidate):
        return {
            **base,
            "outcome": "CROSS_ORIGIN_REDIRECT_REJECTED",
            "units": 0,
            "index_attempts": index_attempts,
            "index_final_url": final_url,
        }
    identity = body_identity(index_html, row, final_url, basis)
    if not identity["pass"]:
        return {
            **base,
            "outcome": "PROPERTY_IDENTITY_UNPROVEN",
            "units": 0,
            "index_attempts": index_attempts,
            "index_final_url": final_url,
            "identity": identity,
        }

    plan_rows = parse_entrata_prospectportal_html(index_html, final_url)
    raw_rows: list[dict[str, Any]] = []
    parser_counts: dict[str, dict[str, int]] = {}
    for parser in (
        parse_entrata_pp_unit_cards,
        parse_entrata_pp_jd_fp_cards,
        parse_entrata_modern_units_data,
    ):
        parsed = parser(index_html, final_url)
        parser_counts.setdefault(final_url, {})[parser.__name__] = len(parsed)
        raw_rows.extend(parsed)

    plan_links = [
        link
        for link in find_entrata_pp_plan_links(index_html, final_url)
        if same_origin(link, final_url)
    ][:MAX_LINKS]
    vus_links = [
        link
        for _, link in _extract_vus_urls([(final_url, index_html)], final_url)
        if same_origin(link, final_url)
    ][:MAX_LINKS]
    detail_fetches: list[dict[str, Any]] = []
    for link in vus_links:
        body, fetched_url, attempts = bounded_get(
            session,
            link,
            predicate=lambda value: bool(value.strip()),
            referer=final_url,
            xhr=True,
        )
        detail_fetches.append(
            {
                "kind": "view_unit_spaces",
                "url": link,
                "final_url": fetched_url,
                "attempts": attempts,
            }
        )
        if body and same_origin(fetched_url, final_url):
            parsed = parse_prospectportal_unit_spaces(body, link)
            parser_counts.setdefault(link, {})[
                "parse_prospectportal_unit_spaces"
            ] = len(parsed)
            raw_rows.extend(parsed)

    for link in plan_links:
        body, fetched_url, attempts = bounded_get(
            session,
            link,
            predicate=lambda value: any(
                marker in value.casefold() for marker in DETAIL_MARKERS
            ),
            referer=final_url,
        )
        detail_fetches.append(
            {
                "kind": "plan_detail",
                "url": link,
                "final_url": fetched_url,
                "attempts": attempts,
            }
        )
        if not body or not same_origin(fetched_url, final_url):
            continue
        for parser in (
            parse_entrata_pp_unit_cards,
            parse_entrata_pp_jd_fp_cards,
            parse_entrata_modern_units_data,
        ):
            parsed = parser(body, link)
            parser_counts.setdefault(link, {})[parser.__name__] = len(parsed)
            raw_rows.extend(parsed)

    units = strict_units(raw_rows, final_url)
    all_sources = sorted(
        {str(unit.get("source_api_url") or "") for unit in units}
    )
    native_source_id_rows = sum(
        bool(unit.get("source_ids")) for unit in units
    )
    fetches_complete = all(
        any(attempt.get("accepted") for attempt in fetch.get("attempts", []))
        for fetch in detail_fetches
    ) if detail_fetches else True
    outcome = (
        "STRICT_UNIT_QUALIFIED"
        if units
        else "PLAN_ONLY_CURRENT"
        if plan_rows
        else "NO_NATIVE_UNIT_ROSTER"
    )
    return {
        **base,
        "outcome": outcome,
        "units": len(units),
        "plans": len(plan_rows),
        "identity": identity,
        "property_identity_match": True,
        "contamination_verdict": (
            "pass_exact_property_same_origin_native_positive_rent"
            if units
            else "no_strict_native_positive_rent_rows"
        ),
        "index_final_url": final_url,
        "index_attempts": index_attempts,
        "published_plan_links": plan_links,
        "published_vus_links": vus_links,
        "detail_fetches": detail_fetches,
        "detail_fetches_complete": fetches_complete,
        "parser_counts": parser_counts,
        "native_identity_rows": len(units),
        "native_positive_rent_rows": len(units),
        "native_source_id_rows": native_source_id_rows,
        "source_urls": all_sources,
        "native_rows": json_safe(units),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = residual_rows()
    prior = prior_hb_rows()
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(audit_one, row, prior): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "property_id": int(row["property_id"]),
                    "property_name": row.get("property_name") or "",
                    "outcome": "AUDIT_EXCEPTION",
                    "error": f"{type(exc).__name__}: {exc}",
                    "units": 0,
                    "hyperbrowser_sessions_used": 0,
                }
            results.append(result)
            print(
                json.dumps(
                    {
                        key: result.get(key)
                        for key in (
                            "property_id",
                            "property_name",
                            "outcome",
                            "units",
                            "plans",
                        )
                    }
                ),
                flush=True,
            )
    results.sort(key=lambda item: int(item["property_id"]))
    summary = {
        "result_type": "current_direct_static_entrata_residual_audit",
        "capture_timestamp_utc": datetime.now(UTC).isoformat(),
        "cohort_properties": len(results),
        "strict_unit_qualified_properties": sum(
            row.get("outcome") == "STRICT_UNIT_QUALIFIED" for row in results
        ),
        "strict_unit_qualified_property_ids": [
            row["property_id"]
            for row in results
            if row.get("outcome") == "STRICT_UNIT_QUALIFIED"
        ],
        "native_positive_rent_rows": sum(
            int(row.get("native_positive_rent_rows") or 0) for row in results
        ),
        "hyperbrowser_sessions_used": 0,
        "outcome_counts": {
            outcome: sum(row.get("outcome") == outcome for row in results)
            for outcome in sorted({str(row.get("outcome")) for row in results})
        },
    }
    OUTPUT.write_text(
        json.dumps({"summary": summary, "results": results}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"summary": summary}, indent=2))


if __name__ == "__main__":
    main()
