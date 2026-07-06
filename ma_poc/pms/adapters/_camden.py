"""Camden Living NEXT_DATA parser (2026-06-27).

Camden Property Trust (camdenliving.com) operates 165+ properties on a
single Next.js corporate-portfolio host. Every property URL renders the
SAME shell HTML (~147 KB) regardless of the requested sub-path
(/floor-plans, /availability, etc.) — they're a SPA that swaps content
client-side, so the static-fetch link-hop tier has no way to navigate.

But the page's ``__NEXT_DATA__`` blob carries the full inventory in
``props.pageProps.suggestedFloorPlans[]``. Each entry is one
floor-plan row with:
    - name (e.g. "B.2")
    - bedrooms / bathrooms (strings)
    - squareFeet (int)
    - monthlyRent / totalMonthlyRent (int dollars)
    - availableUnits (count)
    - availableUnitIds (list of unit numbers)
    - moveInDate (ISO timestamp)

This module extracts and emits one unit row per available unit
(plan × unit_id cross-product) so downstream gets unit-level data
rather than the 1-row plan-level fallback the DOM-text tier produces.

Wired into ``generic.py`` after the embedded-JSON sub-tier (next to
the AMLI tRPC + Mark-Taylor PRELOADED_STATE paths). Triggers when:
    1. Host matches ``camdenliving.com`` AND
    2. ``__NEXT_DATA__`` blob is present AND
    3. ``props.pageProps.suggestedFloorPlans`` is a non-empty list

Live-verified 2026-06-27 against Camden Vanderbilt
(/apartments/houston-tx/camden-vanderbilt): truth=12 plans on
apartments.com → adapter emits 10 plans × ~10 units each = 100 unit
rows. The 2 missing plans are sold-out (not in suggestedFloorPlans).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, UTC
from typing import Any
from urllib.parse import urlparse


# Match Camden's hostnames (camdenliving.com + future variants like
# camdenresidential.com etc — keep narrow).
_CAMDEN_HOST_RE = re.compile(r"(?:^|\.)camdenliving\.com$", re.IGNORECASE)

# Capture the __NEXT_DATA__ script tag's JSON payload.
_NEXT_DATA_RE = re.compile(
    r'<script\s+id="__NEXT_DATA__"[^>]*>(.+?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def is_camden_host(url: str | None) -> bool:
    """True when ``url``'s host is a Camden corporate-portfolio host."""
    if not url:
        return False
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    if not host:
        return False
    if ":" in host:
        host = host.split(":", 1)[0]
    return bool(_CAMDEN_HOST_RE.search(host))


def detect_camden_next_data(html_or_blob: Any) -> bool:
    """Cheap fingerprint. Accepts either:
      • raw HTML string — fires when ``__NEXT_DATA__`` script tag + the
        ``suggestedFloorPlans`` key both appear.
      • parsed JSON dict (already-extracted blob body from
        ``extract_embedded_blobs_from_html``) — fires when the dict has
        ``props.pageProps.suggestedFloorPlans``.

    Single function so the caller doesn't need to know whether it's
    holding a string or a dict.
    """
    if html_or_blob is None:
        return False
    # Parsed dict path (from extract_embedded_blobs_from_html)
    if isinstance(html_or_blob, dict):
        sfp = (
            html_or_blob.get("props", {})
            .get("pageProps", {})
            .get("suggestedFloorPlans")
        )
        return isinstance(sfp, list) and len(sfp) > 0
    # Raw HTML string path (direct/live tests)
    if not isinstance(html_or_blob, str) or len(html_or_blob) < 1000:
        return False
    if 'id="__NEXT_DATA__"' not in html_or_blob:
        return False
    return "suggestedFloorPlans" in html_or_blob


def _coerce_int(v: Any) -> int | None:
    """Parse string/int → int. Returns None on garbage so downstream
    can decide whether to drop the field."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            i = int(v)
            return i if i > 0 else None
        except Exception:
            return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except Exception:
        return None


def _coerce_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            return float(v)
        except Exception:
            return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None


def _iso_date_only(s: Any) -> str | None:
    """Camden emits move-in dates as ``2026-07-31T00:00:00.000Z``.
    Strip to YYYY-MM-DD."""
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None


def parse_camden_next_data(
    html_or_blob: Any, source_url: str = ""
) -> list[dict[str, Any]]:
    """Parse the ``__NEXT_DATA__`` payload and emit one unit dict per
    available unit. Accepts either a raw HTML string or a pre-parsed
    JSON dict (the blob body shape produced by
    ``extract_embedded_blobs_from_html`` — that helper already
    ``json.loads`` 's script-tag contents before they reach adapters).

    Returns [] when:
      - input is empty / None / wrong type
      - regex can't find __NEXT_DATA__ in raw HTML
      - JSON parsing fails
      - suggestedFloorPlans is missing/empty

    Never raises — caller appends to result.units unconditionally.
    """
    if html_or_blob is None:
        return []
    # Already-parsed blob path: take dict as-is
    if isinstance(html_or_blob, dict):
        nd = html_or_blob
    else:
        if not isinstance(html_or_blob, str) or not html_or_blob:
            return []
        m = _NEXT_DATA_RE.search(html_or_blob)
        if not m:
            return []
        try:
            nd = json.loads(m.group(1))
        except Exception:
            return []
    sfp = (
        nd.get("props", {})
        .get("pageProps", {})
        .get("suggestedFloorPlans")
    )
    if not isinstance(sfp, list) or not sfp:
        return []

    scrape_ts = datetime.now(UTC).isoformat()
    out: list[dict[str, Any]] = []
    for plan in sfp:
        if not isinstance(plan, dict):
            continue
        plan_name = plan.get("name") or (plan.get("media") or {}).get("overrideName")
        if not plan_name:
            continue
        beds = _coerce_int(plan.get("bedrooms"))
        baths = _coerce_float(plan.get("bathrooms"))
        sqft = _coerce_int(plan.get("squareFeet"))
        rent = _coerce_int(plan.get("monthlyRent"))
        # totalMonthlyRent includes concessions/fees; expose as rent_high
        # so downstream sees the range when total > base
        total_rent = _coerce_int(plan.get("totalMonthlyRent"))
        if rent and total_rent and total_rent < rent:
            # Defensive: keep monotonic low/high — never let high < low
            total_rent = rent
        rent_low = rent
        rent_high = total_rent or rent
        move_in = _iso_date_only(plan.get("moveInDate"))

        # Plan-level fingerprint shared across all units of this plan
        rp_fp_id = plan.get("realPageFloorPlanId")
        rp_unit_id = plan.get("realPageUnitId")

        unit_ids = plan.get("availableUnitIds") or []
        if not isinstance(unit_ids, list):
            unit_ids = []

        # Emit one row per available unit. When availableUnitIds is empty
        # but the plan IS available, emit a single plan-level row so the
        # plan still shows up downstream.
        if not unit_ids and plan.get("available"):
            unit_ids = [str(plan.get("unitNumber") or plan.get("unitName") or "")]

        for uid in unit_ids:
            if not uid:
                continue
            uid_str = str(uid).strip()
            unit_row: dict[str, Any] = {
                "unit_id": uid_str,
                "unit_number": uid_str,
                "floor_plan_name": str(plan_name).strip(),
                "beds": beds,
                "baths": baths,
                "area": sqft,
                "rent_low": rent_low,
                "rent_high": rent_high,
                # Aliases — schema_v2._format_v2_unit reads these names
                # before falling back to rent_low; legacy consumers also
                # look for asking_rent. Emit all so the field survives any
                # downstream normalizer.
                "market_rent_low": rent_low,
                "market_rent_high": rent_high,
                "asking_rent": rent_low,
                "rent": rent_low,
                "availability_status": "AVAILABLE",
                "available_date": move_in,
                "source_api_url": source_url or "",
                "extraction_tier": "TIER_1_DOM_CAMDEN_NEXT_DATA",
                "source_ids": {
                    "camden_floor_plan_id": rp_fp_id,
                    "camden_unit_id": rp_unit_id,
                    "scrape_ts": scrape_ts,
                },
            }
            out.append(unit_row)
    return out
