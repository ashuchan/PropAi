"""Redirect-path and fail-closed controls for Hyperbrowser raw GET."""

from __future__ import annotations

import pytest

from ma_poc.fetch import hyperbrowser_backend as hb


class _Session:
    def __init__(self, page: object) -> None:
        self.page = page
        self.closed = False

    async def open(self) -> object:
        return self.page

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_raw_get_uses_cross_host_final_landing_path_and_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPERBROWSER_MAX_CALLS_PER_PROPERTY", "1")
    hb.reset_hyperbrowser_property_counts()
    requested: list[str] = []

    class _RedirectPage:
        url = (
            "https://operator.example/apartments/edgefield/"
            "?utm_source=redirect&utm_campaign=edgefield"
        )

        async def goto(self, url: str, **kwargs: object) -> None:
            assert url == "https://vanity.example/"

        async def evaluate(self, _js: str, relative: str) -> dict[str, object]:
            requested.append(relative)
            return {"status": 200, "body": "exact-property-page"}

    session = _Session(_RedirectPage())
    result = await hb.hb_raw_get(
        "https://vanity.example/",
        "redirect-cross-host",
        session_factory=lambda: session,
    )

    assert result == (200, "exact-property-page")
    assert requested == [
        "/apartments/edgefield/?utm_source=redirect&utm_campaign=edgefield"
    ]
    assert session.closed
    assert hb.hyperbrowser_property_call_count("redirect-cross-host") == 1


@pytest.mark.asyncio
async def test_raw_get_preserves_same_host_path_and_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPERBROWSER_MAX_CALLS_PER_PROPERTY", "1")
    hb.reset_hyperbrowser_property_counts()
    requested: list[str] = []

    class _SameHostPage:
        url = "https://property.example/floorplans/?beds=2&available=true"

        async def goto(self, url: str, **kwargs: object) -> None:
            assert url == self.url

        async def evaluate(self, _js: str, relative: str) -> dict[str, object]:
            requested.append(relative)
            return {"status": 200, "body": "same-host"}

    session = _Session(_SameHostPage())
    result = await hb.hb_raw_get(
        "https://property.example/floorplans/?beds=2&available=true",
        "redirect-same-host",
        session_factory=lambda: session,
    )

    assert result == (200, "same-host")
    assert requested == ["/floorplans/?beds=2&available=true"]
    assert session.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["goto", "evaluate"])
async def test_raw_get_redirect_handling_fails_closed_and_closes_session(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    monkeypatch.setenv("HYPERBROWSER_MAX_CALLS_PER_PROPERTY", "1")
    hb.reset_hyperbrowser_property_counts()

    class _BrokenPage:
        url = "https://operator.example/apartments/edgefield/"

        async def goto(self, _url: str, **_kwargs: object) -> None:
            if failure_stage == "goto":
                raise RuntimeError("navigation failed")

        async def evaluate(self, _js: str, _relative: str) -> dict[str, object]:
            if failure_stage == "evaluate":
                raise RuntimeError("raw fetch failed")
            raise AssertionError("evaluate must not run after navigation failure")

    session = _Session(_BrokenPage())
    result = await hb.hb_raw_get(
        "https://vanity.example/",
        f"redirect-failure-{failure_stage}",
        session_factory=lambda: session,
    )

    assert result == (0, "")
    assert session.closed
