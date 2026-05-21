"""
Cortland adapter.

Research log
------------
Single operator, single host (cortland.com). Server-rendered marketing
site. The per-property ``/available-apartments/`` page embeds a clean
``preload = {"floorplans": {...}}`` JSON blob (HTML-entity-encoded in
the markup). Discovered via user HAR + probe (2026-05-18).

Structure (verified, cortland-brier-creek, 34 priced units):
  preload.floorplans[<fpId>] = {
    id, title, bedroom, bathroom, square_feet, floors[], min_rent,
    max_rent, apartments[<unitId>], availability[<date>],
    availprice: { "<unitId>": {apartment_number, date(epoch ms), price} }
  }

``availprice`` is the genuine unit-level map: real apartment_number +
per-unit price + availability epoch. No auth, no API call — the data is
in the page HTML. Strictly richer than the prior partial TIER_3_DOM
path (which missed ~40% of units).

Recipe (deterministic, public):
  1. fetch {property_base}/available-apartments/
  2. brace-match the floorplans object out of ``preload = {...}``
  3. flatten floorplans[].availprice -> unit dicts -> post_process
"""

from __future__ import annotations

import html as _html
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bs4 import BeautifulSoup

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

OLL_TIER = "TIER_1_API_CORTLAND"


def _epoch_to_date(v: Any) -> str:
    """Cortland ``date`` is epoch milliseconds -> ``YYYY-MM-DD``."""
    try:
        ms = int(v)
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def _extract_floorplans(html: str) -> dict[str, Any]:
    """Brace-match the ``floorplans`` object out of the embedded preload.

    The blob is HTML-entity-encoded in the markup, so unescape first.
    """
    d = _html.unescape(html or "")
    i = d.find('"floorplans"')
    if i < 0:
        return {}
    j = d.find("{", i)
    if j < 0:
        return {}
    depth = 0
    end = -1
    for k in range(j, len(d)):
        c = d[k]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = k + 1
                break
    if end < 0:
        return {}
    try:
        obj = json.loads(d[j:end])
    except (json.JSONDecodeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


# ── Card-DOM parser (2026-05-21) ────────────────────────────────────────────
#
# Cortland migrated from the ``preload = {floorplans: ...}`` JSON envelope to
# server-side-rendered ``<div class="apartments__card">`` cards sometime
# between 2026-05-18 (when the original adapter was written) and 2026-05-21
# (when we re-verified). The new card text shape is identical across all
# 196 Cortland properties — confirmed live on cortland-macarthur (64 cards):
#
#   Apt #441007                                  ← unit number link text
#   Volterra                                     ← floor plan name
#   Apt #441007                                  ← repeated as h3/heading
#   Starting at $1,481                           ← rent
#   Floor 1                                      ← floor
#   1 Bed | 1 Bath | 740 sq. ft.                 ← beds | baths | sqft
#   Available starting 7/15  (or "Available Now")  ← availability
#
# The data is in static HTML — curl_cffi chrome120 sees the same cards a
# real browser does. No Playwright render needed.

_APT_NUMBER_RE = re.compile(r"Apt\s*#\s*([A-Z0-9][A-Z0-9\-]{1,12})", re.IGNORECASE)
_STARTING_AT_RE = re.compile(r"Starting\s+at\s+\$\s*([1-9]\d{0,3}(?:,\d{3})*)", re.IGNORECASE)
_FLOOR_RE = re.compile(r"Floor\s+(\d{1,3})", re.IGNORECASE)
_BBS_RE = re.compile(
    r"(\d+(?:\.\d+)?|studio)\s*Bed[s]?\s*[|•/· \s]+\s*"
    r"(\d+(?:\.\d+)?)\s*Bath[s]?\s*[|•/· \s]+\s*"
    r"(\d{2,5}(?:,\d{3})*)\s*sq\.?\s*ft\.?",
    re.IGNORECASE,
)
_AVAIL_DATE_RE = re.compile(
    r"Available\s+(starting\s+)?(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?",
    re.IGNORECASE,
)
_AVAIL_NOW_RE = re.compile(r"Available\s+Now", re.IGNORECASE)


def parse_cortland_cards(html: str, url: str) -> list[dict[str, str]]:
    """Parse Cortland's new ``.apartments__card`` SSR HTML into unit dicts.

    Each card produces one unit. Floor-plan name, unit number, rent, floor,
    beds, baths, sqft, and availability date are all in the card's
    visible text — no XHR, no Playwright. Returns empty list if no cards
    are found (caller falls back to legacy ``parse_cortland_units``).

    Date normalization: dates ship as ``M/D`` (no year on the marketing
    page) — we interpret them as the next-occurrence of that month/day
    relative to today. ``Available Now`` → today's date.
    """
    if not html or "apartments__card" not in html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    today = datetime.now(tz=UTC).date()
    out: list[dict[str, str]] = []

    for card in soup.find_all("div", class_="apartments__card"):
        text = card.get_text("\n", strip=True)
        if not text:
            continue
        # Unit number — first "Apt #X" mention in the card text
        unum_m = _APT_NUMBER_RE.search(text)
        if not unum_m:
            continue  # not a unit card
        unit_number = unum_m.group(1)

        # Floor plan name — the first non-Apt#-prefixed line of the
        # ``apartments__card-columns`` span. Walk text lines, skip the
        # leading "Apt #X" lines, the first remaining line is the plan.
        floor_plan_name = ""
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("Apt") or _APT_NUMBER_RE.fullmatch(line):
                continue
            if line.startswith("Starting at") or line.startswith("Floor "):
                continue
            if "Bed" in line and "Bath" in line:
                continue
            if "Available" in line:
                continue
            # First non-noise line: that's the plan name
            floor_plan_name = line
            break

        # Rent
        rent_m = _STARTING_AT_RE.search(text)
        rent = None
        if rent_m:
            try:
                rent = int(rent_m.group(1).replace(",", ""))
            except (ValueError, TypeError):
                rent = None

        # Floor
        floor_m = _FLOOR_RE.search(text)
        floor = floor_m.group(1) if floor_m else ""

        # Beds / baths / sqft from the "N Bed | N Bath | NNN sq. ft." line.
        # The ``&nbsp;`` non-breaking-space variant is normalised by
        # ``get_text``; the regex's whitespace class catches both.
        bbs_m = _BBS_RE.search(text)
        beds = ""
        baths = ""
        sqft = ""
        if bbs_m:
            bed_raw = bbs_m.group(1)
            beds = "0" if bed_raw.lower() == "studio" else bed_raw
            baths = bbs_m.group(2)
            sqft = bbs_m.group(3).replace(",", "")

        # Availability — "Available Now" OR "Available starting M/D"
        avail_status = ""
        avail_date = ""
        if _AVAIL_NOW_RE.search(text):
            avail_status = "AVAILABLE"
            avail_date = today.isoformat()
        else:
            adm = _AVAIL_DATE_RE.search(text)
            if adm:
                mm = int(adm.group(2))
                dd = int(adm.group(3))
                yy = adm.group(4)
                year = today.year
                if yy:
                    year = int(yy) if len(yy) == 4 else 2000 + int(yy)
                else:
                    # No year — assume next-occurrence of MM/DD. If the
                    # MM/DD already passed this year, roll to next year.
                    try:
                        candidate = datetime(year, mm, dd).date()
                        if candidate < today:
                            year += 1
                    except ValueError:
                        pass
                try:
                    avail_date = datetime(year, mm, dd).date().isoformat()
                    avail_status = "AVAILABLE"
                except ValueError:
                    avail_date = ""

        beds_int = None
        if beds:
            try:
                beds_int = int(float(beds))
            except (ValueError, TypeError):
                beds_int = None

        out.append(
            make_unit_dict(
                floor_plan_name=floor_plan_name,
                bed_label=bed_label_from(beds_int, floor_plan_name),
                bedrooms=beds,
                bathrooms=baths,
                sqft=sqft,
                unit_number=unit_number,
                floor=floor,
                rent_range=format_rent_range(rent, rent) if rent else "",
                rent_low=rent,
                rent_high=rent,
                availability_status=avail_status,
                availability_date=avail_date,
                concession="",
                source_api_url=url,
                extraction_tier=OLL_TIER,
            )
        )

    return out


def parse_cortland_units(floorplans: dict[str, Any], url: str) -> list[dict[str, str]]:
    """Flatten ``floorplans[].availprice`` into standard unit dicts."""
    units: list[dict[str, str]] = []
    for fp in floorplans.values():
        if not isinstance(fp, dict):
            continue
        fp_name = str(fp.get("title") or "").strip()
        beds_raw = fp.get("bedroom")
        baths_raw = fp.get("bathroom")
        try:
            beds = int(float(beds_raw)) if beds_raw not in (None, "") else None
        except (TypeError, ValueError):
            beds = None
        try:
            baths = float(baths_raw) if baths_raw not in (None, "") else None
        except (TypeError, ValueError):
            baths = None
        sqft_raw = fp.get("square_feet")
        sqft = str(sqft_raw) if sqft_raw not in (None, "", 0) else ""

        # 2026-05-19 capture-first: Cortland preload carries a specials/
        # concession flag/text at floorplan level (probe saw
        # `specials_flag` + concession phrases). Alias-tolerant; raw
        # passthrough; empty when no active special (correct, not a bug).
        _conc = ""
        for _ck in ("specials", "special", "concession", "concessions",
                    "specials_description", "specials_text", "promotion",
                    "offer", "incentive"):
            _cv = fp.get(_ck)
            if isinstance(_cv, str) and _cv.strip():
                _conc = _cv.strip()
                break

        availprice = fp.get("availprice")
        if not isinstance(availprice, dict):
            continue
        for info in availprice.values():
            if not isinstance(info, dict):
                continue
            unit_no = str(info.get("apartment_number") or "").strip()
            rent = money_to_int(str(info.get("price") or "")) or None
            avail_date = _epoch_to_date(info.get("date"))
            units.append(
                make_unit_dict(
                    floor_plan_name=fp_name,
                    bed_label=bed_label_from(beds, fp_name),
                    bedrooms=str(beds) if beds is not None else "",
                    bathrooms=(
                        str(int(baths)) if baths is not None and baths == int(baths)
                        else (str(baths) if baths is not None else "")
                    ),
                    sqft=sqft,
                    unit_number=unit_no,
                    rent_range=format_rent_range(rent, rent),
                    rent_low=rent,
                    rent_high=rent,
                    availability_status="AVAILABLE",
                    availability_date=avail_date,
                    concession=_conc,
                    source_api_url=url,
                    extraction_tier=OLL_TIER,
                )
            )
    return units


async def _fetch_available_html(page: Page | None, ctx: AdapterContext) -> tuple[str, str]:
    """Return (html, url) for the page that carries the preload blob.

    Prefer ``{base}/available-apartments/`` (richest — has availprice).
    Fall back to the raw fetch body / current page if it already has it.
    """
    base = (getattr(ctx, "base_url", "") or "").rstrip("/")
    target = f"{base}/available-apartments/" if base else ""
    if target:
        try:
            from ma_poc.pms.adapters._probe import probe_get

            r = probe_get(target, timeout=20)
            if getattr(r, "status_code", 0) == 200 and r.text and '"floorplans"' in r.text:
                return str(r.text), target
        except Exception:
            pass
    fr = getattr(ctx, "fetch_result", None)
    body = getattr(fr, "body", None)
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:
            body = None
    if isinstance(body, str) and '"floorplans"' in body:
        return body, getattr(ctx, "base_url", "") or ""
    if page is not None:
        try:
            h = await page.content()
            if h and '"floorplans"' in h:
                return h, getattr(ctx, "base_url", "") or ""
        except Exception:
            pass
    return "", target or (getattr(ctx, "base_url", "") or "")


class CortlandAdapter:
    """Cortland PMS adapter (embedded preload JSON)."""

    pms_name: str = "cortland"
    _fingerprints: list[str] = ["cortland.com", "available-apartments"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=OLL_TIER)

        html, url = await _fetch_available_html(page, ctx)

        # Path 1 (legacy, pre-2026-05-21): ``preload = {floorplans: ...}`` JSON
        # blob. Some Cortland properties may still serve this if they're on
        # a different deploy tier, so we try it first. Empty result → fall
        # through.
        floorplans = _extract_floorplans(html)
        all_units = parse_cortland_units(floorplans, url) if floorplans else []

        # Path 2 (current, 2026-05-21+): server-side-rendered
        # ``<div class="apartments__card">`` cards. Cortland migrated the
        # /available-apartments/ page to this shape — the legacy preload
        # JSON is gone. 64 cards observed on cortland-macarthur live.
        if not all_units:
            all_units = parse_cortland_cards(html, url)

        if all_units:
            from ma_poc.extraction.post_process import post_process

            _pp_parsed = len(all_units)
            _pp = post_process(all_units, property_id=getattr(ctx, "property_id", None))
            if _pp.n_admitted > 0:
                result.units = _pp.admitted
                result.plan_summaries = _pp.plan_summaries
                result.winning_url = url or None
                result.confidence = min(0.90, 0.7 + 0.05 * _pp.n_admitted)
            else:
                result.confidence = 0.0
                result.errors.append(
                    f"CORTLAND_VALIDITY_REJECTED: {_pp_parsed} parsed rows "
                    f"failed unit_validity"
                )
        else:
            result.confidence = 0.0
            result.errors.append(
                "No Cortland unit data (preload floorplans/availprice not found)"
            )

        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
