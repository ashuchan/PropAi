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
The ``leasing.realpage.com`` OLL endpoint is behind DataDome + Akamai Bot
Manager plus a browser-minted ``xyz`` base64 header token, and is a
*stateful* PUT threaded by ``ClientSessionID`` + incrementing
``BpmSequence``/``LogSequence``. A server-side curl/httpx replay is
bot-blocked and session-less. The ONLY viable strategy is
**browser-based Tier-1 API interception**: the pipeline's patchright
browser loads the OLL wizard at ``<property>/content/apply`` (which
passes DataDome as a legit browser, mints the token, and bootstraps the
appstate session automatically), and this adapter parses the
``OLL.SearchFloorPlan`` PUT response(s) from the captured network log
(``ctx._api_responses``). The request is never forged server-side.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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
            # No-Units fallback: emit a floorplan-summary record so the
            # plan is not lost (matches OneSite's null-/units behaviour).
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
                    unit_number=str(fp.get("Id") or ""),
                    rent_range=format_rent_range(fp_rent_lo, fp_rent_hi),
                    availability_status="AVAILABLE",
                    available_units="" if avail_units is None else str(avail_units),
                    source_api_url=source_url,
                    extraction_tier=OLL_TIER,
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

        if all_units:
            result.units = all_units
            result.winning_url = (
                result.api_responses[0].get("url") if result.api_responses else None
            )
            result.confidence = min(0.90, 0.7 + 0.05 * len(all_units))
        else:
            result.confidence = 0.0
            result.errors.append("No RealPage OLL data found in captured API responses")

        return result

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
