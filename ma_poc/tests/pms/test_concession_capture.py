"""Integration tests for the property-level concession capture in scraper.py.

Covers:

  * ``_capture_concession_from_html``: script/style/noscript body
    strip, sentence-extend for banner-header rows, 300-char cap,
    no-match return None.
  * ``_probe_specials_pages``: probes the fixed path list, returns
    the first match, silent failures on timeout / 4xx / 5xx /
    network error.
  * Source-grep pins: the script-strip and sentence-extend code
    paths are present (catches refactor regression).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# ─────────────────────────────────────────────────────────────────────
# _capture_concession_from_html — text capture pipeline
# ─────────────────────────────────────────────────────────────────────


class TestCaptureFromHtml:
    def test_clean_html_capture(self) -> None:
        from ma_poc.pms.scraper import _capture_concession_from_html

        html = "<html><body><h1>Welcome</h1><div class='banner'>Get 2 months free rent today!</div></body></html>"
        result = _capture_concession_from_html(html)
        assert result is not None
        assert "2 months free rent" in result

    def test_script_body_does_not_leak_into_capture(self) -> None:
        """Script body is stripped BEFORE the tag-strip flatten — the
        canary's #1 bug. Capture window must not contain JS code."""
        from ma_poc.pms.scraper import _capture_concession_from_html

        html = (
            "<html><body>"
            "<script>"
            "if (href.indexOf('?') == -1) { el.setAttribute('href', 'x'); } "
            "var PropLeadSource = function() { return 'foo'; };"
            "</script>"
            "<div class='banner'>2 months free rent on a 13-month lease!</div>"
            "</body></html>"
        )
        result = _capture_concession_from_html(html)
        assert result is not None
        assert "2 months free rent" in result
        # The JS body must not appear in the captured snippet.
        assert "indexOf" not in result
        assert "PropLeadSource" not in result
        assert "setAttribute" not in result

    def test_style_body_does_not_leak_into_capture(self) -> None:
        from ma_poc.pms.scraper import _capture_concession_from_html

        html = (
            "<html><body>"
            "<style>"
            ".banner { padding: 12px; background-color: #fff; !important; }"
            "</style>"
            "<div class='banner'>Save $500 today!</div>"
            "</body></html>"
        )
        result = _capture_concession_from_html(html)
        assert result is not None
        assert "$500" in result
        assert "padding" not in result
        assert "background-color" not in result

    def test_noscript_body_stripped(self) -> None:
        from ma_poc.pms.scraper import _capture_concession_from_html

        html = (
            "<html><body>"
            "<noscript>JavaScript is required for this experience.</noscript>"
            "<div>Limited Time Offer! Get 1 month free rent on signing!</div>"
            "</body></html>"
        )
        result = _capture_concession_from_html(html)
        assert result is not None
        assert "JavaScript" not in result

    def test_header_only_extended_forward_to_body(self) -> None:
        """Sentence-extend: header + body sentence must merge under 300
        chars when the body lives in the next sentence."""
        from ma_poc.pms.scraper import _capture_concession_from_html

        # Banner header in one sentence, body in the next — without the
        # sentence-extend fix, single-sentence pick drops the body.
        html = (
            "<html><body>"
            "<div>Limited Time Offer! Move in by 6/15 and get 1 month free rent today.</div>"
            "</body></html>"
        )
        result = _capture_concession_from_html(html)
        assert result is not None
        # The body sentence must be present alongside the header.
        assert "1 month free rent" in result
        assert "Limited Time Offer" in result

    def test_300_char_cap_respected(self) -> None:
        from ma_poc.pms.scraper import _capture_concession_from_html

        # Massive run-on text — capture must cap.
        long_text = "Get 2 months free rent! " + ("Restrictions apply. " * 50)
        html = f"<html><body><div>{long_text}</div></body></html>"
        result = _capture_concession_from_html(html)
        assert result is not None
        assert len(result) <= 300

    def test_no_match_returns_none(self) -> None:
        from ma_poc.pms.scraper import _capture_concession_from_html

        html = "<html><body>Welcome to our beautiful community.</body></html>"
        assert _capture_concession_from_html(html) is None

    def test_empty_or_invalid_input(self) -> None:
        from ma_poc.pms.scraper import _capture_concession_from_html

        assert _capture_concession_from_html("") is None
        assert _capture_concession_from_html(None) is None  # type: ignore[arg-type]
        assert _capture_concession_from_html(123) is None  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────
# _probe_specials_pages — URL discovery via the L1 stealth fetcher
# ─────────────────────────────────────────────────────────────────────


def _make_fake_fetch_result(
    *,
    url: str,
    outcome_ok: bool = True,
    body: bytes | None = None,
    captcha: bool = False,
):
    """Build a FetchResult matching the L1 contract."""
    from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode

    return FetchResult(
        url=url,
        outcome=FetchOutcome.OK if outcome_ok else FetchOutcome.BOT_BLOCKED,
        status=200 if outcome_ok else 403,
        body=body,
        headers={},
        render_mode=RenderMode.GET,
        final_url=url,
        attempts=1,
        elapsed_ms=10,
        captcha_detected=captcha,
    )


@pytest.mark.asyncio
async def test_specials_probe_routes_through_jugnu_fetch(monkeypatch: Any) -> None:
    """First successful response wins; probe issues a CrawlTask per path."""
    from ma_poc.pms import scraper

    captured_tasks: list = []

    async def _fake_fetch(task):
        captured_tasks.append(task)
        if task.url.endswith("/specials"):
            return _make_fake_fetch_result(
                url=task.url,
                outcome_ok=True,
                body=b"<html><body><div>2 months free on select units!</div></body></html>",
            )
        return _make_fake_fetch_result(url=task.url, outcome_ok=False)

    monkeypatch.setattr("ma_poc.fetch.fetch", _fake_fetch)

    snippet, source_url = await scraper._probe_specials_pages(
        "https://x.test/", property_id="P-001"
    )
    assert snippet is not None
    assert "2 months free" in snippet
    assert source_url == "https://x.test/specials"
    # Every task carries the property_id sticky-key for identity selection.
    assert all(t.property_id == "P-001" for t in captured_tasks)
    # The probe walks the canonical _SPECIALS_PATHS list.
    probed_paths = [t.url.replace("https://x.test", "") for t in captured_tasks]
    assert "/specials" in probed_paths


@pytest.mark.asyncio
async def test_specials_probe_skips_captcha_detected(monkeypatch: Any) -> None:
    """A captcha-flagged response must NOT feed the concession regex.

    Real-world bug class: feeding a Cloudflare "Just a moment..."
    interstitial into the regex returns false positives like "Just a"
    that look like offer text. The L1 captcha_detected flag is the
    canonical signal — when True, skip the body even on a 200.
    """
    from ma_poc.pms import scraper

    async def _fake_fetch(task):
        # Every path returns a captcha-flagged 200 with body that
        # contains the word "free" — should NOT be picked up.
        return _make_fake_fetch_result(
            url=task.url,
            outcome_ok=True,
            body=b"<html>Just a moment... 2 months free</html>",
            captcha=True,
        )

    monkeypatch.setattr("ma_poc.fetch.fetch", _fake_fetch)

    snippet, source_url = await scraper._probe_specials_pages(
        "https://x.test/", property_id="P-001"
    )
    assert snippet is None
    assert source_url is None


@pytest.mark.asyncio
async def test_specials_probe_skips_non_ok(monkeypatch: Any) -> None:
    """403 / 503 / DEAD_URL outcomes must be skipped silently."""
    from ma_poc.pms import scraper

    async def _fake_fetch(task):
        return _make_fake_fetch_result(url=task.url, outcome_ok=False)

    monkeypatch.setattr("ma_poc.fetch.fetch", _fake_fetch)

    snippet, source_url = await scraper._probe_specials_pages(
        "https://x.test/", property_id="P-001"
    )
    assert snippet is None
    assert source_url is None


@pytest.mark.asyncio
async def test_specials_probe_handles_invalid_url() -> None:
    from ma_poc.pms import scraper

    # Invalid URL — return None silently.
    snippet, source_url = await scraper._probe_specials_pages("")
    assert snippet is None
    assert source_url is None


@pytest.mark.asyncio
async def test_specials_probe_silent_on_fetcher_exception(monkeypatch: Any) -> None:
    """Probe must not propagate exceptions from jugnu_fetch."""
    from ma_poc.pms import scraper

    async def _raise(task):
        raise OSError("network down")

    monkeypatch.setattr("ma_poc.fetch.fetch", _raise)

    snippet, source_url = await scraper._probe_specials_pages(
        "https://x.test/", property_id="P-001"
    )
    assert snippet is None
    assert source_url is None


# ─────────────────────────────────────────────────────────────────────
# Early-exit cap + telemetry — production safety net
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_specials_probe_respects_max_paths_cap(monkeypatch: Any) -> None:
    """``max_paths=N`` MUST bound the number of paths actually fetched.

    Real-world: a property whose `/specials` returns captcha will burn
    ``len(_SPECIALS_PATHS) * timeout`` seconds without the cap. With
    ``max_paths=4`` (default) the worst-case is bounded to 4 × timeout.
    """
    from ma_poc.pms import scraper

    fetched_urls: list[str] = []

    async def _fake_fetch(task):
        fetched_urls.append(task.url)
        return _make_fake_fetch_result(url=task.url, outcome_ok=False)

    monkeypatch.setattr("ma_poc.fetch.fetch", _fake_fetch)

    await scraper._probe_specials_pages(
        "https://x.test/", property_id="P-001", max_paths=4
    )
    assert len(fetched_urls) == 4, f"cap not honored: {fetched_urls}"

    # And verify a different cap value is also honored.
    fetched_urls.clear()
    await scraper._probe_specials_pages(
        "https://x.test/", property_id="P-001", max_paths=2
    )
    assert len(fetched_urls) == 2

    # Default cap is 4.
    fetched_urls.clear()
    await scraper._probe_specials_pages(
        "https://x.test/", property_id="P-001"
    )
    assert len(fetched_urls) == 4


@pytest.mark.asyncio
async def test_specials_probe_first_success_short_circuits_cap(monkeypatch: Any) -> None:
    """First success returns immediately — cap doesn't force more attempts."""
    from ma_poc.pms import scraper

    fetched_urls: list[str] = []

    async def _fake_fetch(task):
        fetched_urls.append(task.url)
        # First path returns a valid concession; rest would 404.
        if task.url.endswith("/specials"):
            return _make_fake_fetch_result(
                url=task.url,
                outcome_ok=True,
                body=b"<html><body>2 months free rent!</body></html>",
            )
        return _make_fake_fetch_result(url=task.url, outcome_ok=False)

    monkeypatch.setattr("ma_poc.fetch.fetch", _fake_fetch)

    snippet, _ = await scraper._probe_specials_pages(
        "https://x.test/", property_id="P-001", max_paths=4
    )
    assert snippet is not None
    # Only one fetch happened — short-circuit on first success.
    assert len(fetched_urls) == 1


@pytest.mark.asyncio
async def test_specials_probe_emits_hop_captcha_event(monkeypatch: Any) -> None:
    """Every captcha-detected hop emits HOP_CAPTCHA_DETECTED.

    Production telemetry uses this to measure hop-captcha rate without
    URL-pattern filtering on the much-noisier FETCH_CAPTCHA_DETECTED.
    """
    from ma_poc.observability.events import EventKind
    from ma_poc.pms import scraper

    captured: list[tuple] = []

    def _fake_emit(kind, pid, **data):
        captured.append((kind, pid, data))

    async def _fake_fetch(task):
        # Every probed path returns a captcha-flagged response.
        return _make_fake_fetch_result(
            url=task.url,
            outcome_ok=True,
            body=b"<html>Just a moment...</html>",
            captcha=True,
        )

    monkeypatch.setattr("ma_poc.fetch.fetch", _fake_fetch)
    monkeypatch.setattr("ma_poc.observability.events.emit", _fake_emit)

    await scraper._probe_specials_pages(
        "https://x.test/", property_id="P-CAPTCHA", max_paths=3
    )

    hop_captcha_events = [
        d for (k, _pid, d) in captured if k == EventKind.HOP_CAPTCHA_DETECTED
    ]
    # One HOP_CAPTCHA_DETECTED per probed path.
    assert len(hop_captcha_events) == 3
    # Payload schema.
    for d in hop_captcha_events:
        assert d["context"] == "specials_probe"
        assert "url" in d


@pytest.mark.asyncio
async def test_specials_probe_emits_concession_probe_result_found(monkeypatch: Any) -> None:
    """Per-property terminal event fires once with outcome=found."""
    from ma_poc.observability.events import EventKind
    from ma_poc.pms import scraper

    captured: list[tuple] = []

    def _fake_emit(kind, pid, **data):
        captured.append((kind, pid, data))

    async def _fake_fetch(task):
        if task.url.endswith("/specials"):
            return _make_fake_fetch_result(
                url=task.url,
                outcome_ok=True,
                body=b"<html><body>1 month free rent</body></html>",
            )
        return _make_fake_fetch_result(url=task.url, outcome_ok=False)

    monkeypatch.setattr("ma_poc.fetch.fetch", _fake_fetch)
    monkeypatch.setattr("ma_poc.observability.events.emit", _fake_emit)

    await scraper._probe_specials_pages(
        "https://x.test/", property_id="P-OK", max_paths=4
    )

    result_events = [
        d for (k, _pid, d) in captured if k == EventKind.CONCESSION_PROBE_RESULT
    ]
    assert len(result_events) == 1
    assert result_events[0]["outcome"] == "found"
    assert result_events[0]["paths_attempted"] == 1


@pytest.mark.asyncio
async def test_specials_probe_emits_concession_probe_result_all_blocked(monkeypatch: Any) -> None:
    """All-captcha probe emits outcome=all_blocked — the WAF escalation signal."""
    from ma_poc.observability.events import EventKind
    from ma_poc.pms import scraper

    captured: list[tuple] = []

    def _fake_emit(kind, pid, **data):
        captured.append((kind, pid, data))

    async def _fake_fetch(task):
        return _make_fake_fetch_result(
            url=task.url,
            outcome_ok=True,
            body=b"<html>Just a moment...</html>",
            captcha=True,
        )

    monkeypatch.setattr("ma_poc.fetch.fetch", _fake_fetch)
    monkeypatch.setattr("ma_poc.observability.events.emit", _fake_emit)

    await scraper._probe_specials_pages(
        "https://x.test/", property_id="P-BLOCKED", max_paths=3
    )

    result_events = [
        d for (k, _pid, d) in captured if k == EventKind.CONCESSION_PROBE_RESULT
    ]
    assert len(result_events) == 1
    assert result_events[0]["outcome"] == "all_blocked"
    assert result_events[0]["paths_attempted"] == 3
    assert result_events[0]["captcha_count"] == 3


@pytest.mark.asyncio
async def test_specials_probe_emits_concession_probe_result_exhausted(monkeypatch: Any) -> None:
    """All-404 probe emits outcome=exhausted — distinct from all_blocked."""
    from ma_poc.observability.events import EventKind
    from ma_poc.pms import scraper

    captured: list[tuple] = []

    def _fake_emit(kind, pid, **data):
        captured.append((kind, pid, data))

    async def _fake_fetch(task):
        return _make_fake_fetch_result(url=task.url, outcome_ok=False)

    monkeypatch.setattr("ma_poc.fetch.fetch", _fake_fetch)
    monkeypatch.setattr("ma_poc.observability.events.emit", _fake_emit)

    await scraper._probe_specials_pages(
        "https://x.test/", property_id="P-404", max_paths=3
    )

    result_events = [
        d for (k, _pid, d) in captured if k == EventKind.CONCESSION_PROBE_RESULT
    ]
    assert len(result_events) == 1
    # No captchas — pure exhaustion via non-OK responses.
    assert result_events[0]["outcome"] == "exhausted"
    assert result_events[0]["captcha_count"] == 0


@pytest.mark.asyncio
async def test_specials_probe_not_modified_falls_through_to_stealth_probe(
    monkeypatch: Any,
) -> None:
    """L1 NOT_MODIFIED (304, empty body) must fall through to stealth_probe.

    Real-world bug: L1's conditional-GET cache stores an ETag from the
    prior probe. The next probe sends ``If-None-Match`` with that ETag,
    the server matches and returns 304 with empty body, and L1 reports
    ``outcome=NOT_MODIFIED, body=None``. For an entry-page fetch this
    is the carry-forward signal; for a one-shot concession probe it's
    a coverage loss — we need the CURRENT body to re-scan. The fix
    re-fetches via stealth_probe (no conditional cache) on NOT_MODIFIED.
    """
    from dataclasses import replace as _dc_replace

    from ma_poc.fetch.contracts import FetchOutcome
    from ma_poc.pms import scraper

    async def _fake_fetch(task):
        # Every path returns NOT_MODIFIED with no body — simulates the
        # canary regression where L1's cache was warm from a prior run.
        return _dc_replace(
            _make_fake_fetch_result(url=task.url),
            outcome=FetchOutcome.NOT_MODIFIED,
            status=304,
            body=None,
        )

    monkeypatch.setattr("ma_poc.fetch.fetch", _fake_fetch)

    # stealth_probe returns the actual body — proves the fallback fired.
    async def _fake_stealth(url, **kwargs):
        if url.endswith("/specials"):
            return (
                b"<html><body><div>1 month free rent on signing!</div></body></html>",
                200,
                None,
            )
        return None, 404, None

    monkeypatch.setattr("ma_poc.fetch.probe.stealth_probe", _fake_stealth)

    snippet, source_url = await scraper._probe_specials_pages(
        "https://x.test/", property_id="P-001"
    )
    assert snippet is not None
    assert "1 month free" in snippet


# ─────────────────────────────────────────────────────────────────────
# Source-grep pins — catch refactor regression
# ─────────────────────────────────────────────────────────────────────


def test_script_strip_pattern_present_in_scraper() -> None:
    """The script/style/noscript body-strip regex must remain in
    scraper.py. If a future refactor removes it, the JS/CSS leak
    returns and ~50% of canary captures get polluted again."""
    src = Path(__file__).resolve().parents[2] / "pms" / "scraper.py"
    text = src.read_text(encoding="utf-8")
    assert "<(script|style|noscript)" in text


def test_sentence_extend_pattern_present_in_scraper() -> None:
    """The forward sentence-extend (parts[idx + 1: idx + 3]) must
    remain so banner-header rows pick up the body sentence."""
    src = Path(__file__).resolve().parents[2] / "pms" / "scraper.py"
    text = src.read_text(encoding="utf-8")
    assert "parts[idx + 1: idx + 3]" in text or "parts[idx + 1:idx + 3]" in text
