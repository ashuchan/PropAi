"""Verdict computation — derives a property-level verdict from pipeline results.

Decision rules (first match wins):
1. carry_forward_applied → CARRY_FORWARD
2. fetch hard-fail → FAILED_UNREACHABLE
3. extract empty → FAILED_NO_DATA
4. majority rejected → PARTIAL
5. else → SUCCESS

Also hosts the cross-consumer verdict resolver
(``scan_event_ledger_verdicts`` + ``resolve_verdict``) used by both
``reporting/run_report.py`` and ``observability/slo_watcher.py`` to make
the headline success metric robust against ``_meta.verdict`` corruption
(see docs/2026_05_11_regressions_fix_design.md, Bug A v0.2).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# Event kind for the canonical per-property emit. Defined here (not
# imported from ``ma_poc.observability.events``) to avoid a reporting →
# observability layer dependency just for one string constant.
_EMIT_KIND = "output.property_emitted"


class Verdict(StrEnum):
    """Property-level outcome verdict."""

    SUCCESS = "SUCCESS"
    #: Stage 2 (2026-05-12): the property returned valid plan-level data
    #: but no per-apartment units. A floor plan summary is a real extraction
    #: outcome (the page genuinely doesn't expose unit-level inventory),
    #: not a failure. ``verdict_is_success`` recognises both ``SUCCESS`` and
    #: ``SUCCESS_PLAN_LEVEL`` so the headline metric counts both correctly.
    SUCCESS_PLAN_LEVEL = "SUCCESS_PLAN_LEVEL"
    #: 2026-05-17 — timeout-recovery success. The per-property wallclock
    #: budget fired before the full cascade completed, but the link-hop
    #: accumulator had already buffered at least one valid unit. Distinct
    #: from ``PARTIAL`` (validation-majority-rejected) so the success
    #: classifier can admit timeout-rescued records without admitting
    #: bulk-rejected ones.
    SUCCESS_PARTIAL = "SUCCESS_PARTIAL"
    FAILED_UNREACHABLE = "FAILED_UNREACHABLE"
    FAILED_NO_DATA = "FAILED_NO_DATA"
    CARRY_FORWARD = "CARRY_FORWARD"
    #: Validation-majority-rejected. ``compute()`` returns this when the
    #: schema gate drops more than half of the extracted rows on validity
    #: grounds (dim-less rows, junk floor-plan names, etc.). The remaining
    #: rows are still shipped but the headline classifier treats the run
    #: as a partial failure — the data we have is suspect.
    PARTIAL = "PARTIAL"
    #: Stage 3 (2026-05-12): the URL is dead (404 / 410 / 451 / NXDOMAIN).
    #: Terminal: re-running won't change the outcome. Excluded from the
    #: success-rate denominator (we never had a chance to extract) — see
    #: ``verdict_excluded_from_success_rate``. Routed to a re-discovery
    #: queue rather than the standard DLQ retry escalation.
    DEAD_URL = "DEAD_URL"


#: Verdicts that count toward the success-rate numerator. Use
#: ``verdict_is_success(v)`` rather than ``v == Verdict.SUCCESS`` so a
#: future addition of another success-class verdict (e.g. a hypothetical
#: ``SUCCESS_ENRICHED``) lands cleanly in one place.
#:
#: 2026-05-17 (revised): ``SUCCESS_PARTIAL`` is the success-class verdict
#: for timeout-rescued records that buffered at least one real unit before
#: the per-property wallclock fired. ``PARTIAL`` is reserved for the
#: validation-majority-rejected path and stays OUT of the success set —
#: a run where the schema gate dropped >50% of rows is suspect, even if
#: a few rows survived. Pre-revision (2026-05-16) we admitted both under
#: ``PARTIAL`` which conflated genuine timeout-rescued successes with
#: low-quality validation-rejected runs in the headline metric.
_SUCCESS_VERDICTS: frozenset[str] = frozenset(
    {
        Verdict.SUCCESS.value,
        Verdict.SUCCESS_PLAN_LEVEL.value,
        Verdict.SUCCESS_PARTIAL.value,
    }
)


def verdict_is_success(verdict: Verdict | str | None) -> bool:
    """``True`` when *verdict* counts toward success-rate numerator.

    Accepts a :class:`Verdict` enum value, the underlying string, or
    ``None``. Returns ``False`` for unknown / missing verdicts.

    Recognises:
      - ``SUCCESS`` — unit-level inventory extracted.
      - ``SUCCESS_PLAN_LEVEL`` — plan-level summary extracted (Stage 2,
        2026-05-11).
      - ``SUCCESS_PARTIAL`` — timeout-rescued record with at least one
        real unit buffered before the per-property wallclock fired
        (2026-05-17).

    ``PARTIAL`` (validation-majority-rejected) is intentionally NOT
    included — those runs surfaced data the gate dropped because most
    rows failed dimensionality checks, so the surviving rows are suspect.
    """
    if verdict is None:
        return False
    if isinstance(verdict, Verdict):
        return verdict.value in _SUCCESS_VERDICTS
    return str(verdict) in _SUCCESS_VERDICTS


#: Verdicts that should be excluded from the success-rate **denominator**.
#: A property classified ``DEAD_URL`` had no chance to extract; counting it
#: as a failure unfairly penalises the run-rate. Reporting layers compute
#: ``rate = successes / (total - dead_urls)`` and surface dead URLs as a
#: separate panel.
_DEAD_URL_VERDICTS: frozenset[str] = frozenset({Verdict.DEAD_URL.value})


def verdict_excluded_from_success_rate(verdict: Verdict | str | None) -> bool:
    """``True`` when *verdict* should be removed from the success-rate
    denominator. Currently only ``DEAD_URL`` qualifies — a terminal "we
    can't reach this resource because the site says it doesn't exist"
    classification (Stage 3 of the 2026-05-11 fix).
    """
    if verdict is None:
        return False
    if isinstance(verdict, Verdict):
        return verdict.value in _DEAD_URL_VERDICTS
    return str(verdict) in _DEAD_URL_VERDICTS


@dataclass(frozen=True)
class VerdictResult:
    """Immutable verdict for a property scrape."""

    verdict: Verdict
    reason: str
    source: str  # "fetch", "extract", "validate", "carry_forward"


def _all_hops_bot_blocked(hop_summary: dict[str, Any] | None) -> bool:
    """True iff at least one hop was attempted AND every hop was BOT_BLOCKED.

    ``hop_summary`` is populated by ``pms/scraper.py::_try_link_hop`` and
    threaded onto the result dict via ``_HOP_OVERWRITE_KEYS``. Shape::

        {"hop_count_total": int, "hop_count_bot_blocked": int}

    Returns False when ``hop_summary`` is missing/empty (the scrape never
    needed to hop, so the entry was the only thing tried). Returns False
    on malformed shapes (non-int values, missing keys) — defensive against
    upstream serialization drift, never raises.
    """
    if not isinstance(hop_summary, dict):
        return False
    try:
        total = int(hop_summary.get("hop_count_total") or 0)
        blocked = int(hop_summary.get("hop_count_bot_blocked") or 0)
    except (TypeError, ValueError):
        return False
    return total > 0 and blocked == total


def compute(
    fetch_outcome: str | None = None,
    extract_result: Any = None,
    validated: Any = None,
    carry_forward_applied: bool = False,
    units_hollow: bool = False,
    plan_summaries: list[Any] | None = None,
    fetch_body: bytes | str | None = None,
    fetch_final_url: str | None = None,
    hop_summary: dict[str, Any] | None = None,
) -> VerdictResult:
    """Compute the verdict for a property scrape.

    Args:
        fetch_outcome: FetchOutcome value string.
        extract_result: ExtractResult or dict with records.
        validated: ValidatedRecords or None.
        carry_forward_applied: Whether carry-forward was used.
        units_hollow: All extracted units are hollow (no substantive fields).
        plan_summaries: Stage 2 plan-level rows from ``post_process``. When
            non-empty AND no unit-level records survive, the verdict is
            ``SUCCESS_PLAN_LEVEL`` rather than ``FAILED_NO_DATA``. Pass
            ``None`` (or omit) to keep pre-Stage-2 behaviour.
        fetch_body: Raw entry-fetch body. When supplied AND the body matches
            a known vendor-lockout / parked-domain pattern (see
            ``reporting.vendor_lockout``), the verdict short-circuits to
            ``DEAD_URL`` regardless of extract outcome — these properties
            have been administratively shut down (non-payment, expired
            domain, parked) and rerunning will never recover them. Pass
            ``None`` to skip the check (e.g. unit tests that don't care).
        fetch_final_url: Post-redirect URL. Some vendor-lockout cases are
            recognisable from the URL alone (cf. ``nonpayment.spherexx.com``)
            even before the body is decoded.

    Returns:
        VerdictResult with verdict, reason, and source.
    """
    # F7 fix: carry-forward is always checked first, before any other signal.
    if carry_forward_applied:
        return VerdictResult(Verdict.CARRY_FORWARD, "carry_forward_applied", "carry_forward")

    # Stage 3: dead URLs are terminal — distinct from transient unreachable
    # so reporting can exclude them from the success-rate denominator.
    if fetch_outcome == "DEAD_URL":
        return VerdictResult(
            Verdict.DEAD_URL,
            "url is dead (404 / 410 / 451 / NXDOMAIN)",
            "fetch",
        )

    if fetch_outcome and fetch_outcome not in ("OK", "NOT_MODIFIED"):
        return VerdictResult(
            Verdict.FAILED_UNREACHABLE,
            f"fetch outcome: {fetch_outcome}",
            "fetch",
        )

    # 2026-05-18 (Bug #6): vendor-lockout / parked-domain detection.
    # Fetch was OK on paper but the body is a hosting-vendor takedown stub
    # (cityclubapartments.com -> nonpayment.spherexx.com 962 bytes was the
    # canonical case on the 2026-05-18 cloud run). These properties pre-fix
    # routed through LLM_GATE_NO_BODY -> FAILED_NO_DATA, which is wrong:
    # the URL is terminally dead at the application level, no amount of
    # re-extraction will recover it. Reclassify as DEAD_URL so it's
    # excluded from the success-rate denominator and routed to re-discovery.
    if fetch_body is not None or fetch_final_url is not None:
        try:
            from ma_poc.reporting.vendor_lockout import detect_vendor_lockout
            is_lockout, label = detect_vendor_lockout(
                fetch_body, final_url=fetch_final_url,
            )
            if is_lockout:
                return VerdictResult(
                    Verdict.DEAD_URL,
                    label or "vendor_lockout",
                    "fetch",
                )
        except Exception as exc:  # noqa: BLE001 — never let verdict raise
            log.debug("vendor_lockout check failed: %s", exc)

    # Check extract result
    if extract_result is not None:
        records = getattr(extract_result, "records", None)
        if records is None and isinstance(extract_result, dict):
            records = extract_result.get("records", extract_result.get("units", []))
        if not records:
            # Stage 2: a property with plan-level data but no per-unit records
            # is SUCCESS_PLAN_LEVEL, not FAILED_NO_DATA. The page genuinely
            # exposed plan summaries; that's the only thing the site offers.
            if plan_summaries:
                return VerdictResult(
                    Verdict.SUCCESS_PLAN_LEVEL,
                    f"plan-level data only ({len(plan_summaries)} plans)",
                    "extract",
                )
            # Bug #1 (2026-05-18): when entry fetched OK but every link-hop
            # came back BOT_BLOCKED (Cloudflare challenge, SGCAPTCHA, etc.),
            # the property is functionally unreachable for extraction. The
            # downstream `/conventional/` Yardi captcha cluster (~193 PIDs on
            # 2026-05-18) was misclassified as FAILED_NO_DATA pre-fix; that
            # bucket is for "page loaded but no inventory found", which
            # implies the page is reachable. Reclassify.
            if _all_hops_bot_blocked(hop_summary):
                return VerdictResult(
                    Verdict.FAILED_UNREACHABLE,
                    f"all_hops_bot_blocked ({hop_summary.get('hop_count_total', 0)})",  # type: ignore[union-attr]
                    "fetch",
                )
            return VerdictResult(Verdict.FAILED_NO_DATA, "no records extracted", "extract")
    else:
        if plan_summaries:
            return VerdictResult(
                Verdict.SUCCESS_PLAN_LEVEL,
                f"plan-level data only ({len(plan_summaries)} plans)",
                "extract",
            )
        if _all_hops_bot_blocked(hop_summary):
            return VerdictResult(
                Verdict.FAILED_UNREACHABLE,
                f"all_hops_bot_blocked ({hop_summary.get('hop_count_total', 0)})",  # type: ignore[union-attr]
                "fetch",
            )
        return VerdictResult(Verdict.FAILED_NO_DATA, "no extract result", "extract")

    # Check validation
    if validated is not None:
        rejected_count = len(getattr(validated, "rejected", []))
        accepted_count = len(getattr(validated, "accepted", []))
        if rejected_count > accepted_count and (rejected_count + accepted_count) > 0:
            return VerdictResult(Verdict.PARTIAL, "majority rejected by validation", "validate")

    # F1: if units were extracted but all are hollow and no rescue tier helped
    if units_hollow:
        # Stage 2: same plan-level-rescue path applies when units are hollow.
        if plan_summaries:
            return VerdictResult(
                Verdict.SUCCESS_PLAN_LEVEL,
                f"units hollow; plan-level data present ({len(plan_summaries)} plans)",
                "validate",
            )
        return VerdictResult(Verdict.FAILED_NO_DATA, "units_hollow_all_tiers", "validate")

    return VerdictResult(Verdict.SUCCESS, "all checks passed", "extract")


# ── Cross-consumer verdict resolver ──────────────────────────────────────────
#
# Both ``run_report.build`` and ``slo_watcher.check`` derive per-property
# pass/fail counts from ``_meta.verdict``. On 2026-05-11 Bug A demonstrated
# that ``_meta.verdict`` can silently go missing in ``properties.json``
# while the runner's ``output.property_emitted`` events.jsonl entries
# remain correct. The two helpers below let any consumer cross-reference
# the event ledger as an authoritative secondary source. Both functions
# are pure (no I/O beyond the explicit ``run_dir`` argument) and never
# raise.


def scan_event_ledger_verdicts(run_dir: Path) -> dict[str, str]:
    """Read events.jsonl in ``run_dir`` and return ``{property_id: verdict}``.

    Only ``output.property_emitted`` events are considered. Other event
    kinds are ignored — callers that need bot/captcha classification
    should read events.jsonl themselves (see
    ``reporting/run_report._scan_event_ledger`` for the multi-signal
    scan used by report generation).

    Returns an empty dict when:
      - ``run_dir/events.jsonl`` does not exist
      - the file is unreadable
      - the file contains no ``output.property_emitted`` events

    Last-write-wins for duplicate property_ids; matches the runner's
    "one emit per property at end of _process_property" invariant.
    """
    out: dict[str, str] = {}
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        return out
    try:
        for line in events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("kind") != _EMIT_KIND:
                continue
            pid = evt.get("property_id")
            if not pid:
                continue
            verdict = evt.get("verdict")
            if isinstance(verdict, str) and verdict:
                out[str(pid)] = verdict
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "scan_event_ledger_verdicts: failed to scan %s: %s",
            events_path, exc,
        )
    return out


def resolve_verdict(
    meta_verdict: str,
    event_verdict: str | None,
    pid: str,
) -> str:
    """Resolve the authoritative per-property verdict from two sources.

    Rule (see docs/2026_05_11_regressions_fix_design.md, Bug A v0.1/v0.2):
      * If only one source is present, use it.
      * If both present and agree, return that value.
      * If both present and disagree, the event-ledger value wins and a
        warning is logged. Disagreement is a runner bug — both sources
        are written from the same ``verdict.verdict.value`` at the tail
        of ``_process_property`` — so we want to surface it loudly, not
        silently.

    Args:
        meta_verdict: ``_meta.verdict`` for the property (or "" if absent).
        event_verdict: ``output.property_emitted.verdict`` for the property
            (or None if no emit event exists).
        pid: property identifier used only for the disagreement warning.

    Returns the resolved verdict string, or "" if both sources are absent.
    """
    if event_verdict and meta_verdict and event_verdict != meta_verdict:
        log.warning(
            "verdict disagreement for property %s — "
            "_meta.verdict=%r, event.verdict=%r; events wins.",
            pid, meta_verdict, event_verdict,
        )
        return event_verdict
    return event_verdict or meta_verdict or ""
