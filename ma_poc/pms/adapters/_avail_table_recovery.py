"""Available-units recovery for own-site data surfaces that ship a full unit
roster the primary tiers miss (2026-07-31, #93).

A long tail of small-CMS / WordPress marketing sites embed a complete,
UNIT-LEVEL availability roster in their own page — but in a shape none of the
primary tiers normalize, so the property lands FAILED_NO_DATA or plan-level
despite publishing every apartment. This module is a **code-only** recovery arm
(no browser, no network): it reads the already-fetched body off ``ctx`` and
parses these surfaces directly, mirroring :func:`recover_rently`.

It runs from ``_universal_recovery.recover_universal_embed`` — i.e. only AFTER
the primary adapter returned nothing, so it can never override a better result.

Surface 1 — **MITS-ILS ``window.__FP_DATA__``** (this file): a MITS/ILS
``PhysicalProperty`` object serialized into a JS global (JCM Living /
handiwork-theme WordPress; e.g. Pleasant View). The ``#the-floorplans`` DOM is
JS-populated and empty in static HTML, so the roster is reachable ONLY from the
blob. Two things defeat the generic embedded-blob scanner and are handled here:
a Cloudflare ``__cf_email__`` anchor injected mid-string (its unescaped inner
quotes break ``json.loads``), and the MITS-ILS ``ILS_Unit[].Units.Unit`` shape,
which is not in the generic normalizer's vocabulary.

Further surfaces (Squarespace ``pre-wrap`` unit blocks, etc.) can be added as
sibling parsers behind :func:`recover_avail_table`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    make_unit_dict,
    money_to_int,
)

log = logging.getLogger(__name__)

# The MITS-ILS roster is assigned to this JS global. Match the LHS only; the
# object itself is walked with a brace scanner (a regex cannot balance braces).
_FP_DATA_LHS_RE = re.compile(r"window\.__FP_DATA__\s*=\s*\{")

# Cloudflare email-obfuscation injects a full <a class="__cf_email__" …>…</a>
# INSIDE a JSON string value. Its unescaped inner double-quotes corrupt the
# string boundary, so json.loads raises before we ever reach the units. The
# anchor's visible text is always the junk "[email protected]" cf placeholder,
# so replacing the whole tag with a plain token is lossless for our purposes.
_CF_EMAIL_ANCHOR_RE = re.compile(r"<a\b[^>]*__cf_email__[^>]*>.*?</a>", re.DOTALL)


def _extract_balanced_object(text: str, brace_start: int) -> str | None:
    """Return the ``{...}`` substring beginning at ``brace_start``.

    Scans forward honouring JSON string literals and backslash escapes, so an
    inner ``}`` inside a string value does not close the object early (the trap
    a non-greedy ``\\{.*?\\}`` regex falls into). ``None`` if unbalanced.
    """
    depth = 0
    in_str = False
    esc = False
    for j in range(brace_start, len(text)):
        c = text[j]
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
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start : j + 1]
    return None


def _norm_num_str(v: Any) -> str:
    """MITS-ILS numerics arrive as strings with a trailing space ("2 ")."""
    return str(v or "").strip()


def _first_ident(unit: dict[str, Any], id_type: str) -> str:
    """MITS ``Identification`` is a list (or lone dict) of typed ids."""
    ident = unit.get("Identification")
    if isinstance(ident, dict):
        ident = [ident]
    if not isinstance(ident, list):
        return ""
    for entry in ident:
        if not isinstance(entry, dict):
            continue
        attrs = entry.get("@attributes") or {}
        if str(attrs.get("IDType") or "") == id_type:
            return str(attrs.get("IDValue") or "").strip()
    return ""


def _made_ready_iso(ils_unit: dict[str, Any]) -> str:
    """``MadeReadyDate.@attributes {Year,Month,Day}`` -> ISO ``YYYY-MM-DD``."""
    mrd = ils_unit.get("MadeReadyDate")
    if not isinstance(mrd, dict):
        return ""
    attrs = mrd.get("@attributes") or {}
    try:
        y = int(str(attrs.get("Year") or "").strip())
        m = int(str(attrs.get("Month") or "").strip())
        d = int(str(attrs.get("Day") or "").strip())
    except (TypeError, ValueError):
        return ""
    if not (1 <= m <= 12 and 1 <= d <= 31 and 2000 <= y <= 2100):
        return ""
    return f"{y:04d}-{m:02d}-{d:02d}"


def parse_mits_ils_fp_data(body: str, base_url: str = "") -> list[dict[str, Any]]:
    """Parse a MITS-ILS ``window.__FP_DATA__`` blob into unit rows.

    Returns UNIT-LEVEL rows (real apartment numbers) from ``ILS_Unit[]`` — NOT
    the ``FloorPlan[]`` summaries, whose ``UnitCount``/``UnitsAvailable`` are
    inflated marketing totals (852/245/…) and would mis-size availability.
    ``[]`` when the blob is absent or unparseable. Never raises.
    """
    try:
        m = _FP_DATA_LHS_RE.search(body)
        if not m:
            return []
        raw = _extract_balanced_object(body, m.end() - 1)
        if not raw:
            return []
        raw = _CF_EMAIL_ANCHOR_RE.sub("email-hidden", raw)
        data = json.loads(raw)
        if not isinstance(data, dict):
            return []
        ils = data.get("ILS_Unit")
        if isinstance(ils, dict):
            ils = [ils]
        if not isinstance(ils, list):
            return []

        rows: list[dict[str, Any]] = []
        for entry in ils:
            if not isinstance(entry, dict):
                continue
            unit = ((entry.get("Units") or {}).get("Unit")) or {}
            if not isinstance(unit, dict):
                continue
            unit_number = (
                str(unit.get("MarketingName") or "").strip()
                or _first_ident(unit, "ILS_UnitID")
            )
            if not unit_number:
                continue  # no real apartment anchor -> not a unit row

            beds = _norm_num_str(unit.get("UnitBedrooms"))
            baths = _norm_num_str(unit.get("UnitBathrooms"))
            sqft = _norm_num_str(unit.get("MinSquareFeet"))
            # UnitRent is "2749.0000" — round to the whole dollar.
            rent_raw = _norm_num_str(unit.get("UnitRent"))
            rent = money_to_int(rent_raw.split(".")[0]) if rent_raw else None
            # NB the source key is misspelled "Floonplan" (SIC).
            fp_name = str(
                unit.get("FloonplanName") or unit.get("FloorplanName") or ""
            ).strip()
            beds_int: int | None
            try:
                beds_int = int(float(beds)) if beds else None
            except ValueError:
                beds_int = None

            # NB: no PMS-native source_ids emitted — the MITS ``ILS_UnitID`` /
            # ``FloorPlanID`` are not registered scopes in core.source_ids, and
            # registering a scope on one example is premature (probe >=3 first).
            # The real ``unit_number`` (232, 211A, …) is the join anchor.
            rows.append(
                make_unit_dict(
                    unit_number=unit_number,
                    floor_plan_name=fp_name,
                    bed_label=bed_label_from(beds_int, fp_name),
                    bedrooms=str(beds_int) if beds_int is not None else "",
                    bathrooms=baths,
                    sqft=sqft,
                    rent_low=rent,
                    rent_high=rent,
                    # Every ILS_Unit row is a genuinely-offered apartment with a
                    # rent (VacancyClass Vacant/Available now, or Occupied/On
                    # Notice pre-leasing). The made-ready date carries the WHEN.
                    availability_status="AVAILABLE",
                    availability_date=_made_ready_iso(entry),
                    source_api_url=base_url,
                    extraction_tier="TIER_1_EMBEDDED_MITS_ILS",
                )
            )
        return rows
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
        return []


# ── Surface 2: Squarespace pre-wrap unit blocks ──────────────────────────────
#
# A Squarespace marketing site (e.g. Cricket Flats) lists each available
# apartment as a ``white-space:pre-wrap`` <p> inside a ``sqs-html-content``
# block, four <br>-separated lines: ``Unit 406`` / plan prose / ``$2,850`` /
# ``Available Now``. squarespace_nopms declares SYNDICATION_ONLY and its generic
# recovery misses because the container classes carry no plan-word and beds are
# spelled out ("One Bedroom"), so <2 digit signals fire. This reads them by the
# ``^Unit \d+`` shape instead — UNIT-LEVEL (real apartment numbers).
_SQSP_UNIT_RE = re.compile(r"^\s*Unit\s+(\d+)\b", re.IGNORECASE)
_SQSP_RENT_RE = re.compile(r"\$\s*([\d,]+)")
_SQSP_AVAIL_RE = re.compile(r"Available\s+(Now|\d{1,2}/\d{1,2})", re.IGNORECASE)
_BR_SPLIT_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_WORD_TO_NUM: dict[str, int] = {
    "studio": 0, "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6,
}


def _word_count(text: str, noun: str) -> int | None:
    """"Two Baths" -> 2. ``noun`` is "bed" or "bath" (matches the plural too)."""
    m = re.search(rf"(studio|zero|one|two|three|four|five|six)\s+{noun}", text, re.I)
    if not m:
        return None
    return _WORD_TO_NUM.get(m.group(1).lower())


def parse_squarespace_unit_blocks(
    body: str, base_url: str = ""
) -> list[dict[str, Any]]:
    """Parse Squarespace ``pre-wrap`` "Unit N / plan / $rent / Available X"
    blocks into UNIT-LEVEL rows. ``[]`` when none match. Never raises.

    Deliberately strict — a paragraph must START with ``Unit <digits>`` AND
    carry a ``$rent`` — so the site's footer/contact/CTA ``pre-wrap`` blocks
    ('Ready to explore…', the address, the phone number) are excluded, and the
    parser fail-closes on any Squarespace page that is not this roster shape.
    """
    try:
        soup = BeautifulSoup(body, "lxml")
        rows: list[dict[str, Any]] = []
        for p in soup.select("div.sqs-html-content p"):
            style = str(p.get("style") or "").replace(" ", "")
            if "white-space:pre-wrap" not in style:
                continue
            segments = [
                BeautifulSoup(s, "lxml").get_text(" ", strip=True)
                for s in _BR_SPLIT_RE.split(p.decode_contents())
            ]
            segments = [s for s in segments if s]
            if not segments:
                continue
            m_unit = _SQSP_UNIT_RE.match(segments[0])
            if not m_unit:
                continue
            joined = " ".join(segments)
            m_rent = _SQSP_RENT_RE.search(joined)
            if not m_rent:
                continue  # a real unit block always quotes a price
            rent = money_to_int(m_rent.group(1))

            plan = segments[1] if len(segments) > 1 else ""
            beds = _word_count(plan, "bed")
            baths = _word_count(plan, "bath")
            m_av = _SQSP_AVAIL_RE.search(joined)
            avail_date = ""
            if m_av and m_av.group(1).lower() != "now":
                avail_date = m_av.group(1)  # "8/1" — downstream date parse adds year

            rows.append(
                make_unit_dict(
                    unit_number=m_unit.group(1),
                    floor_plan_name=plan,
                    bed_label=bed_label_from(beds, plan),
                    bedrooms=str(beds) if beds is not None else "",
                    bathrooms=str(baths) if baths is not None else "",
                    rent_low=rent,
                    rent_high=rent,
                    availability_status="AVAILABLE",
                    availability_date=avail_date,
                    source_api_url=base_url,
                    extraction_tier="TIER_1_DOM_SQUARESPACE_UNIT_BLOCK",
                )
            )
        return rows
    except Exception:  # noqa: BLE001 — recovery net must never raise
        return []


async def recover_avail_table(ctx: Any) -> list[dict[str, Any]]:
    """Code-only recovery of an own-site available-units roster.

    Reads the already-fetched body off ``ctx.fetch_result`` and tries each
    known embedded/DOM roster surface in turn. Returns ``[]`` for pages that
    match none. Never raises — it is a recovery net.
    """
    try:
        fr = getattr(ctx, "fetch_result", None)
        if fr is None:
            return []
        raw = getattr(fr, "body", None)
        if isinstance(raw, bytes):
            body = raw.decode("utf-8", "replace")
        elif isinstance(raw, str):
            body = raw
        else:
            return []
        if not body:
            return []
        base_url = str(getattr(fr, "final_url", "") or "") or str(
            getattr(ctx, "base_url", "") or ""
        )

        # Surface 1: MITS-ILS window.__FP_DATA__ (unit-level).
        units = parse_mits_ils_fp_data(body, base_url)
        if units:
            return units

        # Surface 2: Squarespace pre-wrap "Unit N / $rent" blocks (unit-level).
        units = parse_squarespace_unit_blocks(body, base_url)
        if units:
            return units

        return []
    except Exception:  # noqa: BLE001 — recovery net must never raise
        return []
