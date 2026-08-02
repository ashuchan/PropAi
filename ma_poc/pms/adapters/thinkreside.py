"""ThinkRESIDE (Resite Multi Family Marketing) adapter — SSR floorplan +
per-plan unit-table extractor.

Research log (deep-probe 2026-05-25, canary 1ef1060)
----------------------------------------------------
Vendor: **Resite Multi Family Marketing** (resite.com /
``thinkresite.com`` parent), CMS branded **ThinkRESIDE**. Asset CDN
``resite-themes.nyc3.digitaloceanspaces.com`` (DigitalOcean Spaces);
neighborhood-POI API ``api.thinkresite.dev``; contact-form proxy
``forms.thinkresite.dev/api/submit/<form-id>``; image CDN
``media.thinkresite.cloud`` / ``themes.thinkresite.cloud``.

Cohort sized 2026-05-25 against canary 1ef1060 post-phase16 v2 by
broad probe of 1,234 properties (all 934 ``n_full=0`` + 300 random):

* 3 confirmed sites — Orchard Ridge (liveatorchardridge.com),
  Indy Flats (indyflatsapts.com), Deer Run (liveatdeerrunapts.com)
* extrapolated to full canary (~4,983 properties) ≈ 12 ThinkRESIDE
* all 3 currently fall to ``TIER_MERGED_CROSS_PAGE`` / generic plan-
  text with ``n_full=0`` — operator publishes unit-level rents but
  the generic cascade misses the per-plan drill

The vendor ships TWO theme families with distinct DOM shapes — this
adapter handles both via the same fingerprint set:

**Pattern A — "bns-community2019" theme (Indy Flats, Deer Run)**
``/floorplans`` index page renders a ``<ul>`` of plan-summary cards::

  <article data-type="realpageApi">
    <ul>
      <li data-beds="0" data-baths="1" data-price="$680"
          data-rent-min="" data-sqft="467">
        <a href="/floorplans/windsor-studio">
          <h3>Windsor Studio</h3>
          <h4><strong>$680</strong><em>467 Sq. Ft.</em></h4>
          <div class="meta"><span class="beds">0 Beds</span>
                             <span class="baths">1 Baths</span></div>
          <span class="status">8</span>  <!-- N units available -->
        </a>
      </li>
      ...
    </ul>
  </article>

The per-plan ``/floorplans/{slug}`` detail page renders the unit
roster inline in a ``<table class="fp-availability-list">``::

  <table class="fp-availability-list">
    <thead><tr><th>Unit</th><th>Available Date</th>
               <th>Square Feet</th><th>Rent</th>
               <th>Apply</th></tr></thead>
    <tbody>
      <tr>
        <td>210</td>
        <td class="avail-date" data-date="Now">Now</td>
        <td class="g6m"></td>
        <td class="tp">$830.0000</td>
        <td><a class="button" href="">Apply Now</a></td>
      </tr>
      <tr>
        <td>312</td>
        <td class="avail-date" data-date="06/23/2026">06/23/2026</td>
        ...
      </tr>
    </tbody>
  </table>

Cells are: ``[unit_number, available_date, sqft, rent, apply]``.
The ``data-date`` attr is canonical (``"Now"`` or ``MM/DD/YYYY``);
rent comes as ``$830.0000`` (4 trailing decimals). ``td.g6m`` is the
sqft slot — often empty (sqft inherited from plan-level data-sqft).

**Pattern B — "ascent" theme (Orchard Ridge)**
Plan-summary cards live on the *home* page (not ``/floorplans``)::

  <div id="One Bedroom" class="floorplan-item" data-beds="1"
       data-baths="1" data-price="Call for pricing" data-sqft="648">
    <a href="/floorplans/one-bedroom">...</a>
  </div>

Detail page table shape is identical to Pattern A, but the operator
may have an empty ``<tbody>`` (no leasable units published) — those
plans emit plan-level summary rows only. Orchard Ridge in our sample
ships "Call for pricing" everywhere, no unit-level data; the adapter
still emits the 3 plan-level rows so the catalog is captured.

**Pattern C — "towncommunity" theme (Deer Run)**
Plan list on ``/floorplans`` carries no per-plan ``<li data-*>``
attrs — just static cards with subpage links. Detail pages lack
``fp-availability-list`` entirely; only plan-level rent/sqft text on
the card. Operator publishes static rent ranges ("$1225 - $1450")
but no unit-level data. Plan-level fallback handles this.

Detector wiring: HTML markers fire at 0.87 (above co-resident
chat-widget hosts like MeetElise at 0.85; below Knock at 0.90).
Routed to ThinkResideAdapter which performs the plan-card walk +
per-plan detail fetch.

NOT to confuse with: ``api.thinkresite.dev/neighborhoods/{id}`` is a
Walk-Score-style POI feed (restaurants, parks) — not unit data.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

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

log = logging.getLogger(__name__)

_TIER = "TIER_1_DOM_THINKRESIDE"

# Fingerprints — at least one must appear in the HTML body for the
# detector and the adapter's self-confirm. Hosts are stable across
# every ThinkRESIDE theme; the company's own ``Powered by Resite
# Multi Family Marketing`` footer string is the fallback when an
# operator self-hosts the asset bundles.
_FINGERPRINTS: tuple[str, ...] = (
    "thinkresite.dev",
    "themes.thinkresite.cloud",
    "media.thinkresite.cloud",
    "resite-themes.nyc3.digitaloceanspaces.com",
    "resiteimages.nyc3.cdn.digitaloceanspaces.com",
    "resite multi family marketing",
)

# ``$830.0000`` / ``$1,250.00`` → 830 / 1250 (4-decimal vendor format
# OR comma-grouped 2-decimal). money_to_int handles trailing zeros.
_RENT_CELL_RE = re.compile(r"\$[\d,]+(?:\.\d+)?", re.IGNORECASE)
# data-date="Now" or data-date="MM/DD/YYYY" or data-date="YYYY-MM-DD".
_DATE_MMDDYYYY_RE = re.compile(
    r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$"
)
_DATE_ISO_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$")
_DATE_NOW_RE = re.compile(r"^\s*now\s*$", re.IGNORECASE)


def _norm_avail_date(raw: str) -> str:
    """Preserve ThinkRESIDE availability semantics at the adapter boundary.

    ``"Now"`` is a source token, not a calendar date.  Keep it verbatim so
    the production formatter can resolve it against the run capture date and
    record ``available_now`` provenance.  Explicit ``MM/DD/YYYY`` values are
    normalized to ISO; already-ISO values pass through. Empty / unparseable
    values remain absent rather than fabricating a date.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if _DATE_NOW_RE.match(s):
        return s
    m = _DATE_MMDDYYYY_RE.match(s)
    if m:
        mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yy < 100:
            # Two-digit years: 00–69 → 20xx, 70–99 → 19xx (POSIX
            # strptime convention).
            yy = 2000 + yy if yy <= 69 else 1900 + yy
        try:
            return datetime(yy, mm, dd).date().isoformat()
        except ValueError:
            return ""
    m = _DATE_ISO_RE.match(s)
    if m:
        yy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(yy, mm, dd).date().isoformat()
        except ValueError:
            return ""
    return ""


def _strip_dollars(text: str) -> int | None:
    """Pull the first dollar-formatted number out of *text* → int.

    ThinkRESIDE rent cells are formatted ``$830.0000`` (4 decimals)
    or ``$1,250.00`` (comma + 2 decimals); ``money_to_int`` from
    ``_parsing`` handles both via its generic dollar regex. Returns
    ``None`` for ``"Call for pricing"`` / blank / no numeric.
    """
    m = _RENT_CELL_RE.search(text or "")
    if not m:
        return None
    return money_to_int(m.group(0))


def _int_str(val: str | int | None) -> str:
    """Stringify an int-like value; empty string on non-numeric input.

    ThinkRESIDE ``data-beds="0"`` is a Studio (0 bedrooms) — keep the
    literal ``"0"`` rather than coercing to empty.
    """
    if val is None:
        return ""
    s = str(val).strip()
    if not s:
        return ""
    if not re.match(r"^\d+(?:\.\d+)?$", s):
        return ""
    try:
        f = float(s)
    except (TypeError, ValueError):
        return ""
    if f == int(f):
        return str(int(f))
    return s


def parse_thinkreside_plan_index(html: str, base_url: str) -> list[dict[str, Any]]:
    """Walk the ``/floorplans`` index (Pattern A ``<li data-beds>``) or
    the home page (Pattern B ``<div class="floorplan-item">``), or the
    current towncommunity theme (Pattern C ``<li class="floorplan">``),
    and return one plan-summary dict per card.

    Output shape — what each emitted dict carries:

      * ``name``  — plan label from ``<h3>`` (Pattern A) or the
        ``id`` attr (Pattern B)
      * ``beds`` / ``baths`` / ``sqft`` — string-coerced, ``""`` when
        absent
      * ``price_raw`` — verbatim ``data-price`` (``$680`` or
        ``"Call for pricing"``)
      * ``rent_low`` — int parsed from ``price_raw`` (``None`` for
        non-numeric)
      * ``detail_url`` — absolute URL of the per-plan drill (``""``
        when card carries no link)
      * ``status_units`` — value of the ``<span class="status">``
        unit-count badge (Pattern A only), int, ``None`` if absent

    Order preserved (operator's authored display order). Cards
    without any of the four ``data-*`` dims are skipped — they're
    decorative section headers, not plan cards.
    """
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    out: list[dict[str, Any]] = []
    base = (base_url or "").rstrip("/")
    seen_links: set[str] = set()

    # Pattern A — <li data-beds=...>
    for li in soup.find_all("li", attrs={"data-beds": True}):
        rec = _plan_card_from_li(li, base)
        if rec is None:
            continue
        link = rec.get("detail_url") or ""
        if link and link in seen_links:
            continue
        if link:
            seen_links.add(link)
        out.append(rec)

    # Pattern B — <div class="floorplan-item" data-beds=...>
    for div in soup.find_all(
        "div",
        class_=lambda c: bool(c) and "floorplan-item" in c,
    ):
        if not div.get("data-beds"):
            continue
        rec = _plan_card_from_div(div, base)
        if rec is None:
            continue
        link = rec.get("detail_url") or ""
        if link and link in seen_links:
            continue
        if link:
            seen_links.add(link)
        out.append(rec)

    # Pattern C — current towncommunity ``li.floorplan`` cards.  These do
    # not carry any ``data-*`` dimensions, so bind the parse to the exact
    # structured child classes and to one same-property /floorplans/ link.
    # Pattern A cards cannot enter here because they do not carry the
    # ``floorplan`` class; the seen-link guard also prevents mixed-theme
    # pages from duplicating a plan.
    for li in soup.select("li.floorplan"):
        rec = _plan_card_from_towncommunity_li(li, base)
        if rec is None:
            continue
        link = rec.get("detail_url") or ""
        if link in seen_links:
            continue
        seen_links.add(link)
        out.append(rec)

    return out


def _plan_card_from_li(li: Any, base: str) -> dict[str, Any] | None:
    """Pattern-A ``<li data-beds>`` → plan-summary dict."""
    beds = li.get("data-beds") or ""
    baths = li.get("data-baths") or ""
    price_raw = li.get("data-price") or ""
    sqft = li.get("data-sqft") or ""
    # Plan name: first <h3>. Fall back to the anchor text.
    h3 = li.find("h3")
    name = h3.get_text(" ", strip=True) if h3 else ""
    anchor = li.find("a", href=True)
    href = anchor.get("href") if anchor else ""
    if not name and anchor:
        name = anchor.get_text(" ", strip=True)
    status_span = li.find("span", class_="status")
    status_units: int | None = None
    if status_span:
        try:
            status_units = int(status_span.get_text(strip=True))
        except (TypeError, ValueError):
            status_units = None
    if not (beds or baths or sqft or name):
        return None
    return {
        "name": name,
        "beds": _int_str(beds),
        "baths": _int_str(baths),
        "sqft": _int_str(sqft),
        "price_raw": price_raw,
        "rent_low": _strip_dollars(price_raw),
        "detail_url": _abs_url(href, base),
        "status_units": status_units,
    }


def _plan_card_from_div(div: Any, base: str) -> dict[str, Any] | None:
    """Pattern-B ``<div class="floorplan-item" data-beds>`` → plan-summary dict.

    Plan name on the ascent theme lives on the ``id`` attr (e.g.
    ``id="One Bedroom"``) rather than on ``<h3>`` text — the inner
    ``<h3>`` is an anchor wrapper. Prefer the id; fall back to <h3>.
    """
    beds = div.get("data-beds") or ""
    baths = div.get("data-baths") or ""
    price_raw = div.get("data-price") or ""
    sqft = div.get("data-sqft") or ""
    name = div.get("id") or ""
    if not name:
        h3 = div.find("h3")
        if h3:
            name = h3.get_text(" ", strip=True)
    anchor = div.find("a", href=True)
    href = anchor.get("href") if anchor else ""
    if not (beds or baths or sqft or name):
        return None
    return {
        "name": name,
        "beds": _int_str(beds),
        "baths": _int_str(baths),
        "sqft": _int_str(sqft),
        "price_raw": price_raw,
        "rent_low": _strip_dollars(price_raw),
        "detail_url": _abs_url(href, base),
        "status_units": None,
    }


_TOWN_SQFT_RE = re.compile(
    r"^\s*(\d[\d,]*)\s*(?:[-\N{EN DASH}\N{EM DASH}]\s*(\d[\d,]*))?\s*$"
)


def _town_value(details: Any, class_name: str) -> str:
    """Return the value span from one structured towncommunity field."""
    node = details.select_one(f"li.{class_name}")
    if node is None:
        return ""
    span = node.find("span", recursive=False)
    if span is None:
        span = node.find("span")
    return span.get_text(" ", strip=True) if span is not None else ""


def _same_property_floorplan_url(href: str, base: str) -> str:
    """Resolve one authored detail link, rejecting cross-property hops."""
    resolved = _abs_url((href or "").strip(), base)
    if not resolved or not base:
        return ""
    try:
        target = urlsplit(resolved)
        origin = urlsplit(base)
    except ValueError:
        return ""
    target_host = (target.hostname or "").lower().removeprefix("www.")
    origin_host = (origin.hostname or "").lower().removeprefix("www.")
    path = re.sub(r"/+", "/", target.path or "")
    if not target_host or target_host != origin_host:
        return ""
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2 or segments[0].lower() != "floorplans":
        return ""
    return resolved


def _plan_card_from_towncommunity_li(
    li: Any, base: str
) -> dict[str, Any] | None:
    """Parse one current towncommunity ``li.floorplan`` catalogue card.

    The narrow structural/host gate matters: generic recommendation cards
    elsewhere on the page must never become this property's plans.
    """
    details = li.select_one("div.floorplan-details")
    if details is None:
        return None
    name_node = details.select_one("h2.name")
    name = name_node.get_text(" ", strip=True) if name_node is not None else ""
    if not name:
        return None

    authored_links: list[str] = []
    for anchor in li.select("a[href]"):
        resolved = _same_property_floorplan_url(str(anchor.get("href") or ""), base)
        if resolved and resolved not in authored_links:
            authored_links.append(resolved)
    if len(authored_links) != 1:
        return None

    beds_raw = _town_value(details, "beds")
    baths_raw = _town_value(details, "baths")
    sqft_raw = _town_value(details, "sqft")
    beds = _int_str(beds_raw)
    baths = _int_str(baths_raw)

    sqft = ""
    sqft_low: int | None = None
    sqft_high: int | None = None
    sqft_match = _TOWN_SQFT_RE.match(sqft_raw)
    if sqft_match:
        try:
            sqft_low = int(sqft_match.group(1).replace(",", ""))
            sqft_high = int(
                (sqft_match.group(2) or sqft_match.group(1)).replace(",", "")
            )
        except ValueError:
            sqft_low = sqft_high = None
        if (
            sqft_low is not None
            and sqft_high is not None
            and 150 <= sqft_low <= sqft_high <= 10_000
        ):
            sqft = re.sub(r"\s+", " ", sqft_raw).strip()
        else:
            sqft_low = sqft_high = None

    price_node = details.select_one("li.price")
    price_min_node = details.select_one("li.price .price-min")
    price_max_node = details.select_one("li.price .price-max")
    price_min_raw = (
        price_min_node.get_text(" ", strip=True) if price_min_node is not None else ""
    )
    price_max_raw = (
        price_max_node.get_text(" ", strip=True) if price_max_node is not None else ""
    )
    if price_min_raw and price_max_raw:
        price_raw = (
            price_min_raw
            if price_min_raw == price_max_raw
            else f"{price_min_raw} - {price_max_raw}"
        )
    else:
        price_raw = (
            price_node.get_text(" ", strip=True) if price_node is not None else ""
        )
    rent_low = _strip_dollars(price_min_raw or price_raw)
    rent_high = _strip_dollars(price_max_raw) if price_max_raw else rent_low

    # A structured catalogue card needs at least two independently labelled
    # dimensions. Name/link alone is too weak and would admit decorative or
    # recommendation content.
    structured_count = sum(
        bool(value) for value in (beds, baths, sqft, rent_low or rent_high)
    )
    if structured_count < 2:
        return None

    return {
        "name": name,
        "beds": beds,
        "baths": baths,
        # Keep the exact visible range in ``sqft``; the scalar V2 area field
        # intentionally resolves it to the low bound at formatting time.
        "sqft": sqft,
        "sqft_low": sqft_low,
        "sqft_high": sqft_high,
        "price_raw": price_raw,
        "rent_low": rent_low,
        "rent_high": rent_high,
        "detail_url": authored_links[0],
        "status_units": None,
    }


def _abs_url(href: str, base: str) -> str:
    """Resolve a relative ``/floorplans/{slug}`` href against the site
    base. Already-absolute URLs (``http://`` or ``//``) pass through.
    """
    if not href:
        return ""
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        # Protocol-relative — assume https (Resite serves all sites
        # over TLS in 2026).
        return f"https:{href}"
    if href.startswith("/") and base:
        return f"{base}{href}"
    if base:
        return f"{base}/{href.lstrip('/')}"
    return href


def parse_thinkreside_unit_table(
    html: str, plan: dict[str, Any], source_url: str
) -> list[dict[str, Any]]:
    """Walk ``<table class="fp-availability-list"> <tbody>`` rows on a
    per-plan detail page and emit one unit dict per row.

    The table is always 5 cells in the order
    ``[unit_number, available_date, sqft, rent, apply]``. The ``avail-
    date`` cell carries the canonical date in its ``data-date`` attr
    (``"Now"`` or ``MM/DD/YYYY``); the visible cell text mirrors it.
    Sqft (``<td class="g6m">``) is often empty — fall back to the
    plan-level ``data-sqft`` from the index card.

    Returns ``[]`` when there's no table at all, no ``<tbody>``, or
    every row is malformed. Empty ``<tbody>`` (the operator hasn't
    published unit-level inventory) returns ``[]``; the caller falls
    back to a plan-level summary row.
    """
    if not html or "fp-availability-list" not in html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    table = soup.find(
        "table",
        class_=lambda c: bool(c) and "fp-availability-list" in c,
    )
    if table is None:
        return []
    tbody = table.find("tbody")
    if tbody is None:
        return []

    out: list[dict[str, Any]] = []
    plan_name = str(plan.get("name") or "")
    plan_beds = str(plan.get("beds") or "")
    plan_baths = str(plan.get("baths") or "")
    plan_sqft = str(plan.get("sqft") or "")

    for tr in tbody.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            # Need at least unit / date / sqft / rent. ``apply`` is
            # optional (some operators ship a 4-cell variant).
            continue
        unit_number = cells[0].get_text(" ", strip=True)
        if not unit_number:
            continue
        # data-date attr is canonical; fall back to text.
        date_cell = cells[1]
        date_raw = date_cell.get("data-date") or date_cell.get_text(" ", strip=True)
        avail_date = _norm_avail_date(date_raw)
        sqft_text = cells[2].get_text(" ", strip=True)
        sqft = _int_str(sqft_text) or plan_sqft
        rent_text = cells[3].get_text(" ", strip=True)
        rent = _strip_dollars(rent_text)
        # Bedroom 0 ("Studio") must be preserved — _int_str returns "0".
        out.append(
            make_unit_dict(
                floor_plan_name=plan_name,
                bed_label=bed_label_from(
                    int(plan_beds) if plan_beds.isdigit() else None,
                    plan_name,
                ),
                bedrooms=plan_beds,
                bathrooms=plan_baths,
                sqft=sqft,
                unit_number=unit_number,
                rent_range=format_rent_range(rent, rent),
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                availability_date=avail_date,
                source_api_url=source_url,
                extraction_tier=_TIER,
                source_ids={
                    "thinkreside_plan_slug": _slug_from_url(source_url),
                    "thinkreside_unit": unit_number,
                },
            )
        )
    return out


def _slug_from_url(url: str) -> str:
    """``https://x.com/floorplans/barbee-1-bedroom`` → ``barbee-1-bedroom``.

    Used to populate ``source_ids.thinkreside_plan_slug`` so downstream
    diff tooling can group units back to their plan when the plan name
    in ``floor_plan_name`` is the human-facing display label rather
    than a stable identifier.
    """
    if not url:
        return ""
    return url.rstrip("/").rsplit("/", 1)[-1]


def thinkreside_plan_summary_row(
    plan: dict[str, Any], source_url: str
) -> dict[str, Any] | None:
    """Emit a plan-level summary when the per-plan unit table is empty
    or absent (Pattern B ascent theme, Pattern C towncommunity theme).

    Sets ``availability_status`` only from inventory evidence:

      * ``"AVAILABLE"`` — ``status_units`` is explicitly positive
      * ``"UNKNOWN"`` — no physical row or available-unit count exists;
        a catalogue price alone is not proof of current availability
      * ``"UNAVAILABLE"`` — only when ``status_units`` is explicitly
        0 (Pattern A index reports 0 available, but no roster page
        was fetched for some reason)

    Returns ``None`` when neither name nor any dim is present —
    nothing to emit.
    """
    name = str(plan.get("name") or "")
    if not name and not (plan.get("beds") or plan.get("sqft")):
        return None
    rent = plan.get("rent_low")
    rent_high = plan.get("rent_high")
    if not isinstance(rent_high, int):
        rent_high = rent if isinstance(rent, int) else None
    status_units = plan.get("status_units")
    if status_units == 0:
        status = "UNAVAILABLE"
    elif isinstance(status_units, int) and status_units > 0:
        status = "AVAILABLE"
    else:
        status = "UNKNOWN"
    available_units = ""
    if isinstance(status_units, int):
        available_units = str(status_units)
    return make_unit_dict(
        floor_plan_name=name,
        bed_label=bed_label_from(
            int(plan["beds"]) if str(plan.get("beds", "")).isdigit() else None,
            name,
        ),
        bedrooms=str(plan.get("beds") or ""),
        bathrooms=str(plan.get("baths") or ""),
        sqft=str(plan.get("sqft") or ""),
        unit_number="",
        rent_range=(
            format_rent_range(rent, rent_high)
            if isinstance(rent, int) or isinstance(rent_high, int)
            else ""
        ),
        rent_low=rent if isinstance(rent, int) else None,
        rent_high=rent_high,
        availability_status=status,
        available_units=available_units,
        source_api_url=source_url,
        extraction_tier=_TIER,
        source_ids={
            "thinkreside_plan_slug": _slug_from_url(
                plan.get("detail_url") or ""
            ),
        },
    )


def _fetch_text(url: str, timeout: int = 20) -> str:
    """Probe a ThinkRESIDE URL via ``probe_get`` — returns ``""`` on any
    non-200 / network failure so the caller falls through cleanly.
    """
    try:
        from ma_poc.pms.adapters._probe import probe_get
    except ImportError:
        return ""
    try:
        # ThinkRESIDE first-party pages are directly reachable.  Never spend
        # or invoke Web Unlocker from this production adapter path.
        r = probe_get(url, timeout=timeout, unlocker=False)
    except Exception as exc:
        log.debug("thinkreside fetch error url=%s err=%s", url, exc)
        return ""
    if getattr(r, "status_code", 0) != 200:
        return ""
    return str(getattr(r, "text", "") or "")


def _read_html(ctx: AdapterContext) -> str:
    """Recover the property page HTML from the L1 fetch_result."""
    fr = getattr(ctx, "fetch_result", None)
    body = getattr(fr, "body", None) if fr is not None else None
    if isinstance(body, bytes):
        try:
            return body.decode("utf-8", errors="replace")
        except Exception:
            return ""
    if isinstance(body, str):
        return body
    return ""


class ThinkResideAdapter:
    """ThinkRESIDE / Resite Multi Family Marketing adapter.

    Two-step DOM extraction:

      1. Parse plan-summary cards from the L1 HTML (``<li data-beds>``
         on ``/floorplans``, OR ``<div class="floorplan-item">`` on
         the home page).
      2. For each plan card with a ``detail_url``, fetch the per-plan
         page and parse ``<table class="fp-availability-list">`` for
         unit-level rows. Plans whose table is empty or absent emit a
         plan-level summary instead.

    The L1 body may already be ``/floorplans`` (when the resolver
    picked it) OR the home page. If the L1 body is the home page and
    no ``<li data-beds>`` plans are found, the adapter probes
    ``{base}/floorplans`` once before giving up.
    """

    pms_name: str = "thinkreside"

    def __init__(self) -> None:
        self._fingerprints = list(_FINGERPRINTS)

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    def matches_response_body(self, body: Any) -> bool:
        if isinstance(body, bytes):
            try:
                body = body.decode("utf-8", errors="replace")
            except Exception:
                return False
        if not isinstance(body, str):
            return False
        low = body.lower()
        return any(fp in low for fp in self._fingerprints)

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_TIER)

        html = _read_html(ctx)
        if not html:
            result.tier_used = f"{_TIER}_NO_HTML"
            result.errors.append("THINKRESIDE: no HTML body available")
            return result

        # Guard against detector misroute — confirm a fingerprint is
        # actually present before we emit anything.
        if not self.matches_response_body(html):
            result.tier_used = f"{_TIER}_NO_FINGERPRINT"
            result.errors.append(
                "THINKRESIDE: detector matched but body lacks Resite markers"
            )
            return result

        base = (getattr(ctx, "base_url", "") or "").rstrip("/")
        plans = parse_thinkreside_plan_index(html, base)
        index_url = base or ""

        # If the L1 body is the home page (Pattern A) and yielded no
        # plan cards, the operator likely puts the index at /floorplans —
        # probe that once before giving up. Pattern B (ascent) cards
        # live on the home page directly, so this only fires for sites
        # where the home page doesn't carry them.
        if not plans and base:
            fp_url = f"{base}/floorplans"
            fp_html = _fetch_text(fp_url)
            if fp_html:
                plans = parse_thinkreside_plan_index(fp_html, base)
                if plans:
                    index_url = fp_url

        if not plans:
            result.tier_used = f"{_TIER}_NO_PLANS"
            result.errors.append(
                "THINKRESIDE: no supported structured plan cards"
            )
            return result

        raw_units: list[dict[str, Any]] = []
        for plan in plans:
            detail_url = plan.get("detail_url") or ""
            unit_rows: list[dict[str, Any]] = []
            if detail_url:
                detail_html = _fetch_text(detail_url)
                if detail_html:
                    unit_rows = parse_thinkreside_unit_table(
                        detail_html, plan, detail_url
                    )
            if unit_rows:
                raw_units.extend(unit_rows)
                continue
            # No unit rows ⇒ plan-level summary. Empty tbody, absent
            # table, or no detail page all fall through here so the
            # plan catalog isn't lost.
            summary = thinkreside_plan_summary_row(
                plan, detail_url or index_url
            )
            if summary is not None:
                raw_units.append(summary)

        if not raw_units:
            result.tier_used = f"{_TIER}_EMPTY"
            result.errors.append(
                "THINKRESIDE: plan walk + per-plan drill yielded 0 rows"
            )
            return result

        from ma_poc.extraction.post_process import post_process

        pp = post_process(
            raw_units, property_id=getattr(ctx, "property_id", None)
        )
        if pp.n_admitted == 0:
            result.tier_used = f"{_TIER}_VALIDITY_REJECTED"
            result.errors.append(
                f"THINKRESIDE: {len(raw_units)} rows failed unit_validity"
            )
            return result

        result.units = pp.units
        result.plan_summaries = pp.plan_summaries
        result.winning_url = index_url or None
        result.tier_used = _TIER
        # Mirror Reinhold's confidence shape — DOM tables are small
        # and deterministic but lower-trust than RentCafe API.
        result.confidence = min(0.92, 0.7 + 0.04 * pp.n_admitted)
        return result
