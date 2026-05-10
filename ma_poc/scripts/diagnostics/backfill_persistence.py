#!/usr/bin/env python3
"""PR 4 — One-time backfill of saved mappings/patches from cloud-run artifacts.

After PR 1 + PR 2 deploy, the persistence layer correctly accepts mappings
and patches the LLM produced. But the prior day's LLM extractions were
silently dropped by the now-fixed bugs and aren't in the DB. Without
backfill the cache builds organically over 7+ days; with backfill the
cache is hot tomorrow.

This script:
  1. Walks ``c:/tmp/run-{date}/shard_*/{llm_report,llm_diagnostics}/`` (the
     mirrored cloud-run artifacts).
  2. For each per-property LLM report, reconstructs the mapping_dict the
     fixed ``save_llm_field_mapping`` would have written.
  3. For each per-property field-recovery diagnostic, reconstructs the
     patch_dict the fixed ``save_field_patch`` would have written.
  4. Replays both through the live writers (idempotent — re-runnable).

DRY-RUN by default. Pass ``--apply`` to actually write to the DB.

See ``ma_poc/docs/backfill_persistence_design.md`` for the full design rationale.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# parents[2] = ma_poc/ (lets `from services.X` / `from models.X` resolve)
# parents[3] = PropAi/ (lets `from ma_poc.X` resolve)
_MA_POC_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LOCAL_MIRROR = Path("c:/tmp")
DEFAULT_OUT_ROOT = _MA_POC_ROOT / "data" / "reports"

# Conservative confidence floor for replaying a recovered field as a
# FieldPatch. Matches the producer's threshold in
# ``scripts/runners/jugnu.py::_run_null_field_recovery``.
PATCH_CONFIDENCE_FLOOR = 0.85

# field_name values FieldPatch's Pydantic Literal accepts. Mirror of
# ``models/scrape_profile.py::FieldPatch.field_name`` AND
# ``scripts/runners/jugnu.py::_PATCH_FIELDS``.
_VALID_PATCH_FIELDS = frozenset({
    "rent_low", "rent_high", "asking_rent",
    "market_rent_low", "market_rent_high",
    "unit_id", "unit_number", "floor_plan_name",
    "beds", "bedrooms", "baths", "bathrooms", "sqft",
    "available_date", "availability_date",
})

log = logging.getLogger("backfill_persistence")


def _ensure_imports_resolve() -> None:
    """Add ma_poc/ and PropAi/ to sys.path so deferred imports resolve.

    Idempotent. Mirrors the boot pattern in ``scripts/runners/jugnu.py`` so
    a backfill script can be invoked from any CWD.
    """
    for p in (_PROJECT_ROOT, _MA_POC_ROOT):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)


# ── Stats ──────────────────────────────────────────────────────────────────


@dataclass
class BackfillStats:
    run_date: str
    shards_seen: list[str] = field(default_factory=list)

    properties_visited: int = 0
    profiles_loaded: int = 0
    profiles_missing: int = 0  # pid in artifacts but no profile in store

    mapping_files_seen: int = 0
    mapping_files_malformed: int = 0
    mappings_attempted: int = 0
    mappings_persisted: int = 0
    mapping_drop_reasons: Counter[str] = field(default_factory=Counter)

    patch_files_seen: int = 0
    patch_files_malformed: int = 0
    patches_attempted: int = 0
    patches_persisted: int = 0
    patch_drop_reasons: Counter[str] = field(default_factory=Counter)

    profile_save_errors: int = 0


# ── URL extraction from prompt text ────────────────────────────────────────


# Producer-side template at config/prompts/api_analysis.txt embeds the URL
# as a labelled line. If the template changes, this regex needs to follow.
_API_URL_LINE_RE = re.compile(r"^\s*API URL:\s*(\S+)\s*$", re.MULTILINE | re.IGNORECASE)


def extract_api_url_from_prompt(prompt: str) -> str | None:
    """Return the api_url referenced in an API_ANALYSIS LLM prompt, or None."""
    if not prompt:
        return None
    m = _API_URL_LINE_RE.search(prompt)
    if not m:
        return None
    return m.group(1).strip()


# ── Mapping reconstruction (Channel 1) ─────────────────────────────────────


def reconstruct_mapping_dicts(llm_report_json: Any) -> list[dict[str, Any]]:
    """Walk an `llm_report/{pid}.json` payload and yield mapping_dicts.

    Yields one mapping_dict per ``API_ANALYSIS`` interaction whose
    ``raw_response`` parses cleanly and contains ``json_paths`` or
    ``response_envelope`` (the writer's degraded-mapping path persists
    envelope-only mappings too — PR 1).

    Returns ``[]`` for malformed inputs (including non-dict top-level JSON).
    """
    out: list[dict[str, Any]] = []
    if not isinstance(llm_report_json, dict):
        return out
    interactions = llm_report_json.get("interactions") or []
    if not isinstance(interactions, list):
        return out
    for interaction in interactions:
        if not isinstance(interaction, dict):
            continue
        if interaction.get("tier") != "API_ANALYSIS":
            continue
        if not interaction.get("success"):
            continue
        raw = interaction.get("raw_response") or ""
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        # Skip noise verdicts — those are handled by the noise blocklist
        # writer, not the mapping writer.
        if parsed.get("has_unit_data") is False:
            continue

        api_url = extract_api_url_from_prompt(interaction.get("user_prompt", ""))
        if not api_url:
            continue

        json_paths = parsed.get("json_paths") or {}
        response_envelope = parsed.get("response_envelope") or ""
        if not isinstance(json_paths, dict):
            json_paths = {}
        if not isinstance(response_envelope, str):
            response_envelope = ""

        # The LLM occasionally emits ``"some_field": null`` for fields it
        # couldn't map. Pydantic ``LlmFieldMapping.json_paths: dict[str,str]``
        # rejects None values, so drop them here rather than letting the
        # whole mapping fail validation downstream.
        json_paths = {
            k: v for k, v in json_paths.items()
            if isinstance(k, str) and isinstance(v, str) and v.strip()
        }

        # Skip when both are empty — writer drops these as
        # ``empty_paths_and_envelope`` per PR 1.
        if not json_paths and not response_envelope:
            continue

        out.append({
            "api_url_pattern": api_url,
            "json_paths": json_paths,
            "response_envelope": response_envelope,
        })
    return out


# ── Patch reconstruction (Channel 4) ───────────────────────────────────────


def reconstruct_patch_dicts(field_recovery_json: Any) -> list[dict[str, Any]]:
    """Walk a ``llm_diagnostics/{pid}_field_recovery.json`` payload and yield patch_dicts.

    Channel 4 backfill is best-effort. The cloud-run artifact does NOT preserve
    the api_url that the recovery applied to — the null_field_recovery prompt
    template (config/prompts/null_field_recovery.txt) uses SOURCE_FRAGMENT, not
    "API URL: <url>", and the ``_llm_interaction`` block stores cost-accounting
    fields only (no prompt body). So this function only emits a patch_dict when
    the user_prompt happens to contain an ``API URL:`` line — which it won't
    for current cloud-run runs but might for future producers that add it.

    Tolerant of malformed input — returns ``[]`` rather than raising.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(field_recovery_json, list):
        return out
    for item in field_recovery_json:
        if not isinstance(item, dict):
            continue
        recovered = item.get("recovered_fields") or []
        if not isinstance(recovered, list):
            continue

        interaction = item.get("_llm_interaction") or {}
        prompt = interaction.get("user_prompt", "") if isinstance(interaction, dict) else ""
        api_url = extract_api_url_from_prompt(prompt)
        if not api_url:
            continue

        # The recovered_fields all came from the SAME source body in the
        # producer (see _run_null_field_recovery), so they share api_url.
        for rf in recovered:
            if not isinstance(rf, dict):
                continue
            field_name = rf.get("field_name")
            if not isinstance(field_name, str) or field_name not in _VALID_PATCH_FIELDS:
                continue
            confidence = rf.get("confidence", 0.0)
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                continue
            if confidence < PATCH_CONFIDENCE_FLOOR:
                continue
            source_path = rf.get("source_path") or ""
            if not isinstance(source_path, str) or not source_path.strip():
                continue
            if source_path.strip().lower() == "not_present":
                continue

            out.append({
                "api_url_pattern": api_url,
                "field_name": field_name,
                "json_path": source_path,
                "confidence": confidence,
                "parser_fix": rf.get("parser_fix"),
                # No envelope hash recoverable from artifacts — leave blank.
            })
    return out


# ── Backfill orchestration ─────────────────────────────────────────────────


def _shard_dirs(run_dir: Path) -> list[Path]:
    return sorted(
        [p for p in run_dir.iterdir() if p.is_dir() and p.name.startswith("shard_")],
        key=lambda p: int(p.name.split("_", 1)[1]) if p.name.split("_", 1)[1].isdigit() else -1,
    )


def _load_json_or_none(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("backfill: failed to read %s — %s", path.name, exc)
        return None


def _build_profile_store(apply_mode: bool) -> Any:
    """Return a profile store for backfill use.

    Apply mode wires the production data-provider (Postgres if configured).
    Dry-run mode wires an in-memory NullStore that records calls without
    persisting — keeps dry-run truly read-only against the DB.
    """
    if not apply_mode:
        return _NullStore()
    _ensure_imports_resolve()
    from data_provider.factory import get_data_provider
    return get_data_provider().profiles


@dataclass
class _NullStore:
    """Dry-run profile store. Tracks calls in counters without writing.

    Returns a freshly-bootstrapped ScrapeProfile for any get(), so the
    save_*_field writers see a non-None object and can simulate a real
    upsert. Calls to put() are no-ops (records nothing — dry-run is ABOUT
    counting what WOULD save).
    """
    bootstraps: int = 0
    puts: int = 0

    def get(self, canonical_id: str) -> Any:
        _ensure_imports_resolve()
        from models.scrape_profile import ScrapeProfile
        self.bootstraps += 1
        return ScrapeProfile(canonical_id=canonical_id)

    def put(self, profile: Any) -> None:
        self.puts += 1

    def list_ids(self) -> list[str]:
        return []

    def delete(self, canonical_id: str) -> bool:
        return False


def backfill_one_property(
    pid: str,
    mapping_dicts: list[dict[str, Any]],
    patch_dicts: list[dict[str, Any]],
    store: Any,
    stats: BackfillStats,
) -> None:
    """Apply backfill for a single property. Best-effort per dict."""
    _ensure_imports_resolve()
    from services.profile_updater import save_field_patch, save_llm_field_mapping

    profile = store.get(pid)
    if profile is None:
        stats.profiles_missing += 1
        return
    stats.profiles_loaded += 1

    for m in mapping_dicts:
        stats.mappings_attempted += 1
        try:
            ok = save_llm_field_mapping(profile, m)
        except Exception as exc:
            log.warning("save_llm_field_mapping raised for pid=%s: %s", pid, exc)
            stats.mapping_drop_reasons["exception"] += 1
            continue
        if ok:
            stats.mappings_persisted += 1
        else:
            stats.mapping_drop_reasons["writer_returned_false"] += 1

    for p in patch_dicts:
        stats.patches_attempted += 1
        try:
            ok = save_field_patch(profile, p)
        except Exception as exc:
            log.warning("save_field_patch raised for pid=%s: %s", pid, exc)
            stats.patch_drop_reasons["exception"] += 1
            continue
        if ok:
            stats.patches_persisted += 1
        else:
            stats.patch_drop_reasons["writer_returned_false"] += 1

    try:
        store.put(profile)
    except Exception as exc:
        log.warning("profile put failed for pid=%s: %s", pid, exc)
        stats.profile_save_errors += 1


def run_backfill(
    run_date: str,
    local_mirror_root: Path = DEFAULT_LOCAL_MIRROR,
    shard_filter: int | None = None,
    apply_mode: bool = False,
) -> BackfillStats:
    """Walk per-shard artifacts and backfill the persistence layer.

    Returns the populated ``BackfillStats``. Always best-effort — does
    not raise on per-property failures (those count against
    ``profile_save_errors`` / ``*_drop_reasons``).
    """
    run_dir = local_mirror_root / f"run-{run_date}"
    if not run_dir.exists():
        raise FileNotFoundError(f"Run directory not found: {run_dir}")

    stats = BackfillStats(run_date=run_date)
    store = _build_profile_store(apply_mode=apply_mode)

    shards = _shard_dirs(run_dir)
    if shard_filter is not None:
        shards = [s for s in shards if s.name == f"shard_{shard_filter}"]
        if not shards:
            raise ValueError(f"shard_{shard_filter} not found under {run_dir}")
    stats.shards_seen = [s.name for s in shards]

    for shard in shards:
        log.info("Backfilling %s …", shard.name)
        # Walk llm_report/{pid}.json
        llm_report_dir = shard / "llm_report"
        per_pid_mappings: dict[str, list[dict[str, Any]]] = {}
        if llm_report_dir.is_dir():
            for f in sorted(llm_report_dir.glob("*.json")):
                stats.mapping_files_seen += 1
                payload = _load_json_or_none(f)
                if payload is None:
                    stats.mapping_files_malformed += 1
                    continue
                pid = f.stem  # filename is "{pid}.json"
                per_pid_mappings[pid] = reconstruct_mapping_dicts(payload)

        # Walk llm_diagnostics/{pid}_field_recovery.json
        diag_dir = shard / "llm_diagnostics"
        per_pid_patches: dict[str, list[dict[str, Any]]] = {}
        if diag_dir.is_dir():
            for f in sorted(diag_dir.glob("*_field_recovery.json")):
                stats.patch_files_seen += 1
                payload = _load_json_or_none(f)
                if payload is None:
                    stats.patch_files_malformed += 1
                    continue
                # Filename is "{pid}_field_recovery.json"
                pid = f.stem.removesuffix("_field_recovery")
                per_pid_patches[pid] = reconstruct_patch_dicts(payload)

        # Apply per property (union of pids that produced mappings or patches)
        all_pids = set(per_pid_mappings) | set(per_pid_patches)
        for pid in sorted(all_pids):
            stats.properties_visited += 1
            backfill_one_property(
                pid=pid,
                mapping_dicts=per_pid_mappings.get(pid, []),
                patch_dicts=per_pid_patches.get(pid, []),
                store=store,
                stats=stats,
            )

    return stats


# ── Report rendering ───────────────────────────────────────────────────────


def render_report_md(stats: BackfillStats, apply_mode: bool) -> str:
    banner = "" if apply_mode else "**[DRY RUN]** No DB writes performed. Re-run with `--apply` to persist.\n\n"
    lines = [
        f"# Persistence backfill — {stats.run_date}",
        "",
        f"**Generated:** {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"**Mode:** {'APPLY (writes to DB)' if apply_mode else 'DRY RUN'}",
        f"**Shards processed:** {len(stats.shards_seen)} ({', '.join(stats.shards_seen[:5])}{' …' if len(stats.shards_seen) > 5 else ''})",
        "",
        banner.rstrip(),
        "## Summary",
        "",
        "| Metric | Count |",
        "|---|---|",
        f"| Properties visited | {stats.properties_visited} |",
        f"| Profiles loaded successfully | {stats.profiles_loaded} |",
        f"| Profiles missing (pid in artifacts, not in store) | {stats.profiles_missing} |",
        f"| Profile save errors | {stats.profile_save_errors} |",
        "",
        "### Channel 1 — llm_field_mappings",
        "",
        "| Metric | Count |",
        "|---|---|",
        f"| llm_report files seen | {stats.mapping_files_seen} |",
        f"| llm_report files malformed | {stats.mapping_files_malformed} |",
        f"| Mappings attempted | {stats.mappings_attempted} |",
        f"| Mappings persisted (or would-have, dry-run) | **{stats.mappings_persisted}** |",
        "",
    ]
    if stats.mapping_drop_reasons:
        lines += [
            "Mapping drop reasons:",
            "",
            "| Reason | Count |",
            "|---|---|",
        ]
        for reason, n in stats.mapping_drop_reasons.most_common():
            lines.append(f"| `{reason}` | {n} |")
        lines.append("")

    lines += [
        "### Channel 4 — field_patches",
        "",
        "| Metric | Count |",
        "|---|---|",
        f"| field_recovery files seen | {stats.patch_files_seen} |",
        f"| field_recovery files malformed | {stats.patch_files_malformed} |",
        f"| Patches attempted | {stats.patches_attempted} |",
        f"| Patches persisted (or would-have, dry-run) | **{stats.patches_persisted}** |",
        "",
    ]
    if stats.patch_drop_reasons:
        lines += [
            "Patch drop reasons:",
            "",
            "| Reason | Count |",
            "|---|---|",
        ]
        for reason, n in stats.patch_drop_reasons.most_common():
            lines.append(f"| `{reason}` | {n} |")
        lines.append("")

    return "\n".join(lines) + "\n"


# ── CLI ────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-date", required=True, help="Run date YYYY-MM-DD")
    p.add_argument(
        "--local-mirror",
        default=str(DEFAULT_LOCAL_MIRROR),
        help=f"Where per-date mirrors live (default {DEFAULT_LOCAL_MIRROR})",
    )
    p.add_argument(
        "--shard", type=int, default=None,
        help="Limit backfill to a single shard index (testing).",
    )
    p.add_argument(
        "--apply", action="store_true",
        help="Actually write to the DB. Default is DRY RUN.",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="Per-property progress logging.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="[%(levelname)s] %(message)s",
    )

    try:
        stats = run_backfill(
            run_date=args.run_date,
            local_mirror_root=Path(args.local_mirror),
            shard_filter=args.shard,
            apply_mode=args.apply,
        )
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    out_dir = DEFAULT_OUT_ROOT
    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = f"backfill_{args.run_date}{'_DRYRUN' if not args.apply else ''}.md"
    out_path = out_dir / out_name
    out_path.write_text(render_report_md(stats, apply_mode=args.apply), encoding="utf-8")

    print(
        f"[{'apply' if args.apply else 'dryrun'}] {args.run_date}: "
        f"mappings_persisted={stats.mappings_persisted} "
        f"patches_persisted={stats.patches_persisted} "
        f"profiles_loaded={stats.profiles_loaded}"
    )
    print(f"[ok] report written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
