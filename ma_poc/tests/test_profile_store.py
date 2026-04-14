"""Tests for ProfileStore — claude-scrapper-arch.md Step 6.2."""
from __future__ import annotations

import pytest

from models.scrape_profile import ProfileMaturity, ScrapeProfile
from services.profile_store import ProfileStore


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / "profiles")


def test_save_and_load_roundtrip(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="abc-001")
    store.save(p)
    loaded = store.load("abc-001")
    assert loaded is not None
    assert loaded.canonical_id == "abc-001"
    assert loaded.confidence.maturity == ProfileMaturity.COLD


def test_load_nonexistent_returns_none(store: ProfileStore) -> None:
    assert store.load("nonexistent") is None


def test_save_increments_version(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="ver-001", version=1)
    store.save(p)
    p.version += 1
    store.save(p)
    loaded = store.load("ver-001")
    assert loaded is not None
    assert loaded.version == 2


def test_save_creates_audit_copy(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="aud-001", version=3)
    store.save(p)
    audit_path = store._audit / "aud-001_3.json"
    assert audit_path.exists()


def test_bootstrap_from_meta_detects_rentcafe(store: ProfileStore) -> None:
    p = store.bootstrap_from_meta(
        "rc-001", {"name": "Test"}, "https://www.rentcafe.com/apartments/foo"
    )
    assert p.dom_hints.platform_detected == "rentcafe"
    assert p.confidence.maturity == ProfileMaturity.COLD
    # Profile file should be created
    assert store.load("rc-001") is not None


def test_bootstrap_from_meta_detects_entrata(store: ProfileStore) -> None:
    p = store.bootstrap_from_meta(
        "ent-001", {}, "https://my.entrata.com/property"
    )
    assert p.dom_hints.platform_detected == "entrata"


def test_bootstrap_from_meta_unknown_platform(store: ProfileStore) -> None:
    p = store.bootstrap_from_meta(
        "unk-001", {}, "https://some-custom-site.com"
    )
    assert p.dom_hints.platform_detected is None
    assert p.canonical_id == "unk-001"


def test_list_by_maturity(store: ProfileStore) -> None:
    cold = ScrapeProfile(canonical_id="cold-001")
    hot = ScrapeProfile(canonical_id="hot-001")
    hot.confidence.maturity = ProfileMaturity.HOT
    store.save(cold)
    store.save(hot)
    colds = store.list_by_maturity(ProfileMaturity.COLD)
    hots = store.list_by_maturity(ProfileMaturity.HOT)
    assert len(colds) == 1
    assert colds[0].canonical_id == "cold-001"
    assert len(hots) == 1
    assert hots[0].canonical_id == "hot-001"
