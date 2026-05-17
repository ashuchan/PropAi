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
"""

from __future__ import annotations

import copy
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


# ── Internal helpers ──────────────────────────────────────────────────────────


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
) -> None:
    """Read the logical field via aliases; null all aliases if out-of-bounds.

    Side effects on *unit*:
      * Out-of-bounds values: every alias of the field is set to None.
      * A ``_sanity_dropped`` list records the field-label.
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
    # Out of bounds — null every alias and record the drop.
    _null_all_aliases(unit, alias_tuple)
    dropped: list[str] = unit.setdefault(_SANITY_DROPPED_KEY, [])
    if field_label not in dropped:
        dropped.append(field_label)


# ── Public API ────────────────────────────────────────────────────────────────


def _sanitize_sqft_vs_beds(unit: dict[str, Any]) -> None:
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
    _null_all_aliases(unit, SQFT_KEYS)
    dropped: list[str] = unit.setdefault(_SANITY_DROPPED_KEY, [])
    label = "area_implausible_for_beds"
    if label not in dropped:
        dropped.append(label)


def sanity_bound(unit: dict[str, Any]) -> dict[str, Any]:
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
    """
    if not isinstance(unit, dict):
        return unit
    out: dict[str, Any] = copy.deepcopy(unit)
    _sanitize_field(out, BEDS_KEYS, _BEDS_RANGE, "beds")
    _sanitize_field(out, BATHS_KEYS, _BATHS_RANGE, "baths")
    _sanitize_field(out, SQFT_KEYS, _SQFT_RANGE, "area")
    _sanitize_field(out, RENT_LO_KEYS, _RENT_RANGE, "rent_low")
    _sanitize_field(out, RENT_HI_KEYS, _RENT_RANGE, "rent_high")
    _sanitize_sqft_vs_beds(out)
    return out
