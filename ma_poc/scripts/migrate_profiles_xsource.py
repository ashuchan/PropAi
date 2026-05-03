#!/usr/bin/env python3
"""One-shot migration to add Phase 6 / 7 / 10 / 11 / 12 schema fields to
existing profiles. Run BEFORE starting Phase 5 in production. Idempotent.

Adds defaults (no behavior change until cascade reads them):
  - LlmFieldMapping.consecutive_replay_failures = 0
  - LlmFieldMapping.last_replayed_at = None
  - LlmFieldMapping.source_envelope_hash = ""
  - LlmFieldMapping.quality_score = 1.0
  - ApiHints.field_patches = []                     (Phase 7)
  - ApiHints.source_observations = []               (Phase 11)
  - DomHints.consecutive_misses = 0                 (Phase 8)
  - ExtractionConfidence.cold_run_count = 0         (Phase 10)
  - ExtractionConfidence.last_sources_run = []      (Phase 11)
  - ScrapeProfile.cluster_key = ""                  (Phase 12)

Pydantic v2 model_validate handles all defaults automatically thanks to
extra="ignore" — this script is a pure idempotent upgrade-then-resave.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as module or script
REPO = Path(__file__).resolve().parents[2]
if str(REPO / "ma_poc") not in sys.path:
    sys.path.insert(0, str(REPO / "ma_poc"))

from models.scrape_profile import ScrapeProfile  # noqa: E402


def migrate_one(path: Path, dry_run: bool = False) -> dict:
    """Migrate a single profile JSON file in place. Returns a status dict."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"path": str(path), "status": "PARSE_ERROR", "error": str(exc)}

    try:
        profile = ScrapeProfile.model_validate(data)
    except Exception as exc:
        return {"path": str(path), "status": "VALIDATE_ERROR", "error": str(exc)}

    new_data = profile.model_dump(mode="json")
    if new_data == data:
        return {"path": str(path), "status": "NO_CHANGE"}

    if not dry_run:
        path.write_text(
            json.dumps(new_data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    return {"path": str(path), "status": "UPGRADED" if not dry_run else "DRY_RUN_WOULD_UPGRADE"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles-dir", default=str(REPO / "ma_poc" / "config" / "profiles"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="0 = no limit")
    args = parser.parse_args()

    base = Path(args.profiles_dir)
    if not base.exists():
        print(f"profiles dir not found: {base}")
        return 1

    counts = {"NO_CHANGE": 0, "UPGRADED": 0, "DRY_RUN_WOULD_UPGRADE": 0,
              "PARSE_ERROR": 0, "VALIDATE_ERROR": 0}
    seen = 0
    for p in sorted(base.glob("*.json")):
        if p.parent.name == "_audit":
            continue
        result = migrate_one(p, dry_run=args.dry_run)
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        seen += 1
        if result["status"] in ("PARSE_ERROR", "VALIDATE_ERROR"):
            print(f"  {result['status']}: {p.name} — {result.get('error','')[:120]}")
        if args.limit and seen >= args.limit:
            break

    print(f"Scanned {seen} profile(s):")
    for k, v in counts.items():
        if v:
            print(f"  {k}: {v}")
    if counts["PARSE_ERROR"] or counts["VALIDATE_ERROR"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
