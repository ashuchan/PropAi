"""RentCafe Nestin per-plan DOM recovery (2026-05-20).

The 2026-05-20 JSON-LD recovery probe (35 properties, see
``project_jsonld_recovery_2026-05-20.md``) found that 89% of the 298-prop
JSON-LD ALL_fail bucket are RentCafe-Nestin marketing sites where the
unit + rent + date data lives one nav-hop deeper at
``/floorplans/{plan-slug}`` — NOT in JSON-LD on the landing or index.

Two DOM layouts observed:

* **Layout A1 (table)** — Stonewater, Chatwell, Hayden Place:

      | Apartment | Sq. Ft. | Rent      | Date Available | Action     |
      | #4112-3   | 900     | $1,099.00 | 5/20/2026      | APPLY NOW  |

  Single ``<table>`` with a header row. Each ``<tr>`` under ``<tbody>``
  is one real unit. Columns keyed by ``<td data-label="Apartment">``
  attributes or by positional ``<td>`` index.

* **Layout A2 (card)** — Altair, Hampton (Brazos), Hampton Meridian, LINQ:

      Apartment: # 0200
      Starting at: $2,622.88
      ...repeated per unit, no table wrapper

  Repeated DOM blocks with literal ``APARTMENT[:\\s]+#\\s*<value>``
  text. Sometimes paired ``Date Available:`` text; often omitted in
  card layout.

Both layouts emit the same row schema. Detection at scrape time picks
the variant: prefer ``<table>`` shape, fall through to card-text scan.

The discovery + fetch uses ``probe_get`` (curl_cffi, optional residential
proxy + Web-Unlocker escalation) — same path SecureCafe probe uses,
clears the Cloudflare-fronted ``/floorplans`` index for sites where
patchright was 403-walled.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
)

log = logging.getLogger(__name__)


# RentCafe-Nestin marketing-template signal: image CDN host + per-plan
# detail-page URL pattern. Either fires the recovery on the
# property's ``/floorplans`` index.
_NESTIN_IMAGE_CDN = "resource.rentcafe.com"

# Per-plan detail-page link pattern. The href is relative to the property
# origin, e.g. ``<a href="/floorplans/a1">`` (Stonewater),
# ``<a href="/floorplans/1-bed-%7c-1-bath">`` (Chatwell, with URL-encoded
# pipe), ``<a href="/floorplans/plan-a-one-bedroom-renovated">`` (Altair).
_FLOORPLAN_DETAIL_HREF_RE = re.compile(
    r"^/floorplans/(?!$)(?P<slug>[A-Za-z0-9._%/+-]+?)/?$",
)

# Unit-table column header text (case-insensitive match on the ``<th>``
# text contents — Nestin uses these exact labels across all properties).
_TABLE_HEADER_LABELS = ("apartment", "rent", "date available")

# Card-layout unit identifier: ``APARTMENT: # <value>`` or ``Apartment # <value>``.
# Captures ``4112-3``, ``1120``, ``0200``, ``B306``, ``C201`` — strip any
# leading ``#`` and trim. The non-greedy ``[A-Z0-9-]+?`` accepts real
# Nestin unit numbers without picking up surrounding text.
_CARD_APT_RE = re.compile(
    r"\bApartment\b[:\s#]+(?P<unit>[A-Z0-9][A-Z0-9-]{0,15})\b",
    re.IGNORECASE,
)

# Card-layout rent: ``Starting at: $X.XX``, ``$X,XXX.XX``, or bare ``$X``.
# Handles sub-$1000 (e.g. Chatwell's $823.00) and decimals.
# 2026-05-20 correction from JSON-LD memo (rent-regex rule): the prior
# ``\$[1-9][0-9],?[0-9]{2,3}`` missed sub-$1k rents AND decimals; this
# version handles both.
_RENT_VALUE_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{1,2})?)")

# Date column / card-text pattern: ``M/D/YYYY``, ``MM/DD/YYYY``,
# ``M-D-YYYY``. ISO-format (``YYYY-MM-DD``) is also accepted by adapters
# downstream so we don't transform here.
_AVAILABILITY_DATE_RE = re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b")


def is_nestin_template(html: str) -> bool:
    """True when *html* looks like a RentCafe Nestin marketing site.

    Detection is image-CDN host based — the same image CDN is loaded by
    every Nestin-template property (``resource.rentcafe.com``), and the
    detection is robust to per-property CSS/class differences.
    """
    return _NESTIN_IMAGE_CDN in (html or "").lower()


def _money_to_int(text: str) -> int | None:
    """Parse a ``$X,XXX.XX`` or bare-digit string into a rounded int.

    Returns ``None`` on any parse failure (no rent extractable). Handles
    sub-$1000 ($823.00 → 823) and decimals (rounds half-up).
    """
    if not text:
        return None
    m = _RENT_VALUE_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    try:
        return int(round(float(raw)))
    except (ValueError, TypeError):
        return None


def _normalize_unit_number(raw: str) -> str:
    """Strip leading ``#`` and whitespace; return the canonical unit identifier."""
    return (raw or "").strip().lstrip("#").strip()


def _origin_of(url: str) -> str:
    """Return ``scheme://host`` for *url*; '' on parse failure."""
    try:
        p = urlparse(url)
    except Exception:
        return ""
    if not p.scheme or not p.netloc:
        return ""
    return urlunparse((p.scheme, p.netloc, "", "", "", ""))


def _find_floorplan_detail_urls(index_html: str, origin: str) -> list[str]:
    """Extract per-plan detail-page URLs from the ``/floorplans`` index HTML.

    Returns a deduped list of absolute URLs. Sorted by source order so the
    detail-page iteration is stable.
    """
    if not index_html or not origin:
        return []
    try:
        soup = BeautifulSoup(index_html, "html.parser")
    except Exception:
        return []

    seen: set[str] = set()
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("/floorplans/"):
            continue
        # Skip the index itself + non-detail subpaths (gallery, etc.) — the
        # detail-page regex requires a non-empty slug.
        m = _FLOORPLAN_DETAIL_HREF_RE.match(href)
        if not m:
            continue
        full = urljoin(origin + "/", href.lstrip("/"))
        if full in seen:
            continue
        seen.add(full)
        out.append(full)
    return out


def _parse_table_layout(
    detail_html: str, source_url: str, floor_plan_name: str = ""
) -> list[dict[str, Any]]:
    """Parse Layout A1 — RentCafe Nestin ``<table>`` shape.

    Looks for a ``<table>`` with ``<th>`` text matching Apartment + Rent +
    Date Available. Each ``<tr>`` under ``<tbody>`` becomes one unit.

    Returns ``[]`` if no matching table is found.
    """
    if not detail_html:
        return []
    try:
        soup = BeautifulSoup(detail_html, "html.parser")
    except Exception:
        return []

    for table in soup.find_all("table"):
        # Header text (lower-cased) for label-matching.
        headers = [
            (th.get_text(separator=" ", strip=True) or "").lower()
            for th in table.find_all("th")
        ]
        if not headers:
            continue
        # Require all three label tokens present in some header cell.
        header_blob = " | ".join(headers)
        if not all(label in header_blob for label in _TABLE_HEADER_LABELS):
            continue

        # Build a column-index → label map so we can pull values
        # positionally OR via ``data-label`` (Nestin uses both).
        col_index: dict[str, int] = {}
        for i, h in enumerate(headers):
            for label in ("apartment", "sq. ft.", "sq ft", "sqft", "rent", "date available"):
                if label in h and label not in col_index:
                    col_index[label] = i

        units: list[dict[str, Any]] = []
        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue

            def _by_label(label: str, _tds=tds, _cols=col_index) -> str:
                # 1) data-label attribute (most reliable)
                for td in _tds:
                    dl = (td.get("data-label") or "").strip().lower()
                    if dl and label in dl:
                        return td.get_text(separator=" ", strip=True) or ""
                # 2) positional fallback
                i = _cols.get(label)
                if i is not None and i < len(_tds):
                    return _tds[i].get_text(separator=" ", strip=True) or ""
                return ""

            apt_text = _by_label("apartment")
            unit_number = _normalize_unit_number(apt_text)
            if not unit_number:
                continue
            rent_int = _money_to_int(_by_label("rent"))
            sqft_text = _by_label("sq. ft.") or _by_label("sq ft") or _by_label("sqft")
            sqft = "".join(c for c in sqft_text if c.isdigit())
            date_text = _by_label("date available")
            date_m = _AVAILABILITY_DATE_RE.search(date_text)
            availability_date = date_m.group(1) if date_m else ""

            units.append(
                make_unit_dict(
                    floor_plan_name=floor_plan_name,
                    bed_label=bed_label_from(None, floor_plan_name),
                    bedrooms="",
                    bathrooms="",
                    sqft=sqft,
                    unit_number=unit_number,
                    rent_range=format_rent_range(rent_int, rent_int),
                    rent_low=rent_int,
                    rent_high=rent_int,
                    availability_status="AVAILABLE",
                    availability_date=availability_date,
                    source_api_url=source_url,
                    extraction_tier="TIER_1_DOM_RENTCAFE_NESTIN",
                )
            )
        if units:
            return units
    return []


def _parse_card_layout(
    detail_html: str, source_url: str, floor_plan_name: str = ""
) -> list[dict[str, Any]]:
    """Parse Layout A2 — RentCafe Nestin repeated-card / div-block shape.

    Looks for repeated text blocks matching ``APARTMENT: # <value>`` and
    extracts the nearest ``$X.XX`` rent + optional ``Date Available:`` date
    from the same enclosing element (or its parent if needed).

    Returns ``[]`` if no card-shape matches.
    """
    if not detail_html:
        return []
    try:
        soup = BeautifulSoup(detail_html, "html.parser")
    except Exception:
        return []

    units: list[dict[str, Any]] = []
    seen_units: set[str] = set()

    # Find every element containing the Apartment-# pattern. Walk up to find
    # the smallest container that ALSO has rent — that's the unit card.
    for el in soup.find_all(string=_CARD_APT_RE):
        m = _CARD_APT_RE.search(el)
        if not m:
            continue
        unit_number = _normalize_unit_number(m.group("unit"))
        if not unit_number or unit_number in seen_units:
            continue

        # Walk parent chain up to 4 levels to find the unit-card container
        # (the smallest enclosing block that includes rent text).
        container = el.parent
        rent_text = ""
        for _ in range(5):
            if container is None:
                break
            block_text = container.get_text(separator=" ", strip=True) or ""
            if "$" in block_text:
                rent_text = block_text
                break
            container = container.parent
        if not rent_text:
            continue
        rent_int = _money_to_int(rent_text)
        if rent_int is None:
            continue

        date_m = _AVAILABILITY_DATE_RE.search(rent_text)
        availability_date = date_m.group(1) if date_m else ""

        seen_units.add(unit_number)
        units.append(
            make_unit_dict(
                floor_plan_name=floor_plan_name,
                bed_label=bed_label_from(None, floor_plan_name),
                bedrooms="",
                bathrooms="",
                sqft="",
                unit_number=unit_number,
                rent_range=format_rent_range(rent_int, rent_int),
                rent_low=rent_int,
                rent_high=rent_int,
                availability_status="AVAILABLE",
                availability_date=availability_date,
                source_api_url=source_url,
                extraction_tier="TIER_1_DOM_RENTCAFE_NESTIN",
            )
        )
    return units


def parse_nestin_detail_page(
    detail_html: str, source_url: str, floor_plan_name: str = ""
) -> list[dict[str, Any]]:
    """Parse a single ``/floorplans/{slug}`` detail page for unit rows.

    Tries Layout A1 (table) first — it's the more reliable shape when
    present. Falls through to Layout A2 (card text) when no table matches.
    Returns ``[]`` on any extraction failure (caller decides whether to
    fall back to plan-level emission).
    """
    units = _parse_table_layout(detail_html, source_url, floor_plan_name)
    if units:
        return units
    return _parse_card_layout(detail_html, source_url, floor_plan_name)


def _section_heading_for_plan(detail_html: str) -> str:
    """Extract the plan name from a detail-page heading (``<h1>`` / ``<h2>``).

    Nestin detail pages title the plan in the first heading element, e.g.
    "1 Bed | 1 Bath" or "Coronado". Falls back to ``""`` if no heading is
    found.
    """
    if not detail_html:
        return ""
    try:
        soup = BeautifulSoup(detail_html, "html.parser")
    except Exception:
        return ""
    for tag in ("h1", "h2", "h3"):
        el = soup.find(tag)
        if el:
            text = el.get_text(separator=" ", strip=True)
            if text and len(text) < 80:
                return text
    return ""


async def recover_rentcafe_nestin_per_plan(
    landing_html: str,
    base_url: str,
    *,
    fetcher: Any = None,  # callable(url) -> object with .status_code + .text
) -> tuple[list[dict[str, Any]], str]:
    """Run the Nestin per-plan recovery against the property's marketing site.

    Args:
        landing_html: server HTML of the property landing or ``/floorplans``
            page. Must contain the ``resource.rentcafe.com`` Nestin signal
            for the recovery to fire; caller should pre-check via
            ``is_nestin_template`` to skip work on non-Nestin properties.
        base_url: the property's ``scheme://host`` (used to resolve relative
            href links to absolute URLs for fetching).
        fetcher: callable taking a URL, returning an object with
            ``.status_code`` and ``.text`` attributes. Default uses
            ``ma_poc.pms.adapters._probe.probe_get`` (curl_cffi + optional
            residential proxy + Web-Unlocker escalation). Injected for
            unit testing.

    Returns:
        ``(units, source_url)`` where ``units`` is the list of unit dicts
        in the canonical adapter shape (``make_unit_dict``) with
        ``extraction_tier="TIER_1_DOM_RENTCAFE_NESTIN"``. ``source_url`` is
        the ``/floorplans`` URL used (the canonical provenance URL).
        Returns ``([], "")`` when the property isn't Nestin-shaped or no
        detail pages yielded units.
    """
    if not is_nestin_template(landing_html or ""):
        return [], ""

    origin = _origin_of(base_url)
    if not origin:
        return [], ""

    # The detail-page links may be on the landing page already, or on a
    # dedicated ``/floorplans`` index. Try both: scan landing_html first,
    # then fetch /floorplans if no detail links found.
    detail_urls = _find_floorplan_detail_urls(landing_html, origin)
    floorplans_url = f"{origin}/floorplans"
    if not detail_urls:
        if fetcher is None:
            try:
                from ma_poc.pms.adapters._probe import probe_get
                fetcher = lambda u: probe_get(u, timeout=20)  # noqa: E731
            except ImportError:
                return [], ""
        try:
            resp = fetcher(floorplans_url)
        except Exception as exc:
            log.debug("nestin /floorplans fetch failed: %s", exc)
            return [], ""
        if getattr(resp, "status_code", 0) != 200:
            return [], ""
        index_html = getattr(resp, "text", "") or ""
        detail_urls = _find_floorplan_detail_urls(index_html, origin)

    if not detail_urls:
        return [], ""

    # Resolve the fetcher now (used for every detail-page fetch).
    if fetcher is None:
        try:
            from ma_poc.pms.adapters._probe import probe_get
            fetcher = lambda u: probe_get(u, timeout=20)  # noqa: E731
        except ImportError:
            return [], ""

    all_units: list[dict[str, Any]] = []
    for detail_url in detail_urls:
        try:
            resp = fetcher(detail_url)
        except Exception as exc:
            log.debug("nestin detail-page fetch failed url=%s err=%s", detail_url, exc)
            continue
        if getattr(resp, "status_code", 0) != 200:
            continue
        detail_html = getattr(resp, "text", "") or ""
        if not detail_html:
            continue
        plan_name = _section_heading_for_plan(detail_html)
        units = parse_nestin_detail_page(detail_html, detail_url, plan_name)
        all_units.extend(units)

    return all_units, floorplans_url
