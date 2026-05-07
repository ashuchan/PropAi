"""
Migration script: add default ``fetch`` block to scrape profiles that lack it.

Walks config/profiles/*.json, checks for the "fetch" key, and adds a default
FetchProfile block when missing.  Idempotent — running twice produces no change
after the first pass.

Usage (from repo root or scripts/):
    python scripts/migrate_profiles_add_fetch.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# Allow running from repo root or scripts/
_repo = Path(__file__).resolve().parent.parent
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))

from ma_poc.models.fetch_tier import FetchTier  # noqa: E402
from ma_poc.models.scrape_profile import FetchProfile, ScrapeProfile  # noqa: E402
from ma_poc.services.profile_store import ProfileStore  # noqa: E402

logger = logging.getLogger(__name__)

_DEFAULT_FETCH_BLOCK: dict = FetchProfile().model_dump(mode="json")


def migrate_profiles(profiles_dir: Path) -> dict:
    """Add a default fetch block to every profile that lacks one.

    Args:
        profiles_dir: Directory containing ``*.json`` profile files.

    Returns:
        Summary dict: migrated_count, already_had_fetch_count, errors, total.
    """
    store = ProfileStore(profiles_dir)

    summary = {
        "total": 0,
        "migrated_count": 0,
        "already_had_fetch_count": 0,
        "errors": 0,
    }

    for path in sorted(profiles_dir.glob("*.json")):
        summary["total"] += 1
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))

            if "fetch" in raw:
                summary["already_had_fetch_count"] += 1
                logger.debug("Skipping %s — already has fetch block", path.name)
                continue

            # Load via ProfileStore so model validation runs
            profile = ScrapeProfile.model_validate(raw)
            # profile.fetch defaults to FetchProfile() — persist it via store
            store.save(profile)

            summary["migrated_count"] += 1
            logger.info("Migrated %s", path.stem)

        except Exception:
            logger.exception("Failed to migrate %s", path.name)
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
        print("\nAll profiles processed successfully.")


if __name__ == "__main__":
    main()
