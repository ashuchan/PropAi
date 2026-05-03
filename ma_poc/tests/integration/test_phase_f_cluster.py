"""Phase F tests — cluster_key population from PMS detector."""

from __future__ import annotations

import pytest

from models.scrape_profile import ProfileMaturity, ScrapeProfile
from pms.detector import DetectedPMS


def test_f1_cluster_key_populated_from_detected_pms() -> None:
    """When a COLD profile has no cluster_key and detection provides an account
    ID, cluster_key must be set."""
    p = ScrapeProfile(canonical_id="phase_f_001")
    assert p.cluster_key == ""

    detected = DetectedPMS(
        pms="rentcafe",
        confidence=0.9,
        pms_client_account_id="acct_xyz_123",
    )
    pms_client_id = str(detected.pms_client_account_id or "")
    if pms_client_id and not p.cluster_key:
        p.cluster_key = pms_client_id

    assert p.cluster_key == "acct_xyz_123"


def test_f1_cluster_key_not_overwritten() -> None:
    """An already-set cluster_key must not be overwritten by a new detection."""
    p = ScrapeProfile(canonical_id="phase_f_002")
    p.cluster_key = "original_key"

    detected = DetectedPMS(
        pms="rentcafe",
        confidence=0.9,
        pms_client_account_id="new_key",
    )
    pms_client_id = str(detected.pms_client_account_id or "")
    if pms_client_id and not p.cluster_key:
        p.cluster_key = pms_client_id

    assert p.cluster_key == "original_key"


def test_f1_empty_client_id_leaves_cluster_key_blank() -> None:
    """When detection provides no account ID, cluster_key stays empty."""
    p = ScrapeProfile(canonical_id="phase_f_003")
    detected = DetectedPMS(pms="generic", confidence=0.5)

    pms_client_id = str(detected.pms_client_account_id or "")
    if pms_client_id and not p.cluster_key:
        p.cluster_key = pms_client_id

    assert p.cluster_key == ""


def test_f2_find_cluster_mates_returns_profiles_with_same_key(tmp_path) -> None:
    """find_cluster_mates must return profiles sharing cluster_key, excluding self."""
    from services.profile_store import ProfileStore
    from services.cluster_store import find_cluster_mates

    store = ProfileStore(tmp_path / "profiles")
    # Create cluster mates — must be HOT for find_cluster_mates to return them
    for i in range(3):
        mate = ScrapeProfile(canonical_id=f"mate_{i:03d}")
        mate.cluster_key = "acct_xyz"
        mate.confidence.maturity = ProfileMaturity.HOT
        store.save(mate)
    # Self — same key, must be excluded from mates
    self_p = ScrapeProfile(canonical_id="self_001")
    self_p.cluster_key = "acct_xyz"
    store.save(self_p)
    # Unrelated profile
    other = ScrapeProfile(canonical_id="other_001")
    other.cluster_key = "different_acct"
    store.save(other)

    mates = find_cluster_mates(store, "acct_xyz", "self_001", max_mates=10)
    mate_ids = {m.canonical_id for m in mates}
    assert "self_001" not in mate_ids, "Self must be excluded from mates"
    assert "other_001" not in mate_ids, "Different cluster key must be excluded"
    assert len(mate_ids) == 3
