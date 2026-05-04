"""Phase 6 / 7 — amenities and concessions observation reports.

Both reports are observation-only (H7): no record is rejected for missing
either field, and downstream merge / validation never depends on them.
The reports are emitted at the end of every Jugnu run from each
property's result dict.

Output files (under ``data/runs/{date}/``):
    amenities_report.json
    concessions_report.json
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_TOP_AMENITIES = 50


def _normalize_amenity(s: str) -> str:
    return " ".join(s.strip().lower().split())


def aggregate_property_amenities(units: list[dict[str, Any]], explicit: list[str] | None = None) -> list[str]:
    """Union the unit-level amenity arrays into one deduplicated property list.

    ``explicit`` is the property-level list emitted by the LLM (when the
    template surfaces it). It's merged with the per-unit aggregate so
    callers get one canonical set.
    """
    seen: dict[str, None] = {}
    for u in units or []:
        amenities = u.get("amenities")
        if not isinstance(amenities, list):
            continue
        for raw in amenities:
            if not isinstance(raw, str):
                continue
            v = _normalize_amenity(raw)
            if v:
                seen.setdefault(v, None)
    if explicit:
        for raw in explicit:
            if isinstance(raw, str):
                v = _normalize_amenity(raw)
                if v:
                    seen.setdefault(v, None)
    return sorted(seen.keys())


def _attribute_extraction_tier(prop: dict[str, Any]) -> str:
    """Best-effort label for which tier produced the data on this property."""
    extract_result = prop.get("_extract_result") or {}
    if isinstance(extract_result, dict):
        return str(extract_result.get("tier_used") or prop.get("extraction_tier_used") or "unknown")
    return str(getattr(extract_result, "tier_used", None) or prop.get("extraction_tier_used") or "unknown")


def build_amenities_report(
    properties: list[dict[str, Any]],
    run_dir: Path,
    run_date: str,
) -> dict[str, Any]:
    """Build and write ``amenities_report.json`` for the run."""
    per_property: list[dict[str, Any]] = []
    properties_with = 0
    total_amenity_strings = 0
    occurrence_counter: Counter[str] = Counter()
    tier_breakdown: Counter[str] = Counter()

    for prop in properties:
        units = prop.get("units") or []
        explicit = prop.get("property_amenities") if isinstance(prop.get("property_amenities"), list) else None
        amenities = aggregate_property_amenities(units, explicit)
        if not amenities:
            continue
        properties_with += 1
        total_amenity_strings += len(amenities)
        for a in amenities:
            occurrence_counter[a] += 1
        tier = _attribute_extraction_tier(prop)
        tier_breakdown[tier] += 1
        per_property.append(
            {
                "property_id": prop.get("_property_id") or prop.get("property_id") or "",
                "amenities_count": len(amenities),
                "amenities": amenities,
                "extraction_tier": tier,
            }
        )

    report = {
        "run_date": run_date,
        "execution": "phase_6_amenities_observation",
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "properties_total": len(properties),
            "properties_with_amenities": properties_with,
            "properties_with_zero_amenities": len(properties) - properties_with,
            "amenities_total_count": total_amenity_strings,
            "unique_amenity_strings": len(occurrence_counter),
            "top_50_amenities": [
                {"name": name, "property_count": count}
                for name, count in occurrence_counter.most_common(_TOP_AMENITIES)
            ],
            "extraction_source_breakdown": dict(tier_breakdown),
        },
        "per_property": per_property,
    }

    out_path = run_dir / "amenities_report.json"
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    log.info("amenities_report written to %s", out_path)
    return report


# ── Concessions ──────────────────────────────────────────────────────────────


def _coerce_optional_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _first_concession_unit(units: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the first unit on a property that carries a concession_text."""
    for u in units or []:
        text = u.get("concession_text")
        if isinstance(text, str) and text.strip():
            return u
    return None


def build_concessions_report(
    properties: list[dict[str, Any]],
    run_dir: Path,
    run_date: str,
) -> dict[str, Any]:
    """Build and write ``concessions_report.json`` for the run."""
    per_property: list[dict[str, Any]] = []
    properties_with = 0
    source_breakdown: Counter[str] = Counter()
    phrase_counter: Counter[str] = Counter()
    with_value = 0
    text_only = 0

    for prop in properties:
        units = prop.get("units") or []
        winner = _first_concession_unit(units)
        if winner is None:
            continue
        properties_with += 1
        text = (winner.get("concession_text") or "").strip()
        value = _coerce_optional_float(winner.get("concession_value"))
        source = winner.get("concession_source") or "unspecified"
        source_breakdown[source] += 1
        if value is not None:
            with_value += 1
        else:
            text_only += 1
        # Use the lowercased first 80 chars as the phrase key so similar
        # promotions cluster ("1 month free", "$500 off first month") even
        # when capitalisation varies.
        phrase_key = text.lower()[:80]
        phrase_counter[phrase_key] += 1
        per_property.append(
            {
                "property_id": prop.get("_property_id") or prop.get("property_id") or "",
                "concession_text": text,
                "concession_value": value,
                "extraction_source": source,
                "extraction_tier": _attribute_extraction_tier(prop),
            }
        )

    report = {
        "run_date": run_date,
        "execution": "phase_7_concessions_observation",
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "properties_total": len(properties),
            "properties_with_any_concession": properties_with,
            "concession_extraction_breakdown": dict(source_breakdown),
            "value_distribution": {
                "with_numeric_value": with_value,
                "text_only_no_value": text_only,
            },
            "top_concession_phrases": [
                {"phrase": phrase, "count": count}
                for phrase, count in phrase_counter.most_common(20)
            ],
        },
        "per_property": per_property,
    }

    out_path = run_dir / "concessions_report.json"
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    log.info("concessions_report written to %s", out_path)
    return report
