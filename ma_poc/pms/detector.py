"""
Offline PMS detector.

Research log
------------
Web sources consulted:
  - https://www.realpage.com/products/onesite (verified: onlineleasing.realpage.com is the hosted-portal subdomain pattern; URL segment "#k=" is the apartment-key fragment, accessed 2026-04-17)
  - https://www.yardi.com/products/rentcafe/ (verified: RentCafe is Yardi's marketing portal; legacy vanity sites backed by RentCafe historically use .aspx paths, accessed 2026-04-17)
  - https://www.entrata.com/resident-experience (verified: Entrata property sites expose /Apartments/module/ and ``commoncf.entrata.com`` widget endpoints, accessed 2026-04-17)
  - https://www.appfolio.com/property-manager/online-leasing (verified: listing URLs use ``{slug}.appfolio.com/listings/...``, accessed 2026-04-17)
  - https://www.avaloncommunities.com (single-REIT custom stack; confirmed from raw captures)
Real payloads inspected (under data/runs/2026-04-15/raw_api/):
  - 254976 (San Artes, Mark-Taylor mgmt) — vanity domain, no direct PMS host captured; supports mgmt-prior fallback
  - 12060, 238166, 239499, 256856, 260116, 26617, 268836, 282036, 283726, 35593, 93977 — sightmap.com captures observed
  - 238997 — avaloncommunities.com captures
  - 293707 — api.ws.realpage.com captures (RealPage OLL API endpoint)
OneSite URL samples (from property_reports/):
  - https://9216254.onlineleasing.realpage.com/
  - https://8756399.onlineleasing.realpage.com/#k=44781
Key findings:
  - The property CSV contains only vanity domains — no direct PMS URLs. Offline
    detection from URL alone is therefore weak for most rows; mgmt-company
    priors and HTML markers are load-bearing.
  - OneSite numeric subdomain is a strong fingerprint where present; 3+ distinct
    IDs (8756399, 9216254, and the generic handoff-doc example) validate the
    pattern ``^(?P<id>\\d{3,9})\\.onlineleasing\\.realpage\\.com``.
  - ``commoncf.entrata.com`` is an Entrata widget-CDN host — its presence in
    intercepted API bodies is a strong Entrata signal, but that's a Phase 3
    (adapter) concern, not offline detection.
  - RentCafe vanity URLs historically expose ``.aspx`` paths. We treat this as
    a 0.70-confidence heuristic, not a definitive match.
"""
from __future__ import annotations

import re
import typing as t
import urllib.parse
from dataclasses import dataclass, field
from typing import Literal

PmsName = Literal[
    "rentcafe",
    "entrata",
    "appfolio",
    "onesite",
    "sightmap",
    "realpage_oll",
    "avalonbay",
    "squarespace_nopms",
    "wix_nopms",
    "custom",
    "unknown",
]

Strategy = Literal[
    "api_first",
    "jsonld_first",
    "dom_first",
    "portal_hop",
    "syndication_only",
    "cascade",
]

_STRATEGY_BY_PMS: dict[str, Strategy] = {
    "rentcafe": "jsonld_first",
    "entrata": "api_first",
    "appfolio": "api_first",
    "onesite": "api_first",
    "sightmap": "api_first",
    "realpage_oll": "portal_hop",
    "avalonbay": "api_first",
    "squarespace_nopms": "syndication_only",
    "wix_nopms": "syndication_only",
    "custom": "cascade",
    "unknown": "cascade",
}

# Management-company → typical PMS priors. Lowercase keys; matched with strip.
# Sources: CLAUDE.md + claude_refactor.md handoff notes. Each entry's rationale
# is in the trailing comment so future maintainers can see provenance.
MGMT_TO_PMS_PRIOR: dict[str, PmsName] = {
    "mark-taylor": "entrata",              # Handoff: Mark-Taylor is an Entrata-only shop
    "mark taylor": "entrata",              # Same, alt spelling
    "lindsey management": "rentcafe",      # Handoff: Lindsey is Yardi/RentCafe
    "avalonbay communities": "avalonbay",  # Direct — AvalonBay properties use the REIT's custom stack
}

# Host-suffix patterns that are definitive. First match wins.
_HOST_FINGERPRINTS: list[tuple[re.Pattern[str], PmsName, float, str]] = [
    (re.compile(r"^\d{3,9}\.onlineleasing\.realpage\.com$"), "onesite", 0.95, "host matches OneSite numeric-prefix pattern"),
    (re.compile(r"(?:^|\.)rentcafe\.com$"), "rentcafe", 0.95, "host ends in rentcafe.com"),
    (re.compile(r"(?:^|\.)sightmap\.com$"), "sightmap", 0.95, "host ends in sightmap.com"),
    (re.compile(r"(?:^|\.)avaloncommunities\.com$"), "avalonbay", 0.95, "host ends in avaloncommunities.com"),
    (re.compile(r"(?:^|\.)entrata\.com$"), "entrata", 0.95, "host ends in entrata.com"),
    (re.compile(r"(?:^|\.)appfolio\.com$"), "appfolio", 0.95, "host ends in appfolio.com"),
    # RealPage portals that are NOT the OneSite OLL subdomain shape — e.g.
    # portal.realpage.com, api.ws.realpage.com. Lower confidence because the
    # RealPage domain covers multiple products.
    (re.compile(r"(?:^|\.)realpage\.com$"), "realpage_oll", 0.80, "host ends in realpage.com (non-OneSite RealPage product)"),
]

_ONESITE_CLIENT_ID_RE = re.compile(r"^(?P<id>\d{3,9})\.onlineleasing\.realpage\.com$")
_APPFOLIO_CLIENT_ID_RE = re.compile(r"^(?P<id>[a-z0-9-]+)\.appfolio\.com$")
_RENTCAFE_CLIENT_ID_RE = re.compile(r"^(?P<id>[a-z0-9-]+)\.rentcafe\.com$")
# Entrata property IDs are embedded in the URL path (handoff: entrata.py:1480)
_ENTRATA_PATH_ID_RE = re.compile(r"/(?P<id>\d{3,8})(?:/|$)")


@dataclass
class DetectedPMS:
    pms: PmsName
    confidence: float
    evidence: list[str] = field(default_factory=list)
    pms_client_account_id: str | None = None
    recommended_strategy: Strategy = "cascade"


def _empty_result(reason: str = "no signal") -> DetectedPMS:
    return DetectedPMS(
        pms="unknown",
        confidence=0.0,
        evidence=[reason],
        pms_client_account_id=None,
        recommended_strategy=_STRATEGY_BY_PMS["unknown"],
    )


def _parse_host(url: str) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except (ValueError, TypeError):
        return None
    host = (parsed.hostname or "").lower()
    return host or None


_KNOWN_LITERALS: frozenset[str] = frozenset({
    "rentcafe", "entrata", "appfolio", "onesite", "sightmap",
    "realpage_oll", "avalonbay", "squarespace_nopms", "wix_nopms",
})


def _lookup_csv_pms_override(csv_row: dict[str, object] | None) -> tuple[PmsName, float, str] | None:
    """CSV may carry an explicit ``pms_platform`` hint. Values that match a
    known literal are trusted at high confidence; values that do not match a
    literal signal a custom platform we lack an adapter for.
    """
    if not csv_row:
        return None
    for key in ("pms_platform", "PMS Platform", "pms"):
        raw = csv_row.get(key)
        if not isinstance(raw, str):
            continue
        normalized = raw.strip().lower()
        if not normalized:
            continue
        if normalized in _KNOWN_LITERALS:
            return t.cast(PmsName, normalized), 0.95, f"csv.pms_platform={normalized!r}"
        return "custom", 0.75, f"csv.pms_platform={normalized!r} (no adapter — custom)"
    return None


def _lookup_mgmt_prior(csv_row: dict[str, object] | None) -> tuple[PmsName, str] | None:
    if not csv_row:
        return None
    for key in ("Management Company", "management_company", "Mgmt Company", "mgmt"):
        raw = csv_row.get(key)
        if not isinstance(raw, str):
            continue
        normalized = raw.strip().lower()
        if not normalized:
            continue
        pms = MGMT_TO_PMS_PRIOR.get(normalized)
        if pms is not None:
            return pms, f"mgmt-prior: {raw.strip()!r} -> {pms}"
    return None


def _client_id_for(pms: PmsName, host: str, path: str) -> str | None:
    if pms == "onesite":
        m = _ONESITE_CLIENT_ID_RE.match(host)
        return m.group("id") if m else None
    if pms == "appfolio":
        m = _APPFOLIO_CLIENT_ID_RE.match(host)
        # Reject generic subdomains that are not client slugs
        if m and m.group("id") not in {"www", "", "app"}:
            return m.group("id")
        return None
    if pms == "rentcafe":
        m = _RENTCAFE_CLIENT_ID_RE.match(host)
        if m and m.group("id") not in {"www", "cdngeneralcf", "resource", "cdn"}:
            return m.group("id")
        return None
    if pms == "entrata":
        m = _ENTRATA_PATH_ID_RE.search(path or "")
        return m.group("id") if m else None
    return None


def _detect_host(url: str) -> tuple[PmsName, float, list[str], str | None] | None:
    host = _parse_host(url)
    if not host:
        return None
    try:
        path = urllib.parse.urlparse(url).path or ""
    except (ValueError, TypeError):
        path = ""
    for pattern, pms, confidence, reason in _HOST_FINGERPRINTS:
        if pattern.search(host):
            return pms, confidence, [f"{reason} ({host})"], _client_id_for(pms, host, path)
    return None


def _detect_url_extension(url: str) -> tuple[PmsName, float, list[str]] | None:
    """``.aspx`` paths on non-Microsoft vanity domains signal RentCafe/Yardi."""
    try:
        parsed = urllib.parse.urlparse(url)
    except (ValueError, TypeError):
        return None
    path = (parsed.path or "").lower()
    host = (parsed.hostname or "").lower()
    if not path.endswith(".aspx"):
        return None
    if host.endswith("microsoft.com") or host.endswith("live.com") or host.endswith("sharepoint.com"):
        return None
    return "rentcafe", 0.70, [f".aspx path on vanity host ({host}) — RentCafe/Yardi heuristic"]


def _detect_html_markers(page_html: str) -> tuple[PmsName, float, list[str]] | None:
    h = page_html.lower()
    # Platform giveaway scripts first — these are strong "not-a-PMS" signals.
    if "static.parastorage.com" in h or "wix.com" in h:
        return "wix_nopms", 0.85, ["Wix script/platform marker in HTML"]
    if "squarespace.com" in h:
        return "squarespace_nopms", 0.85, ["Squarespace script/platform marker in HTML"]
    # PMS-specific markers. Checked after the no-PMS platforms so that a
    # Squarespace site with a linked "rentcafe.com" image asset doesn't
    # misfire as RentCafe.
    if "entrata.com" in h or "/apartments/module/" in h or "entrata-widget" in h:
        return "entrata", 0.85, ["Entrata marker in HTML (entrata.com / /Apartments/module/ / entrata-widget)"]
    if "rentcafe" in h or "yardi" in h:
        return "rentcafe", 0.80, ["RentCafe/Yardi marker in HTML"]
    if "onlineleasing.realpage.com" in h:
        return "onesite", 0.85, ["OneSite marker in HTML (onlineleasing.realpage.com)"]
    if "sightmap.com" in h:
        return "sightmap", 0.80, ["SightMap iframe/script marker in HTML"]
    if ".appfolio.com" in h:
        return "appfolio", 0.80, ["AppFolio marker in HTML"]
    return None


def detect_pms(
    url: str,
    csv_row: dict[str, object] | None = None,
    page_html: str | None = None,
) -> DetectedPMS:
    """Detect the PMS backing a property URL using offline signals only.

    Signal priority (first confident hit wins):
      1. URL host fingerprints
      2. URL extension heuristic (``.aspx``)
      3. HTML platform-giveaway scripts + PMS-specific markers
      4. CSV management-company priors

    Never raises; bad inputs return ``DetectedPMS(pms="unknown", confidence=0.0)``.
    """
    try:
        return _detect_pms_impl(url, csv_row, page_html)
    except Exception as exc:  # defensive: detector must never crash the pipeline
        return DetectedPMS(
            pms="unknown",
            confidence=0.0,
            evidence=[f"detector-internal-error: {type(exc).__name__}"],
            recommended_strategy=_STRATEGY_BY_PMS["unknown"],
        )


def _detect_pms_impl(
    url: str,
    csv_row: dict[str, object] | None,
    page_html: str | None,
) -> DetectedPMS:
    evidence: list[str] = []

    # 0. Explicit CSV override (highest-trust signal — the human told us)
    override = _lookup_csv_pms_override(csv_row)
    if override is not None:
        pms, conf, reason = override
        return DetectedPMS(
            pms=pms,
            confidence=conf,
            evidence=[reason],
            recommended_strategy=_STRATEGY_BY_PMS[pms],
        )

    host_hit = _detect_host(url) if isinstance(url, str) else None
    if host_hit is not None:
        pms, confidence, host_evidence, client_id = host_hit
        return DetectedPMS(
            pms=pms,
            confidence=confidence,
            evidence=host_evidence,
            pms_client_account_id=client_id,
            recommended_strategy=_STRATEGY_BY_PMS[pms],
        )

    # 2. URL extension heuristic
    ext_hit = _detect_url_extension(url) if isinstance(url, str) else None

    # 3. HTML markers (optional input)
    html_hit = _detect_html_markers(page_html) if isinstance(page_html, str) and page_html else None

    # 4. CSV management-company prior
    mgmt_hit = _lookup_mgmt_prior(csv_row)

    # Rank candidates by confidence; combine evidence from all matching signals.
    candidates: list[tuple[PmsName, float, list[str]]] = []
    if html_hit is not None:
        candidates.append(html_hit)
    if ext_hit is not None:
        candidates.append(ext_hit)
    if mgmt_hit is not None:
        pms, reason = mgmt_hit
        candidates.append((pms, 0.70, [reason]))

    if not candidates:
        return _empty_result("no URL/HTML/mgmt signal")

    # Consensus boost: if two independent signals agree, bump confidence.
    pms_votes: dict[PmsName, float] = {}
    pms_evidence: dict[PmsName, list[str]] = {}
    for pms, conf, reasons in candidates:
        pms_votes[pms] = pms_votes.get(pms, 0.0) + conf
        pms_evidence.setdefault(pms, []).extend(reasons)

    best_pms = max(pms_votes, key=lambda k: pms_votes[k])
    # Base confidence is the max single-signal confidence for the winning PMS;
    # two agreeing signals bump toward (but not past) 0.95.
    agreeing = [c for (p, c, _) in candidates if p == best_pms]
    base = max(agreeing)
    combined = min(0.95, base + 0.10 * (len(agreeing) - 1))
    evidence = pms_evidence[best_pms]

    return DetectedPMS(
        pms=best_pms,
        confidence=combined,
        evidence=evidence,
        pms_client_account_id=None,
        recommended_strategy=_STRATEGY_BY_PMS[best_pms],
    )
