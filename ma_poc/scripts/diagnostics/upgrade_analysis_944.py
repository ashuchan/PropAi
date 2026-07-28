"""Segment the reached-but-not-gold Canary residue into measurable levers.

Acceptance criteria (2026-07-28 upgrade analysis):
* Read the immutable 2026-07-27 Canary properties only and explicitly report
  any mismatch against the brief's declared 944-property cohort.
* Partition every observed plan-level or no-data property into one concrete,
  public-surface recovery mechanism before making any conversion-rate claim.
* Select a deterministic, unbiased sample of at least 25 properties per lever
  (or every property for smaller levers), suitable for resumable live probing.
* Persist only sanitized property worklists and phase-one summaries.  Never
  persist cookies, rendered pages, API response bodies, or a static date.

This module deliberately performs Phase 1 only.  It does not label a lever as
recoverable: Phase 2 must establish a real apartment anchor and its own numeric
rent in one public row before a conversion rate is computed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage  # type: ignore[import-untyped]

from ma_poc.scripts.diagnostics._public_url import normalize_public_url

_RUN_PREFIX = "runs/2026-07-27-full-0d54ca7"
_INVESTIGATION_PREFIX = "investigations/2026-07-28-upgrade-analysis-944"
_DECLARED_COUNTS = {"SUCCESS_PLAN_LEVEL": 656, "FAILED_NO_DATA": 288}
_SAMPLE_SIZE = 25
UNSETTLED_LEVER = "UNSETTLED_NOT_CLASSIFIABLE"


@dataclass(frozen=True, slots=True)
class CandidateProperty:
    """Sanitized evidence needed to assign one property to a Phase-2 lever."""

    apartment_id: str
    website: str
    property_name: str
    verdict: str
    detected_pms: str
    winning_tier: str
    publish_ceiling: str | None
    render_mode: str | None
    fetch_error: str | None
    unsettled_reason: str | None = None


def _text(value: object | None) -> str:
    """Return a trimmed string, converting absent values to an empty string."""
    return str(value or "").strip()


def candidate_from_property(property_row: dict[str, Any]) -> CandidateProperty:
    """Create a safe candidate record from one v2 Canary property object.

    A row whose identity or public route cannot be established is *not*
    dropped and does not abort the run: it is returned carrying an
    ``unsettled_reason`` so it stays visible and countable in the Phase-1
    report.  The seeded ``website`` column legitimately holds a scheme-less
    marketing host for part of the cohort, so it is normalized through the
    same rule the fetch path uses rather than being rejected.

    Args:
        property_row: One property from a Canary shard's ``properties.json``.

    Returns:
        The minimal metadata necessary for offline segmentation.
    """
    meta = property_row.get("_meta") or {}
    provenance = meta.get("provenance") or {}
    fetch = provenance.get("fetch") or {}
    raw_website = _text(property_row.get("website"))
    property_name = _text(property_row.get("proj_name"))
    apartment_id = _text(meta.get("canonical_id") or property_row.get("apartment_id"))
    website = normalize_public_url(raw_website)
    reasons: list[str] = []
    if not apartment_id:
        # Keep the row addressable instead of losing it: a stable surrogate key
        # derived from the row's own sanitized evidence.
        digest = hashlib.sha256(f"{raw_website}\x1f{property_name}".encode()).hexdigest()[:12]
        apartment_id = f"unidentified-{digest}"
        reasons.append("MISSING_CANONICAL_ID")
    if website is None:
        reasons.append(f"UNUSABLE_PUBLIC_WEBSITE:{raw_website or '<empty>'}")
    ceiling = meta.get("publish_ceiling") or {}
    return CandidateProperty(
        apartment_id=apartment_id,
        website=website or "",
        property_name=property_name,
        verdict=_text(meta.get("verdict")),
        detected_pms=_text(provenance.get("detected_pms") or "unknown").lower(),
        winning_tier=_text(provenance.get("winning_tier")),
        publish_ceiling=_text(ceiling.get("verdict")) or None,
        render_mode=_text(fetch.get("render_mode")) or None,
        fetch_error=_text(fetch.get("error_signature")) or None,
        unsettled_reason="; ".join(reasons) or None,
    )


def assign_lever(candidate: CandidateProperty) -> str:
    """Assign exactly one evidence-backed recovery mechanism to a candidate.

    The priority order keeps directly observed API failure modes separate from
    broad rendering and route-discovery work.  It is a *segmentation*, not a
    claim that the mechanism will convert the property.

    A candidate carrying an ``unsettled_reason`` has no usable public starting
    route or identity, so it gets its own bucket: it is reported, never
    silently folded into a mechanism it cannot be worked through.
    """
    if candidate.unsettled_reason:
        return UNSETTLED_LEVER
    tier = candidate.winning_tier.upper()
    pms = candidate.detected_pms
    ceiling = (candidate.publish_ceiling or "").upper()
    if "ENTRATA_NO_RESPONSE" in tier:
        return "ENTRATA_API_NO_RESPONSE_DOM_FALLBACK"
    if "ONESITE_NO_RESPONSE" in tier:
        return "ONESITE_API_NO_RESPONSE_RENDER_FALLBACK"
    if "RENTCAFE_SHAPE_REJECTED" in tier:
        return "RENTCAFE_API_SHAPE_REPAIR"
    if "ENTRATA_SHAPE_REJECTED" in tier:
        return "ENTRATA_API_SHAPE_REPAIR"
    if "SHAPE_REJECTED" in tier or "_EMPTY" in tier or "NO_RESPONSE" in tier:
        return "OTHER_PLATFORM_API_CONTRACT_REPAIR"
    if candidate.verdict == "FAILED_NO_DATA" and ceiling == "NEEDS_RENDER":
        return "SPA_RENDER_AND_PUBLIC_ROUTE"
    if candidate.verdict == "FAILED_NO_DATA" and ceiling == "EXTRACTION_MISS":
        return "EXTRACTION_MISS_PUBLIC_ROUTE"
    if candidate.verdict == "SUCCESS_PLAN_LEVEL" and pms in {
        "entrata",
        "rentcafe",
        "onesite",
        "appfolio",
        "rentmanager",
        "resman",
        "sightmap",
        "knock",
        "funnel",
        "realpage_cws",
        "onsite_apply",
    }:
        return "PMS_PUBLIC_AVAILABILITY_ROUTE"
    return "GENERIC_PUBLIC_ROUTE_DISCOVERY"


def deterministic_sample(
    candidates: Iterable[CandidateProperty], *, lever: str, sample_size: int = _SAMPLE_SIZE
) -> list[CandidateProperty]:
    """Return a stable pseudo-random sample without convenience ordering bias."""
    def sort_key(candidate: CandidateProperty) -> str:
        return hashlib.sha256(f"2026-07-28-upgrade-analysis-944:{lever}:{candidate.apartment_id}".encode()).hexdigest()

    return sorted(candidates, key=sort_key)[:sample_size]


def segment_candidates(candidates: Iterable[CandidateProperty]) -> dict[str, list[CandidateProperty]]:
    """Partition candidates into a deterministic mapping of lever to properties."""
    segments: dict[str, list[CandidateProperty]] = defaultdict(list)
    for candidate in candidates:
        segments[assign_lever(candidate)].append(candidate)
    return {lever: sorted(rows, key=lambda row: row.apartment_id) for lever, rows in sorted(segments.items())}


def phase_one_payload(candidates: Sequence[CandidateProperty]) -> dict[str, Any]:
    """Build the full machine-readable Phase-1 report and per-lever samples."""
    verdict_counts = Counter(candidate.verdict for candidate in candidates)
    observed_counts = {verdict: verdict_counts.get(verdict, 0) for verdict in _DECLARED_COUNTS}
    segments = segment_candidates(candidates)
    levers: list[dict[str, Any]] = []
    for lever, rows in segments.items():
        unsettled = lever == UNSETTLED_LEVER
        # Unsettled rows are listed in full: a sample would hide part of an
        # already-unexplained residue.
        sample = rows if unsettled else deterministic_sample(rows, lever=lever)
        levers.append(
            {
                "lever": lever,
                "properties_addressed": len(rows),
                "sample_n_planned": len(sample),
                "conversion_rate": None,
                "rate_status": "UNSETTLED" if unsettled else "UNMEASURED_PHASE_1",
                "sample": [asdict(item) for item in sample],
            }
        )
    # Rank by how much of the residue each mechanism addresses; the unsettled
    # bucket is never ranked as work, it sorts last regardless of size.
    levers.sort(key=lambda entry: (entry["lever"] == UNSETTLED_LEVER, -int(entry["properties_addressed"]), entry["lever"]))
    for rank, entry in enumerate(levers, start=1):
        entry["rank"] = None if entry["lever"] == UNSETTLED_LEVER else rank
    unsettled_rows = segments.get(UNSETTLED_LEVER, [])
    return {
        "workflow_version": "upgrade-analysis-944-phase1-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "source_run_prefix": _RUN_PREFIX,
        "declared_counts": _DECLARED_COUNTS,
        "observed_counts": observed_counts,
        "declared_total": sum(_DECLARED_COUNTS.values()),
        "observed_total": len(candidates),
        "cohort_matches_brief": observed_counts == _DECLARED_COUNTS,
        "cohort_mismatch_note": (
            None
            if observed_counts == _DECLARED_COUNTS
            else "Immutable shard verdicts differ from the brief; no properties were silently removed. "
            "Resolve the six-plan-level discrepancy before publishing a 944-only forecast."
        ),
        "unsettled_total": len(unsettled_rows),
        "unsettled": [
            {"apartment_id": row.apartment_id, "verdict": row.verdict, "reason": row.unsettled_reason}
            for row in unsettled_rows
        ],
        "levers": levers,
    }


def _properties_from_document(document: object) -> list[dict[str, Any]]:
    """Normalize both historical list and object shard layouts into properties."""
    if isinstance(document, list):
        return [row for row in document if isinstance(row, dict)]
    if isinstance(document, dict):
        rows = document.get("properties", [])
        return [row for row in rows if isinstance(row, dict)]
    raise ValueError("unsupported_properties_document")


def load_candidates_from_gcs(client: storage.Client, *, bucket_name: str, run_prefix: str = _RUN_PREFIX) -> list[CandidateProperty]:
    """Read only the immutable Canary shard objects and return target verdicts."""
    bucket = client.bucket(bucket_name)
    names = sorted(blob.name for blob in client.list_blobs(bucket, prefix=f"{run_prefix.rstrip('/')}/") if blob.name.endswith("/properties.json"))
    if len(names) != 100:
        raise ValueError(f"expected_100_shards_found:{len(names)}")

    def load(name: str) -> list[dict[str, Any]]:
        return _properties_from_document(json.loads(bucket.blob(name).download_as_bytes()))

    with ThreadPoolExecutor(max_workers=12) as executor:
        all_properties = [row for shard in executor.map(load, names) for row in shard]
    candidates = [candidate_from_property(row) for row in all_properties if _text((row.get("_meta") or {}).get("verdict")) in _DECLARED_COUNTS]
    return sorted(candidates, key=lambda candidate: candidate.apartment_id)


def publish_first_generation(client: storage.Client, *, bucket_name: str, object_name: str, payload: dict[str, Any]) -> None:
    """Write a Phase-1 artifact exactly once, protecting existing evidence."""
    try:
        client.bucket(bucket_name).blob(object_name).upload_from_string(
            json.dumps(payload, sort_keys=True, indent=2) + "\n",
            content_type="application/json",
            if_generation_match=0,
        )
    except PreconditionFailed as exc:
        raise RuntimeError(f"checkpoint_already_exists:gs://{bucket_name}/{object_name}") from exc


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the deliberately narrow Phase-1 command-line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default="jugnu-canary")
    parser.add_argument("--run-prefix", default=_RUN_PREFIX)
    parser.add_argument("--output-object", default=f"{_INVESTIGATION_PREFIX}/phase1-segmentation.json")
    parser.add_argument("--dry-run", action="store_true", help="Print summary without publishing a GCS checkpoint.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run offline segmentation and publish one guarded Phase-1 worklist."""
    args = parse_args(argv)
    client = storage.Client()
    candidates = load_candidates_from_gcs(client, bucket_name=args.bucket, run_prefix=args.run_prefix)
    payload = phase_one_payload(candidates)
    if not args.dry_run:
        publish_first_generation(client, bucket_name=args.bucket, object_name=args.output_object, payload=payload)
    summary_keys = ("observed_counts", "observed_total", "cohort_matches_brief", "unsettled_total", "unsettled", "levers")
    print(json.dumps({key: payload[key] for key in summary_keys}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI
    raise SystemExit(main())
