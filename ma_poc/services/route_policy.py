"""Agentic router — Phase 1: deterministic signals + policy (no LLM, not yet wired).

See ``investigations/2026-07-24-agentic-router/ROUTER_SPEC.md``. This module is the
foundation: it collects the scattered fetch/extraction signals into one struct
(``compute_signals``) and applies a deterministic routing policy (``route``) that
returns a ``source_planner.Decision`` from a CLOSED action menu.

Design invariants (hard rules from the spec):
  * The router NEVER decides success — gold stays an objective gate elsewhere. The
    router only picks *what to try next* (or MARK_VERIFIED_EMPTY / MARK_DEAD_URL,
    which are routing outcomes, not gold claims).
  * Phase 1 is deterministic-only. ``route`` returns ``INVOKE_AGENT`` where the
    deterministic layer is unsure — Phase 2 wires the actual agent behind that.
  * Additive: nothing imports/calls this in the hot path yet. It is meant to run
    in SHADOW mode first (compute + log the decision, compare to what the pipeline
    actually did) before it is allowed to act.

Everything here is defensive — ``compute_signals`` never raises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ma_poc.services.source_planner import Decision

# ── closed action menu (extends source_planner's) ────────────────────────────
# Existing planner actions (reused): STOP, ESCALATE_LINK_HOP, ESCALATE_LLM_TARGETED,
# ESCALATE_LLM_MONOLITHIC, ACCEPT_PARTIAL. Router adds:
ROUTE_ADAPTER = "ROUTE_ADAPTER"          # target_field_group carries the adapter name
TRY_DIRECT_GET = "TRY_DIRECT_GET"        # target_field_group carries the pms name
ESCALATE_RENDER = "ESCALATE_RENDER"
ESCALATE_HB_SHELL = "ESCALATE_HB_SHELL"
MARK_VERIFIED_EMPTY = "MARK_VERIFIED_EMPTY"
MARK_DEAD_URL = "MARK_DEAD_URL"
INVOKE_AGENT = "INVOKE_AGENT"
STOP = "STOP"

# host fingerprints → the pms the body points at (drives TRY_DIRECT_GET / ROUTE_ADAPTER)
_FINGERPRINTS: dict[str, str] = {
    "doorway-api.knockrentals.com": "knock",
    "doorway.knck.io": "knock",
    "knockdoorway": "knock",
    "sightmap.com": "sightmap",
    "securecafe.com": "rentcafe",
    "rentcafe.com": "rentcafe",
    "rpfp_config": "realpage",
    "api.ws.realpage.com": "realpage",
    "entrata.com": "entrata",
    "appfolio.com": "appfolio",
    "on-site.com": "onsite",
}

_RENT_RE = re.compile(r"\$\s?\d{3,}|\brent\b|\bprice\b|marketrent", re.IGNORECASE)
_UNIT_RE = re.compile(r"unit[_\s-]?number|availableunits|\bfloorplan|unitnumber", re.IGNORECASE)
_CF_SHELL_RE = re.compile(r"just a moment|checking your browser|verify you are human|cf-challenge", re.IGNORECASE)


@dataclass(frozen=True)
class RouteSignals:
    """One cheap, deterministic snapshot of a property's fetch + partial extract."""

    outcome: str = "UNKNOWN"
    status: int | None = None
    body_bytes: int = 0
    content_type: str = ""
    cf_shell: bool = False
    xhr_captured: int = 0
    has_rent_signal: bool = False
    has_unit_signal: bool = False
    pms_fingerprints: list[str] = field(default_factory=list)
    known_endpoint_match: bool = False
    maturity: str = "COLD"
    preferred_tier: str | None = None
    consecutive_failures: int = 0
    units_extracted: int = 0  # from the partial result, if any


def _body_text(fetch_result: Any) -> str:
    body = getattr(fetch_result, "body", None)
    if isinstance(body, bytes):
        try:
            return body.decode("utf-8", errors="replace")
        except Exception:
            return ""
    return body if isinstance(body, str) else ""


def _outcome_str(fetch_result: Any) -> str:
    o = getattr(fetch_result, "outcome", None)
    return getattr(o, "value", None) or (str(o) if o is not None else "UNKNOWN")


def compute_signals(
    fetch_result: Any,
    detected: Any = None,
    profile: Any = None,
    units_extracted: int = 0,
) -> RouteSignals:
    """Collect the routing signals. Never raises — missing inputs → conservative defaults."""
    body = _body_text(fetch_result)
    low = body[:200_000].lower()  # cap the scan

    fps = sorted({pms for token, pms in _FINGERPRINTS.items() if token in low})

    # captured XHR/API responses that look like data (json-ish)
    nlog = getattr(fetch_result, "network_log", None) or []
    xhr = 0
    nlog_urls: list[str] = []
    for e in nlog:
        try:
            ct = str(e.get("content_type") or "")
            nlog_urls.append(str(e.get("url") or ""))
            if "json" in ct.lower():
                xhr += 1
        except Exception:
            continue

    # does a profile known_endpoint's HOST appear in the body or captured XHR urls?
    known_match = False
    try:
        from urllib.parse import urlsplit

        eps = getattr(profile.api_hints, "known_endpoints", None) or []
        ep_hosts = []
        for ep in eps:
            u = (ep.get("url_pattern") or ep.get("url")) if isinstance(ep, dict) else (
                getattr(ep, "url_pattern", None) or getattr(ep, "url", None)
            )
            if u:
                host = urlsplit(u if "://" in u else "https://" + u).netloc
                if host:
                    ep_hosts.append(host.lower())
        joined = low + " " + " ".join(nlog_urls).lower()
        known_match = any(h in joined for h in ep_hosts if len(h) > 5)
    except Exception:
        known_match = False

    try:
        maturity = str(getattr(profile.confidence, "maturity", "COLD") or "COLD").upper()
        pref = getattr(profile.confidence, "preferred_tier", None)
        cf = int(getattr(profile.confidence, "consecutive_failures", 0) or 0)
    except Exception:
        maturity, pref, cf = "COLD", None, 0

    return RouteSignals(
        outcome=_outcome_str(fetch_result),
        status=getattr(fetch_result, "status", None),
        body_bytes=len(body),
        content_type=str(getattr(fetch_result, "content_type", "") or ""),
        cf_shell=bool(getattr(fetch_result, "captcha_detected", False)) or bool(_CF_SHELL_RE.search(body[:5000])),
        xhr_captured=xhr,
        has_rent_signal=bool(_RENT_RE.search(low)),
        has_unit_signal=bool(_UNIT_RE.search(low)),
        pms_fingerprints=fps,
        known_endpoint_match=known_match,
        maturity=maturity,
        preferred_tier=str(pref) if pref is not None else None,
        consecutive_failures=cf,
        units_extracted=int(units_extracted or 0),
    )


def route(signals: RouteSignals, profile: Any = None) -> Decision:
    """Deterministic routing policy over the signals. Returns a Decision from the
    closed menu; ``INVOKE_AGENT`` where the deterministic layer is genuinely unsure
    (Phase 2 wires the agent behind that). Never raises.

    Ordering mirrors ROUTER_SPEC §4: confident replay → strong rule → uncertain→agent.
    """
    # 0. We already have units — nothing to route, let the gold gate decide.
    if signals.units_extracted > 0:
        return Decision(action=STOP, rationale="units already extracted; gold gate decides")

    # 1. Dead URL — hand to re-discovery, don't burn escalations.
    if signals.outcome in ("DEAD_URL", "HARD_FAIL") and signals.status in (404, 410, 451):
        return Decision(action=MARK_DEAD_URL, rationale=f"dead url status={signals.status}")

    # 2. A stored endpoint appears in the body/XHR → replay the direct-GET for that PMS.
    if signals.known_endpoint_match and signals.pms_fingerprints:
        pms = signals.pms_fingerprints[0]
        return Decision(action=TRY_DIRECT_GET, target_field_group=pms,
                        rationale=f"known endpoint + {pms} fingerprint")

    # 3. CF-shell / blocked but we know the PMS → HB in-page fetch of the data page.
    if (signals.cf_shell or signals.outcome == "BOT_BLOCKED") and signals.pms_fingerprints:
        return Decision(action=ESCALATE_HB_SHELL, target_field_group=signals.pms_fingerprints[0],
                        rationale="CF/blocked with known PMS → HB in-page fetch")

    # 4. Reached OK, PMS fingerprint present, but static body has no units → SPA that
    #    needs a render to fire its XHR.
    if signals.outcome == "OK" and signals.pms_fingerprints and not signals.has_unit_signal:
        return Decision(action=ESCALATE_RENDER, target_field_group=signals.pms_fingerprints[0],
                        rationale="OK + PMS fingerprint, no static units → render SPA")

    # 5. Reached OK, real page, but genuinely no rent/unit signal anywhere → likely
    #    verified-empty (a SUCCESS, not a failure). Conservative: only when the body is
    #    substantial (a real page, not a shell) and no PMS/data hints exist.
    if (
        signals.outcome == "OK"
        and signals.body_bytes > 8_000
        and not signals.cf_shell
        and not signals.has_rent_signal
        and not signals.has_unit_signal
        and not signals.pms_fingerprints
    ):
        return Decision(action=MARK_VERIFIED_EMPTY, rationale="real page, no rent/unit/pms signal")

    # 6. Deterministic layer is unsure → hand to the agent (Phase 2). Includes: OK body
    #    with rent/unit signal but no PMS route, unknown fingerprints, misroute suspects.
    return Decision(action=INVOKE_AGENT,
                    rationale="no confident deterministic route (rent/unit signal without a known PMS route, or ambiguous)")
