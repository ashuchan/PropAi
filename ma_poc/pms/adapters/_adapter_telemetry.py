"""Shared per-adapter telemetry helpers.

2026-05-22: factored out of ``rentcafe.py`` so the same diagnostic shape
can be applied to every PMS adapter (G5, OneSite, Entrata, ProspectPortal,
etc.). Pre-2026-05-22 every PMS adapter returned silently on failure —
events.jsonl carried zero ``tier_attempted`` events from inside the
adapter, so every failure looked identical to "adapter never ran". The
2026-05-22 SecureCafe regex bug took 4+ hours to diagnose because of
this gap. New adapters should call into this module instead of
re-implementing.

Three building blocks:

* :func:`log_adapter_stage` — short stage-outcome event (``ok`` / ``skipped_*``
  / ``status_NNN`` / ``cf_challenge_shell`` / ``parse_returned_empty``).
* :func:`log_adapter_diag` — rich raw-signal capture (caption text,
  heading text, data-label inventory, first-row context) — fires ONLY on
  silent-empty failures so the event budget tracks the failure rate.
* :func:`classify_probe_body` — distinguishes CF challenge shells from
  genuine 200-OK empty bodies from real success.

All helpers wrap their emit() calls in try/except: telemetry must never
break the scrape.
"""

from __future__ import annotations

import os
import re
from typing import Any

# Cloudflare interstitial fingerprints — re-used by every adapter that
# fetches a CF-protected endpoint via probe_get / page.evaluate.
CF_CHALLENGE_MARKERS: tuple[str, ...] = (
    "challenge-platform",
    "cf-mitigated",
    "__cf_chl_",
    "cf-please-wait",
    "_cf_chl_opt",
    "/cdn-cgi/challenge-platform",
)


# ──────────────────────────────────────────────────────────────────────
# Stage events — emit one per recovery path attempted.
# ──────────────────────────────────────────────────────────────────────


def log_adapter_stage(
    adapter: str,
    property_id: str,
    stage: str,
    outcome: str,
    *,
    units: int = 0,
    reason: str = "",
    **extra: Any,
) -> None:
    """Emit ``extract.tier_attempted`` for one stage of an adapter cascade.

    Event shape::

        kind            = "extract.tier_attempted"
        tier_key        = "<adapter>:<stage>"      # e.g. "g5:urn_pick"
        outcome         = <stage-specific>          # ok|skipped_*|status_NNN|etc.
        ran_units       = <int>
        reason          = <short text, max 200 chars>
        via_proxy       = bool(env PROBE_PROXY_URL set)
        via_unlocker    = bool(env WEB_UNLOCKER_KEY set)
        **extra         = adapter-specific signal_* fields

    Never raises (observability is best-effort).
    """
    try:
        from ma_poc.observability.events import EventKind, emit

        emit(
            EventKind.TIER_ATTEMPTED,
            property_id or "unknown",
            tier_key=f"{adapter}:{stage}",
            outcome=str(outcome)[:80],
            ran_units=int(units or 0),
            reason=str(reason)[:200],
            via_proxy=bool(os.getenv("PROBE_PROXY_URL", "").strip()),
            via_unlocker=bool(os.getenv("WEB_UNLOCKER_KEY", "").strip()),
            **{k: v for k, v in extra.items() if v is not None},
        )
    except Exception:
        # Telemetry must never break the scrape.
        pass


# ──────────────────────────────────────────────────────────────────────
# Raw-signal diagnostics — fire on silent parser failures.
# ──────────────────────────────────────────────────────────────────────


# Patterns we mine the failing body for. Each pattern targets one
# structural feature that's useful for diagnosing the failure without
# re-fetching: caption text, heading text, row-attribute inventory,
# table-id grouping, common PMS-template markers.
_DIAG_PATTERNS: "dict[str, re.Pattern[str]]" = {
    "caption": re.compile(r"<caption[^>]*>([^<]{1,400})</caption>", re.I),
    "heading": re.compile(r"<h[1-3][^>]*>([^<]{1,200})</h[1-3]>", re.I),
    "table_id": re.compile(r"id=['\"]((?:divFPH_|fp-|unit-|floorplan-)[A-Za-z0-9_-]+)['\"]", re.I),
    "data_label": re.compile(r"data-label=['\"]?([A-Za-z][A-Za-z .]{0,40})['\"]", re.I),
    "floorplan_id": re.compile(r"(?:FloorPlanID|floorplan[_-]?id|fpId)\s*[=:]\s*['\"]?(\d{3,})", re.I),
    # PMS-template / vendor markers — counted so we can detect "this
    # property is actually a different PMS" failure modes.
    "vendor_marker": re.compile(
        r"\b(fp-container|fp-unit|sightmap\.com/embed|apts247\.info|"
        r"securecafe\.com|onlineleasing\.realpage|api\.ws\.realpage|"
        r"engrain|RPFP_config|prospectportal\.com|knock-knock|doorway\.knck\.io|"
        r"g5-cl[a-z]?-)",
        re.I,
    ),
}


def capture_body_diagnostics(body: str, *, max_per_kind: int = 5) -> dict[str, Any]:
    """Mine *body* for structural signals so a silent parser failure is
    debuggable from events.jsonl alone.

    Returns a dict shaped for inclusion as ``signal_*`` fields on a
    tier_attempted event. Each list is capped at ``max_per_kind``
    entries; strings are truncated to 200 chars. Total payload stays
    under ~3KB so it fits comfortably inside the observability ledger
    line budget.
    """
    if not body:
        return {"body_len": 0}

    out: dict[str, Any] = {"body_len": len(body)}

    captions = [
        re.sub(r"\s+", " ", m.group(1)).strip()[:200]
        for m in list(_DIAG_PATTERNS["caption"].finditer(body))[:max_per_kind]
    ]
    if captions:
        out["caption_samples"] = captions

    headings = [
        re.sub(r"\s+", " ", m.group(1)).strip()[:200]
        for m in list(_DIAG_PATTERNS["heading"].finditer(body))[:max_per_kind]
    ]
    if headings:
        out["heading_samples"] = headings

    table_ids = list({m.group(1) for m in _DIAG_PATTERNS["table_id"].finditer(body)})[:max_per_kind]
    if table_ids:
        out["table_ids"] = sorted(table_ids)

    labels = sorted({m.group(1).strip() for m in _DIAG_PATTERNS["data_label"].finditer(body)})
    if labels:
        out["data_label_inventory"] = labels[: max_per_kind * 3]

    fp_ids = list({m.group(1) for m in _DIAG_PATTERNS["floorplan_id"].finditer(body)})[:max_per_kind]
    if fp_ids:
        out["floorplan_ids_seen"] = sorted(fp_ids)

    # 350-char window before a key marker — useful for any adapter that
    # has a "row" or "unit" container. Callers can hint with anchor_text.
    # Default anchor: the most-common unit-row marker across PMS templates.
    for anchor in ("AvailUnitRow", "fp-unit", "fp-container", "data-floorplan-price",
                   "unit_space", "<tr class='unit"):
        pos = body.find(anchor)
        if pos > 0:
            out["first_row_anchor"] = anchor
            out["first_row_ctx"] = body[max(0, pos - 350):pos][-300:]
            break

    # Vendor-marker presence counts — answers "is this actually a
    # different PMS than the detector thought?" deterministically.
    vendors: dict[str, int] = {}
    for m in _DIAG_PATTERNS["vendor_marker"].finditer(body):
        key = m.group(1).lower()
        vendors[key] = vendors.get(key, 0) + 1
    if vendors:
        out["vendor_markers"] = vendors

    # CF challenge fingerprint count — per-marker so dashboards can see
    # WHICH challenge variant fired without re-fetching the body.
    cf_counts = {m: body.count(m) for m in CF_CHALLENGE_MARKERS if m in body}
    if cf_counts:
        out["cf_marker_counts"] = cf_counts

    return out


def log_adapter_diag(
    adapter: str,
    property_id: str,
    stage: str,
    body: str,
    *,
    reason: str = "",
    **extra_signals: Any,
) -> None:
    """Emit a rich diagnostic event after a parser silently returned empty.

    Same envelope as :func:`log_adapter_stage` (so the existing analyzer
    aggregations pick it up automatically), but the payload carries
    ``signal_*`` fields from :func:`capture_body_diagnostics` plus any
    adapter-specific signals passed via ``extra_signals``.

    Should only fire when the parent stage already emitted a
    "silent empty" outcome (parse returned 0 despite visible markup).
    """
    try:
        from ma_poc.observability.events import EventKind, emit

        signals = capture_body_diagnostics(body)
        signals.update(extra_signals)
        emit(
            EventKind.TIER_ATTEMPTED,
            property_id or "unknown",
            tier_key=f"{adapter}:{stage}_diag",
            outcome="parser_silent_empty",
            ran_units=0,
            reason=str(reason)[:200],
            via_proxy=bool(os.getenv("PROBE_PROXY_URL", "").strip()),
            via_unlocker=bool(os.getenv("WEB_UNLOCKER_KEY", "").strip()),
            **{f"signal_{k}": v for k, v in signals.items()},
        )
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────
# Probe-response classification — shared CF / status / parse logic.
# ──────────────────────────────────────────────────────────────────────


def classify_probe_body(
    status: int,
    body: str,
    *,
    success_marker: str | None = None,
    cf_shell_size_cap: int = 15000,
) -> tuple[str, str]:
    """Categorise a probe response into (outcome, reason).

    Distinguishes:

    * ``ok`` — 200 + ``success_marker`` present (if provided)
    * ``cf_challenge_shell`` — 200 + body < cap + contains a CF marker
    * ``blocked_status_403/429/503`` — explicit anti-bot status codes
    * ``status_NNN`` — any other non-200 status
    * ``no_success_marker`` — 200 but missing the caller's success token

    Pass ``success_marker`` (e.g. ``"AvailUnitRow"`` or ``"unit_space"``)
    to get the most specific outcome distinction. Without it, only
    status + CF-shell detection runs.
    """
    body = body or ""
    body_len = len(body)
    if status != 200:
        if status in (403, 429, 503):
            return (f"blocked_status_{status}", f"len={body_len}")
        return (f"status_{status}", f"len={body_len}")
    cf_hit = any(m in body for m in CF_CHALLENGE_MARKERS)
    if cf_hit and body_len < cf_shell_size_cap:
        return ("cf_challenge_shell", f"len={body_len} markers_seen=True")
    if success_marker:
        if success_marker in body:
            return ("ok", f"len={body_len} marker_count={body.count(success_marker)}")
        return ("no_success_marker", f"len={body_len} marker={success_marker!r}")
    return ("ok", f"len={body_len}")
