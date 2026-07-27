"""Reinhold Residential adapter (``rr-unit-block`` SSR table).

Research log
------------
Live-probed 2026-05-25 against canary 1ef1060 regression #15
(chocolateworks-living.com/availability/). Reinhold Residential is a
~8-property NE operator running each site on WordPress + Divi child
theme with a custom availability shortcode. Yardi RentCafe is the
back-end (apply links go to ``<sub>.securecafeapplicant.com/onlineleasing
/.../rentaloptions/<apartmentId>/<floorplanId>``), but unit data is
fully server-rendered into the page HTML by ``units-api.php`` in the
Divi-child theme — no XHR, no Playwright, no widget hop.

Probed sister sites (rr-unit-block confirmed):
  - chocolateworks-living.com  (8 units, 1BR + 1BR Loft)
  - shadyside-living.com       (18 units, 2 rr-unit-blocks)
  - sharplesworks-living.com   (5 units, 2 rr-unit-blocks)
  - trinityrow-living.com      (4 units, 2 rr-unit-blocks)
  - waterfront2-living.com     (1 unit, 1 rr-unit-block)

DOM shape (verified, chocolateworks):

  <div class="et_pb_module et_pb_toggle rr-pricing-toggle ...">
    <h5 class="et_pb_toggle_title"
        aria-label="1 Bedroom 420 - 1250 SQ. FT. | FROM $1975.00">...</h5>
    <div class="et_pb_toggle_content">
      <div class="rr-unit-block">
        <div class="rr-unit-header">
          <div class="rr-price-column">Type</div>
          <div class="rr-price-column">Floor Plan</div>
          <div class="rr-price-column">Rent</div>
          <div class="rr-price-column">Available</div>
        </div>
        <div class="rr-unit">
          <div class="rr-price-column">1 Bed 1 Bath</div>
          <div class="rr-price-column">
            <a href=".../units-api.php?property=N&ApartmentId=M">449-0203</a>
          </div>
          <div class="rr-price-column">$1975.00</div>
          <div class="rr-price-column">06/06/2026</div>
          <div class="rr-price-column">
            <a href="...securecafeapplicant.com/.../rentaloptions/M/F">APPLY</a>
          </div>
        </div>
        ...
      </div>
    </div>
  </div>

A toggle whose aria-label ends in ``| Call for Pricing`` has no
rr-unit-block inside (just a phone-CTA paragraph) — these emit no
unit rows.

The rr-pricing-toggle/rr-unit-block class pair is unique to Reinhold's
theme (rr- = Reinhold Residential); no other operator on the cataloged
4,800-property worklist uses it. Routing key is the HTML marker, not
host (sites are vanity ``*-living.com`` domains).
"""

from __future__ import annotations

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

REINHOLD_TIER = "TIER_1_DOM_REINHOLD"

# aria-label of the toggle header: "1 Bedroom 420 - 1250 SQ. FT. | FROM $1975.00"
# or "2 Bedrooms 920 - 1430 SQ. FT. | FROM Call for Pricing"
_BED_LABEL_RE = re.compile(
    r"^\s*(\d+)\s+Bedroom|^\s*Studio\b", re.IGNORECASE
)
# Type column: "1 Bed 1 Bath", "Studio 1 Bath", "2 Bed 2 Bath - Den"
_TYPE_BEDS_RE = re.compile(r"(\d+)\s*Bed\b", re.IGNORECASE)
_TYPE_BATHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Bath", re.IGNORECASE)
_TYPE_STUDIO_RE = re.compile(r"\bstudio\b", re.IGNORECASE)
# Date: 06/06/2026 (M/D/YYYY)
_DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$")
_AVAIL_NOW_RE = re.compile(r"^\s*Available\s+Now\s*$", re.IGNORECASE)
# Apply URL → securecafeapplicant.com/.../rentaloptions/<apartmentId>/<floorplanId>
_APPLY_IDS_RE = re.compile(
    r"securecafeapplicant\.com/[^\"]*?/rentaloptions/(\d+)/(\d+)",
    re.IGNORECASE,
)
# units-api.php?property=NNN&ApartmentId=MMM  (Divi-child theme proxy URL)
_UNITS_API_PROPERTY_RE = re.compile(r"property=(\d+)", re.IGNORECASE)
_UNITS_API_APARTMENT_RE = re.compile(r"ApartmentId=(\d+)", re.IGNORECASE)


def _norm_date(raw: str) -> str:
    """``06/06/2026`` → ``2026-06-06``; ``Available Now`` → today.

    Empty string on any other input (unparseable / not a date).
    """
    if _AVAIL_NOW_RE.match(raw or ""):
        return datetime.now(tz=UTC).date().isoformat()
    m = _DATE_RE.match(raw or "")
    if not m:
        return ""
    mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if yy < 100:
        yy += 2000
    try:
        return datetime(yy, mm, dd).date().isoformat()
    except ValueError:
        return ""


def _toggle_beds(aria_label: str, type_text: str) -> int | None:
    """Derive bedroom count. Prefer the per-unit Type cell ("1 Bed 1 Bath");
    fall back to the toggle aria-label ("1 Bedroom ..."). Studio → 0."""
    if _TYPE_STUDIO_RE.search(type_text or "") or _TYPE_STUDIO_RE.search(aria_label or ""):
        return 0
    m = _TYPE_BEDS_RE.search(type_text or "")
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    m = _BED_LABEL_RE.match(aria_label or "")
    if m and m.group(1):
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def _toggle_baths(type_text: str) -> str:
    m = _TYPE_BATHS_RE.search(type_text or "")
    if not m:
        return ""
    val = m.group(1)
    # Drop trailing ``.0`` so ``1.0`` → ``1`` (matches other adapters).
    try:
        f = float(val)
        if f == int(f):
            return str(int(f))
        return val
    except ValueError:
        return val


def parse_reinhold_units(html: str, url: str) -> list[dict[str, Any]]:
    """Walk every ``rr-pricing-toggle`` → ``rr-unit-block`` → ``rr-unit`` and
    emit one unit dict per row. Returns ``[]`` if no toggles or no units.

    Column order is fixed by Reinhold's theme: ``Type | Floor Plan |
    Rent | Available | Apply``. The first four are read from
    ``rr-price-column`` cells; the apply ``<a href>`` (when present) is
    mined for ``securecafeapplicant.com`` source_ids.
    """
    if not html or "rr-unit-block" not in html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    out: list[dict[str, Any]] = []

    toggles = soup.find_all(
        lambda tag: tag.name == "div"
        and "rr-pricing-toggle" in (tag.get("class") or [])
    )
    # Fallback: if toggles aren't tagged, fall back to any rr-unit-block on the page.
    blocks: list[Any] = []
    if toggles:
        for tog in toggles:
            for blk in tog.find_all("div", class_="rr-unit-block"):
                # Carry the toggle's aria-label down so per-unit parsing can
                # use it as a bed-label fallback.
                header = tog.find(
                    lambda t: t.name == "h5"
                    and "et_pb_toggle_title" in (t.get("class") or [])
                )
                aria = header.get("aria-label", "") if header else ""
                blocks.append((aria, blk))
    else:
        for blk in soup.find_all("div", class_="rr-unit-block"):
            blocks.append(("", blk))

    for aria_label, block in blocks:
        for unit_div in block.find_all("div", class_="rr-unit"):
            cells = unit_div.find_all("div", class_="rr-price-column")
            if len(cells) < 4:
                continue  # malformed row; ignore
            type_text = cells[0].get_text(" ", strip=True)
            unit_cell = cells[1]
            unit_number = unit_cell.get_text(" ", strip=True)
            rent_text = cells[2].get_text(" ", strip=True)
            avail_text = cells[3].get_text(" ", strip=True)

            if not unit_number:
                continue

            beds = _toggle_beds(aria_label, type_text)
            baths = _toggle_baths(type_text)
            rent = money_to_int(rent_text)

            avail_date = _norm_date(avail_text)
            # Reinhold lists only available units in rr-unit rows, so a row
            # with a parseable future date OR a present rent is AVAILABLE.
            avail_status = "AVAILABLE"

            # Apply link mines apartmentId / floorPlanId for source_ids.
            #
            # 2026-07-27: emitted as ``securecafe_apartment_id``, not the bare
            # ``apartment_id`` it used to be. The bare name collides with the v2
            # OUTPUT's PROPERTY-level ``apartment_id`` field (every record in
            # properties.json is keyed by it), so once a source-id key can
            # promote a row to a unit-level anchor, a future adapter reusing
            # that name for a PROPERTY id would silently mint one apartment out
            # of a whole building. Registered per-unit in core/source_ids.py
            # (fixtures 7/7 and 18/18 distinct); the bare name is registered
            # there as permanently non-admissible.
            source_ids: dict[str, Any] = {}
            for a in unit_div.find_all("a"):
                href = a.get("href") or ""
                m = _APPLY_IDS_RE.search(href)
                if m:
                    source_ids["securecafe_apartment_id"] = m.group(1)
                    source_ids["floor_plan_id"] = m.group(2)
                    break
            # Unit-number cell also links to units-api.php — extract the
            # property+apartment ids from there (richer when present).
            if "securecafe_apartment_id" not in source_ids:
                inner_a = unit_cell.find("a")
                if inner_a:
                    href = inner_a.get("href") or ""
                    pm = _UNITS_API_PROPERTY_RE.search(href)
                    am = _UNITS_API_APARTMENT_RE.search(href)
                    if pm:
                        source_ids["property_id"] = pm.group(1)
                    if am:
                        source_ids["securecafe_apartment_id"] = am.group(1)

            # Reinhold doesn't ship a per-unit sqft cell — the sqft range
            # lives only in the toggle aria-label ("420 - 1250 SQ. FT.")
            # which is a plan-level range, not a per-unit value. Leave
            # sqft empty rather than write a misleading range to a per-unit
            # field; the plan-level fact can be reconstructed downstream
            # from the floor_plan_name + range if needed.

            # Floor plan name is the type variant (e.g. "1 Bed 1 Bath -
            # Loft") — that distinguishes the Loft from the standard 1BR
            # within the same toggle and is the only plan-level naming
            # Reinhold exposes.
            floor_plan_name = type_text

            out.append(
                make_unit_dict(
                    floor_plan_name=floor_plan_name,
                    bed_label=bed_label_from(beds, type_text),
                    bedrooms=str(beds) if beds is not None else "",
                    bathrooms=baths,
                    sqft="",
                    unit_number=unit_number,
                    rent_range=format_rent_range(rent, rent),
                    rent_low=rent,
                    rent_high=rent,
                    availability_status=avail_status,
                    availability_date=avail_date,
                    source_api_url=url,
                    extraction_tier=REINHOLD_TIER,
                    source_ids=source_ids or None,
                )
            )

    return out


async def _fetch_availability_html(page: Any, ctx: AdapterContext) -> tuple[str, str]:
    """Return ``(html, url)`` for the page that holds rr-unit-block.

    Prefer ``{base}/availability/`` (where Reinhold renders the table).
    Fall back to ``ctx.fetch_result.body`` and finally to a live page
    fetch if Playwright is attached.
    """
    base = (getattr(ctx, "base_url", "") or "").rstrip("/")
    target = f"{base}/availability/" if base else ""

    # 1. Try the canonical /availability/ URL first.
    if target:
        try:
            from ma_poc.pms.adapters._probe import probe_get

            r = probe_get(target, timeout=20)
            if getattr(r, "status_code", 0) == 200 and r.text and "rr-unit-block" in r.text:
                return str(r.text), target
        except Exception:
            pass

    # 2. Body the framework already fetched for us.
    fr = getattr(ctx, "fetch_result", None)
    body = getattr(fr, "body", None)
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:
            body = None
    if isinstance(body, str) and "rr-unit-block" in body:
        return body, getattr(ctx, "base_url", "") or ""

    # 3. Live page (only if Playwright is attached).
    if page is not None:
        try:
            evaluate = getattr(page, "content", None)
            if callable(evaluate):
                h = await page.content()
                if isinstance(h, str) and "rr-unit-block" in h:
                    return h, getattr(ctx, "base_url", "") or target or ""
        except Exception:
            pass

    return "", target or (getattr(ctx, "base_url", "") or "")


class ReinholdAdapter:
    """Reinhold Residential adapter (Divi child theme rr-unit-block table)."""

    pms_name: str = "reinhold"
    _fingerprints: list[str] = [
        "rr-unit-block",
        "rr-pricing-toggle",
        "reinholdresidential.com",
    ]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=REINHOLD_TIER)

        html, url = await _fetch_availability_html(page, ctx)
        units = parse_reinhold_units(html, url) if html else []

        if not units:
            result.confidence = 0.0
            result.errors.append(
                "REINHOLD_NO_UNITS: no rr-unit-block rows parsed from /availability/"
            )
            return result

        from ma_poc.extraction.post_process import post_process

        pp = post_process(units, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted > 0:
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.winning_url = url or None
            # Reinhold tables are deterministic + small (≤30 units typical);
            # cap confidence at 0.92 (one tier below RentCafe API).
            result.confidence = min(0.92, 0.7 + 0.04 * pp.n_admitted)
            return result

        result.confidence = 0.0
        result.errors.append(
            f"REINHOLD_VALIDITY_REJECTED: {len(units)} rows failed unit_validity"
        )
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
