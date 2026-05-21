"""AMLI Residential tRPC blob extractor.

Validated 2026-05-21 against the captured AMLI South Shore HAR
(Austin, TX). The HTML page embeds a Next.js __NEXT_DATA__ blob with
the entire tRPC state, which includes 36 unit-level records nested
under ``props.pageProps.trpcState.json.queries[].state.data[].units``.

The generic ``parse_api_responses`` only finds 5 of those 36 units
(13% recovery) and drops bedrooms / bathrooms / floor_plan_name —
that's the root cause of the T4_code_merge_cross_page failure label
for AMLI in production.

Fixture: ``ma_poc/tests/fixtures/amli/south_shore_property.html`` (527 KB).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ma_poc.pms.adapters._amli import (
    detect_amli_trpc_blob,
    parse_amli_trpc_blob,
)
from ma_poc.pms.adapters._html_extract import extract_embedded_blobs_from_html

_FIXTURE = Path("ma_poc/tests/fixtures/amli/south_shore_property.html")


def _extract_next_data_blob() -> dict:
    """Helper: pull the __NEXT_DATA__ JSON out of the fixture HTML."""
    html = _FIXTURE.read_text(encoding="utf-8")
    m = re.search(
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html, re.S,
    )
    assert m is not None
    return json.loads(m.group(1))


# ─────────────────────────────────────────────────────────────────────
# Detector
# ─────────────────────────────────────────────────────────────────────


def test_detect_matches_real_amli_blob() -> None:
    """The real AMLI __NEXT_DATA__ blob must trip the detector."""
    blob = _extract_next_data_blob()
    assert detect_amli_trpc_blob(blob) is True


def test_detect_rejects_non_next_data_object() -> None:
    assert detect_amli_trpc_blob({}) is False
    assert detect_amli_trpc_blob({"foo": "bar"}) is False


def test_detect_rejects_next_data_without_trpc() -> None:
    """A Next.js page without tRPC state must not be matched —
    protects against false-positive routing on other Next.js sites."""
    blob = {
        "buildId": "x",
        "props": {"pageProps": {"someOtherShape": []}},
    }
    assert detect_amli_trpc_blob(blob) is False


def test_detect_rejects_trpc_without_units() -> None:
    """tRPC envelope present but no query carries a units array →
    not AMLI-shaped, must be rejected."""
    blob = {
        "buildId": "x",
        "props": {
            "pageProps": {
                "trpcState": {
                    "json": {
                        "queries": [
                            {"state": {"data": [{"foo": "bar"}]}},
                        ]
                    }
                }
            }
        },
    }
    assert detect_amli_trpc_blob(blob) is False


def test_detect_rejects_non_dict() -> None:
    assert detect_amli_trpc_blob(None) is False
    assert detect_amli_trpc_blob([]) is False
    assert detect_amli_trpc_blob("string") is False


# ─────────────────────────────────────────────────────────────────────
# Parser — end-to-end against real fixture
# ─────────────────────────────────────────────────────────────────────


def test_parser_finds_all_distinct_units() -> None:
    """Real fixture has 36 unit entries across multiple tRPC queries
    but only 29 DISTINCT unitIds (units appear in multiple queries
    — homepage + floorplans + property pages all materialize them).
    Parser must dedupe to 29.

    Compare: the generic parser only emits 5 of these 29 (17% recovery)
    AND drops bedrooms/bathrooms/floor_plan_name — that's the root
    cause of the T4_code_merge_cross_page failure label for AMLI."""
    blob = _extract_next_data_blob()
    units = parse_amli_trpc_blob(blob, source_url="https://www.amli.com/test/")
    assert len(units) == 29, (
        f"expected 29 distinct units after dedup; got {len(units)}."
    )


def test_parser_emits_floor_plan_metadata_per_unit() -> None:
    """Every unit must carry the floor-plan-level metadata —
    floor_plan_name, bedrooms, bathrooms — inherited from its
    containing tRPC query.state.data[] entry."""
    blob = _extract_next_data_blob()
    units = parse_amli_trpc_blob(blob)
    # Every unit must have a non-empty floor_plan_name
    no_plan = [u for u in units if not u["floor_plan_name"]]
    assert no_plan == [], f"{len(no_plan)} units missing floor_plan_name"
    # bedrooms / bathrooms come from bedroomMax / bathroomMax — must be
    # populated (AMLI doesn't ship Studio-only buildings; South Shore has
    # mixed bedrooms).
    bedrooms_set = {u["bedrooms"] for u in units if u["bedrooms"]}
    assert bedrooms_set, "no unit got a bedrooms value"
    bathrooms_set = {u["bathrooms"] for u in units if u["bathrooms"]}
    assert bathrooms_set, "no unit got a bathrooms value"


def test_parser_emits_unit_level_rent() -> None:
    """Each unit must have its own rent value (not the floor plan's
    minimum). The fixture has unit-level rent in $1,710-$2,036 range."""
    blob = _extract_next_data_blob()
    units = parse_amli_trpc_blob(blob)
    with_rent = [u for u in units if u["market_rent_low"]]
    assert len(with_rent) >= 25, (
        f"expected at least 25 of 29 units to have rent; got {len(with_rent)}"
    )
    for u in with_rent:
        assert 500 < u["market_rent_low"] < 50_000, (
            f"unit rent outside band: {u['market_rent_low']}"
        )


def test_parser_emits_unit_number_per_record() -> None:
    """The whole reason this adapter exists: production needs per-unit
    granularity, not floor-plan-aggregate."""
    blob = _extract_next_data_blob()
    units = parse_amli_trpc_blob(blob)
    with_unum = [u for u in units if u["unit_number"]]
    assert len(with_unum) >= 25, (
        f"expected ≥25 units with unit_number; got {len(with_unum)}"
    )
    # All unit numbers should be distinct after dedup
    nums = [u["unit_number"] for u in with_unum]
    assert len(nums) == len(set(nums)), (
        "duplicate unit_numbers leaked through dedup"
    )


def test_parser_emits_availability_date_iso_format() -> None:
    """``rpAvailableDate`` is ISO YYYY-MM-DD. Verify the parser
    preserves it (strips any ``T...`` suffix from realPageAvailabilityDate
    fallback)."""
    blob = _extract_next_data_blob()
    units = parse_amli_trpc_blob(blob)
    dates = {u["availability_date"] for u in units if u["availability_date"]}
    assert dates, "no unit got an availability_date"
    for d in dates:
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", d), (
            f"availability_date not ISO YYYY-MM-DD: {d!r}"
        )


def test_parser_attaches_backing_pms_ids() -> None:
    """AMLI is dual-backed by RealPage + Entrata. Per-unit IDs from both
    PMSes are present in the tRPC payload — the parser surfaces them so
    downstream entity resolution can de-dup against canonical PMS IDs."""
    blob = _extract_next_data_blob()
    units = parse_amli_trpc_blob(blob)
    realpage = sum(1 for u in units if u.get("realpage_unit_id"))
    entrata = sum(1 for u in units if u.get("entrata_unit_id"))
    # Most units carry a PMS-backing ID from at least one of the two.
    assert realpage + entrata >= len(units), (
        f"expected most units to carry a PMS-backing ID; "
        f"realpage={realpage} entrata={entrata} units={len(units)}"
    )


def test_parser_returns_empty_on_non_amli_blob() -> None:
    """Defensive contract: a non-AMLI blob → empty list, no exception."""
    assert parse_amli_trpc_blob({}) == []
    assert parse_amli_trpc_blob({"buildId": "x", "props": {}}) == []
    assert parse_amli_trpc_blob(None) == []


# ─────────────────────────────────────────────────────────────────────
# Integration with the existing embedded-JSON extractor
# ─────────────────────────────────────────────────────────────────────


def test_integration_extract_blobs_then_parse_amli() -> None:
    """End-to-end: the generic embedded-JSON extractor finds the
    __NEXT_DATA__ blob; the AMLI parser walks it into unit records.

    This is the path the adapter dispatch should use:
      1. ``extract_embedded_blobs_from_html(html)`` → list of blobs
      2. For each blob, ``detect_amli_trpc_blob(blob.body)`` then
         ``parse_amli_trpc_blob(blob.body)``.
    """
    html = _FIXTURE.read_text(encoding="utf-8")
    blobs = extract_embedded_blobs_from_html(html)
    next_data = next(
        (b for b in blobs if b["url"] == "embedded:json-block:__NEXT_DATA__"),
        None,
    )
    assert next_data is not None, (
        f"__NEXT_DATA__ blob missing; got URLs: {[b['url'] for b in blobs]}"
    )
    assert detect_amli_trpc_blob(next_data["body"])
    units = parse_amli_trpc_blob(
        next_data["body"], source_url="https://www.amli.com/x/"
    )
    assert len(units) == 29
