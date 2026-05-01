"""Tests for the FetchTier enum — Phase E1."""

from __future__ import annotations

import json

from ma_poc.models.fetch_tier import FetchTier


def test_tier_ordering() -> None:
    """Lower tiers must be cheaper — ordering must never be changed."""
    assert FetchTier.DIRECT < FetchTier.DC_PROXY
    assert FetchTier.DC_PROXY < FetchTier.RESIDENTIAL
    assert FetchTier.RESIDENTIAL < FetchTier.UNLOCKER
    assert FetchTier.UNLOCKER < FetchTier.DLQ_PARK


def test_tier_arithmetic() -> None:
    """IntEnum allows natural escalation arithmetic."""
    assert FetchTier(FetchTier.DIRECT + 1) == FetchTier.STEALTH_LOCAL


def test_tier_serialization() -> None:
    """FetchTier serialises as an int and round-trips through JSON."""
    assert FetchTier.DC_PROXY.value == 2

    # Round-trip through JSON
    serialized = json.dumps(FetchTier.DC_PROXY)
    loaded_int = json.loads(serialized)
    assert loaded_int == 2
    assert FetchTier(loaded_int) == FetchTier.DC_PROXY
