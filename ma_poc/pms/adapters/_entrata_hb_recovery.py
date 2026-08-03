"""Bounded Hyperbrowser recovery for Entrata ProspectPortal plan grids.

Entrata marketing pages commonly publish one exact
``/{city}/{property}/conventional/`` link.  Plain HTTP and the user's ordinary
Chrome session receive a Cloudflare interstitial for this cohort, while the
configured clean Hyperbrowser residential render can load the public grid.
The grid itself is usually plan-level; real apartments, when available, live
on the same-origin per-plan links.

This helper spends at most one Hyperbrowser session, loads the published grid,
and replays its bounded same-origin plan links inside that session.  CAPTCHA
solving remains hard-disabled by :mod:`ma_poc.fetch.hyperbrowser_backend`; no
unlocker, FlareSolverr, or fingerprint-rotation path is reachable here.
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

_MAX_PLAN_URLS = 30
_MAX_INDEX_BODY_BYTES = 3_000_000
_MAX_PLAN_BODY_CHARS = 1_000_000
_HB_SETTLE_SECONDS = 5.0
_MAX_INDEX_NAV_ATTEMPTS = 2

_CONVENTIONAL_PATH_RE = re.compile(
    r"^/[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*/conventional/?$",
    re.IGNORECASE,
)
_LEGACY_CONVENTIONAL_PATH_RE = re.compile(
    r"^/Apartments/module/property_info/property(?:%5B|\[)id(?:%5D|\])/\d+/conventional/?$",
    re.IGNORECASE,
)
_LEGACY_CONVENTIONAL_ATTR_RE = re.compile(
    r"""(?:href|action)=["']((?:https?://[^"'\s]+)?/Apartments/module/property_info/property(?:%5B|\[)id(?:%5D|\])/\d+/conventional/?)["']""",
    re.IGNORECASE,
)
_LEGACY_PROPERTY_ROOT_PATH_RE = re.compile(
    r"^/Apartments/module/property_info/property(?:%5B|\[)id(?:%5D|\])/\d+/?$",
    re.IGNORECASE,
)
_LEGACY_PROPERTY_ROOT_ATTR_RE = re.compile(
    r"""(?:href|action)=["']((?:https?:)?//[^"'\s]+/Apartments/module/property_info/property(?:%5B|\[)id(?:%5D|\])/\d+/?)["']""",
    re.IGNORECASE,
)
_NAME_STOPWORDS = frozenset(
    {
        "apartment",
        "apartments",
        "and",
        "apt",
        "apts",
        "community",
        "home",
        "homes",
        "of",
        "on",
        "residence",
        "residences",
        "the",
        "townhome",
        "townhomes",
        "townhouse",
        "townhouses",
        "at",
        "i",
        "ii",
        "iii",
        "iv",
    }
)
_ADDRESS_STOPWORDS = frozenset(
    {
        "avenue",
        "ave",
        "boulevard",
        "blvd",
        "circle",
        "court",
        "ct",
        "drive",
        "dr",
        "east",
        "highway",
        "lane",
        "ln",
        "north",
        "northeast",
        "northwest",
        "parkway",
        "pkwy",
        "place",
        "pl",
        "road",
        "rd",
        "south",
        "southeast",
        "southwest",
        "st",
        "street",
        "unit",
        "west",
    }
)
_RENT_FIELDS = (
    "market_rent_low",
    "market_rent_high",
    "rent_low",
    "rent_high",
    "asking_rent",
    "rent",
)

_FETCH_PLAN_JS = f"""async (path) => {{
  try {{
    const response = await fetch(path, {{
      credentials: 'include',
      headers: {{'Accept': 'text/html,application/xhtml+xml'}}
    }});
    const body = await response.text();
    return {{
      status: response.status,
      oversized: body.length > {_MAX_PLAN_BODY_CHARS},
      body: body.length <= {_MAX_PLAN_BODY_CHARS} ? body : ''
    }};
  }} catch (e) {{
    return {{status: 0, oversized: false, body: ''}};
  }}
}}"""

_FETCH_XHR_JS = f"""async (path) => {{
  try {{
    const response = await fetch(path, {{
      credentials: 'include',
      headers: {{
        'Accept': 'text/html, */*; q=0.01',
        'X-Requested-With': 'XMLHttpRequest'
      }}
    }});
    const body = await response.text();
    return {{
      status: response.status,
      oversized: body.length > {_MAX_PLAN_BODY_CHARS},
      body: body.length <= {_MAX_PLAN_BODY_CHARS} ? body : ''
    }};
  }} catch (e) {{
    return {{status: 0, oversized: false, body: ''}};
  }}
}}"""


@dataclass(frozen=True)
class EntrataHbRecovery:
    """Outcome of one strictly-scoped ProspectPortal browser drill."""

    attempted: bool = False
    complete: bool = False
    units: list[dict[str, Any]] = field(default_factory=list)
    plan_rows: list[dict[str, Any]] = field(default_factory=list)
    html_responses: list[dict[str, Any]] = field(default_factory=list)
    unit_source_provenance: list[dict[str, Any]] = field(default_factory=list)
    winning_url: str = ""
    failure_reason: str = ""


@dataclass(frozen=True)
class _EntrataSnippetTarget:
    """Exact operator-published Entrata website-snippet boundary."""

    url: str
    host: str
    property_id: str
    provider_name_tokens: tuple[str, ...]


_SNIPPET_DETAIL_PATH_RE = re.compile(
    r"^/apartments/module/property_floorplans/"
    r"property\[id\]/(?P<property_id>\d{3,12})/"
    r"property_floorplan\[id\]/(?P<floorplan_id>\d{2,12})/"
    r"(?:[^/]+/)*occupancy_type/conventional(?:/|$)",
    re.IGNORECASE,
)
_SNIPPET_IFRAME_ID_RE = re.compile(r"^website_(?P<property_id>\d{3,12})$")


def _name_tokens(value: str) -> list[str]:
    return [
        token for token in re.findall(r"[a-z0-9]+", (value or "").casefold()) if token not in _NAME_STOPWORDS
    ]


def _parent_address_match(address: str, visible_tokens: set[str]) -> bool:
    """Match one configured street number plus a distinctive street token.

    This is only a fallback for a provider-brand alias (for example the CSV
    name ``Avenir on Fifteenth`` vs. the published brand ``Avenir``).  A bare
    city/ZIP or generic street suffix can never satisfy the boundary.
    """
    tokens = re.findall(r"[a-z0-9]+", (address or "").casefold())
    street_numbers = [token for token in tokens if token.isdigit()]
    distinctive = [
        token
        for token in tokens
        if len(token) >= 3 and token not in _ADDRESS_STOPWORDS and not token.isdigit()
    ]
    return bool(
        street_numbers
        and distinctive
        and street_numbers[0] in visible_tokens
        and any(token in visible_tokens for token in distinctive)
    )


def _property_owned_snippet_target(ctx: Any) -> _EntrataSnippetTarget | None:
    """Return one strict Entrata website-snippet iframe, or ``None``.

    Scully's public roster is not a ``/conventional/`` grid and its host does
    not end in ``prospectportal.com``.  The marketing page instead publishes a
    numeric Entrata ``website_<property-id>`` iframe on an exact property child
    host.  Admit that shape only when the parent page contains every
    distinctive configured-name token plus the configured city/ZIP, the child
    belongs to the marketing domain, and the child host itself contains at
    least one distinctive property-name token.  Multiple matching inventory
    iframes are ambiguous and fail closed.
    """
    html = _body_from_ctx(ctx)
    base_url = _source_url_from_ctx(ctx)
    property_name = str(getattr(ctx, "property_name", "") or "").strip()
    address = str(getattr(ctx, "address", "") or "").strip()
    city = str(getattr(ctx, "city", "") or "").strip()
    zip_code = str(getattr(ctx, "zip_code", "") or "").strip()
    if not html or not base_url or not property_name:
        return None

    try:
        from bs4 import BeautifulSoup

        parent = urlsplit(base_url)
        parent_host = (parent.hostname or "").casefold().rstrip(".")
        marketing_host = parent_host.removeprefix("www.")
        soup = BeautifulSoup(html, "lxml")
        visible = soup.get_text(" ", strip=True).casefold()
    except Exception:
        return None
    wanted = _name_tokens(property_name)
    visible_tokens = set(_name_tokens(visible))
    full_name_match = all(token in visible_tokens for token in wanted)
    exact_address_fallback = _parent_address_match(address, visible_tokens)
    if (
        not marketing_host
        or not wanted
        or not (full_name_match or exact_address_fallback)
        or (city and not all(token in visible_tokens for token in _name_tokens(city)))
        or (zip_code and zip_code[:5] not in visible)
    ):
        return None

    targets: list[_EntrataSnippetTarget] = []
    for iframe in soup.select("iframe[src]"):
        raw_src = str(iframe.get("src") or "").strip()
        candidate = urljoin(base_url, raw_src)
        try:
            parsed = urlsplit(candidate)
            explicit_port = parsed.port
            query = parse_qs(parsed.query)
        except (TypeError, ValueError):
            continue
        host = (parsed.hostname or "").casefold().rstrip(".")
        iframe_id = str(iframe.get("id") or "").strip().casefold()
        id_match = _SNIPPET_IFRAME_ID_RE.fullmatch(iframe_id)
        compact_host = re.sub(r"[^a-z0-9]+", "", host)
        provider_tokens = tuple(token for token in wanted if len(token) >= 4 and token in compact_host)
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or explicit_port is not None
            or not host
            or host == marketing_host
            or not host.endswith(f".{marketing_host}")
            or id_match is None
            or not provider_tokens
            or query.get("snippet_type", [""])[0].casefold() != "website"
            or query.get("is_responsive_snippet", [""])[0] != "1"
            or query.get("occupancy_type", [""])[0].casefold() not in {"1", "conventional"}
            or "application_authentication" in parsed.path.casefold()
            or "guest_card" in parsed.path.casefold()
        ):
            continue
        # A protocol-relative operator iframe on an HTTP marketing page is
        # routinely upgraded by the provider to HTTPS (Bridgeview is the live
        # Scully example). Navigate to the security upgrade directly so the
        # later same-origin boundary does not misclassify the redirect as a
        # cross-origin hop. Explicit foreign hosts remain rejected above.
        if parsed.scheme.casefold() == "http" and raw_src.startswith("//"):
            candidate = parsed._replace(scheme="https").geturl()
        if "host_domain=" not in candidate.casefold():
            separator = "" if candidate.endswith(("?", "&")) else ("&" if parsed.query else "?")
            candidate += separator + f"host_domain={parent_host}"
        targets.append(
            _EntrataSnippetTarget(
                url=candidate,
                host=host,
                property_id=id_match.group("property_id"),
                provider_name_tokens=provider_tokens,
            )
        )
    return targets[0] if len(targets) == 1 else None


def _snippet_provider_identity_match(
    html: str,
    target: _EntrataSnippetTarget,
) -> bool:
    if not html:
        return False
    try:
        from bs4 import BeautifulSoup

        visible = set(_name_tokens(BeautifulSoup(html, "lxml").get_text(" ", strip=True)))
    except Exception:
        return False
    return all(token in visible for token in target.provider_name_tokens)


def _snippet_plan_links(
    html: str,
    index_url: str,
    target: _EntrataSnippetTarget,
) -> tuple[list[tuple[str, str, str]], bool]:
    """Return exact same-property ``(url, relative, plan_name)`` links.

    The boolean is an integrity result. A foreign host/property id, missing
    explicit plan title, duplicate URL with conflicting title, or over-budget
    index invalidates the entire snippet rather than silently selecting rows.
    """
    try:
        from bs4 import BeautifulSoup

        from ma_poc.pms.adapters.entrata import _entrata_snippet_plan_name

        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return [], False
    by_url: dict[str, tuple[str, str]] = {}
    observed_detail = False
    for anchor in soup.select("a[href]"):
        raw = str(anchor.get("href") or "").strip()
        candidate = urljoin(index_url, raw)
        try:
            parsed = urlsplit(candidate)
        except (TypeError, ValueError):
            continue
        match = _SNIPPET_DETAIL_PATH_RE.match(unquote(parsed.path))
        if match is None:
            continue
        observed_detail = True
        if (parsed.hostname or "").casefold().rstrip(".") != target.host or match.group(
            "property_id"
        ) != target.property_id:
            return [], False
        relative = _same_origin_relative(candidate, index_url)
        plan_name = _entrata_snippet_plan_name(anchor)
        if not relative or not plan_name:
            return [], False
        existing = by_url.get(candidate)
        if existing is not None and existing[1] != plan_name:
            return [], False
        by_url[candidate] = (relative, plan_name)
    if not observed_detail or not by_url or len(by_url) > _MAX_PLAN_URLS:
        return [], False
    return [(url, relative, plan_name) for url, (relative, plan_name) in by_url.items()], True


def strict_conventional_url(
    html: str,
    base_url: str,
    property_name: str,
) -> str:
    """Return one identity-matched Entrata conventional URL, or ``""``.

    The underlying Entrata helper permits a linked ``*.prospectportal.com``
    twin because vanity sites routinely hand off there.  Dedicated property
    microsites can also publish an exact grid on a different vanity host, so
    this browser route considers those raw published candidates behind a
    stronger boundary: every distinctive property-name token must occur in
    the candidate host/path.  A PMC page linking a sibling property therefore
    fails closed instead of drilling its roster.
    """
    if not base_url or not property_name:
        return ""

    from ma_poc.pms.adapters.entrata import (
        _PP_CONVENTIONAL_RE,
        _find_pp_conventional_index,
    )

    wanted = _name_tokens(property_name)
    if not wanted:
        return ""

    # A caller may already be positioned on the exact conventional grid.  In
    # that case Entrata pages do not normally emit a redundant self-link, so
    # anchor-only discovery would skip the browser recovery entirely.  Feed
    # the source URL through the same path, origin and property-identity gates
    # as discovered anchors; broad marketing URLs still fail closed below.
    candidates = _find_pp_conventional_index(html, base_url) if html else []
    # The shared static helper intentionally rejects non-ProspectPortal
    # cross-host URLs because it lacks property identity context.  Here the
    # property-name gate below provides that context, allowing a dedicated
    # property microsite while still rejecting portfolio sibling links.
    if html:
        for match in _PP_CONVENTIONAL_RE.finditer(html):
            published = urljoin(base_url, match.group(1))
            if published not in candidates:
                candidates.append(published)
        # Older ProspectPortal pages publish an opaque property-id route which
        # redirects to the canonical modern grid.  Harvest only an exact
        # page-published route; never synthesize the property id or slug.
        for match in _LEGACY_CONVENTIONAL_ATTR_RE.finditer(html):
            published = urljoin(base_url, match.group(1))
            if published not in candidates:
                candidates.append(published)
        # A second legacy form publishes the exact property-info root; Entrata
        # redirects that public route to the canonical conventional grid.  As
        # above, harvest only the page-provided numeric route.
        for match in _LEGACY_PROPERTY_ROOT_ATTR_RE.finditer(html):
            raw_published = match.group(1)
            published = (
                f"https:{raw_published}"
                if raw_published.startswith("//")
                else urljoin(base_url, raw_published)
            )
            if published not in candidates:
                candidates.append(published)
    if base_url not in candidates:
        candidates.append(base_url)

    matches: list[str] = []
    for candidate in candidates:
        try:
            parsed = urlsplit(candidate)
            explicit_port = parsed.port
        except (TypeError, ValueError):
            continue
        host = (parsed.hostname or "").casefold().rstrip(".")
        modern_path = bool(_CONVENTIONAL_PATH_RE.fullmatch(parsed.path))
        legacy_path = bool(_LEGACY_CONVENTIONAL_PATH_RE.fullmatch(parsed.path))
        legacy_root_path = bool(_LEGACY_PROPERTY_ROOT_PATH_RE.fullmatch(parsed.path))
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or explicit_port is not None
            or parsed.query
            or parsed.fragment
            or not (modern_path or legacy_path or legacy_root_path)
        ):
            continue
        if legacy_path:
            try:
                base_host = (urlsplit(base_url).hostname or "").casefold().rstrip(".")
            except (TypeError, ValueError):
                continue
            if not base_host or host != base_host:
                continue
        if legacy_root_path:
            try:
                base_host = (urlsplit(base_url).hostname or "").casefold().rstrip(".")
            except (TypeError, ValueError):
                continue
            if not base_host or (host != base_host and not host.endswith("prospectportal.com")):
                continue
        identity_source = f"{host} {parsed.path}"
        identity_tokens = set(_name_tokens(identity_source))
        # Vanity domains commonly concatenate the full property name (for
        # example ``springridgeonfletcher.com``), while the canonical path
        # shortens it to ``spring-ridge-apartments``.  Permit distinctive
        # 4+-character tokens inside that compact host/path representation;
        # short tokens still require an exact boundary match.
        compact_identity = re.sub(r"[^a-z0-9]+", "", identity_source.casefold())
        if not all(
            token in identity_tokens or (len(token) >= 4 and token in compact_identity) for token in wanted
        ):
            continue
        normalized = candidate if candidate.endswith("/") else candidate + "/"
        if normalized not in matches:
            matches.append(normalized)

    return matches[0] if len(matches) == 1 else ""


def _body_from_ctx(ctx: Any) -> str:
    fetch_result = getattr(ctx, "fetch_result", None)
    body = getattr(fetch_result, "body", None)
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return body if isinstance(body, str) else ""


def _source_url_from_ctx(ctx: Any) -> str:
    fetch_result = getattr(ctx, "fetch_result", None)
    return str(getattr(fetch_result, "final_url", "") or getattr(ctx, "base_url", "") or "").strip()


def _positive_numeric_rent(row: dict[str, Any]) -> bool:
    for key in _RENT_FIELDS:
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(float(value)) and float(value) > 0:
            return True
    return False


def _validated_units(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one coherent, priced Entrata apartment roster.

    Eight properties in the Aug-02 stratified canary mixed the legacy
    per-plan cards with Entrata's modern embedded ``unitsData`` roster.  The
    two families described the same apartments but one carried a building
    label and the other did not, so the old ``(building, unit_number)`` key
    retained both.  Across all eight live outputs the two unit-number sets
    were subset-comparable.  Select the strict superset; on equality prefer
    the property-scoped per-plan family, which carries richer building/native
    metadata.  Non-comparable families remain a union and therefore fail
    conservatively rather than silently dropping distinct inventory.
    """
    from ma_poc.core.identity import unit_has_real_anchor

    eligible = [
        row
        for row in (rows or [])
        if isinstance(row, dict)
        and unit_has_real_anchor(row)
        and _positive_numeric_rent(row)
        and str(row.get("unit_number") or "").strip()
    ]
    modern = [
        row
        for row in eligible
        if str(row.get("extraction_tier") or "").upper()
        == "TIER_1_DOM_ENTRATA_MODERN"
    ]
    scoped = [
        row
        for row in eligible
        if str(row.get("extraction_tier") or "").upper()
        != "TIER_1_DOM_ENTRATA_MODERN"
    ]
    if modern and scoped:
        modern_numbers = {
            str(row.get("unit_number") or "").strip().casefold()
            for row in modern
        }
        scoped_numbers = {
            str(row.get("unit_number") or "").strip().casefold()
            for row in scoped
        }
        if modern_numbers > scoped_numbers:
            eligible = modern
        elif scoped_numbers >= modern_numbers:
            # Equal sets intentionally land here: per-plan rows retain
            # building/native identity that the blank-building modern copy
            # omits. A strict scoped superset also wins naturally.
            eligible = scoped

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in eligible:
        building = str(row.get("building") or "").strip().casefold()
        unit_number = str(row.get("unit_number") or "").strip().casefold()
        key = (building, unit_number)
        if not unit_number or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _source_evidence(
    *,
    rows: list[dict[str, Any]],
    url: str,
    body: str,
    via: str,
    property_id: str,
    response_kind: str = "unit_roster",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Stamp rows and build an immutable-body archive/provenance pair."""
    from ma_poc.pms.source_provenance import (
        build_unit_source_provenance,
        response_sha256,
    )

    digest = response_sha256(body)
    for index, row in enumerate(rows):
        row.setdefault("source_response_sha256", digest)
        row.setdefault("source_response_url", url)
        row.setdefault("source_record_locator", f"entrata-row:{index}")
    identity = {
        "status": "MATCH",
        "configured_property_id": str(property_id or ""),
        "boundary": "property_scoped_entrata_route",
    }
    response = {
        "url": url,
        "status": 200,
        "body": body,
        "response_sha256": digest,
        "response_kind": response_kind,
        "via": via,
        "identity": identity,
    }
    provenance = build_unit_source_provenance(
        provider="entrata",
        source_url=url,
        body=body,
        unit_count=len(rows),
        identity=identity,
        response_kind=response_kind,
        status=200,
    )
    return response, provenance


def _selected_source_evidence(
    units: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    provenance: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep only response bodies referenced by the final coherent roster."""
    selected_hashes = {
        str(row.get("source_response_sha256") or "")
        for row in units
        if str(row.get("source_response_sha256") or "")
    }
    if not selected_hashes:
        return [], []
    return (
        [
            item
            for item in responses
            if str(item.get("response_sha256") or "") in selected_hashes
            or any(
                str(p.get("response_sha256") or "") in selected_hashes
                and str(p.get("source_url") or "") == str(item.get("url") or "")
                for p in provenance
            )
        ],
        [
            item
            for item in provenance
            if str(item.get("response_sha256") or "") in selected_hashes
        ],
    )


def _same_origin_relative(candidate: str, origin_url: str) -> str:
    """Return a same-origin path/query for in-page fetch, else ``""``."""
    try:
        parsed = urlsplit(candidate)
        origin = urlsplit(origin_url)
    except (TypeError, ValueError):
        return ""
    if (
        parsed.scheme.casefold() != origin.scheme.casefold()
        or (parsed.hostname or "").casefold() != (origin.hostname or "").casefold()
        or parsed.port != origin.port
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _url_matches_property_name(candidate: str, property_name: str) -> bool:
    """Require every distinctive property token in a seed-page detail URL.

    A conventional grid is already identity-scoped by
    :func:`strict_conventional_url`.  Links copied from the broader seed page
    need this additional boundary so a PMC portfolio page cannot contribute a
    sibling property's plan URL merely because it uses the same host.
    """
    wanted = _name_tokens(property_name)
    if not wanted:
        return False
    try:
        parsed = urlsplit(candidate)
    except (TypeError, ValueError):
        return False
    observed = set(_name_tokens(f"{parsed.hostname or ''} {parsed.path}"))
    return all(token in observed for token in wanted)


def _looks_like_entrata_inventory_grid(html: str) -> bool:
    """Recognize an inventory-bearing Entrata response, not a false-200 shell.

    Current Cloudflare-fronted vanity sites can return a small HTTP-200
    interstitial on the first navigation in a clean browser session and the
    real grid on the second navigation in that *same* session.  These markers
    are all exact surfaces consumed by the parsers below; a generic marketing
    page or challenge shell does not satisfy the gate.
    """
    low = (html or "").casefold()
    return any(
        marker in low
        for marker in (
            "fp-group-item",
            "fp-card",
            "unit-item",
            "view_unit_spaces",
            "unitsdata",
            "jd-fp-unit-card",
        )
    )


async def recover_entrata_hb_conventional(
    ctx: Any,
    *,
    session_factory: Any = None,
) -> EntrataHbRecovery:
    """Recover priced apartments using one clean Hyperbrowser session.

    A miss is never raised.  ``complete=True`` means a recognized plan grid
    was loaded and every bounded per-plan link returned a usable HTTP 200;
    callers may then retain the plan catalogue without repeating the same
    blocked static drill.
    """
    seed_html = _body_from_ctx(ctx)
    base_url = _source_url_from_ctx(ctx)
    property_name = str(getattr(ctx, "property_name", "") or "").strip()
    conventional_url = strict_conventional_url(
        seed_html,
        base_url,
        property_name,
    )
    snippet_target = None if conventional_url else _property_owned_snippet_target(ctx)
    index_url = conventional_url or (snippet_target.url if snippet_target is not None else "")
    if not index_url:
        return EntrataHbRecovery()

    if session_factory is None:
        from ma_poc.config.feature_flags import hb_enabled

        if not hb_enabled():
            return EntrataHbRecovery()

    from ma_poc.fetch.hyperbrowser_backend import (
        _NAV_TIMEOUT_MS,
        _hb_try_reserve_property,
        _HbSession,
    )

    property_id = str(getattr(ctx, "property_id", "") or "")
    if not _hb_try_reserve_property(
        property_id,
        priority=True,
        reason="property_bound_entrata_route",
    ):
        return EntrataHbRecovery()

    session = (session_factory or (lambda: _HbSession(mode="render")))()
    attempted = True
    try:
        from ma_poc.pms.adapters.entrata import (
            _extract_vus_urls,
            find_entrata_pp_plan_links,
            parse_entrata_modern_units_data,
            parse_entrata_pp_jd_fp_cards,
            parse_entrata_pp_unit_cards,
            parse_entrata_prospectportal_html,
            parse_prospectportal_unit_spaces,
        )

        # Exact property-matched detail routes already present on the seed can
        # be replayed even if the conventional index is a sparse shell.  Do
        # not spend the retry in that case.  Otherwise, retry one navigation
        # inside the existing session when the first 200 lacks every Entrata
        # inventory marker.  Live 2026-07-31 validation: Fenestra, Hanover
        # Montrose, and Echelon all returned a ~27 KB false-200 shell first and
        # their 200-330 KB real grids on attempt two.
        seed_links = (
            []
            if snippet_target is not None
            else [
                link
                for link in find_entrata_pp_plan_links(seed_html, base_url)
                if _url_matches_property_name(link, property_name)
            ]
        )
        page = await session.open()
        index_html = ""
        for _attempt in range(_MAX_INDEX_NAV_ATTEMPTS):
            await page.goto(
                index_url,
                wait_until="domcontentloaded",
                timeout=_NAV_TIMEOUT_MS,
            )
            if _HB_SETTLE_SECONDS > 0:
                await asyncio.sleep(_HB_SETTLE_SECONDS)
            index_html = await page.content()
            if seed_links or _looks_like_entrata_inventory_grid(index_html):
                break

        if not isinstance(index_html, str) or not index_html:
            return EntrataHbRecovery(
                attempted=attempted,
                failure_reason="EMPTY_INDEX_BODY",
            )
        if len(index_html.encode("utf-8", errors="replace")) > _MAX_INDEX_BODY_BYTES:
            return EntrataHbRecovery(
                attempted=attempted,
                failure_reason="INDEX_BODY_OVERSIZED",
            )

        title = ""
        try:
            title = str(await page.title() or "")
        except Exception:
            title = ""
        if not seed_links and not _looks_like_entrata_inventory_grid(index_html):
            challenge_text = f"{title}\n{index_html[:8000]}".casefold()
            if any(
                marker in challenge_text
                for marker in (
                    "just a moment",
                    "verify you are human",
                    "checking your browser",
                    "cf-chl-",
                )
            ):
                return EntrataHbRecovery(
                    attempted=attempted,
                    failure_reason="CHALLENGE_UNSOLVED",
                )

        final_url = str(getattr(page, "url", "") or index_url)
        if not _same_origin_relative(final_url, index_url):
            return EntrataHbRecovery(
                attempted=attempted,
                failure_reason="CROSS_ORIGIN_REDIRECT_REJECTED",
            )
        if snippet_target is not None and (
            (urlsplit(final_url).hostname or "").casefold().rstrip(".") != snippet_target.host
            or not _snippet_provider_identity_match(
                index_html,
                snippet_target,
            )
        ):
            return EntrataHbRecovery(
                attempted=attempted,
                failure_reason="SNIPPET_PROVIDER_IDENTITY_REJECTED",
            )

        plan_rows = parse_entrata_prospectportal_html(index_html, final_url)
        parsed_units: list[dict[str, Any]] = []
        for parser in (
            parse_entrata_pp_unit_cards,
            parse_entrata_pp_jd_fp_cards,
            parse_entrata_modern_units_data,
        ):
            parsed_units.extend(parser(index_html, final_url))
        evidence_responses: list[dict[str, Any]] = []
        evidence_provenance: list[dict[str, Any]] = []
        index_evidence_rows = parsed_units if parsed_units else plan_rows
        if index_evidence_rows:
            _response, _provenance = _source_evidence(
                rows=index_evidence_rows,
                url=final_url,
                body=index_html,
                via="entrata_pp_hyperbrowser_index",
                property_id=property_id,
                response_kind=("unit_roster" if parsed_units else "floor_plan_catalog"),
            )
            evidence_responses.append(_response)
            evidence_provenance.append(_provenance)

        raw_links = find_entrata_pp_plan_links(index_html, final_url)
        # Some current Entrata themes publish exact per-plan links on the
        # marketing seed page but return a blank/blocked conventional grid.
        # Reuse those already-observed links in this same HB session, subject
        # to the same-origin check below and a property-name identity match.
        # Belle Meade is the live 2026-07-31 example: direct detail HTTP 403,
        # clean HB detail 200 with apartment 506, while /conventional/ exposes
        # no parseable plan links at all.
        plan_links: list[tuple[str, str, str]] = []
        if snippet_target is not None:
            snippet_links, snippet_links_valid = _snippet_plan_links(
                index_html,
                final_url,
                snippet_target,
            )
            if not snippet_links_valid:
                return EntrataHbRecovery(
                    attempted=attempted,
                    plan_rows=plan_rows,
                    winning_url=final_url,
                    failure_reason="SNIPPET_DETAIL_BOUNDARY_REJECTED",
                )
            plan_links.extend(snippet_links)
        else:
            for link in [*raw_links, *seed_links]:
                relative = _same_origin_relative(link, final_url)
                candidate = (link, relative, "")
                if relative and candidate not in plan_links:
                    plan_links.append(candidate)
                if len(plan_links) >= _MAX_PLAN_URLS:
                    break

        # The grid's active-plan buttons carry exact ``view_unit_spaces`` XHR
        # URLs.  They require the browser's cookies, same-origin Referer and
        # X-Requested-With header, so ordinary direct replay can 403/400 even
        # though the already-open grid can fetch them.  Replaying them inside
        # this existing HB session is the missing unit-level step for the
        # older ProspectPortal template; it creates no additional session.
        vus_links: list[tuple[str, str]] = []
        vus_sources = [(final_url, index_html)]
        if seed_html:
            vus_sources.append((base_url, seed_html))
        for _, link in _extract_vus_urls(vus_sources, final_url):
            relative = _same_origin_relative(link, final_url)
            if relative and (link, relative) not in vus_links:
                vus_links.append((link, relative))
            if len(vus_links) >= _MAX_PLAN_URLS:
                break

        vus_fetches_ok = bool(vus_links)
        vus_rows: list[dict[str, Any]] = []
        for link, relative in vus_links:
            try:
                response = await page.evaluate(_FETCH_XHR_JS, relative)
            except Exception:
                response = None
            if not isinstance(response, dict):
                vus_fetches_ok = False
                continue
            try:
                status = int(response.get("status") or 0)
            except (TypeError, ValueError):
                status = 0
            detail_html = response.get("body")
            if (
                status != 200
                or response.get("oversized") is True
                or not isinstance(detail_html, str)
                or not detail_html
            ):
                vus_fetches_ok = False
                continue
            _parsed_vus = parse_prospectportal_unit_spaces(detail_html, link)
            if _parsed_vus:
                _response, _provenance = _source_evidence(
                    rows=_parsed_vus,
                    url=link,
                    body=detail_html,
                    via="entrata_pp_hyperbrowser_view_unit_spaces",
                    property_id=property_id,
                )
                evidence_responses.append(_response)
                evidence_provenance.append(_provenance)
                vus_rows.extend(_parsed_vus)

        validated_vus = _validated_units(vus_rows)
        if validated_vus:
            selected_responses, selected_provenance = _selected_source_evidence(
                validated_vus,
                evidence_responses,
                evidence_provenance,
            )
            return EntrataHbRecovery(
                attempted=attempted,
                complete=bool(plan_rows and vus_fetches_ok),
                units=validated_vus,
                plan_rows=plan_rows,
                html_responses=selected_responses,
                unit_source_provenance=selected_provenance,
                winning_url=final_url,
            )

        all_detail_fetches_ok = bool(plan_links)
        for link, relative, plan_name in plan_links:
            try:
                response = await page.evaluate(_FETCH_PLAN_JS, relative)
            except Exception:
                response = None
            if not isinstance(response, dict):
                all_detail_fetches_ok = False
                continue
            try:
                status = int(response.get("status") or 0)
            except (TypeError, ValueError):
                status = 0
            detail_html = response.get("body")
            if (
                status != 200
                or response.get("oversized") is True
                or not isinstance(detail_html, str)
                or not detail_html
            ):
                all_detail_fetches_ok = False
                continue
            if snippet_target is not None and not _snippet_provider_identity_match(
                detail_html,
                snippet_target,
            ):
                return EntrataHbRecovery(
                    attempted=attempted,
                    plan_rows=plan_rows,
                    winning_url=final_url,
                    failure_reason="SNIPPET_DETAIL_IDENTITY_REJECTED",
                )
            detail_rows = parse_entrata_pp_unit_cards(
                detail_html,
                link,
                plan_name,
            )
            detail_rows.extend(parse_entrata_pp_jd_fp_cards(detail_html, link))
            detail_rows.extend(parse_entrata_modern_units_data(detail_html, link))
            if detail_rows:
                _response, _provenance = _source_evidence(
                    rows=detail_rows,
                    url=link,
                    body=detail_html,
                    via="entrata_pp_hyperbrowser_plan_detail",
                    property_id=property_id,
                )
                evidence_responses.append(_response)
                evidence_provenance.append(_provenance)
            if snippet_target is not None:
                for row in detail_rows:
                    row["source_property_id"] = snippet_target.property_id
                    row["source_property_name"] = property_name
                    row["source_property_provenance"] = "exact_operator_published_entrata_website_snippet"
                    row["source_portal_url"] = final_url
            parsed_units.extend(detail_rows)

        units = _validated_units(parsed_units)
        if snippet_target is not None and units:
            native_ids = [
                str((row.get("source_ids") or {}).get("entrata_uid") or "").strip().casefold()
                for row in units
            ]
            if any(not value for value in native_ids) or len(native_ids) != len(set(native_ids)):
                return EntrataHbRecovery(
                    attempted=attempted,
                    plan_rows=plan_rows,
                    winning_url=final_url,
                    failure_reason="SNIPPET_NATIVE_ID_COLLISION_REJECTED",
                )
        # A grid advertising active view_unit_spaces URLs but returning no
        # parsed rows is not authoritative plan-only evidence.  Keep recovery
        # incomplete so later tiers can investigate rather than certifying a
        # parser miss as a true no-inventory ceiling.
        complete = bool(plan_rows and plan_links and all_detail_fetches_ok and (units or not vus_links))
        selected_responses, selected_provenance = _selected_source_evidence(
            units,
            evidence_responses,
            evidence_provenance,
        )
        if not units and plan_rows:
            # Plan-only is still a successful extraction surface. Preserve the
            # exact index body that produced the catalogue for offline replay.
            selected_responses = evidence_responses[:1]
            selected_provenance = evidence_provenance[:1]
        return EntrataHbRecovery(
            attempted=attempted,
            complete=complete,
            units=units,
            plan_rows=plan_rows,
            html_responses=selected_responses,
            unit_source_provenance=selected_provenance,
            winning_url=final_url,
            failure_reason="" if units else "NO_NATIVE_UNIT_ROSTER",
        )
    except Exception as exc:
        return EntrataHbRecovery(
            attempted=attempted,
            failure_reason=f"{type(exc).__name__}",
        )
    finally:
        try:
            await session.close()
        except Exception:
            pass
