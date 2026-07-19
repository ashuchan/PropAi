"""L1-push of the collapsed-reveal click (task #37 Track 2).

_drive_reveal_in_render fires interactive_reveal.maybe_reveal on the still-open
render page so the click-to-reveal (Pattern B) cohort's rents fold into the body
— L3 stays page-free. These pin the flag-gate + never-fail contract; the reveal
mechanics themselves are covered by interactive_reveal's own tests.
"""

from __future__ import annotations

import pytest

from ma_poc.fetch.fetcher import _drive_reveal_in_render


class _Page:
    """Placeholder — the stubbed maybe_reveal ignores it."""


@pytest.mark.asyncio
async def test_flag_off_does_not_call_maybe_reveal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INTERACTION_REVEAL", raising=False)
    calls: list[object] = []

    async def _spy(page: object, **kw: object) -> dict:
        calls.append(page)
        return {"triggered": True}

    monkeypatch.setattr("ma_poc.pms.interactive_reveal.maybe_reveal", _spy)
    assert await _drive_reveal_in_render(_Page()) is False
    assert calls == []  # not invoked when the flag is off


@pytest.mark.asyncio
async def test_flag_on_triggered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERACTION_REVEAL", "true")

    async def _spy(page: object, **kw: object) -> dict:
        return {"triggered": True, "clicks": 1, "rent_delta": 5}

    monkeypatch.setattr("ma_poc.pms.interactive_reveal.maybe_reveal", _spy)
    assert await _drive_reveal_in_render(_Page()) is True


@pytest.mark.asyncio
async def test_flag_on_not_triggered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERACTION_REVEAL", "1")

    async def _spy(page: object, **kw: object) -> dict:
        return {"triggered": False, "reason": "no_reveal_text"}

    monkeypatch.setattr("ma_poc.pms.interactive_reveal.maybe_reveal", _spy)
    assert await _drive_reveal_in_render(_Page()) is False


@pytest.mark.asyncio
async def test_never_fail_on_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERACTION_REVEAL", "yes")

    async def _boom(page: object, **kw: object) -> dict:
        raise RuntimeError("playwright hung")

    monkeypatch.setattr("ma_poc.pms.interactive_reveal.maybe_reveal", _boom)
    assert await _drive_reveal_in_render(_Page()) is False


@pytest.mark.asyncio
async def test_non_dict_result_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERACTION_REVEAL", "true")

    async def _weird(page: object, **kw: object) -> object:
        return None

    monkeypatch.setattr("ma_poc.pms.interactive_reveal.maybe_reveal", _weird)
    assert await _drive_reveal_in_render(_Page()) is False


@pytest.mark.asyncio
async def test_page_html_is_forwarded_to_maybe_reveal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The already-captured body must be forwarded so maybe_reveal skips its own
    page.content() (the residual render-hang source). Non-reveal pages then do
    zero page ops."""
    monkeypatch.setenv("INTERACTION_REVEAL", "true")
    seen: dict[str, object] = {}

    async def _spy(page: object, *, page_html: object = None, **kw: object) -> dict:
        seen["page_html"] = page_html
        return {"triggered": False, "reason": "no_reveal_text"}

    monkeypatch.setattr("ma_poc.pms.interactive_reveal.maybe_reveal", _spy)
    await _drive_reveal_in_render(_Page(), "<html>rendered body</html>")
    assert seen["page_html"] == "<html>rendered body</html>"
