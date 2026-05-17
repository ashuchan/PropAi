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

from ma_poc.extraction.canonical import (
    AVAIL_DATE_KEYS,
    BATHS_KEYS,
    BEDS_KEYS,
    BUILDING_KEYS,
    FP_ID_KEYS,
    RENT_HI_KEYS,
    RENT_LO_KEYS,
    SQFT_KEYS,
    UID_KEYS,
    get_numeric,
    get_str,
    is_present,
)
from ma_poc.extraction.classify import classify
from ma_poc.extraction.infer import infer
from ma_poc.extraction.sanity import sanity_bound
from ma_poc.extraction.unit_identity import canonical_unit_key, merge_units
from ma_poc.extraction.unit_number import extract_unit_number, normalize_unit_number
from ma_poc.validation.unit_validity import absence_reasons, is_valid_unit


def _cross_page_rank(unit: dict[str, Any]) -> int:
    """Higher rank = better representative when two rows share a unit_number.

    Weighted sum over canonical aliases: per-apartment evidence (real
    unit_id, availability_date, floor, building, rent) is weighted ≥ 2,
    plan-level attributes (beds, baths, area) at 1. A row with just
    ``(beds, baths, area)`` scores 3; a row with a real ``unit_id`` alone
    scores 5 — the unit-id row wins.

    Uses ``get_numeric`` / ``get_str`` from ``canonical`` so PascalCase
    (``BedRooms``), camelCase (``bedRooms``), and adapter-emitter variants
    (``bedrooms``, ``market_rent_low``, ``sqft``) all hit the same alias
    table. Without the alias-aware lookup the rank degenerates on
    production-shape rows that lack the V2-canonical literals.

    Inferred ids score 0 on the unit_id slot (real uid only scores +5) —
    no double-penalty needed.
    """
    score = 0

    # Per-apartment identity / signals — strong (weight ≥ 2)
    uid = get_str(unit, UID_KEYS)
    if (
        uid
        and not uid.startswith("inferred_")
        and unit.get("_inferred_id") is not True
    ):
        score += 5
    if get_str(unit, AVAIL_DATE_KEYS):
        score += 3
    if get_numeric(unit, ("floor",)) is not None:
        score += 2
    if get_str(unit, BUILDING_KEYS):
        score += 2

    # Concrete pricing — strong
    if get_numeric(unit, RENT_LO_KEYS) is not None:
        score += 3
    if get_numeric(unit, RENT_HI_KEYS) is not None:
        score += 3

    # Plan-level attributes — weak
    if get_numeric(unit, BEDS_KEYS) is not None:
        score += 1
    if get_numeric(unit, BATHS_KEYS) is not None:
        score += 1
    if get_numeric(unit, SQFT_KEYS) is not None:
        score += 1
    return score


def _building_key(unit: dict[str, Any]) -> str:
    """Stable string key for the unit's ``building`` (when present).

    Empty string means "no building", which collides only with other rows
    that also have no building — so townhome complexes that reuse unit
    numbers across buildings stay separate, while properties without any
    building info dedup as a single namespace.

    Reads via the canonical ``BUILDING_KEYS`` alias table — covers
    ``Building`` / ``BuildingName`` / ``bldg`` / etc. without hardcoding
    a per-call-site key list.
    """
    v = get_str(unit, BUILDING_KEYS)
    return v.strip().lower() if v else ""


def _collect_plan_codes(plan_summaries: list[dict[str, Any]]) -> frozenset[str]:
    """Normalised set of ``floor_plan_id`` strings drawn from plan_summaries.

    Used by ``classify()`` (D16 item 5) and the inverse-B4 guard (item 8)
    to deny promotion / route out unit rows whose ``unit_number`` matches
    a known plan code.

    Plan codes are normalised through ``normalize_unit_number`` — the same
    helper that processes the unit-row ``unit_number`` value — so the two
    sides of the equality check use identical canonicalisation. Without
    this symmetry, a plan code of ``"618.0"`` (LLM float-coerced) never
    matches a unit_number of ``"618"`` (already normalised) and the guard
    silently misses the duplicate.
    """
    out: set[str] = set()
    for p in plan_summaries:
        if not isinstance(p, dict):
            continue
        v = get_str(p, FP_ID_KEYS)
        if not v:
            continue
        norm = normalize_unit_number(v)
        if norm:
            out.add(norm)
    return frozenset(out)


@dataclass
class PostProcessResult:
    """Output of ``post_process``.

    ``units`` are admitted rows that describe specific apartments (Stage 2
    "unit-level"). ``plan_summaries`` are admitted rows that describe
    floor-plan-level summaries (Stage 2 "plan-level"). ``rejected`` carries
    ``(unit, [REASON_*, ...])`` pairs for observability; Stage 1 callers
    can emit ``EventKind.UNIT_VALIDITY_REJECTED`` from this.

    ``cross_page_dedup_collapses`` records how many rows the cross-page
    unit-number dedup pass (D16 item 6) folded into existing buckets —
    surfaced into the run report so operators can see the leak is closing.

    ``inverse_b4_rerouted`` records how many rows the inverse-B4 guard
    (D16 item 8) moved out of ``units`` because their ``unit_number``
    matched a known plan code.
    """

    units: list[dict[str, Any]] = field(default_factory=list)
    plan_summaries: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[tuple[dict[str, Any], list[str]]] = field(default_factory=list)
    cross_page_dedup_collapses: int = 0
    inverse_b4_rerouted: int = 0

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

    def to_meta(self) -> dict[str, int]:
        """D16: telemetry payload for ``AdapterResult._post_process_meta``.

        Adapters stash this on the result so the scraper can surface the
        counts to the run report without re-running ``post_process``.
        """
        return {
            "cross_page_dedup_collapses": self.cross_page_dedup_collapses,
            "inverse_b4_rerouted": self.inverse_b4_rerouted,
            "n_unit_level": self.n_unit_level,
            "n_plan_level": self.n_plan_level,
        }


def _cross_page_dedup_units(
    units: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """D16 item 6 — collapse duplicate unit rows by apartment number.

    Multi-page floor-plan-page crawls (the capability added in commit
    ``4ac6611``) routinely emit the same physical apartment from two
    pages with different ``floor_plan_name`` values. Today this slips
    through the source merger because each page becomes its own
    ``ExtractedSource`` and the inferred-id hash diverges across plans.

    Strategy:
      * Bucket by ``canonical_unit_key(u)`` — apartment number first,
        real-uid fallback, ``None`` for inferred-only rows.
      * Within each bucket, sort by ``_cross_page_rank`` descending.
      * Keep the top-ranked row; merge complementary fields from lower-
        ranked rows into it via ``merge_units`` (provenance-safe).
      * Rows with no usable identity (inferred-only or no uid) bypass
        dedup entirely — never collapse on a synthetic hash.

    Returns the deduplicated list and the count of rows that were folded
    away (always ``>= 0``).
    """
    if not units:
        return units, 0

    # ``canonical_unit_key`` returns ``None`` for rows with only an
    # inferred uid — those land in ``unbucketable`` and are preserved
    # as-is rather than silently collapsed against another inferred-only
    # row that happens to share the same hash.
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    unbucketable: list[dict[str, Any]] = []

    for u in units:
        key = canonical_unit_key(u)
        if key is None:
            unbucketable.append(u)
            continue
        buckets.setdefault(key, []).append(u)

    collapses = 0
    merged_units: list[dict[str, Any]] = []
    for _key, rows in buckets.items():
        if len(rows) == 1:
            merged_units.append(rows[0])
            continue
        # Sort by rank descending, then by original list position (which
        # is preserved because Python sort is stable). Highest-rank wins
        # as the canonical row; lower-rank rows contribute missing fields.
        rows_sorted = sorted(rows, key=_cross_page_rank, reverse=True)
        primary = rows_sorted[0]
        for other in rows_sorted[1:]:
            merge_units(primary, other)
            collapses += 1
        merged_units.append(primary)

    # Preserve unbucketable rows at the tail. Stable order across the
    # bucketed survivors is not guaranteed (dict iteration order in
    # CPython 3.7+ is insertion order, so it's effectively stable).
    return merged_units + unbucketable, collapses


def _inverse_b4_guard(
    units: list[dict[str, Any]],
    plan_codes: frozenset[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """D16 item 8 — re-route rows whose ``unit_number`` is a known plan code.

    B4 (in the adapter) drops plan-summary rows that duplicate unit rows by
    ``floor_plan_id``. This is the inverse: a row in ``units`` whose
    ``unit_number`` matches any plan code is almost certainly a plan-
    aggregate row that leaked through with a noisy per-unit signal.
    Move it to ``plan_summaries``.

    Returns ``(kept_units, moved_to_plans, count)``.
    """
    if not units or not plan_codes:
        return units, [], 0
    kept: list[dict[str, Any]] = []
    moved: list[dict[str, Any]] = []
    for u in units:
        raw_un = extract_unit_number(u)
        if raw_un is None:
            kept.append(u)
            continue
        norm = normalize_unit_number(raw_un)
        if norm and norm in plan_codes:
            moved.append(u)
        else:
            kept.append(u)
    return kept, moved, len(moved)


def post_process(
    raw_units: list[dict[str, Any]] | None,
    *,
    property_id: str | None = None,
) -> PostProcessResult:
    """Canonicalise + validate a list of raw unit dicts.

    Pipeline (per row): ``infer`` → ``sanity_bound`` → ``is_valid_unit``
    → ``classify``. After the per-row pass, two property-scoped passes
    run on the partitioned results:

      * **Cross-page unit-number dedup** (D16 item 6) — fold rows that
        share a normalised ``unit_number`` (within the same ``building``)
        into a single representative, merging complementary fields.
        Sets ``cross_page_dedup_collapses`` on the result.
      * **Inverse-B4 plan-code guard** (D16 item 8) — re-route rows whose
        ``unit_number`` matches a known ``floor_plan_id`` from the same
        property's plan_summaries to the plan_summaries partition.
        Sets ``inverse_b4_rerouted`` on the result.

    Idempotency: a list already processed by ``post_process`` produces
    the same partition (same ``units`` / ``plan_summaries`` membership)
    on every subsequent call. Two flavours:

      * **Counter-idempotent** (cross-page dedup): the first call merged
        duplicates so the second finds none — ``cross_page_dedup_collapses``
        is 0 on every subsequent call.
      * **Outcome-idempotent** (inverse-B4): a suspect row with a plan-
        code-shaped ``unit_number`` AND a per-unit signal is classified
        as "unit" via the signal path, then rerouted by inverse-B4. On
        a replay, the same flow fires — the row is admitted as "unit"
        again, then rerouted again. ``inverse_b4_rerouted`` stays
        non-zero on each pass but the row keeps landing in the same
        partition (``plan_summaries``). The partition is stable; the
        counter is not.

    Output ordering: bucketed units appear before "unbucketable" rows
    (those with no recognisable ``unit_number``). Within each group the
    relative order from the input is preserved (Python dict insertion
    order in CPython 3.7+). Callers that need the original input order
    must not depend on this pass — but no downstream consumer relies on
    inter-row order today.

    Edge case — Pass-1 promotion without sibling plan-aggregate:
        A row with ``unit_number="2B4"`` and ``floor_plan_id="2B4"``
        but NO sibling row carrying ``floor_plan_id="2B4"`` as a
        plan-aggregate is admitted as a unit. The two-pass classify
        only discovers plan codes from rows that classified as "plan"
        in Pass 1; a row promoted via unit_number on Pass 1 doesn't
        contribute to ``plan_codes``. The inverse-B4 guard (item 8)
        then has no plan_code to match against. In practice the
        Olympic by Windsor-style case is handled correctly because
        plan-aggregate rows DO accompany the suspect unit rows. A
        bare suspect row in isolation cannot be disambiguated without
        external evidence.

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

    # ── Per-row infer + sanity + validity ─────────────────────────────────
    sanitized_rows: list[dict[str, Any]] = []
    for raw in iterator:
        if not isinstance(raw, dict):
            out.rejected.append(({"_raw": str(raw)[:64]}, ["NOT_A_DICT"]))
            continue
        inferred = infer(raw, property_id=property_id)
        sanitized = sanity_bound(inferred)
        if not is_valid_unit(sanitized):
            out.rejected.append((sanitized, absence_reasons(sanitized)))
            continue
        sanitized_rows.append(sanitized)

    if not sanitized_rows:
        return out

    # ── Two-pass classify with plan-code awareness (D16 item 5) ───────────
    #
    # The unit_number-promotion path in classify() consults the property's
    # plan-code set so a row whose unit_number matches a known plan code is
    # denied promotion. The plan-code set is derived from the FIRST pass:
    # rows that classify as "plan" without any plan_codes context.
    #
    # Pass 1 — classify naively to discover the plan-code set.
    first_pass_plans: list[dict[str, Any]] = []
    for row in sanitized_rows:
        if classify(row) == "plan":
            first_pass_plans.append(row)
    plan_codes = _collect_plan_codes(first_pass_plans)

    # Pass 2 — re-classify each row with plan_codes context. A row promoted
    # to unit-level on pass 1 (via natural unit_id) stays unit-level; a row
    # that needed unit_number promotion gets denied if its unit_number
    # matches a discovered plan code.
    for row in sanitized_rows:
        if classify(row, plan_codes=plan_codes) == "unit":
            out.units.append(row)
        else:
            out.plan_summaries.append(row)

    # ── Cross-page unit-number dedup (D16 item 6) ─────────────────────────
    out.units, out.cross_page_dedup_collapses = _cross_page_dedup_units(out.units)

    # ── Inverse-B4 plan-code guard (D16 item 8) ───────────────────────────
    #
    # Recompute plan_codes from the final plan_summaries set — pass-2
    # classification may have moved rows between partitions.
    final_plan_codes = _collect_plan_codes(out.plan_summaries)
    kept_units, moved_to_plans, n_moved = _inverse_b4_guard(
        out.units, final_plan_codes
    )
    out.units = kept_units
    out.plan_summaries.extend(moved_to_plans)
    out.inverse_b4_rerouted = n_moved

    return out
