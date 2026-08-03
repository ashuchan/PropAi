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

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.core.source_ids import PER_UNIT_EVIDENCE_KEYS

log = logging.getLogger(__name__)


# Event kind for the canonical per-property emit. Defined here (not
# imported from ``ma_poc.observability.events``) to avoid a reporting →
# observability layer dependency just for one string constant.
_EMIT_KIND = "output.property_emitted"


# 2026-07-18 verdict-hygiene: per-unit backend source ids that establish
# unit-level identity even when a row has no natural unit_number, so the
# plan-vs-unit downgrade must NOT treat such a row as plan-level. PER-UNIT
# ids ONLY — never *_floor_plan_id / *_fpid / *_slug (those are plan-scoped).
#
# 2026-07-27: the local 12-key ``PER_UNIT_SOURCE_ID_KEYS`` frozenset that used
# to live here is DELETED, not aliased — an alias is how the drift against
# ``core.identity``'s parallel list started. Membership now comes from the
# shared registry. Four entries were removed outright in that move:
#   camden_unit_id  — PLAN-scoped (366 rows / 129 distinct; 30% of (property,
#                     plan) pairs rotated value over six days). 745 units.
#   edifice_unit_id — a verbatim copy of unit_no, i.e. of unit_number. 164 units.
#   thinkreside_unit— a verbatim copy of unit_number AND not unique
#                     ('312' x3 in property 271195). 87 units.
#   securecafe_id   — DEAD: no adapter has ever written it. 0 units.
# All 996 touched units already carry a natural non-``inferred_`` unit_id, so
# the measured verdict-outcome delta of the removal is ZERO — it removes a
# latent bug, it does not change behaviour.
#
# Symmetrically, the SIX keys this move ADDS change no verdict either: this
# predicate runs on PRE-format adapter rows (``jugnu.py:1820`` passes
# ``result["units"]``), where ``unit_has_real_anchor`` already returns True via
# ``unit_number`` — junk filtering happens later, in ``_format_v2_unit``. So
# the ``or`` at :97 / :442 short-circuits before this function is consulted.
# Property-level verdict flips measured across all three run-artifact sets:
# ZERO. The one row with a real delta is property 35256's, which LOSES
# ``camden_unit_id`` as evidence (it is genuinely plan-scoped) — 1 row in
# 2026-07-12, 1 in the canary, 0 in plancohort, no property-level change.
# Do not describe this consolidation as a gold-recovery lever; it is a
# correctness cleanup. See ``ma_poc/core/source_ids.py`` MEASURED PRODUCTION
# IMPACT.
#
# ``reporting -> core`` is an established edge (this module already imports
# ``ma_poc.core.identity`` below).


def _has_per_unit_source_id(unit: dict[str, Any]) -> bool:
    """True when *unit* carries a real per-unit backend id in ``source_ids``.

    Uses ``PER_UNIT_EVIDENCE_KEYS`` — the classify-only view, which needs
    uniqueness-within-property but NOT the cross-run stability that identity's
    minting view demands.
    """
    sids = unit.get("source_ids") or {}
    if not isinstance(sids, dict):
        return False
    return any(sids.get(k) for k in PER_UNIT_EVIDENCE_KEYS)


def _units_are_unit_level(units: list[dict[str, Any]] | None) -> bool:
    """True when the accepted units carry real unit-level identity AND rent —
    i.e. they must NOT be demoted to plan-level.

    A row is unit-level when it has a natural (non-``inferred_``) unit_id OR a
    real per-unit source id; the property must additionally carry a rent
    signal over its available / real-anchor units.
    """
    if not units:
        return False
    from ma_poc.validation.schema_gate import property_has_rent_signal

    # 2026-07-25: was ``not str(u.get("unit_id", "")).startswith("inferred_")``,
    # which had two failure modes, both proven against the 2026-07-25 run:
    #   1. ORDERING — unit_id is minted later, in _format_v2_unit. At verdict
    #      time the key is absent, "" never starts with "inferred_", and every
    #      plan-level row read as real identity.
    #   2. ``str(None)`` — an explicit ``unit_id: None`` stringifies to "None",
    #      which also does not start with "inferred_", so the emptiest possible
    #      row counted as the strongest identity.
    # unit_has_real_anchor resolves the same anchors identity itself uses, and
    # works on pre- and post-format rows alike.
    has_identity = any(
        unit_has_real_anchor(u) or _has_per_unit_source_id(u) for u in units
    )
    return has_identity and property_has_rent_signal(units)


class Verdict(StrEnum):
    """Property-level outcome verdict."""

    SUCCESS = "SUCCESS"
    #: Stage 2 (2026-05-12): the property returned valid plan-level data
    #: but no per-apartment units. A floor plan summary is a real extraction
    #: outcome (the page genuinely doesn't expose unit-level inventory),
    #: not a failure. ``verdict_is_success`` recognises both ``SUCCESS`` and
    #: ``SUCCESS_PLAN_LEVEL`` so the headline metric counts both correctly.
    SUCCESS_PLAN_LEVEL = "SUCCESS_PLAN_LEVEL"
    #: 2026-05-23: the operator's site explicitly publishes "no units
    #: available at this time" (or one of nine sibling phrases — see
    #: ``ma_poc.pms.adapters._no_availability``). The page was scraped
    #: successfully and we captured the operator's actual stated state;
    #: there is just nothing to lease right now. Counts toward the
    #: success-rate numerator (we DID extract what the operator publishes),
    #: but is intentionally distinct from SUCCESS so dashboards can split
    #: "zero-inventory operators" from "with-inventory extractions".
    SUCCESS_NO_AVAILABILITY = "SUCCESS_NO_AVAILABILITY"
    #: 2026-07-25: the operator publishes ONLY floor plans — no per-apartment
    #: data exists to extract. Distinct from SUCCESS_PLAN_LEVEL (where we DID
    #: extract plan rows): this is the PROVEN ceiling, graded
    #: ``PublishCeiling.CONFIRMED_PLAN_ONLY`` by ``reporting.publish_ceiling``
    #: with its rent-token / unit-vocab / embed guards satisfied. A success:
    #: we correctly determined the limit of what the operator discloses.
    SUCCESS_PLAN_ONLY_PUBLISHED = "SUCCESS_PLAN_ONLY_PUBLISHED"
    #: 2026-07-25: the operator publishes NO rent or unit data at all, proven
    #: (``PublishCeiling.CONFIRMED_NO_DATA``: cascade ran empty, zero rent
    #: tokens, zero unit vocabulary, plus a positive operator signal such as an
    #: empty-inventory or pre-leasing page). "No data available" is the correct
    #: answer for this property, not a scrape failure — so it counts toward the
    #: success numerator while staying its own visible bucket.
    #:
    #: The bar is deliberately high: ANY rent token in the HTML alongside zero
    #: extracted units is graded EXTRACTION_MISS (our bug) and stays
    #: FAILED_NO_DATA. That guard exists because a real page (rentcafe pid
    #: 18158) carried "No apartments available" AND listed 2 units at $1,795 —
    #: a no-data claim must never rest on a marker string.
    SUCCESS_NO_DATA_PUBLISHED = "SUCCESS_NO_DATA_PUBLISHED"
    FAILED_UNREACHABLE = "FAILED_UNREACHABLE"
    FAILED_NO_DATA = "FAILED_NO_DATA"
    CARRY_FORWARD = "CARRY_FORWARD"
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
_SUCCESS_VERDICTS: frozenset[str] = frozenset(
    {
        Verdict.SUCCESS.value,
        Verdict.SUCCESS_PLAN_LEVEL.value,
        Verdict.SUCCESS_NO_AVAILABILITY.value,
        # 2026-07-25: proven publish ceilings. We correctly determined the
        # limit of what the operator discloses — that is a successful scrape,
        # not a failure. Kept as DISTINCT values (not folded into SUCCESS) so a
        # plain verdict count shows each bucket instead of hiding zero-data
        # properties inside the headline.
        Verdict.SUCCESS_PLAN_ONLY_PUBLISHED.value,
        Verdict.SUCCESS_NO_DATA_PUBLISHED.value,
    }
)

#: publish-ceiling grade -> the verdict it justifies. Only these two grades are
#: gold-eligible; EXTRACTION_MISS / NEEDS_RENDER / UNCERTAIN stay FAILED_NO_DATA
#: because they mean "we could not prove it", which is our problem, not the
#: operator's.
CEILING_VERDICTS: dict[str, str] = {
    "CONFIRMED_PLAN_ONLY": Verdict.SUCCESS_PLAN_ONLY_PUBLISHED.value,
    "CONFIRMED_NO_DATA": Verdict.SUCCESS_NO_DATA_PUBLISHED.value,
}


def verdict_for_publish_ceiling(
    current_verdict: str | None, ceiling_grade: str | None
) -> str | None:
    """Return the upgraded verdict a proven publish ceiling justifies, else None.

    ``reporting.publish_ceiling`` grades every zero-unit result but is ADDITIVE
    — it stamps ``_meta.publish_ceiling`` and historically never touched the
    verdict, so a property we had PROVEN publishes nothing still shipped as
    FAILED_NO_DATA and dragged the success rate down for doing the right thing.

    Deliberately narrow: only upgrades FAILED_NO_DATA, and only on the two
    CONFIRMED grades. Never downgrades anything, and never upgrades a property
    whose grade is EXTRACTION_MISS (rent tokens present + zero units extracted
    = our bug, and the largest such cohort was 182 properties in the 2026-07-25
    run — those must stay visible as failures).
    """
    if current_verdict != Verdict.FAILED_NO_DATA.value:
        return None
    return CEILING_VERDICTS.get(str(ceiling_grade or ""))


def _has_any_extracted_units(extract_result: Any) -> bool:
    """``True`` when the extraction cascade produced at least one record.

    Used to stop a failed ENTRY fetch from vetoing data the cascade actually
    recovered (salvage checkpoint, link-hop sub-page, plan-text tier). Tolerates
    both the dataclass shape (``.records``) and the dict shape
    (``{"records": [...]}`` / ``{"units": [...]}``). Never raises.
    """
    if extract_result is None:
        return False
    try:
        records = getattr(extract_result, "records", None)
        if records is None and isinstance(extract_result, dict):
            records = extract_result.get("records")
            if records is None:
                records = extract_result.get("units")
        return bool(records)
    except Exception:  # pragma: no cover — defensive only
        return False


def verdict_is_success(verdict: Verdict | str | None) -> bool:
    """``True`` when *verdict* counts toward success-rate numerator.

    Accepts a :class:`Verdict` enum value, the underlying string, or
    ``None``. Returns ``False`` for unknown / missing verdicts.

    Recognises both ``SUCCESS`` (unit-level inventory extracted) and
    ``SUCCESS_PLAN_LEVEL`` (plan-level summary extracted — Stage 2 of the
    2026-05-11 fix).
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


def compute(
    fetch_outcome: str | None = None,
    extract_result: Any = None,
    validated: Any = None,
    carry_forward_applied: bool = False,
    units_hollow: bool = False,
    plan_summaries: list[Any] | None = None,
    verdict_quality_override: str | None = None,
    units: list[dict[str, Any]] | None = None,
    operator_no_availability: bool = False,
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
        verdict_quality_override: Authoritative downgrade signal set by
            scraper.py Path C plan-level fallback (``result["_verdict_quality"]``).
            When ``"SUCCESS_PLAN_LEVEL"``, an otherwise-SUCCESS verdict is
            downgraded; other verdicts (FAILED_*, DEAD_URL, CARRY_FORWARD,
            PARTIAL) are unaffected.
        units: The final unit list. When provided AND the computed verdict
            is SUCCESS, applies the verdict-honesty downgrade: if every
            unit has an ``inferred_*`` UID prefix OR no unit carries a
            numeric rent value, downgrade to SUCCESS_PLAN_LEVEL. Catches
            the 1,031-prop inflated-SUCCESS bucket flagged by the 2026-05-20
            JSON-LD recovery audit.
        operator_no_availability: 2026-05-23. The page carried an
            explicit "no units available" statement (krcapartments-class
            cohort, ~10 properties). When True AND extraction produced
            no records, return SUCCESS_NO_AVAILABILITY instead of
            FAILED_NO_DATA — the scrape succeeded; the operator is just
            reporting zero inventory right now.

    Returns:
        VerdictResult with verdict, reason, and source.
    """
    # F7 fix: carry-forward is always checked first, before any other signal.
    if carry_forward_applied:
        return VerdictResult(Verdict.CARRY_FORWARD, "carry_forward_applied", "carry_forward")

    # Stage 3: a dead ENTRY URL is terminal only when the extraction cascade
    # recovered nothing. Six properties in the stratified Aug-02 canary
    # produced 107 priced, physically identified units from bounded sub-routes
    # despite a 404/410 entry response. The recovered inventory is stronger
    # evidence than the configured URL's status and must reach the normal
    # SUCCESS/PLAN decision below.
    if fetch_outcome == "DEAD_URL" and not _has_any_extracted_units(extract_result):
        return VerdictResult(
            Verdict.DEAD_URL,
            "url is dead (404 / 410 / 451 / NXDOMAIN)",
            "fetch",
        )

    # A non-OK fetch outcome is only DISPOSITIVE when nothing was extracted.
    # 2026-07-25 RCA: this check used to fire unconditionally and vetoed units
    # the cascade had already produced — a property whose base fetch failed but
    # whose salvage/link-hop path still returned priced units was stamped
    # FAILED_UNREACHABLE and its data thrown away. Measured on the 5k canary:
    # 10 properties / 68 units / 63 with rent were being discarded this way,
    # and another 20 were mislabelled "unreachable" despite a cascade that ran.
    # Extraction succeeding is stronger evidence than the entry fetch failing.
    if fetch_outcome and fetch_outcome not in ("OK", "NOT_MODIFIED"):
        if not _has_any_extracted_units(extract_result):
            return VerdictResult(
                Verdict.FAILED_UNREACHABLE,
                f"fetch outcome: {fetch_outcome}",
                "fetch",
            )

    # Check extract result
    if extract_result is not None:
        records = getattr(extract_result, "records", None)
        if records is None and isinstance(extract_result, dict):
            records = extract_result.get("records", extract_result.get("units", []))
        if not records:
            # 2026-05-23: operator-published "no availability" wins over
            # FAILED_NO_DATA. We DID extract the operator's stated state
            # (zero inventory); that's a successful scrape, not a missing-
            # data failure. Keep this check above the plan_summaries
            # fallback — when both signals are present, the explicit
            # zero-availability statement is the more authoritative one.
            if operator_no_availability:
                return VerdictResult(
                    Verdict.SUCCESS_NO_AVAILABILITY,
                    "operator published zero availability",
                    "extract",
                )
            # Stage 2: a property with plan-level data but no per-unit records
            # is SUCCESS_PLAN_LEVEL, not FAILED_NO_DATA. The page genuinely
            # exposed plan summaries; that's the only thing the site offers.
            if plan_summaries:
                return VerdictResult(
                    Verdict.SUCCESS_PLAN_LEVEL,
                    f"plan-level data only ({len(plan_summaries)} plans)",
                    "extract",
                )
            return VerdictResult(Verdict.FAILED_NO_DATA, "no records extracted", "extract")
    else:
        if operator_no_availability:
            return VerdictResult(
                Verdict.SUCCESS_NO_AVAILABILITY,
                "operator published zero availability",
                "extract",
            )
        if plan_summaries:
            return VerdictResult(
                Verdict.SUCCESS_PLAN_LEVEL,
                f"plan-level data only ({len(plan_summaries)} plans)",
                "extract",
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
        # 2026-05-23: hollow units + explicit operator-no-availability
        # statement → SUCCESS_NO_AVAILABILITY. The "hollow units" are
        # the no-availability placeholder rows the adapter emitted.
        if operator_no_availability:
            return VerdictResult(
                Verdict.SUCCESS_NO_AVAILABILITY,
                "units hollow; operator published zero availability",
                "validate",
            )
        # Stage 2: same plan-level-rescue path applies when units are hollow.
        if plan_summaries:
            return VerdictResult(
                Verdict.SUCCESS_PLAN_LEVEL,
                f"units hollow; plan-level data present ({len(plan_summaries)} plans)",
                "validate",
            )
        return VerdictResult(Verdict.FAILED_NO_DATA, "units_hollow_all_tiers", "validate")

    # 2026-05-20 verdict-honesty downgrade: an otherwise-SUCCESS property
    # is downgraded to SUCCESS_PLAN_LEVEL when (a) Path C's plan-level
    # fallback already stamped ``_verdict_quality="SUCCESS_PLAN_LEVEL"`` or
    # (b) every accepted unit is plan-level-shaped — either an inferred_*
    # UID prefix on all rows OR no rent on any row.
    # (e) demote-guard (2026-07-18): honor the Path-C plan-level stamp UNLESS
    # the accepted units actually carry real unit-level identity + rent — a
    # stale _verdict_quality must not bury rows that pass every gate.
    if (
        verdict_quality_override == Verdict.SUCCESS_PLAN_LEVEL.value
        and not _units_are_unit_level(units)
    ):
        return VerdictResult(
            Verdict.SUCCESS_PLAN_LEVEL,
            "path_c_plan_level_fallback",
            "validate",
        )

    if units:
        # (c) honor real per-unit source ids (2026-07-18): a row with a real
        # backend per-unit id (sightmap_unit_id / entrata_uid / …) is
        # unit-level even without a natural unit_number, so it is not
        # "inferred" for the plan-vs-unit decision.
        # 2026-07-25: see _units_are_unit_level — this predicate had the same
        # two defects (unit_id not yet minted at verdict time; str(None) ==
        # "None"). Either one made a single row defeat `all`, so a wholly
        # plan-level property shipped as SUCCESS.
        all_inferred = all(
            not unit_has_real_anchor(u) and not _has_per_unit_source_id(u)
            for u in units
        )
        if all_inferred:
            return VerdictResult(
                Verdict.SUCCESS_PLAN_LEVEL,
                f"all_inferred_uids ({len(units)} units)",
                "validate",
            )
        # Late import to avoid layer-ordering issues; predicate is pure.
        from ma_poc.validation.schema_gate import property_has_rent_signal

        if not property_has_rent_signal(units):
            return VerdictResult(
                Verdict.SUCCESS_PLAN_LEVEL,
                f"no_rent_signal ({len(units)} units)",
                "validate",
            )

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
