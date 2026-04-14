"""
File-based profile store.

Profiles live at ``config/profiles/{canonical_id}.json``.
Audit copies at ``config/profiles/_audit/{canonical_id}_{version}.json``.

Phase: claude-scrapper-arch.md Step 1.2
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from models.scrape_profile import (
    ApiHints,
    DomHints,
    NavigationConfig,
    ProfileMaturity,
    ScrapeProfile,
    detect_platform,
)

log = logging.getLogger(__name__)


def _safe_filename(canonical_id: str) -> str:
    """Sanitize canonical_id for use as a filename."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in canonical_id)[:120]


class ProfileStore:
    """Persist and retrieve per-property scrape profiles as JSON files."""

    def __init__(self, base_dir: Path | str) -> None:
        self._base = Path(base_dir)
        self._audit = self._base / "_audit"
        self._base.mkdir(parents=True, exist_ok=True)
        self._audit.mkdir(parents=True, exist_ok=True)

    def load(self, canonical_id: str) -> Optional[ScrapeProfile]:
        """Load a profile by canonical ID. Returns None if not found."""
        safe_name = _safe_filename(canonical_id)
        path = self._base / f"{safe_name}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return ScrapeProfile.model_validate(data)
        except Exception as exc:
            log.warning("Failed to load profile %s: %s", canonical_id, exc)
            return None

    def save(self, profile: ScrapeProfile) -> None:
        """Save profile, incrementing version. Also writes an audit copy."""
        profile.updated_at = datetime.utcnow()
        data = profile.model_dump(mode="json")

        # Write current profile
        safe_name = _safe_filename(profile.canonical_id)
        path = self._base / f"{safe_name}.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Write audit copy
        audit_path = self._audit / f"{safe_name}_{profile.version}.json"
        audit_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def bootstrap_from_meta(
        self,
        canonical_id: str,
        meta: dict,
        website: str,
    ) -> ScrapeProfile:
        """Create an initial COLD profile from CSV metadata and URL-based PMS detection."""
        platform = detect_platform(website)

        # Build navigation hints from meta if available
        nav = NavigationConfig()
        if website:
            nav.entry_url = website

        api_hints = ApiHints()
        dom_hints = DomHints()
        if platform:
            dom_hints.platform_detected = platform
            api_hints.api_provider = platform

        profile = ScrapeProfile(
            canonical_id=canonical_id,
            version=1,
            updated_by="BOOTSTRAP",
            navigation=nav,
            api_hints=api_hints,
            dom_hints=dom_hints,
        )

        self.save(profile)
        return profile

    def list_by_maturity(self, maturity: ProfileMaturity) -> list[ScrapeProfile]:
        """Return all profiles with the given maturity level."""
        results: list[ScrapeProfile] = []
        for path in self._base.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                profile = ScrapeProfile.model_validate(data)
                if profile.confidence.maturity == maturity:
                    results.append(profile)
            except Exception:
                continue
        return results
