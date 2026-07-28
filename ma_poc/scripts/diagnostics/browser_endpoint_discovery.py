"""Cohort-wide browser/XHR discovery for arbitrary multifamily websites.

Acceptance criteria (2026-07-27 cohort-wide endpoint discovery):
* Consume only route-plan records created from the exact requested cohort.
* Use a property-isolated browser context and Bright residential proxy for
  each property by default; never share cookies or browser state across
  properties.  An explicitly requested local validation may instead use this
  device's direct outbound IP, which is recorded in the checkpoint.
* Capture real browser XHR/fetch responses after public availability controls
  are opened, and accept API proof only for a unit id plus numeric rent in one
  returned row.
* Keep SSR DOM proof, public plan-price evidence, API proof, and access blocks
  distinct. Persist only a strict, durable API/DOM result through a GCS
  generation guard; never store response bodies, cookies, or static dates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from google.cloud import storage  # type: ignore[import-untyped]

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.fetch.browser_pool import BrowserContextPool
from ma_poc.fetch.headers import chrome_header_set
from ma_poc.fetch.http_client import make_http_client
from ma_poc.fetch.proxy.base import ProxyConfig, ProxyTier
from ma_poc.fetch.proxy.brightdata import BrightDataProvider
from ma_poc.fetch.stealth import IdentityPool
from ma_poc.models.fetch_tier import FetchTier
from ma_poc.pms.adapters._api_parser import parse_api_responses
from ma_poc.pms.adapters._html_extract import extract_jsonld_from_html, extract_units_from_dom
from ma_poc.pms.adapters._parsing import parse_rent_range
from ma_poc.pms.adapters._pms_portal_hop import _to_rentcafe_availableunits
from ma_poc.pms.adapters._probe import web_unlocker_call_count, web_unlocker_get
from ma_poc.pms.adapters._prospectportal_warm_replay import (
    expand_endpoint_template,
    extract_floorplan_ids,
)
from ma_poc.pms.adapters.entrata import (
    parse_entrata_pp_unit_cards,
    parse_prospectportal_unit_spaces,
)
from ma_poc.pms.adapters.rentcafe import parse_securecafe_availableunits
from ma_poc.pms.adapters.residentservices365 import parse_rs365_unit_blocks
from ma_poc.pms.adapters.resman import _extract_unittypes, parse_resman_unittypes
from ma_poc.scripts.diagnostics.cohort_endpoint_route_plan import (
    DiscoveryRoute,
    RoutePlanRecord,
    _parse_gcs_uri,
)
from ma_poc.services.endpoint_discovery_profiles import (
    DiscoveryClassification,
    DiscoveryEvidence,
    durable_endpoint_template,
    persist_generation_guarded,
)

_WORKFLOW_VERSION = "browser-endpoint-discovery-v8"
_MAX_CAPTURED_RESPONSES = 80
_MAX_CAPTURE_BODY_BYTES = 1_000_000
_CAPTURE_BODY_TIMEOUT_SECONDS = 3.0
_MAX_CONTROLS = 60
_MAX_ANCHOR_HREFS = 250
_MAX_CLICKS = 5
_MAX_DETAIL_PAGE_DRILLS = 3
_MAX_PUBLIC_PORTAL_LINKS = 4
_MAX_UNLOCKER_PUBLIC_DRILLS = 3
_HREF_RE = re.compile(r"<a\b[^>]*?\bhref\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_CONTROL_TEXT_RE = re.compile(
    r"(?:check|view|see|show)\s+(?:availability|available|units?)|"
    r"available\s+(?:units?|apartments?)|availability|floor\s*plans?|pricing",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_KEYS = frozenset({"token", "api_key", "apikey", "auth", "signature", "sig", "cookie"})
_EXPLICIT_UNIT_ID_KEYS = frozenset(
    {
        "unitnumber",
        "unit_number",
        "unitid",
        "unit_id",
        "apartmentnumber",
        "apartment_number",
        "apartmentname",
        "apartment_name",
        "unitcode",
        "unit_code",
    }
)


@dataclass(frozen=True, slots=True)
class BrowserEndpointProbeResult:
    """Sanitized outcome of one isolated browser discovery attempt."""

    warm_status: int | None
    classification: DiscoveryClassification
    warm_page_url: str | None = None
    strict_api_rows: tuple[dict[str, Any], ...] = ()
    strict_dom_rows: tuple[dict[str, Any], ...] = ()
    dom_proof_page_url: str | None = None
    dom_roster_scope: str = "DOM_SCOPE_UNPROVEN"
    endpoint_url: str | None = None
    controls_clicked: int = 0
    controls_matched: int = 0
    frames_seen: int = 0
    max_dom_rows_seen: int = 0
    networkidle_reached: bool = False
    xhr_total_seen: int = 0
    api_responses_considered: int = 0
    capture_truncated: bool = False
    bodies_dropped_oversize: int = 0
    forms_present: bool = False
    navigation_levels_reached: int = 0
    plan_rows_observed: int = 0
    plan_rows_with_numeric_price: int = 0
    plan_price_low: int | None = None
    plan_price_high: int | None = None
    warm_urls_tried: tuple[str, ...] = ()
    detail_urls_tried: tuple[str, ...] = ()
    public_portal_links_observed: tuple[str, ...] = ()
    observed_xhr_paths: tuple[str, ...] = ()
    blocked_public_paths: tuple[str, ...] = ()
    web_unlocker_attempted_paths: tuple[str, ...] = ()
    web_unlocker_success_paths: tuple[str, ...] = ()
    web_unlocker_budget_exhausted: bool = False
    known_endpoint_replay_status: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class WebUnlockerRevalidation:
    """Sanitized public-page evidence returned by the capped unlocker fallback."""

    attempted_paths: tuple[str, ...] = ()
    success_paths: tuple[str, ...] = ()
    strict_rows: tuple[dict[str, Any], ...] = ()
    proof_page_url: str | None = None
    plan_rows_observed: int = 0
    plan_rows_with_numeric_price: int = 0
    plan_price_low: int | None = None
    plan_price_high: int | None = None
    floorplan_ids: tuple[str, ...] = ()
    budget_exhausted: bool = False


@dataclass(frozen=True, slots=True)
class KnownEndpointReplay:
    """Strict direct-endpoint evidence fetched on a sticky residential client."""

    endpoint_template: str | None = None
    strict_rows: tuple[dict[str, Any], ...] = ()
    response_status: int | None = None


def is_availability_control(text: str) -> bool:
    """Return whether visible control text is a safe availability exploration action."""
    return bool(_CONTROL_TEXT_RE.search(text or ""))


def availability_control_indexes(texts: list[str]) -> list[int]:
    """Return bounded DOM positions whose labels expose public inventory.

    Bulk-reading control labels avoids one round trip and one-second timeout
    per element.  A site with several embedded widgets can otherwise consume
    an entire property budget while merely inspecting unrelated navigation.
    """
    return [index for index, text in enumerate(texts[:_MAX_CONTROLS]) if is_availability_control(text)]


def is_inventory_frame_url(frame_url: str, page_url: str) -> bool:
    """Return whether an embedded frame is a plausible public inventory surface.

    Marketing pages frequently embed chat, analytics, and blank support
    frames. Parsing every one repeats expensive full-document extraction and
    can hide a simple same-site floor-plan route behind the property timeout.
    The top-level page is inspected separately; this filter preserves same-
    origin floor-plan frames and the known public PMS inventory hosts.
    """
    frame = urlparse(frame_url)
    page = urlparse(page_url)
    if frame.scheme not in {"http", "https"} or not frame.netloc:
        return False
    if frame_url.rstrip("/") == page_url.rstrip("/"):
        return False
    text = f"{frame.netloc}{frame.path}".lower()
    if any(
        marker in text
        for marker in (
            "appfolio.com",
            "onlineleasing",
            "prospectportal.com",
            "securecafe",
            "rentcafe",
            "myresman.com",
            "managebuilding.com",
            "entrata",
            "realpage",
            "availability",
            "available",
            "floorplan",
            "floor-plan",
            "listing",
            "units",
        )
    ):
        return True
    return frame.netloc == page.netloc and frame.path not in {"", "/"}


async def bounded_response_body(response: Any) -> bytes | None:
    """Read one captured XHR body without letting a stream hold the property open.

    Analytics and chat widgets sometimes keep an XHR/fetch response pending
    indefinitely. They are not inventory evidence, so dropping an unfinished
    body is safer than converting an otherwise complete property probe into a
    timeout. Completed JSON responses still receive the normal strict parser.
    """
    try:
        payload = await asyncio.wait_for(
            response.body(), timeout=_CAPTURE_BODY_TIMEOUT_SECONDS
        )
    except Exception:
        return None
    return payload if isinstance(payload, bytes) else None


async def wait_for_network_settle(page: Any, *, timeout_ms: int = 8_000) -> bool:
    """Best-effort wait for SPA activity after navigation or an inventory click.

    A browser ``goto`` cancels in-flight XHR requests when the next route is
    opened.  Waiting for ``networkidle`` before we move on lets slow public
    availability calls complete, especially through a residential proxy.  It
    is deliberately non-fatal: persistent analytics traffic must not turn a
    valid public route into a failed property.
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        return True
    except Exception:
        return False


async def wait_for_inventory_settle(page: Any, *, timeout_ms: int = 8_000) -> bool:
    """Allow nested public inventory widgets a short post-idle render window.

    ``networkidle`` is necessary to avoid aborting slow XHR, but an iframe can
    be inserted immediately after it resolves and populate its listings on a
    subsequent render tick.  The bounded pause is deliberately short and runs
    only after a real navigation or availability interaction.
    """
    networkidle_reached = await wait_for_network_settle(page, timeout_ms=timeout_ms)
    try:
        await page.wait_for_timeout(1_500)
    except Exception:
        pass
    return networkidle_reached


def is_public_detail_drill_url(url: str, warm_url: str) -> bool:
    """Allow only same-site public pricing or floor-plan detail navigation.

    A plan-card link often contains the only rendered unit table or triggers
    the availability XHR.  Application, resident and authentication paths are
    intentionally excluded: this worker is limited to public inventory.
    """
    parsed = urlparse(url)
    warm = urlparse(warm_url)
    path = parsed.path.lower()
    if parsed.scheme not in {"http", "https"} or parsed.netloc != warm.netloc:
        return False
    if parsed.fragment:
        return False
    if url.rstrip("/") == warm_url.rstrip("/"):
        return False
    if path.endswith((".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")):
        return False
    if any(marker in path for marker in ("apply", "lease", "resident", "login", "authentication")):
        return False
    return any(
        marker in path
        for marker in (
            "floorplan",
            "floor-plan",
            "availability",
            "available",
            "pricing",
            "unit",
            "apartment",
            "conventional",
            "models",
        )
    )


def is_public_portal_link(url: str, warm_url: str) -> bool:
    """Identify an observed public PMS portal link without constructing URLs."""
    parsed = urlparse(url)
    warm = urlparse(warm_url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.netloc == warm.netloc:
        return False
    if any(marker in path for marker in ("application_authentication", "resident", "login")):
        return False
    return any(
        marker in host
        for marker in (
            "prospectportal.com",
            "securecafe.com",
            "securecafeapplicant.com",
            "rentcafe.com",
            "myresman.com",
            "appfolio.com",
            "onlineleasing.realpage.com",
        )
    )


def portal_origin(url: str) -> str | None:
    """Reduce an observed public PMS link to a public origin for visible navigation."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/"


def public_portal_navigation_url(url: str) -> str | None:
    """Return the safest observed public portal navigation target.

    Most portals are best rediscovered from their public origin.  RealPage
    Online Leasing uses a public ``#k=<property-key>`` fragment to select the
    property inside a shared SPA. SecureCafe's public floor-plan handoff uses
    an observed ``/onlineleasing/.../oleapplication.aspx?stepname=floorplan``
    route, which redirects to its Applicant roster; reducing either form to
    the origin loses the property and inventory context. Preserve only those
    observed, non-sensitive inventory routes exactly; all other vendors retain
    the origin-only behavior.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query_values = {key.lower(): value.lower() for key, value in query}
    has_sensitive_query = any(key.lower() in _SENSITIVE_QUERY_KEYS for key, _ in query)
    if host.endswith(".onlineleasing.realpage.com"):
        return url
    # ResMan's ``a`` and ``p`` parameters are public property identifiers,
    # not transient browser state. Reducing this observed availability link to
    # its origin loses the property selection altogether, which in turn makes
    # a date-scoped applicant roster impossible to validate.
    if (
        host.endswith(".myresman.com")
        and path == "/portal/applicants/availability"
        and {"a", "p"}.issubset(query_values)
        and not has_sensitive_query
    ):
        return url
    if not has_sensitive_query and host.endswith((".securecafe.com", ".securecafeapplicant.com")):
        is_securecafe_floorplan_handoff = (
            host.endswith(".securecafe.com")
            and "/onlineleasing/" in path
            and query_values.get("stepname") in {"floorplan", "floorplans", "availability", "availableunits"}
        )
        is_applicant_inventory_path = (
            host.endswith(".securecafeapplicant.com")
            and "/onlineleasing/" in path
            and any(marker in path for marker in ("floorplan", "availability", "available", "unit"))
        )
        if is_securecafe_floorplan_handoff or is_applicant_inventory_path:
            return url
    return portal_origin(url)


def resman_date_scoped_url(url: str, roster_date: date | None) -> str:
    """Apply a transient run-date filter to one observed ResMan portal URL.

    The public ResMan applicant page accepts ``moveInDate`` plus
    ``refreshPricing=true``.  Those parameters select the renter-visible
    roster and must be used for browser validation, but are deliberately not
    durable profile state.  Non-ResMan URLs and ordinary discovery calls are
    returned unchanged.
    """
    if roster_date is None:
        return url
    parsed = urlparse(url)
    if (
        not parsed.netloc.lower().endswith(".myresman.com")
        or parsed.path.lower() != "/portal/applicants/availability"
    ):
        return url
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in {"moveindate", "refreshpricing"}
    ]
    query.extend(
        (
            ("moveInDate", f"{roster_date.month}/{roster_date.day}/{roster_date.year}"),
            ("refreshPricing", "true"),
        )
    )
    return urlunparse(parsed._replace(query=urlencode(query)))


def without_resman_date_scope(url: str) -> str:
    """Remove transient ResMan roster-date selectors before checkpointing a URL."""
    parsed = urlparse(url)
    if not parsed.netloc.lower().endswith(".myresman.com"):
        return url
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in {"moveindate", "refreshpricing"}
    ]
    return urlunparse(parsed._replace(query=urlencode(query)))


def sanitized_xhr_path(url: str) -> str | None:
    """Return an auditable origin/path only; omit queries, cookies, and dates."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def public_route_key(url: str) -> str | None:
    """Canonicalize a public route for bounded navigation deduplication.

    Fragments and queries are not a distinct discovery target.  Keeping this
    separate from the URL actually fetched preserves a ResMan property query
    when needed, while preventing paid unlocker calls for the same document.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"


def is_safe_endpoint_template(url: str | None) -> bool:
    """Reject public URLs that carry credentials or a non-durable request date."""
    if not durable_endpoint_template(url):
        return False
    assert url is not None
    try:
        query = parse_qsl(urlparse(url).query, keep_blank_values=True)
    except ValueError:
        return False
    return not any(key.lower() in _SENSITIVE_QUERY_KEYS for key, _ in query)


def strict_api_proof(
    responses: list[dict[str, Any]], property_id: str
) -> tuple[str | None, tuple[dict[str, Any], ...]]:
    """Return the first captured endpoint with strict unit-plus-rent evidence."""
    for response in responses:
        url = str(response.get("url") or "")
        body = response.get("body")
        if not url or not isinstance(body, (dict, list)):
            continue
        if is_floorplan_aggregate_response(url, body):
            continue
        try:
            rows = parse_api_responses([{"url": url, "body": body}], property_id=property_id)
        except Exception:
            continue
        strict_rows = strict_listing_rows(rows)
        if strict_rows:
            return url, tuple(strict_rows)
    return None, ()


def is_floorplan_aggregate_response(url: str, body: dict[str, Any] | list[Any]) -> bool:
    """Identify a plan-summary endpoint that lacks real per-unit evidence.

    A generic API parser can legitimately use a row's ``id`` as a fallback
    identity. On a ``/floorplans`` endpoint that id normally denotes the
    shared plan, not an apartment. Such rows are useful plan-price evidence,
    but must never satisfy strict unit/API proof. A nested ``units`` collection
    or an explicit unit-id field keeps the endpoint eligible.
    """
    path = urlparse(url).path.lower()
    if "floorplan" not in path or "unit" in path:
        return False
    return not raw_body_has_explicit_unit_identity(body)


def raw_body_has_explicit_unit_identity(value: Any, *, parent_key: str = "") -> bool:
    """Return whether a JSON response exposes a concrete unit identity field.

    The recursive check accepts an explicit apartment/unit field anywhere in
    the payload. It also accepts a generic ``id`` only when it occurs inside a
    collection explicitly named ``units``—a bounded exception for vendors
    whose unit objects use a database id rather than ``unitNumber``.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).replace("-", "_").lower()
            if normalized in _EXPLICIT_UNIT_ID_KEYS and item not in (None, ""):
                return True
            if normalized == "id" and "unit" in parent_key.lower() and item not in (None, ""):
                return True
            if raw_body_has_explicit_unit_identity(item, parent_key=normalized):
                return True
    elif isinstance(value, list):
        return any(raw_body_has_explicit_unit_identity(item, parent_key=parent_key) for item in value)
    return False


def api_floorplan_plan_rows(responses: list[dict[str, Any]], property_id: str) -> list[dict[str, Any]]:
    """Convert unambiguous API floorplan aggregates into non-unit plan evidence."""
    plan_rows: list[dict[str, Any]] = []
    for response in responses:
        url = str(response.get("url") or "")
        body = response.get("body")
        if not url or not isinstance(body, (dict, list)):
            continue
        if not is_floorplan_aggregate_response(url, body):
            continue
        try:
            parsed = parse_api_responses([{"url": url, "body": body}], property_id=property_id)
        except Exception:
            continue
        for row in parsed:
            if not isinstance(row, dict):
                continue
            plan = dict(row)
            plan_id = str(plan.get("unit_number") or "").strip()
            plan["unit_number"] = ""
            if plan_id:
                source_ids = dict(plan.get("source_ids") or {})
                source_ids["api_floorplan_id"] = plan_id
                plan["source_ids"] = source_ids
            plan_rows.append(plan)
    return plan_rows


def strict_listing_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep concrete units with a numeric rent, including normalized rent ranges.

    Some mature project parsers intentionally emit ``rent_range`` for generic
    APIs instead of a numeric market-rent field. This bridge accepts that
    already-normalized range only after parsing a number from it; it never
    accepts a plan row, an inferred ID, or non-numeric ``Call for pricing``.
    """
    strict: list[dict[str, Any]] = []
    for row in rows:
        unit_number = str(row.get("unit_number") or "").strip()
        if not unit_number or unit_number.startswith("inferred_") or not unit_has_real_anchor(row):
            continue
        rent = row.get("market_rent_low", row.get("asking_rent"))
        if isinstance(rent, (int, float)) and not isinstance(rent, bool):
            strict.append(row)
            continue
        low, high = parse_rent_range(str(row.get("rent_range") or ""))
        if low is None and high is None:
            continue
        normalized = dict(row)
        normalized["market_rent_low"] = low if low is not None else high
        normalized["market_rent_high"] = high if high is not None else low
        strict.append(normalized)
    return strict


def public_plan_pricing(rows: list[dict[str, Any]]) -> tuple[int, int, int | None, int | None]:
    """Summarize inferred/blank-id plan cards without treating them as units."""
    plans = 0
    priced = 0
    values: list[int] = []
    for row in rows:
        unit_number = str(row.get("unit_number") or "").strip()
        if unit_number and not unit_number.startswith("inferred_"):
            continue
        if not any(str(row.get(key) or "").strip() for key in ("floor_plan_name", "bedrooms", "sqft")):
            continue
        plans += 1
        row_values: list[int] = []
        for key in ("market_rent_low", "market_rent_high", "asking_rent"):
            value = row.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                row_values.append(int(value))
        if not row_values:
            low, high = parse_rent_range(str(row.get("rent_range") or ""))
            if low is not None:
                row_values.append(low)
            if high is not None and high != low:
                row_values.append(high)
        if row_values:
            priced += 1
            values.extend(row_values)
    return plans, priced, min(values) if values else None, max(values) if values else None


def classify_non_strict_discovery(
    *,
    plan_rows_observed: int,
    blocked_public_paths: tuple[str, ...],
    unlocker_success_paths: tuple[str, ...],
    unlocker_budget_exhausted: bool = False,
) -> DiscoveryClassification:
    """Classify a probe with no strict API or DOM proof.

    A visible marketing plan card does not prove that the property has no
    public unit roster.  When an observed availability/portal route remains
    blocked after the bounded Unlocker attempt, retain ``ACCESS_BLOCKED`` and
    record plan evidence separately.  ``PUBLIC_PLAN_ONLY`` is reserved for
    properties whose attempted public routes were reachable (or recovered)
    yet showed only floor-plan-level pricing.  A global Unlocker cap can stop
    a route walk midway through its observed public links; that is incomplete
    discovery evidence, never a plan-only conclusion.
    """
    if unlocker_budget_exhausted:
        return DiscoveryClassification.API_NOT_FOUND_YET
    recovered = set(unlocker_success_paths)
    unresolved_blocks = [path for path in blocked_public_paths if path not in recovered]
    if unresolved_blocks:
        return DiscoveryClassification.ACCESS_BLOCKED
    if plan_rows_observed:
        return DiscoveryClassification.PUBLIC_PLAN_ONLY
    return DiscoveryClassification.API_NOT_FOUND_YET


def _warm_urls(record: RoutePlanRecord) -> list[str]:
    """Rank public warm documents from most to least useful for this route.

    Saved profiles can contain a PDF, an expired application-authentication
    URL, and a real marketing or portal page together.  The discovery worker
    must not blindly choose the profile's first URL: doing so turns already
    public unit tables into false access blocks.  This selector rejects
    non-HTML/stateful paths, then prioritises the public document that the
    route's deterministic parser or browser drill can actually consume.
    """
    ranked: list[tuple[int, int, str]] = []
    for position, candidate in enumerate(record.public_url_candidates):
        parsed = urlparse(candidate)
        path = parsed.path.lower()
        query = {key.lower(): value.lower() for key, value in parse_qsl(parsed.query)}
        path_and_query = f"{path}?{parsed.query}".lower()
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or "/api/" in path
            or path.endswith((".pdf", ".png", ".jpg", ".jpeg", ".webp"))
            or "application_authentication" in path_and_query
            or "kill_session" in path_and_query
            or any(value == "undefined" for value in query.values())
        ):
            continue
        host = parsed.netloc.lower()
        score = 0
        if record.route == DiscoveryRoute.RESMAN_PORTAL_DISCOVERY:
            if "myresman.com" in host and "/portal/applicants/availability" in path:
                score += 100
        elif record.route == DiscoveryRoute.RENTCAFE_PORTAL_DISCOVERY:
            if (
                any(marker in host for marker in ("securecafe.com", "rentcafe.com"))
                and "/onlineleasing/" in path
            ):
                score += 100
        elif record.route == DiscoveryRoute.ENTRATA_BROWSER_XHR_DISCOVERY:
            if any(marker in path for marker in ("conventional", "floorplan", "availability", "pricing")):
                score += 80
        elif record.route == DiscoveryRoute.APPFOLIO_BROWSER_XHR_DISCOVERY:
            if "/listings" in path or "appfolio.com" in host:
                score += 80
        if any(
            marker in path
            for marker in (
                "availability",
                "available",
                "floorplan",
                "floor-plan",
                "pricing",
                "units",
                "rentals",
                "listings",
                "conventional",
            )
        ):
            score += 20
        ranked.append((score, -position, candidate))
    ranked.sort(reverse=True)
    selected_urls: list[str] = []
    for _, _, selected in ranked:
        normalized = (
            str(_to_rentcafe_availableunits(selected))
            if record.route == DiscoveryRoute.RENTCAFE_PORTAL_DISCOVERY
            else selected
        )
        if normalized not in selected_urls:
            selected_urls.append(normalized)
    return selected_urls


def _warm_url(record: RoutePlanRecord) -> str | None:
    """Return the highest-ranked public warm document for compatibility callers."""
    candidates = _warm_urls(record)
    return candidates[0] if candidates else None


def portal_rows_from_html(record: RoutePlanRecord, html: str, source_url: str) -> list[dict[str, Any]]:
    """Parse an SSR inventory table before generic DOM heuristics run.

    ResMan, SecureCafe, and Apollo/365 ResidentServices publish public
    data-bearing pages whose structure is deliberately unlike the generic
    marketing DOM. Calling their existing deterministic parsers from the
    same sticky browser session prevents a unit roster from being
    misclassified as ``PUBLIC_PLAN_ONLY``.
    """
    # Apollo/365 ResidentServices renders the public roster server-side at a
    # per-plan ``/Marketing/FloorPlans/Units/{guid}`` URL.  It is an SSR
    # inventory surface even if the historical profile did not identify the
    # vendor, so dispatch on its concrete markup rather than the generic
    # discovery lane.  ``parse_rs365_unit_blocks`` deliberately requires a
    # data unit code or an "Apartment/Apt/Unit <id>" heading: it cannot turn a
    # bare floor-plan name into a synthetic unit (RentCafe identity guard).
    parsed: list[dict[str, Any]] = []
    if "/marketing/floorplans/units/" in source_url.lower() and "unit-details" in html.lower():
        parsed.extend(parse_rs365_unit_blocks(html, source_url))
    is_resman_portal = (
        urlparse(source_url).netloc.lower().endswith(".myresman.com")
        and urlparse(source_url).path.lower() == "/portal/applicants/availability"
    )
    if record.route == DiscoveryRoute.RESMAN_PORTAL_DISCOVERY or is_resman_portal:
        unittypes = _extract_unittypes(html)
        parsed.extend(parse_resman_unittypes(unittypes, source_url) if unittypes else [])
    elif record.route == DiscoveryRoute.RENTCAFE_PORTAL_DISCOVERY:
        parsed.extend(parse_securecafe_availableunits(html, source_url))
    return [dict(row) for row in parsed if isinstance(row, dict)]


def is_safe_public_unlocker_url(url: str) -> bool:
    """Accept a public inventory URL for a transient unlocker fetch only.

    The URL is never persisted or emitted with its query.  We still reject
    authentication/application paths and sensitive query parameters so an
    observed public link cannot turn the discovery fallback into a session or
    credential replay mechanism.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    path = parsed.path.lower()
    if any(marker in path for marker in ("application_authentication", "resident", "login")):
        return False
    try:
        query = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError:
        return False
    return not any(key.lower() in _SENSITIVE_QUERY_KEYS for key, _ in query)


def unlocked_public_links(html: str, source_url: str) -> list[str]:
    """Find bounded public inventory links from an unlocker HTML response.

    This is intentionally a source-derived navigation queue, not URL
    construction: same-host floor-plan links are followed directly and an
    observed external PMS link is reduced to its public origin only when its
    source path does not itself identify an inventory page. RealPage's public
    property fragment is retained because it is required to select inventory.
    """
    links: list[str] = []
    for href in _HREF_RE.findall(html):
        candidate = urljoin(source_url, href)
        if is_public_detail_drill_url(candidate, source_url):
            selected = candidate
        elif is_public_portal_link(candidate, source_url):
            path = urlparse(candidate).path.lower()
            selected = (
                candidate
                if any(
                    marker in path
                    for marker in ("floor", "availability", "available", "unit", "conventional")
                )
                else public_portal_navigation_url(candidate) or ""
            )
        else:
            continue
        if selected and is_safe_public_unlocker_url(selected) and selected not in links:
            links.append(selected)
        if len(links) == _MAX_UNLOCKER_PUBLIC_DRILLS:
            break
    return links


def rows_from_unlocked_html(record: RoutePlanRecord, html: str, source_url: str) -> list[dict[str, Any]]:
    """Parse only public SSR/DOM listing rows from an unlocker HTML response."""
    rows = portal_rows_from_html(record, html, source_url)
    try:
        if "prospectportal.com" in source_url.lower():
            rows.extend(parse_entrata_pp_unit_cards(html, source_url))
        else:
            extracted, _ = extract_units_from_dom(html, source_url)
            rows.extend(extracted)
    except Exception:
        pass
    return [dict(row) for row in rows if isinstance(row, dict)]


def prospectportal_known_template(record: RoutePlanRecord) -> str | None:
    """Return one durable ProspectPortal unit endpoint template, if learned."""
    for template in record.known_endpoint_templates:
        if "action=view_unit_spaces" in template and "{floorplan_id}" in template:
            return str(template)
    return None


def discovery_proxy_config(
    record: RoutePlanRecord, *, direct_device_ip: bool
) -> ProxyConfig:
    """Return the isolated network route for one browser-validation property.

    Args:
        record: Cohort route record whose canonical ID seeds a sticky session.
        direct_device_ip: When true, use the local machine's normal outbound
            connection rather than a Bright residential exit.  This is an
            explicit local-validation mode, never an automatic fallback.

    Returns:
        A direct config or a property-sticky Bright residential config.

    Raises:
        RuntimeError: If Bright credentials are absent in the default mode.
    """
    if direct_device_ip:
        return ProxyConfig(tier=ProxyTier.DIRECT)
    return BrightDataProvider().get_config(
        tier=ProxyTier.RESIDENTIAL,
        canonical_id=record.canonical_id,
    )


async def replay_known_prospectportal_endpoint(
    record: RoutePlanRecord,
    warm_page_url: str,
    floorplan_ids: tuple[str, ...],
    *,
    direct_device_ip: bool = False,
) -> KnownEndpointReplay:
    """Replay one learned PP endpoint through the selected validation route.

    Web Unlocker contributes only the fresh public floor-plan ID.  The direct
    endpoint response itself must independently contain a strict row.  Default
    discovery uses a property-sticky Bright residential session; explicitly
    requested local validation keeps the replay on the device's direct IP.
    """
    template = prospectportal_known_template(record)
    if template is None or not floorplan_ids:
        return KnownEndpointReplay()
    endpoint_url = expand_endpoint_template(
        template,
        warm_page_url=warm_page_url,
        floorplan_id=floorplan_ids[0],
        move_in_date=date.today(),
    )
    if endpoint_url is None:
        return KnownEndpointReplay(template)
    proxy_config = discovery_proxy_config(record, direct_device_ip=direct_device_ip)
    proxy = proxy_config.to_httpx_url()
    client = make_http_client(
        FetchTier.DIRECT if proxy_config.is_direct else FetchTier.RESIDENTIAL,
        proxy,
    )
    identity = IdentityPool().pick_chrome_only(sticky_key=record.canonical_id)
    headers = chrome_header_set(identity, cold_visit=False)
    headers.update(
        {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "text/html, */*; q=0.01",
            "Referer": warm_page_url,
        }
    )
    try:
        response = await client.request("GET", endpoint_url, headers=headers, cookies={}, timeout=45.0)
        status = getattr(response, "status_code", None)
        if status != 200:
            return KnownEndpointReplay(template, (), status)
        content = getattr(response, "content", b"")
        html = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content or "")
        return KnownEndpointReplay(
            template,
            tuple(strict_listing_rows(parse_prospectportal_unit_spaces(html, endpoint_url))),
            status,
        )
    except Exception:
        return KnownEndpointReplay(template)
    finally:
        await client.aclose()


async def revalidate_blocked_public_routes_with_unlocker(
    record: RoutePlanRecord, blocked_public_urls: list[str]
) -> WebUnlockerRevalidation:
    """Use at most three observed public routes to find strict SSR/DOM evidence.

    The paid fallback is invoked only after browser residential access has
    recorded a concrete blocked public route.  Raw responses stay in memory;
    the result exposes only origin/path audit evidence and strict rows.
    """
    pending = [url for url in blocked_public_urls if is_safe_public_unlocker_url(url)]
    visited: set[str] = set()
    attempted_paths: list[str] = []
    success_paths: list[str] = []
    best_plans: tuple[int, int, int | None, int | None] = (0, 0, None, None)
    floorplan_ids: list[str] = []
    budget_exhausted = False

    def _unlocker_budget_is_exhausted() -> bool:
        """Return whether this process has exhausted its explicit Unlocker cap."""
        raw_cap = os.getenv("WEB_UNLOCKER_MAX_CALLS_PER_JOB", "").strip()
        try:
            cap = int(raw_cap)
        except ValueError:
            return False
        return cap > 0 and web_unlocker_call_count() >= cap

    while pending and len(visited) < _MAX_UNLOCKER_PUBLIC_DRILLS:
        if _unlocker_budget_is_exhausted():
            budget_exhausted = True
            break
        candidate = pending.pop(0)
        candidate_key = public_route_key(candidate)
        if candidate_key is None or candidate_key in visited:
            continue
        visited.add(candidate_key)
        path = sanitized_xhr_path(candidate)
        if path is not None:
            attempted_paths.append(path)
        response = await asyncio.to_thread(web_unlocker_get, candidate)
        if response.status_code != 200 or not response.text:
            continue
        if path is not None:
            success_paths.append(path)
        for floorplan_id in extract_floorplan_ids(response.text, max_floorplans=3):
            if floorplan_id not in floorplan_ids:
                floorplan_ids.append(floorplan_id)
        rows = rows_from_unlocked_html(record, response.text, candidate)
        strict_rows = tuple(strict_listing_rows(rows))
        plans = public_plan_pricing(rows)
        if plans[0] > best_plans[0] or (plans[0] == best_plans[0] and plans[1] > best_plans[1]):
            best_plans = plans
        if strict_rows:
            return WebUnlockerRevalidation(
                tuple(attempted_paths),
                tuple(success_paths),
                strict_rows,
                candidate,
                *plans,
                tuple(floorplan_ids),
                budget_exhausted,
            )
        for link in unlocked_public_links(response.text, candidate):
            link_key = public_route_key(link)
            pending_keys = {public_route_key(item) for item in pending}
            if link_key is not None and link_key not in visited and link_key not in pending_keys:
                pending.append(link)
    return WebUnlockerRevalidation(
        tuple(attempted_paths),
        tuple(success_paths),
        (),
        None,
        *best_plans,
        tuple(floorplan_ids),
        budget_exhausted,
    )


async def dismiss_nonessential_popups(page: Any) -> None:
    """Close public marketing overlays before bounded availability exploration.

    The worker never fills or submits forms. It only dismisses overlays that
    block already-classified public availability controls, then returns if a
    theme uses an unrecognised close selector.
    """
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    for selector in (
        "[aria-label='Close']",
        "[aria-label='close']",
        ".mfp-close",
        ".modal-close",
        ".popup-close",
        ".close-button",
        "button.close",
    ):
        try:
            locator = page.locator(selector).first
            if await locator.count():
                await locator.click(timeout=1_000, force=True)
        except Exception:
            continue


async def _capture_browser_property(
    *,
    pool: BrowserContextPool,
    identities: IdentityPool,
    record: RoutePlanRecord,
    page: Any | None = None,
    direct_device_ip: bool = False,
    roster_date: date | None = None,
    allow_known_endpoint_replay: bool = True,
) -> BrowserEndpointProbeResult:
    """Open public availability UI, capture XHRs, and return sanitized proof only.

    ``page`` is supplied by the scheduler once it has acquired a property-
    isolated context.  That keeps browser-pool queue time out of the active
    property budget: a queued property has not started discovery yet and must
    not be converted into empty timeout evidence.
    """
    warm_candidates = _warm_urls(record)
    if not warm_candidates:
        return BrowserEndpointProbeResult(
            warm_status=None,
            classification=DiscoveryClassification.API_NOT_FOUND_YET,
            error="no-public-warm-url",
        )
    warm_url = warm_candidates[0]
    if page is None:
        identity = identities.pick_chrome_only(sticky_key=record.canonical_id)
        proxy = discovery_proxy_config(record, direct_device_ip=direct_device_ip)
        page = await pool.acquire(identity, proxy=proxy)
    captured: list[dict[str, Any]] = []
    capture_tasks: list[asyncio.Task[None]] = []
    warm_urls_tried: list[str] = []
    detail_urls_tried: list[str] = []
    public_portal_links_observed: list[str] = []
    blocked_public_paths: list[str] = []
    blocked_public_urls: list[str] = []
    controls_matched = 0
    frames_seen = 0
    max_dom_rows_seen = 0
    networkidle_reached = False
    xhr_total_seen = 0
    capture_truncated = False
    bodies_dropped_oversize = 0
    forms_present = False
    navigation_levels_reached = 0

    def _result(*args: Any, **kwargs: Any) -> BrowserEndpointProbeResult:
        """Build an outcome with auditable negative-path browser telemetry."""
        kwargs.setdefault("controls_matched", controls_matched)
        kwargs.setdefault("frames_seen", frames_seen)
        kwargs.setdefault("max_dom_rows_seen", max_dom_rows_seen)
        kwargs.setdefault("networkidle_reached", networkidle_reached)
        kwargs.setdefault("xhr_total_seen", xhr_total_seen)
        kwargs.setdefault("capture_truncated", capture_truncated)
        kwargs.setdefault("bodies_dropped_oversize", bodies_dropped_oversize)
        kwargs.setdefault("forms_present", forms_present)
        kwargs.setdefault("navigation_levels_reached", navigation_levels_reached)
        return BrowserEndpointProbeResult(*args, **kwargs)

    def _record_blocked_route(url: str) -> None:
        """Retain one sanitized public route that could not be reached."""
        path = sanitized_xhr_path(url)
        if path is not None and path not in blocked_public_paths:
            blocked_public_paths.append(path)
        if is_safe_public_unlocker_url(url) and url not in blocked_public_urls:
            blocked_public_urls.append(url)

    async def _capture_response(response: Any) -> None:
        """Retain bounded JSON XHR/fetch bodies only for in-memory strict parsing."""
        nonlocal bodies_dropped_oversize, capture_truncated, xhr_total_seen
        try:
            request = response.request
            content_type = str(response.headers.get("content-type") or "").lower()
            resource_type = str(request.resource_type or "").lower()
            if resource_type in {"xhr", "fetch"}:
                xhr_total_seen += 1
            if resource_type not in {"xhr", "fetch"} and "json" not in content_type:
                return
            if int(response.status) < 200 or int(response.status) >= 300:
                return
            if len(captured) >= _MAX_CAPTURED_RESPONSES:
                capture_truncated = True
                return
            payload = await bounded_response_body(response)
            if not payload or len(payload) > _MAX_CAPTURE_BODY_BYTES:
                if payload and len(payload) > _MAX_CAPTURE_BODY_BYTES:
                    bodies_dropped_oversize += 1
                return
            try:
                body = json.loads(payload)
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
                return
            if not isinstance(body, (dict, list)):
                return
            captured.append({"url": str(response.url), "body": body})
        except Exception:
            return

    capture_accepting = True

    def _on_response(response: Any) -> None:
        """Start a bounded body capture while this property still owns its page."""
        if capture_accepting:
            capture_tasks.append(asyncio.create_task(_capture_response(response)))

    page.on("response", _on_response)
    warm_status: int | None = None
    controls_clicked = 0
    rendered_rows: list[dict[str, Any]] = []
    portal_rows: list[dict[str, Any]] = []
    dom_proof_page_url: str | None = None
    try:
        last_error: str | None = None
        for candidate in warm_candidates[:3]:
            warm_urls_tried.append(candidate)
            navigation_candidate = resman_date_scoped_url(candidate, roster_date)
            try:
                response = await page.goto(
                    navigation_candidate, wait_until="domcontentloaded", timeout=45_000
                )
                candidate_status = response.status if response else None
            except Exception as exc:
                warm_status = None
                last_error = f"warm-error:{type(exc).__name__}"
                _record_blocked_route(candidate)
                continue
            warm_url = candidate
            warm_status = candidate_status
            if candidate_status is None or candidate_status < 400:
                navigation_levels_reached = max(navigation_levels_reached, 1)
                networkidle_reached = await wait_for_inventory_settle(page) or networkidle_reached
                break
            _record_blocked_route(candidate)
            last_error = "warm-not-ok"
        if warm_status is None or warm_status >= 400:
            unlocker = await revalidate_blocked_public_routes_with_unlocker(record, blocked_public_urls)
            known_endpoint = (
                await replay_known_prospectportal_endpoint(
                    record,
                    warm_url,
                    unlocker.floorplan_ids,
                    direct_device_ip=direct_device_ip,
                )
                if allow_known_endpoint_replay
                else KnownEndpointReplay()
            )
            if known_endpoint.strict_rows:
                return _result(
                    warm_status=warm_status,
                    classification=DiscoveryClassification.API_VERIFIED,
                    warm_page_url=warm_url,
                    strict_api_rows=known_endpoint.strict_rows,
                    endpoint_url=known_endpoint.endpoint_template,
                    plan_rows_observed=unlocker.plan_rows_observed,
                    plan_rows_with_numeric_price=unlocker.plan_rows_with_numeric_price,
                    plan_price_low=unlocker.plan_price_low,
                    plan_price_high=unlocker.plan_price_high,
                    warm_urls_tried=tuple(warm_urls_tried),
                    blocked_public_paths=tuple(blocked_public_paths),
                    web_unlocker_attempted_paths=unlocker.attempted_paths,
                    web_unlocker_success_paths=unlocker.success_paths,
                    web_unlocker_budget_exhausted=unlocker.budget_exhausted,
                    known_endpoint_replay_status=known_endpoint.response_status,
                )
            if unlocker.strict_rows:
                return _result(
                    warm_status=warm_status,
                    classification=DiscoveryClassification.SSR_DOM_ONLY,
                    warm_page_url=warm_url,
                    strict_dom_rows=unlocker.strict_rows,
                    dom_proof_page_url=unlocker.proof_page_url,
                    plan_rows_observed=unlocker.plan_rows_observed,
                    plan_rows_with_numeric_price=unlocker.plan_rows_with_numeric_price,
                    plan_price_low=unlocker.plan_price_low,
                    plan_price_high=unlocker.plan_price_high,
                    warm_urls_tried=tuple(warm_urls_tried),
                    blocked_public_paths=tuple(blocked_public_paths),
                    web_unlocker_attempted_paths=unlocker.attempted_paths,
                    web_unlocker_success_paths=unlocker.success_paths,
                    web_unlocker_budget_exhausted=unlocker.budget_exhausted,
                    known_endpoint_replay_status=known_endpoint.response_status,
                )
            classification = classify_non_strict_discovery(
                plan_rows_observed=unlocker.plan_rows_observed,
                blocked_public_paths=tuple(blocked_public_paths),
                unlocker_success_paths=unlocker.success_paths,
                unlocker_budget_exhausted=unlocker.budget_exhausted,
            )
            return _result(
                warm_status=warm_status,
                classification=classification,
                warm_page_url=warm_url,
                plan_rows_observed=unlocker.plan_rows_observed,
                plan_rows_with_numeric_price=unlocker.plan_rows_with_numeric_price,
                plan_price_low=unlocker.plan_price_low,
                plan_price_high=unlocker.plan_price_high,
                warm_urls_tried=tuple(warm_urls_tried),
                blocked_public_paths=tuple(blocked_public_paths),
                web_unlocker_attempted_paths=unlocker.attempted_paths,
                web_unlocker_success_paths=unlocker.success_paths,
                web_unlocker_budget_exhausted=unlocker.budget_exhausted,
                known_endpoint_replay_status=known_endpoint.response_status,
                error=last_error or "warm-not-ok",
            )
        try:
            forms_present = bool(
                await page.locator(
                    "input[type='date'], input[name*='move' i], input[id*='move' i], "
                    "form[action*='availability' i]"
                ).count()
            )
        except Exception:
            forms_present = False
        await dismiss_nonessential_popups(page)

        async def _read_dom(scope: Any = page) -> list[dict[str, Any]]:
            """Extract current rendered rows from the page or a live iframe."""
            nonlocal max_dom_rows_seen
            try:
                html = await scope.content()
                source_url = str(getattr(scope, "url", page.url))
                if "prospectportal.com" in source_url.lower():
                    rows = parse_entrata_pp_unit_cards(html, source_url)
                else:
                    rows, _ = extract_units_from_dom(html, source_url)
                # Public marketing-unit pages frequently expose the actual
                # apartment only through Schema.org JSON-LD.  The generic DOM
                # extractor intentionally ignores scripts, so include this
                # independent SSR source before applying the strict unit+rent
                # gate below.
                rows.extend(extract_jsonld_from_html(html, source_url))
                normalized: list[dict[str, Any]] = []
                if isinstance(rows, list):
                    for row in rows:
                        if isinstance(row, dict):
                            normalized.append(dict(row))
                max_dom_rows_seen = max(max_dom_rows_seen, len(normalized))
                return normalized
            except Exception:
                return []

        async def _current_portal_rows() -> list[dict[str, Any]]:
            """Return deterministic portal rows from the currently rendered page."""
            try:
                return portal_rows_from_html(record, await page.content(), page.url)
            except Exception:
                return []

        async def _click_current_availability_controls() -> list[dict[str, Any]]:
            """Open bounded availability controls in every currently live frame."""
            nonlocal controls_clicked, controls_matched, frames_seen, networkidle_reached
            best_rows = await _read_dom()
            # A direct marketing-unit URL can already contain strict SSR or
            # JSON-LD rows. Do not click away from that proof merely because a
            # floor-plan tab later yields a larger collection of plan-only
            # cards. This was the exact false-negative path for Rustic Woods.
            if strict_listing_rows(best_rows):
                return best_rows
            live_frames: list[Any] = []
            for frame in page.frames:
                try:
                    if not frame.is_detached() and is_inventory_frame_url(str(frame.url), page.url):
                        live_frames.append(frame)
                except Exception:
                    continue
            frames_seen = max(frames_seen, len(page.frames))
            # First inspect every frame without clicking. A top-level
            # “Floor plans” navigation can replace/detach a populated iframe
            # before we reach it; Emerson's AppFolio roster demonstrated this
            # exact false-negative path.
            for frame in live_frames:
                frame_rows = await _read_dom(frame)
                if strict_listing_rows(frame_rows):
                    return frame_rows
                if len(frame_rows) > len(best_rows):
                    best_rows = frame_rows

            # No pre-rendered roster: only now use the bounded interaction
            # budget to expose a lazy availability surface.
            clicks_this_level = 0
            for frame in [page, *live_frames]:
                controls = frame.locator("a, button, [role='button']")
                try:
                    control_texts = await controls.evaluate_all(
                        f"(nodes) => nodes.slice(0, {_MAX_CONTROLS}).map("
                        "(node) => (node.innerText || node.textContent || '').trim())"
                    )
                except Exception:
                    continue
                if not isinstance(control_texts, list):
                    continue
                matching_indexes = availability_control_indexes(
                    [text if isinstance(text, str) else "" for text in control_texts]
                )
                for index in matching_indexes:
                    if clicks_this_level >= _MAX_CLICKS:
                        break
                    control = controls.nth(index)
                    controls_matched += 1
                    try:
                        await control.click(timeout=2_500, force=True)
                    except Exception:
                        continue
                    controls_clicked += 1
                    clicks_this_level += 1
                    networkidle_reached = await wait_for_inventory_settle(page) or networkidle_reached
                    current_rows = await _read_dom(frame)
                    if strict_listing_rows(current_rows):
                        return current_rows
                    if len(current_rows) > len(best_rows):
                        best_rows = current_rows
            return best_rows

        async def _detail_drill_urls() -> list[str]:
            """Collect a few same-site unit/detail links before navigation mutates the page."""
            urls: list[str] = []
            anchors = page.locator("a")
            try:
                hrefs = await anchors.evaluate_all(
                    f"(nodes) => nodes.slice(0, {_MAX_ANCHOR_HREFS}).map("
                    "(node) => node.getAttribute('href'))"
                )
            except Exception:
                return urls
            if not isinstance(hrefs, list):
                return urls
            for href in hrefs:
                if not isinstance(href, str) or not href:
                    continue
                candidate = urljoin(page.url, href)
                if not is_public_detail_drill_url(candidate, page.url) or candidate in urls:
                    continue
                urls.append(candidate)
                if len(urls) == _MAX_DETAIL_PAGE_DRILLS:
                    break
            return urls

        async def _current_public_portal_links() -> list[str]:
            """Capture observed external PMS inventory links from the visible page."""
            urls: list[str] = []
            anchors = page.locator("a")
            try:
                hrefs = await anchors.evaluate_all(
                    f"(nodes) => nodes.slice(0, {_MAX_ANCHOR_HREFS}).map("
                    "(node) => node.getAttribute('href'))"
                )
            except Exception:
                return urls
            if not isinstance(hrefs, list):
                return urls
            for href in hrefs:
                if not isinstance(href, str) or not href:
                    continue
                candidate = urljoin(page.url, href)
                if not is_public_portal_link(candidate, warm_url):
                    continue
                portal_url = public_portal_navigation_url(candidate)
                if portal_url is None or portal_url in urls:
                    continue
                urls.append(portal_url)
                if len(urls) == _MAX_PUBLIC_PORTAL_LINKS:
                    break
            return urls

        detail_urls = await _detail_drill_urls()
        public_portal_links_observed = await _current_public_portal_links()
        rendered_rows = await _click_current_availability_controls()
        portal_rows = await _current_portal_rows()
        if strict_listing_rows([*portal_rows, *rendered_rows]):
            dom_proof_page_url = without_resman_date_scope(page.url)
        else:
            # A marketing home page often points first to a floor-plan index,
            # whose cards then point to the actual unit roster. Explore that
            # one additional public level before falling back to a PMS portal.
            # The total cap prevents generic navigation from becoming a crawl.
            detail_pending = [(url, 1) for url in detail_urls]
            detail_visited: set[str] = set()
            while detail_pending and len(detail_visited) < _MAX_DETAIL_PAGE_DRILLS:
                detail_url, detail_depth = detail_pending.pop(0)
                if detail_url in detail_visited:
                    continue
                detail_visited.add(detail_url)
                detail_urls_tried.append(detail_url)
                try:
                    detail_response = await page.goto(
                        detail_url, wait_until="domcontentloaded", timeout=30_000
                    )
                    if detail_response is not None and int(detail_response.status) >= 400:
                        _record_blocked_route(detail_url)
                        continue
                    navigation_levels_reached = max(navigation_levels_reached, 2)
                    networkidle_reached = await wait_for_inventory_settle(page) or networkidle_reached
                    await dismiss_nonessential_popups(page)
                except Exception:
                    _record_blocked_route(detail_url)
                    continue
                detail_rendered_rows = await _click_current_availability_controls()
                detail_portal_rows = await _current_portal_rows()
                for portal_url in await _current_public_portal_links():
                    if portal_url not in public_portal_links_observed:
                        public_portal_links_observed.append(portal_url)
                detail_rows = [*detail_portal_rows, *detail_rendered_rows]
                if len(detail_rendered_rows) > len(rendered_rows):
                    rendered_rows = detail_rendered_rows
                if len(detail_portal_rows) > len(portal_rows):
                    portal_rows = detail_portal_rows
                if strict_listing_rows(detail_rows):
                    rendered_rows = detail_rendered_rows
                    portal_rows = detail_portal_rows
                    dom_proof_page_url = without_resman_date_scope(page.url)
                    break
                if detail_depth < 2:
                    for nested_url in await _detail_drill_urls():
                        if nested_url not in detail_visited:
                            detail_pending.append((nested_url, detail_depth + 1))
        # A marketing site can expose only a public ProspectPortal tour link.
        # We navigate to that observed portal *origin* (never a constructed
        # property path), then follow its own visible public floor-plan link.
        # This reaches the unit surface without treating an application URL as
        # inventory or guessing an unavailable endpoint.
        if dom_proof_page_url is None:
            for portal_root in tuple(public_portal_links_observed):
                if portal_root not in warm_urls_tried:
                    warm_urls_tried.append(portal_root)
                try:
                    portal_navigation_url = resman_date_scoped_url(portal_root, roster_date)
                    portal_response = await page.goto(
                        portal_navigation_url, wait_until="domcontentloaded", timeout=30_000
                    )
                    if portal_response is not None and int(portal_response.status) >= 400:
                        _record_blocked_route(portal_root)
                        continue
                    navigation_levels_reached = max(navigation_levels_reached, 3)
                    networkidle_reached = await wait_for_inventory_settle(page) or networkidle_reached
                    await dismiss_nonessential_popups(page)
                except Exception:
                    _record_blocked_route(portal_root)
                    continue
                portal_rendered_rows = await _click_current_availability_controls()
                portal_static_rows = await _current_portal_rows()
                portal_rows_current = [*portal_static_rows, *portal_rendered_rows]
                if strict_listing_rows(portal_rows_current):
                    warm_url = portal_root
                    rendered_rows = portal_rendered_rows
                    portal_rows = portal_static_rows
                    dom_proof_page_url = without_resman_date_scope(page.url)
                    break
                portal_pending = [(url, 1) for url in await _detail_drill_urls()]
                portal_visited: set[str] = set()
                while portal_pending and len(portal_visited) < _MAX_DETAIL_PAGE_DRILLS:
                    portal_detail_url, portal_depth = portal_pending.pop(0)
                    if portal_detail_url in portal_visited:
                        continue
                    portal_visited.add(portal_detail_url)
                    detail_urls_tried.append(portal_detail_url)
                    try:
                        portal_detail_response = await page.goto(
                            portal_detail_url, wait_until="domcontentloaded", timeout=30_000
                        )
                        if portal_detail_response is not None and int(portal_detail_response.status) >= 400:
                            _record_blocked_route(portal_detail_url)
                            continue
                        navigation_levels_reached = max(navigation_levels_reached, 4)
                        networkidle_reached = await wait_for_inventory_settle(page) or networkidle_reached
                        await dismiss_nonessential_popups(page)
                    except Exception:
                        _record_blocked_route(portal_detail_url)
                        continue
                    portal_detail_rendered_rows = await _click_current_availability_controls()
                    portal_detail_static_rows = await _current_portal_rows()
                    portal_detail_rows = [*portal_detail_static_rows, *portal_detail_rendered_rows]
                    if not strict_listing_rows(portal_detail_rows):
                        if portal_depth < 2:
                            for next_url in await _detail_drill_urls():
                                if next_url not in portal_visited:
                                    portal_pending.append((next_url, portal_depth + 1))
                        continue
                    warm_url = portal_root
                    rendered_rows = portal_detail_rendered_rows
                    portal_rows = portal_detail_static_rows
                    dom_proof_page_url = without_resman_date_scope(page.url)
                    break
                if dom_proof_page_url is not None:
                    break
        if capture_tasks:
            await asyncio.gather(*capture_tasks, return_exceptions=True)
        endpoint_url, api_rows = strict_api_proof(captured, record.canonical_id)
        observed_xhr_paths = tuple(
            sorted(
                {
                    path
                    for response in captured
                    if (path := sanitized_xhr_path(str(response.get("url") or ""))) is not None
                }
            )
        )
        api_plan_rows = api_floorplan_plan_rows(captured, record.canonical_id)
        observed_rows = [*portal_rows, *rendered_rows, *api_plan_rows]
        dom_rows = tuple(strict_listing_rows(observed_rows))
        plans, priced_plans, low, high = public_plan_pricing(observed_rows)
        dom_roster_scope = (
            "CURRENT_PUBLIC_ROSTER"
            if roster_date is not None
            and dom_proof_page_url is not None
            and urlparse(dom_proof_page_url).netloc.lower().endswith(".myresman.com")
            else "DOM_SCOPE_UNPROVEN"
        )
        if api_rows:
            return _result(
                warm_status,
                DiscoveryClassification.API_VERIFIED,
                warm_page_url=warm_url,
                strict_api_rows=api_rows,
                strict_dom_rows=dom_rows,
                dom_proof_page_url=dom_proof_page_url,
                dom_roster_scope=dom_roster_scope,
                endpoint_url=endpoint_url,
                controls_clicked=controls_clicked,
                api_responses_considered=len(captured),
                plan_rows_observed=plans,
                plan_rows_with_numeric_price=priced_plans,
                plan_price_low=low,
                plan_price_high=high,
                warm_urls_tried=tuple(warm_urls_tried),
                detail_urls_tried=tuple(detail_urls_tried),
                public_portal_links_observed=tuple(public_portal_links_observed),
                observed_xhr_paths=observed_xhr_paths,
                blocked_public_paths=tuple(blocked_public_paths),
            )
        if dom_rows:
            return _result(
                warm_status,
                DiscoveryClassification.SSR_DOM_ONLY,
                warm_page_url=warm_url,
                strict_dom_rows=dom_rows,
                dom_proof_page_url=dom_proof_page_url,
                dom_roster_scope=dom_roster_scope,
                controls_clicked=controls_clicked,
                api_responses_considered=len(captured),
                plan_rows_observed=plans,
                plan_rows_with_numeric_price=priced_plans,
                plan_price_low=low,
                plan_price_high=high,
                warm_urls_tried=tuple(warm_urls_tried),
                detail_urls_tried=tuple(detail_urls_tried),
                public_portal_links_observed=tuple(public_portal_links_observed),
                observed_xhr_paths=observed_xhr_paths,
                blocked_public_paths=tuple(blocked_public_paths),
            )
        unlocker = (
            await revalidate_blocked_public_routes_with_unlocker(record, blocked_public_urls)
            if blocked_public_urls
            else WebUnlockerRevalidation()
        )
        known_endpoint = (
            await replay_known_prospectportal_endpoint(
                record,
                warm_url,
                unlocker.floorplan_ids,
                direct_device_ip=direct_device_ip,
            )
            if allow_known_endpoint_replay
            else KnownEndpointReplay()
        )
        if known_endpoint.strict_rows:
            return _result(
                warm_status,
                DiscoveryClassification.API_VERIFIED,
                warm_page_url=warm_url,
                strict_api_rows=known_endpoint.strict_rows,
                endpoint_url=known_endpoint.endpoint_template,
                controls_clicked=controls_clicked,
                api_responses_considered=len(captured),
                plan_rows_observed=unlocker.plan_rows_observed,
                plan_rows_with_numeric_price=unlocker.plan_rows_with_numeric_price,
                plan_price_low=unlocker.plan_price_low,
                plan_price_high=unlocker.plan_price_high,
                warm_urls_tried=tuple(warm_urls_tried),
                detail_urls_tried=tuple(detail_urls_tried),
                public_portal_links_observed=tuple(public_portal_links_observed),
                observed_xhr_paths=observed_xhr_paths,
                blocked_public_paths=tuple(blocked_public_paths),
                web_unlocker_attempted_paths=unlocker.attempted_paths,
                web_unlocker_success_paths=unlocker.success_paths,
                web_unlocker_budget_exhausted=unlocker.budget_exhausted,
                known_endpoint_replay_status=known_endpoint.response_status,
            )
        if unlocker.strict_rows:
            return _result(
                warm_status,
                DiscoveryClassification.SSR_DOM_ONLY,
                warm_page_url=warm_url,
                strict_dom_rows=unlocker.strict_rows,
                dom_proof_page_url=unlocker.proof_page_url,
                controls_clicked=controls_clicked,
                api_responses_considered=len(captured),
                plan_rows_observed=unlocker.plan_rows_observed,
                plan_rows_with_numeric_price=unlocker.plan_rows_with_numeric_price,
                plan_price_low=unlocker.plan_price_low,
                plan_price_high=unlocker.plan_price_high,
                warm_urls_tried=tuple(warm_urls_tried),
                detail_urls_tried=tuple(detail_urls_tried),
                public_portal_links_observed=tuple(public_portal_links_observed),
                observed_xhr_paths=observed_xhr_paths,
                blocked_public_paths=tuple(blocked_public_paths),
                web_unlocker_attempted_paths=unlocker.attempted_paths,
                web_unlocker_success_paths=unlocker.success_paths,
                web_unlocker_budget_exhausted=unlocker.budget_exhausted,
                known_endpoint_replay_status=known_endpoint.response_status,
            )
        if unlocker.plan_rows_observed > plans or (
            unlocker.plan_rows_observed == plans and unlocker.plan_rows_with_numeric_price > priced_plans
        ):
            plans = unlocker.plan_rows_observed
            priced_plans = unlocker.plan_rows_with_numeric_price
            low = unlocker.plan_price_low
            high = unlocker.plan_price_high
        classification = classify_non_strict_discovery(
            plan_rows_observed=plans,
            blocked_public_paths=tuple(blocked_public_paths),
            unlocker_success_paths=unlocker.success_paths,
            unlocker_budget_exhausted=unlocker.budget_exhausted,
        )
        return _result(
            warm_status,
            classification,
            warm_page_url=warm_url,
            controls_clicked=controls_clicked,
            api_responses_considered=len(captured),
            plan_rows_observed=plans,
            plan_rows_with_numeric_price=priced_plans,
            plan_price_low=low,
            plan_price_high=high,
            warm_urls_tried=tuple(warm_urls_tried),
            detail_urls_tried=tuple(detail_urls_tried),
            public_portal_links_observed=tuple(public_portal_links_observed),
            observed_xhr_paths=observed_xhr_paths,
            blocked_public_paths=tuple(blocked_public_paths),
            web_unlocker_attempted_paths=unlocker.attempted_paths,
            web_unlocker_success_paths=unlocker.success_paths,
            web_unlocker_budget_exhausted=unlocker.budget_exhausted,
            known_endpoint_replay_status=known_endpoint.response_status,
            error="blocked-public-path" if blocked_public_paths else None,
        )
    finally:
        # The property watchdog can cancel this coroutine while an XHR body
        # reader is still awaiting a response. Drain those tasks before the
        # isolated page is released so a timeout remains clean retry evidence
        # rather than leaking a TargetClosedError into the batch logs.
        capture_accepting = False
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass
        for task in capture_tasks:
            if not task.done():
                task.cancel()
        if capture_tasks:
            await asyncio.gather(*capture_tasks, return_exceptions=True)
        await pool.release(page)


def _load_plan_records(client: storage.Client, plan_uri: str) -> list[RoutePlanRecord]:
    """Load a durable route plan without accepting records from another format."""
    bucket_name, object_name = _parse_gcs_uri(plan_uri)
    text = client.bucket(bucket_name).blob(object_name).download_as_text()
    records: list[RoutePlanRecord] = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
            records.append(
                RoutePlanRecord(
                    canonical_id=str(row["canonical_id"]),
                    source_url=str(row["source_url"]),
                    public_url_candidates=tuple(str(item) for item in row["public_url_candidates"]),
                    detected_platform=str(row.get("detected_platform") or "unknown"),
                    known_endpoint_count=int(row.get("known_endpoint_count") or 0),
                    route=DiscoveryRoute(str(row["route"])),
                    known_endpoint_templates=tuple(
                        str(item) for item in row.get("known_endpoint_templates", [])
                    ),
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return records


def _profile_blob(client: storage.Client, prefix_uri: str, canonical_id: str) -> storage.Blob:
    """Return the profile blob used for a generation-guarded verified write."""
    bucket_name, prefix = _parse_gcs_uri(prefix_uri)
    return client.bucket(bucket_name).blob(f"{prefix.rstrip('/')}/{canonical_id}.json".lstrip("/"))


def select_batch_records(
    records: list[RoutePlanRecord], limit: int, *, stratified: bool
) -> list[RoutePlanRecord]:
    """Select a bounded batch, optionally taking one record from each lane first."""
    if not stratified:
        return records[:limit]
    selected: list[RoutePlanRecord] = []
    seen_routes: set[DiscoveryRoute] = set()
    for record in records:
        if record.route in seen_routes:
            continue
        selected.append(record)
        seen_routes.add(record.route)
        if len(selected) == limit:
            return selected
    selected_ids = {record.canonical_id for record in selected}
    for record in records:
        if record.canonical_id in selected_ids:
            continue
        selected.append(record)
        if len(selected) == limit:
            break
    return selected


def filter_uncompleted_records(
    records: list[RoutePlanRecord], completed_ids: set[str]
) -> list[RoutePlanRecord]:
    """Keep only properties without a checkpoint from this workflow version."""
    return [record for record in records if record.canonical_id not in completed_ids]


def _completed_ids_from_checkpoint_payloads(payloads: list[str]) -> set[str]:
    """Read durable completed rows, retaining timeout evidence for retry."""
    completed: set[str] = set()
    for payload in payloads:
        for line in payload.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("workflow_version") != _WORKFLOW_VERSION:
                continue
            if row.get("error") == "property-timeout":
                continue
            canonical_id = str(row.get("canonical_id") or "").strip()
            if canonical_id:
                completed.add(canonical_id)
    return completed


def _load_completed_ids(client: storage.Client, checkpoint_prefix_uri: str) -> set[str]:
    """Load this workflow's durable checkpoint set without reading profiles."""
    bucket_name, prefix = _parse_gcs_uri(checkpoint_prefix_uri)
    blobs = client.list_blobs(bucket_name, prefix=f"{prefix.rstrip('/')}/browser-batch-")
    payloads: list[str] = []
    for blob in blobs:
        try:
            payloads.append(blob.download_as_text())
        except Exception:
            continue
    return _completed_ids_from_checkpoint_payloads(payloads)


def _timed_out_ids_from_checkpoint_payloads(payloads: list[str]) -> set[str]:
    """Return current-workflow property ids with timeout-only checkpoint evidence."""
    timed_out: set[str] = set()
    for payload in payloads:
        for line in payload.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("workflow_version") != _WORKFLOW_VERSION:
                continue
            if row.get("error") != "property-timeout":
                continue
            canonical_id = str(row.get("canonical_id") or "").strip()
            if canonical_id:
                timed_out.add(canonical_id)
    return timed_out


def _load_timed_out_ids(client: storage.Client, checkpoint_prefix_uri: str) -> set[str]:
    """Read timeout evidence so later batches prioritize fresh properties first."""
    bucket_name, prefix = _parse_gcs_uri(checkpoint_prefix_uri)
    payloads: list[str] = []
    for blob in client.list_blobs(bucket_name, prefix=f"{prefix.rstrip('/')}/browser-batch-"):
        try:
            payloads.append(blob.download_as_text())
        except Exception:
            continue
    return _timed_out_ids_from_checkpoint_payloads(payloads)


async def _probe_record(
    *,
    client: storage.Client,
    profile_prefix: str,
    pool: BrowserContextPool,
    identities: IdentityPool,
    record: RoutePlanRecord,
    commit_profiles: bool,
    property_timeout_seconds: int = 180,
    direct_device_ip: bool = False,
) -> dict[str, Any]:
    """Probe one route item and conditionally persist only durable strict proof."""
    identity = identities.pick_chrome_only(sticky_key=record.canonical_id)
    proxy = discovery_proxy_config(record, direct_device_ip=direct_device_ip)
    # Queue time is not a property attempt. Acquire first, then start the
    # watchdog around actual navigation and inventory discovery.
    page = await pool.acquire(identity, proxy=proxy)
    try:
        result = await asyncio.wait_for(
            _capture_browser_property(
                pool=pool,
                identities=identities,
                record=record,
                page=page,
                direct_device_ip=direct_device_ip,
            ),
            timeout=property_timeout_seconds,
        )
    except TimeoutError:
        return {
            "workflow_version": _WORKFLOW_VERSION,
            "canonical_id": record.canonical_id,
            "source_url": record.source_url,
            "route": record.route.value,
            "classification": DiscoveryClassification.API_NOT_FOUND_YET.value,
            "profile_persistence": "checkpoint_only",
            "error": "property-timeout",
            "observed_at": datetime.now(UTC).isoformat(),
        }
    persistence = "checkpoint_only"
    warm_url = result.warm_page_url or _warm_url(record)
    if commit_profiles and warm_url:
        if result.classification == DiscoveryClassification.API_VERIFIED and is_safe_endpoint_template(
            result.endpoint_url
        ):
            evidence = DiscoveryEvidence(
                canonical_id=record.canonical_id,
                classification=result.classification,
                warm_page_url=warm_url,
                strict_row_count=len(result.strict_api_rows),
                endpoint_template=result.endpoint_url,
                endpoint_provider=record.detected_platform,
            )
            persistence = await asyncio.to_thread(
                lambda: (
                    persist_generation_guarded(
                        _profile_blob(client, profile_prefix, record.canonical_id), evidence
                    ).outcome
                )
            )
        elif result.classification == DiscoveryClassification.SSR_DOM_ONLY:
            evidence = DiscoveryEvidence(
                canonical_id=record.canonical_id,
                classification=result.classification,
                warm_page_url=warm_url,
                strict_row_count=len(result.strict_dom_rows),
                unit_page_url=result.dom_proof_page_url,
            )
            persistence = await asyncio.to_thread(
                lambda: (
                    persist_generation_guarded(
                        _profile_blob(client, profile_prefix, record.canonical_id), evidence
                    ).outcome
                )
            )
        elif result.classification == DiscoveryClassification.API_VERIFIED:
            persistence = "verified_but_non_durable_endpoint"
    return {
        "workflow_version": _WORKFLOW_VERSION,
        "browser_network_mode": "direct_device_ip" if direct_device_ip else "bright_residential",
        "canonical_id": record.canonical_id,
        "source_url": record.source_url,
        "warm_page_url": warm_url,
        "warm_urls_tried": list(result.warm_urls_tried),
        "route": record.route.value,
        "classification": result.classification.value,
        "warm_status": result.warm_status,
        "strict_api_unit_rent_rows": len(result.strict_api_rows),
        "strict_dom_unit_rent_rows": len(result.strict_dom_rows),
        "unit_page_url": result.dom_proof_page_url,
        "detail_urls_tried": list(result.detail_urls_tried),
        "public_portal_links_observed": list(result.public_portal_links_observed),
        "observed_xhr_paths": list(result.observed_xhr_paths),
        "blocked_public_paths": list(result.blocked_public_paths),
        "web_unlocker_used": bool(result.web_unlocker_attempted_paths),
        "web_unlocker_attempted_paths": list(result.web_unlocker_attempted_paths),
        "web_unlocker_success_paths": list(result.web_unlocker_success_paths),
        "web_unlocker_budget_exhausted": bool(result.web_unlocker_budget_exhausted),
        "known_endpoint_replay_status": result.known_endpoint_replay_status,
        "endpoint_template": result.endpoint_url if is_safe_endpoint_template(result.endpoint_url) else None,
        "profile_persistence": persistence,
        "controls_clicked": result.controls_clicked,
        "controls_matched": result.controls_matched,
        "frames_seen": result.frames_seen,
        "max_dom_rows_seen": result.max_dom_rows_seen,
        "networkidle_reached": result.networkidle_reached,
        "xhr_total_seen": result.xhr_total_seen,
        "api_responses_considered": result.api_responses_considered,
        "capture_truncated": result.capture_truncated,
        "bodies_dropped_oversize": result.bodies_dropped_oversize,
        "forms_present": result.forms_present,
        "navigation_levels_reached": result.navigation_levels_reached,
        "public_plan_pricing": {
            "plans_observed": result.plan_rows_observed,
            "plans_with_numeric_price": result.plan_rows_with_numeric_price,
            "price_low": result.plan_price_low,
            "price_high": result.plan_price_high,
        },
        "error": result.error,
        "observed_at": datetime.now(UTC).isoformat(),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """Run one bounded, platform-agnostic browser/XHR discovery batch."""
    # The existing unlocker client reads its budget at call time.  Set an
    # explicit per-process cap so a bounded discovery batch cannot spend past
    # its auditable allowance even when several properties block in parallel.
    os.environ["WEB_UNLOCKER_MAX_CALLS_PER_JOB"] = str(args.web_unlocker_max_calls)
    client = storage.Client(project=args.project)
    records = await asyncio.to_thread(_load_plan_records, client, args.route_plan_gcs_uri)
    if args.route:
        accepted = set(args.route)
        records = [record for record in records if record.route.value in accepted]
    if args.canonical_id:
        requested_ids = set(args.canonical_id)
        records = [record for record in records if record.canonical_id in requested_ids]
    if args.retry_completed:
        completed_ids: set[str] = set()
        timed_out_ids: set[str] = set()
    else:
        completed_ids = await asyncio.to_thread(
            _load_completed_ids, client, args.checkpoint_gcs_prefix
        )
        timed_out_ids = await asyncio.to_thread(
            _load_timed_out_ids, client, args.checkpoint_gcs_prefix
        )
    pending_records = filter_uncompleted_records(records, completed_ids)
    pending_records.sort(key=lambda record: record.canonical_id in timed_out_ids)
    selected = select_batch_records(pending_records, args.limit, stratified=args.stratified)
    pool = BrowserContextPool(max_contexts=args.concurrency)
    identities = IdentityPool()
    try:
        rows = await asyncio.gather(
            *(
                _probe_record(
                    client=client,
                    profile_prefix=args.profile_gcs_prefix,
                    pool=pool,
                    identities=identities,
                    record=record,
                    commit_profiles=args.commit_profiles,
                    property_timeout_seconds=args.property_timeout_seconds,
                    direct_device_ip=args.direct_device_ip,
                )
                for record in selected
            )
        )
    finally:
        await pool.close()
    bucket_name, prefix = _parse_gcs_uri(args.checkpoint_gcs_prefix)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{prefix.rstrip('/')}/browser-batch-{stamp}.jsonl".lstrip("/")
    client.bucket(bucket_name).blob(name).upload_from_string(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        content_type="application/x-ndjson",
        if_generation_match=0,
    )
    classifications: dict[str, int] = {}
    for row in rows:
        label = str(row["classification"])
        classifications[label] = classifications.get(label, 0) + 1
    return {
        "route_plan_size": len(records),
        "previously_completed": len(records) - len(pending_records),
        "remaining_after_batch": len(pending_records) - len(selected),
        "selected": len(selected),
        "completed": len(rows),
        "classifications": classifications,
        "checkpoint": f"gs://{bucket_name}/{name}",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse bounded browser-batch arguments for the current route-plan artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--route-plan-gcs-uri",
        default="gs://jugnu-canary/investigations/2026-07-26-unit-endpoint-discovery/cohort-route-plan-20260727T011801Z.jsonl",
    )
    parser.add_argument("--profile-gcs-prefix", default="gs://jugnu-canary/profiles/plancohort-run/")
    parser.add_argument(
        "--checkpoint-gcs-prefix",
        default="gs://jugnu-canary/investigations/2026-07-26-unit-endpoint-discovery/",
    )
    parser.add_argument("--project", default="jugnu-494013")
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument(
        "--property-timeout-seconds",
        type=int,
        default=180,
        help="Hard cap per property; timeout evidence remains retryable.",
    )
    parser.add_argument(
        "--web-unlocker-max-calls",
        type=int,
        default=10,
        help="Maximum paid public-route discovery fetches in this process (0 is uncapped).",
    )
    parser.add_argument("--route", action="append", default=[])
    parser.add_argument(
        "--canonical-id",
        action="append",
        default=[],
        help="Probe only an explicit cohort property ID (repeatable, for validation).",
    )
    parser.add_argument(
        "--stratified",
        action="store_true",
        help="Use one property per discovery lane before filling remaining slots.",
    )
    parser.add_argument("--commit-profiles", action="store_true")
    parser.add_argument(
        "--direct-device-ip",
        action="store_true",
        help=(
            "Use this machine's direct outbound IP for local validation. "
            "This is explicit and checkpointed; Bright residential remains the default."
        ),
    )
    parser.add_argument(
        "--retry-completed",
        action="store_true",
        help="Re-run prior current-version checkpoints for targeted debugging only.",
    )
    args = parser.parse_args(argv)
    if (
        args.limit < 1
        or args.concurrency < 1
        or args.property_timeout_seconds < 1
        or args.web_unlocker_max_calls < 0
    ):
        parser.error("limits, concurrency, and timeout must be positive; unlocker cap cannot be negative")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run a browser/XHR batch and print only its durable summary."""
    print(json.dumps(asyncio.run(run(parse_args(argv))), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
