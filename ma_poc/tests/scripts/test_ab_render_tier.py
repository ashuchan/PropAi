"""Tests for the 2a render-tier A/B harness aggregation."""
from __future__ import annotations

from ma_poc.scripts.ab_render_tier import ArmResult, UrlResult


def _r(outcome: str, *, captcha: bool = False, ms: int = 1000) -> UrlResult:
    return UrlResult(
        pid="P", url="u", outcome=outcome, status=None,
        captcha=captcha, block_signature=None, elapsed_ms=ms,
    )


def test_summary_computes_rates() -> None:
    arm = ArmResult(name="chromium/geo-off")
    arm.results = [
        _r("OK", ms=1000),
        _r("OK", ms=2000),
        _r("BOT_BLOCKED", captcha=True, ms=3000),
        _r("TRANSIENT", ms=4000),
    ]
    s = arm.summary()
    assert s["arm"] == "chromium/geo-off"
    assert s["n"] == 4
    assert s["ok"] == 2
    assert s["pass_rate"] == 0.5
    assert s["blocked"] == 1
    assert s["captcha_abort"] == 1
    assert s["errored"] == 1
    assert s["median_ms"] == 3000  # sorted [1000,2000,3000,4000], index n//2 == 2


def test_summary_empty_is_safe() -> None:
    s = ArmResult(name="x").summary()
    assert s["n"] == 0 and s["pass_rate"] == 0.0
