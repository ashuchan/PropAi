"""Current, direct-first audit of the 29 remaining RentCafe FAILED_NO_DATA rows.

Evidence-only runner.  It reads the shared cohort/ledger, but never writes
either one and never changes production code.  Direct curl_cffi requests are
forced to ``unlocker=False`` and ``proxies={}``; the only browser material read
here is the already-bounded priority-3 Hyperbrowser capture made with
``solveCaptchas=false`` and ``useStealth=false``.
"""

from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


HERE = Path(__file__).resolve().parent
SHARED_ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
CANDIDATE_REPO = Path(
    "/Users/ankur/PropAi-main/.claude/worktrees/wf_94b9d351-073-7"
)
sys.path.insert(0, str(CANDIDATE_REPO))

# Belt-and-suspenders: every probe call below also passes explicit direct-only
# kwargs, but clearing these avoids an accidental paid/provider fallback if a
# helper changes underneath an old evidence checkout.
os.environ["PROBE_PROXY_URL"] = ""
os.environ["WEB_UNLOCKER_KEY"] = ""

from ma_poc.extraction.post_process import post_process  # noqa: E402
from ma_poc.pms.adapters import _probe  # noqa: E402


_ORIGINAL_PROBE_GET = _probe.probe_get


def direct_probe_get(url: str, *args: Any, **kwargs: Any) -> Any:
    """Repository probe with every escalation surface explicitly disabled."""
    kwargs["unlocker"] = False
    kwargs["proxies"] = {}
    kwargs["verify"] = True
    kwargs.pop("retries", None)
    return _ORIGINAL_PROBE_GET(url, *args, **kwargs)


# Adapter imports performed inside recovery helpers resolve this module-level
# symbol, so patching it makes the full local E2E checks direct-only as well.
_probe.probe_get = direct_probe_get

from ma_poc.pms.adapters._rentcafe_hosted_table import (  # noqa: E402
    parse_rentcafe_hosted_table,
)
from ma_poc.pms.adapters._rentcafe_nestin import parse_nestin_detail_page  # noqa: E402
from ma_poc.pms.adapters._securecafe_applicant import (  # noqa: E402
    applicant_api_url,
    find_applicant_targets,
    parse_securecafe_applicant_floorplans,
)
from ma_poc.pms.adapters.base import AdapterContext  # noqa: E402
from ma_poc.pms.adapters.rentcafe import (  # noqa: E402
    RentCafeAdapter,
    _SECURECAFE_URL_RE,
    parse_rentcafe_ysi_unitslist,
    parse_securecafe_availableunits,
)
from ma_poc.pms.adapters.rentcafe_layout_tab import (  # noqa: E402
    parse_rentcafe_lt_applyga,
)
from ma_poc.pms.detector import detect_pms  # noqa: E402


CONCURRENCY = 6
EXPECTED_COHORT_SIZE = 29
HB_RAW_DIR = HERE / "hb_priority3_raw"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _archived_html(property_id: str) -> str:
    path = SHARED_ROOT / "raw_all" / f"{property_id}.html.gz"
    if not path.exists():
        return ""
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
        return handle.read().replace("\\/", "/")


def _hb_html(property_id: str) -> str:
    path = HB_RAW_DIR / f"{property_id}.html.gz"
    if not path.exists():
        return ""
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _absolute_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    return value if "://" in value else f"https://{value}"


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _same_host(left: str, right: str) -> bool:
    return bool(_host(left)) and _host(left) == _host(right)


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


def _fetch(url: str) -> dict[str, Any]:
    try:
        response = direct_probe_get(url, timeout=25)
    except Exception as exc:  # evidence row, never abort the cohort
        return {
            "requested_url": url,
            "status": 0,
            "final_url": url,
            "body": "",
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
    body = str(getattr(response, "text", "") or "")
    return {
        "requested_url": url,
        "status": int(getattr(response, "status_code", 0) or 0),
        "final_url": str(getattr(response, "url", "") or url),
        "body": body,
        "error": "",
    }


def _fetch_many(items: list[tuple[str, str]]) -> dict[str, dict[str, Any]]:
    """Fetch ``[(stable_key, url)]`` concurrently, preserving stable keys."""
    out: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = {executor.submit(_fetch, url): key for key, url in items}
        for future in as_completed(futures):
            out[futures[future]] = future.result()
    return out


_NAME_DROP = {
    "a",
    "an",
    "and",
    "apartments",
    "apartment",
    "at",
    "community",
    "i",
    "ii",
    "iii",
    "residences",
    "the",
}
_STREET_DROP = {
    "n",
    "s",
    "e",
    "w",
    "north",
    "south",
    "east",
    "west",
    "street",
    "st",
    "avenue",
    "ave",
    "boulevard",
    "blvd",
    "road",
    "rd",
    "drive",
    "dr",
    "lane",
    "ln",
    "way",
    "court",
    "ct",
}


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (value or "").lower())


def _name_match(expected: str, observed: str) -> bool:
    exp = [token for token in _tokens(expected) if token not in _NAME_DROP]
    obs = set(_tokens(observed))
    return bool(exp) and all(token in obs for token in exp)


def _address_match(expected: str, observed: str) -> bool:
    exp = _tokens(expected)
    obs = _tokens(observed)
    if not exp or not obs:
        return False
    exp_num = re.match(r"\d+", exp[0])
    if not exp_num:
        return False
    exp_street = [token for token in exp[1:] if token not in _STREET_DROP]
    if not exp_street:
        return False
    # ``observed`` may be a whole marketing page, not just an address. Find
    # the configured house number anywhere in that text and bind street-name
    # tokens to a short local window so an unrelated footer/property cannot
    # satisfy the match. ``1105A`` legitimately matches configured ``1105``.
    for index, token in enumerate(obs):
        observed_num = re.match(r"\d+", token)
        if not observed_num or observed_num.group() != exp_num.group():
            continue
        window = set(obs[index + 1 : index + 12])
        if all(street_token in window for street_token in exp_street):
            return True
    return False


def _page_text(html: str) -> str:
    if not html:
        return ""
    try:
        return " ".join(BeautifulSoup(html, "html.parser").get_text(" ", strip=True).split())
    except Exception:
        return ""


def _page_identity(record: dict[str, str], html: str) -> dict[str, bool]:
    text = _page_text(html)
    return {
        "name_match": _name_match(record["property_name"], text),
        "address_match": _address_match(record["address"], text),
    }


def _published_same_origin_routes(html: str, base_url: str) -> list[dict[str, str]]:
    if not html or not base_url:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for tag, attr, kind in (("a", "href", "anchor"), ("form", "action", "form")):
        for element in soup.find_all(tag):
            raw = str(element.get(attr) or "").strip()
            if not raw:
                continue
            full = urljoin(base_url, raw).split("#", 1)[0]
            if not _same_host(full, base_url):
                continue
            path = urlparse(full).path.lower()
            if not any(
                marker in path
                for marker in ("floorplan", "floor-plan", "availableunit", "availability")
            ):
                continue
            if full in seen:
                continue
            seen.add(full)
            out.append(
                {
                    "url": full,
                    "published_as": kind,
                    "published_method": str(element.get("method") or "get").lower(),
                }
            )
    return out


def _securecafe_bases(html: str) -> list[str]:
    out: list[str] = []
    for match in _SECURECAFE_URL_RE.finditer(html or ""):
        base = (
            f"https://{match.group('sub')}.securecafe.com"
            f"/onlineleasing/{match.group('slug')}"
        )
        if base not in out:
            out.append(base)
    return out


def _positive_rent(row: dict[str, Any]) -> bool:
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


def _native_unit(row: dict[str, Any]) -> str:
    return str(row.get("unit_number") or "").strip()


def _real_native(row: dict[str, Any]) -> bool:
    unit = _native_unit(row)
    return bool(unit) and not unit.upper().startswith("WAIT")


def _row_sample(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "unit_number",
            "floor_plan_name",
            "bedrooms",
            "bathrooms",
            "sqft",
            "market_rent_low",
            "market_rent_high",
            "availability_date",
            "source_api_url",
            "source_ids",
        )
    }


def _audit_rows(
    property_id: str,
    rows_by_parser: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    strongest_parser, raw_rows = max(
        rows_by_parser.items(), key=lambda item: len(item[1]), default=("", [])
    )
    raw_native_positive = [
        row for row in raw_rows if _native_unit(row) and _positive_rent(row)
    ]
    waitlist_rows = [
        row
        for row in raw_native_positive
        if _native_unit(row).upper().startswith("WAIT")
    ]
    strict_candidates = [
        row for row in raw_native_positive if _real_native(row)
    ]
    processed = post_process(strict_candidates, property_id=property_id)
    admitted = [
        row
        for row in processed.admitted
        if _real_native(row) and _positive_rent(row)
    ]
    return (
        {
            "parser_counts": {key: len(value) for key, value in rows_by_parser.items()},
            "strongest_parser": strongest_parser,
            "raw_rows": len(raw_rows),
            "raw_native_positive_rent_rows": len(raw_native_positive),
            "waitlist_pseudo_rows": len(waitlist_rows),
            "strict_real_native_pre_postprocess": len(strict_candidates),
            "strict_real_native_postprocess": len(admitted),
            "sample_rows": [_row_sample(row) for row in raw_native_positive[:3]],
        },
        admitted,
    )


def _parse_html_route(
    property_id: str, html: str, source_url: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = {
        "securecafe_availableunits": parse_securecafe_availableunits(html, source_url),
        "rentcafe_ysi_unitslist": parse_rentcafe_ysi_unitslist(html, source_url),
        "rentcafe_hosted_table": parse_rentcafe_hosted_table(html, source_url),
        "rentcafe_nestin_detail": parse_nestin_detail_page(html, source_url),
        "rentcafe_layout_tab_applyga": parse_rentcafe_lt_applyga(html, source_url),
    }
    return _audit_rows(property_id, rows)


def _applicant_property_fields(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    plans = payload.get("floorPlanList") or []
    if not plans or not isinstance(plans[0], dict):
        return {}
    floorplan = plans[0].get("floorPlan") or {}
    if not isinstance(floorplan, dict):
        return {}
    return {
        key: floorplan.get(key)
        for key in ("PropertyName", "Address", "City", "State", "Zipcode", "PropertyID")
    }


def _public_route_row(
    record: dict[str, str],
    kind: str,
    discovery_source: str,
    fetch: dict[str, Any],
    metrics: dict[str, Any],
    *,
    property_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identity = _page_identity(record, fetch["body"])
    fields = property_fields or {}
    observed_name = str(fields.get("PropertyName") or "")
    observed_address = str(fields.get("Address") or "")
    return {
        "property_id": int(record["property_id"]),
        "property_name": record["property_name"],
        "route_kind": kind,
        "discovery_source": discovery_source,
        "requested_url": fetch["requested_url"],
        "status": fetch["status"],
        "final_url": fetch["final_url"],
        "body_bytes": len(fetch["body"].encode("utf-8", "replace")),
        "body_sha256": _sha256(fetch["body"]),
        "page_name_match": identity["name_match"],
        "page_address_match": identity["address_match"],
        "source_property_name": observed_name,
        "source_property_address": observed_address,
        "source_name_match": _name_match(record["property_name"], observed_name),
        "source_address_match": _address_match(record["address"], observed_address),
        "strongest_parser": metrics["strongest_parser"],
        "raw_rows": metrics["raw_rows"],
        "raw_native_positive_rent_rows": metrics["raw_native_positive_rent_rows"],
        "waitlist_pseudo_rows": metrics["waitlist_pseudo_rows"],
        "strict_real_native_pre_postprocess": metrics["strict_real_native_pre_postprocess"],
        "strict_real_native_postprocess": metrics["strict_real_native_postprocess"],
        "parser_counts": json.dumps(metrics["parser_counts"], sort_keys=True),
        "sample_rows": json.dumps(metrics["sample_rows"], sort_keys=True),
        "error": fetch["error"],
    }


def _metadata() -> dict[str, dict[str, str]]:
    rows = _read_csv(CANDIDATE_REPO / "ma_poc/config/properties.csv")
    return {str(row.get("apartmentid") or "").strip(): row for row in rows}


def _cohort() -> list[dict[str, str]]:
    metadata = _metadata()
    rows = [
        row
        for row in _read_csv(SHARED_ROOT / "strict_recovery_remaining_current.csv")
        if row.get("current_detected_adapter") == "rentcafe"
    ]
    if len(rows) != EXPECTED_COHORT_SIZE:
        raise RuntimeError(f"expected 29 RentCafe residuals, found {len(rows)}")
    out = []
    for row in rows:
        canonical = metadata.get(row["property_id"], {})
        merged = dict(row)
        merged["property_name"] = str(
            row.get("property_name") or canonical.get("name") or ""
        )
        merged["website"] = _absolute_url(
            str(row.get("website") or canonical.get("website") or "")
        )
        merged["address"] = str(canonical.get("address") or "")
        merged["city"] = str(canonical.get("city") or "")
        merged["state"] = str(canonical.get("state") or "")
        merged["zip_code"] = str(canonical.get("zip") or "")
        out.append(merged)
    return out


async def _adapter_e2e(
    record: dict[str, str], html: str, final_url: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    context = AdapterContext(
        base_url=record["website"],
        detected=detect_pms(record["website"], page_html=html),
        profile=None,
        expected_total_units=None,
        property_id=record["property_id"],
        fetch_result=SimpleNamespace(
            body=html.encode("utf-8", "replace"), final_url=final_url
        ),
        property_name=record["property_name"],
        address=record["address"],
        city=record["city"],
        state=record["state"],
        zip_code=record["zip_code"],
    )
    context._api_responses = []
    try:
        result = await asyncio.wait_for(
            RentCafeAdapter().extract(None, context), timeout=120
        )
    except Exception as exc:
        return (
            {
                "passed": False,
                "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                "tier": "",
                "winning_url": "",
                "adapter_units": 0,
                "strict_native_positive_rent_rows": 0,
                "plans": 0,
            },
            [],
        )
    strict = [
        row
        for row in result.units
        if _real_native(row) and _positive_rent(row)
    ]
    return (
        {
            "passed": bool(strict),
            "error": " | ".join(result.errors[-6:]),
            "tier": result.tier_used,
            "winning_url": str(result.winning_url or ""),
            "adapter_units": len(result.units),
            "strict_native_positive_rent_rows": len(strict),
            "plans": len(result.plan_summaries),
        },
        strict,
    )


async def main() -> None:
    cohort = _cohort()
    by_id = {row["property_id"]: row for row in cohort}

    # Phase 1 — current marketing pages, all 29, direct only.
    root_fetches = _fetch_many(
        [(row["property_id"], row["website"]) for row in cohort]
    )

    cohort_snapshot: list[dict[str, Any]] = []
    for record in cohort:
        fetched = root_fetches[record["property_id"]]
        identity = _page_identity(record, fetched["body"])
        cohort_snapshot.append(
            {
                **record,
                "current_status": fetched["status"],
                "current_final_url": fetched["final_url"],
                "current_body_bytes": len(fetched["body"].encode("utf-8", "replace")),
                "current_body_sha256": _sha256(fetched["body"]),
                "current_name_match": identity["name_match"],
                "current_address_match": identity["address_match"],
                "current_error": fetched["error"],
            }
        )

    route_rows: list[dict[str, Any]] = []
    route_admitted: dict[tuple[str, str], list[dict[str, Any]]] = {}

    # Audit every current root body before following links.
    for record in cohort:
        fetched = root_fetches[record["property_id"]]
        metrics, admitted = _parse_html_route(
            record["property_id"], fetched["body"], fetched["final_url"]
        )
        route_rows.append(
            _public_route_row(record, "marketing_root", "current_direct", fetched, metrics)
        )
        route_admitted[(record["property_id"], fetched["final_url"])] = admitted

    # Phase 2 — current same-origin routes explicitly published as anchors or
    # forms.  Two bounded hops catch /floorplans -> form action=/availableunits.
    published_by_pid: dict[str, dict[str, dict[str, str]]] = {}
    first_items: list[tuple[str, str]] = []
    for record in cohort:
        pid = record["property_id"]
        root = root_fetches[pid]
        routes = _published_same_origin_routes(root["body"], root["final_url"])[:4]
        published_by_pid[pid] = {route["url"]: route for route in routes}
        first_items.extend((f"{pid}|{route['url']}", route["url"]) for route in routes)
    first_fetches = _fetch_many(first_items)

    second_items: list[tuple[str, str]] = []
    for key, fetched in first_fetches.items():
        pid, _ = key.split("|", 1)
        known = published_by_pid[pid]
        for route in _published_same_origin_routes(
            fetched["body"], fetched["final_url"]
        ):
            if route["url"] in known or len(known) >= 8:
                continue
            known[route["url"]] = route
            second_items.append((f"{pid}|{route['url']}", route["url"]))
    second_fetches = _fetch_many(second_items)

    all_same_origin = {**first_fetches, **second_fetches}
    for key, fetched in all_same_origin.items():
        pid, requested = key.split("|", 1)
        record = by_id[pid]
        route = published_by_pid[pid].get(requested, {})
        metrics, admitted = _parse_html_route(
            pid, fetched["body"], fetched["final_url"]
        )
        route_rows.append(
            _public_route_row(
                record,
                f"same_origin_{route.get('published_as', 'route')}",
                "current_direct_published",
                fetched,
                metrics,
            )
        )
        route_admitted[(pid, fetched["final_url"])] = admitted

    # Phase 3 — exact SecureCafe routes harvested from current direct HTML,
    # the archived July source, and the three bounded clean-browser captures.
    base_sources: dict[tuple[str, str], set[str]] = {}
    for record in cohort:
        pid = record["property_id"]
        sources = {
            "current_direct": root_fetches[pid]["body"],
            "archived_0731": _archived_html(pid),
            "hb_exact_clean": _hb_html(pid),
        }
        for label, html in sources.items():
            for base in _securecafe_bases(html)[:5]:
                base_sources.setdefault((pid, base), set()).add(label)
    secure_items = [
        (f"{pid}|{base}", f"{base}/availableunits.aspx")
        for (pid, base) in base_sources
    ]
    secure_fetches = _fetch_many(secure_items)
    for key, fetched in secure_fetches.items():
        pid, base = key.split("|", 1)
        record = by_id[pid]
        metrics, admitted = _parse_html_route(
            pid, fetched["body"], fetched["final_url"]
        )
        route_rows.append(
            _public_route_row(
                record,
                "securecafe_availableunits",
                "+".join(sorted(base_sources[(pid, base)])),
                fetched,
                metrics,
            )
        )
        route_admitted[(pid, fetched["final_url"])] = admitted

    # Phase 4 — bound Applicant API targets only. Never pair a bare portal
    # with an unrelated page-wide propertyId; the candidate helper enforces it.
    applicant_sources: dict[tuple[str, str, str, str], set[str]] = {}
    for record in cohort:
        pid = record["property_id"]
        for label, html in (
            ("current_direct", root_fetches[pid]["body"]),
            ("archived_0731", _archived_html(pid)),
            ("hb_exact_clean", _hb_html(pid)),
        ):
            for target in find_applicant_targets(html):
                applicant_sources.setdefault(
                    (pid, target.sub, target.slug, target.property_id), set()
                ).add(label)
    applicant_items = [
        (
            f"{pid}|{sub}|{slug}|{property_id}",
            applicant_api_url(sub, property_id),
        )
        for (pid, sub, slug, property_id) in applicant_sources
    ]
    applicant_fetches = _fetch_many(applicant_items)
    for key, fetched in applicant_fetches.items():
        pid, sub, slug, property_id = key.split("|", 3)
        record = by_id[pid]
        try:
            payload: Any = json.loads(fetched["body"])
        except (json.JSONDecodeError, TypeError):
            payload = None
        rows = parse_securecafe_applicant_floorplans(payload, fetched["final_url"])
        metrics, admitted = _audit_rows(pid, {"securecafe_applicant": rows})
        route_rows.append(
            _public_route_row(
                record,
                "securecafe_applicant_api",
                "+".join(
                    sorted(applicant_sources[(pid, sub, slug, property_id)])
                ),
                fetched,
                metrics,
                property_fields=_applicant_property_fields(payload),
            )
        )
        route_admitted[(pid, fetched["final_url"])] = admitted

    # Current local full-adapter E2E for the three source-qualified winners.
    e2e_inputs = {
        "29566": (
            root_fetches["29566"]["body"],
            root_fetches["29566"]["final_url"],
            "heatherwoodfl.securecafeapplicant.com",
        ),
        "277913": (
            root_fetches["277913"]["body"],
            root_fetches["277913"]["final_url"],
            "/redwood-perrysburg-oregon-road/availableunits.aspx",
        ),
        "220907": (
            _hb_html("220907"),
            "https://keystonemanagement.com/goldsboro-north-carolina-apartments/reserve-at-bradbury-place",
            "/reserve-at-bradbury-place-0/availableunits.aspx",
        ),
    }
    recoveries: list[dict[str, Any]] = []
    for pid, (html, final_url, expected_source_fragment) in e2e_inputs.items():
        record = by_id[pid]
        e2e, units = await _adapter_e2e(record, html, final_url)
        identity = _page_identity(record, html)
        source_urls = sorted(
            {
                str(row.get("source_api_url") or "")
                for row in units
                if str(row.get("source_api_url") or "")
            }
        )
        expected_source = bool(source_urls) and all(
            expected_source_fragment in source_url for source_url in source_urls
        )
        strict_pass = bool(
            e2e["passed"]
            and identity["name_match"]
            and identity["address_match"]
            and expected_source
            and len(source_urls) == 1
        )
        recoveries.append(
            {
                "property_id": int(pid),
                "property_name": record["property_name"],
                "website": record["website"],
                "rp_oracle_native_unit_rows": int(
                    record.get("rp_oracle_native_unit_rows") or 0
                ),
                "strict_pass": strict_pass,
                "units": len(units),
                "property_identity_match": bool(
                    identity["name_match"] and identity["address_match"]
                ),
                "property_name_match": identity["name_match"],
                "property_address_match": identity["address_match"],
                "contamination_verdict": (
                    "pass_exact_property_single_winning_source_no_sibling_rows"
                    if strict_pass
                    else "reject_identity_or_source_boundary"
                ),
                "native_identity_rows": len(units),
                "native_positive_rent_rows": len(units),
                "source_urls": source_urls,
                "sample_native_unit_ids": [
                    _native_unit(row) for row in units[:5]
                ],
                "unit_samples": [_row_sample(row) for row in units[:5]],
                "local_validation": "current_candidate_full_rentcafe_adapter_e2e_direct_only",
                "adapter_e2e": e2e,
                "source_support": (
                    "current_direct_exact_bound_applicant"
                    if pid == "29566"
                    else "current_direct_exact_securecafe"
                    if pid == "277913"
                    else "hb_discovered_exact_published_securecafe_then_direct_data_fetch"
                ),
            }
        )

    strict_recoveries = [row for row in recoveries if row["strict_pass"]]

    # Notable strict rejections: useful guardrails, not conversions.
    def _find_route(pid: str, url_fragment: str) -> dict[str, Any] | None:
        for row in route_rows:
            if str(row["property_id"]) == pid and url_fragment in row["final_url"]:
                return row
        return None

    notable_rejections = [
        {
            "property_id": 218786,
            "property_name": "Cooper's Landing",
            "reason": "Applicant UnitAvailability contains only WAIT* pseudo-unit codes with historical dates; no apartment number qualifies",
            "evidence": _find_route("218786", "propertyId=480033"),
        },
        {
            "property_id": 46915,
            "property_name": "Barberton",
            "reason": "exact /availableunits publishes four real rows, but current parser loses dimensions and post_process admits zero; not claimed as current code recovery",
            "evidence": _find_route("46915", "/availableunits"),
        },
        {
            "property_id": 219752,
            "property_name": "Lamphouse",
            "reason": "exact published SecureCafe route redirects to 2,822-byte Applicant shell and no bound myOlePropertyId is published; unsafe to synthesize",
            "evidence": _find_route("219752", "203-main-llc"),
        },
        {
            "property_id": 69558,
            "property_name": "Spring Hill Apartments",
            "reason": "exact name/address-matched Applicant API is currently sold out; zero native unit rows",
            "evidence": _find_route("69558", "getfloorplanandavailableunits"),
        },
        {
            "property_id": 231543,
            "property_name": "Autumn Hills",
            "reason": "exact name/address-matched Applicant API is currently sold out; zero native unit rows",
            "evidence": _find_route("231543", "getfloorplanandavailableunits"),
        },
    ]

    ledger_rows = _read_csv(SHARED_ROOT / "strict_recovery_ledger_current.csv")
    ledger_ids = {str(row.get("property_id") or "") for row in ledger_rows}
    candidate_ids = {str(row["property_id"]) for row in strict_recoveries}
    overlaps = sorted(candidate_ids & ledger_ids, key=int)
    net_new_ids = sorted(candidate_ids - ledger_ids, key=int)
    projected_total = len(ledger_ids) + len(net_new_ids)

    candidate_head = subprocess.check_output(
        ["git", "-C", str(CANDIDATE_REPO), "rev-parse", "HEAD"], text=True
    ).strip()
    applicant_source = CANDIDATE_REPO / "ma_poc/pms/adapters/_securecafe_applicant.py"
    provenance = {
        "candidate_repo": str(CANDIDATE_REPO),
        "candidate_head": candidate_head,
        "applicant_source_sha256": hashlib.sha256(applicant_source.read_bytes()).hexdigest(),
        "network_policy": {
            "direct_first": True,
            "probe_unlocker": False,
            "probe_proxies": {},
            "llm_enabled": False,
            "canary": False,
            "hyperbrowser": {
                "targets": 3,
                "solveCaptchas": False,
                "useStealth": False,
                "summary": str(HERE / "hb_priority3_summary.json"),
            },
        },
    }

    summary = {
        "cohort_file": str(SHARED_ROOT / "strict_recovery_remaining_current.csv"),
        "cohort_current_adapter": "rentcafe",
        "cohort_properties": len(cohort),
        "root_direct_status_counts": {
            str(status): sum(
                int(row["current_status"] == status) for row in cohort_snapshot
            )
            for status in sorted({row["current_status"] for row in cohort_snapshot})
        },
        "route_audit_rows": len(route_rows),
        "strict_candidate_properties": len(strict_recoveries),
        "strict_candidate_ids": sorted(candidate_ids, key=int),
        "authoritative_ledger_snapshot_properties": len(ledger_ids),
        "authoritative_overlap_count": len(overlaps),
        "authoritative_overlap_ids": overlaps,
        "strict_net_new_properties": len(net_new_ids),
        "strict_net_new_ids": net_new_ids,
        "projected_ledger_properties_if_accepted": projected_total,
        "projected_failed344_recovery_percent_if_accepted": round(
            100 * projected_total / 344, 4
        ),
        "projected_remaining_to_60_percent_gate": max(0, 207 - projected_total),
        "projected_remaining_to_75_percent_target": max(0, 258 - projected_total),
        "rp_native_overlap_properties": sum(
            int(row["rp_oracle_native_unit_rows"] > 0) for row in strict_recoveries
        ),
        "rp_native_overlap_ids": [
            str(row["property_id"])
            for row in strict_recoveries
            if row["rp_oracle_native_unit_rows"] > 0
        ],
        "paid_canary_run": False,
        "shared_ledger_mutated": False,
        "production_code_mutated": False,
        "provenance": provenance,
    }

    # Materialize only new investigation artifacts.
    _write_csv(
        HERE / "cohort29.csv",
        cohort_snapshot,
        [
            "property_id",
            "property_name",
            "website",
            "address",
            "city",
            "state",
            "source_adapter_0731",
            "current_detected_adapter",
            "rp_oracle_native_unit_rows",
            "rp_oracle_distinct_floorplans",
            "prior_disposition",
            "current_status",
            "current_final_url",
            "current_body_bytes",
            "current_body_sha256",
            "current_name_match",
            "current_address_match",
            "current_error",
        ],
    )
    _write_csv(
        HERE / "direct_route_audit.csv",
        route_rows,
        [
            "property_id",
            "property_name",
            "route_kind",
            "discovery_source",
            "requested_url",
            "status",
            "final_url",
            "body_bytes",
            "body_sha256",
            "page_name_match",
            "page_address_match",
            "source_property_name",
            "source_property_address",
            "source_name_match",
            "source_address_match",
            "strongest_parser",
            "raw_rows",
            "raw_native_positive_rent_rows",
            "waitlist_pseudo_rows",
            "strict_real_native_pre_postprocess",
            "strict_real_native_postprocess",
            "parser_counts",
            "sample_rows",
            "error",
        ],
    )
    (HERE / "strict_net_new_recoveries.json").write_text(
        json.dumps(
            {
                "provenance": provenance,
                "e2e_candidates": recoveries,
                "recoveries": strict_recoveries,
                "overlap_ids": overlaps,
                "net_new_ids": net_new_ids,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_csv(
        HERE / "strict_net_new_recoveries.csv",
        [
            {
                **row,
                "source_urls": " | ".join(row["source_urls"]),
                "sample_native_unit_ids": " | ".join(row["sample_native_unit_ids"]),
                "unit_samples": json.dumps(row["unit_samples"], sort_keys=True),
                "adapter_e2e": json.dumps(row["adapter_e2e"], sort_keys=True),
            }
            for row in strict_recoveries
        ],
        [
            "property_id",
            "property_name",
            "website",
            "rp_oracle_native_unit_rows",
            "strict_pass",
            "units",
            "property_identity_match",
            "contamination_verdict",
            "native_identity_rows",
            "native_positive_rent_rows",
            "source_urls",
            "sample_native_unit_ids",
            "local_validation",
            "source_support",
            "adapter_e2e",
            "unit_samples",
        ],
    )
    (HERE / "notable_strict_rejections.json").write_text(
        json.dumps(notable_rejections, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (HERE / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
