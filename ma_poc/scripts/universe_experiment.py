#!/usr/bin/env python3
"""Universe-vs-legacy live-fetch experiment.

Default mode is **live** — for each property in the input CSV the harness:

  1. Builds a CrawlTask and runs the L1 fetcher once (with whatever proxy /
     stealth identity / network capture is configured in the env).
  2. Snapshots the resulting body + network_log so both modes start from
     identical site state — no second fetch, no drift between the two
     replays.
  3. Runs the GenericAdapter twice, once with JUGNU_LLM_TIER_MODE=legacy
     and once with =universe, against that snapshot.
  4. Compares with experiment_comparator.compare and writes per-property
     and summary outputs.

LLM provider, vision provider, and proxy config are all picked up from
ma_poc/.env exactly as the production runner sees them.

Usage
-----
  python scripts/universe_experiment.py --csv config/properties.csv --n 10
  python scripts/universe_experiment.py --csv config/properties.csv --n 5 --seed 7
  python scripts/universe_experiment.py --from-artifacts --run-date 2026-04-17 --n 10

The --from-artifacts mode is the legacy path that replays saved raw_html
+ raw_api artefacts under data/runs/{date}/. Use it only when you want
to test parser changes against historical site state.

Outputs land under ``data/experiments/universe_vs_legacy_{timestamp}/``:
  - samples.json             : the chosen properties + bucket labels
  - per_property/{cid}.json  : both replay outputs + per-property comparison
  - summary.json             : aggregated verdict
  - summary.md               : human-readable report
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

# Allow `python scripts/universe_experiment.py` from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load .env so OPENROUTER_API_KEY, BRIGHTDATA_*, etc. are available before
# the L1 fetcher / LLM provider import-time reads.
try:
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / "ma_poc" / ".env")
except Exception:
    pass

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult  # noqa: E402
from ma_poc.pms.adapters.generic import GenericAdapter  # noqa: E402
from ma_poc.pms.detector import DetectedPMS  # noqa: E402
from ma_poc.services.experiment_comparator import (  # noqa: E402
    Comparison,
    ReplayResult,
    compare,
    summarise,
)
from ma_poc.services.experiment_sampler import Sample, pick_sample  # noqa: E402

log = logging.getLogger("universe_experiment")


# ---------------------------------------------------------------------------
# CSV input
# ---------------------------------------------------------------------------


def _load_csv_rows(csv_path: Path, n: int, seed: int) -> list[dict[str, str]]:
    """Read the property CSV and return ``n`` rows picked deterministically.

    Accepts the standard PropAi shape (apartmentid,name,address,city,state,
    zip,website) and falls back to picking by the first row that has a
    URL-like column.
    """
    rows: list[dict[str, str]] = []
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({k: (v or "").strip() for k, v in row.items()})

    if not rows:
        return []

    keyed: list[tuple[str, dict[str, str]]] = []
    for i, row in enumerate(rows):
        cid = row.get("apartmentid") or row.get("Property ID") or row.get("Unique ID") or str(i)
        digest = hashlib.sha256(f"{cid}|{seed}".encode("utf-8")).hexdigest()
        keyed.append((digest, row))
    keyed.sort(key=lambda t: t[0])
    return [r for _, r in keyed[:n]]


def _row_to_sample(row: dict[str, str]) -> Sample:
    """Map a CSV row into the Sample dataclass the comparator expects."""
    cid = row.get("apartmentid") or row.get("Property ID") or row.get("Unique ID") or ""
    name = row.get("name") or row.get("Property Name") or ""
    url = row.get("website") or row.get("Website") or row.get("url") or ""
    return Sample(
        canonical_id=str(cid),
        property_id=str(cid),
        property_name=str(name),
        url=str(url),
        bucket="live",
        tier_used="",
        units_count=0,
        raw_api_path=None,
        raw_html_path=None,
    )


# ---------------------------------------------------------------------------
# L1 fetch (live mode) — produces a body + network_log snapshot
# ---------------------------------------------------------------------------


async def _live_fetch(sample: Sample) -> tuple[str, list[dict[str, Any]], dict[str, Any]] | None:
    """Run the L1 fetcher once; return (html, api_responses, fetch_diag).

    Returns None on hard failure so the experiment can skip that property
    cleanly. The resulting api_responses are JSON-parsed when possible to
    match the shape the adapter cascade expects (same prep as in
    pms.scraper).
    """
    try:
        from ma_poc.discovery.contracts import CrawlTask, TaskReason
        from ma_poc.fetch import fetch as jugnu_fetch
        from ma_poc.fetch.contracts import RenderMode
    except Exception as exc:
        log.error("could not import L1 fetcher: %s", exc)
        return None

    task = CrawlTask(
        url=sample.url,
        property_id=sample.canonical_id or sample.property_id,
        priority=1,
        budget_ms=180_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.RENDER,
        parent_task_id=None,
    )

    try:
        result = await jugnu_fetch(task)
    except Exception as exc:
        log.warning("fetch raised on %s: %s", sample.url, exc)
        return None

    outcome_val = result.outcome.value if hasattr(result.outcome, "value") else str(result.outcome)
    diag = {
        "outcome": outcome_val,
        "status": result.status,
        "elapsed_ms": result.elapsed_ms,
        "render_mode": str(result.render_mode),
        "proxy_used": result.proxy_used,
        "final_url": result.final_url,
        "error_signature": result.error_signature,
        "network_log_count": len(result.network_log or []),
    }

    if outcome_val != "OK":
        log.info("fetch %s → %s (%s)", sample.canonical_id, outcome_val, result.error_signature)
        return None

    body = result.body
    if body is None:
        return None
    try:
        html = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    except Exception:
        html = ""

    # Mirror pms.scraper's network_log → api_responses prep so the adapter
    # sees the same dict shape it gets in production.
    prepared: list[dict[str, Any]] = []
    for entry in result.network_log or []:
        if not isinstance(entry, dict):
            continue
        raw_body = entry.get("body")
        parsed_body: Any = raw_body
        if isinstance(raw_body, str) and raw_body.strip().startswith(("{", "[")):
            try:
                parsed_body = json.loads(raw_body)
            except Exception:
                parsed_body = raw_body
        prepared.append(
            {
                "url": entry.get("url", ""),
                "body": parsed_body,
                "status": entry.get("status"),
                "content_type": entry.get("content_type"),
            }
        )
    return html, prepared, diag


# ---------------------------------------------------------------------------
# Replay — runs the adapter once in the named mode against the snapshot
# ---------------------------------------------------------------------------


def _build_ctx(sample: Sample, html: str, api_responses: list[dict[str, Any]]) -> AdapterContext:
    """Construct an AdapterContext that mirrors what the production cascade
    builds. Profile is None — the experiment is measuring cold extraction."""
    ctx = AdapterContext(
        base_url=sample.url,
        detected=DetectedPMS(pms="unknown", confidence=0.0),
        profile=None,
        expected_total_units=None,
        property_id=sample.property_id or sample.canonical_id,
        property_name=sample.property_name,
    )
    if html:
        ctx.fetch_result = type("_FR", (), {"body": html.encode("utf-8")})()  # type: ignore[attr-defined]
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


async def _replay_one(
    sample: Sample,
    mode: str,
    html: str,
    api_responses: list[dict[str, Any]],
) -> tuple[ReplayResult, dict[str, Any] | None]:
    """Run the adapter once and return (ReplayResult, universe_debug | None).

    The second element is None for legacy mode and for any run where
    JUGNU_UNIVERSE_DEBUG isn't enabled. The harness writes it into the
    per-property JSON so post-mortem analysis has full visibility into
    what the gather → rank → explore loop did.
    """
    os.environ["JUGNU_LLM_TIER_MODE"] = mode
    ctx = _build_ctx(sample, html, copy.deepcopy(api_responses))

    t0 = time.monotonic()
    try:
        result: AdapterResult = await GenericAdapter().extract(page=None, ctx=ctx)
    except Exception as exc:
        return (
            ReplayResult(
                units=[],
                tier_used="ERRORED",
                errors=[f"adapter_raised: {exc}"],
                wall_clock_ms=int((time.monotonic() - t0) * 1000),
            ),
            None,
        )

    interactions: list[dict[str, Any]] = getattr(result, "_llm_interactions", []) or []
    cost = sum(float(i.get("cost_usd") or 0.0) for i in interactions)
    debug = getattr(result, "_universe_debug", None)
    return (
        ReplayResult(
            units=list(result.units),
            tier_used=result.tier_used,
            llm_cost_usd=cost,
            llm_calls=len(interactions),
            wall_clock_ms=int((time.monotonic() - t0) * 1000),
            errors=list(result.errors),
        ),
        debug,
    )


# ---------------------------------------------------------------------------
# Artefact-replay path (kept for parser regression tests)
# ---------------------------------------------------------------------------


def _load_raw_api(path: str | None) -> list[dict[str, Any]]:
    if not path or not Path(path).is_file():
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict) and isinstance(data.get("api_responses"), list):
        return [d for d in data["api_responses"] if isinstance(d, dict)]
    return []


def _load_raw_html(path: str | None) -> str:
    if not path or not Path(path).is_file():
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


async def _replay_pair_from_artifact(
    sample: Sample,
) -> tuple[ReplayResult, ReplayResult, dict[str, Any] | None] | None:
    html = _load_raw_html(sample.raw_html_path)
    api_responses = _load_raw_api(sample.raw_api_path)
    if not html and not api_responses:
        log.info("skip %s — no html and no raw_api artifact", sample.canonical_id)
        return None
    legacy, _ = await _replay_one(sample, "legacy", html, api_responses)
    universe, debug = await _replay_one(sample, "universe", html, api_responses)
    return legacy, universe, debug


async def _replay_pair_live(
    sample: Sample,
) -> tuple[ReplayResult, ReplayResult, dict[str, Any] | None] | None:
    snapshot = await _live_fetch(sample)
    if snapshot is None:
        return None
    html, api_responses, _diag = snapshot
    legacy, _ = await _replay_one(sample, "legacy", html, api_responses)
    universe, debug = await _replay_one(sample, "universe", html, api_responses)
    return legacy, universe, debug


# ---------------------------------------------------------------------------
# Output writing
# ---------------------------------------------------------------------------


def _provenance() -> dict[str, Any]:
    """Capture the LLM + proxy + model env so re-runs are reproducible."""
    return {
        "llm_provider": os.getenv("LLM_PROVIDER") or "",
        "openrouter_model": os.getenv("OPENROUTER_MODEL") or "",
        "vision_provider": os.getenv("VISION_PROVIDER") or "",
        "proxy_provider": os.getenv("PROXY_PROVIDER") or "",
        "proxy_host": os.getenv("PROXY_HOST") or "",
        "brightdata_resi_zone": os.getenv("BRIGHTDATA_RESI_ZONE") or "",
        "schema_version": os.getenv("SCHEMA_VERSION") or "",
    }


def _write_samples(out_dir: Path, samples: list[Sample], mode: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": mode,
        "provenance": _provenance(),
        "samples": [asdict(s) for s in samples],
    }
    (out_dir / "samples.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_per_property(
    out_dir: Path,
    sample: Sample,
    legacy: ReplayResult,
    universe: ReplayResult,
    comparison: Comparison,
    universe_debug: dict[str, Any] | None = None,
) -> None:
    per = out_dir / "per_property"
    per.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "sample": asdict(sample),
        "legacy": asdict(legacy),
        "universe": asdict(universe),
        "comparison": comparison.to_dict(),
    }
    if universe_debug:
        # Lives in its own key so comparator-only consumers stay happy.
        payload["universe_debug"] = universe_debug
    (per / f"{sample.canonical_id or sample.property_id or 'unknown'}.json").write_text(
        json.dumps(payload, indent=2, default=str),
        encoding="utf-8",
    )


def _write_summary(out_dir: Path, comparisons: list[Comparison]) -> None:
    summary = summarise(comparisons)
    (out_dir / "summary.json").write_text(
        json.dumps(summary.to_dict(), indent=2),
        encoding="utf-8",
    )

    md_lines = [
        "# Universe-vs-Legacy Experiment",
        "",
        f"- Compared: {summary.n_compared}",
        f"- Wins (universe strictly better): {summary.n_wins}",
        f"- Regressions (universe lost units legacy had): {summary.n_regressions}",
        f"- Ties: {summary.n_ties}",
        f"- Avg unit count delta: {summary.avg_unit_count_delta:+.2f}",
        f"- Avg cost delta: ${summary.avg_cost_delta_usd:+.4f} per property",
        f"- Avg latency delta: {summary.avg_latency_delta_ms:+.0f} ms",
        "",
        "## Field coverage delta (universe - legacy)",
    ]
    for field, delta in summary.avg_field_coverage_delta.items():
        md_lines.append(f"- **{field}**: {delta:+.2%}")

    md_lines += [
        "",
        "## Per-bucket breakdown",
    ]
    for bucket, stats in summary.by_bucket.items():
        md_lines.append(
            f"- **{bucket}** - n={stats['n']}, "
            f"wins={stats['wins']}, regressions={stats['regressions']}, "
            f"avg unit delta {stats['avg_unit_count_delta']:+.2f}, "
            f"avg cost delta ${stats['avg_cost_delta_usd']:+.4f}"
        )

    md_lines += [
        "",
        "## Promote verdict",
        "",
        f"**{'PROMOTE' if summary.promote else 'HOLD'}** - {summary.promote_reason}",
        "",
    ]
    (out_dir / "summary.md").write_text("\n".join(md_lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Universe-vs-legacy experiment harness")
    src = p.add_mutually_exclusive_group()
    src.add_argument(
        "--csv",
        default=None,
        help="property CSV (live-fetch mode). Default: ma_poc/config/properties.csv",
    )
    src.add_argument(
        "--from-artifacts",
        action="store_true",
        help="replay saved raw_html + raw_api from a prior run instead of live fetching",
    )
    p.add_argument("--run-date", default=None, help="run date when --from-artifacts is set")
    p.add_argument(
        "--data-dir",
        default="ma_poc/data",
        help="data root for runs/ and raw_html/ (default: ma_poc/data)",
    )
    p.add_argument("--n", type=int, default=10, help="sample size (default: 10)")
    p.add_argument("--seed", type=int, default=42, help="determinism seed (default: 42)")
    p.add_argument(
        "--bucket",
        default=None,
        help="single-bucket mode (artefact mode only)",
    )
    p.add_argument(
        "--out-dir",
        default=None,
        help="output dir (default: data/experiments/universe_vs_legacy_{timestamp})",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap number of properties actually replayed (smoke test)",
    )
    p.add_argument(
        "--only-id",
        default=None,
        help="restrict the run to a single apartmentid / canonical_id (e.g. 39156)",
    )
    p.add_argument(
        "--debug-universe",
        action="store_true",
        help="enable JUGNU_UNIVERSE_DEBUG=1 so the explorer captures pre-rank "
             "universe, LLM ranker output, prompts, and raw responses",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="max in-flight properties (default 1 = sequential). Properties "
             "are I/O-bound — each one spends most of its time awaiting "
             "Playwright renders and LLM responses, so 4-8 concurrent gives "
             "near-linear speedup until the OpenRouter rate limit or browser "
             "RAM cap is hit.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


async def _main_async(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Flip the universe-explorer debug flag BEFORE the explorer module
    # reads it (which it does at module import inside _run_universe_explorer).
    if args.debug_universe:
        os.environ["JUGNU_UNIVERSE_DEBUG"] = "1"
        # universe_explorer reads DEBUG at module import; reload if it's
        # already imported so the flag takes effect.
        import importlib

        try:
            import ma_poc.services.universe_explorer as _ue

            importlib.reload(_ue)
        except Exception:
            pass
        log.info("JUGNU_UNIVERSE_DEBUG=1 — capturing pre-rank universe, prompts, raw responses")

    data_dir = Path(args.data_dir)
    out_root = Path(args.out_dir) if args.out_dir else (
        data_dir / "experiments" / f"universe_vs_legacy_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
    )
    out_root.mkdir(parents=True, exist_ok=True)
    log.info("output dir: %s", out_root)
    log.info("provenance: %s", _provenance())

    # Choose mode + assemble sample list.
    if args.from_artifacts:
        if not args.run_date:
            log.error("--from-artifacts requires --run-date YYYY-MM-DD")
            return 2
        run_dir = data_dir / "runs" / args.run_date
        if not run_dir.is_dir():
            log.error("run dir not found: %s", run_dir)
            return 2
        samples = pick_sample(
            run_dir=run_dir,
            n=args.n,
            seed=args.seed,
            bucket_filter=args.bucket,
        )
        mode_label = f"from-artifacts:{args.run_date}"
        replay_pair = _replay_pair_from_artifact
    else:
        csv_path = Path(args.csv) if args.csv else (_REPO_ROOT / "ma_poc" / "config" / "properties.csv")
        if not csv_path.is_file():
            log.error("CSV not found: %s", csv_path)
            return 2
        rows = _load_csv_rows(csv_path, args.n, args.seed)
        samples = [_row_to_sample(r) for r in rows if (r.get("website") or r.get("Website"))]
        mode_label = f"live:{csv_path.name}"
        replay_pair = _replay_pair_live

    if args.only_id:
        wanted = args.only_id.strip()
        samples = [s for s in samples if s.canonical_id == wanted or s.property_id == wanted]
        if not samples:
            # Single-property mode without going through the sampler — synthesise
            # a Sample directly from the CSV so users can target an id that
            # wasn't in the picked subset.
            log.info("--only-id %s not in picked sample, looking it up in the CSV", wanted)
            csv_path = Path(args.csv) if args.csv else (_REPO_ROOT / "ma_poc" / "config" / "properties.csv")
            with csv_path.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    cid = (row.get("apartmentid") or row.get("Property ID") or "").strip()
                    if cid == wanted:
                        samples = [_row_to_sample({k: (v or "").strip() for k, v in row.items()})]
                        break
            if not samples:
                log.error("--only-id %s not found in CSV", wanted)
                return 2
    if args.limit is not None:
        samples = samples[: args.limit]
    log.info("picked %d samples (mode=%s)", len(samples), mode_label)
    _write_samples(out_root, samples, mode_label)

    comparisons: list[Comparison] = []
    semaphore = asyncio.Semaphore(max(1, args.concurrency))

    async def _process(idx: int, sample: Sample) -> Comparison | None:
        """Run one property under the concurrency cap and write its file."""
        async with semaphore:
            log.info(
                "[%d/%d START] %s (%s) — %s",
                idx,
                len(samples),
                sample.canonical_id,
                sample.bucket,
                sample.url,
            )
            pair = await replay_pair(sample)
            if pair is None:
                log.info("[%d/%d SKIP] %s — no fetch artifact", idx, len(samples), sample.canonical_id)
                return None
            legacy, universe, universe_debug = pair
            comparison = compare(
                legacy=legacy,
                universe=universe,
                canonical_id=sample.canonical_id,
                bucket=sample.bucket,
            )
            _write_per_property(
                out_root,
                sample,
                legacy,
                universe,
                comparison,
                universe_debug=universe_debug,
            )
            log.info(
                "[%d/%d DONE] %s — legacy: tier=%s units=%d cost=$%.4f calls=%d  |  universe: tier=%s units=%d cost=$%.4f calls=%d",
                idx,
                len(samples),
                sample.canonical_id,
                legacy.tier_used,
                len(legacy.units),
                legacy.llm_cost_usd,
                legacy.llm_calls,
                universe.tier_used,
                len(universe.units),
                universe.llm_cost_usd,
                universe.llm_calls,
            )
            return comparison

    if args.concurrency > 1:
        log.info("running %d properties with concurrency=%d", len(samples), args.concurrency)
    results = await asyncio.gather(
        *(_process(i, s) for i, s in enumerate(samples, 1)),
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, Comparison):
            comparisons.append(r)
        elif isinstance(r, BaseException):
            log.warning("property raised: %s", r)

    _write_summary(out_root, comparisons)
    log.info("done — wrote summary.json and summary.md to %s", out_root)
    return 0


def main() -> None:
    args = _parse_args()
    sys.exit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
