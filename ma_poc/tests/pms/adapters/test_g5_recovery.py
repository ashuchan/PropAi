"""G5 universal-recovery (2026-05-24).

Pins the wrapper that lets G5Adapter run as a recovery path when
other primary adapters returned 0 units AND the homepage HTML has a
g5-cl-* URN. Closes detector-misroute cases (Knock won the race but
the data lives behind the G5 GraphQL API).
"""
from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.pms.adapters._g5_recovery import recover_g5


def _make_ctx(*, body: bytes | str = b"<html></html>", final_url: str = "https://example.com/"):
    @dataclasses.dataclass
    class _FR:
        body: bytes | str | None
        final_url: str

    ctx = MagicMock()
    ctx.fetch_result = _FR(
        body=body if isinstance(body, bytes) else body.encode(),
        final_url=final_url,
    )
    ctx.base_url = final_url
    return ctx


# ── short-circuit guards ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovery_noop_when_no_fetch_result() -> None:
    ctx = MagicMock()
    ctx.fetch_result = None
    units = await recover_g5(None, ctx)
    assert units == []


@pytest.mark.asyncio
async def test_recovery_noop_when_body_has_no_g5_marker() -> None:
    """Cheap exit on bodies that have NO g5-cl marker at all."""
    ctx = _make_ctx(body="<html><body>no g5 here</body></html>")
    with patch("ma_poc.pms.adapters.g5.G5Adapter") as adapter_mock:
        units = await recover_g5(None, ctx)
    assert units == []
    adapter_mock.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_noop_when_no_urn_candidates() -> None:
    """The substring 'g5-cl' appears but no actual URN matches the
    pattern (random g5-cl in CSS class name etc.). Don't fire."""
    ctx = _make_ctx(body="<div class='g5-clip-overflow'>hi</div>")
    with patch("ma_poc.pms.adapters.g5.G5Adapter") as adapter_mock:
        units = await recover_g5(None, ctx)
    assert units == []
    adapter_mock.assert_not_called()


# ── happy path: URN present → adapter runs → units returned ──────────


@pytest.mark.asyncio
async def test_recovery_invokes_g5_adapter_when_urn_present() -> None:
    """Body has a valid g5-cl-* URN → G5Adapter is instantiated and
    its extract() method is called."""
    body = '<img src="/g5-cl-1abc-some-property/img.jpg">'
    ctx = _make_ctx(body=body)

    fake_result = MagicMock()
    fake_result.units = [
        {"unit_number": "101", "market_rent_low": 1500, "sqft": "650"},
    ]
    fake_adapter_cls = MagicMock()
    fake_adapter_inst = MagicMock()
    fake_adapter_inst.extract = AsyncMock(return_value=fake_result)
    fake_adapter_cls.return_value = fake_adapter_inst

    with patch("ma_poc.pms.adapters.g5.G5Adapter", fake_adapter_cls):
        units = await recover_g5(None, ctx)

    assert len(units) == 1
    fake_adapter_cls.assert_called_once()
    fake_adapter_inst.extract.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_stamps_extraction_tier_when_missing() -> None:
    """Units returned by G5Adapter without extraction_tier get the
    RECOVERY label so reporting can distinguish this path."""
    body = '<img src="/g5-cl-1abc/img.jpg">'
    ctx = _make_ctx(body=body)

    fake_result = MagicMock()
    fake_result.units = [{"unit_number": "1"}]  # no tier
    fake_adapter_cls = MagicMock()
    fake_adapter_cls.return_value.extract = AsyncMock(return_value=fake_result)

    with patch("ma_poc.pms.adapters.g5.G5Adapter", fake_adapter_cls):
        units = await recover_g5(None, ctx)

    assert units[0]["extraction_tier"] == "TIER_1_API_G5_RECOVERY"


@pytest.mark.asyncio
async def test_recovery_preserves_existing_extraction_tier() -> None:
    """If G5Adapter stamped its own tier (e.g. TIER_2_API_G5_APOLLO),
    do NOT overwrite it — the more-specific label wins."""
    body = '<img src="/g5-cl-1abc/img.jpg">'
    ctx = _make_ctx(body=body)

    fake_result = MagicMock()
    fake_result.units = [
        {"unit_number": "1", "extraction_tier": "TIER_2_API_G5_APOLLO"}
    ]
    fake_adapter_cls = MagicMock()
    fake_adapter_cls.return_value.extract = AsyncMock(return_value=fake_result)

    with patch("ma_poc.pms.adapters.g5.G5Adapter", fake_adapter_cls):
        units = await recover_g5(None, ctx)

    assert units[0]["extraction_tier"] == "TIER_2_API_G5_APOLLO"


# ── failure paths ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovery_returns_empty_when_adapter_yields_no_units() -> None:
    """G5 GraphQL returned a complex but with no apartments (operator
    has no inventory) → recovery returns [] cleanly."""
    body = '<img src="/g5-cl-1abc/img.jpg">'
    ctx = _make_ctx(body=body)

    fake_result = MagicMock()
    fake_result.units = []
    fake_adapter_cls = MagicMock()
    fake_adapter_cls.return_value.extract = AsyncMock(return_value=fake_result)

    with patch("ma_poc.pms.adapters.g5.G5Adapter", fake_adapter_cls):
        units = await recover_g5(None, ctx)

    assert units == []


@pytest.mark.asyncio
async def test_recovery_swallows_adapter_exceptions() -> None:
    """G5Adapter raising must not propagate — recovery is best-effort
    and returns [] on internal failures."""
    body = '<img src="/g5-cl-1abc/img.jpg">'
    ctx = _make_ctx(body=body)

    fake_adapter_cls = MagicMock()
    fake_adapter_cls.return_value.extract = AsyncMock(
        side_effect=RuntimeError("simulated G5 blowup")
    )

    with patch("ma_poc.pms.adapters.g5.G5Adapter", fake_adapter_cls):
        units = await recover_g5(None, ctx)

    assert units == []
