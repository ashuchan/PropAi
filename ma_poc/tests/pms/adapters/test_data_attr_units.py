"""Rich data-attribute unit anchors → UNIT-LEVEL, code-only (#93, 2026-07-31).

Entrata ProspectPortal marketing pages (e.g. thevillagedallas.com/…/
the-village-lakes → villagelakeslpc.prospectportal.com) render each apartment
as ``div.unit-body[data-unit-id][data-unit-number]`` carrying every field in
data-attributes. The generic DOM cascade never admitted the container (no
matching class; ``[data-unit]`` does not match ``data-unit-id``), so the
property landed plan-level despite publishing its full roster in the STATIC
HTML. ``_extract_data_attr_unit`` reads the attributes directly.

Fixture ``village_lakes.html`` is the live static page (fetched compliant
DIRECT GET 2026-07-31): 24 apartments, all priced.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ma_poc.pms.adapters._html_extract import extract_units_from_dom

_FIX = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "avail_table"
    / "village_lakes.html"
)


@pytest.fixture(scope="module")
def units() -> list[dict]:
    html = _FIX.read_text(encoding="utf-8")
    rows, _ = extract_units_from_dom(
        html, "https://www.thevillagedallas.com/properties/the-village-lakes/"
    )
    return rows


def test_recovers_all_24_units(units: list[dict]) -> None:
    assert len(units) == 24


def test_every_unit_is_gold(units: list[dict]) -> None:
    # real distinct anchor + numeric rent in the same row = strict gold.
    ids = [str(u.get("unit_id") or "") for u in units]
    assert all(ids) and len(set(ids)) == 24
    assert all(u.get("market_rent_low") for u in units)


def test_field_values_for_a_known_unit(units: list[dict]) -> None:
    by = {u.get("unit_number"): u for u in units}
    u = by["0609"]
    assert u["unit_id"] == "4112304"
    assert u["market_rent_low"] == 1868
    assert str(u["sqft"]) == "879"
    assert str(u["beds"]) == "2"
    assert str(u["baths"]) == "1"
    assert u["building"] == "Building 6"
    assert u["availability_status"] == "AVAILABLE"
    assert u["availability_date"] == "2026-07-23"


class TestSyntheticGate:
    def test_neither_id_nor_number_is_skipped(self) -> None:
        html = '<html><body><div data-rent="1500">x</div></body></html>'
        rows, _ = extract_units_from_dom(html, "https://x.test/")
        assert rows == []

    def test_data_status_unavailable(self) -> None:
        html = (
            '<html><body>'
            '<div data-unit-id="u1" data-unit-number="101" data-rent="1500" '
            'data-status="Occupied No Notice" data-bedrooms="1">x</div>'
            '</body></html>'
        )
        rows, _ = extract_units_from_dom(html, "https://x.test/")
        assert len(rows) == 1
        assert rows[0]["availability_status"] == "UNAVAILABLE"
        assert rows[0]["unit_id"] == "u1"
