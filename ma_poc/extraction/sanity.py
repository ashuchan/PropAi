"""Sanity bounds — clamp implausible values to absent. Never rejects rows.

A row with ``beds=99, baths=2, area=1019, rent_low=1500`` should not be
dropped just because beds is junk. Sanity nulls the beds field and leaves
the rest intact. ``is_valid_unit`` then decides admissibility from the
remaining dimensions — baths=2 or area=1019 are enough for it to pass.

Production-tuned bounds (mirror the existing schema_gate / schema_v2 ranges):

  * ``beds``        ∈ [0, 7]         — studios start at 0; 7BR is the max
                                       observed apartment plan
  * ``baths``       ∈ [0, 10]        — 0 means "no separate bathroom"
  * ``area``        ∈ [150, 10000]   — ABSENT sentinel (-1) preserved
  * ``rent_low``    ∈ [200, 50000]   — $200 floor catches deposit/fee
                                       leakage; $50k ceiling catches
                                       commas-as-decimals corruption
  * ``rent_high``   ∈ [200, 50000]   — same

Field-aliases: sanity reads the logical field via canonical's alias
resolution (case-insensitive across BEDS_KEYS / BATHS_KEYS / etc.).
When out-of-range, EVERY alias of that field is nulled — including the
V2 canonical name AND non-canonical variants — so a subsequent
``get_numeric`` finds nothing rather than falling through to a stale
alias.

ABSENT sentinel: an explicit ``area=-1`` (the "we tried, source didn't
have it" marker) is preserved through sanity. ``get_numeric`` already
treats it as not-present, so it never reaches the bounds check.

This module is **pure**: ``sanity_bound(unit)`` returns a new dict,
never mutates its input.

Observability (2026-07-28, task #69)
------------------------------------
Every clamp used to be SILENT. A $1 rent, a phone number read as rent, an
``area=45`` — each was nulled into a value byte-identical to genuine
operator absence, so the run's ~7.3K null rents and ~7.8K area sentinels
were undecomposable and no value-plausibility audit could see outside the
bands BY CONSTRUCTION.

``sanity_bound`` now RECORDS every clamp it fires:

  * per-row — a ``_sanity_clamped`` list of
    ``{field, reason, value, bounds, tier}`` dicts carrying the
    **pre-clamp value**. In-pipeline only; the V2 formatter emits a fixed
    key set so this never reaches ``properties.json`` (the existing
    ``_sanity_dropped`` marker has the same fate — 0 occurrences across
    the 104,964 rows of run-2026-07-27-full-0d54ca7).
  * per-run — a process-wide :class:`ClampLedger` keyed by
    ``(field, tier, reason)`` plus a per-property index, so
    "which tier produces the most clamped rents" is one call
    (:meth:`ClampLedger.by_field`).

The ``<field>_raw`` twins do NOT already hold the pre-clamp value for a
sanity clamp: ``ma_poc/scripts/runners/jugnu.py`` snapshots ``_raw_src``
from the unit dict at FORMAT time, which is downstream of the adapter's
``post_process`` → ``sanity_bound``, by which point every alias of the
clamped field is already ``None``. (The formatter's own later clamp —
``_format_area``'s [150, 10000] → -1 — does survive into ``area_raw``;
that is why 29 of the run's 7,752 area sentinels still carry a numeric
``area_raw`` below 150.)

**Nothing about the emitted values changes.** The bounds are untouched;
this module only stops throwing away the evidence.
"""

from __future__ import annotations

import copy
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field as _dc_field
from typing import Any, Final

from ma_poc.extraction.canonical import (
    BATHS_KEYS,
    BEDS_KEYS,
    RENT_HI_KEYS,
    RENT_LO_KEYS,
    SQFT_KEYS,
    get_numeric,
)

# ── Bounds ────────────────────────────────────────────────────────────────────

_BEDS_RANGE: Final[tuple[float, float]] = (0.0, 7.0)
_BATHS_RANGE: Final[tuple[float, float]] = (0.0, 10.0)
_SQFT_RANGE: Final[tuple[float, float]] = (150.0, 10_000.0)
_RENT_RANGE: Final[tuple[float, float]] = (200.0, 50_000.0)

# Cross-field sqft floor by bedroom count — generous lower bounds that catch
# obvious Tier-4 LLM hallucinations (e.g. "2BR @ 310 sqft" = LLM extracted
# the deposit amount as sqft) without rejecting micro-units. Tuned 2026-05-13
# against the 1,156 "sqft too small for beds" rows surfaced in validation;
# every observed case is well outside these floors.
#
# Margins are deliberately wide. Real-world minimums per HUD compact-living
# data: 0BR ≥ 200, 1BR ≥ 350, 2BR ≥ 500, 3BR ≥ 700, 4+BR ≥ 900. The bad rows
# averaged 300 sqft for 3BR (impossible) and 320 sqft for 2BR (impossible).
_SQFT_FLOOR_BY_BEDS: Final[dict[int, int]] = {
    0: 200,
    1: 350,
    2: 500,
    3: 700,
    4: 900,
    5: 1_100,
    6: 1_300,
    7: 1_500,
}


# ── Provenance marker ────────────────────────────────────────────────────────

#: Field name on the output dict carrying a list of field-labels that
#: sanity nulled. Surfaced via observability so analytics can group
#: out-of-bound patterns by source.
_SANITY_DROPPED_KEY: Final[str] = "_sanity_dropped"

#: Field name on the output dict carrying the FULL clamp evidence — one
#: dict per fired clamp with the pre-clamp value. Additive sibling of
#: ``_sanity_dropped`` (which stays a bare list of labels so no existing
#: consumer or test changes shape).
_SANITY_CLAMPED_KEY: Final[str] = "_sanity_clamped"


# ── Clamp evidence: reason codes ─────────────────────────────────────────────

#: The value was below the field's lower bound (a $1 rent, ``area=45``,
#: a deposit/fee leaked into the rent column).
REASON_BELOW_MIN: Final[str] = "BELOW_MIN"

#: The value was above the field's upper bound (a phone number or a
#: comma-as-decimal corruption read as rent; a price read as sqft).
REASON_ABOVE_MAX: Final[str] = "ABOVE_MAX"

#: Cross-field: sqft is inside [150, 10000] but impossibly small for the
#: (trusted) bed count — the ``_SQFT_FLOOR_BY_BEDS`` pass.
REASON_IMPLAUSIBLE_FOR_BEDS: Final[str] = "IMPLAUSIBLE_FOR_BEDS"

#: Tier label used when nothing in scope identified the producing tier.
UNKNOWN_TIER: Final[str] = "UNKNOWN"

#: Aliases carrying the producing tier on a raw adapter unit dict, in
#: precedence order. Checked case-insensitively.
_TIER_KEYS: Final[tuple[str, ...]] = (
    "extraction_tier",
    "_extraction_tier",
    "tier_used",
    "_tier",
    "tier",
)

#: Per-(field, tier, reason) example cap. Bounded so a pathological run
#: cannot grow the ledger without limit; counts stay exact regardless.
_EXAMPLES_PER_KEY: Final[int] = 25


# ── Clamp evidence: ambient tier context ─────────────────────────────────────

_TIER_CTX: ContextVar[str | None] = ContextVar("sanity_clamp_tier", default=None)


@contextmanager
def clamp_tier_context(tier: str | None) -> Iterator[None]:
    """Attribute every clamp fired inside the block to *tier*.

    For callers (the scraper, an adapter) that know the producing tier but
    do not stamp it on each unit dict. An explicit ``tier=`` argument to
    :func:`sanity_bound` still wins; the unit dict's own tier aliases are
    consulted only when neither is set.

    Args:
        tier: Tier label, e.g. ``"TIER_1_API_RENTCAFE_SECURECAFE"``. A
            falsy value clears the context rather than recording ``""``.

    Yields:
        None. The previous context value is restored on exit, including
        when the body raises.
    """
    token = _TIER_CTX.set(str(tier) if tier else None)
    try:
        yield
    finally:
        _TIER_CTX.reset(token)


# ── Clamp evidence: the ledger ───────────────────────────────────────────────


def _finite_or_none(value: float) -> float | None:
    """``None`` for ±inf / NaN, the value otherwise. Strict-JSON guard."""
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


@dataclass(frozen=True)
class ClampSample:
    """One recorded clamp, with the value that was thrown away.

    Attributes:
        field: Logical field label — ``beds`` / ``baths`` / ``area`` /
            ``rent_low`` / ``rent_high``.
        tier: Producing tier, or ``"UNKNOWN"``.
        reason: One of ``BELOW_MIN`` / ``ABOVE_MAX`` /
            ``IMPLAUSIBLE_FOR_BEDS``.
        value: The pre-clamp numeric value that was nulled.
        bounds: ``(low, high)`` the value was tested against. For
            ``IMPLAUSIBLE_FOR_BEDS`` this is ``(floor_for_beds, inf)``.
        property_id: Canonical property id when the caller supplied one.
        unit_id: Best-effort unit anchor, for joining back to a row.
    """

    field: str
    tier: str
    reason: str
    value: float
    bounds: tuple[float, float]
    property_id: str | None = None
    unit_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Strict-JSON-safe dict form.

        ``bounds`` becomes a 2-element list; an infinite bound (the open
        upper end of the sqft-vs-beds floor) becomes ``None`` rather than
        the ``Infinity`` literal, which ``json.dumps`` emits happily and
        every non-Python consumer rejects.
        """
        return {
            "field": self.field,
            "tier": self.tier,
            "reason": self.reason,
            "value": self.value,
            "bounds": [_finite_or_none(self.bounds[0]), _finite_or_none(self.bounds[1])],
            "property_id": self.property_id,
            "unit_id": self.unit_id,
        }


@dataclass
class ClampLedger:
    """Process-wide, thread-safe aggregate of every clamp that fired.

    The point of the whole task: a clamped value must be countable and
    attributable, not merely absent. Three views, all exact counts:

      * ``counts``      — ``Counter`` keyed by ``(field, tier, reason)``.
      * ``per_property``— ``{property_id: Counter[(field, reason)]}``, so
        a downstream consumer (task #56's area-absence labels) can ask
        "was THIS property's area clamped, or genuinely not published?"
      * ``examples``    — up to :data:`_EXAMPLES_PER_KEY` :class:`ClampSample`
        per key, each carrying the pre-clamp value.
    """

    counts: Counter[tuple[str, str, str]] = _dc_field(default_factory=Counter)
    per_property: dict[str, Counter[tuple[str, str]]] = _dc_field(default_factory=dict)
    examples: dict[tuple[str, str, str], list[ClampSample]] = _dc_field(
        default_factory=dict
    )
    _lock: threading.Lock = _dc_field(default_factory=threading.Lock, repr=False)

    def record(self, sample: ClampSample) -> None:
        """Add *sample*. Thread-safe; counts are exact, examples capped."""
        key = (sample.field, sample.tier, sample.reason)
        with self._lock:
            self.counts[key] += 1
            bucket = self.examples.setdefault(key, [])
            if len(bucket) < _EXAMPLES_PER_KEY:
                bucket.append(sample)
            if sample.property_id:
                pp = self.per_property.setdefault(sample.property_id, Counter())
                pp[(sample.field, sample.reason)] += 1

    def total(self) -> int:
        """Total clamps recorded across every field / tier / reason."""
        with self._lock:
            return sum(self.counts.values())

    def by_field(self, field_label: str) -> Counter[str]:
        """Tier → count for one field, most-clamped tier first.

        This is the query the task names: ``by_field("rent_low")`` answers
        "which tier produces the most clamped rents".
        """
        with self._lock:
            out: Counter[str] = Counter()
            for (fld, tier, _reason), n in self.counts.items():
                if fld == field_label:
                    out[tier] += n
            return out

    def by_tier(self, tier: str) -> Counter[str]:
        """Field → count for one tier, most-clamped field first."""
        with self._lock:
            out: Counter[str] = Counter()
            for (fld, tr, _reason), n in self.counts.items():
                if tr == tier:
                    out[fld] += n
            return out

    def rows(self) -> list[dict[str, Any]]:
        """Flat, sorted, JSON-safe rows — the aggregable export.

        One row per ``(field, tier, reason)``, descending by count, each
        carrying its capped example list.
        """
        with self._lock:
            items = sorted(
                self.counts.items(), key=lambda kv: (-kv[1], kv[0])
            )
            return [
                {
                    "field": fld,
                    "tier": tier,
                    "reason": reason,
                    "n": n,
                    "examples": [s.to_dict() for s in self.examples.get(key, [])],
                }
                for key, n in items
                for fld, tier, reason in (key,)
            ]

    def to_dict(self) -> dict[str, Any]:
        """Full JSON-safe snapshot: totals, per-key rows, per-property index."""
        rows = self.rows()
        with self._lock:
            per_property = {
                pid: {f"{fld}:{reason}": n for (fld, reason), n in ctr.items()}
                for pid, ctr in self.per_property.items()
            }
            by_field: Counter[str] = Counter()
            by_tier: Counter[str] = Counter()
            for (fld, tier, _reason), n in self.counts.items():
                by_field[fld] += n
                by_tier[tier] += n
            total = sum(self.counts.values())
        return {
            "total_clamps": total,
            "properties_affected": len(per_property),
            "by_field": dict(by_field.most_common()),
            "by_tier": dict(by_tier.most_common()),
            "rows": rows,
            "per_property": per_property,
        }

    def reset(self) -> None:
        """Drop everything. For tests and for per-run isolation."""
        with self._lock:
            self.counts.clear()
            self.per_property.clear()
            self.examples.clear()


_LEDGER: Final[ClampLedger] = ClampLedger()


def clamp_ledger() -> ClampLedger:
    """The process-wide :class:`ClampLedger` singleton."""
    return _LEDGER


def reset_clamp_ledger() -> None:
    """Clear the process-wide ledger (test / per-run isolation helper)."""
    _LEDGER.reset()


# ── Internal helpers ──────────────────────────────────────────────────────────


def _resolve_tier(unit: dict[str, Any], explicit: str | None) -> str:
    """Best-effort producing-tier label for a clamp record.

    Precedence: explicit argument → ambient
    :func:`clamp_tier_context` → the unit dict's own tier aliases →
    :data:`UNKNOWN_TIER`. Never raises; never returns an empty string.
    """
    if explicit:
        return str(explicit)
    ambient = _TIER_CTX.get()
    if ambient:
        return str(ambient)
    lowered = {
        k.lower(): v for k, v in unit.items() if isinstance(k, str)
    }
    for key in _TIER_KEYS:
        val = lowered.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return UNKNOWN_TIER


def _unit_anchor(unit: dict[str, Any]) -> str | None:
    """Best-effort per-row anchor so an example can be joined back."""
    for key in ("unit_id", "unit_number", "_unit_number", "unit_name"):
        val = unit.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text[:64]
    return None


def _record_clamp(
    unit: dict[str, Any],
    *,
    field_label: str,
    reason: str,
    value: float,
    bounds: tuple[float, float],
    tier: str | None,
    property_id: str | None,
) -> None:
    """Write one clamp to the per-row list AND the process-wide ledger.

    Side effects:
      * appends a ``{field, reason, value, bounds, tier}`` dict to
        ``unit[_SANITY_CLAMPED_KEY]`` (created on first clamp);
      * records a :class:`ClampSample` on :func:`clamp_ledger`.

    Never raises — observability must not be able to fail an extraction.
    """
    resolved_tier = _resolve_tier(unit, tier)
    evidence: list[dict[str, Any]] = unit.setdefault(_SANITY_CLAMPED_KEY, [])
    evidence.append(
        {
            "field": field_label,
            "reason": reason,
            "value": value,
            "bounds": [bounds[0], bounds[1]],
            "tier": resolved_tier,
        }
    )
    _LEDGER.record(
        ClampSample(
            field=field_label,
            tier=resolved_tier,
            reason=reason,
            value=value,
            bounds=bounds,
            property_id=str(property_id) if property_id else None,
            unit_id=_unit_anchor(unit),
        )
    )


def _null_all_aliases(unit: dict[str, Any], alias_tuple: tuple[str, ...]) -> None:
    """Set every alias of a logical field to ``None`` in *unit*.

    Case-insensitive — ``BEDS``, ``Beds``, ``beds`` all caught when
    ``alias_tuple`` contains ``"beds"``. Mutates *unit* in place; caller
    is responsible for working on a copy.
    """
    aliases_lower = {k.lower() for k in alias_tuple}
    for k in list(unit.keys()):
        if isinstance(k, str) and k.lower() in aliases_lower:
            unit[k] = None


def _sanitize_field(
    unit: dict[str, Any],
    alias_tuple: tuple[str, ...],
    bounds: tuple[float, float],
    field_label: str,
    *,
    tier: str | None = None,
    property_id: str | None = None,
) -> None:
    """Read the logical field via aliases; null all aliases if out-of-bounds.

    Side effects on *unit*:
      * Out-of-bounds values: every alias of the field is set to None.
      * A ``_sanity_dropped`` list records the field-label.
      * A ``_sanity_clamped`` list records the PRE-CLAMP value with a
        ``BELOW_MIN`` / ``ABOVE_MAX`` reason code, and the same record
        lands in the process-wide :func:`clamp_ledger`.
      * In-range and absent (None / ABSENT) values: no change.
    """
    value = get_numeric(unit, alias_tuple)
    if value is None:
        # Absent — nothing to check. (ABSENT sentinel resolves to None
        # via get_numeric's sentinel handling.)
        return
    low, high = bounds
    if low <= value <= high:
        # In range — keep as-is.
        return
    # Out of bounds — RECORD the evidence before destroying it, then null
    # every alias. Recording first matters: _null_all_aliases wipes the
    # only copy of the value, and the ``<field>_raw`` twin is snapshotted
    # downstream of here, so nothing else in the pipeline retains it.
    _record_clamp(
        unit,
        field_label=field_label,
        reason=REASON_BELOW_MIN if value < low else REASON_ABOVE_MAX,
        value=value,
        bounds=bounds,
        tier=tier,
        property_id=property_id,
    )
    _null_all_aliases(unit, alias_tuple)
    dropped: list[str] = unit.setdefault(_SANITY_DROPPED_KEY, [])
    if field_label not in dropped:
        dropped.append(field_label)


# ── Public API ────────────────────────────────────────────────────────────────


def _sanitize_sqft_vs_beds(
    unit: dict[str, Any],
    *,
    tier: str | None = None,
    property_id: str | None = None,
) -> None:
    """Null sqft when it's impossibly small for the (already-sanitised) beds.

    Catches the 2026-05-13 validation-pass finding of 1,156 rows with
    "sqft too small for bed count" (Tier-4 LLM_DOM dominant; LLM
    confused the deposit/fee amount with sqft). beds is trusted; sqft
    is the suspect field.

    Side effects on *unit*:
      * If beds is present and sqft is present and sqft < floor(beds):
        every sqft alias set to None and ``_sanity_dropped`` annotated
        with ``"area_implausible_for_beds"``.
      * Otherwise no change.

    Pre-condition: ``_sanitize_field`` has already run on both beds and
    sqft, so any out-of-absolute-range values are already None.
    """
    beds = get_numeric(unit, BEDS_KEYS)
    sqft = get_numeric(unit, SQFT_KEYS)
    if beds is None or sqft is None:
        return
    try:
        beds_int = int(beds)
    except (TypeError, ValueError):
        return
    floor = _SQFT_FLOOR_BY_BEDS.get(beds_int)
    if floor is None or sqft >= floor:
        return
    # Record before destroying — same rule as _sanitize_field. The upper
    # bound is open here (the rule is "too small for this bed count"), so
    # the recorded bounds are (floor_for_beds, +inf).
    _record_clamp(
        unit,
        field_label="area",
        reason=REASON_IMPLAUSIBLE_FOR_BEDS,
        value=sqft,
        bounds=(float(floor), float("inf")),
        tier=tier,
        property_id=property_id,
    )
    _null_all_aliases(unit, SQFT_KEYS)
    dropped: list[str] = unit.setdefault(_SANITY_DROPPED_KEY, [])
    label = "area_implausible_for_beds"
    if label not in dropped:
        dropped.append(label)


def sanity_bound(
    unit: dict[str, Any],
    *,
    tier: str | None = None,
    property_id: str | None = None,
) -> dict[str, Any]:
    """Apply production sanity bounds to a unit dict. Return a new dict.

    Reads each dimensional / pricing field via its alias group. Values
    outside the production-tuned range are nulled across every alias of
    that field. In-range and absent values are preserved.

    Does NOT reject the unit — even if every field gets nulled, the
    returned dict still exists. ``is_valid_unit`` decides admissibility
    downstream.

    A ``_sanity_dropped`` list on the output dict records which fields
    were nulled (one entry per logical field). Useful for observability
    and for the canary's per-property diff.

    Idempotent: a second call on the result is a no-op (every field is
    either in-range or already None).

    Non-mutation: input dict is deep-copied; mutations to the result
    never propagate back.

    2026-05-13: added cross-field ``area_implausible_for_beds`` null pass
    AFTER the per-field absolute-range pass. Catches Tier-4 LLM-DOM
    hallucinations where the LLM confused deposit/fee amounts with sqft
    (1,156 rows in the 2026-05-13 daily output had 2BR/3BR units with
    impossibly small sqft). Beds is trusted; sqft alone is nulled.

    2026-07-28 (#69): every clamp is now RECORDED — see the module
    docstring. ``tier`` / ``property_id`` only label those records; they
    never affect which values are kept. The emitted values are
    bit-identical to the pre-#69 behaviour.

    Args:
        unit: Raw unit dict in any producer shape. Non-dicts pass through.
        tier: Producing tier label for clamp attribution. Falls back to
            :func:`clamp_tier_context`, then the unit's own tier aliases,
            then ``"UNKNOWN"``.
        property_id: Canonical property id, recorded on each clamp so a
            consumer can ask "was this property's area clamped?".
    """
    if not isinstance(unit, dict):
        return unit
    out: dict[str, Any] = copy.deepcopy(unit)
    ctx = {"tier": tier, "property_id": property_id}
    _sanitize_field(out, BEDS_KEYS, _BEDS_RANGE, "beds", **ctx)
    _sanitize_field(out, BATHS_KEYS, _BATHS_RANGE, "baths", **ctx)
    _sanitize_field(out, SQFT_KEYS, _SQFT_RANGE, "area", **ctx)
    _sanitize_field(out, RENT_LO_KEYS, _RENT_RANGE, "rent_low", **ctx)
    _sanitize_field(out, RENT_HI_KEYS, _RENT_RANGE, "rent_high", **ctx)
    _sanitize_sqft_vs_beds(out, **ctx)
    return out
