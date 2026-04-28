"""Stratified sampler for the universe-vs-legacy experiment harness.

Reads a production run's ``properties.json`` and picks a representative
sample of properties across five buckets:

  - ``tier1``           — deterministic Tier 1 wins (regression check)
  - ``tier_2_3``        — JSON-LD or DOM wins
  - ``tier4_llm``       — current LLM tier wins (head-to-head with universe)
  - ``link_hop``        — link-hop recovered units
  - ``failed_no_data``  — verdict was FAILED_NO_DATA but body was present

Selection is deterministic per ``(canonical_id, seed)`` so re-running the
sampler with the same seed picks the same properties.

Tolerates both pipeline formats:
  - Jugnu: tier in ``_extract_result.tier_used``, verdict in ``_meta.verdict``.
  - Legacy: tier in ``_meta.scrape_tier_used``; no verdict — failure is
    inferred from ``len(units) == 0 AND has_raw_api``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# Default bucket targets — fractions sum to 1.0 (sampler tops up from
# adjacent buckets if any single bucket is short).
DEFAULT_BUCKET_TARGETS: dict[str, float] = {
    "tier1": 0.25,
    "tier_2_3": 0.15,
    "tier4_llm": 0.25,
    "link_hop": 0.15,
    "failed_no_data": 0.20,
}


@dataclass(frozen=True)
class Sample:
    """One picked property — enough metadata for the comparator to run."""

    canonical_id: str
    property_id: str
    property_name: str
    url: str
    bucket: str
    tier_used: str
    units_count: int
    raw_api_path: str | None
    raw_html_path: str | None


def _tier_used(record: dict[str, Any]) -> str:
    """Read the tier identifier from either pipeline format."""
    extract_result = record.get("_extract_result") or {}
    if isinstance(extract_result, dict) and extract_result.get("tier_used"):
        return str(extract_result["tier_used"])
    meta = record.get("_meta") or {}
    return str(meta.get("scrape_tier_used") or "")


def _link_hop_used(record: dict[str, Any]) -> bool:
    return bool(record.get("_link_hop_success"))


def _verdict(record: dict[str, Any]) -> str:
    meta = record.get("_meta") or {}
    return str(meta.get("verdict") or "")


def _bucket_for(record: dict[str, Any]) -> str | None:
    """Classify a property record into one of the five experiment buckets.

    Returns None when the record doesn't fit any bucket (e.g. carry-forward
    only, no extraction signal, or PROPERTY_DISAPPEARED).
    """
    tier = _tier_used(record).upper()
    verdict = _verdict(record).upper()
    units = record.get("units") or []

    if _link_hop_used(record) and units:
        return "link_hop"
    if verdict == "FAILED_NO_DATA":
        return "failed_no_data"
    # Legacy fallback when verdict is missing: treat empty-units-with-tier
    # as a failure case worth replaying.
    if not units and tier and tier not in ("CARRY_FORWARD",):
        return "failed_no_data"
    if tier.startswith("TIER_4") or "LLM" in tier:
        return "tier4_llm"
    if tier.startswith("TIER_2") or tier.startswith("TIER_3") or "JSONLD" in tier or "DOM" in tier:
        return "tier_2_3"
    if tier.startswith("TIER_1") or tier.startswith("TIER_5_5_EXPLORATORY"):
        # TIER_5_5_EXPLORATORY is the legacy pipeline's "Phase 5 won
        # via API interception" — treat it as tier1 for the experiment.
        return "tier1"
    return None


def _resolve_artifact_paths(
    run_dir: Path,
    property_id: str,
    canonical_id: str,
) -> tuple[Path | None, Path | None]:
    """Find the raw_api and raw_html artifacts for a property.

    Tries property_id first (legacy pipeline naming) then canonical_id
    (Jugnu pipeline naming). Returns (raw_api_path, raw_html_path) where
    each is None when not found.
    """
    raw_api = run_dir / "raw_api"
    raw_api_path: Path | None = None
    if raw_api.is_dir():
        for stem in (property_id, canonical_id):
            cand = raw_api / f"{stem}.json"
            if cand.is_file():
                raw_api_path = cand
                break

    # raw_html lives under data/raw_html/{date}/{id}/...; the run_dir's
    # parent is data/runs/, so we walk back to data/raw_html/{date}.
    raw_html_path: Path | None = None
    data_dir = run_dir.parent.parent if run_dir.parent.name == "runs" else None
    if data_dir is not None:
        date_dir = data_dir / "raw_html" / run_dir.name
        if date_dir.is_dir():
            for stem in (property_id, canonical_id):
                cand = date_dir / f"{stem}.html"
                if cand.is_file():
                    raw_html_path = cand
                    break

    return raw_api_path, raw_html_path


def pick_sample(
    run_dir: Path,
    n: int = 50,
    seed: int = 42,
    bucket_filter: str | None = None,
    targets: dict[str, float] | None = None,
) -> list[Sample]:
    """Pick a stratified, deterministic sample of ``n`` properties.

    Parameters
    ----------
    run_dir
        ``data/runs/{date}/`` of a prior production run.
    n
        Total sample size. Per-bucket counts derive from ``targets``.
    seed
        Determinism seed — same seed + same run_dir always picks the
        same properties.
    bucket_filter
        If set (one of the bucket names), only that bucket is sampled
        and ``n`` becomes the size of that single bucket. Useful for
        focused investigations.
    targets
        Per-bucket fractions. Defaults to ``DEFAULT_BUCKET_TARGETS``.
    """
    props_path = run_dir / "properties.json"
    if not props_path.is_file():
        raise FileNotFoundError(f"properties.json not found at {props_path}")

    with props_path.open(encoding="utf-8") as f:
        records = json.load(f)
    if not isinstance(records, list):
        raise ValueError(f"{props_path} is not a list — got {type(records).__name__}")

    targets = targets or DEFAULT_BUCKET_TARGETS

    by_bucket: dict[str, list[dict[str, Any]]] = {b: [] for b in targets}
    for rec in records:
        bucket = _bucket_for(rec)
        if bucket is None or bucket not in by_bucket:
            continue
        if bucket_filter and bucket != bucket_filter:
            continue
        by_bucket[bucket].append(rec)

    selected: list[Sample] = []

    if bucket_filter:
        # Single-bucket mode: pull exactly ``n`` from the named bucket.
        chosen = _deterministic_pick(by_bucket.get(bucket_filter, []), n, seed)
        selected.extend(_to_samples(chosen, bucket_filter, run_dir))
    else:
        for bucket, frac in targets.items():
            quota = max(1, int(round(n * frac)))
            chosen = _deterministic_pick(by_bucket[bucket], quota, seed)
            selected.extend(_to_samples(chosen, bucket, run_dir))

        # If any bucket fell short, top up from the next-largest bucket.
        if len(selected) < n:
            shortfall = n - len(selected)
            already_chosen_ids = {s.canonical_id for s in selected}
            remainder = [
                rec
                for bucket_recs in by_bucket.values()
                for rec in bucket_recs
                if _canonical_id_of(rec) not in already_chosen_ids
            ]
            extra = _deterministic_pick(remainder, shortfall, seed)
            selected.extend(_to_samples(extra, "topup", run_dir))

    return selected


def _canonical_id_of(record: dict[str, Any]) -> str:
    meta = record.get("_meta") or {}
    return str(meta.get("canonical_id") or record.get("Property ID") or record.get("Unique ID") or "")


def _property_id_of(record: dict[str, Any]) -> str:
    meta = record.get("_meta") or {}
    return str(record.get("Property ID") or record.get("Unique ID") or meta.get("canonical_id") or "")


def _deterministic_pick(records: list[dict[str, Any]], n: int, seed: int) -> list[dict[str, Any]]:
    """Pick ``n`` records deterministically by sorting on ``sha256(canonical_id+seed)``.

    Using SHA256 (not built-in ``hash``) so the same seed picks the same
    records across Python processes, matching the established pattern at
    :func:`ma_poc.validation.identity_fallback.compute_fallback_id`.
    """
    if n <= 0 or not records:
        return []
    keyed = []
    for rec in records:
        cid = _canonical_id_of(rec) or _property_id_of(rec)
        digest = hashlib.sha256(f"{cid}|{seed}".encode("utf-8")).hexdigest()
        keyed.append((digest, rec))
    keyed.sort(key=lambda t: t[0])
    return [rec for _, rec in keyed[:n]]


def _to_samples(records: list[dict[str, Any]], bucket: str, run_dir: Path) -> list[Sample]:
    out: list[Sample] = []
    for rec in records:
        canonical = _canonical_id_of(rec)
        prop_id = _property_id_of(rec)
        raw_api, raw_html = _resolve_artifact_paths(run_dir, prop_id, canonical)
        out.append(
            Sample(
                canonical_id=canonical,
                property_id=prop_id,
                property_name=str(rec.get("Property Name") or ""),
                url=str(rec.get("Website") or rec.get("url") or ""),
                bucket=bucket,
                tier_used=_tier_used(rec),
                units_count=len(rec.get("units") or []),
                raw_api_path=str(raw_api) if raw_api else None,
                raw_html_path=str(raw_html) if raw_html else None,
            )
        )
    return out
