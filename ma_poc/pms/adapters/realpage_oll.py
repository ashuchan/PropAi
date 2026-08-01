"""
RealPage OLL (Online Leasing) adapter.

Research log
------------
Web sources consulted:
  - https://www.realpage.com/ — RealPage platform overview (accessed 2026-04-17)
  - RealPage API patterns documented in scripts/entrata.py and scrape_properties.py
Reverse-engineered API contract (committed 2026-05-17):
  - investigations/2026-05-17-canary-iterate/artifacts/analysis/
    categoryD_realpage_OLL_api.json — full request/response contract for the
    "Category-D" / lochraven-like cluster (~187 properties).

Two distinct RealPage API shapes are handled here:

  1. ``api.ws.realpage.com/v2/property/{id}/floorplans`` — the legacy
     OneSite-shared envelope ``{status, message, response: {floorplans:[...]}}``.
     Parsed via ``parse_realpage_floorplans`` (shared with OneSite). KEPT.

  2. ``leasing.realpage.com/RP.Leasing.AppService.WebHost/appstate/v1/?...
     BpmId=OLL.SearchFloorPlan...`` — the stateful OLL/BPM wizard PUT
     response. Units live at
     ``Workflow.ActivityGroups[].GroupActivities[]`` where ``__type``
     contains ``ApartmentSelectionLeaseMgmtActivity`` → ``.Units[]``.
     Parsed via ``parse_realpage_oll_workflow`` (NEW).

Access constraint (critical)
----------------------------
The ``leasing.realpage.com`` OLL workflow endpoint is behind DataDome + Akamai
Bot Manager and is stateful. That workflow remains interception-only: the
adapter parses responses already captured from the public browser flow and
never forges its session requests.

Numeric ``{propertyId}.onlineleasing.realpage.com`` portals expose a separate,
public, static ``CmsSiteManager/...Proxy/GetUnits`` roster. For those roots the
adapter can make one bounded direct GET, provided the payload ``propertyId``
matches the numeric host. This route uses no proxy, unlocker, challenge solver,
or browser fingerprint manipulation and accepts only apartment identity plus
positive rent.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from ma_poc.pms.adapters._daily_runner_parsers import (
    realpage_units_to_adapter_shape as _dr_realpage_units,
)
from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.onesite import (
    _is_realpage_units_response,
    parse_realpage_floorplans,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

OLL_TIER = "TIER_1_API_REALPAGE_OLL"
ONLINELEASING_GETUNITS_TIER = "TIER_1_API_REALPAGE_ONLINELEASING_GETUNITS"

log = logging.getLogger(__name__)

# ``/Date(1779339600000-0500)/`` or ``/Date(1779339600000)/`` — .NET
# JSON date. Group 1 = epoch milliseconds (may be negative), group 2 =
# optional ``±HHMM`` timezone offset which we deliberately ignore: the
# millisecond value is already an absolute UTC instant, the offset is
# only the server's display zone.
_DOTNET_DATE_RE = re.compile(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/")

# OLL appstate URL marker. The BpmId varies (SearchFloorPlan, Menu,
# HomeDetails, UnitSelection…); any appstate/v1 body that carries a
# ``Workflow`` with ``ApartmentSelectionLeaseMgmtActivity`` is parsed.
_OLL_URL_MARKERS = (
    "leasing.realpage.com",
    "rp.leasing.appservice",
    "/appstate/v1",
)

# Public OneSite/RealPage portal root. Keep this deliberately narrower than a
# generic ``realpage.com`` match: the first numeric host label is the
# property-scoped ``propertyId`` returned by the GetUnits payload. Marketing
# pages sometimes carry escaped URLs inside JSON, which are normalised before
# this regex runs.
_ONLINELEASING_ROOT_RE = re.compile(
    r"(?:https?:)?//(?P<root>\d{3,10})\.onlineleasing\.realpage\.com"
    r"(?=[/:?#\s\"']|$)",
    re.IGNORECASE,
)
_ONLINELEASING_MAX_ROOTS = 3
_ONLINELEASING_DISCOVERY_TEXT_LIMIT = 2_000_000
_ONLINELEASING_PROBE_TIMEOUT_S = 12
_ONLINELEASING_MAX_BODY_BYTES = 2_000_000
_ONLINELEASING_MAX_REDIRECTS = 3
_ONLINELEASING_GETUNITS_QUERY = (
    "act=Proxy/GetUnits&available=true&honordisplayorder=true"
)


def _onlineleasing_getunits_url(root_id: str) -> str:
    """Return the one public, property-scoped roster URL for *root_id*."""
    return (
        f"https://{root_id}.onlineleasing.realpage.com/"
        f"CmsSiteManager/callback.aspx?{_ONLINELEASING_GETUNITS_QUERY}"
    )


def _discovery_text(value: Any) -> str:
    """Convert a bounded context value to text for portal-root discovery."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    elif isinstance(value, (dict, list)):
        try:
            value = json.dumps(value, separators=(",", ":"))
        except (TypeError, ValueError):
            return ""
    if not isinstance(value, str):
        return ""
    # URLs are commonly embedded as ``https:\/\/...`` or HTML entities.
    return html.unescape(value[:_ONLINELEASING_DISCOVERY_TEXT_LIMIT]).replace(
        "\\/", "/"
    )


def onlineleasing_roots_from_ctx(ctx: AdapterContext) -> list[str]:
    """Discover at most three numeric RealPage portal roots from *ctx*.

    Current navigation URLs are authoritative and are scanned first, followed
    by the fetched page/captured responses and finally persisted navigation
    hints. The hard cap keeps the recovery bounded on portfolio/PMC pages.
    """
    values: list[Any] = [getattr(ctx, "base_url", "")]
    fetch_result = getattr(ctx, "fetch_result", None)
    if fetch_result is not None:
        values.extend(
            [
                getattr(fetch_result, "final_url", ""),
                getattr(fetch_result, "body", None),
            ]
        )

    for response in getattr(ctx, "_api_responses", []) or []:
        if isinstance(response, dict):
            values.extend([response.get("url", ""), response.get("body")])

    profile = getattr(ctx, "profile", None)
    navigation = getattr(profile, "navigation", None) if profile is not None else None
    if navigation is not None:
        values.append(getattr(navigation, "winning_page_url", ""))
        values.extend(getattr(navigation, "availability_links", []) or [])
        values.extend(getattr(navigation, "explored_links", []) or [])

    roots: list[str] = []
    for value in values:
        text = _discovery_text(value)
        if not text:
            continue
        for match in _ONLINELEASING_ROOT_RE.finditer(text):
            root_id = match.group("root")
            if root_id not in roots:
                roots.append(root_id)
                if len(roots) >= _ONLINELEASING_MAX_ROOTS:
                    return roots
    return roots


def parse_scoped_onlineleasing_getunits(
    body: str,
    source_url: str,
    root_id: str,
) -> list[dict[str, Any]]:
    """Parse only available apartments belonging to *root_id*.

    The public endpoint can return occupied rows despite ``available=true``.
    It also exposes both a host-scoped ``propertyId`` and unrelated partner
    identifiers. We require ``propertyId == numeric portal host``, an explicit
    available status, a native apartment number, and positive numeric rent.
    Floor-plan aggregates can therefore never pass this seam.
    """
    try:
        parsed_url = urlparse(source_url)
        expected_host = f"{root_id}.onlineleasing.realpage.com"
        if parsed_url.scheme not in {"http", "https"}:
            return []
        if (parsed_url.hostname or "").lower() != expected_host:
            return []
        payload = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("units"), list):
        return []

    scoped_units: list[dict[str, Any]] = []
    for raw in payload["units"]:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("propertyId") or "").strip() != root_id:
            continue
        if not str(raw.get("leaseStatus") or "").strip().upper().startswith(
            "AVAILABLE"
        ):
            continue
        # ``name`` is also present on floor-plan/catalogue entities in some
        # RealPage shapes. Require the endpoint's explicit apartment field.
        if not str(raw.get("unitNumber") or "").strip():
            continue
        scoped_units.append(raw)
    if not scoped_units:
        return []

    from ma_poc.core.identity import unit_has_real_anchor
    from ma_poc.pms.adapters.realpage_cws import parse_realpage_cws_getunits
    from ma_poc.validation.schema_gate import _is_positive_numeric

    rows = parse_realpage_cws_getunits(
        json.dumps({"units": scoped_units}, separators=(",", ":")),
        source_url,
    )
    strict: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        row["extraction_tier"] = ONLINELEASING_GETUNITS_TIER
        has_rent = any(
            _is_positive_numeric(row.get(field))
            for field in (
                "asking_rent",
                "market_rent_low",
                "market_rent_high",
                "rent_low",
                "rent_high",
            )
        )
        if not unit_has_real_anchor(row) or not has_rent:
            continue
        source_ids = row.get("source_ids")
        source_id = (
            str(source_ids.get("realpage_cws_unit_id") or "")
            if isinstance(source_ids, dict)
            else ""
        )
        key = (str(row.get("unit_number") or "").strip(), source_id)
        if key in seen:
            continue
        seen.add(key)
        strict.append(row)
    return strict


async def _direct_onlineleasing_get(
    url: str,
    root_id: str,
) -> tuple[int, str, str]:
    """Fetch one roster via plain HTTP with hard scope/time/size bounds.

    Ambient proxy variables are disabled with ``trust_env=False``. Redirects
    are followed manually only while they remain on the same numeric property
    host. The streamed response is abandoned once it exceeds 2 MB.
    """
    import httpx

    expected_host = f"{root_id}.onlineleasing.realpage.com"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_ONLINELEASING_PROBE_TIMEOUT_S),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json, application/x-javascript"},
        ) as client:
            current_url = url
            for _ in range(_ONLINELEASING_MAX_REDIRECTS + 1):
                async with client.stream("GET", current_url) as response:
                    status = int(response.status_code)
                    final_url = str(response.url)
                    if (urlparse(final_url).hostname or "").lower() != expected_host:
                        return status, "", final_url
                    if status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            return status, "", final_url
                        next_url = urljoin(final_url, location)
                        if (urlparse(next_url).hostname or "").lower() != expected_host:
                            return status, "", next_url
                        current_url = next_url
                        continue
                    if not 200 <= status < 300:
                        return status, "", final_url
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            if int(content_length) > _ONLINELEASING_MAX_BODY_BYTES:
                                return status, "", final_url
                        except ValueError:
                            pass
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > _ONLINELEASING_MAX_BODY_BYTES:
                            return status, "", final_url
                        chunks.append(chunk)
                    encoding = response.encoding or "utf-8"
                    try:
                        body = b"".join(chunks).decode(encoding, errors="replace")
                    except LookupError:
                        body = b"".join(chunks).decode("utf-8", errors="replace")
                    return status, body, final_url
            return 0, "", current_url
    except (httpx.HTTPError, ValueError):
        return 0, "", url


async def recover_onlineleasing_getunits(
    ctx: AdapterContext,
) -> AdapterResult | None:
    """Try the public GetUnits path for numeric Online Leasing roots.

    This is a direct, non-proxied GET. It never invokes Web Unlocker,
    FlareSolverr, CAPTCHA solving, or browser/fingerprint rotation. At most
    three property roots are tried and every transport/parser failure is
    isolated so the caller can preserve its native result.
    """
    try:
        from ma_poc.config.feature_flags import enable_cws_getunits

        if not enable_cws_getunits():
            return None
        roots = onlineleasing_roots_from_ctx(ctx)
        if not roots:
            return None

        from ma_poc.extraction.post_process import post_process

        for root_id in roots:
            url = _onlineleasing_getunits_url(root_id)
            try:
                status, body, final_url = await asyncio.wait_for(
                    _direct_onlineleasing_get(url, root_id),
                    timeout=_ONLINELEASING_PROBE_TIMEOUT_S + 3,
                )
            except Exception as exc:  # noqa: BLE001 - isolated recovery seam
                log.debug(
                    "realpage onlineleasing GetUnits probe failed root=%s err=%s",
                    root_id,
                    exc,
                )
                continue
            if status != 200 or not body:
                continue
            rows = parse_scoped_onlineleasing_getunits(
                body,
                final_url,
                root_id,
            )
            if not rows:
                continue
            processed = post_process(
                rows,
                property_id=getattr(ctx, "property_id", None),
            )
            # The parser already requires native identity + rent. Use only the
            # canonical unit partition here; never ``admitted`` (which also
            # contains plan summaries).
            if not processed.units:
                continue
            result = AdapterResult(tier_used=ONLINELEASING_GETUNITS_TIER)
            result.units = processed.units
            result.plan_summaries = processed.plan_summaries
            result.winning_url = final_url
            result.confidence = min(0.97, 0.75 + 0.03 * len(processed.units))
            result.api_responses.append(
                {
                    "url": final_url,
                    "status": status,
                    "body": "<onlineleasing-getunits>",
                    "via": "onlineleasing_getunits_direct",
                    "root_id": root_id,
                }
            )
            return result
    except Exception as exc:  # noqa: BLE001 - never break native adapters
        log.debug("realpage onlineleasing GetUnits recovery failed: %s", exc)
    return None


def row_has_strict_unit_rent(row: dict[str, Any]) -> bool:
    """Return True only for a canonical apartment with numeric rent."""
    from ma_poc.core.identity import unit_has_real_anchor
    from ma_poc.validation.schema_gate import _is_positive_numeric

    return unit_has_real_anchor(row) and any(
        _is_positive_numeric(row.get(field))
        for field in (
            "asking_rent",
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
        )
    )
# Public RealPage floor-plan widgets expose both values in their served HTML.
# The API key is intentionally browser-readable (the first-party widget sends
# it as ``x-ws-authkey``); it is never logged or persisted by this adapter.
_PUBLIC_WIDGET_PROPERTY_ID_RE = re.compile(
    r"\bpropertyId\s*(?:=|:)\s*['\"]?(\d+)", re.IGNORECASE
)
_PUBLIC_WIDGET_API_KEY_RE = re.compile(
    r"\bapiKey\s*:\s*['\"]([^'\"]+)", re.IGNORECASE
)
_PUBLIC_REALPAGE_PROPERTY_URL_RE = re.compile(
    r"api\.ws\.realpage\.com/v2/property/(\d+)/(?:floorplans|units)",
    re.IGNORECASE,
)


def dotnet_date_to_iso(raw: Any) -> str:
    """Convert a .NET ``/Date(ms-offset)/`` string to an ISO ``YYYY-MM-DD``.

    Parameters
    ----------
    raw:
        The raw ``AvailableDate`` value. Accepts the
        ``/Date(1779339600000-0500)/`` form, a bare epoch-ms int/str, or
        anything unparseable.

    Returns
    -------
    str
        ``YYYY-MM-DD`` on success, or ``""`` when ``raw`` is missing or
        cannot be parsed. Never raises.
    """
    if raw is None or raw == "":
        return ""
    s = str(raw).strip()
    ms: int | None = None
    m = _DOTNET_DATE_RE.search(s)
    if m:
        try:
            ms = int(m.group(1))
        except ValueError:
            ms = None
    elif s.lstrip("-").isdigit():
        # Bare epoch-ms fallback.
        try:
            ms = int(s)
        except ValueError:
            ms = None
    if ms is None:
        return ""
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return ""
    return dt.date().isoformat()


def _to_int(val: Any) -> int | None:
    """Coerce a numeric-ish value to int, or None when not numeric."""
    if val is None or val == "":
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _iter_oll_activities(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield every ``ApartmentSelectionLeaseMgmtActivity`` dict in a body.

    Walks ``Workflow.ActivityGroups[].GroupActivities[]``. Tolerant of
    missing/none-typed levels — returns an empty list rather than raising.
    """
    out: list[dict[str, Any]] = []
    workflow = body.get("Workflow")
    if not isinstance(workflow, dict):
        return out
    groups = workflow.get("ActivityGroups")
    if not isinstance(groups, list):
        return out
    for group in groups:
        if not isinstance(group, dict):
            continue
        activities = group.get("GroupActivities")
        if not isinstance(activities, list):
            continue
        for act in activities:
            if not isinstance(act, dict):
                continue
            atype = str(act.get("__type") or "")
            if "ApartmentSelectionLeaseMgmtActivity" in atype:
                out.append(act)
    return out


def parse_realpage_oll_workflow(body: dict[str, Any], source_url: str) -> list[dict[str, Any]]:
    """Parse a RealPage OLL ``Workflow`` PUT response into unit dicts.

    Walks ``Workflow.ActivityGroups[].GroupActivities[]``, finds each
    ``ApartmentSelectionLeaseMgmtActivity``, and emits one
    :func:`make_unit_dict` per ``Units[]`` entry. When an activity's
    floorplan has no ``Units`` (e.g. waitlist-only / fully leased), a
    single floorplan-summary record is emitted instead so the plan still
    surfaces with rent/sqft context.

    Parameters
    ----------
    body:
        The parsed JSON response body.
    source_url:
        The intercepted request URL, threaded into each unit dict for
        provenance.

    Returns
    -------
    list[dict]
        Standard unit dicts (possibly empty). Never raises on malformed
        input — a non-dict body or absent ``Workflow`` yields ``[]``.
    """
    units: list[dict[str, Any]] = []
    if not isinstance(body, dict):
        return units

    for activity in _iter_oll_activities(body):
        fp = activity.get("Floorplan")
        fp = fp if isinstance(fp, dict) else {}
        fp_name = str(fp.get("Name") or "")
        beds = _to_int(fp.get("Bedrooms"))
        baths_raw = fp.get("Bathrooms")
        baths = "" if baths_raw is None else str(baths_raw)
        fp_sqft = _to_int(fp.get("MinSquareFeet"))

        raw_units = activity.get("Units")
        unit_list = raw_units if isinstance(raw_units, list) else []
        real_units = [u for u in unit_list if isinstance(u, dict)]

        if real_units:
            for u in real_units:
                unit_no = str(u.get("UnitNumber") or u.get("Id") or "").strip()
                if not unit_no:
                    continue
                rent_lo = _to_int(u.get("MinPriceRange"))
                rent_hi = _to_int(u.get("MaxPriceRange"))
                sqft_val = _to_int(u.get("Squarefeet"))
                if sqft_val is None:
                    sqft_val = fp_sqft
                avail_iso = dotnet_date_to_iso(u.get("AvailableDate"))
                deposit = u.get("Deposit")
                units.append(
                    make_unit_dict(
                        floor_plan_name=fp_name,
                        bed_label=bed_label_from(beds, fp_name),
                        bedrooms=str(beds) if beds is not None else "",
                        bathrooms=baths,
                        sqft=str(sqft_val) if sqft_val is not None else "",
                        unit_number=unit_no,
                        rent_low=rent_lo,
                        rent_high=rent_hi,
                        deposit="" if deposit is None else str(deposit),
                        availability_status="AVAILABLE",
                        availability_date=avail_iso,
                        source_api_url=source_url,
                        extraction_tier=OLL_TIER,
                    )
                )
        else:
            # No-Units fallback: emit a floorplan-summary record so the plan
            # is not lost. ``Floorplan.Id`` is PLAN-scoped provenance, never
            # an apartment number; putting it in ``unit_number`` falsely
            # promoted aggregates to unit-level SUCCESS.
            fp_rent_lo = _to_int(fp.get("MinPriceRange"))
            fp_rent_hi = _to_int(fp.get("MaxPriceRange"))
            if not fp_name and fp_rent_lo is None and fp_rent_hi is None:
                continue
            avail_units = fp.get("AvailableUnits")
            units.append(
                make_unit_dict(
                    floor_plan_name=fp_name,
                    bed_label=bed_label_from(beds, fp_name),
                    bedrooms=str(beds) if beds is not None else "",
                    bathrooms=baths,
                    sqft=str(fp_sqft) if fp_sqft is not None else "",
                    unit_number="",
                    rent_range=format_rent_range(fp_rent_lo, fp_rent_hi),
                    availability_status="AVAILABLE",
                    available_units="" if avail_units is None else str(avail_units),
                    source_api_url=source_url,
                    extraction_tier=OLL_TIER,
                    source_ids={"floorplan_id": fp.get("Id")}
                    if fp.get("Id") is not None
                    else None,
                )
            )
    return units


def _is_oll_workflow_response(body: Any, url: str) -> bool:
    """True when a captured response looks like an OLL appstate Workflow.

    Cheap shape check: a dict body with a ``Workflow`` key, ideally from
    an ``appstate/v1`` URL. We accept on body-shape alone too, because
    the captured URL is sometimes the property origin (CORS-proxied) or
    truncated in the network log.
    """
    if not isinstance(body, dict):
        return False
    if "Workflow" not in body:
        return False
    workflow = body.get("Workflow")
    if not isinstance(workflow, dict):
        return False
    if "ActivityGroups" in workflow:
        return True
    # URL marker as a secondary signal.
    lu = url.lower()
    return any(m in lu for m in _OLL_URL_MARKERS)


class RealPageOllAdapter:
    """RealPage OLL (Online Leasing) PMS adapter.

    Handles two API shapes captured via browser Tier-1 interception:

      * ``api.ws.realpage.com/.../floorplans`` — shared OneSite envelope.
      * ``leasing.realpage.com/...appstate/v1/...OLL.SearchFloorPlan`` —
        the stateful OLL/BPM wizard PUT response (``Workflow`` shape).

    Both paths are scanned every run; whichever yields units wins. The
    OLL request is never forged server-side (DataDome/Akamai-gated) —
    only intercepted from the browser-loaded wizard.
    """

    pms_name: str = "realpage_oll"
    _fingerprints: list[str] = [
        "realpage.com",
        "leasing.realpage.com",
        "rp-leasing-widget",
        "rp.leasing.appservice",
        "/content/apply#k=",
    ]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from captured RealPage API responses.

        Scans ``ctx._api_responses`` for (a) OLL appstate ``Workflow``
        bodies, (b) the legacy ``/floorplans`` envelope, and (c) the
        split ``/units`` endpoint. Returns an :class:`AdapterResult`;
        confidence is 0.0 with an error string when nothing parses.
        """
        result = AdapterResult(tier_used=OLL_TIER)
        all_units: list[dict[str, Any]] = []
        floorplan_rows_found = False
        depth_rows_found = False

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        for resp in api_responses:
            body = resp.get("body")
            url = resp.get("url", "")

            # (a) OLL appstate Workflow PUT response — the Category-D path.
            if _is_oll_workflow_response(body, url):
                try:
                    units = parse_realpage_oll_workflow(body, url) if isinstance(body, dict) else []
                except Exception as exc:
                    units = []
                    result.errors.append(f"realpage-oll-workflow-parse-error: {exc}")
                if units:
                    all_units.extend(units)
                    result.api_responses.append(resp)
                    depth_rows_found = True
                continue

            # (b) Legacy/shared OneSite /floorplans envelope.
            if (
                isinstance(body, dict)
                and isinstance(body.get("response"), dict)
                and "floorplans" in body["response"]
            ):
                fp_units = parse_realpage_floorplans(body, url)
                if fp_units:
                    for u in fp_units:
                        u["extraction_tier"] = OLL_TIER
                    all_units.extend(fp_units)
                    result.api_responses.append(resp)
                    floorplan_rows_found = True

            # (c) Split /units endpoint (null / [] / {response:[...]}).
            elif _is_realpage_units_response(body, url):
                try:
                    u_units = _dr_realpage_units(body, url) or []
                except Exception as exc:
                    u_units = []
                    result.errors.append(f"realpage-units-parse-error: {exc}")
                if u_units:
                    for u in u_units:
                        u["extraction_tier"] = OLL_TIER
                    all_units.extend(u_units)
                    result.api_responses.append(resp)
                    depth_rows_found = True

        if all_units:
            # Native captured apartment rows keep precedence. A captured
            # floor-plan catalogue is useful context but is not a unit-level
            # win; try the numeric portal's public roster before returning it.
            if not any(row_has_strict_unit_rent(row) for row in all_units):
                portal_units = await recover_onlineleasing_getunits(ctx)
                if portal_units is not None:
                    from ma_poc.extraction.post_process import post_process

                    native = post_process(
                        all_units,
                        property_id=getattr(ctx, "property_id", None),
                    )
                    portal_units.plan_summaries = [
                        *native.plan_summaries,
                        *portal_units.plan_summaries,
                    ]
                    portal_units.api_responses = [
                        *result.api_responses,
                        *portal_units.api_responses,
                    ]
                    return portal_units
            # The July 31 fleet run captured only ``/floorplans`` for this
            # family even though the same public widget configuration exposes
            # a ``/units`` roster with ``internalAvailableDate``.  A plan
            # catalogue is therefore a checkpoint, not terminal success.  The
            # direct probe is tightly gated to that exact shape and never uses
            # an unlocker, CAPTCHA service, browser fingerprint rotation, or an
            # external model.  If it fails, the captured floor plans remain the
            # lossless fallback.
            if floorplan_rows_found and not depth_rows_found:
                public_units = await self._try_public_widget_units(
                    ctx, api_responses
                )
                if public_units is not None:
                    return public_units
            result.units = all_units
            result.winning_url = (
                result.api_responses[0].get("url") if result.api_responses else None
            )
            result.confidence = min(0.90, 0.7 + 0.05 * len(all_units))
        else:
            portal_units = await recover_onlineleasing_getunits(ctx)
            if portal_units is not None:
                return portal_units
            # 2026-07-30 (#85) — CWS GetUnits fallback. The LeaseLabs /
            # ``.floorplan-block`` .aspx theme is DETECTED realpage_oll but
            # exposes no OLL/units API in the captured responses; its unit
            # roster is served by the property-hosted CWS ``GetUnits`` proxy
            # (same static JSON endpoint + parser as realpage_cws). Additive:
            # only when the OLL API path found nothing, so it can never remove
            # rows. Live-verified 2026-07-30 on Sierra Verde (15 units) and
            # Meadowcrest (19).
            gu = await self._try_cws_getunits(ctx)
            if gu is not None:
                return gu
            result.confidence = 0.0
            result.errors.append("No RealPage OLL data found in captured API responses")

        return result

    async def _try_public_widget_units(
        self,
        ctx: AdapterContext,
        api_responses: list[dict[str, Any]],
    ) -> AdapterResult | None:
        """Enrich a floorplan-only capture from RealPage's public units API.

        The request is allowed only when the fetched marketing HTML carries a
        public ``apiKey`` and its ``propertyId`` agrees with the property id in
        the captured RealPage URL.  That agreement is an identity guard against
        cross-property contamination on multi-property operator sites.  The
        probe is direct-only and returns ``None`` on every failure so the
        already-captured floor-plan result remains available.
        """
        try:
            from ma_poc.pms.adapters._probe import body_html_from_ctx

            html = body_html_from_ctx(ctx)
            if not html:
                return None

            key_match = _PUBLIC_WIDGET_API_KEY_RE.search(html)
            html_id_match = _PUBLIC_WIDGET_PROPERTY_ID_RE.search(html)
            captured_ids = {
                match.group(1)
                for resp in api_responses
                if (match := _PUBLIC_REALPAGE_PROPERTY_URL_RE.search(
                    str(resp.get("url") or "")
                ))
            }
            if not key_match or len(captured_ids) > 1:
                return None

            html_id = html_id_match.group(1) if html_id_match else ""
            captured_id = next(iter(captured_ids), "")
            if html_id and captured_id and html_id != captured_id:
                return None
            property_id = html_id or captured_id
            if not property_id:
                return None

            referer = ""
            fr = getattr(ctx, "fetch_result", None)
            if fr is not None:
                referer = str(getattr(fr, "final_url", "") or "")
            referer = referer or str(getattr(ctx, "base_url", "") or "")
            parsed = urlparse(referer)
            origin = (
                f"{parsed.scheme}://{parsed.netloc}"
                if parsed.scheme and parsed.netloc
                else ""
            )
            units_url = (
                f"https://api.ws.realpage.com/v2/property/{property_id}/units"
                "?available=true&honordisplayorder=true"
            )
            headers = {
                "Accept": "application/json, text/plain, */*",
                "x-ws-authkey": key_match.group(1).strip(),
            }
            if origin:
                headers["Origin"] = origin
            if referer:
                headers["Referer"] = referer

            import asyncio

            from ma_poc.pms.adapters._probe import probe_get

            response = await asyncio.to_thread(
                probe_get,
                units_url,
                headers=headers,
                timeout=20,
                unlocker=False,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            if not 200 <= status < 300:
                return None
            body = json.loads(str(getattr(response, "text", "") or ""))
            rows = _dr_realpage_units(body, units_url) or []
            for row in rows:
                row["extraction_tier"] = OLL_TIER
            if not rows:
                return None

            from ma_poc.extraction.post_process import post_process

            pp = post_process(
                rows, property_id=getattr(ctx, "property_id", None)
            )
            if pp.n_unit_level <= 0:
                return None

            result = AdapterResult(tier_used=OLL_TIER)
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.winning_url = units_url
            result.confidence = min(0.95, 0.7 + 0.05 * pp.n_unit_level)
            result.api_responses.append(
                {
                    "url": units_url,
                    "status": status,
                    "body": "<realpage-public-units>",
                    "via": "realpage_public_widget_units",
                }
            )
            return result
        except Exception as exc:  # noqa: BLE001 - never break captured fallback
            import logging

            logging.getLogger(__name__).debug(
                "realpage_oll public-units enrichment failed: %s", exc
            )
            return None

    async def _try_cws_getunits(self, ctx: AdapterContext) -> AdapterResult | None:
        """Property-hosted CWS ``GetUnits`` proxy fallback (#85).

        Reuses realpage_cws's endpoint builder + JSON parser. The probe runs
        off the event loop (``asyncio.to_thread``). Returns unit-level rows on
        success, or ``None`` to leave the empty-result path intact (flag off,
        no base URL, fetch error, non-JSON, or zero available units). Never
        raises.
        """
        try:
            from ma_poc.config.feature_flags import enable_cws_getunits

            if not enable_cws_getunits():
                return None
            from ma_poc.pms.adapters.realpage_cws import (
                cws_getunits_url,
                parse_realpage_cws_getunits,
            )

            base = ""
            fr = getattr(ctx, "fetch_result", None)
            if fr is not None:
                base = str(getattr(fr, "final_url", "") or "")
            base = base or (getattr(ctx, "base_url", "") or "")
            url = cws_getunits_url(base)
            if not url:
                return None

            import asyncio

            from ma_poc.pms.adapters._probe import probe_get

            r = await asyncio.to_thread(probe_get, url, timeout=20, unlocker=False)
            body = getattr(r, "text", "") or ""
            rows = parse_realpage_cws_getunits(body, url)
            if not rows:
                return None

            from ma_poc.extraction.post_process import post_process

            pp = post_process(rows, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted <= 0:
                return None
            result = AdapterResult(tier_used="TIER_1_API_REALPAGE_CWS_UNITS")
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.winning_url = url
            result.confidence = min(0.95, 0.7 + 0.05 * pp.n_admitted)
            result.api_responses.append(
                {
                    "url": url,
                    "status": getattr(r, "status_code", 200),
                    "body": "<cws-getunits>",
                    "via": "cws_getunits_oll_fallback",
                }
            )
            return result
        except Exception as exc:  # noqa: BLE001 — never break the adapter
            import logging

            logging.getLogger(__name__).debug(
                "realpage_oll cws-getunits fallback failed: %s", exc
            )
            return None

    def matches_response_body(self, body: Any) -> bool:
        """Body-shape check for ``detector.confirm_detection``.

        Returns True for the OLL ``Workflow`` shape OR the legacy RealPage
        ``{response: {floorplans:[...]}}`` envelope, so a URL-detected OLL
        property is not demoted to ``unknown`` when the wizard's appstate
        response was captured.
        """
        if not isinstance(body, dict):
            return False
        if _is_oll_workflow_response(body, ""):
            return True
        response = body.get("response")
        return isinstance(response, dict) and "floorplans" in response

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
