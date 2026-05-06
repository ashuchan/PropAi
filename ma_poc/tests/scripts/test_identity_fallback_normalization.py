"""Tests for the hardened identity_fallback normalizer + helpers.

Pins the four behaviour changes the merge-analysis fix relies on:

  * Plural-s normalization on phenotype words ("Bedrooms" ≡ "Bedroom").
  * Numeric-string canonicalization ("1.0" ≡ "1", "02" ≡ "2").
  * ``area=-1`` sentinel ≡ missing area.
  * Last-resort floor-plan-only fallback id (single-anchor).
  * ``compute_unit_data_sha256`` determinism.

These are the behaviours that, if regressed, fragment the same physical
unit across multiple inferred ids and reintroduce the silent merge loss
this fix was designed to fix.
"""

from __future__ import annotations

import json

import pytest

from ma_poc.scripts.identity_fallback import (
    _normalize_token,
    assign_fallback_unit_id,
    compute_fallback_unit_id,
    compute_unit_data_sha256,
)


# ── _normalize_token ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Empties / sentinel.
        (None, ""),
        ("", ""),
        ("   ", ""),
        (-1, ""),
        ("-1", ""),
        # Lowercasing + whitespace collapse.
        ("A1", "a1"),
        ("  A1   ", "a1"),
        ("A4 / A4-LL", "a4 / a4-ll"),
        # Numeric canonicalization.
        ("1", "1"),
        (1, "1"),
        ("1.0", "1"),
        (1.0, "1"),
        ("02", "2"),
        ("-04", "-4"),
        ("0", "0"),
        # Plural-s on phenotype words only.
        ("Bedroom", "bedroom"),
        ("Bedrooms", "bedroom"),
        ("2 Bedroom", "2 bedroom"),
        ("2 Bedrooms", "2 bedroom"),
        ("Beds", "bed"),
        ("3 Baths", "3 bath"),
        # Non-phenotype words keep their s — must NOT be stripped.
        ("Address", "address"),
        ("Riverview Apartments", "riverview apartments"),
    ],
)
def test_normalize_token_examples(raw: object, expected: str) -> None:
    assert _normalize_token(raw) == expected


# ── compute_fallback_unit_id (collision behaviour) ──────────────────────────


def _u(**kw: object) -> dict[str, object]:
    base = {
        "floor_plan_name": None,
        "beds": None,
        "baths": None,
        "area": None,
    }
    base.update(kw)
    return base


def test_plural_s_collides_for_same_physical_unit() -> None:
    a = compute_fallback_unit_id(
        _u(floor_plan_name="2 Bedroom", beds=2, baths=1.0, area=1100), "P1"
    )
    b = compute_fallback_unit_id(
        _u(floor_plan_name="2 Bedrooms", beds=2, baths=1, area=1100), "P1"
    )
    assert a == b
    assert a is not None and a.startswith("inferred_")


def test_numeric_strings_collide_with_ints_and_floats() -> None:
    a = compute_fallback_unit_id(_u(floor_plan_name="A1", beds=1, baths=1, area=750), "P1")
    b = compute_fallback_unit_id(
        _u(floor_plan_name="A1", beds="1", baths="1.0", area="750"), "P1"
    )
    c = compute_fallback_unit_id(_u(floor_plan_name="A1", beds=1.0, baths=1, area=750), "P1")
    assert a == b == c


def test_area_minus_one_equals_none() -> None:
    a = compute_fallback_unit_id(_u(floor_plan_name="A1", beds=1, baths=1, area=-1), "P1")
    b = compute_fallback_unit_id(_u(floor_plan_name="A1", beds=1, baths=1, area=None), "P1")
    assert a == b
    assert a is not None


def test_property_id_separates_namespaces() -> None:
    u = _u(floor_plan_name="A1", beds=1, baths=1, area=750)
    assert compute_fallback_unit_id(u, "P1") != compute_fallback_unit_id(u, "P2")


def test_returns_none_when_only_floor_plan_present() -> None:
    """Two-anchor minimum — single anchor is the last-resort path's job."""
    assert compute_fallback_unit_id(_u(floor_plan_name="Studio"), "P1") is None


# ── assign_fallback_unit_id ─────────────────────────────────────────────────


def test_assign_keeps_existing_unit_id() -> None:
    u = {"unit_id": "  101 ", "floor_plan_name": "A1"}
    res = assign_fallback_unit_id(u, "P1")
    assert res == "101"
    assert u["unit_id"] == "101"


def test_assign_promotes_unit_number_to_unit_id() -> None:
    u = {"unit_number": "B-203", "floor_plan_name": "B2"}
    res = assign_fallback_unit_id(u, "P1")
    assert res == "B-203"
    assert u["unit_id"] == "B-203"


@pytest.mark.parametrize("sentinel", ["null", "None", "NULL"])
def test_assign_ignores_string_sentinels(sentinel: str) -> None:
    """``"null"``/``"None"`` masquerading as a unit_id should NOT pass through."""
    u = {
        "unit_id": sentinel,
        "floor_plan_name": "A1",
        "beds": 1,
        "baths": 1,
        "area": 750,
    }
    res = assign_fallback_unit_id(u, "P1")
    assert res is not None
    assert res.startswith("inferred_")
    assert u["unit_id"] == res  # written back


def test_assign_falls_back_to_sha256_fingerprint() -> None:
    u = _u(floor_plan_name="A1", beds=1, baths=1, area=750)
    res = assign_fallback_unit_id(u, "P1")
    assert res is not None and res.startswith("inferred_")
    # Mutates input.
    assert u["unit_id"] == res


def test_assign_falls_back_to_floor_plan_only_when_single_anchor() -> None:
    u = _u(floor_plan_name="Studio Loft")
    res = assign_fallback_unit_id(u, "P1")
    assert res is not None and res.startswith("inferred_")
    # And again should produce the same id (deterministic).
    u2 = _u(floor_plan_name="Studio Loft")
    assert assign_fallback_unit_id(u2, "P1") == res


def test_assign_returns_none_when_truly_anchorless() -> None:
    u: dict[str, object] = {}
    assert assign_fallback_unit_id(u, "P1") is None
    assert "unit_id" not in u  # nothing written


# ── compute_unit_data_sha256 ────────────────────────────────────────────────


def test_unit_data_sha256_is_deterministic() -> None:
    a = compute_unit_data_sha256({"unit_id": "1", "rent_low": 1500, "beds": 1})
    b = compute_unit_data_sha256({"unit_id": "1", "rent_low": 1500, "beds": 1})
    assert a == b
    assert len(a) == 64  # full SHA256 hex


def test_unit_data_sha256_is_key_order_independent() -> None:
    a = compute_unit_data_sha256({"unit_id": "1", "rent_low": 1500})
    b = compute_unit_data_sha256({"rent_low": 1500, "unit_id": "1"})
    assert a == b


def test_unit_data_sha256_changes_when_any_field_changes() -> None:
    base = {"unit_id": "1", "rent_low": 1500}
    h1 = compute_unit_data_sha256(base)
    h2 = compute_unit_data_sha256({**base, "rent_low": 1501})
    assert h1 != h2


def test_unit_data_sha256_handles_non_json_native_values() -> None:
    """``default=str`` so datetimes / UUIDs / Decimals don't blow up."""
    from datetime import date

    u = {"unit_id": "1", "available": date(2026, 5, 6)}
    h = compute_unit_data_sha256(u)
    assert isinstance(h, str) and len(h) == 64


def test_unit_data_sha256_is_not_used_for_identity() -> None:
    """Sanity: the hash is strictly informational, not derived from
    the same inputs as :func:`compute_fallback_unit_id`."""
    u = {"floor_plan_name": "A1", "beds": 1, "baths": 1, "area": 750}
    fp_id = compute_fallback_unit_id(u, "P1")
    full_hash = compute_unit_data_sha256(u)
    assert fp_id != full_hash  # different lengths, different contents
    # Adding rent flips the data hash but NOT the identity id.
    u2 = {**u, "rent_low": 2000}
    assert compute_fallback_unit_id(u2, "P1") == fp_id  # rent excluded from identity
    assert compute_unit_data_sha256(u2) != full_hash  # rent flipped the data hash
