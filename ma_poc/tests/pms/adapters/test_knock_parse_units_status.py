"""Knock ``parse_knock_units`` — availability_status logic.

2026-05-25 canary 1ef1060 post-mortem:
  The prior status rule was ``AVAILABLE iff (u.available AND not
  u.occupied)``. Knock's ``available`` boolean is a separate signal
  from rentability — it was False/null on ~8,580 of 8,597 published
  units in the canary. Result: TIER_1_KNOCK_API full-row collapsed
  from 90.2% (xlsx baseline) to 0.5%.

  Fix: by the time a unit reaches the status assignment it has
  already passed:
    * line 117 — not hidden, not leased, not reserved
    * line 127 — rent is in the $200-$50,000 sanity range

  So it IS a real rent-published listing. Default AVAILABLE; only
  mark UNAVAILABLE when the operator explicitly says occupied.

This file pins the new rule and the gate behaviour around it so a
future refactor can't silently re-introduce the regression.
"""
from __future__ import annotations

from typing import Any

from ma_poc.pms.adapters.knock import parse_knock_units


def _wrap(units: list[dict[str, Any]], layouts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Wrap a units list in the ``{units_data: {units, layouts}}`` envelope
    that ``parse_knock_units`` expects."""
    return {"units_data": {"units": units, "layouts": layouts or []}}


# ─────────────────────────────────────────────────────────────────────
# Core regression: rent-published unit with available=False is the
# Knock canary signature. Pre-fix → UNAVAILABLE → loses Q1 fallback →
# loses full-row. Post-fix → AVAILABLE → Q1 fires → counted complete.
# ─────────────────────────────────────────────────────────────────────


def test_rent_published_no_available_flag_is_AVAILABLE() -> None:
    """The canary signature: rent + name + plan, no ``available``
    flag, no ``occupied`` flag. Must come out AVAILABLE."""
    out = parse_knock_units(_wrap([{
        "name": "203A",
        "price": 1495,
        "bedrooms": 2,
        "bathrooms": 1,
        "layoutName": "B1",
        "area": 1050,
        # no `available`, no `occupied`
    }]))
    assert len(out) == 1
    assert out[0]["availability_status"] == "AVAILABLE"
    assert out[0]["market_rent_low"] == 1495
    assert out[0]["unit_number"] == "203A"


def test_rent_published_available_false_is_AVAILABLE() -> None:
    """Knock's ``available=False`` is a Knock-internal signal (not the
    rentability question) — must NOT downgrade the unit to UNAVAILABLE
    once it has passed the hidden/leased/reserved + rent gates."""
    out = parse_knock_units(_wrap([{
        "name": "508",
        "price": 1295,
        "available": False,
        "occupied": False,
    }]))
    assert len(out) == 1
    assert out[0]["availability_status"] == "AVAILABLE"


def test_rent_published_available_null_is_AVAILABLE() -> None:
    """``available=null`` (missing or explicitly None) → AVAILABLE."""
    out = parse_knock_units(_wrap([{
        "name": "A1",
        "price": 1700,
        "available": None,
    }]))
    assert len(out) == 1
    assert out[0]["availability_status"] == "AVAILABLE"


def test_rent_published_available_true_is_AVAILABLE() -> None:
    """When Knock DOES say ``available=True`` we obviously still
    produce AVAILABLE — the new rule is strictly more lenient, never
    stricter."""
    out = parse_knock_units(_wrap([{
        "name": "ZZ-9",
        "price": 1800,
        "available": True,
    }]))
    assert len(out) == 1
    assert out[0]["availability_status"] == "AVAILABLE"


# ─────────────────────────────────────────────────────────────────────
# The only path to UNAVAILABLE is ``occupied=True``. This pins the
# narrow exit door.
# ─────────────────────────────────────────────────────────────────────


def test_occupied_true_is_UNAVAILABLE() -> None:
    """Explicit ``occupied=True`` is the one signal that flips to
    UNAVAILABLE (downstream consumers may use this to suppress)."""
    out = parse_knock_units(_wrap([{
        "name": "OCC-1",
        "price": 1500,
        "occupied": True,
    }]))
    assert len(out) == 1
    assert out[0]["availability_status"] == "UNAVAILABLE"


def test_occupied_true_with_available_true_still_UNAVAILABLE() -> None:
    """``occupied`` wins over ``available`` — that's the post-fix
    semantics (occupied is the unambiguous 'someone's living there'
    signal; available is the noisy one)."""
    out = parse_knock_units(_wrap([{
        "name": "OCC-2",
        "price": 1500,
        "available": True,
        "occupied": True,
    }]))
    assert out[0]["availability_status"] == "UNAVAILABLE"


# ─────────────────────────────────────────────────────────────────────
# The hidden/leased/reserved + rent gates run BEFORE status — make
# sure the new status logic doesn't accidentally bypass them.
# ─────────────────────────────────────────────────────────────────────


def test_hidden_unit_still_skipped() -> None:
    """``hidden=True`` is dropped before status is even computed (line
    117). Verify nothing in the new code path re-introduces it."""
    out = parse_knock_units(_wrap([{
        "name": "H1",
        "price": 1500,
        "hidden": True,
    }]))
    assert out == []


def test_leased_unit_still_skipped() -> None:
    out = parse_knock_units(_wrap([{
        "name": "L1",
        "price": 1500,
        "leased": True,
    }]))
    assert out == []


def test_reserved_unit_still_skipped() -> None:
    out = parse_knock_units(_wrap([{
        "name": "R1",
        "price": 1500,
        "reserved": True,
    }]))
    assert out == []


def test_unit_with_no_rent_still_skipped() -> None:
    """No rent → dropped before status check (line 127)."""
    out = parse_knock_units(_wrap([{"name": "NR-1"}]))
    assert out == []


def test_unit_with_out_of_range_rent_still_skipped() -> None:
    """Rent < $200 or > $50,000 → dropped (line 127). The new status
    rule does not change this."""
    out = parse_knock_units(_wrap([
        {"name": "LOW", "price": 50},
        {"name": "HIGH", "price": 99_999},
    ]))
    assert out == []


# ─────────────────────────────────────────────────────────────────────
# Canary-scale shape: representative payload with mixed availability
# signals across many units. Confirms the new rule produces a
# realistic distribution.
# ─────────────────────────────────────────────────────────────────────


def test_mixed_payload_realistic_distribution() -> None:
    """Simulate the canary signature: 10 units, 9 with the rent-
    published-but-available-false shape, 1 explicitly occupied.
    Pre-fix → 0 AVAILABLE. Post-fix → 9 AVAILABLE, 1 UNAVAILABLE.
    """
    units = [
        {"name": f"A{i}", "price": 1500 + i * 10, "available": False, "occupied": False}
        for i in range(9)
    ]
    units.append({"name": "OCC", "price": 1800, "occupied": True})

    out = parse_knock_units(_wrap(units))
    assert len(out) == 10
    avail_count = sum(1 for u in out if u["availability_status"] == "AVAILABLE")
    unavail_count = sum(1 for u in out if u["availability_status"] == "UNAVAILABLE")
    assert avail_count == 9, (
        f"Expected 9 AVAILABLE (canary regression fix); got {avail_count}. "
        f"Statuses: {[u['availability_status'] for u in out]}"
    )
    assert unavail_count == 1
