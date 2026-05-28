"""TRANSIENT/BOT_BLOCKED salvage via curl_cffi (2026-05-28).

The 2026-05-27 c612 canary residue surfaced ~10 truth=Y properties where
Playwright returned outcome=TRANSIENT (9) or BOT_BLOCKED (1), but a direct
curl_cffi chrome120 probe returned HTTP 200 + 50-200 KB body in <1 second.
``scrape_jugnu`` used to short-circuit them all as
``generic:no_body_short_circuit`` — dropping valid units.

The salvage block (in scrape_jugnu, just before the existing short-circuit
return) makes one curl_cffi attempt and, if the body is ≥5 KB at status 200,
rewrites ``fetch_result`` to outcome=OK so the normal extraction pipeline
runs. Soft-404 (DEAD_URL) recovery still wins precedence.

The salvage block runs inside scrape_jugnu and is followed by full
``scrape()`` execution — which hits network in unit tests. To keep tests
hermetic, we (a) source-grep pin the production hook (symbols + gate
expression), and (b) decision-mirror the salvage logic via a helper and
exercise the decision matrix directly.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode


_SCRAPER_PATH = Path(__file__).resolve().parents[2] / "pms" / "scraper.py"


@dataclass
class _MockResp:
    status_code: int
    text: str
    url: str = "https://example.com"


def _mirror_salvage(
    fetch_result: FetchResult,
    soft_404_recovery: bool,
    cc_get: Any,
    base_url: str,
) -> tuple[FetchResult, bool]:
    """Mirror of the production TRANSIENT/BOT_BLOCKED salvage block.

    Returns (possibly rewritten fetch_result, salvage_fired). Kept in
    sync with the production hook via the source-grep contract test
    below.
    """
    outcome_val = fetch_result.outcome.value
    if outcome_val not in ("TRANSIENT", "BOT_BLOCKED"):
        return fetch_result, False
    if soft_404_recovery:
        return fetch_result, False
    try:
        r = cc_get(
            base_url,
            impersonate="chrome120",
            timeout=12,
            verify=False,
            allow_redirects=True,
        )
        body_text = r.text if isinstance(r.text, str) else ""
        if r.status_code == 200 and len(body_text) >= 5000:
            body_bytes = body_text.encode("utf-8", errors="replace")
            new_fr = replace(
                fetch_result,
                outcome=FetchOutcome.OK,
                status=200,
                body=body_bytes,
                final_url=str(getattr(r, "url", None) or base_url),
                error_signature="salvaged_via_curl_cffi",
            )
            return new_fr, True
    except Exception:
        pass
    return fetch_result, False


def _fr(outcome: FetchOutcome) -> FetchResult:
    return FetchResult(
        url="https://example.com",
        outcome=outcome,
        status=None,
        body=None,
        headers={},
        render_mode=RenderMode.RENDER,
        final_url="https://example.com",
        attempts=1,
        elapsed_ms=120,
    )


# ─────────────────────────────────────────────────────────────────────
# Hook contract — pin the production symbols so a future refactor that
# drops the salvage block fails loudly.
# ─────────────────────────────────────────────────────────────────────


def test_salvage_hook_symbols_present_in_scraper() -> None:
    src = _SCRAPER_PATH.read_text(encoding="utf-8")
    for symbol in (
        "_transient_salvage",
        "salvaged_via_curl_cffi",
        'outcome_val in ("TRANSIENT", "BOT_BLOCKED")',
        "from curl_cffi import requests as _cc",
        "impersonate=\"chrome120\"",
    ):
        assert symbol in src, (
            f"scraper.py no longer references {symbol!r} — TRANSIENT "
            f"salvage hook is out of sync with this test helper."
        )


def test_salvage_block_lives_before_short_circuit() -> None:
    """The salvage block must run BEFORE the no_body_short_circuit return
    — otherwise the salvage is dead code."""
    src = _SCRAPER_PATH.read_text(encoding="utf-8")
    salvage_idx = src.index("_transient_salvage")
    short_circuit_idx = src.index('"generic:no_body_short_circuit"')
    assert salvage_idx < short_circuit_idx, (
        "salvage block must appear before the short-circuit return"
    )


def test_salvage_gates_on_soft_404_recovery() -> None:
    """The salvage gate must short-circuit when _soft_404_recovery is
    True — DEAD_URL path takes precedence."""
    src = _SCRAPER_PATH.read_text(encoding="utf-8")
    assert "and not _soft_404_recovery" in src, (
        "salvage must defer to the existing _soft_404_recovery path"
    )


# ─────────────────────────────────────────────────────────────────────
# Salvage decision logic.
# ─────────────────────────────────────────────────────────────────────


def test_transient_salvage_flips_to_ok_on_200_with_body() -> None:
    body = "<html><body>" + ("a" * 60_000) + "</body></html>"
    cc_get = lambda *a, **kw: _MockResp(200, body)  # noqa: E731
    new_fr, fired = _mirror_salvage(_fr(FetchOutcome.TRANSIENT), False, cc_get, "https://example.com")
    assert fired is True
    assert new_fr.outcome == FetchOutcome.OK
    assert new_fr.status == 200
    assert new_fr.body is not None and len(new_fr.body) >= 60_000
    assert new_fr.error_signature == "salvaged_via_curl_cffi"


def test_transient_salvage_skipped_on_404_response() -> None:
    cc_get = lambda *a, **kw: _MockResp(404, "Not Found")  # noqa: E731
    new_fr, fired = _mirror_salvage(_fr(FetchOutcome.TRANSIENT), False, cc_get, "https://example.com")
    assert fired is False
    assert new_fr.outcome == FetchOutcome.TRANSIENT  # unchanged


def test_transient_salvage_skipped_on_thin_body() -> None:
    """200 OK but <5000 bytes is the thin-body guard — don't promote a
    404-error-page that happens to return 200."""
    cc_get = lambda *a, **kw: _MockResp(200, "<html><body>tiny</body></html>")  # noqa: E731
    new_fr, fired = _mirror_salvage(_fr(FetchOutcome.TRANSIENT), False, cc_get, "https://example.com")
    assert fired is False
    assert new_fr.outcome == FetchOutcome.TRANSIENT


def test_transient_salvage_swallows_exceptions() -> None:
    """curl_cffi raising must not leak out of the salvage block — fall
    through cleanly to the short-circuit."""
    def cc_get(*a, **kw):
        raise RuntimeError("boom")
    new_fr, fired = _mirror_salvage(_fr(FetchOutcome.TRANSIENT), False, cc_get, "https://example.com")
    assert fired is False
    assert new_fr.outcome == FetchOutcome.TRANSIENT


def test_bot_blocked_salvage_also_fires() -> None:
    """BOT_BLOCKED is in the salvage gate (one canary prop: sparkleam.com)."""
    body = "<html><body>" + ("b" * 60_000) + "</body></html>"
    cc_get = lambda *a, **kw: _MockResp(200, body)  # noqa: E731
    new_fr, fired = _mirror_salvage(_fr(FetchOutcome.BOT_BLOCKED), False, cc_get, "https://example.com")
    assert fired is True
    assert new_fr.outcome == FetchOutcome.OK


def test_dead_url_with_soft_404_skips_salvage() -> None:
    """Soft-404 recovery path (DEAD_URL with substantive body) must take
    precedence — salvage must not run when soft_404_recovery=True. Also
    salvage is scoped to TRANSIENT/BOT_BLOCKED, so DEAD_URL itself
    bypasses the gate."""
    cc_get_calls = []

    def cc_get(*a, **kw):
        cc_get_calls.append(1)
        return _MockResp(200, "x" * 60_000)

    # soft_404_recovery=True with TRANSIENT outcome → no salvage call.
    new_fr, fired = _mirror_salvage(_fr(FetchOutcome.TRANSIENT), True, cc_get, "https://example.com")
    assert fired is False
    assert not cc_get_calls

    # DEAD_URL outcome → no salvage call (out of gate).
    new_fr, fired = _mirror_salvage(_fr(FetchOutcome.DEAD_URL), False, cc_get, "https://example.com")
    assert fired is False
    assert not cc_get_calls


def test_ok_outcome_does_not_trigger_salvage() -> None:
    """outcome=OK already has a good body — salvage must not fire."""
    cc_get_calls = []

    def cc_get(*a, **kw):
        cc_get_calls.append(1)
        return _MockResp(200, "x" * 60_000)

    new_fr, fired = _mirror_salvage(_fr(FetchOutcome.OK), False, cc_get, "https://example.com")
    assert fired is False
    assert not cc_get_calls
    assert new_fr.outcome == FetchOutcome.OK


# The integration paths (running scrape_jugnu end-to-end) are covered
# elsewhere; the salvage-specific behavior is fully pinned above via the
# decision-mirror tests + source-grep contract tests.
