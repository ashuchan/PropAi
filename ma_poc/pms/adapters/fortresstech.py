"""FortressTech leasing-portal adapter — unit-level via iframe SSR data.

FortressTech (``fortresstech.io``) is a Next.js-based leasing-portal
vendor used by small/mid operators who run their public marketing site on
Squarespace (and occasionally Wix). The marketing page embeds a single
iframe pointing at one of two equivalent subdomains:

    https://www.availability.fortresstech.io/unit-availability/{orgId}/{propertyId}/
    https://www.embed.fortresstech.io/unit-availability/{orgId}/{propertyId}/

Both subdomains serve the same Next.js app — only the host alias differs.
A third subdomain (``portal.fortresstech.io``) handles auth / contact-us
and never carries unit data.

Unit-level data is delivered via React Query SSR hydration in
``<script>self.__next_f.push([1, "<chunk>"])</script>`` blocks. The chunk
of interest contains ``"queryKey":["units"]`` and the embedded
``"data":[…]`` array carries one entry per available unit, e.g.:

    {
      "unitId": "95e1da1e-...",
      "unitNumber": "CPC-105",
      "unitQuotingRent": 1095,
      "unitMoveInDate": "2026-08-15",
      "floorPlanName": "The Sheyenne",
      "floorPlanBeds": 1,
      "floorPlanBaths": 1,
      "floorPlanSquareFeet": 777,
      "floorPlanMarketingDescription": "1b/1b"
    }

The iframe URL responds with HTTP 500 (a Next.js soft-error code) even on
otherwise-successful requests, but the SSR-hydration payload is fully
streamed in the response body regardless. The adapter therefore ignores
the status code and parses if the body carries the expected push chunks.

Live-verified 2026-05-25 (canary 1ef1060 regr#14) on PRG Property
Resources Group portfolio — Carlson Place Apartments (9 units across 3
floor plans). Sister sites on prg.propertyresourcesgroup.com use the
same pattern; ``embed.fortresstech.io`` subdomain confirmed on Eagle
Lake.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

_TIER = "TIER_1_SSR_FORTRESSTECH"
_TIER_NO_IFRAME = f"{_TIER}_NO_IFRAME"
_TIER_FETCH_ERROR = f"{_TIER}_FETCH_ERROR"
_TIER_NO_SSR_CHUNK = f"{_TIER}_NO_SSR_CHUNK"
_TIER_EMPTY = f"{_TIER}_EMPTY"
_TIER_VALIDITY_REJECTED = f"{_TIER}_VALIDITY_REJECTED"

# Iframe ``src`` pointing at the FortressTech unit-availability widget.
# Accepts both ``availability.fortresstech.io`` and
# ``embed.fortresstech.io`` (both subdomains serve the same Next.js app;
# ``portal.fortresstech.io`` is the auth/contact host and carries no units).
_IFRAME_SRC_RE = re.compile(
    r'<iframe[^>]*\bsrc=["\']'
    r'(?P<url>https?://[^"\']*(?:availability|embed)\.fortresstech\.io/'
    r'unit-availability/[0-9a-f-]{20,}/[0-9a-f-]{20,}/?[^"\']*)["\']',
    re.IGNORECASE,
)

# Each Next.js streaming chunk is wrapped in ``self.__next_f.push([1,
# "<json-string>"])``. We capture the JSON-quoted string verbatim and let
# ``json.loads`` unescape it.
_NEXT_PUSH_RE = re.compile(
    r"self\.__next_f\.push\(\[\s*1\s*,\s*(\"(?:[^\"\\]|\\.)*\")\s*\]\)",
    re.DOTALL,
)


def find_fortresstech_iframe_url(html: str) -> str | None:
    """Return the FortressTech ``/unit-availability/`` iframe URL, or None.

    Both ``availability.`` and ``embed.`` subdomains are accepted; the
    auth-only ``portal.`` host is rejected by the regex.
    """
    if not html:
        return None
    m = _IFRAME_SRC_RE.search(html)
    return m.group("url") if m else None


def _prefer_availability_host(url: str) -> str:
    """Rewrite an ``embed.fortresstech.io`` iframe URL to the ``availability.``
    host (2026-07-19 roster-confirmation gap #6).

    The ``embed.`` subdomain now serves an empty ~437B client-only shell (no
    ``self.__next_f`` SSR chunk), so the parser found 0 units. The identical
    Next.js app is still SSR-rendered on the ``www.availability.`` alias, which
    carries the hydration chunks. Rewriting is safe when ``embed.`` already
    worked (same app, host alias only). No-op for non-``embed.`` URLs.
    """
    if not url:
        return url
    return url.replace(
        "://www.embed.fortresstech.io", "://www.availability.fortresstech.io"
    ).replace("://embed.fortresstech.io", "://www.availability.fortresstech.io")


# ``<host>.fortresstech.io/{orgId}/{propertyId}/…`` — the org/property UUID pair,
# present on the ``portal.`` register link even when no embed iframe is emitted.
_FT_ORG_PROP_RE = re.compile(
    r"fortresstech\.io/([0-9a-f]{8}-[0-9a-f-]{20,})/([0-9a-f]{8}-[0-9a-f-]{20,})",
    re.IGNORECASE,
)


def fortresstech_availability_url(html: str) -> str | None:
    """Build the SSR availability URL from any FortressTech id pair in *html*.

    Real-world FortressTech marketing pages often embed only the auth-host
    ``portal.fortresstech.io/{orgId}/{propertyId}/register`` link (HTML-entity
    encoded), NOT an ``<iframe>`` to the availability widget. The org/property
    UUIDs are identical across hosts, so we extract them and build the
    ``www.availability.`` SSR URL that carries the ``self.__next_f`` roster.
    Returns ``None`` when no id pair is present.
    """
    if not html:
        return None
    m = _FT_ORG_PROP_RE.search(html)
    if not m:
        return None
    return (
        "https://www.availability.fortresstech.io/unit-availability/"
        f"{m.group(1)}/{m.group(2)}/"
    )


def _balanced_json_array(chunk: str, start: int) -> str | None:
    """Return the JSON array substring beginning at ``chunk[start]`` (which
    must be ``[``), balanced over nested brackets and string literals.

    Returns None when the open bracket has no matching close.
    """
    if start >= len(chunk) or chunk[start] != "[":
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(chunk)):
        c = chunk[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if in_str:
            if c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return chunk[start : i + 1]
    return None


def _extract_units_from_ssr_chunk(chunk: str) -> list[dict[str, Any]] | None:
    """Find the inner ``"data":[…]`` array of unit dicts in *chunk*.

    The chunk is the unescaped Next.js Flight payload string. Returns the
    parsed unit list, ``[]`` when the SSR query returned an empty result,
    or ``None`` when no units array is recognisable.
    """
    if '"queryKey":["units"]' not in chunk:
        return None
    qk_idx = chunk.index('"queryKey":["units"]')
    # The units array is the innermost ``"data":[`` that precedes the
    # ``queryKey`` marker in the same query block. Multiple ``"data":``
    # keys exist at different nesting levels (outer ``state.data``
    # wrapper, inner ``state.data.data`` array of units) — walk
    # candidates from innermost outward.
    candidates = [m.end() - 1 for m in re.finditer(r'"data":\s*\[', chunk[:qk_idx])]
    for open_idx in reversed(candidates):
        arr_str = _balanced_json_array(chunk, open_idx)
        if not arr_str:
            continue
        try:
            arr = json.loads(arr_str)
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(arr, list):
            continue
        # Empty units array is a legitimate "no availability" answer; the
        # outer ``"meta":{"count":0}`` confirms intent. Return [] so the
        # caller emits a clean LIST_EMPTY rather than retrying nested
        # candidates that match unrelated ``data`` keys.
        if not arr:
            return []
        first = arr[0]
        if isinstance(first, dict) and (
            "unitNumber" in first or "unitId" in first or "unitQuotingRent" in first
        ):
            return arr
    return None


def _items_to_units(items: list[dict[str, Any]], source_url: str) -> list[dict[str, str]]:
    """Map FortressTech unit dicts to the standard unit dict shape."""
    out: list[dict[str, str]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        unit_no = str(it.get("unitNumber") or "").strip()
        if not unit_no:
            continue

        plan_name = str(it.get("floorPlanName") or "").strip()
        beds_raw = it.get("floorPlanBeds")
        try:
            beds = int(beds_raw) if beds_raw is not None else None
        except (TypeError, ValueError):
            beds = None
        baths_raw = it.get("floorPlanBaths")
        try:
            baths_f = float(baths_raw) if baths_raw is not None else None
        except (TypeError, ValueError):
            baths_f = None
        baths_s = ""
        if baths_f is not None:
            baths_s = str(int(baths_f)) if baths_f.is_integer() else str(baths_f)

        sqft_raw = it.get("floorPlanSquareFeet")
        sqft = ""
        if sqft_raw not in (None, "", 0):
            try:
                sqft = str(int(float(sqft_raw)))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                sqft = ""

        rent = money_to_int(str(it.get("unitQuotingRent"))) if it.get("unitQuotingRent") is not None else None

        avail_date = str(it.get("unitMoveInDate") or "").strip()

        unit_id_raw = it.get("unitId")
        source_ids: dict[str, Any] = {}
        if isinstance(unit_id_raw, str) and unit_id_raw:
            source_ids["fortresstech_unit_id"] = unit_id_raw

        out.append(
            make_unit_dict(
                floor_plan_name=plan_name,
                bed_label=bed_label_from(beds, plan_name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=baths_s,
                sqft=sqft,
                unit_number=unit_no,
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                available_units="1",
                availability_date=avail_date,
                source_api_url=source_url,
                extraction_tier=_TIER,
                source_ids=source_ids,
            )
        )
    return out


def parse_fortresstech_iframe_html(html: str, source_url: str) -> list[dict[str, str]]:
    """Walk Next.js SSR push chunks; return unit dicts when units found.

    Returns ``[]`` when the iframe streamed an empty units list (valid
    "no availability" state) or when no SSR chunks parse at all — callers
    distinguish these via the tier code they stamp on the result.
    """
    if not html or "self.__next_f" not in html:
        return []
    for m in _NEXT_PUSH_RE.finditer(html):
        raw = m.group(1)
        try:
            chunk = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(chunk, str):
            continue
        if '"queryKey":["units"]' not in chunk:
            continue
        items = _extract_units_from_ssr_chunk(chunk)
        if items is None:
            continue
        return _items_to_units(items, source_url)
    return []


async def _fetch(url: str) -> tuple[int, str]:
    """probe_get wrapper — returns (status_code, body_text).

    FortressTech responds with HTTP 500 even when the SSR payload is
    complete, so callers must NOT gate on status code.
    """
    from ma_poc.pms.adapters._probe import probe_get

    r = probe_get(url, timeout=25)
    return int(getattr(r, "status_code", 0) or 0), (r.text or "")


class FortressTechAdapter:
    """FortressTech leasing-portal adapter.

    Two-step pipeline:

    1. Locate the ``/unit-availability/{orgId}/{propertyId}/`` iframe URL
       in the marketing-page HTML body (``ctx.fetch_result.body``).
    2. Fetch the iframe URL and parse Next.js Flight SSR hydration chunks
       to recover unit-level rows. Status code is ignored: the iframe
       returns HTTP 500 but the React-Query-hydrated payload is fully
       present in the response body regardless.
    """

    pms_name: str = "fortresstech"
    _fingerprints: list[str] = [
        "availability.fortresstech.io",
        "embed.fortresstech.io",
        "fortresstech.io/unit-availability",
    ]

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    def matches_response_body(self, body: Any) -> bool:
        """True when *body* carries the FortressTech SSR hydration shell."""
        if isinstance(body, str):
            low = body.lower()
            return "fortresstech.io" in low or '"querykey":["units"]' in low
        if isinstance(body, bytes):
            try:
                return self.matches_response_body(body.decode("utf-8", errors="replace"))
            except Exception:
                return False
        return False

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_TIER)

        html = ""
        fr = getattr(ctx, "fetch_result", None)
        body = getattr(fr, "body", None) if fr is not None else None
        if isinstance(body, bytes):
            html = body.decode("utf-8", errors="replace")
        elif isinstance(body, str):
            html = body

        # gap #6 (2026-07-19): embed. now serves an empty client shell, and many
        # marketing pages carry only the ``portal.fortresstech.io/{org}/{prop}``
        # register link (no embed iframe). Prefer a direct iframe (rewritten to
        # the SSR availability. host); else build the availability URL from the
        # org/property id pair found anywhere in the HTML.
        iframe_url = find_fortresstech_iframe_url(html)
        if iframe_url:
            iframe_url = _prefer_availability_host(iframe_url)
        else:
            iframe_url = fortresstech_availability_url(html)
        if not iframe_url:
            result.tier_used = _TIER_NO_IFRAME
            result.confidence = 0.0
            result.errors.append(
                "FORTRESSTECH_NO_IFRAME: no "
                "(availability|embed|portal).fortresstech.io/{orgId}/{propertyId} "
                "reference found in HTML"
            )
            return result

        try:
            _status, iframe_html = await _fetch(iframe_url)
        except Exception as exc:
            result.tier_used = _TIER_FETCH_ERROR
            result.errors.append(
                f"fortresstech-fetch-error: {type(exc).__name__}: {str(exc)[:120]}"
            )
            return result

        if not iframe_html or "self.__next_f" not in iframe_html:
            result.tier_used = _TIER_NO_SSR_CHUNK
            result.errors.append(
                "FORTRESSTECH_NO_SSR_CHUNK: iframe response missing "
                "self.__next_f hydration blocks"
            )
            return result

        raw_units = parse_fortresstech_iframe_html(iframe_html, iframe_url)
        if not raw_units:
            result.tier_used = _TIER_EMPTY
            result.winning_url = iframe_url
            result.errors.append(
                "FORTRESSTECH_EMPTY: iframe parsed but produced 0 unit rows "
                "(empty units array or missing queryKey)"
            )
            return result

        from ma_poc.extraction.post_process import post_process

        pp = post_process(raw_units, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted == 0:
            result.tier_used = _TIER_VALIDITY_REJECTED
            result.errors.append(
                f"FORTRESSTECH_VALIDITY_REJECTED: {len(raw_units)} rows "
                "failed unit_validity"
            )
            return result

        result.units = pp.admitted
        result.plan_summaries = pp.plan_summaries
        result.winning_url = iframe_url
        result.confidence = min(0.92, 0.7 + 0.04 * pp.n_admitted)
        result.tier_used = _TIER
        result.api_responses.append(
            {
                "url": iframe_url,
                "status": 200,
                "body": "<fortresstech-iframe-ssr>",
                "via": "fortresstech_probe",
            }
        )
        return result
