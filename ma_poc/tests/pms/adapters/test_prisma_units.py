"""goprisma (Corsa Management) per-unit table → UNIT-LEVEL (#93, 2026-07-31).

`tr.prisma-units-row` renders each apartment in an availability table but the
generic DOM path read no id, so the tab-duplicated rows collapsed by rent/sqft
fingerprint to 3 broken rows and minted a phantom `unit_number="Number"` from
the "Unit Number" column label. The dedicated `_extract_prisma_unit` compact
extractor reads `data-_id` (goprisma unit PK) + `data-unoitId` so the loop's
`unit_id`-first dedup recovers the true 5-unit roster.

Ground truth (saved fixture, Greenwood Village): L16-2 / L33-2 (2BR, 700 sqft,
$1,740) · N207-1 ($1,450) · M397-2 ($1,500) · W116-2 (1BR, 560 sqft, $1,500).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ma_poc.pms.adapters._html_extract import extract_units_from_dom

_FIX = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "avail_table"
    / "corsa_greenwood.html"
)


@pytest.fixture(scope="module")
def units() -> list[dict]:
    html = _FIX.read_text(encoding="utf-8")
    rows, _ = extract_units_from_dom(html, "https://corsamanagement.goprisma.com/")
    return rows


def test_recovers_five_units(units: list[dict]) -> None:
    # Not 3 (fingerprint-collapsed), not 12 (tab-duplicated) — the real 5.
    assert len(units) == 5


def test_unit_numbers_are_the_real_apartments(units: list[dict]) -> None:
    nums = {u.get("unit_number") for u in units}
    assert nums == {"L16-2", "L33-2", "N207-1", "M397-2", "W116-2"}


def test_unit_ids_are_distinct_goprisma_pks(units: list[dict]) -> None:
    ids = [str(u.get("unit_id") or "") for u in units]
    assert all(ids) and len(set(ids)) == 5  # distinct real PKs, none inferred
    assert not any(i.startswith("inferred_") for i in ids)


def test_no_phantom_number_unit(units: list[dict]) -> None:
    # The "Unit Number" column label must not become a unit.
    assert "Number" not in {u.get("unit_number") for u in units}


def test_every_unit_has_rent_and_fields(units: list[dict]) -> None:
    for u in units:
        assert u.get("market_rent_low")  # gold: real anchor + rent
    by = {u["unit_number"]: u for u in units}
    assert by["L16-2"]["market_rent_low"] == 1740
    assert str(by["L16-2"]["sqft"]) == "700"
    assert str(by["L16-2"]["beds"]) == "2"
    assert by["W116-2"]["market_rent_low"] == 1500
