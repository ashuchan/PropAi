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

from datetime import UTC, datetime

from ma_poc.core.identity import (
    _normalize_token,
    assign_fallback_unit_id,
    compute_fallback_unit_id,
    compute_unit_data_sha256,
    seen_at_iso,
    synthesize_unkeyable_id,
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


# ── synthesize_unkeyable_id (no-drop contract) ──────────────────────────────


def test_synthesize_returns_unkeyable_prefixed_id() -> None:
    """Synthesis result is filterable in SQL via the prefix."""
    res = synthesize_unkeyable_id({}, "P1")
    assert res.startswith("unkeyable_")
    assert len(res) == 26  # "unkeyable_" (10) + 16 hex chars


def test_synthesize_mutates_unit_in_place() -> None:
    u: dict[str, object] = {"rent_low": 1200}
    res = synthesize_unkeyable_id(u, "P1")
    assert u["unit_id"] == res


def test_synthesize_is_deterministic_across_calls() -> None:
    """Same payload + same property → same id, every call."""
    u1 = {"rent_low": 1200, "available_date": "2026-06-01"}
    u2 = {"rent_low": 1200, "available_date": "2026-06-01"}
    assert synthesize_unkeyable_id(u1, "P1") == synthesize_unkeyable_id(u2, "P1")


def test_synthesize_namespaced_by_property_id() -> None:
    """Same payload on different properties → different ids."""
    u = {"rent_low": 1200}
    assert synthesize_unkeyable_id(dict(u), "P1") != synthesize_unkeyable_id(dict(u), "P2")


def test_synthesize_distinguishes_distinct_payloads() -> None:
    """Different anchorless payloads must NOT collide."""
    a = synthesize_unkeyable_id({"rent_low": 1200}, "P1")
    b = synthesize_unkeyable_id({"rent_low": 1500}, "P1")
    assert a != b


def test_synthesize_reuses_precomputed_data_sha256() -> None:
    """Optimization: when ``data_sha256`` is already set on the unit, the
    helper must use it verbatim instead of recomputing — that's how the
    upsert paths chain ``compute_unit_data_sha256`` once and feed both
    the column and the synthesis."""
    u = {"rent_low": 1200, "data_sha256": "fakehash"}
    # Synthesize with the bogus hash baked in.
    a = synthesize_unkeyable_id(u, "P1")
    # Independently compute what we'd get if synthesize had used the real hash.
    real_hash = compute_unit_data_sha256({"rent_low": 1200})
    b_via_real = synthesize_unkeyable_id({"rent_low": 1200, "data_sha256": real_hash}, "P1")
    # They must differ — proving ``a`` did NOT recompute.
    assert a != b_via_real


def test_synthesize_always_returns_string_even_on_empty_dict() -> None:
    """No-drop contract guarantee: helper must return a non-empty string
    for any input dict, including totally empty."""
    res = synthesize_unkeyable_id({}, "P1")
    assert isinstance(res, str) and res.startswith("unkeyable_")


def test_synthesize_handles_empty_property_id() -> None:
    """Empty property_id is allowed (caller's choice). The id is still
    deterministic and stable — identity hashes within an unknown-property
    bucket but doesn't crash."""
    res = synthesize_unkeyable_id({"rent_low": 1200}, "")
    assert res.startswith("unkeyable_")


# ── seen_at_iso (date-anchored last_seen_at helper) ─────────────────────────


def test_seen_at_iso_happy_path() -> None:
    """The output's date portion is the run_date and the time portion is
    a valid ISO timestamp."""
    res = seen_at_iso("2026-05-06")
    assert res.startswith("2026-05-06T")
    # Should round-trip through ``datetime.fromisoformat`` — that's the
    # contract every Postgres / SQLite consumer relies on.
    parsed = datetime.fromisoformat(res)
    assert parsed.tzinfo is not None
    # Date matches the splice (not wall-clock).
    assert parsed.date().isoformat() == "2026-05-06"


def test_seen_at_iso_truncates_extra_chars() -> None:
    """Callers passing a full ISO timestamp by accident get the date
    portion only — the time/offset is replaced with wall-clock."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    res = seen_at_iso(f"{today}T00:00:00+00:00")
    assert res.startswith(f"{today}T")
    parsed = datetime.fromisoformat(res)
    # Time portion came from wall-clock, NOT the input — so it's "now-ish".
    delta = abs((datetime.now(UTC) - parsed).total_seconds())
    assert delta < 5.0


def test_seen_at_iso_falls_back_when_empty() -> None:
    """Empty run_date → wall-clock fallback. Pins the no-corruption
    guarantee against the bug the helper was introduced to fix."""
    res = seen_at_iso("")
    parsed = datetime.fromisoformat(res)
    delta = abs((datetime.now(UTC) - parsed).total_seconds())
    assert delta < 5.0


def test_seen_at_iso_falls_back_when_none() -> None:
    res = seen_at_iso(None)  # type: ignore[arg-type]
    parsed = datetime.fromisoformat(res)
    delta = abs((datetime.now(UTC) - parsed).total_seconds())
    assert delta < 5.0


@pytest.mark.parametrize(
    "garbage",
    [
        "DROPTABLE0",        # 10 chars but not a date
        "2026/05/06",        # slashes
        "2026-5-6",          # missing zero-pads
        "abcd-ef-gh",        # alpha
        "    ",              # whitespace only
        "2026",              # too short
    ],
)
def test_seen_at_iso_rejects_malformed(garbage: str) -> None:
    """Anything that fails the strict ``YYYY-MM-DD`` prefix check falls
    back to wall-clock — never spliced into the column. We don't compare
    the result to ``garbage`` directly (today's date legitimately starts
    with ``"2026"``); instead we check the result is a valid ISO
    timestamp whose date is today and whose time is within seconds of
    wall-clock."""
    res = seen_at_iso(garbage)
    parsed = datetime.fromisoformat(res)
    now = datetime.now(UTC)
    assert parsed.date() == now.date()
    assert abs((now - parsed).total_seconds()) < 5.0


def test_seen_at_iso_strips_surrounding_whitespace() -> None:
    """Common upstream issue — whitespace around the run_date string."""
    res = seen_at_iso("  2026-05-06  ")
    assert res.startswith("2026-05-06T")


# ── #34: prefer a captured per-unit source_id over the phenotype hash ────────
def test_fallback_prefers_per_unit_source_id() -> None:
    """A captured appfolio_listing_id becomes the (real, non-synthetic) id
    instead of an inferred_ phenotype hash."""
    u = {
        "floor_plan_name": "A1", "beds": 1, "baths": 1, "sqft": 700, "rent_low": 1500,
        "source_ids": {"appfolio_listing_id": "12345"},
    }
    res = assign_fallback_unit_id(u, "P1")
    assert res == "appfolio-12345"
    assert not res.startswith(("inferred_", "unkeyable_"))


def test_fallback_ignores_plan_level_source_id() -> None:
    """A PLAN-level source id (sightmap_floor_plan_id) is shared across a plan's
    units and must NOT be used as a per-unit id — falls through to the hash."""
    u = {
        "floor_plan_name": "A1", "beds": 1, "baths": 1, "sqft": 700,
        "source_ids": {"sightmap_floor_plan_id": "999"},
    }
    res = assign_fallback_unit_id(u, "P1")
    assert res.startswith("inferred_")


def test_fallback_source_id_rent_stable() -> None:
    """Source-id anchor is rent-independent (daily-join stability)."""
    a = assign_fallback_unit_id({"source_ids": {"apts247_unit_id": "77"}, "rent_low": 1500}, "P1")
    b = assign_fallback_unit_id({"source_ids": {"apts247_unit_id": "77"}, "rent_low": 1600}, "P1")
    assert a == b == "apts247-77"
