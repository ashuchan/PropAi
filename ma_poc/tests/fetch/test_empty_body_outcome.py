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
    """The _OUTCOME_VERDICT_PREFIX dict in scraper.py maps EMPTY_BODY → FAILED_FETCH_EMPTY."""
    from ma_poc.pms.scraper import _OUTCOME_VERDICT_PREFIX
    assert _OUTCOME_VERDICT_PREFIX.get("EMPTY_BODY") == "FAILED_FETCH_EMPTY"
    assert _OUTCOME_VERDICT_PREFIX.get("DEAD_URL") == "FAILED_DEAD_URL"
    assert _OUTCOME_VERDICT_PREFIX.get("BOT_BLOCKED", "FAILED_UNREACHABLE") == "FAILED_UNREACHABLE"


def test_empty_body_is_non_ok_outcome() -> None:
    """EMPTY_BODY is not OK — scraper should short-circuit."""
    assert FetchOutcome.EMPTY_BODY != FetchOutcome.OK
    assert FetchOutcome.EMPTY_BODY != FetchOutcome.NOT_MODIFIED
