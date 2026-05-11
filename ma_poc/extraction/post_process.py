"""Per-unit post-processing orchestrator.

Single entry point that composes the canonical extraction pipeline:

    raw_unit  → infer (fill gaps from name / range string / fallback id)
              → sanity (clamp impossible values to absent)
              → is_valid_unit (gate: at least one numeric dimension)
              → classify (unit-level vs plan-level)
              → admitted (units) | admitted (plan_summaries) | rejected

Every adapter should call ``post_process(units, property_id=...)`` once,
immediately before returning its ``AdapterResult``. This is the single
chokepoint where the unified validity rules are enforced; the per-tier
gates surveyed in the 2026-05-11 design audit (``parse_generic_api``,
``parse_api_responses``, ``has_unit_signals``, ``_container_yields_unit``,
``_offer_to_unit``, ``schema_gate.is_substantive``) all delegate here.

Returns a :class:`PostProcessResult` carrying:

  * ``units``           — validated rows that describe specific apartments
                          (natural unit_id OR per-unit signal like
                          availability_date / floor / building).
  * ``plan_summaries``  — validated rows that describe floor-plan-level
                          summaries (no per-apartment identity). Stage 2
                          surfaces these as a first-class output via
                          ``floor_plans[]`` on the V2 property record.
  * ``rejected``        — ``(unit, [absence_reason, ...])`` pairs for
                          telemetry — rows that failed ``is_valid_unit``.

This module is **pure**: each call produces a new list. Input units are
deep-copied by the underlying ``infer`` / ``sanity_bound``; the caller's
unit dicts are never mutated.

Idempotency: a list that's already passed through ``post_process`` survives
a second call unchanged — ``infer``'s ``_inferred`` marker and ``sanity``'s
in-range-no-op guarantee that.

See docs/2026_05_11_regressions_fix_design.md for the design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ma_poc.extraction.classify import classify
from ma_poc.extraction.infer import infer
from ma_poc.extraction.sanity import sanity_bound
from ma_poc.validation.unit_validity import absence_reasons, is_valid_unit


@dataclass
class PostProcessResult:
    """Output of ``post_process``.

    ``units`` are admitted rows that describe specific apartments (Stage 2
    "unit-level"). ``plan_summaries`` are admitted rows that describe
    floor-plan-level summaries (Stage 2 "plan-level"). ``rejected`` carries
    ``(unit, [REASON_*, ...])`` pairs for observability; Stage 1 callers
    can emit ``EventKind.UNIT_VALIDITY_REJECTED`` from this.
    """

    units: list[dict[str, Any]] = field(default_factory=list)
    plan_summaries: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[tuple[dict[str, Any], list[str]]] = field(default_factory=list)

    @property
    def n_admitted(self) -> int:
        """Total admitted rows: unit-level + plan-level. The verdict layer
        recognises both as "success" (SUCCESS vs SUCCESS_PLAN_LEVEL)."""
        return len(self.units) + len(self.plan_summaries)

    @property
    def n_unit_level(self) -> int:
        return len(self.units)

    @property
    def n_plan_level(self) -> int:
        return len(self.plan_summaries)

    @property
    def n_rejected(self) -> int:
        return len(self.rejected)

    @property
    def admitted(self) -> list[dict[str, Any]]:
        """All admitted rows — unit-level followed by plan-level.

        Convenience accessor for adapter wire-ins that need to populate
        ``AdapterResult.units`` with the combined set (back-compat with
        pre-Stage-2 downstream consumers that haven't yet learned to
        distinguish ``units`` from ``plan_summaries``). Stage 2 V2 schema
        integration (deferred) lifts ``plan_summaries`` into a separate
        ``floor_plans[]`` field on the V2 property record.
        """
        return list(self.units) + list(self.plan_summaries)


def post_process(
    raw_units: list[dict[str, Any]] | None,
    *,
    property_id: str | None = None,
) -> PostProcessResult:
    """Canonicalise + validate a list of raw unit dicts.

    Args:
        raw_units: Adapter-emitted unit dicts in any producer shape. ``None``
            or empty list is accepted (returns an empty result).
        property_id: Canonical property id passed to ``infer`` for the
            unit-id fallback and floor_plan_id derivation. May be ``None``;
            those two passes are skipped if so.

    Returns:
        :class:`PostProcessResult` partitioning admitted units from rejected
        ones. Non-dict / non-iterable input is treated as empty (defensive).
    """
    out = PostProcessResult()

    if not raw_units:
        return out

    # Defensive: callers might hand a non-list; iterate gracefully or bail.
    try:
        iterator = iter(raw_units)
    except TypeError:
        return out

    for raw in iterator:
        if not isinstance(raw, dict):
            # Non-dict entries (None, strings, malformed payloads) are
            # not units. Record as rejected for visibility.
            out.rejected.append(
                ({"_raw": str(raw)[:64]}, ["NOT_A_DICT"])
            )
            continue

        # Stage 1+2 pipeline: infer → sanity → validity → classify.
        inferred = infer(raw, property_id=property_id)
        sanitized = sanity_bound(inferred)
        if not is_valid_unit(sanitized):
            out.rejected.append((sanitized, absence_reasons(sanitized)))
            continue
        # Admitted — partition by unit-level vs plan-level.
        if classify(sanitized) == "unit":
            out.units.append(sanitized)
        else:
            out.plan_summaries.append(sanitized)

    return out
