"""Cross-run sanity checks — compare current record against history.

Flags suspicious changes but does NOT reject. Flags feed into L5
observability and per-property reports.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SanityFlags:
    """Result of cross-run sanity checks for a single unit."""

    rent_swing_pct: float | None = None
    sqft_changed: bool = False
    floor_plan_changed: bool = False
    flags: list[str] = field(default_factory=list)


def check(
    current: dict[str, Any],
    history: dict[str, Any] | None,
) -> SanityFlags:
    """Compare a current unit record against its history.

    Args:
        current: The current unit record dict.
        history: The last accepted record for this unit_id, or None.

    Returns:
        SanityFlags with any detected anomalies.
    """
    if history is None:
        return SanityFlags()

    flags: list[str] = []
    rent_swing: float | None = None
    sqft_changed = False
    fp_changed = False

    # Rent swing check
    curr_rent = _get_rent(current)
    hist_rent = _get_rent(history)
    if curr_rent is not None and hist_rent is not None and hist_rent > 0:
        swing = abs(curr_rent - hist_rent) / hist_rent * 100
        rent_swing = swing
        if swing > 50:
            flags.append("rent_swing_>50pct")
        elif swing > 20:
            flags.append("rent_swing_>20pct")

    # Sqft change check
    curr_sqft = _get_sqft(current)
    hist_sqft = _get_sqft(history)
    if curr_sqft is not None and hist_sqft is not None and hist_sqft > 0:
        diff_pct = abs(curr_sqft - hist_sqft) / hist_sqft * 100
        if diff_pct > 5:
            sqft_changed = True
            flags.append("sqft_changed")

    # Floor plan rename check
    curr_fp = (current.get("floor_plan_type") or "").strip().lower()
    hist_fp = (history.get("floor_plan_type") or "").strip().lower()
    if curr_fp and hist_fp and curr_fp != hist_fp:
        fp_changed = True
        flags.append("floor_plan_renamed")

    return SanityFlags(
        rent_swing_pct=rent_swing,
        sqft_changed=sqft_changed,
        floor_plan_changed=fp_changed,
        flags=flags,
    )


def _get_rent(record: dict[str, Any]) -> float | None:
    """Extract rent value from a record."""
    val = record.get("asking_rent") or record.get("market_rent_low") or record.get("rent")
    if val is not None:
        try:
            return float(val)
        except (ValueError, TypeError):
            pass
    return None


def _get_sqft(record: dict[str, Any]) -> float | None:
    """Extract sqft value from a record."""
    val = record.get("sqft") or record.get("square_feet")
    if val is not None:
        try:
            return float(val)
        except (ValueError, TypeError):
            pass
    return None
