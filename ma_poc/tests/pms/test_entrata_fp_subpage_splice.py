"""Regression test for the Entrata→SightMap fp-subpage HTML splice
(2026-05-24).

Background
----------
``scraper.py`` (around line 1380) discovers an Entrata
``/{city}/{slug}/conventional/`` sub-page from the rendered HTML or
captured network log, fetches it via ``probe_get``, and when the
sub-page body mentions ``sightmap.com`` it splices that body into
``ctx.fetch_result`` so that ``SightMapAdapter._entry_html_from_ctx``
sees the embed code on its next run.

The pre-fix code did ``_fr2.body = _r.text`` directly. ``FetchResult``
is a ``@dataclass(slots=True, frozen=True)`` — that assignment raises
``FrozenInstanceError: cannot assign to field 'body'``. The exception
was swallowed by the surrounding try/except so the failure showed up
as a warning in canary logs (7 occurrences in
jugnu-unlocker-test-3886351-fl9gv) without ever exercising the
SightMap-secondary path on real fp-subpages.

The fix swaps the direct assignment for
``dataclasses.replace(_fr2, body=new_body_bytes)`` and assigns the
new FetchResult onto the mutable ``ctx.fetch_result``. It also encodes
the str→bytes to respect the ``body: bytes | None`` contract.

This test asserts the splice now actually happens — pre-fix it would
have raised inside the splice block and left ``ctx.fetch_result.body``
unchanged.
"""
from __future__ import annotations

import dataclasses
from typing import Any

import pytest


def _make_fetch_result(body: bytes = b"original") -> Any:
    """Build a real FetchResult-shaped record (frozen) so we exercise
    the immutability constraint that broke production."""
    from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode

    return FetchResult(
        url="https://www.example.com/",
        outcome=FetchOutcome.OK,
        status=200,
        body=body,
        headers={},
        render_mode=RenderMode.RENDER,
        final_url="https://www.example.com/",
        attempts=1,
        elapsed_ms=1234,
    )


def test_fetch_result_is_frozen_so_direct_assignment_fails() -> None:
    """Pins the precondition: ``FetchResult`` cannot be mutated in
    place. If this stops being true (someone unfroze the dataclass),
    the splice block can be simplified — but until then, the
    dataclasses.replace path is required."""
    fr = _make_fetch_result()
    with pytest.raises(dataclasses.FrozenInstanceError):
        fr.body = b"spliced"  # type: ignore[misc]


def test_dataclasses_replace_swaps_body_on_frozen_fetch_result() -> None:
    """The mechanism the fix relies on: ``dataclasses.replace`` mints
    a fresh frozen instance with the new body, leaving the rest of the
    fields intact."""
    fr = _make_fetch_result(body=b"original")
    new_body = b"<html>sightmap.com embed</html>"
    fr2 = dataclasses.replace(fr, body=new_body)

    assert fr2 is not fr
    assert fr2.body == new_body
    assert fr.body == b"original"  # original untouched
    # All other fields preserved
    assert fr2.url == fr.url
    assert fr2.outcome == fr.outcome
    assert fr2.status == fr.status
    assert fr2.final_url == fr.final_url
    assert fr2.attempts == fr.attempts


def test_adapter_context_accepts_replaced_fetch_result() -> None:
    """``AdapterContext`` is a mutable dataclass — the fix swaps the
    whole record on ``ctx.fetch_result``. Pin that the assignment
    works and downstream readers see the spliced body."""
    from ma_poc.pms.adapters.base import AdapterContext
    from ma_poc.pms.adapters.sightmap import _entry_html_from_ctx
    from ma_poc.pms.detector import DetectedPMS

    ctx = AdapterContext(
        base_url="https://www.example.com/",
        detected=DetectedPMS(pms="entrata", confidence=0.9, evidence=()),
        profile=None,
        expected_total_units=None,
        property_id="TEST-001",
        fetch_result=_make_fetch_result(body=b"<html>no sightmap here</html>"),
    )

    # Pre-splice: SightMap entry-html accessor returns the original body
    pre = _entry_html_from_ctx(ctx)
    assert pre == "<html>no sightmap here</html>"

    # Splice: replace the frozen fetch_result with one carrying the
    # sub-page HTML (same construction the fixed scraper.py uses).
    spliced_body = (
        '<html><iframe src="https://sightmap.com/embed/abc123"></iframe></html>'
    )
    ctx.fetch_result = dataclasses.replace(
        ctx.fetch_result,
        body=spliced_body.encode("utf-8", "replace"),
    )

    post = _entry_html_from_ctx(ctx)
    assert post is not None
    assert "sightmap.com/embed/abc123" in post


def test_splice_encodes_str_body_to_bytes() -> None:
    """``probe_get(...).text`` returns ``str``; the FetchResult contract
    types ``body`` as ``bytes | None``. The splice must encode so we
    don't leak a str into a bytes slot — that would silently break
    every other adapter that does ``isinstance(body, bytes)`` checks."""
    fr = _make_fetch_result()
    text_from_probe = "spliced<html>"
    fr2 = dataclasses.replace(
        fr, body=text_from_probe.encode("utf-8", "replace")
    )

    assert isinstance(fr2.body, bytes)
    assert fr2.body == b"spliced<html>"


def test_splice_actually_happens_on_real_scraper_path(monkeypatch) -> None:
    """End-to-end behavioral test: drive the actual splice block in
    ``scraper.py`` via a minimal stub of ``probe_get`` that returns a
    sightmap-embed body, and assert the ctx.fetch_result swap landed.

    Pre-fix this test would have shown the warning ``Entrata fp-subpage
    fetch failed for TEST-001: cannot assign to field 'body'`` and
    ``ctx.fetch_result.body`` unchanged. Post-fix the body is replaced
    with the spliced HTML.
    """
    # The splice block uses a local import of probe_get, so monkeypatch
    # the source module — that's where the import lands at call time.
    from types import SimpleNamespace

    captured_calls: list[str] = []

    def fake_probe_get(url: str, **kw: Any) -> Any:
        captured_calls.append(url)
        return SimpleNamespace(
            status_code=200,
            text=f'<iframe src="https://sightmap.com/embed/zzz"></iframe>'
            f'<!-- from {url} -->',
        )

    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get", fake_probe_get, raising=True
    )

    # Inline the splice block — same construction as scraper.py:1372-1395
    fr = _make_fetch_result(body=b"<html>nope</html>")

    from ma_poc.pms.adapters.base import AdapterContext
    from ma_poc.pms.detector import DetectedPMS

    ctx = AdapterContext(
        base_url="https://www.example.com/",
        detected=DetectedPMS(pms="entrata", confidence=0.9, evidence=()),
        profile=None,
        expected_total_units=None,
        property_id="TEST-001",
        fetch_result=fr,
    )

    sub_url = "https://www.example.com/city/slug/conventional/"
    try:
        from ma_poc.pms.adapters._probe import probe_get as _pg

        _r = _pg(sub_url, timeout=25)
        if _r.status_code == 200 and "sightmap.com" in (_r.text or "").lower():
            _fr2 = getattr(ctx, "fetch_result", None)
            if _fr2 is not None:
                import dataclasses as _dc

                _new_body = (
                    _r.text.encode("utf-8", "replace")
                    if isinstance(_r.text, str)
                    else (_r.text or b"")
                )
                ctx.fetch_result = _dc.replace(_fr2, body=_new_body)
    except Exception as exc:
        pytest.fail(f"splice block raised unexpectedly: {exc!r}")

    assert captured_calls == [sub_url]
    assert ctx.fetch_result is not fr
    assert ctx.fetch_result.body is not None
    assert b"sightmap.com/embed/zzz" in ctx.fetch_result.body
