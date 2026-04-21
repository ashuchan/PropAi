"""Tests for stealth — identity pool with deterministic sticky keys."""

from __future__ import annotations

from ma_poc.fetch.stealth import _IDENTITIES, IdentityPool


def test_identity_pool_picks_deterministically_for_sticky_key() -> None:
    pool = IdentityPool()
    id1 = pool.pick(sticky_key="property_123")
    id2 = pool.pick(sticky_key="property_123")
    assert id1 == id2


def test_identity_pool_rotate_changes_pick() -> None:
    pool = IdentityPool()
    id1 = pool.pick(sticky_key="property_123")
    pool.rotate("property_123")
    id2 = pool.pick(sticky_key="property_123")
    assert id1 != id2


def test_identity_pool_uas_are_realistic() -> None:
    for identity in _IDENTITIES:
        assert "Mozilla/5.0" in identity.user_agent
        # Must have a version number pattern
        assert any(c.isdigit() for c in identity.user_agent), (
            f"No version number in UA: {identity.user_agent}"
        )


def test_identity_pool_no_duplicate_entries() -> None:
    uas = [i.user_agent for i in _IDENTITIES]
    assert len(uas) == len(set(uas)), "Duplicate UA strings found"
