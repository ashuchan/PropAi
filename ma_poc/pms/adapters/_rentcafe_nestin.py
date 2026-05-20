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
# The ``#`` is REQUIRED (per the 2026-05-20 pre-canary probe) — without it
# the regex matched "Apartment Homes" / "Apartment Available" / "Apartment
# with" chrome text, extracting bogus unit numbers like "Homes" / "with".
# Captures ``4112-3``, ``1120``, ``0200``, ``B306``, ``C201``.
_CARD_APT_RE = re.compile(
    r"\bApartment\b\s*(?::\s*)?#\s*(?P<unit>[A-Z0-9][A-Z0-9-]{0,15})\b",
    re.IGNORECASE,
)

# Stonewater-shape (Layout A3): applyGAClick button.
# Static HTML carries unit + rent + sqft as arguments to a JS click
# handler — neither table nor card text:
#   <button id="4112-3" onclick="applyGAClick('A1', '1 Bed(s)', '900',
#                                              '1099.00', ...)">Apply Now</button>
# The button ``id`` is the real unit_number; ``onclick`` args 1-4 are
# plan_name, beds-label, sqft, rent. Discovered during 2026-05-20
# pre-canary probe against stonewaterpark.com/floorplans/a1.
_APPLYGA_BUTTON_RE = re.compile(
    r"""id\s*=\s*["'](?P<unit>[A-Z0-9][A-Z0-9-]{1,15})["']"""
    r"""[^>]*onclick\s*=\s*["']applyGAClick\("""
    r"""\s*['"]([^'"]*)['"]"""           # arg1: plan_name
    r"""\s*,\s*['"]([^'"]*)['"]"""       # arg2: beds-label
    r"""\s*,\s*['"]([^'"]*)['"]"""       # arg3: sqft
    r"""\s*,\s*['"]([^'"]*)['"]"""       # arg4: rent
    r""".*?["']\s*>""",
    re.IGNORECASE | re.DOTALL,
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


# Extract the apartment-number value from a text cell that may include
# the "Apartment" label prefix (real Nestin tables sometimes ship an
# inline ``<span class="sr-only">Apartment</span>`` accessibility label
# that get_text() concatenates with the value), an optional ``#``, and
# trailing whitespace. Captures the alphanumeric/hyphenated value.
_UNIT_NUM_EXTRACT_RE = re.compile(
    r"(?:Apartment\b[:\s]*)?#?\s*([A-Z0-9][A-Z0-9-]*)\b",
    re.IGNORECASE,
)


def _normalize_unit_number(raw: str) -> str:
    """Extract canonical unit identifier from text that may include the
    ``Apartment`` label prefix or leading ``#``.

    Examples:
      ``"#1120"`` → ``"1120"``
      ``"Apartment: #1120"`` → ``"1120"``       (sr-only label leak)
      ``"Apartment 4112-3"`` → ``"4112-3"``      (no ``#`` separator)
      ``"  #B306  "`` → ``"B306"``
      ``""`` → ``""``

    2026-05-20: handles the pre-canary probe finding — real Chatwell /
    Hayden Place tables omit ``data-label`` attrs AND the positional-
    fallback ``<td>`` text includes the "Apartment" sr-only span. Without
    this prefix-strip, the unit_number is polluted with the label text.
    """
    if not raw:
        return ""
    text = raw.strip()
    if not text:
        return ""
    m = _UNIT_NUM_EXTRACT_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.lstrip("#").strip()


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
    # 2026-05-20 pre-canary e2e probe finding: Playwright rewrites relative
    # ``<a href="/floorplans/{slug}">`` anchors to absolute URLs
    # (``<a href="https://www.stonewaterpark.com/floorplans/{slug}">``)
    # in ``page.content()`` output. The raw curl_cffi HTML keeps them
    # relative. Accept both shapes — convert absolute matching-origin
    # hrefs to relative paths before applying the slug regex.
    origin_prefix = origin.rstrip("/") + "/"
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        rel_path = href
        if href.startswith(origin_prefix):
            rel_path = "/" + href[len(origin_prefix):]
        elif href.startswith(origin + "/"):  # exact origin (no trailing slash)
            rel_path = href[len(origin):]
        if not rel_path.startswith("/floorplans/"):
            continue
        # Skip the index itself + non-detail subpaths (gallery, etc.) — the
        # detail-page regex requires a non-empty slug.
        m = _FLOORPLAN_DETAIL_HREF_RE.match(rel_path)
        if not m:
            continue
        full = urljoin(origin + "/", rel_path.lstrip("/"))
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


def _parse_applyga_button_layout(
    detail_html: str, source_url: str, floor_plan_name: str = ""
) -> list[dict[str, Any]]:
    """Parse Layout A3 — Stonewater-shape applyGAClick button.

    Real-world (verified 2026-05-20 against stonewaterpark.com/floorplans/a1):

      <button id="4112-3" onclick="applyGAClick('A1', '1 Bed(s)', '900',
                                                 '1099.00', ...)">Apply Now</button>

    The button ``id`` is the unit_number; ``onclick`` args carry the plan
    name, beds-label, sqft, and rent.

    Returns ``[]`` if no apply-button matches.
    """
    if not detail_html or "applyGAClick" not in detail_html:
        return []

    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in _APPLYGA_BUTTON_RE.finditer(detail_html):
        unit_number = _normalize_unit_number(m.group("unit"))
        if not unit_number or unit_number in seen:
            continue
        plan_name = (m.group(2) or "").strip() or floor_plan_name
        beds_label = (m.group(3) or "").strip()  # e.g. "1 Bed(s)" / "Studio"
        sqft_raw = (m.group(4) or "").strip()
        rent_raw = (m.group(5) or "").strip()
        sqft = "".join(c for c in sqft_raw if c.isdigit())
        # onclick args carry the bare rent number ("1099.00") without a
        # ``$`` prefix — ``_money_to_int`` requires ``$``, so parse via
        # ``float()`` directly.
        try:
            rent_int = int(round(float(rent_raw.replace(",", "")))) if rent_raw else None
        except (ValueError, TypeError):
            rent_int = None
        if rent_int is None or rent_int <= 0:
            continue

        # beds-label → numeric extraction (Studio → 0, "1 Bed(s)" → 1)
        beds = ""
        if "studio" in beds_label.lower():
            beds = "0"
        else:
            bm = re.search(r"(\d+)", beds_label)
            if bm:
                beds = bm.group(1)

        seen.add(unit_number)
        units.append(
            make_unit_dict(
                floor_plan_name=plan_name,
                bed_label=beds_label or bed_label_from(None, plan_name),
                bedrooms=beds,
                bathrooms="",
                sqft=sqft,
                unit_number=unit_number,
                rent_range=format_rent_range(rent_int, rent_int),
                rent_low=rent_int,
                rent_high=rent_int,
                availability_status="AVAILABLE",
                availability_date="",
                source_api_url=source_url,
                extraction_tier="TIER_1_DOM_RENTCAFE_NESTIN",
            )
        )
    return units


def parse_nestin_detail_page(
    detail_html: str, source_url: str, floor_plan_name: str = ""
) -> list[dict[str, Any]]:
    """Parse a single ``/floorplans/{slug}`` detail page for unit rows.

    Tries Layout A1 (table) first — most reliable when present.
    Falls through to Layout A3 (applyGAClick button — Stonewater-shape;
    discovered 2026-05-20 pre-canary probe) which is more structured than
    Layout A2 because the data is in well-known ``onclick`` args.
    Finally falls through to Layout A2 (free-form card text) for the
    Altair / Hampton / Meridian / LINQ shape.

    Returns ``[]`` on any extraction failure (caller decides whether to
    fall back to plan-level emission).
    """
    units = _parse_table_layout(detail_html, source_url, floor_plan_name)
    if units:
        return units
    units = _parse_applyga_button_layout(detail_html, source_url, floor_plan_name)
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


class _PageFetchResp:
    """Minimal response shim returned by the in-browser page fetcher."""

    __slots__ = ("status_code", "text")

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


# In-browser fetch via ``page.evaluate(fetch(...))``. Inherits the live
# Playwright session's cookies — including freshly-rotated Cloudflare
# ``cf_clearance`` and ``__cf_bm`` cookies that ``probe_get`` cannot
# obtain because CF challenges new cookies per-path. This is the
# difference between 4/4 success in the 2026-05-20 e2e probe and
# 13/13 detail-page 403s when ``probe_get`` was used.
_PAGE_FETCH_JS = """async (url) => {
    try {
        const r = await fetch(url, {
            credentials: 'include',
            headers: { 'Accept': 'text/html,application/xhtml+xml' },
        });
        return { status: r.status, text: await r.text() };
    } catch (e) {
        return { status: 0, text: '', error: String(e) };
    }
}"""


async def _fetch_via_page(page: Any, url: str) -> _PageFetchResp:
    """Fetch *url* using the browser's fetch API. Returns a response shim."""
    try:
        result = await page.evaluate(_PAGE_FETCH_JS, url)
    except Exception as exc:
        log.debug("nestin page.evaluate fetch failed url=%s err=%s", url, exc)
        return _PageFetchResp(0, "")
    if not isinstance(result, dict):
        return _PageFetchResp(0, "")
    return _PageFetchResp(int(result.get("status", 0) or 0), str(result.get("text", "") or ""))


async def recover_rentcafe_nestin_per_plan(
    landing_html: str,
    base_url: str,
    *,
    fetcher: Any = None,  # callable(url) -> object with .status_code + .text
    page: Any = None,     # Playwright Page; when provided, used in preference
                          # to fetcher for ALL fetches so CF clearance carries
) -> tuple[list[dict[str, Any]], str]:
    """Run the Nestin per-plan recovery against the property's marketing site.

    Args:
        landing_html: server HTML of the property landing or ``/floorplans``
            page. Must contain the ``resource.rentcafe.com`` Nestin signal
            for the recovery to fire; caller should pre-check via
            ``is_nestin_template`` to skip work on non-Nestin properties.
        base_url: the property's ``scheme://host`` (used to resolve relative
            href links to absolute URLs for fetching).
        fetcher: callable taking a URL, returning an object (or coroutine
            yielding an object) with ``.status_code`` and ``.text`` attrs.
            Default uses ``ma_poc.pms.adapters._probe.probe_get`` (curl_cffi
            + optional residential proxy + Web-Unlocker escalation).
            Injected for unit testing.
        page: optional Playwright Page from the live adapter dispatch.
            When provided, detail-page fetches go through ``page.evaluate(
            fetch(...))`` so they inherit the browser's Cloudflare
            clearance cookies. ``probe_get``'s static cf_clearance is
            insufficient for many Nestin sites (CF rotates per-path).

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

    import asyncio as _asyncio

    async def _do_fetch(url: str) -> Any:
        """Unified fetcher: page.evaluate(fetch) when page available,
        else user-injected fetcher, else probe_get default.

        2026-05-20 e2e finding: the property-scoped ContextVar carries
        the HOMEPAGE's ``cf_clearance`` cookie (minted during the L1
        fetch). When that cookie is sent on a DETAIL-page fetch (the
        same host but a different path), Cloudflare returns 403
        because the clearance is path-scoped. Detail-page fetches
        WITHOUT any clearance cookies trigger a fresh CF challenge
        that curl_cffi's chrome120 impersonation passes (200 OK).
        Solution: temporarily clear the cookies for detail-page
        probe_get calls. Other adapters' homepage-cookie behavior
        is unaffected (token scoped to this call only)."""
        if page is not None:
            return await _fetch_via_page(page, url)
        if fetcher is not None:
            r = fetcher(url)
            if _asyncio.iscoroutine(r):
                r = await r
            return r
        try:
            from ma_poc.pms.adapters._probe import (
                probe_get,
                reset_clearance_cookies,
                set_clearance_cookies,
            )
        except ImportError:
            return _PageFetchResp(0, "")
        _clr_tok = set_clearance_cookies(None)
        try:
            return probe_get(url, timeout=20)
        finally:
            reset_clearance_cookies(_clr_tok)

    # The detail-page links may be on the landing page already, or on a
    # dedicated ``/floorplans`` index. Try both: scan landing_html first,
    # then fetch /floorplans if no detail links found.
    detail_urls = _find_floorplan_detail_urls(landing_html, origin)
    floorplans_url = f"{origin}/floorplans"
    if not detail_urls:
        try:
            resp = await _do_fetch(floorplans_url)
        except Exception as exc:
            log.debug("nestin /floorplans fetch failed: %s", exc)
            return [], ""
        if getattr(resp, "status_code", 0) != 200:
            return [], ""
        index_html = getattr(resp, "text", "") or ""
        detail_urls = _find_floorplan_detail_urls(index_html, origin)

    if not detail_urls:
        return [], ""

    # Fail-fast on CF-protected sites: when the first detail fetch returns
    # a CF block status (403/429/503) AND we have no live browser page,
    # subsequent fetches will all fail the same way — abort to avoid
    # wasting ~30s × N-plans of pipeline time. The recovery is best
    # exercised in RENDER mode (page != None); the static-cookie probe_get
    # path can only clear CF for the homepage, not detail URLs.
    _CF_BLOCK_STATUSES = (403, 429, 503)
    all_units: list[dict[str, Any]] = []
    consecutive_block_count = 0
    for detail_url in detail_urls:
        try:
            resp = await _do_fetch(detail_url)
        except Exception as exc:
            log.debug("nestin detail-page fetch failed url=%s err=%s", detail_url, exc)
            continue
        status = getattr(resp, "status_code", 0) or 0
        if status in _CF_BLOCK_STATUSES:
            consecutive_block_count += 1
            # After 3 consecutive CF blocks with no successes yet, the site
            # is CF-walled on detail URLs — bail out. Saves ~25-60s on a
            # 13-plan property like Stonewater.
            if consecutive_block_count >= 3 and not all_units:
                log.info(
                    "nestin recovery aborting after %d consecutive CF blocks "
                    "(origin=%s) — site requires live browser session",
                    consecutive_block_count, origin,
                )
                break
            continue
        consecutive_block_count = 0
        if status != 200:
            continue
        detail_html = getattr(resp, "text", "") or ""
        if not detail_html:
            continue
        plan_name = _section_heading_for_plan(detail_html)
        units = parse_nestin_detail_page(detail_html, detail_url, plan_name)
        all_units.extend(units)

    return all_units, floorplans_url
