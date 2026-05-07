"""Regression tests for ``infer_bed_bath_from_name``.

The inference helper exists to fill bed/bath gaps from the floor-plan
name when the adapter / API didn't supply them. It is intentionally
narrow — every output value must be grounded in a literal substring of
the input. These cases are sourced from the production
``Floorplan- comparisons.csv`` corpus and live ``units.floor_plan_name``
values; new patterns should be added here, not by loosening the regexes.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters._parsing import infer_bed_bath_from_name


@pytest.mark.parametrize(
    "name,expected",
    [
        # ── Explicit pair patterns ──
        ("1BD/1BR-1", (1, 1.0)),
        ("2BR/2BA", (2, 2.0)),
        ("3BD/2.5BR-3", (3, 2.5)),
        ("3 BR / 2.5 BA", (3, 2.5)),
        ("1 Bed 1 Bath", (1, 1.0)),
        ("2 Bedroom 2 Bathroom", (2, 2.0)),
        # ── NxN shorthand ──
        ("1x1", (1, 1.0)),
        ("2x2.5", (2, 2.5)),
        ("3 X 2", (3, 2.0)),
        # ── N/N slash shorthand ──
        ("1/1", (1, 1.0)),
        ("2/2.5", (2, 2.5)),
        # ── Studios ──
        ("Studio", (0, None)),
        ("Studio - 1 Bath", (0, 1.0)),
        ("Micro Studio", (0, None)),
        ("Efficiency", (0, None)),
        ("Junior Studio", (0, None)),
        ("0BR/1BA", (0, 1.0)),
        # ── Single field passes ──
        ("1BR", (1, None)),
        ("2 Bed", (2, None)),
        ("Three Bedroom", (3, None)),
        ("One Bedroom One Bath", (1, 1.0)),
        # ── Marketing names with embedded counts ──
        ("Bainbridge, w/d loft", (None, None)),
        ("The Pedernales", (None, None)),
        ("Wagner", (None, None)),
        ("A1", (None, None)),
        ("TH1", (None, None)),
        ("C4 - Denver Carriage House", (None, None)),
        # ── Names that should NOT false-positive ──
        ("Five Star View", (None, None)),  # "Five" without a bed token
        ("Loft 12B", (None, None)),  # 12 is too high for beds
        # ── Edge inputs ──
        ("", (None, None)),
        (None, (None, None)),
        ("   ", (None, None)),
        # Out-of-range sanity rejection — a regex hit but not plausible.
        ("9 Bedroom Penthouse", (None, None)),
    ],
)
def test_infer_bed_bath_from_name(
    name: str | None, expected: tuple[int | None, float | None]
) -> None:
    assert infer_bed_bath_from_name(name) == expected


def test_inference_is_pure() -> None:
    """The helper must never raise on non-string / unexpected input."""
    # Pass values that aren't str — should silently return (None, None).
    assert infer_bed_bath_from_name(123) == (None, None)  # type: ignore[arg-type]
    assert infer_bed_bath_from_name([]) == (None, None)  # type: ignore[arg-type]
    assert infer_bed_bath_from_name({}) == (None, None)  # type: ignore[arg-type]
