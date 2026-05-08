"""Deploy-time validation for the floor-plan catalog and prompt assets.

Runs as a Dockerfile build step **after** ``verify_csv_mapping.py`` has
produced ``config/csv_floorplan_mapping.json``. Fails the image build
loudly if any of the following preconditions for Cloud Run go wrong:

    1. ``csv_floorplan_mapping.json`` exists with a non-null ``chosen_field``.
       (The verifier itself is the single source of truth for this; we
       re-read the artifact from disk so a half-finished verifier run
       still trips the gate.)
    2. ``FloorplanCatalog`` loads without entering bypass mode and has
       at least ``MIN_INDEX_PROPERTIES`` properties in its index.
    3. All four prompt templates required by the LLM extractor and
       rescue paths are present on disk and contain the
       ``known_floor_plans`` placeholder (single-brace for tier4/api/dom,
       double-brace for rescue).
    4. The handful of modules whose import paths were touched in this PR
       can be imported under the runtime Python environment baked into
       the image.

Exit codes: 0 on success, 1 on any failure. The Dockerfile runs this with
``RUN python -m ma_poc.scripts.validate_deployment`` so a regression in
any of the above is caught at build time, not at the first scrape.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# Coverage floor below which we treat the catalog as unusable. The CSV has
# ~5 K properties; if fewer than 1 000 land in the index something is very
# wrong (truncated CSV, encoding mismatch, mapping key drift).
MIN_INDEX_PROPERTIES = 1_000

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
_MAPPING_PATH = _CONFIG_DIR / "csv_floorplan_mapping.json"
_PROMPT_DIR = _CONFIG_DIR / "prompts"
_REQUIRED_PROMPTS = (
    "tier4_extraction.txt",
    "api_analysis.txt",
    "dom_analysis.txt",
    "api_rescue.txt",
)


def _check_mapping(failures: list[str]) -> None:
    if not _MAPPING_PATH.exists():
        failures.append(f"mapping file missing: {_MAPPING_PATH}")
        return
    try:
        payload = json.loads(_MAPPING_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        failures.append(f"mapping file unreadable JSON: {exc}")
        return
    if not payload.get("chosen_field"):
        failures.append(
            f"chosen_field is null in {_MAPPING_PATH.name}; the verifier "
            "could not pick a join column above the coverage floor — "
            "re-run verify_csv_mapping.py and inspect candidates[]"
        )


def _check_catalog(failures: list[str]) -> None:
    try:
        from ma_poc.services.floorplan_catalog import (
            get_default_catalog,
            reset_default_catalog,
        )
    except Exception as exc:  # noqa: BLE001
        failures.append(f"FloorplanCatalog import failed: {exc}")
        return
    reset_default_catalog()
    try:
        cat = get_default_catalog()
    except Exception as exc:  # noqa: BLE001
        failures.append(f"FloorplanCatalog load raised: {exc}")
        return
    if cat.in_bypass_mode:
        failures.append(
            "FloorplanCatalog loaded in bypass mode — `chosen_field` was "
            "null or the floor-plan CSV is missing/unreadable. Snap step "
            "would silently no-op in production."
        )
        return
    indexed = len(cat._by_property)  # noqa: SLF001 — diagnostic read
    if indexed < MIN_INDEX_PROPERTIES:
        failures.append(
            f"FloorplanCatalog indexed only {indexed} properties; "
            f"expected ≥ {MIN_INDEX_PROPERTIES}. CSV likely truncated or "
            "encoding-broken in the build context."
        )


def _check_prompts(failures: list[str]) -> None:
    for name in _REQUIRED_PROMPTS:
        path = _PROMPT_DIR / name
        if not path.exists():
            failures.append(f"prompt missing: {name}")
            continue
        text = path.read_text(encoding="utf-8")
        # Rescue prompt uses {{double}} placeholders; the other three use
        # {single}. Catch a missed conversion either way.
        if name == "api_rescue.txt":
            if "{{known_floor_plans}}" not in text:
                failures.append(
                    "api_rescue.txt missing {{known_floor_plans}} placeholder"
                )
        else:
            if "{known_floor_plans}" not in text:
                failures.append(
                    f"{name} missing {{known_floor_plans}} placeholder"
                )


def _check_imports(failures: list[str]) -> None:
    """Belt-and-braces check that the modules wired in this PR import cleanly.

    Any path/dependency drift (missing requirements, renamed module, broken
    relative import) shows up as an ImportError here instead of as a 500
    on the first Cloud Run task.
    """
    sentinels = (
        "ma_poc.pms.scraper",
        "ma_poc.pms.adapters.generic",
        "ma_poc.services.floorplan_catalog",
        "ma_poc.services.floorplan_snap",
        "ma_poc.services.llm_extractor",
        "ma_poc.services.llm_api_rescue",
        "ma_poc.validation.schema_gate",
        "ma_poc.reporting.observation_reports",
    )
    for mod in sentinels:
        try:
            __import__(mod)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"import {mod}: {exc}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    failures: list[str] = []
    _check_mapping(failures)
    _check_catalog(failures)
    _check_prompts(failures)
    _check_imports(failures)

    if failures:
        print("DEPLOYMENT VALIDATION FAILED", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    # Re-load to print the summary line; safe — singleton is already warm.
    from ma_poc.services.floorplan_catalog import get_default_catalog

    cat = get_default_catalog()
    indexed = len(cat._by_property)  # noqa: SLF001
    print(
        f"OK: chosen_field={cat.chosen_field} indexed_properties={indexed} "
        f"prompts={len(_REQUIRED_PROMPTS)} validated"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
