"""Carry-forward safety net — re-emit previous data on fetch failure.

Root cause of the 04-17 zero-carry-forward bug:
The old daily_runner.py only called carry-forward when the scraper raised
an exception. But entrata.py catches all exceptions internally and returns
a result with extraction_tier_used="FAILED". This module triggers on ANY
failure outcome (FAILED tier, empty units, or fetch hard-fail), not only
on exceptions.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..observability.events import EventKind, emit

log = logging.getLogger(__name__)

_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def carry_forward_property(
    property_id: str,
    today_run_dir: Path,
    state_store: Any,
    reason: str,
) -> dict[str, Any] | None:
    """Re-emit the previous successful record for a property.

    Args:
        property_id: The canonical property ID.
        today_run_dir: Path to today's run output directory. Used to derive
            the runs-root for the file-walker fallback: ``today_run_dir``
            is ``{schema_root}/runs/{date}`` so ``.parent.parent`` is the
            schema root (``data/`` or ``data/v2/``).
        state_store: The state store (has get_last_known_property method).
        reason: Why carry-forward was triggered.

    Returns:
        The carried record dict, or None if no prior record exists.
    """
    prior = _load_prior_record(property_id, state_store)
    source_date: str | None = None
    if prior is None:
        # StateStore didn't yield a full record. Fall back to walking
        # ``runs/*/properties.json`` newest-first across both schema roots
        # so a v2 run can recover from a prior v1 (or v2) run seamlessly.
        search_roots: list[Path] = []
        try:
            schema_root = today_run_dir.parent.parent  # {schema_root}
            data_root = schema_root.parent if schema_root.name in ("v1", "v2") else schema_root
            # Try the current schema root first, then the other, then base.
            seen: set[Path] = set()
            for candidate in (schema_root, data_root / "v2", data_root / "v1", data_root):
                try:
                    resolved = candidate.resolve()
                except Exception:
                    resolved = candidate
                if resolved in seen:
                    continue
                seen.add(resolved)
                search_roots.append(candidate)
        except Exception:
            search_roots = []
        if search_roots:
            prior, source_date = _load_prior_record_from_runs(
                property_id,
                search_roots,
            )
    if prior is None:
        log.info("No prior record for %s, cannot carry forward", property_id)
        return None

    # Tag the record as carried forward
    meta = prior.get("_meta", {}) or {}
    meta["scrape_outcome"] = "CARRY_FORWARD"
    meta["carry_forward_reason"] = reason
    meta["carry_forward_at"] = datetime.now(UTC).isoformat()
    meta["carry_forward_used"] = True
    if source_date:
        meta["carry_forward_source_date"] = source_date
    prior["_meta"] = meta

    emit(
        EventKind.CARRY_FORWARD_APPLIED,
        property_id,
        reason=reason,
        source_date=source_date,
    )

    log.info("Carried forward property %s: %s", property_id, reason)
    return prior


def should_carry_forward(
    scrape_result: dict[str, Any] | None,
    fetch_outcome: str | None = None,
) -> tuple[bool, str]:
    """Determine if carry-forward should fire for a property.

    Args:
        scrape_result: The scrape result dict, or None if fetch failed.
        fetch_outcome: The FetchOutcome value string, if available.

    Returns:
        Tuple of (should_carry_forward, reason).
    """
    if fetch_outcome in ("HARD_FAIL", "BOT_BLOCKED", "PROXY_ERROR"):
        return True, f"fetch_{fetch_outcome}"

    # NOT_MODIFIED (304) means the conditional-GET cache confirmed the
    # server hasn't changed the resource since last fetch. In that case
    # the *prior* extraction is still correct — carry it forward rather
    # than returning an empty record.
    if fetch_outcome == "NOT_MODIFIED":
        return True, "fetch_NOT_MODIFIED"

    # TRANSIENT after retries exhausted is also a carry-forward candidate —
    # we have no fresh data but should reuse prior rather than emit empty.
    if fetch_outcome == "TRANSIENT":
        return True, "fetch_TRANSIENT"

    if scrape_result is None:
        return True, "no_scrape_result"

    meta = scrape_result.get("_meta", {})
    tier = meta.get("scrape_tier_used", "")

    if "FAIL" in str(tier).upper():
        return True, "extraction_failed"

    units = scrape_result.get("units", [])
    if not units and tier:
        return True, "empty_units"

    return False, ""


def _load_prior_record(property_id: str, state_store: Any) -> dict[str, Any] | None:
    """Load the most recent successful record from state.

    Args:
        property_id: The canonical property ID.
        state_store: State store with property index.

    Returns:
        The prior property record dict, or None.
    """
    try:
        if hasattr(state_store, "get_last_known_property"):
            return state_store.get_last_known_property(property_id)
        if hasattr(state_store, "property_index"):
            idx = state_store.property_index
            if property_id in idx:
                entry = idx[property_id]
                if isinstance(entry, dict) and "last_record" in entry:
                    return entry["last_record"]
    except Exception as exc:
        log.warning("Failed to load prior record for %s: %s", property_id, exc)
    return None


def _load_prior_record_from_runs(
    property_id: str,
    search_roots: list[Path],
) -> tuple[dict[str, Any] | None, str | None]:
    """Walk ``{root}/runs/{date}/properties.json`` newest-first for a record.

    Fallback path for Jugnu where the ``StateStore`` isn't populated with
    full per-property records — the authoritative store is
    ``properties.json`` under each run directory. We scan v1 and v2 roots
    so this works regardless of which schema wrote the last successful run.

    Returns (record, source_date) — record is the raw property dict from
    the prior run's ``properties.json`` (with its ``units`` intact) and
    ``source_date`` is the run-date string the record was read from.
    """
    for root in search_roots:
        runs_dir = root / "runs"
        if not runs_dir.exists():
            continue
        try:
            date_dirs = sorted(
                (p for p in runs_dir.iterdir() if p.is_dir() and _DATE_DIR_RE.match(p.name)),
                key=lambda p: p.name,
                reverse=True,
            )
        except Exception:
            continue
        for d in date_dirs:
            props_file = d / "properties.json"
            if not props_file.exists():
                continue
            try:
                with props_file.open(encoding="utf-8") as f:
                    props = json.load(f)
            except Exception:
                continue
            if not isinstance(props, list):
                continue
            for p in props:
                if not isinstance(p, dict):
                    continue
                meta = p.get("_meta") if isinstance(p.get("_meta"), dict) else {}
                cid = meta.get("canonical_id")
                if cid is None:
                    # Fall back to v1's top-level identity keys
                    cid = (
                        p.get("Unique ID")
                        or p.get("Property ID")
                        or (str(p.get("apartment_id")) if p.get("apartment_id") is not None else None)
                    )
                if str(cid) != str(property_id):
                    continue
                # Only accept records that actually have units — a
                # previous carry-forward or FAILED record is not useful.
                if p.get("units"):
                    return p, d.name
                # Keep scanning older dates on this root before switching
                # roots: an empty record today doesn't mean all past
                # records are empty.
    return None, None
