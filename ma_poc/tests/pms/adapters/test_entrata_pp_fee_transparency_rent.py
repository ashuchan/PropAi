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

TWO LABEL RENDERINGS occur in production. Both were enumerated by
pulling the rent-cell string every PP parse site reads out of ALL 4,097
captured bodies of run-2026-07-27-full-0d54ca7 — not guessed:

  (1) PREFIX + colon (Prospect-Portal fee-transparency themes)
        "Total Monthly Leasing Price From $1,583.98 Base Rent: $1,391+/month"
        "... Starting from $2,562 Base Rent: $2,417+/month"
        "... $2,251.65 + Base Rent: $2,170+/per installment"
        "... $2,172.98 Base Rent: $1,980/month"
      DOM: <span class="pp-base-rent-amount">
  (2) SUFFIX, no colon (the newer ``jd-fp-unit-card`` theme)
        "... $1,731 /mo* 15 months $1,615 Base Rent"
      DOM: <span data-jd-fp-adp="base_display" class="...--base">

A first pass at this fix handled only (1), with a PREFIX-and-colon text
regex. Offline replay of the same 4,097 bodies: 174 inverted rows on 12
properties before, 156 on 9 properties after — the whole of rendering
(2) survived. The rule is therefore DOM-anchored on Entrata's own
per-quantity markup, which covers both; the text regex remains as a
fallback and KEEPS its colon requirement, because a bare "Base Rent"
matches a column header above a genuine range ("Base Rent $1,650 –
$2,084") and a concession ("$305 Off Base Rent").

Fixtures:
  * Unmodified live captures (curl_cffi chrome120, plain unauthenticated
    GET, 2026-07-28):
      - marlowepeoriaplace.com /peoria/marlowe-peoria-place/floorplans/
        a1-794432/fp_name/occupancy_type/conventional/ (.option-row, 8 rows)
      - marlowepeoriaplace.com /peoria/marlowe-peoria-place/conventional/
        (.fp-card index, 12 plans)
      - cyanonpeachtree.com /atlanta/cyan-on-peachtree/conventional/
        (li.fp-group-item index, 23 plans)
  * Verbatim card bytes cut from the RUN'S OWN captured body:
      - prospectportal_jd_fp_unit_cards_fee_transparency_tomoka.html —
        jd-fp-unit-card elements from run-2026-07-27-full-0d54ca7
        raw_html/288502.html.gz (Marlowe Tomoka Village).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from bs4 import BeautifulSoup

from ma_poc.pms.adapters.entrata import (
    _pp_base_rent_node_text,
    _pp_base_rent_scope,
    _pp_money_low_high,
    _pp_rent_cell_text,
    parse_entrata_pp_jd_fp_cards,
    parse_entrata_pp_unit_cards,
    parse_entrata_prospectportal_html,
    parse_prospect_portal_cards,
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
# PROVENANCE, corrected 2026-07-28: an earlier version of this file said
# neither path had a dual-price row in the 2026-07-27 corpus. That was
# wrong. Replaying all 4,097 captured bodies through
# ``parse_entrata_pp_jd_fp_cards`` produced 156 inverted rows across 9
# properties (Solamar Wildwood 61, Marlowe Tomoka Village 54, Preserve at
# Travis Creek 16, Elan Polo Gardens 10, Headwaters at Autumn Hall 5,
# Greenhouse Villas 4, Ayrsley Lofts 2, E6 2, Wyncrest 2) — all of them
# rendering (2), the suffix label. ``test_jd_fp_real_corpus_markup_*``
# below run on those verbatim production bytes.
#
# The ``.unit-card`` U1 path is different: no dual-price row for it was
# found in the corpus, so its case below IS synthetic (the verbatim
# Marlowe fee-transparency block dropped into the real U1 wrapper). It
# pins that the guard reaches that site; it is NOT evidence that the
# shape occurs there in production.
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


# ── every rent-range read site in entrata.py, one case each ────────────────
#
# The first pass at this fix claimed the guard was applied "at the sibling
# sites, so the same defect class cannot survive in a sibling path". It
# survived in TWO of them (``parse_entrata_pp_jd_fp_cards`` and
# ``parse_prospect_portal_cards``) because the claim was made by reading
# the code, not by running it. Every site below is therefore RUN. The
# ordinary-range control asserts the guard is a no-op off the
# fee-transparency template.

_PLAIN_CELL = (
    '<span class="fee-transparency-text">$1,650 – $2,084 per month</span>'
)


def _fp_group_html(cell: str) -> str:
    return (
        '<li class="fp-group-item"><span class="fp-name">Bruce</span>'
        '<div class="fp-col bed-bath"><span class="fp-col-text">1 bd / 1 ba</span></div>'
        f'<div class="fp-col rent"><div class="fp-col-text fee-transparency-wrapper">{cell}</div></div>'
        '<div class="fp-col sq-feet"><span class="fp-col-text">540</span></div></li>'
    )


def _unit_item_html(cell: str) -> str:
    return (
        '<li class="unit-item"><div class="unit-title">The Oak</div>'
        '<div class="unit-bed-bath">1 Bed, 1 Bath, 620 SqFt</div>'
        f'<div class="unit-price">{cell}</div></li>'
    )


def _option_row_html(cell: str) -> str:
    return (
        '<div class="fp-details-container"><h1>A1</h1><span>1 Bed / 1 Bath</span></div>'
        '<div class="option-row title"><div class="detail first">Unit</div></div>'
        '<div class="option-row"><div class="detail first">Unit 2024</div>'
        f'<div class="detail second">{cell}</div>'
        '<div class="detail block">Sq. Ft. 700</div>'
        '<div class="detail block">Available Now</div></div>'
    )


@pytest.mark.parametrize(
    ("site", "run"),
    [
        (
            "_parse_pp_fp_group_item (Template B)",
            lambda cell: parse_entrata_prospectportal_html(_fp_group_html(cell), "u"),
        ),
        (
            "_parse_pp_unit_item (Template C)",
            lambda cell: parse_entrata_prospectportal_html(_unit_item_html(cell), "u"),
        ),
        (
            "_parse_pp_option_rows (U2)",
            lambda cell: parse_entrata_pp_unit_cards(_option_row_html(cell), "u"),
        ),
    ],
    ids=["fp_group_item_B", "unit_item_C", "option_rows_U2"],
)
def test_every_sibling_site_uses_base_rent(site: str, run: Any) -> None:
    dual = run(_FEE_BLOCK)
    assert len(dual) == 1, site
    assert (dual[0]["market_rent_low"], dual[0]["market_rent_high"]) == (
        1391,
        1391,
    ), site
    # control: an ordinary range cell must be untouched by the guard
    plain = run(_PLAIN_CELL)
    assert len(plain) == 1, site
    assert (plain[0]["market_rent_low"], plain[0]["market_rent_high"]) == (
        1650,
        2084,
    ), site


def test_prospect_portal_cards_flat_fee_string_uses_base_rent() -> None:
    """``parse_prospect_portal_cards`` is handed an ALREADY-FLATTENED cell
    string (Playwright ``page.evaluate``), so only the text rule reaches it.
    It still carried the defect after the first pass at this fix."""
    dual = parse_prospect_portal_cards(
        [
            {
                "name": "A1",
                "bedbath": "1 Bed / 1 Bath",
                "sqft": "700 sq. ft",
                "fee": "Total Monthly Leasing Price $1,583.98 Base Rent: $1,391/month",
                "lease": "",
                "deposit": "",
                "availability": "",
                "special": "",
            }
        ],
        "u",
    )
    assert (dual[0]["market_rent_low"], dual[0]["market_rent_high"]) == (1391, 1391)
    plain = parse_prospect_portal_cards(
        [
            {
                "name": "A1",
                "bedbath": "1 Bed / 1 Bath",
                "sqft": "700 sq. ft",
                "fee": "$1,650 – $2,084 per month",
                "lease": "",
                "deposit": "",
                "availability": "",
                "special": "",
            }
        ],
        "u",
    )
    assert (plain[0]["market_rent_low"], plain[0]["market_rent_high"]) == (1650, 2084)


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


# ── rendering (2): the SUFFIX label, on verbatim production bytes ───────────
#
# These are the 156 rows that survived the first pass at this fix. The
# fixture holds jd-fp-unit-card elements copied unmodified out of the
# run's own captured body for Marlowe Tomoka Village.

TOMOKA_URL = "https://marlowetomokavillage.com/floorplans/"
TOMOKA_FIXTURE = "prospectportal_jd_fp_unit_cards_fee_transparency_tomoka.html"


def test_jd_fp_real_corpus_markup_no_inverted_rent() -> None:
    units = parse_entrata_pp_jd_fp_cards(_read(TOMOKA_FIXTURE), TOMOKA_URL)
    assert len(units) == 12, "fixture holds 12 distinct unit cards"
    bad = [
        (u["unit_number"], u["market_rent_low"], u["market_rent_high"])
        for u in units
        if isinstance(u.get("market_rent_low"), int)
        and isinstance(u.get("market_rent_high"), int)
        and u["market_rent_low"] > u["market_rent_high"]
    ]
    assert bad == [], f"inverted rent range on {len(bad)} rows: {bad[:5]}"


def test_jd_fp_real_corpus_markup_rent_is_base_not_gross() -> None:
    """Card #802 renders ``$1,731 /mo*`` (gross) and ``$1,615 Base Rent``."""
    units = parse_entrata_pp_jd_fp_cards(_read(TOMOKA_FIXTURE), TOMOKA_URL)
    by_num = {u["unit_number"]: u for u in units}
    u802 = by_num["802"]
    assert (u802["market_rent_low"], u802["market_rent_high"]) == (1615, 1615)
    # the gross must not survive in either rent column
    assert 1731 not in (u802["market_rent_low"], u802["market_rent_high"])
    # and the rest of the row must be untouched by the narrowing
    assert u802["bedrooms"] == "1"
    assert u802["bathrooms"] == "1"
    assert u802["sqft"] == "798"


# ── DOM-rule table test ────────────────────────────────────────────────────
#
# ``_pp_base_rent_node_text`` is the PRIMARY rule (the text regex is the
# fallback). Rows 1-4 are the must-match set, taken from real production
# markup; rows 5-12 are the must-NOT-match set — every one of them must
# leave the caller's whole-cell text untouched, because claiming any of
# them would turn an inversion bug into a truncation bug.
_NODE_TABLE: list[tuple[str, str, str | None]] = [
    # (label, cell html, expected _pp_base_rent_node_text)
    (
        "PP fee-transparency: pp-base-rent-amount (marlowe, live)",
        '<span class="fee-transparency-text">Total Monthly Leasing Price '
        '<span class="fee-transparency-text-emphasized">$1,583.98</span>'
        '<span class="pp-base-rent"><span class="pp-base-rent-label">Base Rent:'
        '</span><span class="pp-base-rent-amount">$1,391+/month</span></span></span>',
        "$1,391+/month",
    ),
    (
        "jd-fp suffix label: data-jd-fp-adp=base_display (tomoka, corpus)",
        '<span class="jd-fp-card-info__text"><span data-jd-fp-adp="display" '
        'class="jd-fp-strong-text">$1,731 /mo*</span>'
        '<span class="jd-fp-card-info-term-and-base">'
        '<span class="jd-fp-card-info-term-and-base--term" '
        'data-jd-fp-adp="term">15 months</span>'
        '<span class="jd-fp-card-info-term-and-base--base" '
        'data-jd-fp-adp="base_display">$1,615 Base Rent</span></span></span>',
        "$1,615 Base Rent",
    ),
    (
        "jd-fp suffix label by CLASS only (no data-attr)",
        '<span><span class="jd-fp-strong-text">$1,731 /mo*</span>'
        '<span class="jd-fp-card-info-term-and-base--base">$1,615 Base Rent'
        "</span></span>",
        "$1,615 Base Rent",
    ),
    (
        "base rent that is itself a range",
        '<span class="fee-transparency-text">Total Monthly Leasing Price $2,300'
        '<span class="pp-base-rent-amount">$1,900 - $2,050/month</span></span>',
        "$1,900 - $2,050/month",
    ),
    # ── MUST NOT MATCH ────────────────────────────────────────────────────
    (
        "plain single price, no fee transparency (foxlake, live)",
        '<span class="unit-price">From $1,585 per month</span>',
        None,
    ),
    (
        "genuine en-dash range (drexelridge, live)",
        '<span class="unit-price">$1,650 – $2,084/month</span>',
        None,
    ),
    (
        "gross figure only, no base rent published (andante, corpus)",
        '<span class="fee-transparency-text">Total Monthly Leasing Price From '
        "$1,370</span>",
        None,
    ),
    (
        "gross RANGE only, no base rent published (cala, corpus)",
        '<span class="unit-price">Total Monthly Leasing Price $1,444 – $2,108 '
        "Calculate</span>",
        None,
    ),
    (
        "'Base Rent' as a bare fp-col column header above a real range (cyan)",
        '<div class="fp-col rent"><span class="fp-col-title">Base Rent</span>'
        '<span class="fp-col-text">$1,650 – $2,084</span></div>',
        None,
    ),
    (
        "concession text naming base rent",
        '<div class="unit-card"><span class="concession">$305 Off Base Rent'
        "</span></div>",
        None,
    ),
    (
        "marketing banner: UP TO 6 WEEKS FREE BASE RENT (cyan, corpus)",
        '<div class="unit-price"><h2>UP TO 6 WEEKS FREE BASE RENT!</h2>'
        "<span>$1,585</span></div>",
        None,
    ),
    (
        "base-rent node present but carries NO money token",
        '<span class="fee-transparency-text">Total Monthly Leasing Price $1,583'
        '<span class="pp-base-rent-amount">Call for pricing</span></span>',
        None,
    ),
    (
        "em-dash hidden-rent placeholder",
        '<span class="unit-price">—</span>',
        None,
    ),
    ("empty cell", '<span class="unit-price"></span>', None),
]


@pytest.mark.parametrize(
    ("label", "cell_html", "expected"),
    _NODE_TABLE,
    ids=[t[0] for t in _NODE_TABLE],
)
def test_pp_base_rent_node_text_table(
    label: str, cell_html: str, expected: str | None
) -> None:
    el = BeautifulSoup(cell_html, "lxml").body.contents[0]  # type: ignore[union-attr]
    assert _pp_base_rent_node_text(el) == expected, label


@pytest.mark.parametrize(
    ("label", "cell_html", "expected"),
    _NODE_TABLE,
    ids=[t[0] for t in _NODE_TABLE],
)
def test_pp_rent_cell_text_falls_back_to_whole_cell(
    label: str, cell_html: str, expected: str | None
) -> None:
    """Every must-NOT-match row must be byte-identical to the legacy
    ``el.get_text(" ", strip=True)`` the call sites used before the fix."""
    el = BeautifulSoup(cell_html, "lxml").body.contents[0]  # type: ignore[union-attr]
    legacy = el.get_text(" ", strip=True)
    got = _pp_rent_cell_text(el)
    if expected is None:
        assert got == legacy, label
    else:
        assert got == expected, label
        assert got != legacy, label


def test_pp_rent_cell_text_handles_missing_element() -> None:
    assert _pp_rent_cell_text(None) == ""
    assert _pp_base_rent_node_text(None) is None
    # a plain string is not an element — must not raise
    assert _pp_base_rent_node_text("Base Rent: $1,391") is None
