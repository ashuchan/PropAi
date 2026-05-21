"""Build stratified canary CSVs for the May-13 API-tier port.

Reads ``failures.csv`` + ``successes.csv`` from one or more
``ma_poc/data/reports/cloud_run_<date>/`` directories (produced by
``scripts/diagnostics/analyze_cloud_run.py``) and emits two CSVs:

  * ``canary_500.csv`` — 500-property merge-gate sample
  * ``canary_50.csv``  — 50-property dev-iteration subsample

The stratification rules are declared in ``STRATA`` below and follow
§5.2 of ``ma_poc/docs/MAY13_API_TIER_PORT_PLAN.md``. Each bucket
exercises one or more commits in the PR; the script fails loud when a
bucket comes up short of its target so silent under-sampling cannot
mask a regression in a thin bucket (e.g. G5 cloud).

Determinism: sampling uses ``random.Random(seed)`` so re-running with
the same source artifacts produces an identical CSV. Default seed is
2026.

Usage::

    python ma_poc/tests/canary/build_canary_csv.py \\
        --report-dir ma_poc/data/reports/cloud_run_2026-05-19 \\
        --report-dir ma_poc/data/reports/cloud_run_2026-05-18 \\
        --out-dir ma_poc/tests/canary

When multiple ``--report-dir`` are given, rows are merged keyed by
``property_id`` with the most-recent report's row winning on conflict.
This is intentional: the failure population shifts day-to-day, and the
most recent run is the most representative.
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


# ────────────────────────────────────────────────────────────────────
# Source-row model
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PropertyRow:
    """One property's outcome in a prior cloud run.

    Built from ``failures.csv`` or ``successes.csv``. Unknown fields
    default to empty strings so downstream filters can treat them as
    "no signal" without raising.
    """

    property_id: str
    url: str
    domain: str
    verdict: str
    pms_detected: str
    terminal_tier: str
    pattern_id: str
    pattern_sub: str
    units: int  # 0 for failure rows (failures.csv has no units column)

    @property
    def is_failure(self) -> bool:
        return self.verdict in {"FAILED_NO_DATA", "FAILED_UNREACHABLE"}

    @property
    def is_success(self) -> bool:
        # Match the production success classifier in
        # ma_poc/reporting/verdict.py:_SUCCESS_VERDICTS.
        return self.verdict in {"SUCCESS", "SUCCESS_PLAN_LEVEL", "SUCCESS_PARTIAL"}


# ────────────────────────────────────────────────────────────────────
# Stratification rules — declarative
# ────────────────────────────────────────────────────────────────────


@dataclass
class Stratum:
    """One stratified sample bucket.

    Attributes:
        name: Human-readable bucket label; appears in failure warnings
            and the CSV ``_bucket`` audit column.
        target_500: Target count in the 500-property full canary.
        target_50:  Target count in the 50-property quick canary.
        commits:    Which commits this bucket validates (free-form text,
            used only in audit output).
        predicate:  Callable that returns True for rows belonging to
            this bucket. Predicates are applied in declared order; once
            a row is admitted to a bucket it is removed from the pool,
            so order encodes priority.
    """

    name: str
    target_500: int
    target_50: int
    commits: str
    predicate: Callable[[PropertyRow], bool]


def _pms_in(*pms_names: str) -> Callable[[PropertyRow], bool]:
    pms_set = {n.lower() for n in pms_names}

    def pred(r: PropertyRow) -> bool:
        return (r.pms_detected or "").lower() in pms_set

    return pred


def _terminal_tier_contains(*needles: str) -> Callable[[PropertyRow], bool]:
    needles_lower = tuple(n.lower() for n in needles)

    def pred(r: PropertyRow) -> bool:
        tt = (r.terminal_tier or "").lower()
        return any(n in tt for n in needles_lower)

    return pred


def _and(*preds: Callable[[PropertyRow], bool]) -> Callable[[PropertyRow], bool]:
    return lambda r: all(p(r) for p in preds)


def _or(*preds: Callable[[PropertyRow], bool]) -> Callable[[PropertyRow], bool]:
    return lambda r: any(p(r) for p in preds)


# Rules track the table in MAY13_API_TIER_PORT_PLAN.md §5.2. Order is
# load-bearing: known-SUCCESS regression watch fires first so its 150
# rows are reserved before any failure bucket pulls from the same
# pool. Failure-bucket order then follows yield magnitude (largest
# adapter wins first, smallest later) so a shortage in a thin bucket
# is signalled but doesn't starve the big ones.
STRATA: list[Stratum] = [
    Stratum(
        name="known_success_regression_watch",
        target_500=150,
        target_50=15,
        commits="all (regression watchdog)",
        predicate=lambda r: r.is_success,
    ),
    Stratum(
        name="rentcafe",
        target_500=80,
        target_50=6,
        commits="9",
        predicate=_and(
            lambda r: r.is_failure,
            _or(
                _pms_in("rentcafe"),
                _terminal_tier_contains("rentcafe", "securecafe"),
            ),
        ),
    ),
    Stratum(
        name="entrata",
        target_500=60,
        target_50=5,
        commits="5",
        predicate=_and(
            lambda r: r.is_failure,
            _or(
                _pms_in("entrata"),
                _terminal_tier_contains("entrata", "prospectportal"),
            ),
        ),
    ),
    Stratum(
        name="realpage_oll_category_d",
        target_500=40,
        target_50=4,
        commits="10",
        predicate=_and(
            lambda r: r.is_failure,
            _or(
                _pms_in("realpage_oll"),
                _terminal_tier_contains("realpage", "oll"),
            ),
        ),
    ),
    Stratum(
        name="sightmap_shape_rejected",
        target_500=30,
        target_50=3,
        commits="7",
        predicate=_and(
            lambda r: r.is_failure,
            _or(
                _pms_in("sightmap"),
                _terminal_tier_contains("sightmap", "shape_rejected", "amenities_only"),
            ),
        ),
    ),
    Stratum(
        name="appfolio_vanity_or_embed",
        target_500=25,
        target_50=3,
        commits="8",
        predicate=_and(
            lambda r: r.is_failure,
            _or(
                _pms_in("appfolio"),
                _terminal_tier_contains("appfolio"),
            ),
        ),
    ),
    Stratum(
        name="g5_cloud",
        target_500=25,
        target_50=2,
        commits="12 (g5 adapter)",
        # G5 isn't a current main PMS, so failures appear with
        # pms_detected=unknown/custom and host markers in the URL.
        predicate=_and(
            lambda r: r.is_failure,
            lambda r: "g5-cl-" in (r.url or "").lower()
            or "g5search" in (r.url or "").lower()
            or "g5_" in (r.terminal_tier or "").lower(),
        ),
    ),
    Stratum(
        name="onesite_empty_or_no_response",
        target_500=20,
        target_50=2,
        commits="6",
        predicate=_and(
            lambda r: r.is_failure,
            _or(
                _pms_in("onesite"),
                _terminal_tier_contains("onesite"),
            ),
        ),
    ),
    Stratum(
        name="knock",
        target_500=20,
        target_50=2,
        commits="12 (knock adapter)",
        predicate=_and(
            lambda r: r.is_failure,
            lambda r: "knock" in (r.url or "").lower()
            or "doorway" in (r.url or "").lower()
            or "knck.io" in (r.url or "").lower(),
        ),
    ),
    Stratum(
        name="reit_new_adapters",
        target_500=30,
        target_50=3,
        commits="11,13 (cortland/equity/maac/irvine/essex/rentvision)",
        predicate=_and(
            lambda r: r.is_failure,
            lambda r: any(
                marker in (r.url or "").lower()
                for marker in (
                    "cortland.com",
                    "equityapartments.com",
                    "maac.com",
                    "maacommunities.com",
                    "irvinecompanyapartments.com",
                    "essexapartmenthomes.com",
                )
            ),
        ),
    ),
    Stratum(
        name="rentmanager_apts247_iloveleasing",
        target_500=10,
        target_50=1,
        commits="11,12 (rentmanager/apts247)",
        predicate=_and(
            lambda r: r.is_failure,
            lambda r: any(
                marker in (r.url or "").lower()
                for marker in (
                    "rentmanager.com",
                    "apts247",
                    "iloveleasing",
                    "rentdynamics",
                )
            ),
        ),
    ),
    Stratum(
        name="no_leasing_path_resolver",
        target_500=10,
        target_50=1,
        commits="2 (resolver patterns)",
        # Properties whose extraction never started — fetcher succeeded
        # but the resolver couldn't find a leasing path. In the analyzer
        # output these surface as failed_no_data with no terminal_tier
        # (the cascade never ran).
        predicate=_and(
            lambda r: r.is_failure,
            lambda r: not r.terminal_tier
            and r.pms_detected in ("", "unknown", "custom"),
        ),
    ),
]


# ────────────────────────────────────────────────────────────────────
# Loading
# ────────────────────────────────────────────────────────────────────


def _safe_int(s: str) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def load_report_dir(report_dir: Path) -> list[PropertyRow]:
    """Load both ``failures.csv`` and ``successes.csv`` from a report dir.

    Returns rows in the order failures-first, successes-second.
    """
    rows: list[PropertyRow] = []
    for filename, has_units in (("failures.csv", False), ("successes.csv", True)):
        path = report_dir / filename
        if not path.exists():
            print(f"[warn] {path} missing; skipping", file=sys.stderr)
            continue
        with path.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(
                    PropertyRow(
                        property_id=(row.get("property_id") or "").strip(),
                        url=(row.get("url") or "").strip(),
                        domain=(row.get("domain") or "").strip(),
                        verdict=(row.get("verdict") or "").strip(),
                        pms_detected=(row.get("pms_detected") or "").strip(),
                        terminal_tier=(row.get("terminal_tier") or "").strip(),
                        pattern_id=(row.get("pattern_id") or "").strip(),
                        pattern_sub=(row.get("pattern_sub") or "").strip(),
                        units=_safe_int(row.get("units") or "0") if has_units else 0,
                    )
                )
    return rows


def merge_reports(report_dirs: Iterable[Path]) -> list[PropertyRow]:
    """Merge rows from multiple report dirs keyed by ``property_id``.

    Most-recent dir wins on conflict. Order of input dirs is therefore
    significant — pass newest first.
    """
    seen: dict[str, PropertyRow] = {}
    for d in report_dirs:
        for row in load_report_dir(d):
            if not row.property_id:
                continue
            # First write wins because we pass newest first.
            seen.setdefault(row.property_id, row)
    return list(seen.values())


# ────────────────────────────────────────────────────────────────────
# Stratified sampling
# ────────────────────────────────────────────────────────────────────


@dataclass
class StratumSample:
    stratum: Stratum
    rows: list[PropertyRow] = field(default_factory=list)
    shortfall: int = 0  # target - actual; >0 means under-sampled


def _dedupe_by_domain(rows: list[PropertyRow]) -> list[PropertyRow]:
    """Keep at most one row per domain.

    Prevents over-sampling a single PMC. If a domain has both a
    failure and a success row, the failure wins (it's the actionable
    case for this PR).
    """
    by_domain: dict[str, PropertyRow] = {}
    for r in rows:
        if not r.domain:
            # No domain ⇒ can't dedupe; keep as-is.
            by_domain[r.property_id] = r
            continue
        existing = by_domain.get(r.domain)
        if existing is None or (r.is_failure and not existing.is_failure):
            by_domain[r.domain] = r
    return list(by_domain.values())


def stratify(
    rows: list[PropertyRow],
    target: str,
    seed: int = 2026,
) -> list[StratumSample]:
    """Apply ``STRATA`` to ``rows``, returning one sample per stratum.

    ``target`` is ``"500"`` or ``"50"`` and picks the corresponding
    target count from each stratum.

    Sampling is destructive: rows admitted to one stratum are removed
    from the pool before the next stratum runs, so each PID appears at
    most once in the output. Within a stratum, selection is random
    (seeded) once the pool exceeds the target — otherwise all
    candidates are kept.
    """
    if target not in ("500", "50"):
        raise ValueError(f"target must be '500' or '50', got {target!r}")
    rng = random.Random(seed)
    pool = _dedupe_by_domain(rows)
    samples: list[StratumSample] = []
    used: set[str] = set()

    for stratum in STRATA:
        want = stratum.target_500 if target == "500" else stratum.target_50
        candidates = [r for r in pool if r.property_id not in used and stratum.predicate(r)]
        if len(candidates) > want:
            picks = rng.sample(candidates, want)
        else:
            picks = candidates
        shortfall = want - len(picks)
        used.update(p.property_id for p in picks)
        samples.append(StratumSample(stratum=stratum, rows=picks, shortfall=shortfall))

    return samples


# ────────────────────────────────────────────────────────────────────
# CSV writer
# ────────────────────────────────────────────────────────────────────


# Columns match the schema ``ma_poc/scripts/runners/jugnu.py`` accepts.
# ``_bucket`` is an audit-only extra column; jugnu ignores unknown
# columns at parse time, so it's safe to ship.
CANARY_COLUMNS = (
    "property_id",
    "url",
    "name",
    "_bucket",
    "_verdict_baseline",
    "_pms_baseline",
    "_tier_baseline",
)


def write_canary_csv(samples: list[StratumSample], out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(CANARY_COLUMNS)
        for s in samples:
            for r in s.rows:
                writer.writerow(
                    [
                        r.property_id,
                        r.url,
                        r.domain,  # using domain as a stable name fallback
                        s.stratum.name,
                        r.verdict,
                        r.pms_detected,
                        r.terminal_tier,
                    ]
                )
                written += 1
    return written


def print_summary(target: str, samples: list[StratumSample]) -> bool:
    """Print a summary table; return True iff every stratum met its target."""
    print(f"\n=== Canary stratification for target=canary_{target}.csv ===")
    print(f"{'bucket':<40} {'target':>7} {'got':>5} {'short':>6}  commits")
    print("-" * 100)
    all_ok = True
    total_got = 0
    total_target = 0
    for s in samples:
        want = s.stratum.target_500 if target == "500" else s.stratum.target_50
        got = len(s.rows)
        status = "OK " if s.shortfall == 0 else "SHORT"
        print(
            f"{s.stratum.name:<40} {want:>7} {got:>5} {s.shortfall:>6}  "
            f"[{status}] {s.stratum.commits}"
        )
        all_ok = all_ok and (s.shortfall == 0)
        total_got += got
        total_target += want
    print("-" * 100)
    print(f"{'TOTAL':<40} {total_target:>7} {total_got:>5} {total_target-total_got:>6}")
    return all_ok


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Build stratified canary CSVs for the May-13 API-tier port.",
    )
    p.add_argument(
        "--report-dir",
        action="append",
        required=True,
        type=Path,
        help="Cloud-run report directory (newest first). Repeat the flag for "
        "multiple dirs; rows are merged keyed by property_id with newer wins.",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).parent,
        help="Output directory for canary_500.csv + canary_50.csv "
        "(default: alongside this script).",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=2026,
        help="Random seed for sampling (default 2026).",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any stratum is short of its target.",
    )
    args = p.parse_args(argv)

    rows = merge_reports(args.report_dir)
    if not rows:
        print("[error] no rows loaded; check --report-dir paths", file=sys.stderr)
        return 2
    print(f"[info] loaded {len(rows)} unique property rows from {len(args.report_dir)} report(s)")

    samples_500 = stratify(rows, target="500", seed=args.seed)
    samples_50 = stratify(rows, target="50", seed=args.seed)

    n500 = write_canary_csv(samples_500, args.out_dir / "canary_500.csv")
    n50 = write_canary_csv(samples_50, args.out_dir / "canary_50.csv")
    print(f"[info] wrote {n500} rows to {args.out_dir / 'canary_500.csv'}")
    print(f"[info] wrote {n50} rows to {args.out_dir / 'canary_50.csv'}")

    ok_500 = print_summary("500", samples_500)
    ok_50 = print_summary("50", samples_50)

    if args.strict and not (ok_500 and ok_50):
        print("\n[fail] strata are short of target; --strict enabled, exiting 1", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
