

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
