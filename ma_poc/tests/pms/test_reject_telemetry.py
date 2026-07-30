"""The emit chokepoint must say out loud what it refused.

`post_process` populates `PostProcessResult.rejected` explicitly "for
observability", and two docstrings promised an
`EventKind.UNIT_VALIDITY_REJECTED`. The enum member never existed and nothing
read the field, so every row this gate deleted vanished with no count, no log
and no event.

That matters because adapters which never called post_process themselves
(knock, essex, plan_text, realpage_oll, touchtour, ...) meet this gate for the
first time at the FINAL emit — the worst place to drop data quietly. Measured on
run-2026-07-27 it was 74 rows across 25 properties, 65 of them carrying a real
unit number AND a real published rent. Small today; the point of this telemetry
is that it cannot silently become large.

`n_with_unit_number` is the load-bearing field: it separates "thin junk
correctly dropped" from "unit-level gold deleted". It reads `unit_number` and
NOT `unit_id`, because identity has not run at this point in the pipeline and
`unit_id` is still None on every row here — a distinction that has produced
three wrong measurements in this codebase already.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from ma_poc.observability.events import EventKind
from ma_poc.pms.adapters.base import AdapterResult
from ma_poc.pms.scraper import promote_verified_unit_rows


def _row(**over: Any) -> dict[str, Any]:
    """A knock-shaped row: real unit number and rent, no dimensions."""
    row: dict[str, Any] = {
        "unit_number": "06210",
        "market_rent_low": 1415,
        "market_rent_high": 1415,
        "rent_range": "$1,415",
        "bedrooms": None,
        "bathrooms": None,
        "sqft": None,
        "floor_plan_name": None,
        "source_ids": {},
    }
    row.update(over)
    return row


def _run(rows: list[dict[str, Any]], tier: str = "TIER_1_KNOCK_API") -> AdapterResult:
    result = AdapterResult(units=list(rows))
    result.tier_used = tier
    promote_verified_unit_rows(result, property_id="222578")
    return result


def test_the_event_kind_exists() -> None:
    """It did not, for as long as two docstrings claimed it did."""
    assert EventKind.UNIT_VALIDITY_REJECTED.value == "validate.unit_validity_rejected"


def test_rejection_is_logged_with_reasons(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A deleted row must leave a trace naming the count and the reasons."""
    with caplog.at_level(logging.INFO, logger="ma_poc.pms.scraper"):
        _run([_row()])
    msgs = [r.getMessage() for r in caplog.records]
    assert any("post_process rejected 1/1 rows" in m for m in msgs), msgs
    assert any("NO_BEDS" in m for m in msgs), msgs


def test_the_event_reports_whether_a_unit_number_was_present(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The signal that separates deleted gold from dropped junk.

    Read via the emitted event rather than a mock, so a refactor that stops
    calling emit fails this test.
    """
    with caplog.at_level(logging.INFO, logger="ma_poc.observability.events"):
        _run([_row()])
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "validate.unit_validity_rejected" in joined, joined
    assert "'n_with_unit_number': 1" in joined, joined


def test_silent_when_nothing_is_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No noise on the happy path — a complete row must not log anything."""
    complete = _row(
        unit_number="11102", bedrooms=1, bathrooms=1, sqft=841, floor_plan_name="A3R"
    )
    with caplog.at_level(logging.INFO):
        result = _run([complete])
    assert result.units, "a complete row should have been admitted as a unit"
    assert not any(
        "post_process rejected" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_telemetry_failure_never_breaks_the_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observability is not allowed to cost us the extraction.

    The block is wrapped defensively; this proves the wrapper works rather than
    trusting it. An earlier revision of this fix DID raise here — `emit` is a
    local import in this module and referencing it at module scope raised
    NameError, which the wrapper turned into a warning. That is exactly the
    fails-open shape worth a permanent test.
    """
    import ma_poc.observability.events as events_mod

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("telemetry backend down")

    monkeypatch.setattr(events_mod, "emit", _boom)
    result = _run([_row(unit_number="11102", bedrooms=1, bathrooms=1, sqft=841)])
    assert result.units, "extraction must survive a telemetry failure"
