"""Essex Property Trust adapter — UNIT-LEVEL (browser-intercept).

Research log (2026-05-17, user DevTools capture verified)
---------------------------------------------------------
Essex (a public multifamily REIT, essexapartmenthomes.com, ~250
communities) is tagged ``rentcafe`` by the detector but in production
0 of these reached Tier-1 — the marketing site is a Next.js/Vercel app
that returns an empty shell to static/automated fetch (prod
``no_body_short_circuit``), and the public ``securecafe`` portal our
RentCafe adapter probes is only exposed behind resident login.

The real per-unit data is a clean same-origin JSON API:

  GET https://www.essexapartmenthomes.com/api/properties/{propertyId}
      /units/{unitId}/availability?date=YYYY-MM-DD
  (Next.js route /api/properties/[propertyId]/units/[unitId]/availability)

  Response:
    {success, result:{property_id, floorplan_id, unit_id,
      start_date, end_date,
      pricing_by_date:[{date:ISO,
        terms_by_month:[{term_months, rent:"2487.00",
                         deposit:"600.00", apply_url}]}]}}

  (The ``apply_url`` reveals the leasing backend is Nestio/Funnel —
   nestiolistings.com companyID=18855 — but the Essex API is the clean
   surface, so we parse it directly.)

Access constraint
-----------------
NO Authorization/Bearer; cookies present are analytics/consent only.
BUT the endpoint is behind **Vercel Firewall bot protection** — a
plain server-side curl returns HTTP 429 "Vercel Security Check".
Therefore, exactly like the RealPage OLL adapter, the only viable
strategy is **browser-based Tier-1 API interception**: the pipeline's
patchright browser renders the property's floor-plans-and-pricing page
(passing the Vercel challenge as a legit browser), which fires the
per-unit ``/availability`` calls; this adapter parses those responses
from the captured network log (``ctx._api_responses``). The request is
never forged server-side.

Verified: city-view (property 492967, unit 6302379, floorplan
2101784) → 12-month rent $2,487, earliest availability 2026-05-17.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.property_identity import names_match
from ma_poc.pms.source_provenance import (
    build_unit_source_provenance,
    response_sha256,
    sanitise_source_url,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

_TIER = "TIER_1_API_ESSEX"

# 2026-05-18 (user HAR): the BULK endpoint
#   GET /api/properties/{propertyId}/availability?start_date&end_date&format=spa
# returns ALL units in one call and — unlike the per-unit route — clears
# the Vercel bot check under curl_cffi chrome impersonation (verified
# 200, 8 floorplans / 25 units, property 492967). Shape:
#   {"result":{"floorplans":[{...,"units":[{unit_id,name,floorplan_name,
#     beds,baths,sqft,minimum_rent,maximum_rent,availability_date,
#     specials,amenities,floorplate:{floor,building_name}}]}]}}
# 2026-07-18: essexapartmenthomes.com migrated to the Next.js App Router —
# the community HTML no longer carries __NEXT_DATA__ or a raw
# ``"propertyId":"..."``; the id now lives inside an ``__next_f`` streaming
# blob where the JSON quotes are BACKSLASH-ESCAPED (``\"propertyId\":\"514264\"``).
# The old literal-quote pattern matched 0/23 live props → all 23 fell to
# FAILED_NO_DATA. Tolerate the optional backslash before each quote so the
# static probe_get path resolves the id again (validated 23/23, 310 units).
# NB: ``propertyCode`` (e.g. ``p0523894``) is a DECOY — the bulk API 404s on
# it — so the pattern anchors specifically on ``propertyId``.
_PROP_ID_RE = re.compile(
    r'(?:data-property-id=\\?"|/api/properties/|\\?"propertyId\\?"\s*[:=]\s*\\?"?)(\d{5,7})',
    re.IGNORECASE,
)
_API_PROPERTY_ID_RE = re.compile(r"/api/properties/(\d{5,7})/availability", re.IGNORECASE)
_PROPERTY_NAME_ID_RE = re.compile(
    r'"PropertyName"\s*:\s*\{\s*"value"\s*:\s*"([^"]+)"\s*\}'
    r'\s*,\s*"PropertyId"\s*:\s*\{\s*"value"\s*:\s*"(\d{5,7})"',
    re.IGNORECASE,
)
_ITEM_PATH_RE = re.compile(r'"itemPath"\s*:\s*"([^"]+)"', re.IGNORECASE)
_RETRYABLE_BULK_OUTCOMES = frozenset(
    {
        "BULK_EXCEPTION",
        "BULK_HTTP_ERROR",
        "BULK_JSON_ERROR",
        "BULK_SHAPE_REJECTED",
    }
)


@dataclass
class _EssexPageEvidence:
    """One configured-property page attempt and its property boundary."""

    requested_url: str
    final_url: str
    status: int
    via: str
    outcome: str
    property_id: str = ""
    property_name: str = ""
    body: str = ""
    exception_class: str = ""


@dataclass
class _EssexBulkResolution:
    """Bounded page/API resolution plus audit telemetry."""

    sources: list[dict[str, Any]] = field(default_factory=list)
    telemetry: list[dict[str, Any]] = field(default_factory=list)
    outcome: str = "SOURCE_EMPTY"
    property_id: str = ""
    property_name: str = ""
    page_url: str = ""
    page_final_url: str = ""
    retry_used: bool = False


def _metadata_text(html: str) -> str:
    """Flatten Next.js' nested quote escaping for metadata-only matching."""

    # The source is a script-string containing JSON inside another JSON string,
    # so quotes appear as either \" or \\\". Removing backslashes is safe for
    # the bounded metadata regexes below; the original body remains untouched
    # for hashing and parsing.
    return html.replace(chr(92), "")


def _canonical_page_url(url: str) -> str:
    """Configured property URL without tracking query/fragment."""

    try:
        parsed = urlsplit((url or "").strip())
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except Exception:
        return (url or "").split("#", 1)[0].split("?", 1)[0]


def _classify_essex_page(
    html: str,
    *,
    configured_name: str,
    requested_url: str,
    final_url: str,
    status: int,
    via: str,
    exception_class: str = "",
) -> _EssexPageEvidence:
    """Resolve one source-bound Essex property ID or one exact empty cause.

    Current Essex pages embed the global 404 route and a portfolio-wide
    property catalogue alongside the active community route. Therefore
    ``itemPath=/404`` alone is *not* a soft-404 signal, and taking the first
    ``PropertyId`` can select a sibling. A real 404 shell has only the 404
    route and no configured-name property pair.
    """

    requested_url = sanitise_source_url(requested_url)
    final_url = sanitise_source_url(final_url or requested_url)
    if exception_class:
        return _EssexPageEvidence(
            requested_url=requested_url,
            final_url=final_url,
            status=int(status or 0),
            via=via,
            outcome="PAGE_EXCEPTION",
            exception_class=exception_class,
        )
    if status and not 200 <= int(status) < 300:
        return _EssexPageEvidence(
            requested_url=requested_url,
            final_url=final_url,
            status=int(status),
            via=via,
            outcome="PAGE_HTTP_ERROR",
            body=html,
        )
    if not html:
        return _EssexPageEvidence(
            requested_url=requested_url,
            final_url=final_url,
            status=int(status or 0),
            via=via,
            outcome="SOURCE_EMPTY",
        )

    flat = _metadata_text(html)
    item_paths = {path.strip() for path in _ITEM_PATH_RE.findall(flat) if path.strip()}
    non_404_paths = {path for path in item_paths if path.rstrip("/").casefold() != "/404"}
    pairs = _PROPERTY_NAME_ID_RE.findall(flat)
    page_ids = set(_PROP_ID_RE.findall(html))

    if configured_name:
        matched = [(name.strip(), pid) for name, pid in pairs if names_match(configured_name, name)[0]]
        matched_ids = {pid for _name, pid in matched}
        if len(matched_ids) == 1 and (not item_paths or any("apartments" in p.casefold() for p in non_404_paths)):
            pid = next(iter(matched_ids))
            observed_name = next(name for name, candidate in matched if candidate == pid)
            return _EssexPageEvidence(
                requested_url=requested_url,
                final_url=final_url,
                status=int(status or 200),
                via=via,
                outcome="SUCCESS",
                property_id=pid,
                property_name=observed_name,
                body=html,
            )
        # Legacy Essex pages exposed one raw propertyId without the newer
        # PropertyName/PropertyId object pair. A sole ID on a real community
        # route is still source-bound; multiple IDs remain ambiguous.
        if (
            not pairs
            and len(page_ids) == 1
            and (not item_paths or any("apartments" in p.casefold() for p in non_404_paths))
        ):
            return _EssexPageEvidence(
                requested_url=requested_url,
                final_url=final_url,
                status=int(status or 200),
                via=via,
                outcome="SUCCESS",
                property_id=next(iter(page_ids)),
                body=html,
            )
        if len(matched_ids) > 1:
            outcome = "PROPERTY_ID_AMBIGUOUS"
        elif item_paths and not non_404_paths and "/404" in {p.rstrip("/").casefold() for p in item_paths}:
            outcome = "SOURCE_404_SHELL"
        elif pairs or _PROP_ID_RE.search(html):
            outcome = "PROPERTY_IDENTITY_REJECTED"
        else:
            outcome = "SOURCE_PROPERTY_ID_MISSING"
        return _EssexPageEvidence(
            requested_url=requested_url,
            final_url=final_url,
            status=int(status or 200),
            via=via,
            outcome=outcome,
            body=html,
        )

    # Older callers may not have configured CSV identity. Accept only a
    # single page-wide ID; a portfolio payload with multiple IDs is ambiguous.
    if len(page_ids) == 1:
        pid = next(iter(page_ids))
        observed_name = next((name for name, candidate in pairs if candidate == pid), "")
        return _EssexPageEvidence(
            requested_url=requested_url,
            final_url=final_url,
            status=int(status or 200),
            via=via,
            outcome="SUCCESS",
            property_id=pid,
            property_name=observed_name,
            body=html,
        )
    if len(page_ids) > 1:
        outcome = "PROPERTY_ID_AMBIGUOUS"
    elif item_paths and not non_404_paths and "/404" in {p.rstrip("/").casefold() for p in item_paths}:
        outcome = "SOURCE_404_SHELL"
    else:
        outcome = "SOURCE_PROPERTY_ID_MISSING"
    return _EssexPageEvidence(
        requested_url=requested_url,
        final_url=final_url,
        status=int(status or 200),
        via=via,
        outcome=outcome,
        body=html,
    )


def _page_telemetry(page: _EssexPageEvidence) -> dict[str, Any]:
    return {
        "url": page.requested_url,
        "status": page.status,
        "body": None,
        "via": page.via,
        "essex_outcome": page.outcome,
        "page_final_url": page.final_url,
        "source_property_id": page.property_id,
        "source_property_name": page.property_name,
        "exception_class": page.exception_class,
        "response_shape": "essex_property_page" if page.outcome == "SUCCESS" else "",
        "response_sha256": response_sha256(page.body) if page.body else "",
        "row_count": 0,
    }


def _bulk_raw_unit_count(body: Any) -> int:
    if not _is_essex_bulk(body):
        return 0
    return sum(
        1
        for fp in (body.get("result") or {}).get("floorplans") or []
        if isinstance(fp, dict)
        for unit in fp.get("units") or []
        if isinstance(unit, dict)
    )


def _request_payload(source_url: str, property_id: str) -> dict[str, str]:
    try:
        query = parse_qs(urlsplit(source_url).query)
    except Exception:
        query = {}
    return {
        "property_id": property_id,
        "start_date": str((query.get("start_date") or [""])[0]),
        "end_date": str((query.get("end_date") or [""])[0]),
        "format": str((query.get("format") or [""])[0]),
    }


def _is_essex_bulk(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    r = body.get("result")
    return isinstance(r, dict) and isinstance(r.get("floorplans"), list)


def parse_essex_bulk(
    body: dict[str, Any],
    source_url: str,
    *,
    property_id: str = "",
    property_name: str = "",
    page_url: str = "",
    page_final_url: str = "",
) -> list[dict[str, Any]]:
    """Bulk ``/availability?format=spa`` -> all unit-level dicts."""
    r = body.get("result")
    if not isinstance(r, dict):
        return []
    out: list[dict[str, Any]] = []
    for fp in r.get("floorplans") or []:
        if not isinstance(fp, dict):
            continue
        for u in fp.get("units") or []:
            if not isinstance(u, dict):
                continue
            native_unit_id = str(u.get("unit_id") or "").strip()
            unit_no = str(u.get("name") or native_unit_id).strip()
            if not unit_no:
                continue
            native_floorplan_id = str(
                u.get("floorplan_id")
                or fp.get("floorplan_id")
                or fp.get("id")
                or ""
            ).strip()
            fp_name = str(
                u.get("floorplan_name") or fp.get("name") or ""
            ).strip()
            beds_raw = u.get("beds")
            baths_raw = u.get("baths")
            try:
                beds = int(float(beds_raw)) if beds_raw not in (None, "") else None
            except (TypeError, ValueError):
                beds = None
            try:
                baths = float(baths_raw) if baths_raw not in (None, "") else None
            except (TypeError, ValueError):
                baths = None
            sqft_raw = u.get("sqft")
            sqft = str(sqft_raw) if sqft_raw not in (None, "", 0) else ""
            rent_lo = money_to_int(str(u.get("minimum_rent") or "")) or None
            rent_hi = money_to_int(str(u.get("maximum_rent") or "")) or None
            if rent_lo is None and rent_hi is not None:
                rent_lo = rent_hi
            if rent_hi is None and rent_lo is not None:
                rent_hi = rent_lo
            avail = str(u.get("availability_date") or "")
            avail = avail[:10] if "T" in avail else avail
            concession = ""
            sp = u.get("specials")
            if isinstance(sp, list) and sp:
                first = sp[0]
                concession = str(
                    first.get("title") or first.get("description") or ""
                ).strip() if isinstance(first, dict) else str(first).strip()
            fpl = u.get("floorplate") if isinstance(u.get("floorplate"), dict) else {}
            floor = fpl.get("floor")
            building = fpl.get("building_name")
            source_ids: dict[str, str] = {}
            if native_unit_id:
                source_ids["essex_unit_id"] = native_unit_id
            if native_floorplan_id:
                source_ids["essex_floorplan_id"] = native_floorplan_id
            if property_id:
                source_ids["essex_property_id"] = property_id
            unit = make_unit_dict(
                floor_plan_name=fp_name,
                bed_label=bed_label_from(beds, fp_name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=(
                    str(int(baths)) if baths is not None and baths == int(baths)
                    else (str(baths) if baths is not None else "")
                ),
                sqft=sqft,
                unit_number=unit_no,
                unit_name=unit_no,
                floor=str(floor) if floor not in (None, "") else "",
                building=str(building) if building not in (None, "") else "",
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi,
                concession=concession,
                availability_status="AVAILABLE",
                availability_date=avail,
                source_api_url=source_url,
                extraction_tier=_TIER,
                source_ids=source_ids or None,
            )
            if native_unit_id:
                unit["unit_id"] = native_unit_id
            if property_id:
                unit["source_property_id"] = property_id
                unit["source_property_provenance"] = (
                    "essex_configured_page.PropertyName+PropertyId"
                )
                unit["source_request_payload"] = _request_payload(
                    source_url, property_id
                )
            if property_name:
                unit["source_property_name"] = property_name
            if page_url:
                unit["source_page_url"] = sanitise_source_url(page_url)
            if page_final_url:
                unit["source_page_final_url"] = sanitise_source_url(page_final_url)
            unit["source_response_provenance"] = (
                "essex_bulk_availability.result.floorplans[].units[]"
            )
            out.append(unit)
    return out


def _decode_html(body: Any) -> str:
    if isinstance(body, bytes):
        try:
            return body.decode("utf-8", errors="replace")
        except Exception:
            return ""
    return body if isinstance(body, str) else ""


def _fresh_essex_page(ctx: AdapterContext, configured_url: str) -> _EssexPageEvidence:
    """One no-cache configured-page request; never uses a paid unlocker."""

    try:
        from ma_poc.pms.adapters._probe import probe_get

        response = probe_get(
            configured_url,
            timeout=20,
            unlocker=False,
            headers={"cache-control": "no-cache", "pragma": "no-cache"},
        )
        status = int(getattr(response, "status_code", 0) or 0)
        final_url = str(getattr(response, "url", "") or configured_url)
        return _classify_essex_page(
            _decode_html(getattr(response, "text", "")),
            configured_name=str(getattr(ctx, "property_name", "") or ""),
            requested_url=configured_url,
            final_url=final_url,
            status=status,
            via="essex_fresh_configured_page",
        )
    except Exception as exc:  # noqa: BLE001 — adapter boundary never raises
        return _classify_essex_page(
            "",
            configured_name=str(getattr(ctx, "property_name", "") or ""),
            requested_url=configured_url,
            final_url=configured_url,
            status=0,
            via="essex_fresh_configured_page",
            exception_class=type(exc).__name__,
        )


def _bulk_attempt(
    page_evidence: _EssexPageEvidence,
) -> tuple[dict[str, Any] | None, dict[str, Any], str]:
    """Fetch one exact page-derived bulk endpoint with structured telemetry."""

    pid = page_evidence.property_id
    d0 = date.today()
    d1 = d0 + timedelta(days=60)
    url = (
        f"https://www.essexapartmenthomes.com/api/properties/{pid}"
        f"/availability?start_date={d0}&end_date={d1}&format=spa"
    )
    telemetry: dict[str, Any] = {
        "url": url,
        "status": 0,
        "body": None,
        "via": "essex_active_bulk",
        "essex_outcome": "BULK_EXCEPTION",
        "page_url": page_evidence.requested_url,
        "page_final_url": page_evidence.final_url,
        "source_property_id": pid,
        "source_property_name": page_evidence.property_name,
        "exception_class": "",
        "response_shape": "",
        "response_sha256": "",
        "row_count": 0,
    }
    try:
        from ma_poc.pms.adapters._probe import probe_get

        response = probe_get(
            url,
            timeout=20,
            unlocker=False,
            headers={
                "accept": "application/json, text/plain, */*",
                "referer": page_evidence.final_url or page_evidence.requested_url,
                "cache-control": "no-cache",
            },
        )
        status = int(getattr(response, "status_code", 0) or 0)
        telemetry["status"] = status
        raw = getattr(response, "text", "")
        telemetry["response_sha256"] = response_sha256(raw) if raw else ""
        if status != 200:
            telemetry["essex_outcome"] = "BULK_HTTP_ERROR"
            return None, telemetry, "BULK_HTTP_ERROR"
        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            telemetry["exception_class"] = type(exc).__name__
            telemetry["essex_outcome"] = "BULK_JSON_ERROR"
            return None, telemetry, "BULK_JSON_ERROR"
        telemetry["response_sha256"] = response_sha256(body)
        if not _is_essex_bulk(body):
            telemetry["response_shape"] = (
                "dict:" + ",".join(sorted(str(k) for k in body)[:12])
                if isinstance(body, dict)
                else type(body).__name__
            )
            telemetry["essex_outcome"] = "BULK_SHAPE_REJECTED"
            return None, telemetry, "BULK_SHAPE_REJECTED"
        row_count = _bulk_raw_unit_count(body)
        outcome = "SUCCESS" if row_count else "BULK_EMPTY"
        telemetry.update(
            {
                "body": body,
                "response_shape": "essex_bulk",
                "row_count": row_count,
                "essex_outcome": outcome,
            }
        )
        source = {
            **telemetry,
            "property_id": pid,
            "property_name": page_evidence.property_name,
            "page_url": page_evidence.requested_url,
            "page_final_url": page_evidence.final_url,
        }
        return source, telemetry, outcome
    except Exception as exc:  # noqa: BLE001 — adapter boundary never raises
        telemetry["exception_class"] = type(exc).__name__
        return None, telemetry, "BULK_EXCEPTION"


def _captured_bulk_source(
    response: dict[str, Any],
    page_evidence: _EssexPageEvidence,
) -> tuple[dict[str, Any] | None, dict[str, Any], str]:
    """Property-bind one passively captured bulk response."""

    url = sanitise_source_url(str(response.get("url") or ""))
    body = response.get("body")
    match = _API_PROPERTY_ID_RE.search(url)
    response_pid = match.group(1) if match else ""
    shape = _is_essex_bulk(body)
    row_count = _bulk_raw_unit_count(body) if shape else 0
    status = int(response.get("status") or 200)
    if response_pid != page_evidence.property_id:
        outcome = "CAPTURED_PROPERTY_MISMATCH"
    elif not shape:
        outcome = "BULK_SHAPE_REJECTED"
    else:
        outcome = "SUCCESS" if row_count else "BULK_EMPTY"
    telemetry = {
        "url": url,
        "status": status,
        "body": body if shape and response_pid == page_evidence.property_id else None,
        "via": "essex_captured_bulk",
        "essex_outcome": outcome,
        "page_url": page_evidence.requested_url,
        "page_final_url": page_evidence.final_url,
        "source_property_id": page_evidence.property_id,
        "source_property_name": page_evidence.property_name,
        "exception_class": "",
        "response_shape": "essex_bulk" if shape else type(body).__name__,
        "response_sha256": response_sha256(body),
        "row_count": row_count,
    }
    if outcome not in {"SUCCESS", "BULK_EMPTY"}:
        return None, telemetry, outcome
    return (
        {
            **telemetry,
            "property_id": page_evidence.property_id,
            "property_name": page_evidence.property_name,
            "page_url": page_evidence.requested_url,
            "page_final_url": page_evidence.final_url,
        },
        telemetry,
        outcome,
    )


async def _active_fetch_essex_bulk(
    page: Page | None,
    ctx: AdapterContext,
    captured_responses: list[dict[str, Any]] | None = None,
) -> _EssexBulkResolution:
    """Resolve a source-bound property ID and one exact bulk roster.

    A retained/rendered source page is the initial attempt. An explicit 404
    shell, missing property boundary, or retryable bulk failure gets exactly
    one fresh configured-page/API cycle. No sibling URL or guessed property ID
    is ever tried.
    """

    configured_url = _canonical_page_url(str(getattr(ctx, "base_url", "") or ""))
    expected_name = str(getattr(ctx, "property_name", "") or "")
    resolution = _EssexBulkResolution(
        page_url=configured_url,
        page_final_url=configured_url,
    )
    fr = getattr(ctx, "fetch_result", None)
    retained_body = _decode_html(getattr(fr, "body", None))
    retained = _classify_essex_page(
        retained_body,
        configured_name=expected_name,
        requested_url=str(getattr(fr, "url", "") or configured_url),
        final_url=str(getattr(fr, "final_url", "") or configured_url),
        status=int(getattr(fr, "status", 0) or (200 if retained_body else 0)),
        via="essex_retained_page",
    )
    resolution.telemetry.append(_page_telemetry(retained))
    page_evidence = retained if retained.outcome == "SUCCESS" else None

    # A hydrated page can repair a sparse retained response without another
    # request. It does not consume the one bounded network retry.
    if page_evidence is None and page is not None:
        try:
            rendered_body = await page.content()
        except Exception:
            rendered_body = ""
        if rendered_body and response_sha256(rendered_body) != response_sha256(retained_body):
            rendered = _classify_essex_page(
                rendered_body,
                configured_name=expected_name,
                requested_url=configured_url,
                final_url=str(getattr(page, "url", "") or configured_url),
                status=200,
                via="essex_rendered_page",
            )
            resolution.telemetry.append(_page_telemetry(rendered))
            if rendered.outcome == "SUCCESS":
                page_evidence = rendered

    if page_evidence is None:
        page_evidence = _fresh_essex_page(ctx, configured_url)
        resolution.retry_used = True
        resolution.telemetry.append(_page_telemetry(page_evidence))
        if page_evidence.outcome != "SUCCESS":
            resolution.outcome = page_evidence.outcome
            return resolution

    resolution.property_id = page_evidence.property_id
    resolution.property_name = page_evidence.property_name
    resolution.page_url = page_evidence.requested_url
    resolution.page_final_url = page_evidence.final_url

    captured_failure = False
    for response in captured_responses or []:
        response_url = str(response.get("url") or "")
        if not (
            _is_essex_bulk(response.get("body"))
            or _API_PROPERTY_ID_RE.search(response_url)
        ):
            continue
        source, telemetry, outcome = _captured_bulk_source(response, page_evidence)
        resolution.telemetry.append(telemetry)
        if source is not None:
            resolution.sources.append(source)
            resolution.outcome = outcome
            return resolution
        captured_failure = True

    # A rejected captured response is itself the failed API attempt. Refresh
    # the configured page before the one allowed replacement API request.
    if captured_failure and not resolution.retry_used:
        page_evidence = _fresh_essex_page(ctx, configured_url)
        resolution.retry_used = True
        resolution.telemetry.append(_page_telemetry(page_evidence))
        if page_evidence.outcome != "SUCCESS":
            resolution.outcome = page_evidence.outcome
            return resolution
        resolution.property_id = page_evidence.property_id
        resolution.property_name = page_evidence.property_name
        resolution.page_url = page_evidence.requested_url
        resolution.page_final_url = page_evidence.final_url

    source, telemetry, outcome = _bulk_attempt(page_evidence)
    resolution.telemetry.append(telemetry)
    if source is not None:
        resolution.sources.append(source)
        resolution.outcome = outcome
        return resolution

    # Retained valid page + transient/invalid API: one fresh page/API retry.
    if outcome in _RETRYABLE_BULK_OUTCOMES and not resolution.retry_used:
        refreshed = _fresh_essex_page(ctx, configured_url)
        resolution.retry_used = True
        resolution.telemetry.append(_page_telemetry(refreshed))
        if refreshed.outcome != "SUCCESS":
            resolution.outcome = refreshed.outcome
            return resolution
        resolution.property_id = refreshed.property_id
        resolution.property_name = refreshed.property_name
        resolution.page_url = refreshed.requested_url
        resolution.page_final_url = refreshed.final_url
        source, telemetry, outcome = _bulk_attempt(refreshed)
        resolution.telemetry.append(telemetry)
        if source is not None:
            resolution.sources.append(source)

    resolution.outcome = outcome
    return resolution

# /api/properties/<pid>/units/<uid>/availability
_AVAIL_URL_RE = re.compile(
    r"/api/properties/\d+/units/\d+/availability", re.IGNORECASE
)
_MONEY_RE = re.compile(r"[\d.]+")


def _rent_to_int(val: Any) -> int | None:
    """``"2487.00"`` → 2487; junk/empty → None."""
    if val is None:
        return None
    m = _MONEY_RE.search(str(val))
    if not m:
        return None
    try:
        return int(round(float(m.group(0))))
    except (TypeError, ValueError):
        return None


def _is_essex_availability(body: Any, url: str) -> bool:
    if not isinstance(body, dict):
        return False
    if not (body.get("success") and isinstance(body.get("result"), dict)):
        return False
    r = body["result"]
    return "pricing_by_date" in r and "unit_id" in r


def build_unit_id_to_name_map(bulk_body: Any) -> dict[str, str]:
    """Build a ``{unit_id_str: displayed_name}`` map from an Essex bulk
    SPA response (the ``floorplans[*].units[*]`` shape). Used by the
    per-unit availability fallback to resolve the displayed unit name
    when the per-unit endpoint only carries ``unit_id``.

    2026-05-24: defensive code. The per-unit fallback fires when the
    bulk SPA path returns nothing usable but Playwright captured
    individual ``/api/properties/{pid}/units/{uid}/availability`` XHRs.
    Per-unit responses don't carry the ``name`` field; without this
    map the fallback would ship the 7-digit internal ``unit_id`` as
    ``unit_number``. Verified live 2026-05-24 across 10 Essex
    properties — the bulk path wins 100 % of the time today, but
    leaving the fallback un-hardened invites a future regression.
    """
    out: dict[str, str] = {}
    if not isinstance(bulk_body, dict):
        return out
    result = bulk_body.get("result")
    if not isinstance(result, dict):
        return out
    for fp in result.get("floorplans") or []:
        if not isinstance(fp, dict):
            continue
        for u in fp.get("units") or []:
            if not isinstance(u, dict):
                continue
            uid = u.get("unit_id")
            name = u.get("name")
            if uid is None or name in (None, ""):
                continue
            out[str(uid)] = str(name)
    return out


def parse_essex_availability(
    body: dict[str, Any],
    source_url: str,
    unit_id_to_name: dict[str, str] | None = None,
    *,
    property_id: str = "",
    property_name: str = "",
    page_url: str = "",
    page_final_url: str = "",
) -> list[dict[str, Any]]:
    """One Essex ``/availability`` response → at most one unit-level dict.

    The endpoint is per-unit. The canonical asking rent is the
    **12-month** term on the unit's earliest available date (the page
    headlines 12mo as "Best Value"; 1–3-month terms are inflated
    short-stay premiums and are NOT the asking rent). Availability date
    = the first ``pricing_by_date`` entry whose ``terms_by_month`` is
    non-empty (an empty list means the unit is not available that day).
    Returns [] when no date has any term (unit not currently available).

    2026-05-24: ``unit_id_to_name`` lets the caller pass a map built
    from the bulk SPA response so we can ship the displayed unit name
    (e.g. ``"G104"``) instead of the internal 7-digit ``unit_id``
    (e.g. ``"6302046"``). Falls back to ``str(unit_id)`` only when no
    mapping is available — preserving prior behaviour.
    """
    r = body.get("result")
    if not isinstance(r, dict):
        return []
    unit_id = r.get("unit_id")
    if unit_id in (None, "", 0):
        return []
    fp_id = r.get("floorplan_id")
    response_property_id = str(r.get("property_id") or "").strip()
    if property_id and response_property_id and response_property_id != property_id:
        return []
    source_property_id = property_id or response_property_id

    avail_iso = ""
    chosen_terms: list[dict[str, Any]] = []
    for entry in r.get("pricing_by_date") or []:
        if not isinstance(entry, dict):
            continue
        terms = entry.get("terms_by_month") or []
        if terms:
            avail_iso = str(entry.get("date") or "")[:10]
            chosen_terms = [t for t in terms if isinstance(t, dict)]
            break
    if not chosen_terms:
        return []

    # Prefer the 12-month term; else the longest available term (closest
    # to a standard lease, not a short-stay premium).
    by_term = {
        int(t.get("term_months") or 0): t
        for t in chosen_terms
        if t.get("term_months")
    }
    pick = by_term.get(12) or (
        by_term[max(by_term)] if by_term else chosen_terms[0]
    )
    rent = _rent_to_int(pick.get("rent"))
    if rent is None:
        return []
    deposit = pick.get("deposit")

    # Prefer the displayed unit name from the bulk-response map when
    # available; fall back to the 7-digit internal unit_id otherwise.
    display_unit_no = (
        (unit_id_to_name or {}).get(str(unit_id)) or str(unit_id)
    )

    native_unit_id = str(unit_id)
    source_ids = {"essex_unit_id": native_unit_id}
    if fp_id not in (None, ""):
        source_ids["essex_floorplan_id"] = str(fp_id)
    if source_property_id:
        source_ids["essex_property_id"] = source_property_id
    unit = make_unit_dict(
        unit_number=display_unit_no,
        unit_name=display_unit_no,
        floor_plan_name=str(fp_id or ""),
        rent_low=rent,
        rent_high=rent,
        deposit=str(deposit or ""),
        availability_status="AVAILABLE",
        availability_date=avail_iso,
        source_api_url=source_url,
        extraction_tier=_TIER,
        source_ids=source_ids,
    )
    unit["unit_id"] = native_unit_id
    if source_property_id:
        unit["source_property_id"] = source_property_id
        unit["source_property_provenance"] = (
            "essex_configured_page.PropertyName+PropertyId"
            if property_id
            else "essex_unit_availability.result.property_id"
        )
    if property_name:
        unit["source_property_name"] = property_name
    if page_url:
        unit["source_page_url"] = sanitise_source_url(page_url)
    if page_final_url:
        unit["source_page_final_url"] = sanitise_source_url(page_final_url)
    unit["source_response_provenance"] = "essex_unit_availability.result"
    return [unit]


class EssexAdapter:
    """Essex ``/api/properties/{id}/units/{id}/availability`` extractor.

    Browser-intercept only (Vercel-bot-gated): parses the per-unit
    availability responses the rendered floor-plans-and-pricing page
    fires, from ``ctx._api_responses``. One unit per response; dedup by
    unit_id across the captured set.
    """

    pms_name: str = "essex"
    _fingerprints: list[str] = [
        "essexapartmenthomes.com",
        "/api/properties/",
    ]

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    def matches_response_body(self, body: Any) -> bool:
        return _is_essex_availability(body, "") or _is_essex_bulk(body)

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_TIER)
        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])

        # Primary: page-bound bulk /availability?format=spa. The resolver
        # validates passive captures against the configured community and
        # performs at most one fresh page/API recovery cycle.
        resolution = await _active_fetch_essex_bulk(page, ctx, api_responses)
        result.api_responses.extend(resolution.telemetry)
        bulk_sources = resolution.sources
        if bulk_sources:
            bulk_units: list[dict[str, Any]] = []
            for src in bulk_sources:
                try:
                    parsed = parse_essex_bulk(
                        src["body"],
                        src.get("url", ""),
                        property_id=str(src.get("property_id") or ""),
                        property_name=str(src.get("property_name") or ""),
                        page_url=str(src.get("page_url") or ""),
                        page_final_url=str(src.get("page_final_url") or ""),
                    )
                    bulk_units.extend(parsed)
                    if parsed:
                        result.unit_source_provenance.append(
                            build_unit_source_provenance(
                                provider="essex",
                                source_url=str(src.get("url") or ""),
                                body=src.get("body"),
                                unit_count=len(parsed),
                                identity={
                                    "property_id": str(src.get("property_id") or ""),
                                    "property_name": str(src.get("property_name") or ""),
                                    "configured_property_id": str(
                                        getattr(ctx, "property_id", "") or ""
                                    ),
                                    "configured_property_name": str(
                                        getattr(ctx, "property_name", "") or ""
                                    ),
                                    "page_url": str(src.get("page_url") or ""),
                                    "page_final_url": str(
                                        src.get("page_final_url") or ""
                                    ),
                                },
                                status=int(src.get("status") or 200),
                            )
                        )
                except Exception as exc:  # noqa: BLE001 — adapters never raise
                    result.errors.append(
                        f"essex-bulk-parse-error: {type(exc).__name__}: {exc}"
                    )
            if bulk_units:
                result.units = bulk_units
                result.winning_url = bulk_sources[0].get("url") or None
                result.confidence = min(0.90, 0.7 + 0.05 * len(bulk_units))
                return result

        # 2026-05-24: build a unit_id → displayed-name map from ANY
        # bulk-shape response we've captured (passively or via active
        # fetch). The per-unit /availability endpoint only carries
        # unit_id, so without this map the fallback ships the 7-digit
        # internal id as unit_number. Even when bulk had no units to
        # parse (e.g. zero current availability + Playwright captured
        # individual per-unit XHRs from a stale state), the floorplan
        # list often still carries the unit_id→name pairs we need.
        unit_id_to_name: dict[str, str] = {}
        for rsp in api_responses:
            b = rsp.get("body")
            if _is_essex_bulk(b):
                unit_id_to_name.update(build_unit_id_to_name_map(b))
        for src in bulk_sources:
            unit_id_to_name.update(build_unit_id_to_name_map(src.get("body")))

        all_units: list[dict[str, Any]] = []
        seen: set[str] = set()
        per_unit_candidates = 0
        per_unit_property_mismatches = 0
        first_per_unit_url = ""
        for resp in api_responses:
            body = resp.get("body")
            url = str(resp.get("url", ""))
            if not (_AVAIL_URL_RE.search(url) or _is_essex_availability(body, url)):
                continue
            if not isinstance(body, dict):
                continue
            per_unit_candidates += 1
            response_pid = str((body.get("result") or {}).get("property_id") or "")
            if (
                resolution.property_id
                and response_pid
                and response_pid != resolution.property_id
            ):
                per_unit_property_mismatches += 1
                continue
            # Never accept a per-unit capture when the configured source page
            # failed to establish the property boundary.
            if not resolution.property_id:
                per_unit_property_mismatches += 1
                continue
            try:
                units = parse_essex_availability(
                    body,
                    url,
                    unit_id_to_name,
                    property_id=resolution.property_id,
                    property_name=resolution.property_name,
                    page_url=resolution.page_url,
                    page_final_url=resolution.page_final_url,
                )
            except Exception as exc:  # noqa: BLE001 — never raise from an adapter
                result.errors.append(f"essex-parse-error: {type(exc).__name__}: {exc}")
                continue
            for u in units:
                key = str(u.get("unit_id") or u.get("unit_number") or "")
                if key and key not in seen:
                    seen.add(key)
                    all_units.append(u)
                    result.api_responses.append(resp)
                    first_per_unit_url = first_per_unit_url or url
                    result.unit_source_provenance.append(
                        build_unit_source_provenance(
                            provider="essex",
                            source_url=url,
                            body=body,
                            unit_count=1,
                            identity={
                                "property_id": resolution.property_id,
                                "property_name": resolution.property_name,
                                "configured_property_id": str(
                                    getattr(ctx, "property_id", "") or ""
                                ),
                                "page_url": resolution.page_url,
                                "page_final_url": resolution.page_final_url,
                            },
                            status=int(resp.get("status") or 200),
                            response_kind="unit_availability",
                        )
                    )

        if all_units:
            result.units = all_units
            result.winning_url = first_per_unit_url or None
            result.confidence = min(0.90, 0.7 + 0.05 * len(all_units))
        else:
            result.confidence = 0.0
            outcome = resolution.outcome
            if bulk_sources and any(_bulk_raw_unit_count(src.get("body")) for src in bulk_sources):
                outcome = "BULK_PARSE_EMPTY"
            elif per_unit_candidates and per_unit_property_mismatches == per_unit_candidates:
                outcome = "PER_UNIT_PROPERTY_MISMATCH"
            elif outcome == "SUCCESS":
                outcome = "PER_UNIT_PARSE_EMPTY" if per_unit_candidates else "BULK_PARSE_EMPTY"
            result.tier_used = f"{_TIER}_{outcome}"
            compact = [
                {
                    "via": item.get("via"),
                    "outcome": item.get("essex_outcome"),
                    "status": item.get("status"),
                    "url": item.get("url"),
                    "page_final_url": item.get("page_final_url"),
                    "property_id": item.get("source_property_id"),
                    "exception_class": item.get("exception_class"),
                    "response_shape": item.get("response_shape"),
                    "response_sha256": item.get("response_sha256"),
                    "row_count": item.get("row_count"),
                }
                for item in resolution.telemetry
            ]
            result.errors.append(
                f"ESSEX_EMPTY_OUTCOME={outcome} telemetry="
                f"{json.dumps(compact, sort_keys=True, separators=(',', ':'))}"
            )
        return result
