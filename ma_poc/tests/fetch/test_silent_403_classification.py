"""F3 — silent-403 / Cloudflare-edge → BOT_BLOCKED.

Verifies (a) Cloudflare-header detection, (b) silent-block discrimination,
(c) end-to-end classifier behaviour, and (d) H14 (legitimate 403 login
walls are NOT misclassified).

Spec deviation: this codebase has no ``FetchOutcome.SOFT_FAIL``. The
existing tier escalator already escalates on ``FetchOutcome.BOT_BLOCKED``
— that's the outcome silent 403s should produce. The functional fix
F3 ships is the **error_signature** changing from ``HTTP_403`` to
``BOT_BLOCKED`` for silent / Cloudflare-edge 403s, which is what
downstream telemetry keys off of.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ma_poc.fetch.fetcher import (
    FetchOutcome,
    _classify_fetch_outcome,
    _has_cloudflare_signature,
    _is_silent_block,
)

# parents[0]=fetch, [1]=tests, [2]=ma_poc, [3]=PropAi
REPO_ROOT = Path(__file__).resolve().parents[3]
VERDICT_PATH = REPO_ROOT / "docs" / "ANTIBOT_TLS_VERDICT.md"


def test_f3_requires_f2_verdict_present() -> None:
    """H2 ordering: F2 must have produced a verdict before F3 ships."""
    assert VERDICT_PATH.exists(), (
        f"Run `python -m ma_poc.scripts.diagnostics.tls_vs_ip_diagnostic` first. "
        f"Expected {VERDICT_PATH}"
    )
    head = VERDICT_PATH.read_text(encoding="utf-8").splitlines()[:5]
    assert any(ln.strip().startswith("verdict:") for ln in head)


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"server": "cloudflare"}, True),
        ({"Server": "Cloudflare"}, True),
        ({"server": "nginx", "cf-ray": "abc123"}, True),
        ({"cf-mitigated": "challenge"}, True),
        ({"server": "nginx"}, False),
        ({}, False),
    ],
)
def test_f3_cloudflare_signature_detection(headers: dict[str, str], expected: bool) -> None:
    assert _has_cloudflare_signature(headers) is expected


@pytest.mark.parametrize(
    "status,headers,body,expected",
    [
        (403, {"server": "cloudflare"}, b"", True),
        (403, {"cf-ray": "abc"}, b"<html>some content</html>", True),
        (403, {"server": "nginx"}, None, True),
        (403, {"server": "nginx"}, b"", True),
        (403, {"server": "nginx"}, b"   ", True),
        # H14 — legitimate 403 with substantive body
        (
            403,
            {"server": "nginx"},
            b"<html><form action='/login'>" + b"X" * 200 + b"</form></html>",
            False,
        ),
        # 200 unaffected
        (200, {"server": "cloudflare"}, b"", False),
        # 503 handled elsewhere
        (503, {"server": "cloudflare"}, b"", False),
    ],
)
def test_f3_is_silent_block_table(
    status: int,
    headers: dict[str, str],
    body: bytes | None,
    expected: bool,
) -> None:
    assert _is_silent_block(status, headers, body) is expected


def test_f3_403_empty_body_classified_bot_blocked() -> None:
    outcome, sig = _classify_fetch_outcome(403, {"server": "nginx"}, b"", None)
    # Spec deviation: existing FetchOutcome.BOT_BLOCKED is what triggers
    # escalation; the F3 win is the BOT_BLOCKED *signature* on a silent 403.
    assert outcome == FetchOutcome.BOT_BLOCKED
    assert sig == "BOT_BLOCKED"


def test_f3_cf_server_header_classified_bot_blocked() -> None:
    _, sig = _classify_fetch_outcome(403, {"server": "cloudflare"}, b"", None)
    assert sig == "BOT_BLOCKED"


def test_f3_legitimate_403_login_wall_not_misclassified() -> None:
    """H14 — 403 with substantive body and no CF header keeps HTTP_403 sig."""
    body = b"<html><body><h1>Sign in</h1>" + b"X" * 500 + b"</body></html>"
    _, sig = _classify_fetch_outcome(403, {"server": "nginx"}, body, None)
    assert sig != "BOT_BLOCKED"
    # Sanity — the existing classifier still emits HTTP_403 here.
    assert sig == "HTTP_403"
