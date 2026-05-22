"""
OneSite (RealPage) adapter.

Research log
------------
Web sources consulted:
  - https://www.realpage.com/property-management-software/onesite/ — OneSite product (accessed 2026-04-17)
  - RealPage API patterns from scripts/entrata.py and scrape_properties.py
Real payloads inspected (from data/runs/*/raw_api/):
  - 293707 — api.ws.realpage.com/v2/property/7824595/floorplans returning
    {status, message, response: {propertyKey, floorplans: [...]}} with fields:
    id, name, bedRooms, bathRooms, minimumSquareFeet, maximumSquareFeet,
    minimumMarketRent, maximumMarketRent, rentRange, depositAmount, numberOfUnitsDisplay
  - 293707 (run 2026-04-14) — same endpoint, identical schema
Key findings:
  - API endpoint: api.ws.realpage.com/v2/property/{property_id}/floorplans
    and api.ws.realpage.com/v2/property/{property_id}/units (may be null)
  - Response envelope: {status, message, response: {floorplans: [...]}}
  - Unit ID field: id (floorplan-level)
  - Rent field(s): minimumMarketRent/maximumMarketRent (numbers), rentRange (display string)
  - Known gotchas: /units endpoint can return null, [], or {response: null} — three
    shapes for "no availability". When /units is null, emit floorplan-level records
    with rent but no unit_number. Split-endpoint pattern: floorplans + units are
    separate API calls. OneSite URLs have numeric prefix: {id}.onlineleasing.realpage.com
"""

from __future__ import annotations

import re
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

if TYPE_CHECKING:
    from playwright.async_api import Page


# 2026-05-13 port: aliases the RealPage /floorplans payload may carry
# for the first-available date. Pre-port these keys were dropped,
# producing fleet-wide 0% available_date on TIER_1_API_ONESITE.
# schema_v2._format_date handles "NOW" / relative / 2-digit forms
# downstream; ``make_unit_dict`` emits both ``availability_date`` and
# ``available_date`` (Commit 4 dual emission) so the v2 reader picks
# the value up.
_ONESITE_AVAIL_DATE_ALIASES: tuple[str, ...] = (
    "availableDate",
    "firstAvailableDate",
    "dateAvailable",
    "minimumAvailableDate",
    "availabilityDate",
    "available_date",
    "minAvailableDate",
)


def _first_alias(item: dict[str, Any], aliases: tuple[str, ...]) -> str:
    """First non-empty string value among *aliases* in *item*; '' otherwise."""
    for k in aliases:
        v = item.get(k)
        if v:
            return str(v)
    return ""


def parse_realpage_floorplans(body: dict[str, Any], url: str) -> list[dict[str, str]]:
    """Parse RealPage /floorplans response into standard unit dicts.

    2026-05-13 port: now extracts first-available date via the 7-alias
    lookup (``_ONESITE_AVAIL_DATE_ALIASES``).
    """
    units: list[dict[str, str]] = []
    response = body.get("response", {})
    if not isinstance(response, dict):
        return units
    floorplans = response.get("floorplans") or []
    if not isinstance(floorplans, list):
        return units

    for fp in floorplans:
        if not isinstance(fp, dict):
            continue
        name = str(fp.get("name") or "")
        beds_raw = fp.get("bedRooms")
        baths_raw = fp.get("bathRooms")
        beds = int(beds_raw) if beds_raw is not None and str(beds_raw).isdigit() else None
        baths = int(baths_raw) if baths_raw is not None and str(baths_raw).isdigit() else None

        sqft_lo = str(fp.get("minimumSquareFeet") or "")
        sqft_hi = str(fp.get("maximumSquareFeet") or "")
        sqft = sqft_lo if sqft_lo == sqft_hi or not sqft_hi else f"{sqft_lo}-{sqft_hi}"

        rent_lo_raw = fp.get("minimumMarketRent")
        rent_hi_raw = fp.get("maximumMarketRent")
        rent_lo = int(rent_lo_raw) if isinstance(rent_lo_raw, (int, float)) else None
        rent_hi = int(rent_hi_raw) if isinstance(rent_hi_raw, (int, float)) else None

        deposit = str(fp.get("depositAmount") or "")
        num_units = str(fp.get("numberOfUnitsDisplay") or "")

        avail_dt = _first_alias(fp, _ONESITE_AVAIL_DATE_ALIASES)

        units.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths) if baths is not None else "",
                sqft=sqft,
                unit_number=str(fp.get("id") or ""),
                rent_range=format_rent_range(rent_lo, rent_hi),
                deposit=deposit,
                availability_status="AVAILABLE",
                availability_date=avail_dt,
                available_units=num_units,
                source_api_url=url,
                extraction_tier="TIER_1_API_ONESITE",
            )
        )
    return units


def _is_realpage_response(body: Any) -> bool:
    """Check if a response body looks like a RealPage API response."""
    if not isinstance(body, dict):
        return False
    response = body.get("response")
    if isinstance(response, dict) and "floorplans" in response:
        return True
    return False


def _is_realpage_units_response(body: Any, url: str) -> bool:
    """Check for the RealPage /units endpoint shape.

    RealPage splits data across /floorplans and /units. The /units endpoint
    can return null, [], or {response: null} when nothing is available, and
    a list of unit dicts when there is. daily_runner's
    _realpage_units_from_body handles all three shapes.
    """
    if "realpage" not in url.lower():
        return False
    if "/units" not in url.lower():
        return False
    # Accept null / [] / {response: [...]}
    return True


class OneSiteAdapter:
    """OneSite (RealPage) PMS adapter."""

    pms_name: str = "onesite"
    _fingerprints: list[str] = ["onlineleasing.realpage.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from RealPage API responses captured during page load."""
        from ma_poc.pms.adapters._adapter_telemetry import log_adapter_stage

        pid = str(getattr(ctx, "property_id", "") or "unknown")
        result = AdapterResult(tier_used="TIER_1_API_ONESITE")
        all_units: list[dict[str, str]] = []

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        # F7a (2026-05-20): track the property_id we saw in /floorplans so
        # we can probe the matching /units endpoint when /units wasn't
        # captured during page load. RealPage emits /floorplans nearly
        # always (Apply Now button auto-fetches it) but /units only fires
        # when the user clicks into a specific plan — link-hop crawlers
        # often miss it. 9 properties on 2026-05-20 had /floorplans but
        # not /units; all 9 shipped null availability_date.
        realpage_property_id: str | None = None
        units_captured = False
        n_floorplans_resp = 0
        n_units_resp = 0
        for resp in api_responses:
            body = resp.get("body")
            url = resp.get("url", "")
            if _is_realpage_response(body) and isinstance(body, dict):
                n_floorplans_resp += 1
                units = parse_realpage_floorplans(body, url)
                if units:
                    all_units.extend(units)
                    result.api_responses.append(resp)
                # Extract the numeric property_id from the /floorplans URL
                # for the F7a /units probe. Pattern:
                # `api.ws.realpage.com/v2/property/{NUMERIC}/floorplans`.
                if realpage_property_id is None:
                    m = re.search(r"/property/(\d+)/floorplans", url, re.IGNORECASE)
                    if m:
                        realpage_property_id = m.group(1)
            elif _is_realpage_units_response(body, url):
                n_units_resp += 1
                units_captured = True
                # RealPage /units endpoint — body may be null/[]/{response:[...]}
                try:
                    units = _dr_realpage_units(body, url) or []
                except Exception as exc:
                    units = []
                    result.errors.append(f"realpage-units-parse-error: {exc}")
                if units:
                    all_units.extend(units)
                    result.api_responses.append(resp)

        # XHR-capture telemetry — diagnoses "captured API noise but no
        # RealPage shape" vs "no captures at all" vs "captured but parser
        # rejected". This was the single most-asked-for signal in the
        # 2026-05-21 OneSite cluster investigation.
        log_adapter_stage(
            "onesite",
            pid,
            "xhr_capture",
            "ok" if all_units else "no_realpage_shape" if api_responses else "no_api_responses",
            units=len(all_units),
            reason=(
                f"network_responses={len(api_responses)} "
                f"floorplans_matched={n_floorplans_resp} units_matched={n_units_resp} "
                f"realpage_property_id={realpage_property_id}"
            ),
            n_responses=len(api_responses),
            floorplans_matched=n_floorplans_resp,
            units_matched=n_units_resp,
            realpage_property_id=realpage_property_id,
        )

        # F7a (2026-05-20): when /floorplans was captured but /units wasn't,
        # probe /units explicitly. The URL pattern is deterministic; if it
        # returns a non-null body we merge the per-unit rows (which include
        # availability_date) over the plan-level rows we already have.
        # Routes via stealth_probe so we inherit the property's sticky
        # Chrome identity + captcha-detect logic.
        if realpage_property_id and not units_captured and all_units:
            try:
                _u = await _probe_realpage_units_endpoint(
                    realpage_property_id,
                    canonical_id=getattr(ctx, "property_id", None),
                )
                if _u:
                    all_units.extend(_u)
                    # Synthesise a pseudo-response entry so the api_responses
                    # bookkeeping reflects the probe.
                    result.api_responses.append({
                        "url": _realpage_units_url(realpage_property_id),
                        "probed": True,
                        "unit_count": len(_u),
                    })
            except Exception as exc:
                result.errors.append(f"realpage-units-probe-error: {exc}")

        if all_units:
            # F7d (2026-05-20 PID 11317): OneSite /floorplans-only payloads
            # carry plan-level rows with no per-unit availability date. The
            # marketing page often renders the same availability info as
            # `data-availability` / `data-available-date` attributes on
            # apartment cards (verified live against dixonatstonegate.com:
            # 11 data-availability attrs alongside the 11 plan rows). Merge
            # those dates onto the units by unit_id before the validity
            # gate runs.
            try:
                page_html = _read_page_html_from_ctx(ctx)
                if page_html:
                    _augment_units_with_dom_availability(all_units, page_html)
            except Exception as exc:
                result.errors.append(f"onesite-dom-aug-error: {exc}")

            # Stage 1 validity gate.
            from ma_poc.extraction.post_process import post_process

            _pp_parsed = len(all_units)
            _pp = post_process(all_units, property_id=getattr(ctx, "property_id", None))
            if _pp.n_admitted > 0:
                # D16: strict unit-level / plan-level partition.
                result.units = list(_pp.units)
                result.plan_summaries = list(_pp.plan_summaries)
                result.post_process_meta = _pp.to_meta()
                result.winning_url = (
                    result.api_responses[0].get("url") if result.api_responses else None
                )
                result.confidence = min(0.95, 0.7 + 0.05 * _pp.n_admitted)
                # Keep the bare TIER_1_API_ONESITE label for the success path.
            else:
                # 2026-05-13 port: distinct label for "parsed rows but
                # validity gate rejected all". Unblocks Phase-B retry --
                # bare TIER_1_API_ONESITE pre-port indicated success and
                # locked out the retry path.
                result.tier_used = "TIER_1_API_ONESITE_EMPTY"
                result.confidence = 0.0
                result.errors.append(
                    f"ONESITE_VALIDITY_REJECTED: {_pp_parsed} parsed rows "
                    f"failed unit_validity (no numeric dimension)"
                )
        else:
            # 2026-05-13 port: distinct label for "no RealPage-shaped
            # responses captured at all" -- the cluster #6 OLL-widget-shell
            # pattern where the page loaded but nothing fired. Pre-port
            # this also reported TIER_1_API_ONESITE, masking the
            # difference between "we saw data but rejected it" and "we
            # saw nothing".
            result.tier_used = "TIER_1_API_ONESITE_NO_RESPONSE"
            result.confidence = 0.0
            result.errors.append("No RealPage/OneSite floorplan data found in captured API responses")

        log_adapter_stage(
            "onesite",
            pid,
            "cascade_exit",
            result.tier_used or "unknown",
            units=len(result.units),
            reason=(
                f"errors={'|'.join(str(e)[:60] for e in result.errors[:2])[:160]}"
                if result.errors else ""
            ),
        )
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)


# F7d (2026-05-20): OneSite DOM data-availability augmentation
# ─────────────────────────────────────────────────────────────────────
# OneSite's API exposes /floorplans (plan-level) and /units (per-unit).
# When only /floorplans is captured — which is the common case for
# RealPage tenants whose page doesn't trigger the /units XHR — the
# adapter ships plan rows with `availability_date=""` by design (the
# /floorplans endpoint has no date field). The MARKETING site, however,
# typically renders per-unit availability as `data-availability` or
# `data-available-date` attributes on the apartment-card DOM elements.
# Verified live against dixonatstonegate.com 2026-05-20: 11
# data-availability attributes matching the 11 emitted units 1:1.
#
# This DOM augmentation pass extracts (unit_id, date) pairs from the
# page HTML and merges them into the API-extracted units. Non-destructive
# — only fills `availability_date` when the unit lacks one.

# RealPage card data-attr shape — observed live on dixonatstonegate.com:
#   <article data-unit-id="2604014" data-availability="2026-06-15" ...>
# Attribute names vary slightly by tenant; we accept several aliases.
_REALPAGE_DOM_AVAIL_RE = re.compile(
    r'data-(?:availability|available[-_]date|move[-_]in[-_]date|ready[-_]date)'
    r'\s*=\s*"([^"]+)"',
    re.IGNORECASE,
)
_REALPAGE_DOM_UNIT_ID_RE = re.compile(
    r'data-(?:unit[-_]id|unit[-_]number|apartment[-_]id|listing[-_]id)'
    r'\s*=\s*"([^"]+)"',
    re.IGNORECASE,
)


def _read_page_html_from_ctx(ctx: "AdapterContext") -> str:
    """Pull rendered HTML off the AdapterContext fetch_result body."""
    fr = getattr(ctx, "fetch_result", None)
    if fr is None:
        return ""
    body = getattr(fr, "body", None)
    if isinstance(body, bytes):
        try:
            return body.decode("utf-8", errors="replace")
        except Exception:
            return ""
    if isinstance(body, str):
        return body
    return ""


def _extract_dom_avail_pairs(html: str) -> dict[str, str]:
    """Scan `html` for `data-availability`-shaped attribute pairs.

    Returns a dict mapping `unit_id` (or `unit_number`) → date string.
    Best-effort: walks tag-by-tag using BeautifulSoup so we only pair
    an availability attr with the unit_id from the SAME element (not
    one elsewhere in the document — that would cross-pollute IDs).
    """
    out: dict[str, str] = {}
    if not html or len(html) > 5_000_000:
        # Bound the scan — runaway HTML inputs shouldn't burn parser time.
        return out
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return out

    # Find every element that has at least one of the date attributes.
    date_attrs = ("data-availability", "data-available-date",
                  "data-move-in-date", "data-ready-date")
    id_attrs = ("data-unit-id", "data-unit-number", "data-apartment-id",
                "data-listing-id")
    try:
        for el in soup.find_all(True):
            attrs = getattr(el, "attrs", None) or {}
            # Lowercase-key view to handle camelCase / PascalCase attrs.
            attrs_lower = {k.lower(): v for k, v in attrs.items() if isinstance(k, str)}
            date_val = None
            for a in date_attrs:
                v = attrs_lower.get(a)
                if v:
                    date_val = str(v).strip()
                    break
            if not date_val:
                continue
            uid_val = None
            for a in id_attrs:
                v = attrs_lower.get(a)
                if v:
                    uid_val = str(v).strip()
                    break
            if uid_val and date_val:
                # First seen wins — RealPage repeats the apartment card
                # in mobile + desktop renders sometimes.
                out.setdefault(uid_val, date_val)
    except Exception:
        return out
    return out


def _augment_units_with_dom_availability(
    units: list[dict[str, Any]], page_html: str
) -> None:
    """Merge DOM-sourced availability dates onto API-extracted units.

    Non-destructive: only fills `availability_date` when the unit lacks
    one. Matches by `unit_id` first, then `unit_number`. Does not modify
    the list structure — only mutates dicts in place.
    """
    pairs = _extract_dom_avail_pairs(page_html)
    if not pairs:
        return
    for u in units:
        if u.get("availability_date"):
            continue  # already set by API path — don't overwrite
        # Try unit_id then unit_number as the join key.
        uid = str(u.get("unit_id") or u.get("unit_number") or "").strip()
        if not uid:
            continue
        date_val = pairs.get(uid)
        if date_val:
            u["availability_date"] = date_val


# F7a (2026-05-20): explicit /units endpoint probe
# ─────────────────────────────────────────────────────────────────────
# When /floorplans is captured but /units isn't, this probe fetches the
# matching /units endpoint via the stealth-probe path (sticky Chrome
# identity + captcha-detect). Same posture as `_probe_realpage_cws` in
# pms/adapters/generic.py.
#
# Coverage: ~9 properties / day on 2026-05-20 — all OneSite tenants whose
# marketing page didn't trigger the /units XHR during the entry-page load.


def _realpage_units_url(property_id: str) -> str:
    """Canonical /units endpoint URL for a numeric RealPage property_id."""
    return f"https://api.ws.realpage.com/v2/property/{property_id}/units"


async def _probe_realpage_units_endpoint(
    property_id: str,
    canonical_id: str | None = None,
) -> list[dict[str, str]]:
    """Fetch + parse the RealPage /units endpoint.

    Returns adapter-shape unit dicts (list-of-dict with floor_plan_name,
    bedrooms, bathrooms, sqft, unit_number, rent_range, availability_date)
    matching `realpage_units_to_adapter_shape`. Empty list on any failure
    (probe captcha-blocked, body null/empty, parse error).

    Routes via `stealth_probe` for identity stickiness — RealPage's WAF
    profiles UA + TLS fingerprint per source IP so a coherent identity
    across the entry-page fetch and this probe avoids the
    "Frankenstein session" detection signal.
    """
    if not property_id or not property_id.isdigit():
        return []
    url = _realpage_units_url(property_id)
    try:
        from ma_poc.fetch.probe import stealth_probe

        body, status, captcha_provider = await stealth_probe(
            url,
            method="GET",
            property_id=canonical_id or property_id,
            timeout_seconds=15.0,
            telemetry_context="realpage_units_probe",
        )
        if status != 200 or captcha_provider or not body:
            return []
        import json as _json
        try:
            data = _json.loads(body)
        except (ValueError, TypeError):
            return []
        return _dr_realpage_units(data, url) or []
    except Exception:
        return []
