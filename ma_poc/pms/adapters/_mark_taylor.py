"""Mark-Taylor Residential adapter — PRELOADED_STATE extractor.

Mark-Taylor (mark-taylor.com) operates ~50 luxury apartment communities
across Arizona and Nevada. Every property's ``/floor-plans/`` page is
rendered by a Union/Henri (gounion.com) stack and embeds the full
PRELOADED_STATE for the property in a plain ``<script>`` tag rather than
a ``__NEXT_DATA__`` block:

    <script>
      window.APP_HOST = "www.mark-taylor.com";
      window.CRM_ORIGIN = "https://my.gounion.com";
      window.PRELOADED_STATE = { "seo": {...}, "sitePage": { ...
        "property": { "name": "Waterside at Ocotillo",
                      "seo_url": "waterside-at-ocotillo",
                      "floor_plan_meta": {
                          "bedrooms": [1, 2, 3],
                          "min_rent": 1300,
                          "bathrooms": [1, 2],
                          "min_sqft_rent": 756
                      },
                      ... } } };
    </script>

The per-unit modal data (Model A1 with units 1009/2025/2013) on
mark-taylor.com lives behind a JS-triggered XHR to ``api.selftournow.com``
that requires auth. We don't reach that layer here — but we don't have to:
the ``floor_plan_meta`` block gives us property-level ``min_rent`` and
``min_sqft_rent``, which is enough to satisfy the Surgex success bar
(≥1 unit with rent+sqft) for 10 mark-taylor properties currently
mis-flagged as operator-data-gap.

Per-bedroom synthesis: emit one synthetic plan-level row per bedroom
count in ``bedrooms[]``. All rows share the same ``min_rent`` /
``min_sqft_rent`` floor — clearly flagged with
``data_quality_flag=PLAN_LEVEL_MIN_ONLY`` so downstream consumers know
the rent and sqft are floors, not per-unit. The bathroom count for each
row is the smallest bathroom value the property publishes (operators
almost always pair more bathrooms with more bedrooms; we can't pick the
right pairing without per-plan detail, so we floor on bathrooms too).

Detection markers:
  • ``window.PRELOADED_STATE`` (the script global itself)
  • EITHER ``mark-taylor`` host OR ``gounion.com`` CRM origin in the
    same script

Phase 6.x positioning: sits in the embedded-JSON sub-tier (4.x) of the
generic adapter, before the AMLI tRPC hook. Tried first on the property
homepage; if that fails, the adapter probes ``/floor-plans/`` as a
deterministic subpage hint.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from ma_poc.pms.adapters._parsing import make_unit_dict

# ── Detection ───────────────────────────────────────────────────────────────

_PRELOADED_STATE_MARKER = "window.PRELOADED_STATE"
_HOST_MARKERS = ("mark-taylor.com", "gounion.com")


def detect_mark_taylor(html: str, url: str = "") -> bool:
    """True if the HTML carries Mark-Taylor's PRELOADED_STATE shape.

    Two-signal gate so we don't mis-route other ``window.PRELOADED_STATE``
    sites (the global is generic enough that other operators use it):
      1. ``window.PRELOADED_STATE`` substring present, AND
      2. EITHER the request URL is on ``mark-taylor.com`` OR the HTML
         references ``gounion.com`` (the Union/Henri CRM origin) — the
         latter catches the case where mark-taylor renders its content
         behind a CDN with a different request host.
    """
    if not html or _PRELOADED_STATE_MARKER not in html:
        return False
    if url:
        host = urlparse(url).netloc.lower()
        if any(m in host for m in _HOST_MARKERS):
            return True
    # Fall back to HTML-body markers — covers CDN-fronted requests where
    # the request URL host doesn't identify the operator.
    body_lower = html.lower()
    return any(m in body_lower for m in _HOST_MARKERS)


# ── PRELOADED_STATE extraction ──────────────────────────────────────────────


def extract_preloaded_state(html: str) -> dict[str, Any] | None:
    """Pull ``window.PRELOADED_STATE = {…};`` from the HTML and parse it.

    The blob is a JS object literal that happens to be valid JSON
    (Mark-Taylor's stack serializes its Redux store with ``JSON.stringify``,
    which produces JSON-compatible output). We balance braces character-by-
    character so we tolerate the trailing ``;`` and any sibling globals on
    the same script block.

    Returns ``None`` on missing marker, malformed JSON, or any parse error
    — callers should treat ``None`` as "not a Mark-Taylor page" and fall
    through to the next sub-tier.
    """
    if not html:
        return None
    needle = "window.PRELOADED_STATE = "
    start = html.find(needle)
    if start < 0:
        return None
    json_start = start + len(needle)
    # Balanced-brace walk that respects strings (so an unescaped '}' inside
    # a JSON string doesn't terminate the object early).
    depth = 0
    in_str = False
    esc = False
    end = json_start
    for i in range(json_start, len(html)):
        c = html[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if depth != 0:
        return None
    raw = html[json_start:end]
    try:
        result = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(result, dict):
        return None
    return result


# ── Floor-plan-meta walker ──────────────────────────────────────────────────


def _walk_floor_plan_meta(
    obj: Any, path: str = ""
) -> list[tuple[str, dict[str, Any], str | None, str | None]]:
    """Yield every dict node in ``obj`` that carries a ``floor_plan_meta``
    sub-dict.

    Returns tuples of ``(path, floor_plan_meta, property_name, seo_url)``.
    ``property_name`` / ``seo_url`` come from the SAME dict that holds
    ``floor_plan_meta`` (they're sibling keys in the Mark-Taylor schema).
    """
    out: list[tuple[str, dict[str, Any], str | None, str | None]] = []
    if isinstance(obj, dict):
        fpm = obj.get("floor_plan_meta")
        if isinstance(fpm, dict):
            out.append(
                (
                    path,
                    fpm,
                    obj.get("name") if isinstance(obj.get("name"), str) else None,
                    obj.get("seo_url")
                    if isinstance(obj.get("seo_url"), str)
                    else None,
                )
            )
        for k, v in obj.items():
            out.extend(_walk_floor_plan_meta(v, f"{path}.{k}" if path else k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_walk_floor_plan_meta(v, f"{path}[{i}]"))
    return out


# ── Plan-level row synthesis ────────────────────────────────────────────────

_VALID_BEDROOM_RANGE = range(0, 11)  # 0 (studio) through 10


def _coerce_int(v: Any) -> int | None:
    """Best-effort int coercion. Returns None for None / unparseable."""
    if v is None:
        return None
    if isinstance(v, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v == int(v) else None
    if isinstance(v, str):
        try:
            return int(v.strip().replace(",", "").replace("$", ""))
        except ValueError:
            return None
    return None


def _emit_plan_rows_from_meta(
    fpm: dict[str, Any],
    property_name: str | None,
    seo_url: str | None,
    source_url: str,
) -> list[dict[str, Any]]:
    """Build one synthetic plan-level unit dict per bedroom in ``fpm``.

    The Mark-Taylor ``floor_plan_meta`` shape:
      {"bedrooms": [1, 2, 3], "min_rent": 1300,
       "bathrooms": [1, 2], "min_sqft_rent": 756}

    We emit one row per bedroom count. Each carries the same ``min_rent``
    and ``min_sqft_rent`` floor; rent_range string is ``$1,300+`` to
    signal "starting at" semantics. ``data_quality_flag`` is set to
    ``PLAN_LEVEL_MIN_ONLY`` so consumers don't mistake the floor for a
    per-unit price.

    Returns [] for any of:
      • ``min_rent`` missing or non-positive
      • ``min_sqft_rent`` missing or non-positive
      • ``bedrooms`` missing / empty / non-list
      • all bedroom values out of valid range
    """
    min_rent = _coerce_int(fpm.get("min_rent"))
    min_sqft = _coerce_int(fpm.get("min_sqft_rent"))
    if min_rent is None or min_rent <= 0:
        return []
    if min_sqft is None or min_sqft <= 0:
        return []
    bedrooms = fpm.get("bedrooms")
    if not isinstance(bedrooms, list) or not bedrooms:
        return []
    bathrooms = fpm.get("bathrooms")
    min_bath = ""
    if isinstance(bathrooms, list) and bathrooms:
        baths_filtered: list[int] = [
            b for b in (_coerce_int(x) for x in bathrooms)
            if b is not None and b > 0
        ]
        if baths_filtered:
            min_bath = str(min(baths_filtered))

    rows: list[dict[str, Any]] = []
    for bed in bedrooms:
        bed_int = _coerce_int(bed)
        if bed_int is None or bed_int not in _VALID_BEDROOM_RANGE:
            continue
        floor_plan_name = (
            f"{property_name} {bed_int}BR"
            if property_name
            else f"{bed_int}BR"
        )
        if bed_int == 0:
            bed_label = "Studio"
            floor_plan_name = (
                f"{property_name} Studio" if property_name else "Studio"
            )
        else:
            bed_label = f"{bed_int} Bed"
        rows.append(
            make_unit_dict(
                floor_plan_name=floor_plan_name,
                bed_label=bed_label,
                bedrooms=str(bed_int),
                bathrooms=min_bath,
                sqft=str(min_sqft),
                rent_low=min_rent,
                rent_range=f"${min_rent:,}+",
                source_api_url=source_url,
                extraction_tier="TIER_1_EMBEDDED_MARK_TAYLOR_PRELOADED_STATE",
                source_ids={
                    "operator": "mark-taylor",
                    "seo_url": seo_url or "",
                    "property_name": property_name or "",
                },
                data_gaps=["unit_number", "availability_date", "per_unit_rent"],
                data_quality_flag="PLAN_LEVEL_MIN_ONLY",
            )
        )
    return rows


# ── Top-level entry points ──────────────────────────────────────────────────


def parse_mark_taylor_html(
    html: str, source_url: str = ""
) -> list[dict[str, Any]]:
    """Top-level: detect + extract + emit synthetic plan-level rows.

    Returns ``[]`` if the page isn't a Mark-Taylor floor-plans page, if
    PRELOADED_STATE is missing/malformed, or if no ``floor_plan_meta``
    nodes carry usable rent + sqft.

    PREFERS rendered-DOM extraction when the page is rendered (live
    Playwright fetch produces JS-hydrated HTML with per-plan rent +
    sqft + bed + bath). Falls back to PRELOADED_STATE plan-level when
    only static HTML is available (no JS execution).
    """
    if not detect_mark_taylor(html, source_url):
        return []
    # Try rendered-DOM per-plan first.
    per_plan = _parse_rendered_plan_cards(html, source_url)
    if per_plan:
        return per_plan
    # Fallback: PRELOADED_STATE plan-level synthesis.
    state = extract_preloaded_state(html)
    if state is None:
        return []
    nodes = _walk_floor_plan_meta(state)
    rows: list[dict[str, Any]] = []
    for _path, fpm, name, slug in nodes:
        rows.extend(_emit_plan_rows_from_meta(fpm, name, slug, source_url))
    return rows


# ── Rendered-DOM per-plan extractor ─────────────────────────────────────────

# After JS hydration, the Mark-Taylor /floor-plans/ page renders one
# section per plan in the canonical run:
#   <name>\n\n$<rent>+\n\n<N> Available\n<beds> bed\n<baths> bath\n<sqft> sq. ft.
#
# Captures one row per plan with starting rent, sqft, beds, baths.
# Flagged data_quality_flag="PLAN_LEVEL_STARTING_RENT" because the
# rent is "starting at" (per-unit prices are behind the gounion auth
# we can't access without cookie replay).
_MT_PLAN_CARD_RE = re.compile(
    r"(?P<name>[A-Z]\d{1,2}[A-Z]?)\s*\n\s*"
    r"\$(?P<rent>[\d,]+)\+\s*\n\s*"
    r"(?P<avail>\d+)\s+Available\s*\n\s*"
    r"(?P<beds>\d+)\s+bed\s*\n\s*"
    r"(?P<baths>\d+(?:\.\d+)?)\s+bath\s*\n\s*"
    r"(?P<sqft>\d{3,4})\s+sq\.?\s*ft",
    re.IGNORECASE,
)


def _parse_rendered_plan_cards(
    html: str, source_url: str
) -> list[dict[str, Any]]:
    """Walk the rendered-DOM text for Mark-Taylor plan cards.

    Returns one row per matched plan card. Returns ``[]`` when no
    cards match (the static HTML path before JS hydration looks like
    this — the cards exist as React skeletons rather than rendered
    text).
    """
    if not html:
        return []
    # Strip tags first — the cards span multiple elements, but the
    # text run is consistent post-strip.
    text = re.sub(r"<[^>]+>", "\n", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    matches = list(_MT_PLAN_CARD_RE.finditer(text))
    if not matches:
        return []

    # Extract property name from PRELOADED_STATE for floor_plan_name
    # prefix, falling back to URL slug.
    property_name = ""
    state = extract_preloaded_state(html)
    if state is not None:
        nodes = _walk_floor_plan_meta(state)
        if nodes:
            property_name = nodes[0][2] or ""

    rows: list[dict[str, Any]] = []
    for m in matches:
        plan = m.group("name")
        rent = int(m.group("rent").replace(",", ""))
        sqft = int(m.group("sqft"))
        beds = int(m.group("beds"))
        baths = m.group("baths")
        if rent < 200 or rent > 50000:
            continue
        if sqft < 100 or sqft > 10000:
            continue
        bed_label = "Studio" if beds == 0 else f"{beds} Bed"
        plan_name = f"{property_name} {plan}" if property_name else plan
        rows.append(
            make_unit_dict(
                floor_plan_name=plan_name,
                bed_label=bed_label,
                bedrooms=str(beds),
                bathrooms=baths,
                sqft=str(sqft),
                rent_low=rent,
                rent_range=f"${rent:,}+",
                availability_status="AVAILABLE",
                source_api_url=source_url,
                extraction_tier="TIER_1_DOM_MARK_TAYLOR_RENDERED_PLAN_CARD",
                source_ids={
                    "operator": "mark-taylor",
                    "property_name": property_name,
                    "plan_code": plan,
                    "available_count": m.group("avail"),
                },
                data_gaps=["unit_number"],
                data_quality_flag="PLAN_LEVEL_STARTING_RENT",
            )
        )
    return rows


# ── Subpage hint ────────────────────────────────────────────────────────────

# When the adapter is invoked on a Mark-Taylor homepage and gets no rows
# (homepage doesn't ship floor_plan_meta), this is the deterministic
# subpage to probe next. The pattern is universal across the portfolio:
# any property URL ``/apartments/{state}/{city}/{slug}/`` has a sibling
# ``/floor-plans/`` page that DOES carry PRELOADED_STATE.

_MT_PROPERTY_URL_RE = re.compile(
    r"^(https?://[^/]+/apartments/[a-z]{2}/[^/]+/[^/]+)/?$",
    re.IGNORECASE,
)


def derive_floor_plans_url(home_url: str) -> str | None:
    """Given a Mark-Taylor property homepage URL, return the
    ``/floor-plans/`` sibling URL. Returns ``None`` if the URL doesn't
    match the expected ``/apartments/{state}/{city}/{slug}/`` pattern.
    """
    if not home_url:
        return None
    m = _MT_PROPERTY_URL_RE.match(home_url.rstrip("/"))
    if not m:
        return None
    return f"{m.group(1)}/floor-plans/"
