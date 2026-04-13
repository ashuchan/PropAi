"""
Role C — Accuracy sample. Rotating 5–10% of daily successes.

Acceptance criteria (CLAUDE.md PR-04 / Role C):
- Deterministic selection via hashlib.sha256((property_id + scrape_date).encode())
  — NEVER built-in hash() (bug-hunt #15)
- Field-by-field diff vs primary output, written to {date}_vision_comparison.json
- Must NOT modify the primary extraction result (isolation)
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

from llm.factory import get_vision_provider
from models.extraction_result import ExtractionResult


def _sample_rate() -> float:
    try:
        return float(os.getenv("VISION_SAMPLE_RATE", "0.075"))
    except ValueError:
        return 0.075


def select_for_sample(property_id: str, scrape_date: date) -> bool:
    """Bug-hunt #15: deterministic across processes via SHA-256."""
    rate = _sample_rate()
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    digest = hashlib.sha256(f"{property_id}|{scrape_date.isoformat()}".encode()).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return bucket < rate


def _diff_units(primary: list[dict[str, Any]], vision: list[dict[str, Any]]) -> dict[str, Any]:
    """Field-by-field comparison keyed by unit_number."""
    by_id_p = {str(u.get("unit_number")): u for u in primary if u.get("unit_number")}
    by_id_v = {str(u.get("unit_number")): u for u in vision if u.get("unit_number")}
    fields = ("asking_rent", "availability_status", "sqft", "floor_plan_type")
    rows: list[dict[str, Any]] = []
    common = sorted(set(by_id_p) & set(by_id_v))
    agreements = 0
    comparisons = 0
    for unit_id in common:
        for f in fields:
            comparisons += 1
            p = by_id_p[unit_id].get(f)
            v = by_id_v[unit_id].get(f)
            agree = p == v
            if agree:
                agreements += 1
            rows.append({"unit_number": unit_id, "field": f, "primary": p, "vision": v, "agree": agree})
    only_in_p = sorted(set(by_id_p) - set(by_id_v))
    only_in_v = sorted(set(by_id_v) - set(by_id_p))
    return {
        "agreement_rate": (agreements / comparisons) if comparisons else None,
        "comparisons": comparisons,
        "agreements": agreements,
        "only_in_primary": only_in_p,
        "only_in_vision": only_in_v,
        "diffs": rows,
    }


async def write_sample_comparison(
    output_dir: Path | str,
    property_id: str,
    scrape_date: date,
    primary: ExtractionResult,
    screenshot_path: Path | str | None = None,
) -> Path:
    """
    Run vision against the same property and write the comparison JSON.
    Isolated from the primary result — never mutates it.

    Uses the saved screenshot on disk (cheap — no extra browser session).
    Falls back to data/screenshots/{property_id}/{date}.png if no explicit
    screenshot_path is provided.
    """
    out_dir = Path(output_dir) / property_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{scrape_date.isoformat()}_vision_comparison.json"

    primary_units = [u for u in (primary.raw_fields.get("units") or []) if isinstance(u, dict)]
    comparison: dict[str, Any] = {
        "property_id": property_id,
        "scrape_date": scrape_date.isoformat(),
        "primary_tier": int(primary.tier) if primary.tier else None,
        "primary_units": len(primary_units),
        "agreement_rate": None,
        "comparisons": 0,
        "agreements": 0,
        "diffs": [],
        "error": None,
    }

    try:
        provider = get_vision_provider()
    except Exception as exc:
        comparison["error"] = f"vision_provider_unavailable: {exc}"
        out_path.write_text(json.dumps(comparison, indent=2), encoding="utf-8")
        return out_path

    # Locate the screenshot: explicit path > convention path
    img_path: Path | None = None
    if screenshot_path is not None:
        img_path = Path(screenshot_path)
    else:
        # Convention: data/screenshots/{property_id}/{date}.png
        base = Path(output_dir).parent / "screenshots" / property_id
        candidate = base / f"{scrape_date.isoformat()}.png"
        if candidate.exists():
            img_path = candidate

    if img_path is None or not img_path.exists():
        comparison["error"] = "no_screenshot_available"
        out_path.write_text(json.dumps(comparison, indent=2), encoding="utf-8")
        return out_path

    try:
        image_bytes = img_path.read_bytes()
        vision_prompt = (
            "You see a screenshot of an apartment property's pricing/availability page. "
            'Return a JSON object: {"units": [{"unit_number": str, "floor_plan_type": str, '
            '"asking_rent": number, "availability_status": "AVAILABLE"|"UNAVAILABLE"|"UNKNOWN", '
            '"sqft": int|null}]}. Return ONLY the JSON object.'
        )
        payload = await provider.extract_from_images([image_bytes], vision_prompt)
    except Exception as exc:
        comparison["error"] = f"vision_call_failed: {exc}"
        out_path.write_text(json.dumps(comparison, indent=2), encoding="utf-8")
        return out_path

    vision_units = [u for u in payload.get("units", []) if isinstance(u, dict)]
    diff = _diff_units(primary_units, vision_units)
    comparison.update({
        "vision_units": len(vision_units),
        "agreement_rate": diff["agreement_rate"],
        "comparisons": diff["comparisons"],
        "agreements": diff["agreements"],
        "only_in_primary": diff["only_in_primary"],
        "only_in_vision": diff["only_in_vision"],
        "diffs": diff["diffs"],
    })
    out_path.write_text(json.dumps(comparison, indent=2), encoding="utf-8")
    return out_path


__all__ = ["select_for_sample", "write_sample_comparison", "_diff_units"]
