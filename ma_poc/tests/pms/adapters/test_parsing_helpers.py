"""Tests for ``ma_poc.pms.adapters._parsing`` shared helpers.

Focus: the defensive ``_unwrap_name_blob`` normaliser added 2026-05-13 in
response to the validation finding that 2,534 rows of the daily xlsx output
carried JSON-blob floor-plan names like ``{"name":"B06","provider_id":"..."}``
instead of the unwrapped string ``B06``.
"""
from __future__ import annotations

import json

import pytest

from ma_poc.pms.adapters._parsing import (
    _unwrap_name_blob,
    make_unit_dict,
    money_to_int,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # single values — unchanged behavior
        ("$1,450", 1450),
        ("1450.00", 1450),
        ("1,450 USD", 1450),
        ("$1450/mo", 1450),
        ("$1,450+", 1450),
        ("From $1,450", 1450),
        # ranges — MUST resolve to the low bound, never the old
        # digit-concatenation poison (12001400 / 20002500)
        ("$1,200 - $1,400", 1200),
        ("1200-1400", 1200),
        ("$2,000 to $2,500", 2000),
        # non-rent / empty → None
        ("Call for pricing", None),
        ("", None),
        (".", None),
        ("garbage", None),
    ],
)
def test_money_to_int_no_range_concatenation_poison(
    raw: str, expected: int | None
) -> None:
    assert money_to_int(raw) == expected


def test_unwrap_passes_clean_string_through() -> None:
    assert _unwrap_name_blob("B06") == "B06"
    assert _unwrap_name_blob("  Two Bed 2 Bath  ") == "Two Bed 2 Bath"


def test_unwrap_empty_inputs_return_empty_string() -> None:
    assert _unwrap_name_blob(None) == ""
    assert _unwrap_name_blob("") == ""
    assert _unwrap_name_blob({}) == ""


def test_unwrap_extracts_name_from_dict() -> None:
    """The original bug shape — adapter passed the raw API floor-plan dict."""
    blob = {"name": "B06", "provider_id": "4875687"}
    assert _unwrap_name_blob(blob) == "B06"


def test_unwrap_dict_prefers_name_then_floor_plan_name() -> None:
    """When ``name`` is missing, fall back through common alternates."""
    assert _unwrap_name_blob({"floor_plan_name": "A1"}) == "A1"
    assert _unwrap_name_blob({"label": "Studio Deluxe"}) == "Studio Deluxe"
    assert _unwrap_name_blob({"displayName": "Plan 12"}) == "Plan 12"


def test_unwrap_dict_with_no_string_name_drops_blob() -> None:
    """Better than serialising the dict into the output."""
    assert _unwrap_name_blob({"provider_id": "x", "internal_id": 42}) == ""


def test_unwrap_handles_json_dumps_string_form() -> None:
    """Some pipelines pre-emit ``json.dumps(dict)`` into the name slot."""
    serialised = json.dumps({"name": "C2", "provider_id": "abc"})
    assert _unwrap_name_blob(serialised) == "C2"


def test_unwrap_handles_malformed_json_string_falls_back_to_str() -> None:
    """If the blob is JSON-ish but unparseable, keep the raw string."""
    malformed = '{"name":"B06","provider_id":}'  # trailing comma
    # Should NOT crash; should fall back to the string itself.
    out = _unwrap_name_blob(malformed)
    assert out == malformed


def test_make_unit_dict_unwraps_floor_plan_blob_end_to_end() -> None:
    """Adapter call site simulation: ensure the dict shape gets cleaned
    before landing in the output unit dict."""
    out = make_unit_dict(
        floor_plan_name={"name": "B06", "provider_id": "4875687"},
        unit_number="301",
        rent_low=1850,
        rent_high=1850,
    )
    assert out["floor_plan_name"] == "B06"


def test_make_unit_dict_preserves_clean_floor_plan_name() -> None:
    """Regression guard: clean strings must pass through unchanged."""
    out = make_unit_dict(
        floor_plan_name="A1",
        unit_number="100",
        rent_low=1200,
        rent_high=1200,
    )
    assert out["floor_plan_name"] == "A1"
