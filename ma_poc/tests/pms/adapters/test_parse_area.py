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
    stay out (position decides WHICH candidate; the money lookbehind decides
    what is a candidate at all).

2026-07-28 rework
─────────────────
The fix above was correct and is kept.  Two things it broke are repaired here,
both measured against the 4,097 raw pages captured by
run-2026-07-27-full-0d54ca7 (an offline replay — a proxy for the run, not a
measurement of it):

  1. The amenity-context guard started running on four call sites that never
     had it.  It suppressed 31 real areas — every firing on those paths was
     a FALSE suppression — and because ``plan_text`` requires an area to emit
     at all, 2 plan rows vanished.  It is now opt-in; see
     ``AMENITY_GUARD_TABLE``.
  2. ``_extract_rentcafe_option_row`` used to prefer the LABEL-first form and
     stopped doing so, which is the orientation-preference bug d72a6ea fixed
     on ``SQFT_RE`` after three attempts.  Selection is now POSITIONAL for
     everyone, which is the only rule that gets both orientations right; see
     the "ORIENTATION must not decide" pair in ``AREA_TABLE``.
"""

from __future__ import annotations

import pytest

from ma_poc.models.scrape_profile import FieldSelectorMap
from ma_poc.pms.adapters._html_extract import (
    _container_yields_unit,
    extract_units_from_dom,
    extract_with_hints,
)
from ma_poc.pms.adapters._parsing import parse_area
from ma_poc.pms.adapters.plan_text import parse_marketing_plan_text

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
    ("825 sq ft · includes 80 sq ft storage", 825, "earliest valid candidate wins"),
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
    ("726 to 3,100 square feet", 3100, "only the bound with a unit token is a candidate"),
    # ── ORIENTATION must not decide (2026-07-28 rework) ───────────────────
    # Preferring one orientation over the other gets exactly one of the next
    # two rows right.  Only position gets both.  This pair is the whole point
    # of the positional rule; deleting either one lets the bug back in.
    (
        "Unit 402 Rent $1,895 Sq.ft. 725 2 Bed 2 Bath 1,150 sq ft plan",
        725,
        "RentCafe row: the labelled UNIT area precedes the plan's — number-first"
        " preference returns the plan's 1150",
    ),
    (
        "Unit 402 1,118 sq ft 2 Bed 2 Bath Sq Ft: 900 storage+balcony",
        1118,
        "mirror image: the number-first UNIT area precedes the labelled one —"
        " label-first preference returns 900 (this is d72a6ea's defect)",
    ),
    ("Sq.ft. 725", 725, "the bare RentCafe option-row label form"),
    (
        "RENT $3,995 BED / BATH 3 bd / 2.5 ba Square Feet 1,570 Available 8/1/26"
        " 11541 Blucher Ave , 119 Square footage (sq ft) listed includes up to"
        " 158 sq ft of your own garage",
        1570,
        "run row shard_28/17555: number-first preference bound the DISCLAIMER's"
        " 158 over the published 1,570 — a 10x error on a live listing",
    ),
    # ── real rows from run-2026-07-27-full-0d54ca7 ────────────────────────
    (
        "1 Bed , 1 Bath 719 Sq. Ft. 1st Floor Move-in : 07/27 - 08/06",
        719,
        "run row: the old RentCafe rule matched 'Sq. Ft. 1' out of '1st Floor'"
        " and then failed the 150 floor, losing the area (820 occurrences)",
    ),
    (
        "3bd/2.5b Unit 524 3bd/2.5b Premium Rent: $1745 Sq Ft: 1650"
        " Available Date: 07/30/2026 APPLY NOW",
        1650,
        "run row: the old RentCafe rule's (\\d{1,3}(?:,\\d{3})*) alternative"
        " truncated 1650 to 165",
    ),
    (
        "1108 SQ FT 22B-FP Countertops Fireplace Smart Home Technology",
        1108,
        "run row: spaced uppercase SQ FT",
    ),
    (
        "Spacious Kitchen in Two-Bedroom Apartment 1,090 sq ft",
        1090,
        "run row: separator (plan_text used to yield 90)",
    ),
    ("2 beds | 2 baths | 1,056sqft", 1056, "run row: no space before sqft"),
    # Position keeps the area consistent with its siblings: every other field
    # in a row is read with .search() (= first match), so taking the LARGEST
    # area bound it to a different plan than the row's own rent/beds/baths.
    (
        "922 ft² & 1,334 ft²",
        922,
        "run row shard_34/14943, sits beside '$900 & $1,100' — the row's rent"
        " is the FIRST of the pair, so its area must be too (was 1334)",
    ),
    (
        "A2 1 BED 1 BATH $1225 per month | 727SF 1 Available"
        " B1 2 BED 1 BATH $1415 per month | 899SF 5 Available",
        727,
        "run row shard_24/12377: a page-wide blob; beds/baths/rent all come"
        " from plan A2, so the area must too (largest-wins gave 1207)",
    ),
    # ── degenerate input ──────────────────────────────────────────────────
    ("", None, "empty string"),
    (None, None, "None input"),
    ("no numbers at all", None, "no area anywhere"),
]

# ══════════════════════════════════════════════════════════════════════════
# The amenity guard is OPT-IN.  Same input, both settings — the pair is the
# evidence for where each setting belongs.
#
# Guard ON  is used by ``_container_yields_unit`` only (whole unit-CARD text,
#           where a real competing amenity measurement can appear; it has run
#           there since 2026-05-22).
# Guard OFF is used by the plan-text, RentCafe option-row, AppFolio listing
#           and hint-selector paths, which NEVER had it.  7dd85bf switched
#           them all on by accident: over the 4,097 pages captured by
#           run-2026-07-27-full-0d54ca7 that suppressed 11 plan-text areas
#           and 20 listing-row areas, ALL of them the apartment's own, and
#           dropped 2 plan rows outright.
# Every row below is a literal string from that corpus.
# ══════════════════════════════════════════════════════════════════════════
AMENITY_GUARD_TABLE: list[tuple[str, int | None, int | None, str]] = [
    # (text, expected_with_guard, expected_without_guard, why)
    (
        "2 Bedroom with Patio 904 sq ft -",
        None,
        904,
        "run row shard_59/46309: 'Patio' is part of the PLAN NAME; guard-on"
        " dropped this plan row entirely",
    ),
    (
        "You’ll love the huge storage closet and oversized patio in this"
        " 944 sq. ft. apartment! Features include a pass-through kitchen.",
        None,
        944,
        "run row shard_63/36268: marketing prose; guard-on dropped this plan row",
    ),
    (
        "ROBIN WITH PATIO 2 BEDROOM | 1 BATHROOM SQFT 952 LEARN MORE",
        None,
        952,
        "run row: listing-card shape, feature word in the plan name",
    ),
    (
        "3 Bedroom with Patio and Sunroom 1451 sq ft -",
        None,
        1451,
        "run row shard_59/46309",
    ),
    (
        "11C-FP Patio Wingren Side 757 SQ FT Smart Home Technology",
        None,
        757,
        "run row: 'Patio <location>' is this vendor's plan-variant naming",
    ),
    (
        "Balcony Sq Ft: 160",
        None,
        160,
        "the one shape the guard is FOR — an amenity's own measurement, alone."
        " Card text can carry this; prose and per-field selector text cannot,"
        " which is why the setting is per-call-site and not global.",
    ),
]


@pytest.mark.parametrize("text,expected,why", AREA_TABLE, ids=[
    f"{i:02d}" for i in range(len(AREA_TABLE))
])
def test_parse_area_table(text: str | None, expected: int | None, why: str) -> None:
    assert parse_area(text) == expected, why


@pytest.mark.parametrize(
    "text,with_guard,without_guard,why",
    AMENITY_GUARD_TABLE,
    ids=[f"guard{i:02d}" for i in range(len(AMENITY_GUARD_TABLE))],
)
def test_amenity_guard_is_opt_in(
    text: str, with_guard: int | None, without_guard: int | None, why: str
) -> None:
    assert parse_area(text, amenity_guard=True) == with_guard, why
    assert parse_area(text, amenity_guard=False) == without_guard, why


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


# ══════════════════════════════════════════════════════════════════════════
# Call site 2 — plan_text.parse_marketing_plan_text
#
# Literal text from run-2026-07-27-full-0d54ca7 shard_59/46309.  With the
# amenity guard on this whole plan row disappears, because the loop only
# emits when it found an area.
# ══════════════════════════════════════════════════════════════════════════

_PLAN_HTML_PATIO = """
<html><body><div class="fp">
<p>2 Bedroom with Patio 904 sq ft -</p>
<p>Starting at $1,395</p>
</div></body></html>
"""


def test_plan_text_keeps_an_area_whose_plan_name_names_an_amenity() -> None:
    """Anchor line carries the area — exercises the pre-loop ``parse_area``."""
    plans = parse_marketing_plan_text(_PLAN_HTML_PATIO, "https://x.test/")
    assert plans, "the plan row must not be dropped — 'Patio' is part of the name"
    assert [p.get("sqft") for p in plans] == [904]


# Literal text from run-2026-07-27-full-0d54ca7 shard_63/36268.  The area is on
# a PROSE line two lines below the anchor, so this exercises the window loop's
# ``parse_area`` rather than the anchor one.
_PLAN_HTML_PROSE = """
<html><body><div class="fp">
<p>1 Bedroom - 1 Bath</p>
<p>Phase IV</p>
<p>You love the huge storage closet and oversized patio in this 944 sq. ft.
apartment! Features include a pass-through kitchen, foyer with coat closet.</p>
<p>$1,200</p>
</div></body></html>
"""


def test_plan_text_keeps_an_area_stated_in_marketing_prose() -> None:
    """Window-line area — exercises the in-loop ``parse_area``."""
    plans = parse_marketing_plan_text(_PLAN_HTML_PROSE, "https://x.test/")
    assert plans, "the plan row must not be dropped by 'storage closet ... patio'"
    assert [p.get("sqft") for p in plans] == [944]


# ══════════════════════════════════════════════════════════════════════════
# Call site 3 — _extract_rentcafe_option_row, via the production entry point.
#
# `.option-row` appears on only 2 of the 4,097 captured pages and neither
# carries an area, so this regression is LATENT in that corpus: it is pinned
# by construction, on the real row shape taken from
# tests/.../fixtures/entrata/prospectportal_per_plan_option_rows_ariaatella.html
# ══════════════════════════════════════════════════════════════════════════

_RENTCAFE_ROW_HTML = """
<html><body>
<div class="option-row">
  <div class="detail first">Unit 2205</div>
  <div class="detail">Rent $1,344 /month 18mo lease</div>
  <div class="detail">Sq.ft. 682</div>
  <div class="detail">Available Jun 06, 2026</div>
</div>
<div class="option-row">
  <div class="detail first">Unit 1104</div>
  <div class="detail">Rent $1,895 /month</div>
  <div class="detail">Sq.ft. 725</div>
  <div class="detail">2 Bed 2 Bath 1,150 sq ft plan</div>
</div>
</body></html>
"""


def test_rentcafe_option_row_keeps_the_labelled_unit_area() -> None:
    """The labelled per-UNIT area must beat a later number-first plan area.

    Row 2 is the regression: it states the unit's own "Sq.ft. 725" and then
    the plan's "1,150 sq ft".  A number-first-preferring parser stamps 1150 on
    the apartment — the same orientation-preference defect d72a6ea fixed, in
    the opposite direction.
    """
    units, mode = extract_units_from_dom(_RENTCAFE_ROW_HTML, "https://x.test/")
    assert mode == "default"
    by_unit = {u["unit_number"]: u["sqft"] for u in units}
    assert by_unit == {"2205": "682", "1104": "725"}


def test_generic_option_row_skips_the_title_component() -> None:
    """A mobile column label is not an apartment number (PID 23372)."""
    html = _RENTCAFE_ROW_HTML.replace(
        "<html><body>",
        """<html><body>
<div class="option-row title">
  <div class="detail first">Unit</div>
  <div class="detail">Rent</div>
  <div class="detail">Sq.ft.</div>
  <div class="detail">Available</div>
</div>""",
    )

    units, mode = extract_units_from_dom(html, "https://x.test/")

    assert mode == "default"
    assert {u["unit_number"] for u in units} == {"2205", "1104"}


def test_rentcafe_option_row_area_survives_an_amenity_word_in_the_name() -> None:
    """Literal listing-row text from the run; guard-on loses the 952."""
    html = (
        '<html><body><div class="option-row">'
        '<div class="detail first">Unit 12</div>'
        "<div>ROBIN WITH PATIO 2 BEDROOM | 1 BATHROOM SQFT 952 Rent $1,450</div>"
        "</div></body></html>"
    )
    units, _ = extract_units_from_dom(html, "https://x.test/")
    assert [u["sqft"] for u in units] == ["952"]


# ══════════════════════════════════════════════════════════════════════════
# Call site 4 — extract_with_hints' per-field sqft selector.
#
# This branch had no discriminating coverage: the one test that touched it
# (tests/profile/test_dom_hints_per_field.py) asserts a value the regex
# fallback over the container text produces anyway, so neutering the branch
# left it green.  Every case below makes the selector text DISAGREE with the
# container text, so only the selector branch can produce the asserted value.
# ══════════════════════════════════════════════════════════════════════════

_HINTS_HTML = """
<html><body>
<div class="unit-card">
  <span class="unit-no">405</span>
  <span class="price">$1,825/mo</span>
  <span class="beds">2 bed</span>
  <span class="baths">2 bath</span>
  <span class="blurb">Overlooks the 300 sq ft courtyard</span>
  <span class="size">1,180 sq ft</span>
</div>
<div class="unit-card">
  <span class="unit-no">410</span>
  <span class="price">$2,200/mo</span>
  <span class="beds">3 bed</span>
  <span class="baths">2 bath</span>
  <span class="blurb">Private balcony and patio</span>
  <span class="size">Sq. Ft. 1,402</span>
</div>
</body></html>
"""


def test_hint_sqft_selector_beats_the_container_regex() -> None:
    """The nominated element wins, and a thousands separator survives.

    Both cards are built so the container-wide regex CANNOT produce the right
    answer, which is what makes this discriminating — the one pre-existing
    test that touched this branch (tests/profile/test_dom_hints_per_field.py)
    asserts a value the regex fallback yields anyway, so deleting the branch
    entirely left the whole suite green.

      * 405: the courtyard's "300 sq ft" comes FIRST in the container text, so
        the positional regex baseline is 300.
      * 410: "balcony and patio" precedes the area, so the card-text guard
        blanks the regex baseline entirely.

    Before 7dd85bf this branch used ``_SQFT_PATTERN``, which could not read a
    thousands separator at all.
    """
    hints = FieldSelectorMap(
        container=".unit-card",
        unit_id=".unit-no",
        rent=".price",
        sqft=".size",
        bedrooms=".beds",
        bathrooms=".baths",
    )
    units = extract_with_hints(_HINTS_HTML, "https://x.test/", hints)
    assert {u["unit_number"]: u["sqft"] for u in units} == {"405": "1180", "410": "1402"}


def test_hint_sqft_selector_is_not_subject_to_the_amenity_guard() -> None:
    """A feature word in the SAME element must not blank the area.

    The profile nominates this element as THE area field, so there is no
    competing measurement for the guard to protect against — only feature
    words for it to trip over.
    """
    html = (
        '<html><body><div class="c">'
        '<span class="r">$1,500</span><span class="b">2 bed</span>'
        '<span class="s">Patio home — 1,240 sq ft</span>'
        "</div></body></html>"
    )
    hints = FieldSelectorMap(container=".c", rent=".r", bedrooms=".b", sqft=".s")
    units = extract_with_hints(html, "https://x.test/", hints)
    assert [u["sqft"] for u in units] == ["1240"]


def test_hint_sqft_selector_rejects_a_metric_only_value() -> None:
    """A metric-only selector value leaves the regex baseline in place rather
    than laundering a converted number into ``sqft``."""
    html = (
        '<html><body><div class="c">'
        '<span class="r">$1,500</span><span class="b">2 bed</span>'
        '<span class="ba">2 bath</span><span class="s">83.6 m2</span>'
        "</div></body></html>"
    )
    hints = FieldSelectorMap(
        container=".c", rent=".r", bedrooms=".b", bathrooms=".ba", sqft=".s"
    )
    units = extract_with_hints(html, "https://x.test/", hints)
    assert [u["sqft"] for u in units] == [""]
