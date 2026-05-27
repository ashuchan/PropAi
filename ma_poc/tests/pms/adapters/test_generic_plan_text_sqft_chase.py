"""GenericPlanTextAdapter sqft /floorplans-chase tests (2026-05-25).

Covers the sqft=-1 probe fix: 38% of TIER_1_DOM_GENERIC_PLAN_TEXT props
were emitting rows with sqft="" because:
  * ``_SQFT_RE`` didn't match "square feet" / "SQUARE FEET" / "sf"
    (only "sq ft" / "sqft" / "sq. ft.")
  * The existing /floorplans fallback only fired when zero rows were
    extracted from the landing body — not when rows existed but lacked
    sqft.

This file pins:
  1. Expanded ``_SQFT_RE`` matches all live sqft formats.
  2. ``_scan_beds_to_sqft`` builds correct beds→sqft maps from raw text.
  3. ``_enrich_sqft_from_floorplans`` merges sqft by (beds, baths) and
     falls back to beds-only.
  4. ``GenericPlanTextAdapter.extract`` invokes the chase post-rows
     when sqft coverage is < 50%, using probe_get for subpage probes.
  5. Regression guards: Elementor "Starting at $X" still works; the
     existing zero-rows subpage fallback is still active; no extra
     network calls when all rows already have sqft.

Each test names the live signature property it pins.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from ma_poc.pms.adapters.generic_plan_text import (
    _SQFT_RE,
    GenericPlanTextAdapter,
    _enrich_sqft_from_floorplans,
    _scan_beds_to_sqft,
    parse_generic_plan_text,
)

# ── 1. Expanded _SQFT_RE coverage ──


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1930 Sq. Ft", "1930"),           # Alders (period + capitalized)
        ("1,020 Sq. Ft", "1,020"),         # Alders (comma thousands)
        ("944 sq. ft", "944"),             # Stoneycreek
        ("1020sqft", "1020"),              # tight (no space)
        # NEW formats below — were silently dropped pre-fix.
        ("1020 square feet", "1020"),      # Alders (lowercase)
        ("930 SQUARE FEET", "930"),        # Sandpiper (uppercase)
        ("1234 square foot", "1234"),      # singular
        ("750 sf", "750"),                 # bare SF
        ("1100 SF", "1100"),               # bare SF uppercase
    ],
)
def test_sqft_re_matches_all_live_formats(text: str, expected: str) -> None:
    """_SQFT_RE must match every sqft format observed across the 96-prop
    sqft=-1 cohort. Pre-fix the four NEW formats were silent misses."""
    m = _SQFT_RE.search(text)
    assert m is not None, f"_SQFT_RE failed to match {text!r}"
    assert m.group(1) == expected


def test_sqft_re_rejects_plain_numbers() -> None:
    """Defensive: bare numbers without a sqft suffix must NOT match
    (otherwise rent or unit-numbers would be picked up as sqft)."""
    assert _SQFT_RE.search("1234") is None
    assert _SQFT_RE.search("apt 750 has 2 bedrooms") is None


# ── 2. _scan_beds_to_sqft proximity scanner ──


def test_scan_beds_to_sqft_stoneycreek_signature() -> None:
    """Stoneycreek /floor-plans/ pattern: bed token then 200+ chars of
    description ending in sqft. Wider window (350) than main parser's
    100 catches it."""
    body = (
        "1 Bedroom - 1 Bath Phase IV You'll love the huge storage closet "
        "and oversized patio in this 944 sq. ft. apartment! Features "
        "include a pass-through kitchen, foyer with coat closet. "
        "2 Bedroom - 1 Bath Phase IV This 1,128-square-foot apartment has "
        "a spacious master bedroom."
    )
    out = _scan_beds_to_sqft(body)
    assert out.get("1") == "944"
    assert out.get("2") == "1128"


def test_scan_beds_to_sqft_first_match_wins_per_bed() -> None:
    """When multiple sqft hits share a bed count, the FIRST one wins.
    Prevents bed-2's sqft from overwriting bed-1's when the body iterates
    plans in a different order than expected."""
    body = "1 Bedroom 800 sq ft 1 Bedroom 950 sq ft"
    out = _scan_beds_to_sqft(body)
    assert out["1"] == "800"


def test_scan_beds_to_sqft_stops_at_next_bed_boundary() -> None:
    """Without the next-bed boundary check, the scanner would pick up
    a sibling plan's sqft for the current bed. Pin the boundary stop."""
    body = "1 Bedroom no sqft here 2 Bedroom 1200 sq ft"
    out = _scan_beds_to_sqft(body)
    # 1-bed must NOT inherit 2-bed's 1200 sqft.
    assert "1" not in out or out["1"] != "1200"
    assert out["2"] == "1200"


def test_scan_beds_to_sqft_studio_normalised_to_0() -> None:
    """Studio token → key '0' in the output map. Lets bed-count merge
    work uniformly."""
    out = _scan_beds_to_sqft("Studio 550 sq ft")
    assert out.get("0") == "550"


def test_scan_beds_to_sqft_rejects_implausible_sqft() -> None:
    """Defensive: sqft below 150 or above 10000 must be rejected. A
    "1 Bedroom 99 sq ft" entry is a parse error, not real."""
    assert _scan_beds_to_sqft("1 Bedroom 99 sq ft") == {}
    assert _scan_beds_to_sqft("1 Bedroom 50000 sq ft") == {}


# ── 3. _enrich_sqft_from_floorplans merge logic ──


class _FakeProbe:
    """Callable ``probe_get`` stub that records each call. Returns 200
    + the canned body for a matched URL, 404 otherwise. The
    ``call_count`` attribute lets tests assert "no probes fired" without
    pulling in ``unittest.mock.Mock``'s side_effect plumbing."""

    class _R:
        def __init__(self, status_code: int, text: str) -> None:
            self.status_code = status_code
            self.text = text

    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.call_count = 0
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: int = 12) -> Any:  # noqa: ARG002
        self.call_count += 1
        self.calls.append(url)
        if url in self.responses:
            return self._R(200, self.responses[url])
        return self._R(404, "")


def _make_fake_probe(responses: dict[str, str]) -> _FakeProbe:
    return _FakeProbe(responses)


def test_enrich_uses_subpage_when_landing_lacks_sqft() -> None:
    """Sandpiper signature: landing has 0 rows so the existing "if not
    rows" path lifts subpage rows; those rows then have sqft because of
    the _SQFT_RE fix. The chase here verifies that even WITHOUT that
    earlier lift, a separate call to the helper still enriches by
    fetching the subpage."""
    rows = [
        {"bedrooms": "2", "bathrooms": "1", "sqft": "",
         "floor_plan_name": "2 Bedroom / 1 Bath",
         "market_rent_low": 1769},
        {"bedrooms": "3", "bathrooms": "2", "sqft": "",
         "floor_plan_name": "3 Bedroom / 2 Bath",
         "market_rent_low": 2079},
    ]
    html = (
        "<html><body>"
        "Floor Plan 2 Bedroom 1 Bath 930 SQUARE FEET starting at $1769 "
        "Floor Plan 3 Bedroom 2 Bath 1128 SQUARE FEET starting at $2079"
        "</body></html>"
    )
    fake = _make_fake_probe({"https://x.example.com/floorplans/": html})
    with patch("ma_poc.pms.adapters._probe.probe_get", fake):
        out_rows, path, n = _enrich_sqft_from_floorplans(
            rows, "https://x.example.com/"
        )
    assert path == "/floorplans/"
    assert n == 2
    sqfts = sorted(r["sqft"] for r in out_rows)
    assert sqfts == ["1128", "930"]


def test_enrich_uses_homepage_fallback_when_no_subpage() -> None:
    """Majestic signature: site has all data on landing (no /floorplans
    subpage), but per-unit rent rows lack sqft because they sit far from
    the plan-header sqft. The homepage_body fallback recovers sqft via
    proximity scan."""
    rows = [
        {"bedrooms": "1", "bathrooms": "1", "sqft": "",
         "floor_plan_name": "1 Bedroom / 1 Bath",
         "market_rent_low": 2585},
        {"bedrooms": "1", "bathrooms": "1", "sqft": "",
         "floor_plan_name": "1 Bedroom / 1 Bath",
         "market_rent_low": 2615},
        {"bedrooms": "2", "bathrooms": "2", "sqft": "",
         "floor_plan_name": "2 Bedroom / 2 Bath",
         "market_rent_low": 2900},
    ]
    homepage = (
        "Style A1 1 Bedroom 1 Bath 1001 Sq Ft Starting at $2,580 "
        "03-0712 1 Bedroom 1 Bath $2,615 06/23/2026 "
        "Style B2 2 Bedroom 2 Bath 1064 Sq Ft Starting at $2,900"
    )
    # No subpage responses — every probe returns 404.
    fake = _make_fake_probe({})
    with patch("ma_poc.pms.adapters._probe.probe_get", fake):
        out_rows, path, n = _enrich_sqft_from_floorplans(
            rows, "https://x.example.com/", homepage_body=homepage,
        )
    assert path == "<homepage-body>"
    # All three rows are filled in (1-bed gets 1001, 2-bed gets 1064).
    assert n == 3
    assert all(r["sqft"] for r in out_rows)
    one_bed_sqfts = {r["sqft"] for r in out_rows if r["bedrooms"] == "1"}
    assert one_bed_sqfts == {"1001"}
    two_bed_sqfts = {r["sqft"] for r in out_rows if r["bedrooms"] == "2"}
    assert two_bed_sqfts == {"1064"}


def test_enrich_skips_rows_that_already_have_sqft() -> None:
    """Defensive: rows with sqft already populated must not be
    overwritten by the chase (preserves data from primary extraction)."""
    rows = [
        {"bedrooms": "1", "bathrooms": "1", "sqft": "800",  # primary won
         "floor_plan_name": "1 Bed", "market_rent_low": 1500},
        {"bedrooms": "1", "bathrooms": "1", "sqft": "",     # needs chase
         "floor_plan_name": "1 Bed", "market_rent_low": 1600},
    ]
    html = "1 Bedroom 1 Bath 999 sq ft starting at $1500"
    fake = _make_fake_probe({"https://x.example.com/floorplans/": html})
    with patch("ma_poc.pms.adapters._probe.probe_get", fake):
        out_rows, _path, n = _enrich_sqft_from_floorplans(
            rows, "https://x.example.com/"
        )
    assert n == 1
    # First row kept its primary sqft (800), not overwritten with 999.
    assert out_rows[0]["sqft"] == "800"
    # Second row got 999 from chase.
    assert out_rows[1]["sqft"] == "999"


def test_enrich_no_op_when_all_rows_have_sqft() -> None:
    """Early-exit guard: when every row already has sqft, the helper
    skips the network probe entirely. Saves an unnecessary fetch on
    well-extracted properties."""
    rows = [
        {"bedrooms": "1", "sqft": "800", "floor_plan_name": "1 Bed"},
        {"bedrooms": "2", "sqft": "950", "floor_plan_name": "2 Bed"},
    ]
    fake = _make_fake_probe({})
    with patch("ma_poc.pms.adapters._probe.probe_get", fake):
        out_rows, path, n = _enrich_sqft_from_floorplans(
            rows, "https://x.example.com/"
        )
    assert n == 0
    assert path == ""
    assert fake.call_count == 0  # NO network call
    assert out_rows is rows  # identity preserved


def test_enrich_no_op_on_empty_rows() -> None:
    """Empty rows list → graceful no-op (no crash, no fetch)."""
    fake = _make_fake_probe({"https://x.example.com/floorplans/": "x"})
    with patch("ma_poc.pms.adapters._probe.probe_get", fake):
        out_rows, path, n = _enrich_sqft_from_floorplans(
            [], "https://x.example.com/"
        )
    assert (out_rows, path, n) == ([], "", 0)
    assert fake.call_count == 0


def test_enrich_no_op_when_base_url_invalid() -> None:
    """Invalid base_url (no scheme/netloc) → graceful no-op."""
    rows = [{"bedrooms": "1", "sqft": ""}]
    out_rows, path, n = _enrich_sqft_from_floorplans(rows, "not-a-url")
    assert (path, n) == ("", 0)


def test_enrich_prefers_beds_baths_over_beds_only_match() -> None:
    """When both (1, 1) and beds-only-1 entries exist in the map, the
    narrower (1, 1) match wins. Pin the merge precedence."""
    rows = [
        {"bedrooms": "1", "bathrooms": "1", "sqft": "",
         "floor_plan_name": "1 Bed 1 Bath",
         "market_rent_low": 1500},
    ]
    html = (
        # 1 Bedroom 1 Bath 850 sq ft  → (1, "1") → 850
        # 1 Bedroom 2 Bath 950 sq ft  → (1, "2") → 950
        "1 Bedroom 1 Bath 850 sq ft starting at $1500 "
        "1 Bedroom 2 Bath 950 sq ft starting at $1700"
    )
    fake = _make_fake_probe({"https://x.example.com/floorplans/": html})
    with patch("ma_poc.pms.adapters._probe.probe_get", fake):
        out_rows, _path, n = _enrich_sqft_from_floorplans(
            rows, "https://x.example.com/"
        )
    assert n == 1
    assert out_rows[0]["sqft"] == "850"  # NOT 950


# ── 4. Adapter end-to-end with chase ──


class _StubFR:
    def __init__(self, text: str, url: str) -> None:
        self.body = text.encode("utf-8", "replace")
        self.final_url = url
        self.status_code = 200


class _StubCtx:
    def __init__(self, base: str, fr: _StubFR) -> None:
        self.base_url = base
        self.fetch_result = fr
        self.property_id = "TEST"
        self.profile = None


class _StubPage:
    def __init__(self, url: str) -> None:
        self.url = url


async def _make_adapter_call(homepage_html: str, subpage_html: str = ""):
    """Run the adapter against canned homepage + subpage HTML and return
    its AdapterResult. The homepage body becomes ctx.fetch_result.body;
    the subpage is served via the patched probe_get when the adapter's
    sqft chase fires."""
    site = "https://x.example.com/"
    fake = _make_fake_probe(
        {"https://x.example.com/floorplans/": subpage_html} if subpage_html else {}
    )
    ctx = _StubCtx(site, _StubFR(homepage_html, site))
    with patch("ma_poc.pms.adapters._probe.probe_get", fake):
        return await GenericPlanTextAdapter().extract(_StubPage(site), ctx)


@pytest.mark.asyncio
async def test_adapter_chase_fires_when_landing_rows_lack_sqft() -> None:
    """Sandpiper-style: landing returns 0 rows so existing "if not rows"
    path lifts subpage rows, _SQFT_RE fix means those rows now have sqft.
    Pin end-to-end behavior."""
    homepage = "<html><body>About us... no plans here.</body></html>"
    subpage = (
        "<html><body>"
        "2 Bedroom 1 Bath 930 SQUARE FEET Starting at $1769 "
        "3 Bedroom 2 Bath 1128 SQUARE FEET Starting at $2079"
        "</body></html>"
    )
    result = await _make_adapter_call(homepage, subpage_html=subpage)
    units = result.units or []
    assert len(units) >= 2
    sqfts = {u.get("sqft") for u in units if u.get("sqft")}
    assert "930" in sqfts
    assert "1128" in sqfts


@pytest.mark.asyncio
async def test_adapter_chase_recovers_majestic_homepage_per_unit_rows() -> None:
    """Majestic Vernon Hills signature: plan-header rows publish sqft at
    the top of the page while per-unit listings appear hundreds of chars
    later. The primary parser's 100-char lookahead misses sqft on
    per-unit rows; the homepage_body chase fallback recovers it via
    proximity scan with a wider window + beds-only merge.

    To pin the chase behavior without depending on the primary parser's
    cross-plan sqft leak (which itself fills sqft into per-unit rows
    incorrectly when plan headers sit close to per-unit lines), we put
    enough filler between the plan header and the per-unit rows that
    the 100-char primary scan can't reach.
    """
    homepage = (
        "Style A1 1 Bedroom 1 Bath 1001 Sq Ft Starting at $2,580 "
        # ~250 chars of filler so the primary parser's 100-char
        # lookahead from the per-unit rows below can't reach 1001.
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
        "Sed do eiusmod tempor incididunt ut labore et dolore magna "
        "aliqua. Ut enim ad minim veniam, quis nostrud exercitation "
        "ullamco laboris nisi ut aliquip ex ea commodo consequat. "
        # Per-unit rows for 1-bed.
        "Bldg/Unit 03-0712: 1 Bedroom 1 Bath. Asking $2,615 per month. "
        "Bldg/Unit 03-0813: 1 Bedroom 1 Bath. Asking $2,585 per month. "
    )
    result = await _make_adapter_call(homepage, subpage_html="")
    units = result.units or []
    one_bed = [u for u in units if u.get("bedrooms") == "1"]
    assert one_bed, f"no 1-bed rows: {units}"
    # ALL 1-bed rows (plan header + per-unit) must have sqft=1001 after
    # the chase. Pre-fix the per-unit rows would have sqft="".
    sqfts = {u.get("sqft") for u in one_bed}
    assert sqfts == {"1001"}, f"expected all 1-bed sqft=1001, got {sqfts}"
    # Chase fired — at least one chase error in the errors list.
    assert any("sqft chase" in e for e in result.errors), result.errors


@pytest.mark.asyncio
async def test_adapter_no_chase_when_landing_has_enough_sqft() -> None:
    """Defensive: when ≥50% of landing rows already have sqft (e.g.
    Country Village), the chase MUST NOT fire — no spurious probe."""
    # All rows have sqft on landing — chase should not fire.
    homepage = (
        "1 Bedroom 1 Bath 750 Sq Ft Starting at $852 Deposit $300 "
        "2 Bedroom 1 Bath 850 Sq Ft Starting at $978 Deposit $300 "
        "2 Bedroom 2 Bath 950 Sq Ft Starting at $982 Deposit $300 "
        "3 Bedroom 2 Bath 1150 Sq Ft Starting at $1100 Deposit $400"
    )

    site = "https://x.example.com/"
    fake = _make_fake_probe({})  # no subpages
    ctx = _StubCtx(site, _StubFR(homepage, site))
    with patch("ma_poc.pms.adapters._probe.probe_get", fake):
        result = await GenericPlanTextAdapter().extract(_StubPage(site), ctx)
    units = result.units or []
    assert len(units) == 4
    # Chase did not fire — zero probe calls.
    assert fake.call_count == 0, "chase fired despite full sqft coverage"
    # Sqft preserved from primary extraction.
    sqfts = sorted(u["sqft"] for u in units)
    assert sqfts == ["1150", "750", "850", "950"]


# ── 5. Regression guards ──


def test_regression_elementor_starting_at_still_works() -> None:
    """The Elementor "Starting at $X" backwards-lookup fix (ae593e8)
    must remain intact: 1045 on the Park-style cards still emit rows."""
    body = (
        "Residences starting at $2,127\n"
        "1 and 2 Bedroom Luxury Apartments\n"
        "Starting at $2,865 2 Bed | 2 Bath\n"
        "Starting at $2,127 1 Bed | 1 Bath\n"
    )
    rows = parse_generic_plan_text(body, "http://www.1045onthepark.com/")
    rents = {int(r["market_rent_low"]) for r in rows if r.get("market_rent_low")}
    assert 2127 in rents
    assert 2865 in rents


def test_regression_stargate_from_pattern_still_works() -> None:
    """Stargate-style "X Bedroom / Y Bathroom From $Z" rows still parse
    via the forward lookahead. _SQFT_RE expansion must not break
    rent-only rows."""
    body = (
        "1 Bedroom / 1 Bathroom From $1275 "
        "2 Bedroom / 2 Bathroom From $1400 "
        "3 Bedroom / 2 Bathroom From $1655"
    )
    rows = parse_generic_plan_text(body, "u")
    rents = sorted(r["market_rent_low"] for r in rows)
    assert rents == [1275, 1400, 1655]


def test_regression_countryvillage_sqft_still_extracted_inline() -> None:
    """Country Village has sqft INSIDE the main parser's 100-char
    lookahead. The _SQFT_RE expansion must not break the existing
    inline sqft extraction."""
    body = (
        "1 Bedroom 1 Bath 750 Sq Ft Starting at $852 Deposit $300 "
        "2 Bedroom 1 Bath 850 Sq Ft Starting at $978 Deposit $300"
    )
    rows = parse_generic_plan_text(body, "u")
    sqfts = sorted(r["sqft"] for r in rows)
    assert sqfts == ["750", "850"]
