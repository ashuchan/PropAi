"""F2 — diagnostic logic tests (no network).

Verifies the per-URL classifier and aggregate-verdict reduction. Does
not invoke httpx or curl_cffi — those are exercised manually via the
diagnostic CLI; their output produces ``docs/ANTIBOT_TLS_VERDICT.md``.
"""

from __future__ import annotations

import importlib

import pytest

mod = importlib.import_module("ma_poc.scripts.diagnostics.tls_vs_ip_diagnostic")


@pytest.mark.parametrize(
    "a,b,expected",
    [
        (403, 200, "TLS_FINGERPRINT"),
        (403, 403, "IP_REPUTATION"),
        (200, 200, "NOT_REPRODUCIBLE"),
        (200, 403, "UNEXPECTED"),
        (-1, 200, "ERROR"),
    ],
)
def test_f2_classify_pair(a: int, b: int, expected: str) -> None:
    assert mod._classify_pair(a, b) == expected


@pytest.mark.parametrize(
    "verdicts,expected",
    [
        (["TLS_FINGERPRINT"] * 5 + ["IP_REPUTATION"], "TLS_FINGERPRINT"),
        (["IP_REPUTATION"] * 6, "IP_REPUTATION"),
        (["TLS_FINGERPRINT"] * 3 + ["IP_REPUTATION"] * 3, "MIXED"),
        (["NOT_REPRODUCIBLE"] * 5 + ["TLS_FINGERPRINT"], "NOT_REPRODUCIBLE"),
        (["TLS_FINGERPRINT", "IP_REPUTATION", "ERROR", "OTHER(a=429,b=429)"], "INCONCLUSIVE"),
    ],
)
def test_f2_aggregate_verdict(verdicts: list[str], expected: str) -> None:
    assert mod._aggregate_verdict(verdicts) == expected


def test_f2_diagnostic_urls_count_locked() -> None:
    """Drift guard (§8.5): the diagnostic URL list must stay at exactly 6."""
    assert len(mod.DIAGNOSTIC_URLS) == 6
