"""apts247 / Vergence Multifamily direct-JSON adapter.

apts247.info is Yardi's small-property tenant CMS (separate from
*.securecafe.com). Every vanity domain that uses it loads its floor-plan
list from a public REST endpoint on the SAME host as the marketing site:

    GET https://{domain}/api/v1/floorplans/?api_key=<HEX40>
    GET https://{domain}/api/v1/community_info/?api_key=<HEX40>
    GET https://{domain}/api/v1/community_extra_info/?api_key=<HEX40>

The ``api_key`` is published in plain JavaScript inside the page:

    <script>
      ...
      api_key = "aae294c5e900e74edc21e0ce97aa1e332d2b449f";
      ...
    </script>

Detection markers (any one is sufficient):
  • ``static2.apts247.info`` substring in the HTML
  • ``apts247_api`` substring in the HTML
  • An ``api_key = "<HEX40>"`` assignment ALONE is NOT sufficient (other
    operators use that pattern); we always combine the key extraction
    with an apts247 marker.

API shape per floor plan (validated 2026-05-23 on foxrundothan.com):

    {
      "id": 32985,
      "name": "Peanut-R",
      "slug": "peanut-r",
      "bed": 1,
      "bath": 1.0,
      "sq_ft": 693,
      "rent": "$954.50",     # property-level starting rent (string)
      "deposit": "$400 Up To One Month's Rent.",
      "display_bed": "1 Bedroom",
      "units": [
        {
          "id": 991630,
          "number": "172",
          "rent": "$915",       # PER-UNIT rent (string)
          "sq_ft": 693,
          "available_date": "2025-09-26",
          "building": "",
          "floor": "",
          "deposit": "$400",
          ...
        }, ...
      ],
      "wait_units": [...]
    }

Validated against Fox Run (Dothan, AL): 3 plans, 9 units, rent + sqft +
availability date on every unit.

Phase 6.x positioning: feeds the generic adapter as a sub-tier above
the LLM rungs. Activated when the apts247 marker is present in the
fetched HTML.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from ma_poc.pms.adapters._parsing import make_unit_dict

# ── Detection ───────────────────────────────────────────────────────────────

_APTS247_HOST_MARKERS = (
    "static2.apts247.info",
    "static.apts247.info",
    "media.apts247.info",
    "apts247_api",
)

_API_KEY_RE = re.compile(r"""api_key\s*=\s*["']([a-f0-9]{40})["']""")


def detect_apts247(html: str) -> bool:
    """True when the HTML carries an apts247 widget marker.

    Cheap substring scan. The four markers cover the static-asset CDN
    (used by every apts247 site for JS/CSS/components) plus the
    ``apartments247_api.min.js`` bundle name. Catches both the
    Polymer-era widget shape (foxrundothan) and the newer compiled
    bundle.
    """
    if not html:
        return False
    return any(m in html for m in _APTS247_HOST_MARKERS)


def extract_api_key(html: str) -> str | None:
    """Pull the apts247 community ``api_key`` (40-char hex) out of the
    page's inline JavaScript. Returns ``None`` when no match — the
    caller treats that as "can't drill" and falls through.
    """
    if not html:
        return None
    m = _API_KEY_RE.search(html)
    return m.group(1) if m else None


# ── Floor-plans API URL helpers ─────────────────────────────────────────────


def _origin_of(url: str) -> str | None:
    """Return ``https://host`` for ``url`` or ``None`` on parse failure."""
    if not url:
        return None
    try:
        p = urlparse(url)
    except Exception:
        return None
    if not p.scheme or not p.netloc:
        return None
    return f"{p.scheme}://{p.netloc}"


def build_floorplans_url(page_url: str, api_key: str) -> str | None:
    """Build the ``/api/v1/floorplans/`` URL for an apts247 vanity host.

    Returns ``None`` if ``page_url`` doesn't parse to a usable origin.
    The URL stays on the SAME host as the marketing site — apts247
    proxies the call through the vanity domain's nginx so cookies /
    referer remain consistent.
    """
    origin = _origin_of(page_url)
    if not origin or not api_key:
        return None
    return f"{origin}/api/v1/floorplans/?api_key={api_key}"


# ── Parser ──────────────────────────────────────────────────────────────────


_MONEY_RE = re.compile(r"[^\d.]+")


def _money_to_int(s: Any) -> int | None:
    """Coerce ``$1,069.50`` / ``"$915"`` / ``1069`` → ``1069``.

    Drops the cents portion (apts247 uses ``.50`` for some properties;
    integer rent is sufficient for our pricing logic). Returns ``None``
    for missing / unparseable values.
    """
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s) if s > 0 else None
    if not isinstance(s, str):
        return None
    cleaned = _MONEY_RE.sub("", s.strip())
    if not cleaned:
        return None
    try:
        return int(float(cleaned))
    except (ValueError, TypeError):
        return None


def _int_or_none(v: Any) -> int | None:
    """Coerce to int. Returns None for None / non-numeric / negatives.

    Accepts ``0`` (studio bedroom count). Callers that need a strict-
    positive filter (e.g. sqft of 0 is meaningless) should test the
    return value themselves.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, float):
        return int(v) if v >= 0 else None
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return None
    return None


def _str_or_empty(v: Any) -> str:
    return "" if v is None else str(v)


def parse_apts247_floorplans(
    body: Any, source_url: str = ""
) -> list[dict[str, Any]]:
    """Walk an apts247 ``/api/v1/floorplans/`` response into unit dicts.

    Returns ``[]`` for any input that doesn't match the apts247 envelope
    (``{meta, objects: [...]}``). Per-unit records inherit floor-plan
    metadata: bed / bath / floor_plan_name / floor_plan_slug. Per-unit
    rent + sqft + available_date come from the ``units[]`` array.

    When a floor plan has ``units == []`` (no current vacancy), we still
    emit a synthetic plan-level row carrying the plan's starting rent +
    sqft + bed + bath so the property still clears the success bar.
    Such rows are flagged ``data_quality_flag="PLAN_LEVEL_NO_VACANT_UNIT"``
    so downstream layers know per-unit data isn't available right now.
    """
    if not isinstance(body, dict):
        return []
    objs = body.get("objects")
    if not isinstance(objs, list):
        return []

    rows: list[dict[str, Any]] = []
    for fp in objs:
        if not isinstance(fp, dict):
            continue

        fp_id = _str_or_empty(fp.get("id"))
        fp_name = _str_or_empty(fp.get("name"))
        fp_slug = _str_or_empty(fp.get("slug"))
        fp_bed = _int_or_none(fp.get("bed"))
        fp_bath_raw = fp.get("bath")
        fp_bath_str = ""
        if isinstance(fp_bath_raw, (int, float)) and fp_bath_raw > 0:
            # Preserve half-baths: 1.5 → "1.5", 1.0 → "1"
            fp_bath_str = (
                f"{fp_bath_raw:g}" if fp_bath_raw != int(fp_bath_raw)
                else str(int(fp_bath_raw))
            )
        fp_sqft = _int_or_none(fp.get("sq_ft"))
        fp_starting_rent = _money_to_int(fp.get("rent"))
        bed_label = ""
        if fp_bed == 0:
            bed_label = "Studio"
        elif fp_bed is not None:
            bed_label = f"{fp_bed} Bed"

        units = fp.get("units")
        if isinstance(units, list) and units:
            for u in units:
                if not isinstance(u, dict):
                    continue
                # Per-unit rent is canonical when present. We do NOT
                # fall back to the plan-level starting rent here —
                # operator silence on a per-unit field means we don't
                # know that unit's rent, not that it matches the
                # cheapest one. Plan-level fallback only applies when
                # the entire units[] array is empty (handled below).
                unit_rent = _money_to_int(u.get("rent"))
                unit_sqft = _int_or_none(u.get("sq_ft"))
                # Per-unit sqft is rarely missing for apts247, but if
                # it is we accept the plan's sqft (the same shape sqft
                # for every unit of a plan is the apts247 convention).
                if not unit_sqft and fp_sqft:
                    unit_sqft = fp_sqft
                if not unit_rent or not unit_sqft or unit_sqft <= 0:
                    # Skip records where the operator hasn't filled in
                    # either rent or sqft — they can't satisfy the
                    # success bar and would only dilute downstream
                    # confidence. The plan-level fallback below covers
                    # the floor plan as a whole.
                    continue
                rows.append(
                    make_unit_dict(
                        floor_plan_name=fp_name,
                        bed_label=bed_label,
                        bedrooms=str(fp_bed) if fp_bed is not None else "",
                        bathrooms=fp_bath_str,
                        sqft=str(unit_sqft),
                        unit_number=_str_or_empty(u.get("number")),
                        floor=_str_or_empty(u.get("floor")),
                        building=_str_or_empty(u.get("building")),
                        rent_low=unit_rent,
                        rent_range=f"${unit_rent:,}",
                        deposit=_str_or_empty(u.get("deposit")),
                        availability_status="AVAILABLE",
                        availability_date=_str_or_empty(
                            u.get("available_date")
                        ),
                        source_api_url=source_url,
                        extraction_tier="TIER_1_API_APTS247_FLOORPLANS",
                        source_ids={
                            "apts247_floor_plan_id": fp_id,
                            "apts247_unit_id": _str_or_empty(u.get("id")),
                            "apts247_slug": fp_slug,
                        },
                    )
                )
        else:
            # No current units for this plan — emit a plan-level row
            # with the property's starting rent so the success bar
            # still clears. Honest about the gap via data_quality_flag.
            if (
                fp_starting_rent
                and fp_sqft
                and fp_sqft > 0
                and fp_bed is not None
            ):
                rows.append(
                    make_unit_dict(
                        floor_plan_name=fp_name,
                        bed_label=bed_label,
                        bedrooms=str(fp_bed),
                        bathrooms=fp_bath_str,
                        sqft=str(fp_sqft),
                        rent_low=fp_starting_rent,
                        rent_range=f"${fp_starting_rent:,}+",
                        availability_status="UNAVAILABLE",
                        source_api_url=source_url,
                        extraction_tier="TIER_1_API_APTS247_FLOORPLANS",
                        source_ids={
                            "apts247_floor_plan_id": fp_id,
                            "apts247_slug": fp_slug,
                        },
                        data_gaps=["unit_number", "availability_date"],
                        data_quality_flag="PLAN_LEVEL_NO_VACANT_UNIT",
                    )
                )
    return rows
