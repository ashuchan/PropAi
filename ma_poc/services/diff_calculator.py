"""Daily-diff + per-run summary calculator (Python port).

Mirrors the logic in:
  - ``ma_poc/services/src/services/DiffService.ts`` (computeDiff)
  - ``ma_poc/services/src/services/RunService.ts`` (per-run unit / property
    aggregation)

so the email-report script can produce the same numbers as the dashboard
without spinning up the Node/Express API. The shape returned here is a
1:1 mirror of ``services/src/types/diff.ts`` and the relevant fields of
``services/src/types/run.ts`` — converted to Python snake_case dataclasses.

Reads run data from ``property_snapshots.payload`` via the SqlDataProvider
engine; that is the exact same source the TS DiffService consumes when
``DATA_PROVIDER=postgres`` (it's the JSON the runner emits to
``data/runs/{date}/properties.json``, persisted to Postgres by the
data_provider Postgres adapter).

Defensively dedups duplicate ``unit_id`` entries inside each property's
``units`` list — last-write-wins, matching the JS ``new Map()`` behaviour
the TS code relies on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from data_provider.sql.models import PropertySnapshotRow


# ── DTOs (mirror of services/src/types/diff.ts + run.ts) ─────────────────────


@dataclass
class DiffSummary:
    rents_increased: int = 0
    rents_decreased: int = 0
    new_available: int = 0
    became_leased: int = 0
    new_concessions: int = 0
    disappeared_units: int = 0


@dataclass
class RentChange:
    property_id: str
    property_name: str
    unit_id: str
    previous_rent: int
    current_rent: int
    change: int
    change_percent: int
    direction: str  # 'up' | 'down'


@dataclass
class PropertyChange:
    property_id: str
    property_name: str
    change_type: str  # 'NEW' | 'DISAPPEARED'
    details: str
    timestamp: str


@dataclass
class ConcessionChange:
    property_id: str
    property_name: str
    previous_concession: str | None
    current_concession: str | None
    change_type: str  # 'NEW' | 'REMOVED' | 'MODIFIED'


@dataclass
class DailyDiff:
    date: str
    previous_date: str
    summary: DiffSummary
    rent_changes: list[RentChange] = field(default_factory=list)
    property_changes: list[PropertyChange] = field(default_factory=list)
    concession_changes: list[ConcessionChange] = field(default_factory=list)


@dataclass
class RunSummary:
    date: str
    total_properties: int
    succeeded: int
    failed: int
    units_extracted: int
    units_with_rent: int
    units_with_availability: int
    success_rate: float


# ── Internal helpers — mirror of DiffService.ts top-level functions ──────────


def _prop_id(p: dict[str, Any]) -> str:
    """Mirror of getPropId() in DiffService.ts. Tries the human-readable
    keys the runner emits to properties.json first, then falls back to
    apartment_id (which is what the v2 schema/SQL provider uses)."""
    return (
        p.get("Unique ID")
        or p.get("Property ID")
        or (str(p["apartment_id"]) if p.get("apartment_id") is not None else "")
        or ""
    )


def _prop_name(p: dict[str, Any]) -> str:
    """Mirror of getPropName(). Falls back to the property id when no
    human name is present."""
    return p.get("Property Name") or p.get("proj_name") or _prop_id(p)


def _prop_website(p: dict[str, Any]) -> str:
    """Surface the property's website URL — used by the email renderer to
    turn property names into hyperlinks. Snapshots store the raw key
    'website' (lowercase, v2 column name) and/or 'Website' (46-key human
    label). Either is fine."""
    return p.get("Website") or p.get("website") or ""


def _rent(u: dict[str, Any]) -> tuple[float, float]:
    """Mirror of getRent(). v2 emits rent_low/rent_high; legacy emits
    market_rent_low/market_rent_high. Take whichever is present."""
    lo = u.get("rent_low") or u.get("market_rent_low") or 0
    hi = u.get("rent_high") or u.get("market_rent_high") or 0
    return float(lo or 0), float(hi or 0)


def _dedup_units(units: Any) -> dict[str, dict[str, Any]]:
    """Build a unit_id → unit dict, last-write-wins on duplicate ids.

    The runner currently emits duplicate unit_id rows inside a single
    property's units list (observed: 2,075 properties on run 2026-04-26).
    The TS service dedups implicitly by building a ``new Map()`` and
    overwriting on collision; we replicate that here so the diff numbers
    match the dashboard.
    """
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(units, list):
        return out
    for u in units:
        if not isinstance(u, dict):
            continue
        uid = u.get("unit_id")
        if uid is None or uid == "":
            continue
        out[str(uid)] = u
    return out


def _load_run_payloads(session: Session, run_date: str) -> list[dict[str, Any]]:
    """Return all property payloads for the given run.

    Matches what ``IPropertyService.listForRun(date)`` returns on the TS
    side — the per-property serialized record that the runner wrote to
    properties.json (and that the postgres provider persisted to
    ``property_snapshots.payload``).
    """
    rows = (
        session.execute(
            select(PropertySnapshotRow.payload).where(
                PropertySnapshotRow.run_date == run_date
            )
        )
        .scalars()
        .all()
    )
    return [p for p in rows if isinstance(p, dict)]


# ── Public API ──────────────────────────────────────────────────────────────


def compute_run_summary(session: Session, run_date: str) -> RunSummary:
    """Aggregate properties + units for one run.

    Property counts (total / succeeded / failed) come from
    ``property_snapshots`` — one row per property per run, with success
    judged by ``_meta.verdict == 'SUCCESS'`` or "has at least one unit".
    Same heuristic the TS RunService uses after the 2026-04-19 fix.

    Unit counts come from the master ``units`` table filtered by
    ``last_seen_at LIKE '{run_date}%'`` — the canonical post-run
    state with (canonical_id, unit_id) primary-key deduplication
    already enforced. This is the count the UI's run-history table
    shows, and it includes units that the runner persisted via the
    identity_fallback path (units without an explicit unit_id in the
    snapshot payload still land here under a sha256-derived id).
    """
    from sqlalchemy import func, or_, select

    from data_provider.sql.models import UnitRow

    payloads = _load_run_payloads(session, run_date)
    total_properties = len(payloads)
    succeeded = 0
    for p in payloads:
        deduped = _dedup_units(p.get("units") or [])
        meta = p.get("_meta") if isinstance(p.get("_meta"), dict) else {}
        verdict = (meta or {}).get("verdict") or ""
        if deduped or verdict == "SUCCESS":
            succeeded += 1
    failed = total_properties - succeeded
    rate = (succeeded / total_properties) if total_properties else 0.0

    like = f"{run_date}%"
    units_extracted = (
        session.execute(
            select(func.count())
            .select_from(UnitRow)
            .where(UnitRow.last_seen_at.like(like))
        ).scalar()
        or 0
    )
    units_with_rent = (
        session.execute(
            select(func.count())
            .select_from(UnitRow)
            .where(UnitRow.last_seen_at.like(like))
            .where(or_(UnitRow.rent_low.is_not(None), UnitRow.rent_high.is_not(None)))
        ).scalar()
        or 0
    )
    units_with_availability = (
        session.execute(
            select(func.count())
            .select_from(UnitRow)
            .where(UnitRow.last_seen_at.like(like))
            .where(UnitRow.available_date.is_not(None))
        ).scalar()
        or 0
    )

    return RunSummary(
        date=run_date,
        total_properties=total_properties,
        succeeded=succeeded,
        failed=failed,
        units_extracted=int(units_extracted),
        units_with_rent=int(units_with_rent),
        units_with_availability=int(units_with_availability),
        success_rate=rate,
    )


def compute_run_history(session: Session, run_dates: list[str]) -> list[RunSummary]:
    """Convenience: compute_run_summary for each date. Caller decides
    which dates to ask about (e.g. last N descending)."""
    return [compute_run_summary(session, d) for d in run_dates]


def _floorplan_key(
    canonical_id: str, unit: dict[str, Any]
) -> tuple[str, int | None, float | None, int | None] | None:
    """Identity key for "is this the same unit across runs?".

    The runner reassigns ``unit_id`` opportunistically (sometimes the
    portal exposes new ones, sometimes identity_fallback regenerates a
    sha256), so per-unit ids are unstable across runs. The
    ``(canonical_id, beds, baths, sqft)`` floorplan signature is stable —
    a 1-bed/1-bath/750-sqft unit at the same property is the same
    floorplan whether the portal calls it `A101` today and `unit-101`
    tomorrow. Two units in one property that share this signature
    collapse to one floorplan-level entry, which is what the user wants
    to count as "seen across runs".

    Returns None when any of the four fields is missing — those entries
    are excluded from the intersection (we can't decide identity without
    them). Tolerates v2 (`beds`, `baths`, `area`) and legacy
    (`bedrooms`, `bathrooms`, `sqft`) field names; falls through to the
    underscore-prefixed scratch keys some adapters write
    (`_sqft`, `_floor_plan`).
    """
    beds = unit.get("beds")
    if beds is None:
        beds = unit.get("bedrooms")
    baths = unit.get("baths")
    if baths is None:
        baths = unit.get("bathrooms")
    sqft = unit.get("area")
    if sqft is None:
        sqft = unit.get("sqft")
    if sqft is None:
        sqft = unit.get("_sqft")

    if not canonical_id:
        return None
    if beds is None or baths is None or sqft is None:
        return None
    try:
        return (str(canonical_id), int(beds), float(baths), int(sqft))
    except (TypeError, ValueError):
        return None


def _floorplan_keys_for_run(
    session: Session, run_date: str
) -> set[tuple[str, int | None, float | None, int | None]]:
    """Collect the floorplan-signature set for one run from
    property_snapshots."""
    keys: set[tuple[str, int | None, float | None, int | None]] = set()
    rows = (
        session.execute(
            select(
                PropertySnapshotRow.canonical_id,
                PropertySnapshotRow.payload,
            ).where(PropertySnapshotRow.run_date == run_date)
        ).all()
    )
    for cid, payload in rows:
        if not isinstance(payload, dict):
            continue
        units = payload.get("units") or []
        if not isinstance(units, list):
            continue
        for unit in units:
            if not isinstance(unit, dict):
                continue
            key = _floorplan_key(cid, unit)
            if key is not None:
                keys.add(key)
    return keys


def compute_units_seen_in_all_runs(
    session: Session, run_dates: list[str]
) -> int:
    """Count units (by ``(canonical_id, beds, baths, sqft)``) that appear
    in every one of the given runs — set intersection across runs.

    Use case: "how many of the same floorplans showed up on day 24, day
    25 AND day 26" → answer is the size of the intersection. A unit
    that appeared on 24 and 26 but not 25 is excluded from the 3-run
    intersection (it's part of the union, not the intersection).

    Empty input or any single run returning zero keys yields 0.
    """
    if not run_dates:
        return 0
    sets = [_floorplan_keys_for_run(session, d) for d in run_dates]
    if not sets or any(len(s) == 0 for s in sets):
        return 0
    common = sets[0]
    for s in sets[1:]:
        common = common & s
    return len(common)


def compute_daily_diff(
    session: Session, current_date: str, previous_date: str | None
) -> DailyDiff:
    """Line-for-line port of DiffService.ts ``computeDiff`` (Apr 2026).

    Returns a DailyDiff with the 6-metric summary, rent-change details,
    and property-level NEW/DISAPPEARED entries — the same payload the
    Daily Diff dashboard renders.
    """
    if not previous_date:
        return DailyDiff(date=current_date, previous_date=current_date, summary=DiffSummary())

    current = _load_run_payloads(session, current_date)
    previous = _load_run_payloads(session, previous_date)

    if not current:
        return DailyDiff(date=current_date, previous_date=previous_date, summary=DiffSummary())

    prev_map: dict[str, dict[str, Any]] = {}
    for p in previous:
        pid = _prop_id(p)
        if pid:
            prev_map[pid] = p
    curr_map: dict[str, dict[str, Any]] = {}
    for p in current:
        pid = _prop_id(p)
        if pid:
            curr_map[pid] = p

    summary = DiffSummary()
    rent_changes: list[RentChange] = []
    property_changes: list[PropertyChange] = []
    concession_changes: list[ConcessionChange] = []

    for pid, curr in curr_map.items():
        prev = prev_map.get(pid)
        curr_units = _dedup_units(curr.get("units") or [])

        if prev is None:
            property_changes.append(
                PropertyChange(
                    property_id=pid,
                    property_name=_prop_name(curr),
                    change_type="NEW",
                    details=f"New property with {len(curr_units)} units",
                    timestamp=current_date,
                )
            )
            continue

        prev_units = _dedup_units(prev.get("units") or [])

        for uid, cu in curr_units.items():
            pu = prev_units.get(uid)
            if pu is None:
                summary.new_available += 1
                continue

            plo, phi = _rent(pu)
            clo, chi = _rent(cu)
            prev_avg = (plo + phi) / 2.0
            curr_avg = (clo + chi) / 2.0

            if abs(curr_avg - prev_avg) > 1:
                change = curr_avg - prev_avg
                direction = "up" if change > 0 else "down"
                if direction == "up":
                    summary.rents_increased += 1
                else:
                    summary.rents_decreased += 1
                rent_changes.append(
                    RentChange(
                        property_id=pid,
                        property_name=_prop_name(curr),
                        unit_id=uid,
                        previous_rent=round(prev_avg),
                        current_rent=round(curr_avg),
                        change=round(change),
                        change_percent=(
                            round(change / prev_avg * 100) if prev_avg > 0 else 0
                        ),
                        direction=direction,
                    )
                )

            prev_conc = pu.get("concessions") if isinstance(pu, dict) else None
            curr_conc = cu.get("concessions") if isinstance(cu, dict) else None
            if prev_conc != curr_conc:
                if curr_conc and not prev_conc:
                    summary.new_concessions += 1
                concession_changes.append(
                    ConcessionChange(
                        property_id=pid,
                        property_name=_prop_name(curr),
                        previous_concession=prev_conc,
                        current_concession=curr_conc,
                        change_type=(
                            "NEW"
                            if (not prev_conc and curr_conc)
                            else "REMOVED"
                            if (prev_conc and not curr_conc)
                            else "MODIFIED"
                        ),
                    )
                )

        for uid in prev_units:
            if uid not in curr_units:
                summary.disappeared_units += 1

    for pid, prev in prev_map.items():
        if pid in curr_map:
            continue
        property_changes.append(
            PropertyChange(
                property_id=pid,
                property_name=_prop_name(prev),
                change_type="DISAPPEARED",
                details="Property no longer in output",
                timestamp=current_date,
            )
        )

    return DailyDiff(
        date=current_date,
        previous_date=previous_date,
        summary=summary,
        rent_changes=rent_changes,
        property_changes=property_changes,
        concession_changes=concession_changes,
    )


def previous_run_date(run_dates: list[str], run_date: str) -> str | None:
    """Given an ascending list of run_dates and one of them, return the
    immediately preceding date, or None if `run_date` is the earliest /
    not present. Mirrors the index lookup the TS getDailyDiff() does."""
    try:
        idx = run_dates.index(run_date)
    except ValueError:
        return None
    return run_dates[idx - 1] if idx > 0 else None


def lookup_property_info(
    session: Session, canonical_ids: list[str]
) -> dict[str, dict[str, str]]:
    """Bulk-fetch ``{canonical_id: {name, url}}`` for issue-example
    enrichment. Reads the master ``properties`` table directly (the same
    table SqlPropertyStateStore reads through PropertyRow), single
    round-trip for the whole batch.
    """
    if not canonical_ids:
        return {}
    from data_provider.sql.models import PropertyRow

    rows = session.execute(
        select(PropertyRow.canonical_id, PropertyRow.proj_name, PropertyRow.website)
        .where(PropertyRow.canonical_id.in_(set(canonical_ids)))
    ).all()
    return {
        cid: {"name": (name or cid), "url": (website or "")}
        for cid, name, website in rows
    }
