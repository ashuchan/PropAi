"""Integration test — provider _single_attempt persists cookies across calls."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.fetch.contracts import RenderMode
from ma_poc.fetch.http_client import _AdapterResponse


def _task() -> MagicMock:
    t = MagicMock()
    t.url = "https://www.rentcafe.com/onlineleasing/x/floorplans.aspx"
    t.property_id = "PROP_JAR_TEST"
    t.render_mode = RenderMode.GET
    t.budget_ms = 10000
    t.etag = None
    t.last_modified = None
    return t


def _adapter(set_cookies: dict[str, str]) -> AsyncMock:
    a = AsyncMock()
    a.request = AsyncMock(return_value=_AdapterResponse(
        status_code=200,
        headers={},
        content=b"<html></html>",
        final_url="https://www.rentcafe.com/",
        cookies=set_cookies,
    ))
    a.aclose = AsyncMock()
    return a


@pytest.mark.asyncio
async def test_direct_provider_persists_and_reuses_cookies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from ma_poc.fetch.providers.direct import DirectProvider

    # First fetch: server issues cf_clearance.
    with patch(
        "ma_poc.fetch.providers.direct.make_http_client",
        return_value=_adapter({"cf_clearance": "TOKEN1"}),
    ):
        await DirectProvider().fetch(_task(), MagicMock())

    # Second fetch: capture what cookies the adapter receives.
    captured: dict[str, object] = {}
    adapter = _adapter({})
    original_request = adapter.request

    async def _spy(method: str, url: str, *, headers: object, cookies: object = None, timeout: float = 30.0) -> object:
        captured["cookies"] = cookies
        return await original_request(method, url, headers=headers, cookies=cookies, timeout=timeout)

    adapter.request = _spy
    with patch("ma_poc.fetch.providers.direct.make_http_client", return_value=adapter):
        await DirectProvider().fetch(_task(), MagicMock())

    assert captured["cookies"] is not None, "second fetch sent cookies=None — jar not wired"
    assert captured["cookies"].get("cf_clearance") == "TOKEN1", (  # type: ignore[union-attr]
        f"cf_clearance not carried forward; got {captured['cookies']}"
    )
