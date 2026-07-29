"""The zero-inventory contract must hold where the row is WRITTEN.

``core.schema_v2.resolve_plan_row_availability`` runs inside the two v2 unit
formatters. Formatting is not writing. In production
(``scripts/runners/jugnu.py``) the sequence is::

    :1786  formatted = _format_output(result, csv_row, "v2")   # contract runs
    :1787  await _run_null_field_recovery(result, formatted, …) # MUTATES rows
    :3525  unit["rent_low"] = _format_rent(rf.recovered_value)  # ← the bypass
    :1796  result["_v2_formatted"] = formatted                  # stashed
    : 655  formatted = result.get("_v2_formatted")              # read back
           → properties.json

so a plan row with no rent at format time is stamped UNAVAILABLE, then gains a
published price from null-field recovery, and ships UNAVAILABLE anyway — the
status/reality disagreement the contract exists to remove, re-introduced one
layer downstream.

The fix re-asserts the contract in two places: the tail of
``_run_null_field_recovery`` (the mutator repairs after itself, so the dict the
caller stashes is contract-clean, not merely format-clean) and
``_write_properties_incremental`` (THE write boundary for properties.json, so
the guarantee does not depend on a future mutator remembering).

Reproduced on run-2026-07-27-full-0d54ca7 (offline replay over the shipped
artifacts; PROXY, not a measurement — the run predates the contract, so its
``availability_status`` is the SOURCE status and format-time behaviour is
replayed):

  * 404 rows across 117 properties gain a rent AFTER formatting — identified
    by ``rent_low`` set with its ``rent_low_raw`` companion absent, which only
    a post-format mutation can produce (the ``*_raw`` snapshot is taken from
    the INTERNAL unit dict inside ``_format_v2_unit``).
  * 380 of those already carried the plan flag AND a source status of
    UNAVAILABLE, so the contract was a no-op on them either way.
  * 3 would ship UNAVAILABLE while carrying a rent: property 251908
    "The Post House" (``_meta.verdict = SUCCESS_PLAN_LEVEL``, so its
    anchorless rows are plan-level under the current predicate), plans "Post"
    $1,775, "Downtown Loft - F2" $1,595, "Telegram" $1,549, all with source
    status UNKNOWN. Its ``llm_diagnostics/251908_field_recovery.json`` records
    those exact rents recovered from ``$.units_data.units[0].rent`` at
    confidence 0.95.
  * Replaying the fix over the whole run: 3 rows change, 0 of 3,650
    rent-bearing plan rows are coerced, 0 non-plan rows are touched, 0 rows
    are dropped (104,964 in, 104,964 out).

Every assertion below drives the PRODUCTION path — ``jugnu._format_output``
then ``jugnu._run_null_field_recovery`` — not a hand-built row, because the
bypass lives in the seam between those two calls. The LLM is stubbed at
``ma_poc.services.llm_diagnostics.null_field_recovery``; nothing here touches
the network.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from ma_poc.core.schema_v2 import (
    _format_v2_floor_plan as _core_format_v2_floor_plan,
)
from ma_poc.core.schema_v2 import enforce_zero_inventory_contract
from ma_poc.scripts.runners import jugnu
from ma_poc.services.llm_diagnostics import FieldRecovery, RecoveredField

_CSV = {"apartmentid": "251908"}

# A property whose ROSTER is plan-level (the 2026-07-27 shape: the adapter
# records plan-ness on the property verdict, the rows carry the plain tier).
_PLAN_TIER = "TIER_1_PROFILE_MAPPING"


def _plan_row(name: str, **kw: object) -> dict[str, object]:
    """A plan card: named plan, no unit anchor, no published rent."""
    row: dict[str, object] = {
        "floor_plan_name": name,
        "unit_number": "",
        "bedrooms": "1",
        "bathrooms": "1",
        "availability_status": "UNKNOWN",
    }
    row.update(kw)
    return row


def _result(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "extraction_tier_used": _PLAN_TIER,
        "_verdict_quality": "SUCCESS_PLAN_LEVEL",
        "units": rows,
        "plan_summaries": [],
        "_meta": {"canonical_id": "251908"},
        # Preconditions _run_null_field_recovery checks before spending a call.
        "_raw_api_responses": [
            {
                "url": "https://example.test/units_data",
                "body": {"units": [{"rent": 1775}]},
            }
        ],
    }


class _Extract:
    """Stand-in for the AdapterResult ``_run_null_field_recovery`` reads."""

    tier_used = _PLAN_TIER


def _stub_recovery(monkeypatch: pytest.MonkeyPatch, fields: list[RecoveredField]):
    """Replace the LLM call with a fixed recovery. No network, no prompt I/O."""
    calls: list[str] = []

    async def _fake(*, property_id: str, partial_unit: dict, **_: object):
        calls.append(property_id)
        return FieldRecovery(
            property_id=property_id,
            unit_fragment_hash="0" * 64,
            tier_used=_PLAN_TIER,
            recovered_fields=fields,
            recovery_summary="stub",
        )

    monkeypatch.setattr(
        "ma_poc.services.llm_diagnostics.null_field_recovery", _fake
    )
    return calls


async def _run_production_sequence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rows: list[dict[str, object]],
    fields: list[RecoveredField],
) -> dict[str, object]:
    """format → null-field recovery, exactly as ``_process_property`` does it.

    Nothing here re-asserts the contract by hand: ``_run_null_field_recovery``
    is the mutator and must leave the rows contract-clean itself, because the
    caller stashes the dict straight into ``result["_v2_formatted"]``.
    """
    result = _result(rows)
    result["_extract_result"] = _Extract()
    _stub_recovery(monkeypatch, fields)

    formatted = jugnu._format_output(result, dict(_CSV), "v2")
    await jugnu._run_null_field_recovery(result, formatted, tmp_path, "251908")
    return formatted


# ── the contract at format time is unchanged (the fix must not regress it) ──


@pytest.mark.asyncio
async def test_plan_row_without_a_recovered_rent_still_ships_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other half of the acceptance test: no new rent, no withdrawal.

    Recovery returns only a low-confidence rent (below the 0.85 patch gate),
    so nothing lands on the row and the coercion must stand.
    """
    formatted = await _run_production_sequence(
        monkeypatch,
        tmp_path,
        [_plan_row("Telegram")],
        [
            RecoveredField(
                field_name="rent_low",
                recovered_value=1549,
                confidence=0.40,
                source_path="$.units_data.units[0].rent",
            )
        ],
    )
    (row,) = formatted["units"]
    assert row["is_floor_plan_level"] is True
    assert row["rent_low"] is None
    assert row["availability_status"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_plan_row_that_gains_a_rent_does_not_ship_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """THE regression. Pre-fix this row shipped ``UNAVAILABLE`` at $1,775."""
    formatted = await _run_production_sequence(
        monkeypatch,
        tmp_path,
        [_plan_row("Post")],
        [
            RecoveredField(
                field_name="rent_low",
                recovered_value=1775,
                confidence=0.95,
                source_path="$.units_data.units[0].rent",
            ),
            RecoveredField(
                field_name="rent_high",
                recovered_value=1775,
                confidence=0.95,
                source_path="$.units_data.units[0].rent",
            ),
        ],
    )
    (row,) = formatted["units"]
    assert row["is_floor_plan_level"] is True
    assert row["rent_low"] == 1775.0, "precondition: the patch loop landed"
    assert row["availability_status"] != "UNAVAILABLE"
    # …and it is restored to what the SOURCE said, not invented.
    assert row["availability_status"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_withdrawal_restores_the_source_status_not_a_guess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A source that said AVAILABLE gets AVAILABLE back — not UNKNOWN."""
    formatted = await _run_production_sequence(
        monkeypatch,
        tmp_path,
        [_plan_row("Downtown Loft - F2", availability_status="Available Now")],
        [
            RecoveredField(
                field_name="rent_low",
                recovered_value=1595,
                confidence=0.95,
                source_path="$.units_data.units[0].pricing.rent",
            )
        ],
    )
    (row,) = formatted["units"]
    assert row["rent_low"] == 1595.0
    assert row["availability_status"] == "AVAILABLE"


@pytest.mark.asyncio
async def test_a_source_published_unavailable_survives_the_recovered_rent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The 401-row majority on the 2026-07-27 run.

    The operator itself said UNAVAILABLE. Recovering a price does not make the
    plan available, and the write boundary must not pretend otherwise.
    """
    formatted = await _run_production_sequence(
        monkeypatch,
        tmp_path,
        [_plan_row("B02", availability_status="Not Available")],
        [
            RecoveredField(
                field_name="rent_low",
                recovered_value=2340,
                confidence=0.95,
                source_path="$.units_data.units[0].rent",
            )
        ],
    )
    (row,) = formatted["units"]
    assert row["rent_low"] == 2340.0
    assert row["availability_status"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_recovery_never_drops_or_reorders_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No-drop contract across the whole sequence."""
    names = ["Post", "Downtown Loft - F2", "Downtown Loft - C2", "Telegram"]
    formatted = await _run_production_sequence(
        monkeypatch,
        tmp_path,
        [_plan_row(n) for n in names],
        [
            RecoveredField(
                field_name="rent_low",
                recovered_value=1775,
                confidence=0.95,
                source_path="$.units_data.units[0].rent",
            )
        ],
    )
    assert [r["floor_plan_name"] for r in formatted["units"]] == names


# ── the write boundary itself ───────────────────────────────────────────────


def test_write_boundary_reasserts_even_if_the_caller_forgets(
    tmp_path: Path,
) -> None:
    """``_write_properties_incremental`` is the single gate for
    properties.json, so the guarantee cannot depend on a mutator remembering
    to re-assert. A row mutated after the recovery hook still ships clean —
    and the assertion is made against the bytes on disk, not the in-memory
    dict, because the file is what the sync step reads."""
    prop = {
        "units": [
            {
                "is_floor_plan_level": True,
                "availability_status": "UNAVAILABLE",
                "availability_status_raw": "UNKNOWN",
                "rent_low": 1775.0,
                "rent_high": 1775.0,
            }
        ]
    }
    out = tmp_path / "properties.json"
    jugnu._write_properties_incremental(out, [prop])
    written = json.loads(out.read_text())
    assert written[0]["units"][0]["availability_status"] == "UNKNOWN"
    assert written[0]["units"][0]["rent_low"] == 1775.0


def test_write_boundary_is_idempotent_and_drops_nothing() -> None:
    rows = [
        {
            "is_floor_plan_level": True,
            "availability_status": "UNAVAILABLE",
            "availability_status_raw": "UNKNOWN",
            "rent_low": 1775.0,
        },
        {
            "is_floor_plan_level": True,
            "availability_status": "UNAVAILABLE",
            "availability_status_raw": "UNKNOWN",
            "rent_low": None,
            "rent_high": None,
        },
    ]
    assert enforce_zero_inventory_contract(rows) == 1
    assert enforce_zero_inventory_contract(rows) == 0
    assert len(rows) == 2
    assert rows[1]["availability_status"] == "UNAVAILABLE"


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        # ── withdraw ────────────────────────────────────────────────────────
        (
            {"is_floor_plan_level": True, "availability_status": "UNAVAILABLE",
             "availability_status_raw": "UNKNOWN", "rent_low": 1775.0},
            "UNKNOWN",
        ),
        (
            {"is_floor_plan_level": True, "availability_status": "UNAVAILABLE",
             "availability_status_raw": None, "rent_low": 1595.0},
            "UNKNOWN",
        ),
        (
            {"is_floor_plan_level": True, "availability_status": "UNAVAILABLE",
             "availability_status_raw": "Available Now", "rent_high": 2100.0},
            "AVAILABLE",
        ),
        (
            {"is_floor_plan_level": True, "availability_status": "UNAVAILABLE",
             "availability_status_raw": "Join the wait list", "rent_low": 900.0},
            "WAITLIST",
        ),
        (
            {"is_floor_plan_level": True, "availability_status": "UNAVAILABLE",
             "availability_status_raw": "", "rent_low": None,
             "rent_high": 1200.0},
            "UNKNOWN",
        ),
        # ── must NOT withdraw ───────────────────────────────────────────────
        # no rent — the coercion still stands
        (
            {"is_floor_plan_level": True, "availability_status": "UNAVAILABLE",
             "availability_status_raw": "UNKNOWN", "rent_low": None,
             "rent_high": None},
            "UNAVAILABLE",
        ),
        # the source really said UNAVAILABLE
        (
            {"is_floor_plan_level": True, "availability_status": "UNAVAILABLE",
             "availability_status_raw": "Not Available", "rent_low": 2340.0},
            "UNAVAILABLE",
        ),
        # plan-SHAPED but unflagged: shape is not evidence of plan-ness
        (
            {"is_floor_plan_level": False, "availability_status": "UNAVAILABLE",
             "availability_status_raw": "UNKNOWN", "rent_low": 1775.0},
            "UNAVAILABLE",
        ),
        # flag absent entirely
        (
            {"availability_status": "UNAVAILABLE",
             "availability_status_raw": "UNKNOWN", "rent_low": 1775.0},
            "UNAVAILABLE",
        ),
        # already clean — never re-touched
        (
            {"is_floor_plan_level": True, "availability_status": "AVAILABLE",
             "availability_status_raw": "AVAILABLE", "rent_low": 1775.0},
            "AVAILABLE",
        ),
        (
            {"is_floor_plan_level": True, "availability_status": "UNKNOWN",
             "availability_status_raw": None, "rent_low": 1775.0},
            "UNKNOWN",
        ),
        # a null status is never coerced HERE — only the formatter does that
        (
            {"is_floor_plan_level": True, "availability_status": None,
             "availability_status_raw": None, "rent_low": 1775.0},
            None,
        ),
        # no ``*_raw`` companion (core/schema_v2's own row shape): with no
        # record of the source status, an UNAVAILABLE is indistinguishable
        # from a genuine one and must not be withdrawn
        (
            {"is_floor_plan_level": True, "availability_status": "UNAVAILABLE",
             "rent_low": 1775.0},
            "UNAVAILABLE",
        ),
        # a real apartment row is out of scope entirely
        (
            {"is_floor_plan_level": False, "unit_id": "204",
             "availability_status": "UNAVAILABLE",
             "availability_status_raw": "AVAILABLE", "rent_low": 1500.0},
            "UNAVAILABLE",
        ),
    ],
)
def test_withdraw_only_table(row: dict, expected: str | None) -> None:
    enforce_zero_inventory_contract([row])
    assert row.get("availability_status") == expected


def test_non_dict_rows_are_skipped_not_crashed() -> None:
    assert enforce_zero_inventory_contract(["garbage", None, 7]) == 0
    assert enforce_zero_inventory_contract(None) == 0


# ── the floor-plan wrapper: status and date must agree ──────────────────────


@pytest.mark.parametrize(
    "formatter",
    [_core_format_v2_floor_plan, jugnu._format_v2_floor_plan],
    ids=["core.schema_v2", "runners.jugnu"],
)
def test_floor_plan_wrapper_drops_the_date_it_manufactured(formatter) -> None:
    """Both wrappers FORCE ``is_floor_plan_level=True`` after
    ``_format_v2_unit`` has already run. Pre-fix that left a rentless plan card
    shipping ``availability_status='UNAVAILABLE'`` beside
    ``available_date='2026-07-27'`` with no source date behind it — the
    ``_resolve_available_date`` AVAILABLE branch fired before the status was
    rewritten underneath it."""
    out = formatter(
        {
            "floor_plan_name": "A1",
            "bedrooms": 1,
            "bathrooms": 1,
            "availability_status": "AVAILABLE",
        },
        datetime(2026, 7, 27, 3, 0, 0),
        "P1",
    )
    assert out["is_floor_plan_level"] is True
    assert out["availability_status"] == "UNAVAILABLE"
    assert out["available_date"] is None


@pytest.mark.parametrize(
    "formatter",
    [_core_format_v2_floor_plan, jugnu._format_v2_floor_plan],
    ids=["core.schema_v2", "runners.jugnu"],
)
def test_floor_plan_wrapper_keeps_a_source_published_date(formatter) -> None:
    """The drop is narrow: only a date the formatter invented. A date the
    OPERATOR published survives, even on an UNAVAILABLE plan card."""
    out = formatter(
        {
            "floor_plan_name": "A1",
            "bedrooms": 1,
            "bathrooms": 1,
            "availability_status": "AVAILABLE",
            "available_date": "2026-09-01",
        },
        datetime(2026, 7, 27, 3, 0, 0),
        "P1",
    )
    assert out["availability_status"] == "UNAVAILABLE"
    assert out["available_date"] == "2026-09-01"


@pytest.mark.parametrize(
    "formatter",
    [_core_format_v2_floor_plan, jugnu._format_v2_floor_plan],
    ids=["core.schema_v2", "runners.jugnu"],
)
def test_floor_plan_wrapper_leaves_a_rent_bearing_card_alone(formatter) -> None:
    """A plan card with a published price is not zero-inventory: neither the
    status nor the date may be touched."""
    out = formatter(
        {
            "floor_plan_name": "A1",
            "bedrooms": 1,
            "bathrooms": 1,
            "availability_status": "AVAILABLE",
            "market_rent_low": 1775,
        },
        datetime(2026, 7, 27, 3, 0, 0),
        "P1",
    )
    assert out["rent_low"] == 1775.0
    assert out["availability_status"] == "AVAILABLE"
    assert out["available_date"] == "2026-07-27"
