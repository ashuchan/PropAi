"""Canonical free-text area parser — ``_parsing.parse_area`` (2026-07-28).

Defect this pins
────────────────
A string that publishes BOTH units returned the square-metre number instead
of the square-foot one.  Reproduced against HEAD ba68279 on the real
production entry point::

    _container_yields_unit("1 Bed 1 Bath 2,000 sq ft 186 m2")["sqft"] == "186"
    _container_yields_unit("1 Bed 1 Bath 2,000 sq ft (186 m2)")["sqft"] == ""
    _container_yields_unit("2 Bed 2 Bath 1,118 sq ft Balcony Sq Ft: 160")["sqft"] == "160"
    _container_yields_unit("1 Bed 1 Bath 1,200 sq ft")["sqft"] == ""

Root cause — ONE cause, four symptoms: ``_SQFT_PATTERN``'s ``(?<![\\$\\d,])``
lookbehind made a thousands separator unmatchable ("1,118 sq ft" matched
nothing at all), and the label-first fallback that then fired binds the
number AFTER the "sq ft" token — the metric value on a dual-unit string,
the balcony area next to a balcony — with no bounds check.

The three regexes that independently did this job are replaced by one
unit-aware parser.  Selection is by which unit token OWNS the number, so a
metric number can never be returned as square feet.

Guard-rails this file deliberately encodes, because each was broken by an
earlier "fix" to this same module:
  * a ``\\b`` before the unit token cannot exist after a digit — "1,200ft2"
    must still parse (the number-first form therefore has no leading \\b);
  * but the LABEL-first form must have one, or "Loft 2: 350" reads as 350;
  * a positional-only rule reintroduces the "$1145 Sq Ft" bug — money must
    stay out.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters._html_extract import _container_yields_unit
from ma_poc.pms.adapters._parsing import parse_area

# ══════════════════════════════════════════════════════════════════════════
# The table. Every row is (input, expected, why).
# ══════════════════════════════════════════════════════════════════════════
AREA_TABLE: list[tuple[str | None, int | None, str]] = [
    # ── dual-unit: the assigned defect ────────────────────────────────────
    ("900 sqft (83.6 m2)", 900, "dual-unit, imperial first"),
    ("83.6 m2 (900 sqft)", 900, "dual-unit, metric first"),
    ("2,000 sq ft 186 m2", 2000, "dual-unit, no parens — the reproduced bug"),
    ("2,000 sq ft (186 m2)", 2000, "dual-unit, parens + thousands separator"),
    ("2,000 sq ft / 186 sq m", 2000, "dual-unit, slash separator"),
    ("1,615 square feet (150 square metres)", 1615, "spelled-out both units"),
    # ── metric only → absent, never converted (see parse_area docstring) ──
    ("83.6 m2", None, "metric ONLY → absent sentinel, not a converted value"),
    ("186 sq m", None, "metric only, 'sq m'"),
    ("Square Metres: 186", None, "metric label-first"),
    ("186 m²", None, "metric only, unicode superscript"),
    # Label-first + metric: the number is owned by the token that FOLLOWS it.
    # This is the purest form of the defect — an imperial label sitting in
    # front of a metric figure. Returning 186 here is a 10.8x error.
    ("Square Feet 186 m2", None, "label-first, number owned by the following m2"),
    ("Sq. Ft. 200 sq m", None, "label-first, number owned by the following 'sq m'"),
    # ── number-first forms that must keep working ─────────────────────────
    ("1,200ft2", 1200, "no space + ASCII ft2 + separator — a \\b would fail here"),
    ("1200 ft2", 1200, "ASCII ft2"),
    ("1200 ft²", 1200, "unicode superscript"),
    ("1,152 sq ft | from $1700", 1152, "thousands separator (used to yield 152)"),
    ("1,128-square-foot", 1128, "hyphenated"),
    ("S1-449sf-D1", 449, "bare 'sf' inside a plan name (run row, pid 262355)"),
    ("Studio, 1 Bath 565 SF 01", 565, "bare 'SF' (run row, pid 3132)"),
    ("288 sq. ft. | from $1125", 288, "dotted, plan-text free-text form"),
    ("1 BR / 1 BA - 611 sq ft - $605", 611, "canonical number-first"),
    # ── label-first forms ─────────────────────────────────────────────────
    ("Square Feet: 850", 850, "label-first with colon"),
    ("SqFt 833", 833, "label-first, no colon"),
    ("Sq. Ft. 700", 700, "label-first, dotted"),
    ("Sq.ft. 1,025", 1025, "label-first with separator (RentCafe option row)"),
    # ── must NOT match ────────────────────────────────────────────────────
    ("$1145 Sq Ft", None, "money — a label after a price is not an area"),
    ("$1,145 Sq Ft", None, "money with a thousands separator"),
    ("Loft 2: 350", None, "'Loft' + '2' is not the 'ft2' unit token"),
    ("Balcony Sq Ft: 160", None, "amenity area only"),
    ("sfgate.com", None, "'sf' inside a word"),
    ("450 sfh", None, "'sf' inside a token"),
    ("100 sq ft storage included", None, "below the 150 floor"),
    ("12,000 sq ft clubhouse", None, "above the 10000 ceiling"),
    # ── competing measurements in one string ──────────────────────────────
    ("Unit 402 1,118 sq ft Balcony Sq Ft: 60", 1118, "unit area beats balcony"),
    ("2 bed 1,118 sq ft, Balcony Sq Ft: 160", 1118, "balcony >=150 still loses"),
    ("825 sq ft · includes 80 sq ft storage", 825, "largest valid candidate wins"),
    ("12,000 sq ft clubhouse and a 900 sq ft home", 900, "out-of-range dropped, 900 kept"),
    (
        "2 Bedroom / 2 Bath Price: $1290-$1295 Deposit: $200 Square Feet: 980",
        980,
        "a deposit before the label must not become the area",
    ),
    (
        "Price Range $1891 ~ $2087 BR 2 Utly None SqFt 833 Avail 7/2/2026",
        833,
        "label-first with rents before it",
    ),
    ("1 bed 1 bath 655 ft² Rent: $808 Deposit: $300", 655, "superscript beside money"),
    ("726 to 3,100 square feet", 3100, "prose range takes the larger"),
    # ── degenerate input ──────────────────────────────────────────────────
    ("", None, "empty string"),
    (None, None, "None input"),
    ("no numbers at all", None, "no area anywhere"),
]


@pytest.mark.parametrize("text,expected,why", AREA_TABLE, ids=[
    f"{i:02d}" for i in range(len(AREA_TABLE))
])
def test_parse_area_table(text: str | None, expected: int | None, why: str) -> None:
    assert parse_area(text) == expected, why


# ══════════════════════════════════════════════════════════════════════════
# End-to-end through the production entry point — these are the four rows
# that failed against HEAD.
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1 Bed 1 Bath 2,000 sq ft 186 m2", "2000"),
        ("1 Bed 1 Bath 2,000 sq ft (186 m2)", "2000"),
        ("2 Bed 2 Bath 1,118 sq ft Balcony Sq Ft: 160", "1118"),
        ("1 Bed 1 Bath 1,200 sq ft", "1200"),
        ("1 Bed 1 Bath 900 sqft (83.6 m2)", "900"),
    ],
)
def test_container_yields_unit_binds_the_imperial_number(text: str, expected: str) -> None:
    """The metre value must never reach ``sqft``, and a separator must not eat it."""
    u = _container_yields_unit(text)
    assert u is not None
    assert u["sqft"] == expected


def test_container_metric_only_leaves_sqft_empty() -> None:
    """Metric-only text yields NO sqft rather than a converted number.

    ``area`` is a source-measured square-foot integer sitting beside
    ``area_raw``; a silent 83.6 m2 -> 900 conversion would be
    indistinguishable downstream from a real "900 sq ft" measurement.
    """
    u = _container_yields_unit("1 Bed 1 Bath 83.6 m2")
    if u is not None:
        assert u["sqft"] == ""
