"""Tests for migrate_profiles_add_fetch — Phase E1."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure the repo root (ma_poc parent) is importable for the scripts module
_repo = Path(__file__).resolve().parent.parent
_repo_parent = _repo.parent
for _p in (_repo_parent, _repo):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ma_poc.models.fetch_tier import FetchTier  # noqa: E402
from ma_poc.models.scrape_profile import FetchProfile  # noqa: E402
from ma_poc.scripts.migrate_profiles_add_fetch import migrate_profiles  # noqa: E402


def _write_profile(directory: Path, canonical_id: str, extra: dict | None = None) -> Path:
    """Write a minimal profile JSON file and return its path."""
    data: dict = {
        "canonical_id": canonical_id,
        "version": 2,
        "schema_version": "v2",
    }
    if extra:
        data.update(extra)
    path = directory / f"{canonical_id}.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_migration_adds_fetch_to_legacy_profile(tmp_path: Path) -> None:
    """A profile without a 'fetch' key must gain one after migration."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    _write_profile(profiles_dir, "legacy-001")

    summary = migrate_profiles(profiles_dir)

    assert summary["migrated_count"] == 1
    assert summary["already_had_fetch_count"] == 0
    assert summary["errors"] == 0

    saved = json.loads((profiles_dir / "legacy-001.json").read_text(encoding="utf-8"))
    assert "fetch" in saved
    assert saved["fetch"]["tier_floor"] == FetchTier.DIRECT


def test_migration_preserves_existing_fetch(tmp_path: Path) -> None:
    """A profile that already has a 'fetch' key must not be modified."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()

    existing_fetch = FetchProfile(tier_floor=FetchTier.DC_PROXY, total_escalations=5).model_dump(
        mode="json"
    )
    path = _write_profile(profiles_dir, "existing-001", extra={"fetch": existing_fetch})

    mtime_before = path.stat().st_mtime

    summary = migrate_profiles(profiles_dir)

    assert summary["already_had_fetch_count"] == 1
    assert summary["migrated_count"] == 0
    assert summary["errors"] == 0

    # File must not have been rewritten
    assert path.stat().st_mtime == mtime_before


def test_migration_idempotent(tmp_path: Path) -> None:
    """Running migration twice: second pass reports everything as already_had_fetch."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    _write_profile(profiles_dir, "idem-001")
    _write_profile(profiles_dir, "idem-002")

    first = migrate_profiles(profiles_dir)
    assert first["migrated_count"] == 2

    second = migrate_profiles(profiles_dir)
    assert second["already_had_fetch_count"] == 2
    assert second["migrated_count"] == 0
    assert second["errors"] == 0


def test_migration_handles_corrupt_json(tmp_path: Path) -> None:
    """Malformed JSON in a profile file must be counted as an error, not crash."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    bad_path = profiles_dir / "corrupt-001.json"
    bad_path.write_text("{ this is not valid JSON !!!", encoding="utf-8")

    summary = migrate_profiles(profiles_dir)

    assert summary["errors"] == 1
    assert summary["migrated_count"] == 0


def test_migration_writes_audit_copy(tmp_path: Path) -> None:
    """After migration an audit copy of the profile must exist."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    _write_profile(profiles_dir, "audit-001")

    migrate_profiles(profiles_dir)

    audit_dir = profiles_dir / "_audit"
    audit_files = list(audit_dir.glob("audit-001_*.json"))
    assert len(audit_files) >= 1, "Expected at least one audit copy"
