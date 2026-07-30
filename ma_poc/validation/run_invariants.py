"""Run-level invariants — checks that need the WHOLE run, not one record.

`cross_run_sanity.check` compares a single unit against its own history. These
two checks are a different granularity: one compares properties against EACH
OTHER within a run, the other compares a property's published envelope against
the previous run. Neither can be expressed per-record, which is why they live
here rather than there.

Both are deliberately join-free and network-free: they read only the emitted
`properties.json`, so they can be applied retroactively to every archived run.

Each check exists because a real defect reached production and was found by
accident rather than by design:

1. `find_identical_payload_groups` — Redwood Brunswick (278139) and Redwood
   Sugarcreek Township (77994) each shipped 149 rows with a byte-identical area
   distribution ({1620:56, 1709:34, 1381:10, 1294:10, 1327:9}) and identical
   rents (2500, 2181, 2182, 1824, 1759). Two different Ohio communities cannot
   have the same inventory; a company-wide payload was being attributed to each
   individual property. Their `detected_pms` even DIFFERED (funnel vs rentcafe)
   while the output matched, which is the giveaway. Note the existing
   sibling-scope guard does NOT catch this: there the floor_plan_names were
   sibling COMMUNITY names, whereas here they are synthesized bed/bath labels
   ("Studio / 2.00 Bath" on a 1,620 sqft home).

2. `find_envelope_drift` — dropping rows silently distorts the rent/area range
   we publish, with no missing-data signal at all. Measured on the dq29 canary:
   property 222727's rent_high envelope went (1351, 1952) -> (1351, 1472), so a
   client would read a ceiling $480 below what the operator actually advertises,
   and 37979 lost a whole bed class ({4:4,2:3,3:4} -> {2:3,3:4,4:1}). That is
   arguably worse than an outright failure: a FAILED verdict is visible, a
   quietly-narrowed range looks like valid data.

Both read BOTH field vocabularies at every slot. Adapter rows carry
`bedrooms`/`sqft`/`market_rent_low`; v2-formatted rows carry `beds`/`area`/
`rent_low`. Reading one silently degenerates to a constant on the other, which
is the live defect this codebase has hit repeatedly.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: Below this many rows an exact (area, rent) match between two properties is
#: unremarkable — two small properties can legitimately list one 2-bed at the
#: same price. The Redwood case was 149 rows. Kept low enough to catch a real
#: shared payload, high enough not to cry wolf on tiny rosters.
DEFAULT_MIN_ROWS_FOR_COLLISION = 3

#: Fraction of the previous run's envelope width below which a narrowing is
#: reported. 0.7 mirrors the existing drift threshold in `drift_detector`.
DEFAULT_ENVELOPE_RETENTION = 0.7


def _first(row: dict[str, Any], *names: str) -> Any:
    """First present, non-empty value among *names*. Both vocabularies."""
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _num(value: Any) -> float | None:
    """Coerce to float, or None. Sentinels (-1) are absence, not values."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out < 0 else out


def _rows(prop: dict[str, Any]) -> list[dict[str, Any]]:
    """Every emitted row for a property, across BOTH output channels.

    `promote_verified_unit_rows` splits anchored apartments into `units` and
    unanchored plan rows into `floor_plans`. A check that reads one channel
    reports a reclassification as a change.
    """
    out: list[dict[str, Any]] = []
    for key in ("units", "floor_plans", "plan_summaries"):
        for row in prop.get(key) or []:
            if isinstance(row, dict):
                out.append(row)
    return out


def _payload_signature(prop: dict[str, Any]) -> tuple[tuple[Any, ...], ...]:
    """Order-independent signature of a property's published numbers.

    Sorted so row ORDER cannot mask or manufacture a match, and restricted to
    (area, rent_low, rent_high) because those are the values a shared upstream
    payload would carry identically. Names are excluded on purpose: the Redwood
    case had SYNTHESIZED names, so a name-sensitive signature would have missed
    it.
    """
    triples = [
        (
            _num(_first(row, "area", "sqft")),
            _num(_first(row, "rent_low", "market_rent_low", "asking_rent")),
            _num(_first(row, "rent_high", "market_rent_high")),
        )
        for row in _rows(prop)
        # A row with no numbers at all carries no evidence of sharing.
        if any(
            _num(_first(row, *keys)) is not None
            for keys in (
                ("area", "sqft"),
                ("rent_low", "market_rent_low", "asking_rent"),
                ("rent_high", "market_rent_high"),
            )
        )
    ]
    # None-safe ordering: a partially-absent row must sort deterministically
    # rather than raising. Absent sorts after present at each position.
    triples.sort(key=lambda t: tuple((x is None, x or 0.0) for x in t))
    return tuple(triples)


@dataclass(frozen=True)
class IdenticalPayloadGroup:
    """Two or more properties emitting numerically identical inventory."""

    property_ids: list[str]
    property_names: list[str]
    n_rows: int
    detected_pms: list[str]

    @property
    def is_suspicious(self) -> bool:
        """Differing detection with identical output is the strongest tell."""
        return len(set(self.detected_pms)) > 1


def _detected_pms(prop: dict[str, Any]) -> str:
    meta = prop.get("_meta") or {}
    prov = meta.get("provenance") or {}
    if isinstance(prov, dict):
        return str(prov.get("detected_pms") or prov.get("adapter") or "?")
    return "?"


def find_identical_payload_groups(
    properties: list[dict[str, Any]],
    *,
    min_rows: int = DEFAULT_MIN_ROWS_FOR_COLLISION,
    allow_list: set[frozenset[str]] | None = None,
) -> list[IdenticalPayloadGroup]:
    """Properties whose published numbers are identical to another property's.

    Args:
        properties: Emitted property records for one run.
        min_rows: Ignore groups whose signature is shorter than this; a
            one-row coincidence is not evidence.
        allow_list: Sets of property ids known to legitimately match. Requiring
            an explicit set per exemption keeps a real defect from being
            silenced by a broad rule.

    Returns:
        One group per colliding signature, largest row count first.
    """
    by_sig: dict[tuple[tuple[Any, ...], ...], list[dict[str, Any]]] = {}
    for prop in properties:
        sig = _payload_signature(prop)
        if len(sig) < min_rows:
            continue
        by_sig.setdefault(sig, []).append(prop)

    groups: list[IdenticalPayloadGroup] = []
    for sig, props in by_sig.items():
        if len(props) < 2:
            continue
        ids = [str(p.get("apartment_id") or p.get("property_id") or "?") for p in props]
        if allow_list and frozenset(ids) in allow_list:
            continue
        groups.append(
            IdenticalPayloadGroup(
                property_ids=ids,
                property_names=[str(p.get("proj_name") or "?") for p in props],
                n_rows=len(sig),
                detected_pms=[_detected_pms(p) for p in props],
            )
        )
    groups.sort(key=lambda g: (-g.n_rows, g.property_ids))
    return groups


@dataclass(frozen=True)
class EnvelopeDrift:
    """A property whose published range narrowed sharply run-over-run."""

    property_id: str
    property_name: str
    findings: list[str] = field(default_factory=list)


def _envelope(rows: list[dict[str, Any]], *names: str) -> tuple[float, float] | None:
    vals = [v for v in (_num(_first(r, *names)) for r in rows) if v is not None]
    return (min(vals), max(vals)) if vals else None


def _beds_multiset(rows: list[dict[str, Any]]) -> Counter[Any]:
    return Counter(_first(r, "beds", "bedrooms") for r in rows)


def find_envelope_drift(
    current: list[dict[str, Any]],
    prior: list[dict[str, Any]],
    *,
    retention: float = DEFAULT_ENVELOPE_RETENTION,
) -> list[EnvelopeDrift]:
    """Properties whose rent/area envelope or bed mix collapsed vs *prior*.

    Only reports properties present and non-empty in BOTH runs: a property that
    legitimately went to zero inventory is a different (visible) condition, and
    flagging it here would bury the silent case this check exists for.

    Args:
        current: Emitted property records for the run under test.
        prior: The same for the previous run.
        retention: Report when the new envelope width is below this fraction of
            the old one.

    Returns:
        One entry per affected property.
    """
    prior_by_id = {
        str(p.get("apartment_id") or p.get("property_id") or "?"): p for p in prior
    }
    out: list[EnvelopeDrift] = []

    for prop in current:
        pid = str(prop.get("apartment_id") or prop.get("property_id") or "?")
        was = prior_by_id.get(pid)
        if was is None:
            continue
        now_rows, was_rows = _rows(prop), _rows(was)
        if not now_rows or not was_rows:
            continue

        findings: list[str] = []
        for label, names in (
            ("rent_low", ("rent_low", "market_rent_low", "asking_rent")),
            ("rent_high", ("rent_high", "market_rent_high")),
            ("area", ("area", "sqft")),
        ):
            a, b = _envelope(was_rows, *names), _envelope(now_rows, *names)
            if not a or not b:
                continue
            old_width, new_width = a[1] - a[0], b[1] - b[0]
            if old_width > 0 and new_width < old_width * retention:
                findings.append(
                    f"{label}_envelope_narrowed: {a} -> {b}"
                )

        lost = _beds_multiset(was_rows).keys() - _beds_multiset(now_rows).keys()
        # None is "beds not published", not a bed class.
        lost = {b for b in lost if b is not None}
        if lost:
            findings.append(f"beds_class_lost: {sorted(lost, key=str)}")

        if findings:
            out.append(
                EnvelopeDrift(
                    property_id=pid,
                    property_name=str(prop.get("proj_name") or "?"),
                    findings=findings,
                )
            )
    return out
