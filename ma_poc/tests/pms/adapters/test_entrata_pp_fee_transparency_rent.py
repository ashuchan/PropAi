"""Entrata Prospect Portal fee-transparency dual-price rent semantics.

DEFECT (run-2026-07-27-full-0d54ca7): 107 output rows shipped
``rent_low > rent_high`` — an inverted range, wrong by definition. All
107 were Entrata (96 ``TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL`` on Marlowe
Peoria Place I, 11 ``TIER_1_DOM_ENTRATA_PP_SSR_PLAN_LEVEL`` on Cyan on
Peachtree).

ROOT CAUSE: ``_pp_money_low_high`` treated the money tokens inside a
Prospect-Portal rent cell as a monotonic ``(first, last)`` range. On
Entrata's *fee-transparency* template that cell holds TWO DIFFERENT
QUANTITIES, not a range:

    <span class="fee-transparency-text">
      Total Monthly Leasing Price <span class="...-emphasized">$1,583.98</span>
      <span class="pp-base-rent pp-base-rent-line">
        <span class="pp-base-rent-label">Base Rent:</span>
        <span class="pp-base-rent-amount">$1,391/month</span>
      </span>
    </span>

marlowepeoriaplace.com states the definitions on its own /floorplans
page (live capture 2026-07-28, embedded in the index fixture):

    * "Base Rent: The monthly rent for the rental home."
    * "Total Monthly Leasing Price: Base Rent plus fixed, mandatory
       monthly fees."

So the first token is a GROSS figure (rent + fees) and the second is
the advertised asking rent — hence ``low`` (gross) > ``high`` (rent).
The operator's own units-table column header for the advertised price
is literally ``Base Rent``.

FIX: the rent range is taken from the labelled Base-Rent portion only.
The Total Monthly Leasing Price is a different quantity and is NOT
written into the rent range (swapping the two would have hidden the
bug — a $1,391 rent is not a "$1,391-$1,583 range").

Fixtures are unmodified live captures (curl_cffi chrome120, plain
unauthenticated GET, 2026-07-28):
  * marlowepeoriaplace.com /peoria/marlowe-peoria-place/floorplans/
    a1-794432/fp_name/occupancy_type/conventional/   (.option-row, 8 units)
  * marlowepeoriaplace.com /peoria/marlowe-peoria-place/conventional/
    (.fp-card index, 12 plans)
  * cyanonpeachtree.com /atlanta/cyan-on-peachtree/conventional/
    (li.fp-group-item index, 23 plans)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ma_poc.pms.adapters.entrata import (
    _pp_base_rent_scope,
    _pp_money_low_high,
    parse_entrata_pp_jd_fp_cards,
    parse_entrata_pp_unit_cards,
    parse_entrata_prospectportal_html,
)

FIXTURES = Path(__file__).parent / "fixtures" / "entrata"

MARLOWE_PLAN_URL = (
    "https://www.marlowepeoriaplace.com/peoria/marlowe-peoria-place/"
    "floorplans/a1-794432/fp_name/occupancy_type/conventional/"
)
MARLOWE_INDEX_URL = (
    "https://www.marlowepeoriaplace.com/peoria/marlowe-peoria-place/conventional/"
)
CYAN_INDEX_URL = (
    "https://www.cyanonpeachtree.com/atlanta/cyan-on-peachtree/conventional/"
)


def _read(name: str) -> str:
    return (FIXTURES / name).read_text()


def _rents(units: list[dict[str, Any]]) -> list[tuple[Any, Any]]:
    return [(u.get("market_rent_low"), u.get("market_rent_high")) for u in units]


# ── the defect, on the exact production markup ──────────────────────────────


def test_marlowe_option_rows_no_inverted_rent() -> None:
    """96 of the 107 inverted run rows came from this page shape."""
    units = parse_entrata_pp_unit_cards(
        _read("prospectportal_per_plan_option_rows_fee_transparency_marlowe.html"),
        MARLOWE_PLAN_URL,
    )
    # 8 ``.option-row`` nodes in the fixture, 1 of which is the
    # ``.option-row.title`` column-header row -> 7 real units.
    assert len(units) == 7
    assert all(isinstance(u.get("market_rent_low"), int) for u in units)
    bad = [
        (u.get("unit_number"), u.get("market_rent_low"), u.get("market_rent_high"))
        for u in units
        if isinstance(u.get("market_rent_low"), int)
        and isinstance(u.get("market_rent_high"), int)
        and u["market_rent_low"] > u["market_rent_high"]
    ]
    assert bad == [], f"inverted rent range on {len(bad)} rows: {bad[:5]}"


def test_marlowe_option_rows_rent_is_base_rent_not_gross() -> None:
    """Unit 2024 publishes Base Rent $1,391 and Total Monthly Leasing
    Price $1,583.98. The rent range must be the Base Rent."""
    units = parse_entrata_pp_unit_cards(
        _read("prospectportal_per_plan_option_rows_fee_transparency_marlowe.html"),
        MARLOWE_PLAN_URL,
    )
    by_num = {u.get("unit_number"): u for u in units}
    u2024 = by_num["2024"]
    assert (u2024["market_rent_low"], u2024["market_rent_high"]) == (1391, 1391)
    # the gross figure must not leak into either rent column
    assert 1583 not in (u2024["market_rent_low"], u2024["market_rent_high"])


def test_marlowe_fp_card_index_rent_is_base_rent() -> None:
    """Template A (.fp-card) index page — same dual-price block."""
    units = parse_entrata_prospectportal_html(
        _read("prospectportal_index_fp_card_fee_transparency_marlowe.html"),
        MARLOWE_INDEX_URL,
    )
    assert units, "fp-card index produced no plans"
    assert all(
        u["extraction_tier"] == "TIER_1_DOM_ENTRATA_PP_FPCARD" for u in units
    )
    inverted = [r for r in _rents(units) if r[0] and r[1] and r[0] > r[1]]
    assert inverted == [], f"inverted plan rents: {inverted}"
    a1 = next(u for u in units if u["floor_plan_name"] == "A1")
    assert (a1["market_rent_low"], a1["market_rent_high"]) == (1391, 1391)


def test_cyan_fp_group_index_rent_is_base_rent() -> None:
    """Template B (li.fp-group-item) — the 11 SSR_PLAN_LEVEL run rows."""
    units = parse_entrata_prospectportal_html(
        _read("prospectportal_fp_group_fee_transparency_cyan.html"),
        CYAN_INDEX_URL,
    )
    assert units, "fp-group index produced no plans"
    inverted = [r for r in _rents(units) if r[0] and r[1] and r[0] > r[1]]
    assert inverted == [], f"inverted plan rents: {inverted}"
    studio = next(
        u for u in units if u["floor_plan_name"] == "Studio One Bath (580 SF)"
    )
    # live: "Total Monthly Leasing Price Starting from $1,511  Base Rent: $1,448+/month"
    assert (studio["market_rent_low"], studio["market_rent_high"]) == (1448, 1448)


# ── helper-level table test ────────────────────────────────────────────────
#
# The fix keys on Entrata's own ``Base Rent:`` label. Every string below is
# either a real live capture or a deliberate near-miss the rule must NOT
# claim. Cases 5-13 are the MUST-NOT-MATCH set: they must keep the legacy
# first/last range behaviour byte-for-byte.
_TABLE: list[tuple[str, str, tuple[int | None, int | None]]] = [
    # (label, input, expected (low, high))
    # -- dual-price: must yield the Base Rent, never the gross ------------
    (
        "marlowe unit row (live)",
        "Base Rent Total Monthly Leasing Price $1,583.98 "
        "Base Rent: $1,391/month",
        (1391, 1391),
    ),
    (
        "marlowe plan header (live)",
        "Total Monthly Leasing Price From $1,583.98 Base Rent: $1,391+/month",
        (1391, 1391),
    ),
    (
        "cyan plan card (live)",
        "Total Monthly Leasing Price Starting from $1,511 "
        "Base Rent: $1,448+/month",
        (1448, 1448),
    ),
    (
        "dual-price where base rent is itself a range",
        "Total Monthly Leasing Price From $2,100 Base Rent: $1,900 - $2,050/month",
        (1900, 2050),
    ),
    (
        "trailing deposit token after the base rent must not become high",
        "Total Monthly Leasing Price $1,583.98 Base Rent: $1,391/month "
        "$300 security deposit (refundable)",
        (1391, 1391),
    ),
    # -- MUST NOT MATCH: unchanged legacy behaviour -----------------------
    ("plain single price (foxlake, live)", "From $1,585 per month", (1585, 1585)),
    (
        "genuine en-dash range (drexelridge, live)",
        "$1,650 – $2,084/month",
        (1650, 2084),
    ),
    ("genuine hyphen range", "$1,495 - $1,695", (1495, 1695)),
    ("em-dash hidden-rent placeholder", "—", (None, None)),
    ("double-hyphen placeholder", "--", (None, None)),
    ("no money at all", "Call for pricing", (None, None)),
    ("empty", "", (None, None)),
    # "Base Rent" as a bare column header / mobile label (NO colon+amount)
    # is not the dual-price marker — must stay first/last.
    ("bare 'Base Rent' label, single price", "Base Rent $1,585", (1585, 1585)),
    (
        "bare 'Base Rent' label, genuine range",
        "Base Rent $1,495 - $1,695",
        (1495, 1695),
    ),
    # A colon that is not the base-rent label must not be hijacked.
    ("deposit label with colon", "Deposit: $300 Rent $1,585", (300, 1585)),
    (
        "'increased base rent' prose without the label form",
        "Rents from $1,585. Base rent increases yearly.",
        (1585, 1585),
    ),
]


@pytest.mark.parametrize(
    ("label", "text", "expected"),
    [(t[0], t[1], t[2]) for t in _TABLE],
    ids=[t[0] for t in _TABLE],
)
def test_pp_money_low_high_table(
    label: str, text: str, expected: tuple[int | None, int | None]
) -> None:
    assert _pp_money_low_high(text) == expected, label


@pytest.mark.parametrize(
    ("label", "text", "expected"),
    [(t[0], t[1], t[2]) for t in _TABLE],
    ids=[t[0] for t in _TABLE],
)
def test_pp_base_rent_scope_agrees_with_low_high(
    label: str, text: str, expected: tuple[int | None, int | None]
) -> None:
    """``_pp_base_rent_scope`` is the shared narrowing used by the two
    sibling positional-rent sites (``.unit-card`` U1 and ``jd-fp-unit-
    card``). It must fire on exactly the dual-price rows and no others."""
    scope = _pp_base_rent_scope(text)
    dual = "base rent:" in text.lower() and "$" in text
    assert (scope is not None) is dual, label
    if scope is not None:
        assert "Total Monthly Leasing Price" not in scope


# ── sibling positional-rent paths (.unit-card U1 / jd-fp-unit-card) ─────────
#
# Neither path had a dual-price row in the 2026-07-27 corpus, so these are
# SYNTHETIC cards built by dropping the verbatim Marlowe fee-transparency
# block (live capture 2026-07-28) into each template's real wrapper markup.
# They pin that the shared guard reaches both sites; they are NOT evidence
# that the shape occurs there in production.
_FEE_BLOCK = (
    '<span class="fee-transparency-text fee-transparency-label-emphasized">'
    "Total Monthly Leasing Price "
    '<span class="fee-transparency-text-emphasized">$1,583.98</span>'
    '<span class="pp-base-rent pp-base-rent-line" role="note">'
    '<span class="pp-base-rent-label">Base Rent:</span>'
    '<span class="pp-base-rent-amount">$1,391+/month</span>'
    "</span></span>"
)


def test_unit_card_u1_dual_price_uses_base_rent() -> None:
    html = (
        '<div class="unit-card" data-unit-id="4557065">'
        '<h3 class="unit-number">2024</h3>'
        "<div>1 Bed &bull; 1 Bath &bull; 700 SqFt &bull; Available Now</div>"
        f'<div class="unit-pricing">{_FEE_BLOCK}</div>'
        "</div>"
    )
    units = parse_entrata_pp_unit_cards(html, MARLOWE_PLAN_URL)
    assert len(units) == 1
    assert (units[0]["market_rent_low"], units[0]["market_rent_high"]) == (1391, 1391)


def test_jd_fp_unit_card_dual_price_uses_base_rent() -> None:
    html = (
        '<a data-jd-fp-selector="unit-card" title="#2024" data-unit="4557065" '
        'class="jd-fp-unit-card jd-fp-unit-card--row">'
        "<span>1 Bed 1 Bath 700 Sq Ft Available 08/01/2026</span>"
        f"{_FEE_BLOCK}</a>"
    )
    units = parse_entrata_pp_jd_fp_cards(html, MARLOWE_PLAN_URL)
    assert len(units) == 1
    assert (units[0]["market_rent_low"], units[0]["market_rent_high"]) == (1391, 1391)
