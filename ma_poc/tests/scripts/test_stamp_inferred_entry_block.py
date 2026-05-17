"""Tests for ``stamp_inferred_entry_block`` — the helper that retroactively
flags a property's entry as bot-blocked when wedge-rescue surfaced a WAF
response.

Shard_10 fix (2026-05-17) background: the entry RENDER fetch is killed
by the per-property wallclock with ``body=None``. ``looks_like_captcha``
in ``fetch/fetcher.py:329`` only runs ``if result.body``, so the
captcha flag is never set. ``wedge_rescue_decision`` reads False and
returns RETRY, the rescue's HTTP-only GET then immediately hits the
WAF and classifies BOT_BLOCKED. The helper backfills the entry's meta
from that rescue evidence so:
  * Telemetry counts captcha-blocked properties correctly.
  * Subsequent rescue passes (architecture allows them) skip via
    SKIP_ENTRY_CAPTCHA.
  * Verdict reasons can be made specific in downstream analysers.
"""

from __future__ import annotations

from ma_poc.scripts.runners.jugnu import stamp_inferred_entry_block


class TestStamps:
    def test_bot_blocked_only_stamps_entry_bot_blocked(self) -> None:
        meta: dict = {}
        diagnostic = {"bot_blocked": True}
        assert stamp_inferred_entry_block(meta, diagnostic) is True
        assert meta["entry_bot_blocked"] is True
        assert "entry_captcha_detected" not in meta  # only set when explicit
        assert meta["entry_bot_blocked_inferred_from_rescue"] is True

    def test_captcha_detected_stamps_both_flags(self) -> None:
        """When captcha is the explicit signal, both flags get stamped."""
        meta: dict = {}
        diagnostic = {"captcha_detected": True}
        assert stamp_inferred_entry_block(meta, diagnostic) is True
        assert meta["entry_bot_blocked"] is True
        assert meta["entry_captcha_detected"] is True

    def test_captcha_provider_stamped_when_present(self) -> None:
        """Provider name (sgcaptcha / cloudflare / recaptcha) flows through
        so per-host analysis can group failures by provider."""
        meta: dict = {}
        diagnostic = {
            "captcha_detected": True,
            "captcha_provider": "sgcaptcha",
        }
        stamp_inferred_entry_block(meta, diagnostic)
        assert meta["entry_captcha_provider"] == "sgcaptcha"

    def test_provider_only_stamped_when_truthy(self) -> None:
        meta: dict = {}
        diagnostic = {"captcha_detected": True, "captcha_provider": ""}
        stamp_inferred_entry_block(meta, diagnostic)
        assert "entry_captcha_provider" not in meta


class TestNoOpPaths:
    def test_none_diagnostic_is_noop(self) -> None:
        meta: dict = {}
        assert stamp_inferred_entry_block(meta, None) is False
        assert meta == {}

    def test_empty_diagnostic_is_noop(self) -> None:
        meta: dict = {}
        assert stamp_inferred_entry_block(meta, {}) is False
        assert meta == {}

    def test_both_flags_false_is_noop(self) -> None:
        meta: dict = {}
        diagnostic = {"bot_blocked": False, "captcha_detected": False}
        assert stamp_inferred_entry_block(meta, diagnostic) is False
        assert meta == {}

    def test_unrelated_diagnostic_keys_ignored(self) -> None:
        """Other diagnostic fields (status, body_bytes, etc.) don't
        trigger a stamp."""
        meta: dict = {}
        diagnostic = {
            "status": 200,
            "body_bytes": 1024,
            "content_type": "text/html",
        }
        assert stamp_inferred_entry_block(meta, diagnostic) is False
        assert meta == {}


class TestIdempotence:
    def test_stamping_twice_is_idempotent(self) -> None:
        """Calling the helper twice on the same diagnostic yields the
        same state — no duplicate keys, no provider override (rescue
        retries are one-shot today but the contract allows replay)."""
        meta: dict = {}
        diagnostic = {
            "bot_blocked": True,
            "captcha_detected": True,
            "captcha_provider": "cloudflare",
        }
        stamp_inferred_entry_block(meta, diagnostic)
        first_snapshot = dict(meta)
        stamp_inferred_entry_block(meta, diagnostic)
        assert meta == first_snapshot

    def test_existing_meta_preserved(self) -> None:
        """Other fields in _meta (verdict, partial_recovery, etc.) must
        survive the stamp."""
        meta: dict = {
            "verdict": "FAILED_UNREACHABLE",
            "verdict_reason": "per_property_timeout_no_recovery",
            "partial_recovery": False,
        }
        diagnostic = {"bot_blocked": True}
        stamp_inferred_entry_block(meta, diagnostic)
        assert meta["verdict"] == "FAILED_UNREACHABLE"
        assert meta["verdict_reason"] == "per_property_timeout_no_recovery"
        assert meta["partial_recovery"] is False
        assert meta["entry_bot_blocked"] is True


class TestCompositionWithRescueDecision:
    """The retroactive stamp must compose correctly with
    ``wedge_rescue_decision``: after stamping, a re-evaluation should
    return SKIP_ENTRY_CAPTCHA instead of RETRY."""

    def test_stamped_meta_then_decision_skips(self) -> None:
        from ma_poc.scripts.runners.jugnu import wedge_rescue_decision

        meta: dict = {"verdict": "FAILED_UNREACHABLE"}
        # Pre-stamp: rescue would RETRY (the bug).
        assert wedge_rescue_decision(meta, has_units=False) == "RETRY"

        # Rescue surfaced WAF — stamp it.
        stamp_inferred_entry_block(meta, {"bot_blocked": True})

        # Post-stamp: a future rescue pass would correctly skip.
        assert (
            wedge_rescue_decision(meta, has_units=False)
            == "SKIP_ENTRY_CAPTCHA"
        )
