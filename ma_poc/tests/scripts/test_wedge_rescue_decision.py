"""Tests for the wedge-rescue retry decision in ``scripts/runners/jugnu.py``.

The decision is a pure function — it inspects a property record's
``_meta`` dict (and the property's units count, passed as a flag) and
returns one of:

  - ``"RETRY"``                — kick off a HTTP-only retry of this PID
  - ``"SKIP_ENTRY_CAPTCHA"``   — entry was captcha-blocked; retrying
                                 with GET would only fetch the same
                                 captcha stub and trip the LLM-gate,
                                 corrupting the correct ``FAILED_UNREACHABLE``
                                 verdict. Skip and emit a telemetry event.
  - ``"NO_RETRY"``             — result is final / not wedge-prone.

The bug this guard fixes (PIDs 298969 thewattapts, 300327 flatson10th,
3188 thepointeatlapts, 55317 abodes on 2026-05-16): SiteGround SGCAPTCHA
properties returned an 11 KB captcha-challenge page on RENDER and got
correctly flagged as ``FAILED_UNREACHABLE``. The wedge-rescue retry then
fetched the same URL via HTTP GET, received a 215-byte captcha stub,
and re-emitted ``PROPERTY_EMITTED`` with verdict=FAILED_NO_DATA — that
later emit shadowed the correct UNREACHABLE classification.
"""

from __future__ import annotations

import pytest

from ma_poc.scripts.runners.jugnu import wedge_rescue_decision


class TestRetryDecision:
    def test_failed_unreachable_no_captcha_retries(self):
        """The wedge-prone case: timeout/cancel with no entry-captcha
        and no buffered units — kick off HTTP-only retry."""
        meta = {"verdict": "FAILED_UNREACHABLE"}
        assert wedge_rescue_decision(meta, has_units=False) == "RETRY"

    def test_partial_verdict_no_captcha_retries(self):
        """``PARTIAL`` (validation-majority-rejected) without surviving
        units also qualifies for retry — the original verdict is
        wedge-prone and we have nothing to lose."""
        meta = {"verdict": "PARTIAL"}
        assert wedge_rescue_decision(meta, has_units=False) == "RETRY"

    def test_success_partial_with_no_surviving_units_retries(self):
        """Edge case: ``SUCCESS_PARTIAL`` is normally emitted only when
        units were buffered. If somehow the units list is empty after
        timeout rescue (race / serialization quirk), still retry."""
        meta = {"verdict": "SUCCESS_PARTIAL"}
        assert wedge_rescue_decision(meta, has_units=False) == "RETRY"

    def test_partial_recovery_meta_flag_retries(self):
        """The ``partial_recovery=True`` meta flag is set by the
        timeout-rescue path. It's an independent signal from the verdict
        string — pre-fix the meta flag could be set even when verdict
        wasn't yet stamped, so this branch also qualifies for retry."""
        meta = {"verdict": "SUCCESS", "partial_recovery": True}
        assert wedge_rescue_decision(meta, has_units=False) == "RETRY"

    def test_failed_scrape_tier_retries(self):
        """``scrape_tier_used="FAILED"`` is the legacy marker stamped
        on records that exited the cascade without producing any tier
        win. Also wedge-prone."""
        meta = {"verdict": "SUCCESS", "scrape_tier_used": "FAILED"}
        assert wedge_rescue_decision(meta, has_units=False) == "RETRY"


class TestSkipEntryCaptcha:
    def test_failed_unreachable_with_captcha_skips(self):
        """The canonical SGCAPTCHA case — wedge-prone verdict + entry
        captcha — must return SKIP, not RETRY."""
        meta = {
            "verdict": "FAILED_UNREACHABLE",
            "entry_captcha_detected": True,
        }
        assert wedge_rescue_decision(meta, has_units=False) == "SKIP_ENTRY_CAPTCHA"

    def test_failed_unreachable_with_entry_bot_blocked_skips(self):
        """``entry_bot_blocked`` is a sibling flag stamped when the
        bot-block detector fired without an explicit captcha. Same
        retry-poisoning risk."""
        meta = {
            "verdict": "FAILED_UNREACHABLE",
            "entry_bot_blocked": True,
        }
        assert wedge_rescue_decision(meta, has_units=False) == "SKIP_ENTRY_CAPTCHA"

    def test_partial_verdict_with_captcha_skips(self):
        """Any wedge-prone verdict combined with entry-captcha skips."""
        meta = {
            "verdict": "PARTIAL",
            "entry_captcha_detected": True,
        }
        assert wedge_rescue_decision(meta, has_units=False) == "SKIP_ENTRY_CAPTCHA"

    def test_partial_recovery_flag_with_captcha_skips(self):
        meta = {
            "verdict": "SUCCESS",
            "partial_recovery": True,
            "entry_captcha_detected": True,
        }
        assert wedge_rescue_decision(meta, has_units=False) == "SKIP_ENTRY_CAPTCHA"


class TestNoRetry:
    def test_success_no_retry(self):
        """A clean SUCCESS doesn't need rescue."""
        meta = {"verdict": "SUCCESS"}
        assert wedge_rescue_decision(meta, has_units=True) == "NO_RETRY"

    def test_success_partial_with_units_no_retry(self):
        """``SUCCESS_PARTIAL`` with buffered units = we already have
        data, no point retrying the same property."""
        meta = {"verdict": "SUCCESS_PARTIAL"}
        assert wedge_rescue_decision(meta, has_units=True) == "NO_RETRY"

    def test_failed_no_data_no_retry(self):
        """``FAILED_NO_DATA`` is the extraction-side failure — the page
        loaded fine but no units were found. A HTTP-only retry won't
        change the extraction outcome on the same page."""
        meta = {"verdict": "FAILED_NO_DATA"}
        assert wedge_rescue_decision(meta, has_units=False) == "NO_RETRY"

    def test_partial_verdict_with_units_no_retry(self):
        """PARTIAL with units = we have SOME data already, even if
        the validation gate rejected most rows."""
        meta = {"verdict": "PARTIAL"}
        assert wedge_rescue_decision(meta, has_units=True) == "NO_RETRY"

    def test_failed_unreachable_with_units_no_retry(self):
        """Defensive: shouldn't happen in practice (UNREACHABLE means
        the fetch failed before extraction) but if a record somehow has
        both UNREACHABLE verdict and buffered units, we have data."""
        meta = {"verdict": "FAILED_UNREACHABLE"}
        assert wedge_rescue_decision(meta, has_units=True) == "NO_RETRY"

    def test_empty_meta_no_retry(self):
        """An empty meta dict (no verdict set) doesn't trigger retry."""
        assert wedge_rescue_decision({}, has_units=False) == "NO_RETRY"


class TestEdgeCases:
    @pytest.mark.parametrize(
        "verdict_str",
        ["failed_unreachable", "Failed_Unreachable", "FAILED_UNREACHABLE"],
    )
    def test_verdict_case_insensitive(self, verdict_str):
        """The function uppercases the verdict before lookup so the
        runner is robust to verdict-stamping casing drift."""
        meta = {"verdict": verdict_str}
        assert wedge_rescue_decision(meta, has_units=False) == "RETRY"

    def test_falsy_captcha_flag_treated_as_not_blocked(self):
        """``entry_captcha_detected=False`` (vs missing) must NOT skip
        retry. The captcha guard fires only on truthy values."""
        meta = {
            "verdict": "FAILED_UNREACHABLE",
            "entry_captcha_detected": False,
            "entry_bot_blocked": False,
        }
        assert wedge_rescue_decision(meta, has_units=False) == "RETRY"

    def test_captcha_skip_outranks_units_check(self):
        """SKIP_ENTRY_CAPTCHA only fires when no units AND wedge-prone
        AND captcha. With units present we go straight to NO_RETRY
        (we have data, captcha state irrelevant)."""
        meta = {
            "verdict": "FAILED_UNREACHABLE",
            "entry_captcha_detected": True,
        }
        assert wedge_rescue_decision(meta, has_units=True) == "NO_RETRY"


class TestSkipDomainQuarantined:
    """High-density Phase 2 (2026-05-18): domain-quarantine / rate-limit guard.

    A property whose entry-fetch hit DOMAIN_QUARANTINED_IN_RUN (or any
    RATE_LIMITED outcome) should not be retried — the GET retry would
    immediately return the same quarantine result (elapsed_ms=0) and emit a
    duplicate FAILED_UNREACHABLE, inflating the failure count without any
    hope of recovery.
    """

    def test_domain_quarantined_in_run_skips(self):
        """Canonical Essex 2026-05-18 trace: domain quarantined, wedge-prone
        verdict → SKIP, not RETRY."""
        meta = {
            "verdict": "FAILED_UNREACHABLE",
            "domain_quarantined_in_run": True,
        }
        assert wedge_rescue_decision(meta, has_units=False) == "SKIP_DOMAIN_QUARANTINED"

    def test_entry_rate_limited_skips(self):
        """Entry fetch returned RATE_LIMITED but domain not yet fully quarantined.
        Retrying with GET will also be rate-limited — skip."""
        meta = {
            "verdict": "FAILED_UNREACHABLE",
            "entry_rate_limited": True,
        }
        assert wedge_rescue_decision(meta, has_units=False) == "SKIP_DOMAIN_QUARANTINED"

    def test_quarantined_with_units_is_no_retry(self):
        """If units were already buffered, has_units=True gates to NO_RETRY
        regardless of quarantine flag — we have data, no rescue needed."""
        meta = {
            "verdict": "FAILED_UNREACHABLE",
            "domain_quarantined_in_run": True,
        }
        assert wedge_rescue_decision(meta, has_units=True) == "NO_RETRY"

    def test_captcha_takes_precedence_over_quarantine(self):
        """When both captcha and quarantine flags are set, SKIP_ENTRY_CAPTCHA
        fires first (captcha check comes before quarantine check in the function).
        Both are skip paths so the order doesn't matter for correctness, but
        it must be deterministic."""
        meta = {
            "verdict": "FAILED_UNREACHABLE",
            "entry_captcha_detected": True,
            "domain_quarantined_in_run": True,
        }
        assert wedge_rescue_decision(meta, has_units=False) == "SKIP_ENTRY_CAPTCHA"

    def test_falsy_quarantine_flag_does_not_skip(self):
        """``domain_quarantined_in_run=False`` (explicitly set but falsy)
        must NOT trigger the skip — only truthy values qualify."""
        meta = {
            "verdict": "FAILED_UNREACHABLE",
            "domain_quarantined_in_run": False,
            "entry_rate_limited": False,
        }
        assert wedge_rescue_decision(meta, has_units=False) == "RETRY"

    def test_partial_verdict_quarantined_skips(self):
        """Quarantine flag applies to any wedge-prone verdict, not just
        FAILED_UNREACHABLE."""
        meta = {
            "verdict": "PARTIAL",
            "domain_quarantined_in_run": True,
        }
        assert wedge_rescue_decision(meta, has_units=False) == "SKIP_DOMAIN_QUARANTINED"
