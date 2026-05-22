"""Tests for the G — Competing-PMS re-dispatch (2026-05-22).

When the primary adapter exits with 0 units AND 0 plan_summaries but the
page HTML carries a strong fingerprint for a DIFFERENT PMS, scrape()
re-dispatches to that PMS's adapter on the same context before the LLM
rescue / generic fallback fires.

Pinned behaviours:
  - The G step fires only when both ``units`` AND ``plan_summaries`` are
    empty (mirrors the F2/fallback precondition).
  - The secondary adapter is chosen from ``_detector_signals.fingerprints_
    matched`` after filtering out the primary's own label and any label
    that has no registered adapter.
  - Telemetry events ``redetect_eligible`` (always when primary is empty)
    and ``redetect_dispatch`` (only when a secondary actually runs) are
    emitted via the platform-wide ``_adapter_telemetry`` module.
  - When the secondary adapter returns units (or plan_summaries), it
    replaces the empty primary result and the secondary PMS label is
    appended to ``fallback_chain``.
  - When the secondary returns empty too, the original empty result is
    preserved and we fall through to F2 / generic / LLM rescue.
"""

from __future__ import annotations

import asyncio
import inspect

from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult


# ──────────────────────────────────────────────────────────────────────
# Source-level pins (catch refactors that delete the gate)
# ──────────────────────────────────────────────────────────────────────


def test_g_redetect_block_present_in_scraper() -> None:
    """The G block must exist between adapter_exit telemetry and F2 LLM rescue.

    A future refactor that drops the block silently turns the playbook §20.10
    hop-aware re-detection rescue back into a no-op for the ~18 PIDs/day where
    a competing PMS marker dominates but the primary adapter exited empty.
    """
    src = inspect.getsource(scraper_mod)
    assert "G: Competing-PMS re-dispatch" in src, (
        "G-block header comment missing from scrape() — "
        "the playbook §20.10 hop-aware re-detection rescue is gone."
    )
    # The eligibility-and-dispatch sentinel
    assert "redetect_eligible" in src, "G redetect_eligible telemetry missing"
    assert "redetect_dispatch" in src, "G redetect_dispatch telemetry missing"
    assert "_g_redetect_applied" in src, (
        "G applied-marker key missing — analyzer can't count G-rescues."
    )


def test_g_redetect_gate_requires_both_empty_partitions() -> None:
    """The gate must check ``not adapter_result.units AND not plan_summaries``.

    Symmetric to the existing F2 / fallback gate. If a future edit drops
    the plan_summaries arm, G would fire on plan-only properties and waste
    a secondary adapter call when the primary already extracted real
    plan-level data.
    """
    src = inspect.getsource(scraper_mod)
    # Look for the specific compound predicate near the G block header
    g_idx = src.find("G: Competing-PMS re-dispatch")
    assert g_idx > 0, "G block not found"
    # Take a 3500-char window covering the block
    window = src[g_idx : g_idx + 4000]
    assert "not adapter_result.units" in window, (
        "G gate must require empty units"
    )
    assert "plan_summaries" in window, (
        "G gate must also require empty plan_summaries (symmetric to F2)"
    )


def test_g_fingerprint_to_adapter_map_excludes_marketing_only_labels() -> None:
    """The fingerprint→adapter map must not point at adapters that don't exist.

    Marketing stacks (wix, squarespace, hyly, marketapts) have no PMS
    adapter — including them in the map would crash get_adapter() and abort
    the G step.
    """
    src = inspect.getsource(scraper_mod)
    g_idx = src.find("_FP_TO_ADAPTER")
    assert g_idx > 0, "Fingerprint→adapter map missing"
    window = src[g_idx : g_idx + 1500]

    # Marketing-stack labels that MUST be absent (no adapter exists)
    for forbidden in ("\"wix\":", "\"squarespace\":", "\"marketing_hyly\":",
                      "\"marketing_marketapts\":"):
        assert forbidden not in window, (
            f"G fingerprint map includes {forbidden} — adapter does not exist; "
            "would crash get_adapter() at dispatch time."
        )

    # Strong PMS labels that MUST be in the map
    for required in ("\"entrata\":", "\"rentcafe\":", "\"sightmap\":",
                     "\"appfolio\":", "\"onesite\":", "\"marketing_knock\":"):
        assert required in window, (
            f"G fingerprint map missing {required} — "
            "this PMS won't be re-dispatched even when its marker dominates."
        )


# ──────────────────────────────────────────────────────────────────────
# Behavioural test: end-to-end G redispatch via mocked adapters
# ──────────────────────────────────────────────────────────────────────


class _StubAdapter:
    """Minimal AdapterProtocol stand-in for testing."""

    def __init__(self, pms_name: str, result: AdapterResult):
        self.pms_name = pms_name
        self._result = result
        self.calls = 0

    async def extract(self, page, ctx):  # noqa: ANN001
        self.calls += 1
        return self._result


def _make_empty() -> AdapterResult:
    return AdapterResult(units=[], plan_summaries=[], tier_used="TIER_1_API_RENTCAFE_NO_RESPONSE")


def _make_with_units(n: int) -> AdapterResult:
    units = [{"unit_number": f"U{i}", "rent_low": 1500 + i, "rent_high": 1500 + i}
             for i in range(n)]
    return AdapterResult(units=units, plan_summaries=[], tier_used="TIER_1_KNOCK_API")


def test_g_redispatches_to_knock_when_rentcafe_empty_and_knock_marker_present(
    monkeypatch,
) -> None:
    """Primary RentCafe adapter exits empty; HTML has marketing_knock fingerprint;
    Knock adapter is invoked and its units replace the empty primary result.
    """
    primary = _StubAdapter("rentcafe", _make_empty())
    secondary = _StubAdapter("knock", _make_with_units(7))

    def fake_get_adapter(pms_name: str):
        if pms_name == "rentcafe":
            return primary
        if pms_name == "knock":
            return secondary
        # Fallback: return a stub with the requested name + empty result
        # so confirm_detection's `unknown` demotion path doesn't crash.
        return _StubAdapter(pms_name, _make_empty())

    monkeypatch.setattr(scraper_mod, "get_adapter", fake_get_adapter)

    # Bypass detection — return rentcafe with high confidence
    from ma_poc.pms.detector import DetectedPMS

    def fake_detect_pms(url, csv_row=None, page_html=None):
        return DetectedPMS(
            pms="rentcafe",
            confidence=0.90,
            evidence=["url-fingerprint"],
            recommended_strategy=None,
        )

    monkeypatch.setattr(scraper_mod, "detect_pms", fake_detect_pms)

    # Provide a synthetic page_html that has BOTH securecafe.com (for the
    # detector's rentcafe fingerprint) AND doorway.knck.io (for the
    # marketing_knock fingerprint that drives G).
    fake_html = (
        "<html><head><title>foo</title></head>"
        "<body>"
        "<script src='https://prop.securecafe.com/init.js'></script>"
        "<script src='https://doorway.knck.io/widget.js'></script>"
        "</body></html>"
    )

    class _FakeFR:
        body = fake_html.encode()
        network_log: list = []
        captcha_detected = False

    result = asyncio.run(
        scraper_mod.scrape(
            base_url="https://example.com/",
            fetch_result=_FakeFR(),
            property_id="test_g_001",
        )
    )

    # Primary fired, then secondary fired
    assert primary.calls == 1, "Primary RentCafe adapter must be invoked"
    assert secondary.calls == 1, (
        "G redetect must invoke the Knock secondary adapter when "
        "marketing_knock fingerprint is matched on the page HTML"
    )

    # Result was replaced with the secondary's units
    g_marker = result.get("_g_redetect_applied")
    assert g_marker is not None, "_g_redetect_applied marker must be set"
    assert g_marker["primary"] == "rentcafe"
    assert g_marker["secondary"] == "knock"
    assert g_marker["units_recovered"] == 7

    # The reported adapter_used is updated to reflect the secondary
    assert result["_adapter_used"] == "knock"
    # fallback_chain accumulated the redetect tag
    assert any("knock_redetect" in s for s in result.get("_fallback_chain", []))


def test_g_does_not_fire_when_primary_returned_units(monkeypatch) -> None:
    """Primary RentCafe returns units → G must NOT fire (only on empties)."""
    primary = _StubAdapter("rentcafe", _make_with_units(5))
    secondary = _StubAdapter("knock", _make_with_units(99))  # should never be called

    def fake_get_adapter(pms_name: str):
        if pms_name == "rentcafe":
            return primary
        if pms_name == "knock":
            return secondary
        return _StubAdapter(pms_name, _make_empty())

    monkeypatch.setattr(scraper_mod, "get_adapter", fake_get_adapter)

    from ma_poc.pms.detector import DetectedPMS

    def fake_detect_pms(url, csv_row=None, page_html=None):
        return DetectedPMS(
            pms="rentcafe", confidence=0.90,
            evidence=["url-fingerprint"], recommended_strategy=None,
        )
    monkeypatch.setattr(scraper_mod, "detect_pms", fake_detect_pms)

    fake_html = (
        "<html><body>"
        "<script src='https://prop.securecafe.com/init.js'></script>"
        "<script src='https://doorway.knck.io/widget.js'></script>"
        "</body></html>"
    )

    class _FakeFR:
        body = fake_html.encode()
        network_log: list = []
        captcha_detected = False

    result = asyncio.run(
        scraper_mod.scrape(
            base_url="https://example.com/",
            fetch_result=_FakeFR(),
            property_id="test_g_002",
        )
    )

    assert primary.calls == 1
    assert secondary.calls == 0, (
        "G must NOT fire when primary returned non-empty units"
    )
    assert "_g_redetect_applied" not in result


def test_g_does_not_fire_when_no_competing_fingerprint(monkeypatch) -> None:
    """Primary empty but page has no competing PMS marker → G skips dispatch.

    Telemetry ``redetect_eligible`` still fires (with no_alternate outcome),
    but no secondary adapter runs.
    """
    primary = _StubAdapter("rentcafe", _make_empty())
    other = _StubAdapter("knock", _make_with_units(99))

    def fake_get_adapter(pms_name: str):
        if pms_name == "rentcafe":
            return primary
        if pms_name == "knock":
            return other
        return _StubAdapter(pms_name, _make_empty())

    monkeypatch.setattr(scraper_mod, "get_adapter", fake_get_adapter)

    from ma_poc.pms.detector import DetectedPMS

    def fake_detect_pms(url, csv_row=None, page_html=None):
        return DetectedPMS(
            pms="rentcafe", confidence=0.90,
            evidence=["url-fingerprint"], recommended_strategy=None,
        )
    monkeypatch.setattr(scraper_mod, "detect_pms", fake_detect_pms)

    # HTML has rentcafe marker only — no Knock / SightMap / G5 etc.
    fake_html = (
        "<html><body>"
        "<script src='https://prop.securecafe.com/init.js'></script>"
        "<p>Marketing copy only</p>"
        "</body></html>"
    )

    class _FakeFR:
        body = fake_html.encode()
        network_log: list = []
        captcha_detected = False

    result = asyncio.run(
        scraper_mod.scrape(
            base_url="https://example.com/",
            fetch_result=_FakeFR(),
            property_id="test_g_003",
        )
    )

    assert primary.calls == 1
    assert other.calls == 0
    assert "_g_redetect_applied" not in result
