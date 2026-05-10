"""Factory for unit record dicts.

Unit records flow through the pipeline as plain dicts (not typed dataclasses)
until they are stored by the DataProvider. The factory here produces valid,
minimal records with only the fields relevant to the test scenario.

Field groups (per source_planner.FIELD_GROUP):
  identity:     unit_id, floor_plan_name
  physical:     beds, baths, sqft
  transactional: rent_low, rent_high, market_rent_low, market_rent_high,
                 asking_rent, availability_status, available_date
"""

from __future__ import annotations

from typing import Any


def build_unit(
    *,
    unit_id: str | None = None,
    floor_plan_name: str | None = None,
    beds: int = 1,
    baths: float = 1.0,
    sqft: int | None = None,
    rent_low: float | None = None,
    rent_high: float | None = None,
    market_rent_low: float | None = None,
    market_rent_high: float | None = None,
    asking_rent: float | None = None,
    availability_status: str = "AVAILABLE",
    available_date: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a minimal unit record dict.

    Only include keys whose values are explicitly provided (not None), so
    the produced record does not mask missing-field detection in tests that
    verify field completeness.

    Examples::

        # Full transactional record
        u = build_unit(unit_id="101", beds=2, rent_low=2000, rent_high=2200)

        # Physical-only record (for merge testing)
        u = build_unit(unit_id="101", beds=2, baths=2.0, sqft=950)

        # Batch of 3 studio units
        units = [build_unit(unit_id=str(i), beds=0, rent_low=1200) for i in range(3)]
    """
    record: dict[str, Any] = {
        "beds": beds,
        "baths": baths,
        "availability_status": availability_status,
    }
    if unit_id is not None:
        record["unit_id"] = unit_id
    if floor_plan_name is not None:
        record["floor_plan_name"] = floor_plan_name
    if sqft is not None:
        record["sqft"] = sqft
    if rent_low is not None:
        record["rent_low"] = rent_low
    if rent_high is not None:
        record["rent_high"] = rent_high
    if market_rent_low is not None:
        record["market_rent_low"] = market_rent_low
    if market_rent_high is not None:
        record["market_rent_high"] = market_rent_high
    if asking_rent is not None:
        record["asking_rent"] = asking_rent
    if available_date is not None:
        record["available_date"] = available_date
    record.update(extra)
    return record
