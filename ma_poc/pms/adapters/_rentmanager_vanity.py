"""RentManager-vanity SSR HTML extractor (2026-05-24).

HAR-driven adapter for marketing sites that use the RentManager
``twa.rentmanager.com`` family. The marketing pages render a
``.suite-floorplans`` container with one ``.suite-group`` per
floorplan, each carrying ``.suite-group-rate``, ``.suite-group-bath``,
``.suite-group-sqft`` etc. for plan-level pricing — plus an inner
``.suite-group-suites`` with per-unit ``.suite`` rows for unit-level
data when published.

Live-verified (2026-05-24 on wilmingtonpointe.com — 30 suite-groups
with rate ranges like ``From: $790 - $860``).

Detection markers (any sufficient):
  * ``.suite-floorplans`` container class
  * ``.suite-group-rate`` rate display class
  * Link to ``*.twa.rentmanager.com`` in body

Output: real available apartments from inner ``.suite`` rows, plus one
plan-level summary per ``.suite-group`` with rent range, bed/bath/sqft and
availability. Skips groups with rent=$0 (default empty placeholders).
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

_RM_MARKERS = (
    "suite-floorplans",
    "suite-group-rate",
    "twa.rentmanager.com",
)

_RM_NUM_RE = re.compile(r"([\d,]+)")
_RM_MONEY_RE = re.compile(r"\$\s*([\d,]+)(?:\.\d{2})?")
_RM_BED_RE = re.compile(r"(\d+)", re.IGNORECASE)
_RM_BATH_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _has_rm_marker(body: str) -> bool:
    if not body:
        return False
    return any(m in body for m in _RM_MARKERS)


def _txt(el: Any) -> str:
    return el.get_text(" ", strip=True) if el else ""


def parse_rentmanager_vanity_html(
    html: str, url: str
) -> list[dict[str, str]]:
    """Parse RentManager-vanity SSR HTML into unit + plan-level dicts.

    Returns ``[]`` when no ``.suite-group`` containers are present, so
    the caller can fall through cleanly when detection misfires.

    Each ``.suite-group`` becomes one row:

      * ``floor_plan_name``: ``.suite-group-type`` h3 text
      * ``bedrooms``: ``data-bedrooms`` attr OR derived from name
      * ``bathrooms``: ``.suite-group-bath .value``
      * ``sqft``: ``.suite-group-sqft .value`` (often blank)
      * ``rent_low``/``high``: parsed from ``.suite-group-rate .value``
        format ``"From: $790 - $860"`` or ``"$790"``
      * ``available_units``: count of inner ``.suite`` children that are
        actually available (not every priced unit in the property roster)
      * ``availability_status``: ``UNAVAILABLE`` when value is "N/A"

    Skips groups with rate "$0" (placeholder empty groups RentManager
    emits for bedroom buckets with no inventory).
    """
    if not html:
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    groups = soup.select(".suite-group")
    if not groups:
        return []

    units: list[dict[str, str]] = []
    seen_names: set[str] = set()
    seen_suite_ids: set[str] = set()
    for grp in groups:
        # Floorplan name from h3
        name = _txt(grp.select_one(".suite-group-type, h3"))
        if not name:
            continue

        # data-bedrooms attr (most reliable)
        beds_attr = grp.get("data-bedrooms")
        beds: int | None = None
        if beds_attr is not None:
            try:
                beds = int(beds_attr)
            except (TypeError, ValueError):
                beds = None

        # Baths from .suite-group-bath .value
        baths_raw = _txt(grp.select_one(".suite-group-bath .value, .suite-group-bath"))
        baths = ""
        if baths_raw:
            bm = _RM_BATH_RE.search(baths_raw)
            if bm:
                baths = bm.group(1)

        # Sqft from .suite-group-sqft .value (often a range like "570 - 875")
        sqft_raw = _txt(grp.select_one(".suite-group-sqft .value, .suite-group-sqft"))
        sqft = ""
        if sqft_raw:
            sqft_nums = _RM_NUM_RE.findall(sqft_raw)
            if sqft_nums:
                sqft = sqft_nums[0].replace(",", "")

        # Rate from .suite-group-rate .value
        rate_raw = _txt(grp.select_one(".suite-group-rate .value, .suite-group-rate"))
        rate_money = _RM_MONEY_RE.findall(rate_raw)
        rent_lo = (
            money_to_int(rate_money[0]) if rate_money else None
        )
        rent_hi = (
            money_to_int(rate_money[-1]) if rate_money else None
        )
        # Skip placeholder $0 groups
        if rent_lo == 0 and rent_hi == 0:
            continue

        # Availability text
        avail_raw = _txt(
            grp.select_one(".suite-group-availability .value, .suite-group-availability")
        )
        status = "AVAILABLE"
        if avail_raw and (
            "N/A" in avail_raw or "Waitlist" in avail_raw or "Unavailable" in avail_raw
        ):
            status = "UNAVAILABLE"

        # Inner rows are the actual apartment roster.  Older versions counted
        # every priced row as "available", even when its button explicitly
        # said Not Available (Wilmington Pointe: 189 priced rows, only 21
        # available).  Emit only rows with a positive per-unit rent, a numeric
        # physical unit token, a stable data-suite-id, and a non-disabled
        # availability action.  Plan dimensions are safe context inherited by
        # each apartment; identity and price remain row-local.
        suite_rows = grp.select(".suite-group-suites .suite[data-suite-id]")
        available_rows: list[dict[str, str]] = []
        for suite in suite_rows:
            suite_id = str(suite.get("data-suite-id") or "").strip()
            unit_number = _txt(suite.select_one(".suite-type.item"))
            suite_rate_raw = _txt(suite.select_one(".suite-rate.item"))
            suite_rent_values = _RM_MONEY_RE.findall(suite_rate_raw)
            suite_rent = (
                money_to_int(suite_rent_values[0])
                if suite_rent_values
                else None
            )
            suite_sqft_raw = _txt(suite.select_one(".suite-sqft.item"))
            suite_sqft_match = _RM_NUM_RE.search(suite_sqft_raw)
            suite_sqft = (
                suite_sqft_match.group(1).replace(",", "")
                if suite_sqft_match
                else ""
            )
            suite_bath_raw = _txt(suite.select_one(".suite-bath.item"))
            suite_bath_match = _RM_BATH_RE.search(suite_bath_raw)
            suite_baths = suite_bath_match.group(1) if suite_bath_match else baths
            availability_node = suite.select_one(".suite-availability.item")
            availability_text = _txt(availability_node)
            availability_lower = availability_text.casefold()
            availability_classes = {
                str(value).casefold()
                for node in ([availability_node] if availability_node else [])
                for value in (node.get("class") or [])
            }
            for child in availability_node.select("*") if availability_node else []:
                availability_classes.update(
                    str(value).casefold() for value in (child.get("class") or [])
                )
            unavailable = (
                not availability_text
                or "not available" in availability_lower
                or "unavailable" in availability_lower
                or "waitlist" in availability_lower
                or "disabled" in availability_classes
                or "not-available" in availability_classes
            )
            has_available_action = any(
                token in availability_lower
                for token in ("available", "inquire", "apply", "now")
            ) or bool(re.search(r"\d{1,2}/\d{1,2}/\d{4}", availability_text))
            try:
                suite_sqft_value = int(suite_sqft)
            except (TypeError, ValueError):
                suite_sqft_value = 0
            if (
                unavailable
                or not has_available_action
                or not suite_id.isdigit()
                or suite_id in seen_suite_ids
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .#/-]{0,30}", unit_number)
                or not any(char.isdigit() for char in unit_number)
                or not suite_rent
                or not 150 <= suite_sqft_value <= 10_000
            ):
                continue
            seen_suite_ids.add(suite_id)
            available_rows.append(
                make_unit_dict(
                    floor_plan_name=name,
                    bed_label=bed_label_from(beds, name),
                    bedrooms=str(beds) if beds is not None else "",
                    bathrooms=str(suite_baths),
                    sqft=str(suite_sqft_value),
                    unit_number=unit_number,
                    rent_low=suite_rent,
                    rent_high=suite_rent,
                    availability_status="AVAILABLE",
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_RENTMANAGER_VANITY_UNIT",
                    source_ids={"rentmanager_uid": suite_id},
                )
            )
        avail_count = str(len(available_rows)) if available_rows else ""

        # Dedupe by floor_plan_name (some sites emit duplicate group containers)
        dedupe_key = name.strip().lower()
        if dedupe_key not in seen_names and (rent_lo or rent_hi or sqft):
            seen_names.add(dedupe_key)
            units.append(
                make_unit_dict(
                    floor_plan_name=name,
                    bed_label=bed_label_from(beds, name),
                    bedrooms=str(beds) if beds is not None else "",
                    bathrooms=str(baths),
                    sqft=sqft,
                    unit_number="",
                    rent_range=format_rent_range(rent_lo, rent_hi),
                    rent_low=rent_lo,
                    rent_high=rent_hi,
                    availability_status=status,
                    available_units=avail_count,
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_RENTMANAGER_VANITY",
                )
            )
        units.extend(available_rows)
    return units


class RentManagerVanityAdapter:
    """RentManager-vanity SSR adapter — parses ``.suite-group`` rows
    from marketing HTML."""

    pms_name: str = "rentmanager_vanity"
    _fingerprints: list[str] = [
        "suite-floorplans",
        "suite-group-rate",
        "twa.rentmanager.com",
    ]

    async def extract(
        self, page: Page, ctx: AdapterContext
    ) -> AdapterResult:
        result = AdapterResult(tier_used="TIER_1_DOM_RENTMANAGER_VANITY")
        fr = getattr(ctx, "fetch_result", None)
        body = getattr(fr, "body", None) if fr is not None else None
        if isinstance(body, bytes):
            try:
                body = body.decode("utf-8", "replace")
            except Exception:
                body = ""
        if not isinstance(body, str) or not body:
            result.confidence = 0.0
            result.errors.append("rentmanager_vanity: empty body")
            return result
        if not _has_rm_marker(body):
            result.confidence = 0.0
            result.errors.append(
                "rentmanager_vanity: no marker in body"
            )
            return result

        url = ""
        if fr is not None:
            url = str(getattr(fr, "final_url", "") or "")
        url = url or getattr(ctx, "base_url", "") or ""

        units = parse_rentmanager_vanity_html(body, url)
        if not units:
            result.confidence = 0.0
            result.errors.append(
                "rentmanager_vanity: parse returned 0 units "
                "(no .suite-group with extractable rate)"
            )
            return result

        from ma_poc.extraction.post_process import post_process

        pp = post_process(
            units, property_id=getattr(ctx, "property_id", None)
        )
        if pp.n_admitted > 0:
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.winning_url = url
            result.confidence = min(0.92, 0.7 + 0.04 * pp.n_admitted)
            result.api_responses.append(
                {
                    "url": url,
                    "status": 200,
                    "body": "<rentmanager-vanity-suite-groups>",
                    "via": "rentmanager_vanity_ssr",
                }
            )
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
