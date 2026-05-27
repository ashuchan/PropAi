"""Entrata Prospect Portal per-plan ``.option-row`` drill tests.

Chip #98 follow-up (2026-05-25). The original chip #98 drill
(``parse_entrata_pp_unit_cards`` over ``.unit-card`` markup) closed
the regr#9 cohort but did not match Aria-style PP sites that use a
different per-plan URL template AND a different per-row DOM:

  * URL: ``/floorplans/<slug>-<fpid>/fp_name/occupancy_type/<type>/``
    (chip #98 V1 regex required ``<state>/<property>/<slug>-<fpid>-
    <phase>/`` — three path segments after ``/floorplans/``, plus a
    phase digit token)
  * DOM: ``.option-row`` data rows (no ``.unit-card`` blocks)

User-flagged residue: https://www.ariaatella.com/floor-plans (canary
1ef1060 reported n_units=12, only 3-5 strict-pass, tier
TIER_1_DOM_ENTRATA_PP_SSR_PLAN_LEVEL — i.e. chip #98 produced 12
plan-level rows from the SSR grid because the drill could neither
discover the plan link nor parse the per-plan body).

Fixtures are unmodified live captures (curl_cffi chrome120,
2026-05-25):
  * per-plan body — https://ariaatella.prospectportal.com/spring/
    aria-at-ella/floorplans/a1-730162/fp_name/occupancy_type/
    conventional/  (3 units, fpid 730162)
  * conventional/ index — same host /spring/aria-at-ella/conventional/
    (12 plan cards, all with View Details <a> per fp-card)

These tests do not duplicate chip #98's existing coverage in
``test_entrata_pp_unit_drill.py``; they specifically pin the Aria-
style template behavior.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from ma_poc.pms.adapters.entrata import (
    _pp_plan_url_match,
    find_entrata_pp_plan_links,
    parse_entrata_pp_unit_cards,
)


def _sids(u: dict[str, Any]) -> dict[str, str]:
    """make_unit_dict declares ``list[dict[str, str]]`` but ``source_ids``
    is actually a nested dict — cast so mypy --strict accepts the
    test's index access. Mirrors the helper in test_entrata_pp_unit_drill.
    """
    return cast(dict[str, str], u["source_ids"])


FIXTURES = Path(__file__).parent / "fixtures" / "entrata"

# Aria at Ella per-plan URL — V2 template (slug-fpid / fp_name /
# occupancy_type / type).
ARIA_URL = (
    "https://ariaatella.prospectportal.com/spring/aria-at-ella/"
    "floorplans/a1-730162/fp_name/occupancy_type/conventional/"
)
# Existing chip #98 V1 URLs — used for cross-template regression
# guards so the V2 addition can't weaken V1.
RISE_URL = (
    "https://www.risewestarlington.com/floorplans/arlington-TX/"
    "rise-west-arlington/a1-silver-1212885-1/"
)
FOXLAKE_URL = (
    "https://foxlake.prospectportal.com/floorplans/knoxville-knoxville-TN/"
    "fox-lake-apartment-homes/abbington-1440-1/"
)


def _aria_html() -> str:
    return (
        FIXTURES / "prospectportal_per_plan_option_rows_ariaatella.html"
    ).read_text()


def _aria_idx_html() -> str:
    return (
        FIXTURES / "prospectportal_index_with_plan_links_ariaatella.html"
    ).read_text()


# ── _pp_plan_url_match — V2 URL pattern coverage ───────────────────────


def test_aria_url_v2_pattern_matches_slug_and_fpid() -> None:
    """V2 URL helper extracts (slug, fpid) from Aria's 1-segment path.

    Pre-fix the V1 regex returned None for this URL because its 3-
    segment leading prefix didn't match — that meant
    find_entrata_pp_plan_links silently dropped every Aria plan
    link and the drill never fired."""
    m = _pp_plan_url_match(ARIA_URL)
    assert m == ("a1", "730162")


def test_aria_url_v1_still_matches_after_v2_added() -> None:
    """Regression guard: adding V2 must not weaken V1. Rise/Foxlake
    URLs (V1 phased pattern) must still resolve via the helper."""
    assert _pp_plan_url_match(RISE_URL) == ("a1-silver", "1212885")
    assert _pp_plan_url_match(FOXLAKE_URL) == ("abbington", "1440")


def test_aria_url_non_matching_returns_none() -> None:
    """The helper must return None for non-PP URLs so
    find_entrata_pp_plan_links doesn't try to fetch unrelated hrefs."""
    assert _pp_plan_url_match("https://example.com/about") is None
    assert _pp_plan_url_match("https://example.com/floorplans/") is None
    # ``/floorplans/standalone/`` has a slug but no fpid digits — must
    # not match either V1 or V2.
    assert _pp_plan_url_match("https://x.com/floorplans/standalone/") is None


# ── parse_entrata_pp_unit_cards — option-row coverage ──────────────────


def test_aria_yields_three_units_with_real_numbers() -> None:
    """User-flagged URL: 3 .option-row data rows → 3 unit-level rows
    with the canonical PP unit_number (``"2205"``, ``"1104"``,
    ``"2104"``). Pre-fix the per-plan page returned 0 (the .unit-card
    early-bail rejected the body) so chip #98's drill silently fell
    back to the 12 plan-level rows, all with unit_number=""."""
    units = parse_entrata_pp_unit_cards(_aria_html(), ARIA_URL)
    assert len(units) == 3
    unit_numbers = [u["unit_number"] for u in units]
    assert unit_numbers == ["2205", "1104", "2104"]
    # make_unit_dict stores int rents under str-typed keys at runtime —
    # int() casts past the declared list[dict[str, str]] signature.
    rents = [int(u["market_rent_low"]) for u in units]
    assert rents == [1344, 1344, 1384]
    for u in units:
        assert u["floor_plan_name"] == "A1"
        assert u["bedrooms"] == "1"
        assert u["bathrooms"] == "1"
        assert u["sqft"] == "682"
        assert u["extraction_tier"] == "TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL"


def test_aria_emits_stable_uid_and_fpid_in_source_ids() -> None:
    """source_ids.entrata_uid (from button data-unit) + entrata_fpid
    (from data-floorplan / derived URL fpid) carry the canonical PP
    ids. Pinned to live capture so future template drift breaks
    early."""
    units = parse_entrata_pp_unit_cards(_aria_html(), ARIA_URL)
    uids = [_sids(u)["entrata_uid"] for u in units]
    assert uids == ["4632678", "4632621", "4632666"]
    # Every row anchors to the same plan fpid (one per-plan page).
    fpids = {_sids(u)["entrata_fpid"] for u in units}
    assert fpids == {"730162"}


def test_aria_normalises_availability_data_date_to_iso() -> None:
    """The See-Details button carries ``data-date="06/06/2026"`` —
    the canonical PP availability date in numeric form. Prefer it
    over the visible text "Available Jun 06, 2026" because the data
    attribute is locale-stable. Output must be ISO ``YYYY-MM-DD``."""
    units = parse_entrata_pp_unit_cards(_aria_html(), ARIA_URL)
    dates = [u["availability_date"] for u in units]
    # Pinned to live capture: 06/06, 06/08, 06/16 of 2026.
    assert dates == ["2026-06-06", "2026-06-08", "2026-06-16"]


def test_aria_plan_name_derived_from_url_slug_when_caller_silent() -> None:
    """Aria's URL slug ``a1-730162`` → plan name ``"A1"`` (the slug
    title-cased, with the fpid stripped by the V2 regex). Critical:
    every row must carry a non-empty floor_plan_name so the validity
    gate admits it and the plan-level merge can collapse the plan-
    tier and unit-tier observations together."""
    units = parse_entrata_pp_unit_cards(_aria_html(), ARIA_URL)
    assert units[0]["floor_plan_name"] == "A1"


def test_aria_caller_supplied_plan_name_wins_over_url_derivation() -> None:
    """When the adapter already parsed a display name from the index
    page (chip #98 wires this in step 4), the caller's value beats
    the URL slug. Mirrors test_caller_supplied_floor_plan_name_wins
    behavior for the .option-row template."""
    units = parse_entrata_pp_unit_cards(
        _aria_html(), ARIA_URL, floor_plan_name="A1 - Premium Series"
    )
    assert units[0]["floor_plan_name"] == "A1 - Premium Series"


def test_aria_skips_option_row_title_header() -> None:
    """``.option-row.title`` is PP's column-header row (labels
    "Unit", "Rent", "Sq.ft.", "Available" on mobile) — it must NEVER
    be emitted as a unit. The Aria fixture has 4 .option-row blocks
    total; 3 data rows + 1 title row → 3 units (not 4). The visible
    "Unit " mobile-text prefix on .detail.first must also be stripped
    so we get "2205" not "Unit 2205"."""
    units = parse_entrata_pp_unit_cards(_aria_html(), ARIA_URL)
    assert len(units) == 3
    for u in units:
        # The first .detail.first contains the "Unit " prefix — make
        # sure we stripped it (not leaving "Unit 2205").
        assert not u["unit_number"].lower().startswith("unit ")
        # And the literal label "Unit" must never become a unit_number.
        assert u["unit_number"].lower() != "unit"


def test_aria_no_inferred_unit_id_pattern() -> None:
    """REGRESSION GUARD (canary 1ef1060 follow-up): every Aria row
    emitted by the option-row drill MUST have a real unit_number
    from the .detail.first cell — never a synthetic ``inferred_``
    or placeholder ``ent-<uid>``. Without this guard the runner
    would re-introduce inferred ids for the Aria cohort and any
    other Aria-style site."""
    for u in parse_entrata_pp_unit_cards(_aria_html(), ARIA_URL):
        num = str(u.get("unit_number") or "")
        assert num, "empty unit_number on Aria option-row"
        assert not num.startswith("inferred_")
        assert not num.startswith("ent-")


def test_aria_lease_term_extracted_from_lease_term_name_span() -> None:
    """Aria publishes "18mo lease" inside ``.lease-term-name``; the
    parser must surface it on every row so downstream consumers can
    distinguish a 12mo posted rent from an 18mo posted rent (the
    same plan often quotes different rents at different terms)."""
    units = parse_entrata_pp_unit_cards(_aria_html(), ARIA_URL)
    for u in units:
        assert u["lease_term"] == "18mo lease"


def test_aria_does_not_pick_up_deposit_or_lease_text_as_rent() -> None:
    """Aria's .detail.second cell text is "Rent $1,344 /month 18mo
    lease". The parser must extract $1,344 — not pick up any "18mo"
    or "lease" tokens as dollars (they aren't dollar-prefixed but a
    bad regex could still match digits adjacent to "mo"). Pinned to
    each row's known live rent."""
    units = parse_entrata_pp_unit_cards(_aria_html(), ARIA_URL)
    for u in units:
        lo = int(u["market_rent_low"])
        assert 1300 <= lo <= 1400, f"rent {lo} outside Aria's known band"


# ── find_entrata_pp_plan_links — Aria index discovery ──────────────────


def test_aria_plan_links_discovered_from_conventional_index() -> None:
    """The conventional/ index page has 12 .fp-card blocks; each has
    a View Details <a> with the Aria-style /floorplans/<slug>-<fpid>/
    fp_name/... URL. find_entrata_pp_plan_links must discover ALL
    12 (deduped) so the per-plan drill iterates the full plan set.

    Pre-fix the V1-only URL regex matched 0 of these so the drill
    never fired on Aria."""
    links = find_entrata_pp_plan_links(
        _aria_idx_html(), "https://ariaatella.prospectportal.com"
    )
    assert len(links) == 12
    # The A1 plan URL must appear in the discovered set — it's the
    # one the user flagged and the one we have a per-plan fixture for.
    assert ARIA_URL in links
    # Every link must end with /conventional/ (Aria's URL template
    # hard-codes occupancy_type/conventional/ as the trailing path).
    for link in links:
        assert link.endswith("/conventional/")


def test_aria_v2_url_synthetic_index_discovery() -> None:
    """Hand-rolled minimal V2 index: a single .fp-card with a View
    Details <a> pointing to the Aria-style URL template. Pre-fix the
    selector ``.fp-card a[href*='/floorplans/']`` matched but
    _PP_PLAN_URL_RE rejected the href — find_entrata_pp_plan_links
    returned [].

    With the V2 addition the same body now yields the expected
    absolute URL. Cross-origin hrefs are preserved (PP iframe links
    keep their own host so the drill stays on the property)."""
    html = (
        '<html><body>'
        '<div class="fp-card">'
        '<a class="primary btn" '
        'href="https://example.prospectportal.com/spring/example/'
        'floorplans/b2-987654/fp_name/occupancy_type/conventional/">'
        'View Details</a>'
        '</div>'
        '</body></html>'
    )
    links = find_entrata_pp_plan_links(
        html, "https://www.exampleapts.com"
    )
    assert links == [
        "https://example.prospectportal.com/spring/example/floorplans/"
        "b2-987654/fp_name/occupancy_type/conventional/"
    ]


def test_aria_returns_empty_when_no_option_row_or_unit_card() -> None:
    """Loosening the early-bail predicate from "unit-card" only to
    "unit-card or option-row" must NOT cause false positives on
    pages with neither marker. A plain <html></html> still bails."""
    assert parse_entrata_pp_unit_cards(
        "<html><body><p>no rosters here</p></body></html>", ""
    ) == []
    # A page with ``.option-row.title`` ONLY (header but no data rows)
    # must also bail — it's an empty plan with no available units.
    assert parse_entrata_pp_unit_cards(
        '<html><body><div class="option-row title">'
        '<div class="detail">Unit</div></div></body></html>',
        "",
    ) == []
