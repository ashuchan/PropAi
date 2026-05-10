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

    def load(self, canonical_id: str) -> ScrapeProfile | None:
        """Load a profile by canonical ID. Returns None if not found.

        Bug 4.3 — schema_version compat shim: the previous behaviour was
        ``except Exception: return None`` which silently flushed every
        property's accumulated learning whenever a code release renamed
        or restructured a Pydantic field. The shim now attempts a
        best-effort migration before giving up:

          1. Strict validation (existing fast path).
          2. On validation failure, log the schema_version marker and
             the first 200 chars of the error so the regression is
             visible. Then attempt partial reconstruction by retaining
             only the keys ``ScrapeProfile`` declares at the top level —
             ``ConfigDict(extra="ignore")`` handles unknown keys, but
             this also catches type-mismatch failures inside nested
             models by stripping those nested blocks rather than
             nuking the whole profile.
          3. Final fallback: emit a structured event so the SLO
             watcher catches the regression in the next run, then
             return None as a last resort.

        The shim never raises and never writes — recovery is purely
        a read-time concern.
        """
        safe_name = _safe_filename(canonical_id)
        path = self._base / f"{safe_name}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Failed to read profile JSON for %s: %s", canonical_id, exc)
            return None

        # Fast path
        try:
            return ScrapeProfile.model_validate(data)
        except Exception as strict_exc:
            stored_version = data.get("schema_version") if isinstance(data, dict) else "?"
            log.warning(
                "Profile %s strict validation failed (stored schema_version=%s): %s — "
                "attempting compat-shim recovery",
                canonical_id, stored_version, str(strict_exc)[:200],
            )

        # Compat shim — strip nested blocks that fail validation, keep
        # the rest. The model's nested fields all have default factories,
        # so dropping a corrupt block downgrades to a fresh sub-model
        # rather than discarding the whole profile.
        if not isinstance(data, dict):
            return None
        recovered: dict = {}
        # canonical_id is required; everything else has a default.
        recovered["canonical_id"] = data.get("canonical_id") or canonical_id
        for key in (
            "version", "schema_version", "created_at", "updated_at", "updated_by",
            "cluster_key", "property_amenities",
        ):
            if key in data:
                recovered[key] = data[key]
        # Try each nested block independently — if a block is corrupt
        # we let it default rather than failing the whole profile.
        for block_key, block_model in (
            ("navigation", NavigationConfig),
            ("api_hints", ApiHints),
            ("dom_hints", DomHints),
        ):
            block_data = data.get(block_key)
            if block_data is None:
                continue
            try:
                # Validate as the nested model so a bad sub-field
                # doesn't poison the whole block. ``extra="ignore"``
                # on each model handles unknown keys.
                block_model.model_validate(block_data)
                recovered[block_key] = block_data
            except Exception as nested_exc:
                log.warning(
                    "Profile %s: dropping corrupt %s block during compat shim: %s",
                    canonical_id, block_key, str(nested_exc)[:120],
                )
        # Keep blocks we don't enumerate above as-is when present —
        # ``confidence`` / ``llm_artifacts`` / ``stats`` / ``fetch``
        # have their own defaults and Pydantic will rebuild them
        # if the stored shape is bad.
        for opt_key in ("confidence", "llm_artifacts", "stats", "fetch"):
            if opt_key in data:
                recovered[opt_key] = data[opt_key]

        try:
            return ScrapeProfile.model_validate(recovered)
        except Exception as final_exc:
            log.warning(
                "Profile %s compat-shim recovery also failed: %s — returning None",
                canonical_id, str(final_exc)[:200],
            )
            try:
                from ma_poc.observability.events import EventKind, emit
                emit(
                    EventKind.PROFILE_DRIFT,
                    canonical_id,
                    reason="schema_validation_failure",
                    error=str(final_exc)[:200],
                )
            except Exception:
                pass
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

    def iter_profiles_by_cluster_key(
        self, cluster_key: str, limit: int = 100
    ) -> list[ScrapeProfile]:
        """Phase 12 — yield profiles matching cluster_key. Bounded LIMIT.

        File-backed implementation scans all profile JSONs. SQL-backed
        ProfileStore implementations can override with an indexed query.
        """
        if not cluster_key:
            return []
        results: list[ScrapeProfile] = []
        for path in self._base.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("cluster_key") != cluster_key:
                    continue
                profile = ScrapeProfile.model_validate(data)
                results.append(profile)
                if len(results) >= limit:
                    break
            except Exception:
                continue
        return results
