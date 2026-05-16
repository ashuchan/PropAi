"""RC5 — EMPTY_BODY outcome tests.

Validates that:
- FetchOutcome.EMPTY_BODY is a valid enum member
- It has the correct string value "EMPTY_BODY"
- It is not treated as a success (ok() returns False)
- It is not in should_carry_forward() (not retriable via carry-forward)
- scraper.py verdict prefix lookup resolves to FAILED_FETCH_EMPTY
"""

from __future__ import annotations

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode


def _make_result(outcome: FetchOutcome) -> FetchResult:
    return FetchResult(
        url="https://example.com/",
        outcome=outcome,
        status=200,
        body=None,
        headers={},
        render_mode=RenderMode.GET,
        final_url="https://example.com/",
        attempts=1,
        elapsed_ms=100,
        error_signature="EMPTY_BODY_200",
    )


def test_empty_body_outcome_exists() -> None:
    assert FetchOutcome.EMPTY_BODY == "EMPTY_BODY"


def test_empty_body_ok_is_false() -> None:
    result = _make_result(FetchOutcome.EMPTY_BODY)
    assert not result.ok()


def test_empty_body_should_carry_forward_is_false() -> None:
    result = _make_result(FetchOutcome.EMPTY_BODY)
    assert not result.should_carry_forward()


def test_dead_url_outcome_exists() -> None:
    assert FetchOutcome.DEAD_URL == "DEAD_URL"


def test_scraper_verdict_prefix_empty_body() -> None:
    """The _OUTCOME_VERDICT_PREFIX dict in scraper.py maps each
    ``FetchOutcome`` to a distinct verdict prefix.

    Bug #2 update (2026-05-16): the dict was extended from 2 entries
    (EMPTY_BODY, DEAD_URL) to cover all 7 non-OK outcomes so the
    analyzer can distinguish recoverable timeouts/rate-limits from
    permanent infra failures. The pre-fix behaviour of falling through
    to ``FAILED_UNREACHABLE`` for everything except EMPTY_BODY/DEAD_URL
    was actively harmful — operators couldn't tell a CF challenge from
    a DNS failure in the failures.csv. The lookup ``.get(<unknown>,
    "FAILED_UNREACHABLE")`` is still the right default for genuinely
    new outcomes the analyzer hasn't been taught about.
    """
    from ma_poc.pms.scraper import _OUTCOME_VERDICT_PREFIX
    assert _OUTCOME_VERDICT_PREFIX.get("EMPTY_BODY") == "FAILED_FETCH_EMPTY"
    assert _OUTCOME_VERDICT_PREFIX.get("DEAD_URL") == "FAILED_DEAD_URL"
    # Each of the seven additional outcomes from Bug #2 gets a unique prefix.
    assert _OUTCOME_VERDICT_PREFIX.get("CANCELLED") == "FAILED_TIMEOUT"
    assert _OUTCOME_VERDICT_PREFIX.get("RATE_LIMITED") == "FAILED_RATE_LIMITED"
    assert _OUTCOME_VERDICT_PREFIX.get("HARD_FAIL") == "FAILED_HARD_FAIL"
    assert _OUTCOME_VERDICT_PREFIX.get("PROXY_ERROR") == "FAILED_PROXY"
    assert _OUTCOME_VERDICT_PREFIX.get("BOT_BLOCKED") == "FAILED_BOT_BLOCKED"
    assert _OUTCOME_VERDICT_PREFIX.get("TRANSIENT") == "FAILED_TRANSIENT"
    # Truly unknown outcomes still hit the default.
    assert _OUTCOME_VERDICT_PREFIX.get("FUTURE_OUTCOME", "FAILED_UNREACHABLE") == "FAILED_UNREACHABLE"


def test_empty_body_is_non_ok_outcome() -> None:
    """EMPTY_BODY is not OK — scraper should short-circuit."""
    assert FetchOutcome.EMPTY_BODY != FetchOutcome.OK
    assert FetchOutcome.EMPTY_BODY != FetchOutcome.NOT_MODIFIED
