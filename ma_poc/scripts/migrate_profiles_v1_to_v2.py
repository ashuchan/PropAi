"""
Migrate scrape profiles from v1 schema to v2.

Changes applied:
  - api_hints.api_provider: null -> "unknown" (default)
  - api_hints.client_account_id: NEW field, initialized to None
  - dom_hints.platform_detected: REMOVED (duplicates api_provider)
  - navigation.explored_links: capped at 50 entries
  - api_hints.blocked_endpoints: capped at 50
  - api_hints.llm_field_mappings: capped at 20
  - cluster_id: REMOVED from top-level ScrapeProfile
  - confidence.last_success_detection: NEW field, initialized to None
  - confidence.consecutive_unreachable: NEW field, initialized to 0
  - stats: NEW ProfileStats section, all zeros
  - version: bumped to 2

Keeps v1 copy under config/profiles/_audit/<cid>_v1.json.
If api_provider is null/unknown: runs detect_pms(entry_url) to populate it.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Allow running from repo root or scripts/
_repo = Path(__file__).resolve().parent.parent
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))

from pms.detector import detect_pms  # noqa: E402

logger = logging.getLogger(__name__)


def _migrate_one(raw: dict[str, Any]) -> dict[str, Any]:
    """Transform a single v1 profile dict into v2 in-place and return it."""

    # --- api_hints ---
    api_hints = raw.setdefault("api_hints", {})
    if not api_hints.get("api_provider"):
        api_hints["api_provider"] = "unknown"
    api_hints.setdefault("client_account_id", None)

    # Cap blocked_endpoints at 50
    blocked = api_hints.get("blocked_endpoints")
    if isinstance(blocked, list) and len(blocked) > 50:
        api_hints["blocked_endpoints"] = blocked[:50]

    # Cap llm_field_mappings at 20
    mappings = api_hints.get("llm_field_mappings")
    if isinstance(mappings, list) and len(mappings) > 20:
        api_hints["llm_field_mappings"] = mappings[:20]

    # --- dom_hints: remove platform_detected ---
    dom_hints = raw.get("dom_hints", {})
    dom_hints.pop("platform_detected", None)
    raw["dom_hints"] = dom_hints

    # --- navigation: cap explored_links at 50 ---
    navigation = raw.get("navigation", {})
    explored = navigation.get("explored_links")
    if isinstance(explored, list) and len(explored) > 50:
        navigation["explored_links"] = explored[:50]

    # --- Remove cluster_id ---
    raw.pop("cluster_id", None)

    # --- confidence: add new fields ---
    confidence = raw.setdefault("confidence", {})
    confidence.setdefault("last_success_detection", None)
    confidence.setdefault("consecutive_unreachable", 0)

    # --- stats: initialize ---
    raw.setdefault("stats", {
        "total_scrapes": 0,
        "total_successes": 0,
        "total_failures": 0,
        "total_llm_calls": 0,
        "total_llm_cost_usd": 0.0,
        "last_tier_used": None,
        "last_unit_count": 0,
        "p50_scrape_duration_ms": None,
        "p95_scrape_duration_ms": None,
    })

    # --- Detect PMS if api_provider is unknown ---
    entry_url = raw.get("navigation", {}).get("entry_url", "")
    if api_hints.get("api_provider") == "unknown" and entry_url:
        detected = detect_pms(entry_url)
        if detected.pms != "unknown" and detected.confidence >= 0.5:
            api_hints["api_provider"] = detected.pms
            api_hints["client_account_id"] = detected.pms_client_account_id
            confidence["last_success_detection"] = {
                "pms": detected.pms,
                "confidence": detected.confidence,
                "evidence": detected.evidence,
            }

    # --- Bump version ---
    raw["version"] = 2
    raw["updated_at"] = datetime.utcnow().isoformat()

    return raw


def migrate_profiles(
    profiles_dir: Path,
    audit_dir: Path | None = None,
) -> dict[str, Any]:
    """Migrate all profile JSON files in profiles_dir from v1 to v2.

    Args:
        profiles_dir: Directory containing *.json profile files.
        audit_dir: Directory for v1 backup copies.  Defaults to profiles_dir/_audit.

    Returns:
        Summary dict with counts.
    """
    if audit_dir is None:
        audit_dir = profiles_dir / "_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)

    profile_files = sorted(profiles_dir.glob("*.json"))
    summary = {
        "total": 0,
        "migrated": 0,
        "already_v2": 0,
        "errors": 0,
        "pms_detected": 0,
    }

    for pf in profile_files:
        summary["total"] += 1
        cid = pf.stem
        try:
            raw = json.loads(pf.read_text(encoding="utf-8"))

            # Write v1 audit copy (always overwrite — idempotent)
            audit_path = audit_dir / f"{cid}_v1.json"
            audit_path.write_text(
                json.dumps(raw, indent=2, default=str),
                encoding="utf-8",
            )

            old_provider = raw.get("api_hints", {}).get("api_provider")
            migrated = _migrate_one(raw)
            new_provider = migrated.get("api_hints", {}).get("api_provider")

            if old_provider in (None, "unknown") and new_provider not in (None, "unknown"):
                summary["pms_detected"] += 1

            # Write migrated profile
            pf.write_text(
                json.dumps(migrated, indent=2, default=str),
                encoding="utf-8",
            )
            summary["migrated"] += 1
            logger.info("Migrated %s", cid)

        except Exception:
            logger.exception("Failed to migrate %s", cid)
            summary["errors"] += 1

    return summary


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    profiles_dir = _repo / "config" / "profiles"
    if not profiles_dir.is_dir():
        logger.error("Profiles directory not found: %s", profiles_dir)
        sys.exit(1)

    summary = migrate_profiles(profiles_dir)

    print("\n=== Migration Summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    if summary["errors"] > 0:
        print(f"\nWARNING: {summary['errors']} profiles failed migration.")
        sys.exit(1)
    else:
        print("\nAll profiles migrated successfully.")


if __name__ == "__main__":
    main()
