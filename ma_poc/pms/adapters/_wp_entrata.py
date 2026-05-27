"""WordPress Entrata-theme REST API adapter (2026-05-24).

HAR-driven addition for WordPress sites that use the Entrata-branded
theme (entrata-theme / Entrata's WP plugin). The theme exposes a
``wp-json/theme/entrata/v1/floor-plans`` REST endpoint that returns
both floorplan-level and per-unit pricing in a single 400KB+ JSON.

Live-verified (HAR 2026-05-24):
  * olivboulder.com — 38 floorplans, 48 units with rent+sqft

Detection strategy:

The endpoint is at a fixed path relative to the site's WordPress
origin. Detect via:
  1. ``wp-json/theme/entrata`` reference anywhere in the page body
     (script src, link rel, fetch call, etc.)
  2. ``wp-content/themes/entrata`` reference (theme directory hint)
  3. Generic.py routes here when entrata fingerprints fail to capture

Probe path:
  ``{origin}/wp-json/theme/entrata/v1/floor-plans?v=0.955&unitAvailability=true``

Response shape:
  body = {
    "categories": [...],
    "floorplans": {
      "<fp_id>": {
        "id": int, "name": str, "beds": int, "baths": float,
        "sq_ft_min": int, "sq_ft_range": str, "min_rate": int,
        "max_rate": int, "units_available": int, "tabs": ..., etc.
      },
      ...
    },
    "units": {
      "<unit_id>": {
        "id": int, "number": str, "rent": int, "availableOn": str,
        "sqft": int, "floor": int, "floorplanID": int,
        "unitTypeName": str, "available": bool
      },
      ...
    }
  }

Output: per-unit dicts (unit-level extraction — richer than the
floorplan-only Prospect Portal SSR fallbacks).
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    make_unit_dict,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

_WP_ENTRATA_MARKERS = (
    "wp-json/theme/entrata",
    "wp-content/themes/entrata",
    "wp-content/themes/entrata-theme",
)


def _has_wp_entrata_marker(body: str) -> bool:
    if not body:
        return False
    return any(m in body for m in _WP_ENTRATA_MARKERS)


def _parse_iso_or_us_date(s: str) -> str:
    """Pass through if already ISO; convert MM/DD/YYYY → YYYY-MM-DD."""
    if not s:
        return ""
    s = s.strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        mm, dd, yyyy = m.groups()
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
    return s


def parse_wp_entrata_floor_plans(
    body: dict[str, Any], url: str
) -> list[dict[str, str]]:
    """Parse a ``wp-json/theme/entrata/v1/floor-plans`` response.

    Joins ``units`` dict to ``floorplans`` dict by ``floorplanID`` so
    each unit row carries the floorplan's name + beds + baths in
    addition to its own number + rent + sqft + availability.

    Returns ``[]`` when body shape doesn't match (so detection can
    fall through to the next rung). Skips units missing both rent
    and sqft (they wouldn't clear the validity gate anyway).
    """
    if not isinstance(body, dict):
        return []
    fps = body.get("floorplans") or {}
    units = body.get("units") or {}
    if not isinstance(fps, dict) or not isinstance(units, dict):
        return []
    if not units:
        return []

    out: list[dict[str, str]] = []
    for _uid, u in units.items():
        if not isinstance(u, dict):
            continue
        fp_id_raw = u.get("floorplanID")
        fp = fps.get(str(fp_id_raw)) if fp_id_raw is not None else None
        if not isinstance(fp, dict):
            fp = {}

        # Units-level fields
        unit_number = str(u.get("number") or u.get("id") or "")
        rent_raw = u.get("rent")
        sqft_raw = u.get("sqft")
        floor_raw = u.get("floor")
        available_on = _parse_iso_or_us_date(str(u.get("availableOn") or ""))

        # Floorplan-level fields (fallback to unit when fp missing)
        name = str(fp.get("name") or u.get("unitTypeName") or "")
        beds = fp.get("beds")
        baths = fp.get("baths")

        # Numeric coercion
        try:
            rent_i = int(rent_raw) if rent_raw not in (None, "") else None
        except (TypeError, ValueError):
            rent_i = None
        try:
            sqft_i = int(sqft_raw) if sqft_raw not in (None, "") else None
        except (TypeError, ValueError):
            sqft_i = None

        if not rent_i and not sqft_i:
            continue  # validity gate would reject anyway

        beds_i: int | None = None
        if isinstance(beds, (int, float)):
            beds_i = int(beds)
        elif isinstance(beds, str) and beds.isdigit():
            beds_i = int(beds)

        baths_str = ""
        if isinstance(baths, (int, float)):
            baths_str = (
                str(int(baths)) if float(baths).is_integer() else str(baths)
            )
        elif isinstance(baths, str):
            baths_str = baths

        is_available = bool(u.get("available", True))

        out.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds_i, name),
                bedrooms=str(beds_i) if beds_i is not None else "",
                bathrooms=baths_str,
                sqft=str(sqft_i) if sqft_i else "",
                unit_number=unit_number,
                floor=str(floor_raw) if floor_raw is not None else "",
                rent_low=rent_i,
                rent_high=rent_i,
                availability_status=(
                    "AVAILABLE" if is_available else "UNAVAILABLE"
                ),
                availability_date=available_on,
                source_api_url=url,
                extraction_tier="TIER_1_API_WP_ENTRATA",
            )
        )
    return out


async def probe_wp_entrata(ctx: AdapterContext) -> list[dict[str, str]]:
    """Discover the wp-json/theme/entrata endpoint origin from
    ctx.fetch_result.body markers, hit it via curl_cffi, parse.

    Returns ``[]`` when no marker found or probe failed. Never raises.
    """
    fr = getattr(ctx, "fetch_result", None)
    raw = getattr(fr, "body", None) if fr is not None else None
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", errors="replace")
        except Exception:
            return []
    if not isinstance(raw, str) or not raw:
        return []
    if not _has_wp_entrata_marker(raw):
        return []

    # Origin discovery — prefer final_url (post-redirect)
    final_url = ""
    if fr is not None:
        final_url = str(getattr(fr, "final_url", "") or "")
    final_url = final_url or getattr(ctx, "base_url", "") or ""
    try:
        p = urlparse(final_url)
        if not (p.scheme and p.netloc):
            return []
        origin = f"{p.scheme}://{p.netloc}"
    except Exception:
        return []

    probe_url = (
        f"{origin}/wp-json/theme/entrata/v1/floor-plans"
        f"?v=0.955&unitAvailability=true"
    )

    try:
        from ma_poc.pms.adapters._probe import probe_get

        r = probe_get(probe_url, timeout=20)
    except Exception as exc:
        log.debug("wp_entrata probe err: %s", exc)
        return []
    if r.status_code != 200 or not r.text:
        return []
    try:
        body = json.loads(r.text)
    except Exception:
        return []
    return parse_wp_entrata_floor_plans(body, probe_url)


class WpEntrataAdapter:
    """WordPress Entrata-theme REST API adapter."""

    pms_name: str = "wp_entrata"
    _fingerprints: list[str] = [
        "wp-json/theme/entrata",
        "wp-content/themes/entrata",
    ]

    async def extract(
        self, page: Page, ctx: AdapterContext
    ) -> AdapterResult:
        result = AdapterResult(tier_used="TIER_1_API_WP_ENTRATA")
        try:
            units = await probe_wp_entrata(ctx)
        except Exception as exc:  # noqa: BLE001
            units = []
            result.errors.append(
                f"wp_entrata-probe-error: {type(exc).__name__}: {str(exc)[:80]}"
            )

        if not units:
            result.confidence = 0.0
            result.errors.append(
                "wp_entrata: no wp-json/theme/entrata endpoint with parseable units"
            )
            return result

        from ma_poc.extraction.post_process import post_process

        pp = post_process(
            units, property_id=getattr(ctx, "property_id", None)
        )
        if pp.n_admitted > 0:
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.confidence = min(0.95, 0.75 + 0.02 * pp.n_admitted)
            result.api_responses.append({
                "url": (units[0].get("source_api_url", "") if units else ""),
                "status": 200,
                "body": "<wp-entrata-floor-plans>",
                "via": "wp_entrata_probe",
            })
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
