"""RentCafe Nestin per-plan DOM recovery (2026-05-20).

The 35-prop JSON-LD recovery probe (see
``project_jsonld_recovery_2026-05-20.md``) confirmed that 89% of the
298-prop JSON-LD ALL_fail bucket are RentCafe-Nestin marketing sites with
real unit data on ``/floorplans/{plan-slug}`` per-plan detail pages.

Tests cover:
* ``is_nestin_template`` signal detection (positive + negative)
* ``_find_floorplan_detail_urls`` discovery from index HTML
* Layout A1 (``<table>`` shape — Stonewater / Chatwell / Hayden Place)
* Layout A2 (card / div-block shape — Altair / Hampton / LINQ / Meridian)
* End-to-end ``recover_rentcafe_nestin_per_plan`` with injected fetcher
* Layout B (plan-cards-only — Blueberry shape) — recovery returns ``[]``,
  caller falls back to existing plan-level emit
* Sub-$1000 rent regression (Chatwell ``$823.00``) — proves the rent
  regex correction applied (per the JSON-LD memo's regex rule)
* No-Nestin-signal short-circuit (plain Squarespace / Wix should not
  fire the Nestin recovery)
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ma_poc.pms.adapters._rentcafe_nestin import (
    _find_floorplan_detail_urls,
    _money_to_int,
    _normalize_unit_number,
    is_nestin_template,
    parse_nestin_detail_page,
    recover_rentcafe_nestin_per_plan,
)

# ── Layout A1 (table) — Stonewater / Chatwell / Hayden Place shape ──────────


_TABLE_LAYOUT_HTML = """
<html><body>
<h2>1 Bedroom | 1 Bathroom</h2>
<table>
  <thead>
    <tr>
      <th>Apartment</th>
      <th>Sq. Ft.</th>
      <th>Rent</th>
      <th>Date Available</th>
      <th>Action</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td data-label="Apartment">#4112-3</td>
      <td data-label="Sq. Ft.">900</td>
      <td data-label="Rent">$1,099.00</td>
      <td data-label="Date Available">5/20/2026</td>
      <td><a class="btn">APPLY NOW</a></td>
    </tr>
    <tr>
      <td data-label="Apartment">#1120</td>
      <td data-label="Sq. Ft.">576</td>
      <td data-label="Rent">$823.00</td>
      <td data-label="Date Available">7/14/2026</td>
      <td><a class="btn">APPLY NOW</a></td>
    </tr>
  </tbody>
</table>
</body></html>
"""


def test_parse_table_layout_extracts_unit_rows() -> None:
    units = parse_nestin_detail_page(
        _TABLE_LAYOUT_HTML, "https://x.com/floorplans/a1", "1 Bedroom | 1 Bathroom"
    )
    assert len(units) == 2
    u1, u2 = units
    # Real apartment numbers preserved (no inferred_ prefix, no leading #)
    assert u1["unit_number"] == "4112-3"
    assert u2["unit_number"] == "1120"
    # Sub-$1000 rents must be captured (Chatwell-style $823.00 was missed by
    # the old ``\$[1-9][0-9],?[0-9]{2,3}`` regex; this is the regression
    # guard for the rent-regex correction.)
    assert u2["market_rent_low"] == 823
    assert u2["market_rent_high"] == 823
    assert u1["market_rent_low"] == 1099
    assert u1["sqft"] == "900"
    assert u2["sqft"] == "576"
    assert u1["availability_date"] == "5/20/2026"
    assert u2["availability_date"] == "7/14/2026"
    # Provenance: extraction_tier MUST be Nestin-flavored so Path C and
    # the verdict labeler can distinguish from generic-DOM extraction.
    assert u1["extraction_tier"] == "TIER_1_DOM_RENTCAFE_NESTIN"


def test_parse_table_layout_no_data_label_uses_positional_fallback() -> None:
    """Some Nestin tables omit ``data-label`` attrs (older template).
    Positional ``<td>`` index → header should still work."""
    html_no_attrs = _TABLE_LAYOUT_HTML.replace('data-label="Apartment"', "").replace(
        'data-label="Sq. Ft."', ""
    ).replace('data-label="Rent"', "").replace('data-label="Date Available"', "")
    units = parse_nestin_detail_page(html_no_attrs, "https://x.com/floorplans/a1", "A1")
    assert len(units) == 2
    assert units[0]["unit_number"] == "4112-3"
    assert units[1]["market_rent_low"] == 823


def test_parse_table_layout_rejects_table_without_required_headers() -> None:
    """A table without ``Apartment`` + ``Rent`` + ``Date Available`` headers
    (e.g. a feature-comparison table) must NOT be admitted as a unit table."""
    misc_table = """
    <html><body><table>
      <thead><tr><th>Feature</th><th>Available</th></tr></thead>
      <tbody><tr><td>Gym</td><td>Yes</td></tr></tbody>
    </table></body></html>
    """
    assert parse_nestin_detail_page(misc_table, "u", "p") == []


# ── Layout A2 (card / div-block) — Altair / Hampton / LINQ shape ────────────


_CARD_LAYOUT_HTML = """
<html><body>
<h2>Coronado</h2>
<div class="floorplan-units">
  <div class="td-card-available unit-container">
    <h4>APARTMENT: # 0200</h4>
    <p>Starting at: $2,622.88</p>
    <p>Available 5/21/2026</p>
  </div>
  <div class="td-card-available unit-container">
    <h4>APARTMENT: # 0613</h4>
    <p>Starting at: $2,756.88</p>
  </div>
  <div class="td-card-available unit-container">
    <h4>APARTMENT: # 0809</h4>
    <p>Starting at: $2,632.88</p>
  </div>
</div>
</body></html>
"""


def test_parse_card_layout_extracts_unit_blocks() -> None:
    units = parse_nestin_detail_page(
        _CARD_LAYOUT_HTML, "https://x.com/floorplans/coronado", "Coronado"
    )
    assert len(units) == 3
    nums = sorted(u["unit_number"] for u in units)
    assert nums == ["0200", "0613", "0809"]
    # Rents preserve decimals via _money_to_int (rounds to nearest)
    rents = sorted(u["market_rent_low"] for u in units)
    assert rents == [2623, 2633, 2757]  # $2,622.88 / $2,632.88 / $2,756.88 rounded
    assert units[0]["extraction_tier"] == "TIER_1_DOM_RENTCAFE_NESTIN"
    # Date present on the first card only — others omit it gracefully
    dated = [u for u in units if u["availability_date"]]
    assert len(dated) == 1
    assert dated[0]["availability_date"] == "5/21/2026"


def test_parse_card_layout_with_pipe_separator() -> None:
    """Hampton Meridian shape: ``Apartment # 061`` (no colon, no leading #
    on the value) — same regex must catch."""
    hampton_like = """
    <html><body>
      <div><h4>Apartment # 061</h4><p>$1,499</p></div>
      <div><h4>Apartment # 113</h4><p>$1,499</p></div>
    </body></html>
    """
    units = parse_nestin_detail_page(hampton_like, "u", "1x1")
    assert len(units) == 2
    assert {u["unit_number"] for u in units} == {"061", "113"}
    assert all(u["market_rent_low"] == 1499 for u in units)


def test_parse_card_layout_unit_without_rent_is_skipped() -> None:
    """A card with apartment-# text but NO rent must be skipped — emitting
    a row with no rent inflates SUCCESS_PLAN_LEVEL audit count."""
    no_rent = """
    <html><body>
      <div><h4>APARTMENT: # 999</h4><p>Call for pricing</p></div>
    </body></html>
    """
    assert parse_nestin_detail_page(no_rent, "u", "p") == []


def test_parse_card_layout_deduplicates_unit_numbers() -> None:
    """When the same apt-# appears in nested blocks (real-world: header +
    summary card), only one row is emitted."""
    dup = """
    <html><body>
      <div>
        <h4>APARTMENT: # 0200</h4>
        <div>
          <span>APARTMENT: # 0200</span>
          <p>$2,622</p>
        </div>
      </div>
    </body></html>
    """
    units = parse_nestin_detail_page(dup, "u", "p")
    assert len(units) == 1
    assert units[0]["unit_number"] == "0200"


# ── Layout preference: A1 (table) wins over A2 (card) ───────────────────────


def test_table_layout_wins_when_both_shapes_present() -> None:
    """Hayden Place has both a table AND card blocks. Table is the cleaner
    structured shape and must take precedence (date / sqft / rent columns
    are explicit)."""
    both = _TABLE_LAYOUT_HTML.replace(
        "</body>",
        '<div><h4>APARTMENT: # 9999</h4><p>$5,000</p></div></body>',
    )
    units = parse_nestin_detail_page(both, "u", "p")
    # Table emits 4112-3 + 1120. Card-only 9999 must NOT appear because
    # the table path returned units (and is preferred).
    nums = {u["unit_number"] for u in units}
    assert nums == {"4112-3", "1120"}


# ── Nestin signal detection ─────────────────────────────────────────────────


def test_is_nestin_template_positive() -> None:
    assert is_nestin_template(
        '<html><body><img src="https://resource.rentcafe.com/x.png"></body></html>'
    )


def test_is_nestin_template_negative_for_plain_squarespace() -> None:
    """Plain Squarespace / Wix shells without RentCafe CDN must NOT fire."""
    assert not is_nestin_template(
        '<html><body><img src="https://static1.squarespace.com/x.png"></body></html>'
    )


def test_is_nestin_template_handles_empty_html() -> None:
    assert not is_nestin_template("")
    assert not is_nestin_template(None)  # type: ignore[arg-type]


# ── Detail-URL discovery ────────────────────────────────────────────────────


def test_find_floorplan_detail_urls_extracts_per_plan_links() -> None:
    index_html = """
    <html><body>
      <a href="/floorplans/a1">A1 Plan</a>
      <a href="/floorplans/2-bedroom-2-bath">2BR/2BA</a>
      <a href="/floorplans">All plans</a>  <!-- index itself, skip -->
      <a href="/amenities">Amenities</a>  <!-- non-floorplan, skip -->
      <a href="/floorplans/a1">A1 (dup)</a>  <!-- dedupe -->
    </body></html>
    """
    urls = _find_floorplan_detail_urls(index_html, "https://example.com")
    assert urls == [
        "https://example.com/floorplans/a1",
        "https://example.com/floorplans/2-bedroom-2-bath",
    ]


def test_find_floorplan_detail_urls_handles_encoded_pipe() -> None:
    """Chatwell URL: ``/floorplans/1-bed-%7c-1-bath`` (pipe is URL-encoded)."""
    index_html = '<a href="/floorplans/1-bed-%7c-1-bath">1BR/1BA</a>'
    urls = _find_floorplan_detail_urls(index_html, "https://chatwellclub-apts.com")
    assert urls == ["https://chatwellclub-apts.com/floorplans/1-bed-%7c-1-bath"]


def test_find_floorplan_detail_urls_empty_html_returns_empty_list() -> None:
    assert _find_floorplan_detail_urls("", "https://x.com") == []
    assert _find_floorplan_detail_urls("<html></html>", "") == []


# ── End-to-end recover_rentcafe_nestin_per_plan ─────────────────────────────


@dataclass
class _FakeResp:
    status_code: int
    text: str


@pytest.mark.asyncio
async def test_recover_e2e_stonewater_shape() -> None:
    """Full recovery flow: landing has RentCafe-CDN signal + detail-page
    link; fetcher returns the table layout. Should emit 2 unit rows."""
    landing = """
    <html><body>
      <img src="https://resource.rentcafe.com/logo.png">
      <a href="/floorplans/a1">A1</a>
    </body></html>
    """

    def fetcher(url: str) -> _FakeResp:
        if url.endswith("/floorplans/a1"):
            return _FakeResp(200, _TABLE_LAYOUT_HTML)
        return _FakeResp(404, "")

    units, source = await recover_rentcafe_nestin_per_plan(
        landing,
        "https://www.stonewaterpark.com",
        fetcher=fetcher,  # type: ignore[arg-type]
    )
    assert len(units) == 2
    assert {u["unit_number"] for u in units} == {"4112-3", "1120"}
    assert source == "https://www.stonewaterpark.com/floorplans"


@pytest.mark.asyncio
async def test_recover_e2e_fetches_index_when_no_links_on_landing() -> None:
    """When landing has the Nestin signal but no /floorplans/ detail links,
    the recovery fetches /floorplans index to discover them."""
    landing = '<img src="https://resource.rentcafe.com/x.png">'  # no detail links
    fp_index = '<a href="/floorplans/a1">A1</a>'

    fetches: list[str] = []

    def fetcher(url: str) -> _FakeResp:
        fetches.append(url)
        if url.endswith("/floorplans"):
            return _FakeResp(200, fp_index)
        if url.endswith("/floorplans/a1"):
            return _FakeResp(200, _TABLE_LAYOUT_HTML)
        return _FakeResp(404, "")

    units, _ = await recover_rentcafe_nestin_per_plan(
        landing,
        "https://x.com",
        fetcher=fetcher,  # type: ignore[arg-type]
    )
    assert len(units) == 2
    # Fetched /floorplans index first, then the detail page
    assert fetches[0].endswith("/floorplans")
    assert fetches[1].endswith("/floorplans/a1")


@pytest.mark.asyncio
async def test_recover_no_nestin_signal_short_circuits() -> None:
    """Plain Squarespace landing → no recovery attempt, no fetcher calls."""
    landing = '<img src="https://static1.squarespace.com/x.png">'
    calls = []

    def fetcher(url: str) -> _FakeResp:
        calls.append(url)
        return _FakeResp(200, "")

    units, source = await recover_rentcafe_nestin_per_plan(
        landing, "https://plain.com", fetcher=fetcher,  # type: ignore[arg-type]
    )
    assert units == []
    assert source == ""
    assert calls == []  # never invoked


@pytest.mark.asyncio
async def test_recover_layout_b_blueberry_shape_returns_empty() -> None:
    """Blueberry-shape: Nestin signal present, plan cards on index, but
    no per-plan detail pages (no /floorplans/{slug} links + index fetch
    returns no detail links either). Recovery returns [] — caller falls
    back to its existing plan-level emit (Layout B is intentionally not
    a Nestin per-plan responsibility; the JSON-LD memo flags this as the
    ~5% plan-cards-only sub-bucket)."""
    landing = """
    <html><body>
      <img src="https://resource.rentcafe.com/x.png">
      <div class="plan-card">
        <h3>One Bedroom</h3>
        <span>Starting at $1,505.00</span>
      </div>
    </body></html>
    """
    fp_index = """
    <html><body>
      <img src="https://resource.rentcafe.com/x.png">
      <div class="plan-card">
        <h3>One Bedroom</h3>
        <span>Starting at $1,505.00</span>
      </div>
    </body></html>
    """

    def fetcher(url: str) -> _FakeResp:
        return _FakeResp(200, fp_index)

    units, _ = await recover_rentcafe_nestin_per_plan(
        landing, "https://blueberry.com", fetcher=fetcher,  # type: ignore[arg-type]
    )
    assert units == []


@pytest.mark.asyncio
async def test_recover_handles_fetcher_exceptions() -> None:
    """Fetcher raising → recovery returns ``[]`` (never propagates)."""
    landing = '<img src="https://resource.rentcafe.com/x.png"><a href="/floorplans/a1">A1</a>'

    def fetcher(url: str) -> _FakeResp:
        raise RuntimeError("network error")

    units, _ = await recover_rentcafe_nestin_per_plan(
        landing, "https://x.com", fetcher=fetcher,  # type: ignore[arg-type]
    )
    assert units == []


@pytest.mark.asyncio
async def test_recover_partial_success_with_one_detail_404() -> None:
    """When one detail page 404s but another returns valid table, emit
    units from the working one."""
    landing = """
    <img src="https://resource.rentcafe.com/x.png">
    <a href="/floorplans/a1">A1</a>
    <a href="/floorplans/b1">B1</a>
    """

    def fetcher(url: str) -> _FakeResp:
        if url.endswith("/floorplans/a1"):
            return _FakeResp(200, _TABLE_LAYOUT_HTML)
        return _FakeResp(404, "")

    units, _ = await recover_rentcafe_nestin_per_plan(
        landing, "https://x.com", fetcher=fetcher,  # type: ignore[arg-type]
    )
    assert len(units) == 2


# ── Helper-function unit tests ──────────────────────────────────────────────


def test_money_to_int_handles_sub_1000_decimals() -> None:
    """Regression for the Chatwell ``$823.00`` case."""
    assert _money_to_int("$823.00") == 823
    assert _money_to_int("$1,099.00") == 1099
    assert _money_to_int("Starting at: $2,622.88") == 2623
    assert _money_to_int("rent is $1,500") == 1500


def test_money_to_int_returns_none_on_bad_input() -> None:
    assert _money_to_int("") is None
    assert _money_to_int("no money here") is None
    assert _money_to_int("Call for pricing") is None


def test_normalize_unit_number_strips_hash_and_whitespace() -> None:
    assert _normalize_unit_number("#1120") == "1120"
    assert _normalize_unit_number("# 4112-3") == "4112-3"
    assert _normalize_unit_number("  #B306  ") == "B306"
    assert _normalize_unit_number("") == ""


# ─────────────────────────────────────────────────────────────────────
# 2026-05-20 pre-canary probe findings — three bugs surfaced by live
# curl_cffi probes against verified targets:
#   * Chatwell / Hayden table parser: `data-label` attrs absent on real
#     HTML; positional `<td>` text includes the "Apartment" sr-only
#     label prefix → unit_number polluted with "Apartment: #1120"
#   * Altair card parser: `\bApartment\b[:\s#]+` matched "Apartment Homes"
#     / "Apartment Available" chrome text → bogus unit numbers
#   * Stonewater: static HTML uses `<button id="4112-3" onclick=
#     "applyGAClick(...)">` — neither table nor card. New Layout A3 added.
# ─────────────────────────────────────────────────────────────────────


def test_normalize_unit_number_strips_apartment_label_prefix() -> None:
    """Real Chatwell / Hayden tables omit ``data-label`` attrs; positional
    `<td>` text concatenates the "Apartment" sr-only label with the value."""
    from ma_poc.pms.adapters._rentcafe_nestin import _normalize_unit_number
    assert _normalize_unit_number("Apartment: #1120") == "1120"
    assert _normalize_unit_number("Apartment #1120") == "1120"
    assert _normalize_unit_number("Apartment 4112-3") == "4112-3"
    assert _normalize_unit_number("APARTMENT: # B306") == "B306"


def test_card_layout_rejects_apartment_label_false_matches() -> None:
    """Without the `#` requirement, the regex matched chrome text like
    'Apartment Homes' / 'Apartment Available' — bogus units. Real Altair
    page contains 'Altair Apartment Homes' in the header; that must NOT
    produce a unit row."""
    from ma_poc.pms.adapters._rentcafe_nestin import _parse_card_layout
    chrome_text = """
    <html><body>
      <h1>Altair Apartment Homes</h1>
      <p>Apartment Available now! Call us with questions.</p>
      <div><h4>APARTMENT: # 0200</h4><p>Starting at: $2,622</p></div>
    </body></html>
    """
    units = _parse_card_layout(chrome_text, "u", "p")
    assert len(units) == 1
    assert units[0]["unit_number"] == "0200"


def test_applyga_button_layout_extracts_units() -> None:
    """Stonewater shape: `<a id="4112-3" onclick="applyGAClick('A1',
    '1 Bed(s)', '900', '1099.00', ...)">`. Verified live 2026-05-20."""
    from ma_poc.pms.adapters._rentcafe_nestin import _parse_applyga_button_layout
    html = """
    <html><body>
      <a id="4112-3" onclick="applyGAClick('A1', '1 Bed(s)', '900', '1099.00', '1099.00', '4112-3')">Apply Now</a>
      <a id="306-2" onclick="applyGAClick('A1', '1 Bed(s)', '900', '1050.00', '1050.00', '306-2')">Apply Now</a>
    </body></html>
    """
    units = _parse_applyga_button_layout(html, "u", "A1")
    assert len(units) == 2
    assert {u["unit_number"] for u in units} == {"4112-3", "306-2"}
    rents = sorted(u["market_rent_low"] for u in units)
    assert rents == [1050, 1099]
    # beds-label parsed to numeric
    assert all(u["bedrooms"] == "1" for u in units)
    assert all(u["sqft"] == "900" for u in units)


def test_applyga_button_handles_studio_beds_label() -> None:
    """Studio beds-label → bedrooms='0'."""
    from ma_poc.pms.adapters._rentcafe_nestin import _parse_applyga_button_layout
    html = (
        '<a id="100" onclick="applyGAClick(\'S1\', \'Studio\', \'500\', \'1200.00\', '
        "'1200.00', '100')\">Apply Now</a>"
    )
    units = _parse_applyga_button_layout(html, "u", "S1")
    assert len(units) == 1
    assert units[0]["bedrooms"] == "0"


def test_applyga_button_rejects_rows_without_rent() -> None:
    """Apply buttons whose onclick rent arg is missing or zero must NOT
    emit — without rent it's not a real unit row."""
    from ma_poc.pms.adapters._rentcafe_nestin import _parse_applyga_button_layout
    html = (
        '<a id="999" onclick="applyGAClick(\'A1\', \'1 Bed(s)\', \'900\', \'\', '
        "'', '999')\">Apply Now</a>"
    )
    units = _parse_applyga_button_layout(html, "u", "A1")
    assert units == []


def test_apply_button_layout_wins_over_card_when_no_table() -> None:
    """parse_nestin_detail_page cascade: table → applyGA-button → card.
    When the HTML has BOTH apply-buttons AND free-form card text, the
    structured apply-button layout wins (more reliable)."""
    from ma_poc.pms.adapters._rentcafe_nestin import parse_nestin_detail_page
    html = (
        # Apply button shape (Stonewater)
        '<a id="4112-3" onclick="applyGAClick(\'A1\', \'1 Bed(s)\', \'900\', '
        "'1099.00', '1099.00', '4112-3')\">Apply Now</a>"
        # Free-form chrome text that the card regex would match
        '<p>APARTMENT: # 9999 — Starting at: $5,000</p>'
    )
    units = parse_nestin_detail_page(html, "u", "A1")
    # Layout A3 wins; the 9999 card-text row should NOT appear
    nums = {u["unit_number"] for u in units}
    assert "4112-3" in nums
    assert "9999" not in nums


# ─────────────────────────────────────────────────────────────────────
# 2026-05-20 pre-canary e2e finding — Playwright rewrites relative
# anchors to absolute URLs in ``page.content()`` output. Raw curl_cffi
# HTML keeps them relative. Both shapes must be accepted by the
# detail-URL finder, otherwise the recovery silently emits zero units
# when the adapter receives Playwright-rendered HTML (the production
# pipeline path).
# ─────────────────────────────────────────────────────────────────────


def test_find_floorplan_detail_urls_accepts_absolute_hrefs() -> None:
    """Playwright rewrites ``<a href="/floorplans/a">`` to
    ``<a href="https://www.stonewaterpark.com/floorplans/a">``. Without
    accepting both shapes, the recovery emits 0 units in production but
    works fine in standalone tests — verified live 2026-05-20 against
    Stonewater + Chatwell pipeline runs."""
    from ma_poc.pms.adapters._rentcafe_nestin import _find_floorplan_detail_urls

    abs_html = (
        '<a href="https://www.stonewaterpark.com/floorplans/a">A</a>'
        '<a href="https://www.stonewaterpark.com/floorplans/b1r">B1R</a>'
    )
    out = _find_floorplan_detail_urls(abs_html, "https://www.stonewaterpark.com")
    assert out == [
        "https://www.stonewaterpark.com/floorplans/a",
        "https://www.stonewaterpark.com/floorplans/b1r",
    ]


def test_find_floorplan_detail_urls_mixed_relative_and_absolute() -> None:
    """Some real-world HTML carries a mix — raw + JS-rewritten anchors."""
    from ma_poc.pms.adapters._rentcafe_nestin import _find_floorplan_detail_urls

    mixed_html = (
        '<a href="/floorplans/a">A</a>'
        '<a href="https://www.stonewaterpark.com/floorplans/b1r">B1R</a>'
    )
    out = _find_floorplan_detail_urls(mixed_html, "https://www.stonewaterpark.com")
    assert set(out) == {
        "https://www.stonewaterpark.com/floorplans/a",
        "https://www.stonewaterpark.com/floorplans/b1r",
    }


def test_find_floorplan_detail_urls_rejects_foreign_domain_absolute_hrefs() -> None:
    """An absolute href that points to a DIFFERENT host must be ignored
    — that's a cross-site link, not a per-plan detail URL on this property."""
    from ma_poc.pms.adapters._rentcafe_nestin import _find_floorplan_detail_urls

    foreign_html = '<a href="https://other-site.com/floorplans/a">A</a>'
    out = _find_floorplan_detail_urls(foreign_html, "https://www.stonewaterpark.com")
    assert out == []


def test_find_floorplan_detail_urls_dedup_across_relative_and_absolute() -> None:
    """If the same plan appears both as relative and absolute (e.g. JS
    duplicated the anchor), emit only once."""
    from ma_poc.pms.adapters._rentcafe_nestin import _find_floorplan_detail_urls

    dup_html = (
        '<a href="/floorplans/a">A rel</a>'
        '<a href="https://www.stonewaterpark.com/floorplans/a">A abs</a>'
    )
    out = _find_floorplan_detail_urls(dup_html, "https://www.stonewaterpark.com")
    assert out == ["https://www.stonewaterpark.com/floorplans/a"]


# ─────────────────────────────────────────────────────────────────────
# 2026-05-20 e2e finding — Cloudflare path-scoped clearance cookies.
# The homepage's ``cf_clearance`` cookie (minted by the L1 fetcher and
# threaded through the ContextVar) caused 13/13 detail-page 403s. The
# nestin code now clears the ContextVar around its probe_get calls so
# the detail fetch triggers a fresh CF challenge (which chrome120
# passes cleanly with 200). Other adapters keep the cookies intact.
# ─────────────────────────────────────────────────────────────────────


async def test_recovery_clears_clearance_cookies_before_probe_get() -> None:
    """The Nestin recovery's default-path fetcher must reset the
    ``_clearance_cookies`` ContextVar before each probe_get so the
    homepage's stale cf_clearance doesn't poison detail-page fetches."""
    from typing import Any as _Any
    from unittest.mock import MagicMock, patch

    from ma_poc.pms.adapters import _probe
    from ma_poc.pms.adapters import _rentcafe_nestin as nest

    # Install fake "homepage clearance" cookies before the recovery runs
    sentinel = {"cf_clearance": "STALE_HOMEPAGE", "__cf_bm": "STALE_HOMEPAGE"}
    tok = _probe.set_clearance_cookies(sentinel)
    try:
        captured_cookies: list[dict[str, str] | None] = []

        def _spy_probe_get(url: str, **kw: _Any) -> _Any:
            # Snapshot the ContextVar's value when probe_get sees it
            captured_cookies.append(_probe._clearance_cookies.get())
            mock = MagicMock()
            mock.status_code = 200
            mock.text = (
                "<html><body>"
                '<table><thead><tr><th>Apartment</th><th>Rent</th>'
                "<th>Date Available</th></tr></thead>"
                "<tbody><tr><td>#1120</td><td>$1,200</td>"
                "<td>05/01/26</td></tr></tbody></table></body></html>"
            )
            return mock

        # Provide landing HTML with one detail URL so we exercise the
        # detail-fetch path (default fetcher = probe_get).
        landing = (
            "<html><body>"
            '<link rel="preconnect" href="https://resource.rentcafe.com">'
            '<a href="/floorplans/a">A</a>'
            "</body></html>"
        )

        with patch.object(_probe, "probe_get", _spy_probe_get):
            units, _src = await nest.recover_rentcafe_nestin_per_plan(
                landing,
                "https://www.example.com",
            )

        # The recovery saw at least one probe_get call …
        assert len(captured_cookies) >= 1, "probe_get was never called"
        # … and the ContextVar was cleared (empty dict or None) — not
        # the stale homepage sentinel. ``set_clearance_cookies(None)``
        # stores an empty dict; reading it back gives ``{}``.
        assert all(not c for c in captured_cookies), (
            f"probe_get saw stale homepage cookies: {captured_cookies}"
        )
        assert all(
            c is None or "cf_clearance" not in c for c in captured_cookies
        ), f"stale cf_clearance leaked into probe_get: {captured_cookies}"
        # End-to-end: the unit was extracted (proves the cookie-clear
        # didn't break the happy path).
        assert len(units) == 1
        assert units[0]["unit_number"] == "1120"
    finally:
        _probe.reset_clearance_cookies(tok)


async def test_recovery_restores_clearance_cookies_after_fetches() -> None:
    """After Nestin recovery returns, the ContextVar must be back to
    its pre-call state so other adapters keep their homepage clearance."""
    from typing import Any as _Any
    from unittest.mock import MagicMock

    from ma_poc.pms.adapters import _probe
    from ma_poc.pms.adapters import _rentcafe_nestin as nest

    original = {"cf_clearance": "ORIGINAL_HOMEPAGE_CLEARANCE"}
    tok = _probe.set_clearance_cookies(original)
    try:
        landing = (
            "<html><body>"
            '<link rel="preconnect" href="https://resource.rentcafe.com">'
            "</body></html>"
        )

        # Stub fetcher (NOT probe_get) — we just want to verify the
        # ContextVar is restored. With no detail urls discovered, the
        # recovery falls back to fetching /floorplans index via our
        # explicit fetcher (which doesn't touch the ContextVar).
        def _stub(url: str) -> _Any:
            mock = MagicMock()
            mock.status_code = 404
            mock.text = ""
            return mock

        await nest.recover_rentcafe_nestin_per_plan(
            landing, "https://www.example.com", fetcher=_stub,
        )

        # After recovery, the cookies should be back to the original
        # value — not None, not modified.
        current = _probe._clearance_cookies.get()
        assert current == original, (
            f"ContextVar was not restored: got {current}, expected {original}"
        )
    finally:
        _probe.reset_clearance_cookies(tok)
