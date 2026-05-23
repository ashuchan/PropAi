"""Exhaustive unit tests for ``ma_poc.fetch.proxy_gate``.

Every layer of the decision tree gets its own test, every layer's
fail-closed default gets a test, and the per-property budgets are
verified for both the hop counter (max 3) and the byte counter.

The companion ``test_proxy_gate_no_leaks.py`` enforces that no other
file reads ``PROBE_PROXY_URL`` directly — together they form the
strict-allow audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from ma_poc.fetch import proxy_gate
from ma_poc.fetch.proxy_gate import (
    DEFAULT_L1_MAX_ESCALATION_HOPS,
    DEFAULT_MAX_PROXY_BYTES,
    DEFAULT_MAX_PROXY_HOPS,
    ProxyDecisionReason,
    ProxyGateDecision,
    decide,
    decide_l1_escalate,
    mark_host_needs_proxy,
    record_use,
)


# ─── Test fixtures ────────────────────────────────────────────────────


@dataclass
class _StubDetection:
    pms: str = "rentcafe"
    confidence: float = 0.90


@dataclass
class _StubApiHints:
    proxy_required_hosts: list[str] = field(default_factory=list)


@dataclass
class _StubProfile:
    api_hints: _StubApiHints = field(default_factory=_StubApiHints)


@dataclass
class _StubCtx:
    base_url: str = "https://www.montecitoapartments.com/"
    detected: _StubDetection = field(default_factory=_StubDetection)
    profile: _StubProfile | None = field(default_factory=_StubProfile)
    proxy_hops_used: int = 0
    proxy_bytes_used: int = 0
    proxy_max_hops: int = DEFAULT_MAX_PROXY_HOPS
    proxy_max_bytes: int = DEFAULT_MAX_PROXY_BYTES
    property_id: str = "test-pid"


@pytest.fixture
def proxy_env(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set a fake proxy URL for the gate's Layer 0a check."""
    url = "http://user:pass@fake-proxy.test:1234"
    monkeypatch.setenv("PROBE_PROXY_URL", url)
    return url


# ─── Layer 0a — env kill switch ─────────────────────────────────────


def test_no_proxy_configured_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROBE_PROXY_URL", raising=False)
    d = decide("https://prop.securecafe.com/", _StubCtx(), "sc_probe")
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.NO_PROXY_CONFIGURED
    assert d.proxy_url is None
    assert d.proxies() == {}


def test_no_proxy_configured_when_env_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROBE_PROXY_URL", "   ")  # whitespace stripped
    d = decide("https://prop.securecafe.com/", _StubCtx(), "sc_probe")
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.NO_PROXY_CONFIGURED


# ─── Strict-allow — missing ctx or stage ─────────────────────────────


def test_missing_ctx_fails_closed(proxy_env: str) -> None:
    d = decide("https://prop.securecafe.com/", None, "sc_probe")
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.MISSING_CTX_OR_STAGE


def test_missing_stage_fails_closed(proxy_env: str) -> None:
    d = decide("https://prop.securecafe.com/", _StubCtx(), None)
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.MISSING_CTX_OR_STAGE


def test_empty_string_stage_fails_closed(proxy_env: str) -> None:
    d = decide("https://prop.securecafe.com/", _StubCtx(), "")
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.MISSING_CTX_OR_STAGE


# ─── Layer 0b — per-property hop budget (the "max 3 hops" rule) ──────


def test_hop_budget_blocks_after_3_hops(proxy_env: str) -> None:
    """Verify the user-requested max-3-hops cap. After 3 uses, even a
    high-quality Layer 2 host gets denied."""
    ctx = _StubCtx(proxy_hops_used=3)
    d = decide("https://prop.securecafe.com/onlineleasing/x/availableunits.aspx",
               ctx, "sc_probe")
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.PROPERTY_HOP_BUDGET_EXHAUSTED


def test_hop_budget_allows_at_exactly_2_used(proxy_env: str) -> None:
    """At hops=2, budget headroom is 1 — Layer 2 still admits."""
    ctx = _StubCtx(proxy_hops_used=2)
    d = decide("https://prop.securecafe.com/onlineleasing/x/availableunits.aspx",
               ctx, "sc_probe")
    assert d.allow is True
    assert d.reason == ProxyDecisionReason.HOST_ALLOWLIST


def test_hop_budget_respects_custom_max(proxy_env: str) -> None:
    """A caller (e.g. a high-value SC tenant) may raise their cap."""
    ctx = _StubCtx(proxy_hops_used=3, proxy_max_hops=5)
    d = decide("https://prop.securecafe.com/x/availableunits.aspx",
               ctx, "sc_probe")
    assert d.allow is True
    assert d.reason == ProxyDecisionReason.HOST_ALLOWLIST


def test_hop_budget_default_is_3(proxy_env: str) -> None:
    """Default constant is 3 — matches the user requirement."""
    assert DEFAULT_MAX_PROXY_HOPS == 3


# ─── Layer 0c — per-property byte budget ─────────────────────────────


def test_byte_budget_blocks_after_max(proxy_env: str) -> None:
    ctx = _StubCtx(proxy_bytes_used=DEFAULT_MAX_PROXY_BYTES)
    d = decide("https://prop.securecafe.com/", ctx, "sc_probe")
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.PROPERTY_BYTE_BUDGET_EXHAUSTED


# ─── Layer 1 — same-eTLD+1 origin ────────────────────────────────────


def test_same_origin_declines_proxy(proxy_env: str) -> None:
    """Same eTLD+1 → existing clearance cookies serve; proxy unneeded."""
    ctx = _StubCtx(base_url="https://www.maac.com/properties/foo")
    d = decide("https://www.maac.com/api/properties/X/units/available/", ctx, "maac_units")
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.SAME_ORIGIN


def test_same_origin_with_different_subdomain(proxy_env: str) -> None:
    """``www.foo.com`` and ``api.foo.com`` are same eTLD+1."""
    ctx = _StubCtx(base_url="https://www.example.com/page")
    d = decide("https://api.example.com/data", ctx, "sc_probe")
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.SAME_ORIGIN


def test_cross_etld_does_not_match_same_origin(proxy_env: str) -> None:
    """Different eTLD+1 → not same-origin."""
    ctx = _StubCtx(base_url="https://www.montecitoapartments.com/")
    d = decide("https://prop.securecafe.com/onlineleasing/x/availableunits.aspx",
               ctx, "sc_probe")
    # Should reach Layer 2 (allowlist) instead
    assert d.allow is True
    assert d.reason == ProxyDecisionReason.HOST_ALLOWLIST


# ─── Layer 2 — static host allowlist ─────────────────────────────────


@pytest.mark.parametrize("url,reason_expected", [
    # securecafe.com — RentCafe SC drill
    ("https://prop.securecafe.com/onlineleasing/x/availableunits.aspx",
     ProxyDecisionReason.HOST_ALLOWLIST),
    ("https://anotherprop.securecafe.com/", ProxyDecisionReason.HOST_ALLOWLIST),
    # prospectportal.com — Entrata
    ("https://x.prospectportal.com/?module=check_availability",
     ProxyDecisionReason.HOST_ALLOWLIST),
    # iloveleasing.com — RentManager/iLoveLeasing
    ("https://x.iloveleasing.com/portal/availability", ProxyDecisionReason.HOST_ALLOWLIST),
    # onlineleasing.realpage.com — OneSite / RealPage OLL
    ("https://9026050.onlineleasing.realpage.com/", ProxyDecisionReason.HOST_ALLOWLIST),
])
def test_static_allowlist_admits_known_cf_walled_hosts(
    proxy_env: str, url: str, reason_expected: ProxyDecisionReason,
) -> None:
    """Every host in the curated allowlist must hit Layer 2."""
    ctx = _StubCtx()
    d = decide(url, ctx, "sc_probe")
    assert d.allow is True
    assert d.reason == reason_expected
    assert d.proxy_url == proxy_env


def test_allowlist_does_not_admit_unknown_host(proxy_env: str) -> None:
    """A host that isn't in the allowlist must NOT match Layer 2.
    Falls through to Layer 4 quality gate or the default deny."""
    ctx = _StubCtx()
    d = decide("https://random-cms-host.com/", ctx, "sc_probe")
    # No allowlist match, not in profile, but stage is eligible AND
    # confidence is high — Layer 4 admits via high_confidence_cross_origin.
    assert d.allow is True
    assert d.reason == ProxyDecisionReason.HIGH_CONFIDENCE_CROSS_ORIGIN


# ─── Layer 3 — profile-learned per-host block memory ─────────────────


def test_profile_learned_block_admits_proxy(proxy_env: str) -> None:
    """A host previously CF-blocked direct → next run uses proxy by Layer 3."""
    ctx = _StubCtx()
    ctx.profile.api_hints.proxy_required_hosts = ["learned-blocked-host.example"]
    d = decide("https://learned-blocked-host.example/api", ctx, "wp_probe")
    assert d.allow is True
    assert d.reason == ProxyDecisionReason.PROFILE_LEARNED_BLOCK


def test_profile_learned_block_case_insensitive(proxy_env: str) -> None:
    """Host comparison is case-insensitive."""
    ctx = _StubCtx()
    ctx.profile.api_hints.proxy_required_hosts = ["Mixed-Case-Host.Example"]
    d = decide("https://mixed-case-host.example/x", ctx, "sc_probe")
    assert d.allow is True
    assert d.reason == ProxyDecisionReason.PROFILE_LEARNED_BLOCK


def test_profile_none_falls_through_to_layer_4(proxy_env: str) -> None:
    """No profile → Layer 3 is a no-op, Layer 4 takes over."""
    ctx = _StubCtx(profile=None)
    d = decide("https://random-host.com/", ctx, "sc_probe")
    assert d.allow is True
    assert d.reason == ProxyDecisionReason.HIGH_CONFIDENCE_CROSS_ORIGIN


# ─── Layer 4 — quality gate (stage + confidence) ─────────────────────


def test_layer_4_blocks_stage_not_eligible(proxy_env: str) -> None:
    """An unknown stage never triggers Layer 4."""
    ctx = _StubCtx()
    d = decide("https://random-host.com/", ctx, "xhr_capture")
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.STAGE_NOT_PROXY_ELIGIBLE


def test_layer_4_blocks_low_confidence(proxy_env: str) -> None:
    """High confidence is REQUIRED for Layer 4."""
    ctx = _StubCtx(detected=_StubDetection(confidence=0.70))  # below 0.80
    d = decide("https://random-host.com/", ctx, "sc_probe")
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.DETECTION_CONFIDENCE_BELOW_THRESHOLD


def test_layer_4_admits_high_conf_eligible_stage(proxy_env: str) -> None:
    ctx = _StubCtx(detected=_StubDetection(confidence=0.85))
    d = decide("https://random-host.com/", ctx, "sc_probe")
    assert d.allow is True
    assert d.reason == ProxyDecisionReason.HIGH_CONFIDENCE_CROSS_ORIGIN


def test_layer_4_threshold_exactly_0_80(proxy_env: str) -> None:
    """Boundary: confidence == 0.80 → admit."""
    ctx = _StubCtx(detected=_StubDetection(confidence=0.80))
    d = decide("https://random-host.com/", ctx, "sc_probe")
    assert d.allow is True


def test_layer_4_just_below_threshold_denies(proxy_env: str) -> None:
    """Boundary: confidence == 0.799 → deny."""
    ctx = _StubCtx(detected=_StubDetection(confidence=0.799))
    d = decide("https://random-host.com/", ctx, "sc_probe")
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.DETECTION_CONFIDENCE_BELOW_THRESHOLD


# ─── Layer 4 / max 3 hops interplay (user requirement) ───────────────


def test_layer_4_respects_3_hop_cap(proxy_env: str) -> None:
    """User requirement: Layer 4 must not exceed 3 proxy hops per property.

    Even when stage + confidence + cross-origin all match, the 4th call
    must be denied by Layer 0b.
    """
    ctx = _StubCtx(proxy_hops_used=3)  # already at cap
    d = decide("https://random-host.com/", ctx, "sc_probe")
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.PROPERTY_HOP_BUDGET_EXHAUSTED


# ─── Layer ordering (priority of fail-closed checks) ─────────────────


def test_kill_switch_beats_allowlist(proxy_env: str) -> None:
    """Hop budget kill switch fires BEFORE the host allowlist."""
    ctx = _StubCtx(proxy_hops_used=10)
    d = decide("https://prop.securecafe.com/", ctx, "sc_probe")
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.PROPERTY_HOP_BUDGET_EXHAUSTED


def test_same_origin_beats_allowlist(proxy_env: str) -> None:
    """If the marketing site is on the allowlist host (theoretical),
    Layer 1 still fires first."""
    ctx = _StubCtx(base_url="https://x.securecafe.com/")
    d = decide("https://x.securecafe.com/onlineleasing/y", ctx, "sc_probe")
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.SAME_ORIGIN


# ─── record_use — counter accounting ─────────────────────────────────


def test_record_use_increments_hop_counter(proxy_env: str) -> None:
    ctx = _StubCtx()
    d = decide("https://prop.securecafe.com/", ctx, "sc_probe")
    assert d.allow is True
    record_use(ctx, d, response_bytes=12345)
    assert ctx.proxy_hops_used == 1
    assert ctx.proxy_bytes_used == 12345


def test_record_use_is_noop_when_decline(proxy_env: str) -> None:
    """A denied decision must NOT bump counters."""
    ctx = _StubCtx(proxy_hops_used=0)
    decision = ProxyGateDecision(
        allow=False, reason=ProxyDecisionReason.SAME_ORIGIN, host="x.com",
    )
    record_use(ctx, decision, response_bytes=99999)
    assert ctx.proxy_hops_used == 0
    assert ctx.proxy_bytes_used == 0


def test_three_record_uses_then_cap(proxy_env: str) -> None:
    """End-to-end: 3 successful proxy uses → the 4th is budget-blocked."""
    ctx = _StubCtx()
    # All 3 calls allowed; record each.
    for _ in range(3):
        d = decide("https://prop.securecafe.com/", ctx, "sc_probe")
        assert d.allow is True
        record_use(ctx, d, response_bytes=10000)
    # 4th call now blocked by hop budget
    d = decide("https://prop.securecafe.com/", ctx, "sc_probe")
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.PROPERTY_HOP_BUDGET_EXHAUSTED


def test_record_use_handles_read_only_ctx_gracefully(proxy_env: str) -> None:
    """If ctx happens to be a frozen dataclass / Mock with no settable
    fields, record_use logs and returns — never raises."""
    sentinel = SimpleNamespace(proxy_hops_used=0)
    decision = ProxyGateDecision(
        allow=True,
        reason=ProxyDecisionReason.HOST_ALLOWLIST,
        proxy_url="http://x:y@p:1",
        host="h.com",
    )
    record_use(sentinel, decision, response_bytes=1)
    assert sentinel.proxy_hops_used == 1


# ─── mark_host_needs_proxy — Layer 3 self-learn writer ───────────────


def test_mark_host_needs_proxy_appends(proxy_env: str) -> None:
    ctx = _StubCtx()
    mark_host_needs_proxy(ctx, "freshly-blocked.example")
    assert "freshly-blocked.example" in ctx.profile.api_hints.proxy_required_hosts


def test_mark_host_needs_proxy_dedups(proxy_env: str) -> None:
    ctx = _StubCtx()
    mark_host_needs_proxy(ctx, "dup.example")
    mark_host_needs_proxy(ctx, "dup.example")
    mark_host_needs_proxy(ctx, "DUP.EXAMPLE")
    assert ctx.profile.api_hints.proxy_required_hosts.count("dup.example") == 1


def test_mark_host_needs_proxy_caps_at_30(proxy_env: str) -> None:
    ctx = _StubCtx()
    for i in range(50):
        mark_host_needs_proxy(ctx, f"host{i}.example")
    assert len(ctx.profile.api_hints.proxy_required_hosts) == 30
    # Oldest evicted — host49 (most recent) must still be present.
    assert "host49.example" in ctx.profile.api_hints.proxy_required_hosts
    # host0 evicted
    assert "host0.example" not in ctx.profile.api_hints.proxy_required_hosts


def test_mark_host_needs_proxy_no_profile(proxy_env: str) -> None:
    """No profile → no-op, never raises."""
    ctx = _StubCtx(profile=None)
    mark_host_needs_proxy(ctx, "x.com")
    # No exception — that's the contract


def test_mark_host_needs_proxy_empty_host(proxy_env: str) -> None:
    ctx = _StubCtx()
    mark_host_needs_proxy(ctx, "")
    assert ctx.profile.api_hints.proxy_required_hosts == []


# ─── ProxyGateDecision.proxies() convenience ─────────────────────────


def test_proxies_returns_dict_when_allow(proxy_env: str) -> None:
    d = decide("https://prop.securecafe.com/", _StubCtx(), "sc_probe")
    assert d.allow is True
    px = d.proxies()
    assert px == {"http": proxy_env, "https": proxy_env}


def test_proxies_returns_empty_when_deny(proxy_env: str) -> None:
    ctx = _StubCtx(base_url="https://x.securecafe.com/")  # same-origin
    d = decide("https://x.securecafe.com/x", ctx, "sc_probe")
    assert d.allow is False
    assert d.proxies() == {}


# ─── Eligible stages contract ────────────────────────────────────────


@pytest.mark.parametrize("stage", [
    "sc_probe", "sc_homepage_refetch", "sc_parse", "wp_probe",
    "prospectportal_probe", "rentmanager_search", "realpage_cws",
    "nestin_detail_fetch",
])
def test_every_eligible_stage_can_admit(proxy_env: str, stage: str) -> None:
    """Each documented eligible stage must reach Layer 4 admit on a
    confident detection + cross-origin host."""
    ctx = _StubCtx(detected=_StubDetection(confidence=0.85))
    d = decide("https://random-cross-origin.host/", ctx, stage)
    assert d.allow is True
    assert d.reason == ProxyDecisionReason.HIGH_CONFIDENCE_CROSS_ORIGIN


@pytest.mark.parametrize("stage", [
    "xhr_capture", "cascade_exit", "adapter_exit", "wp_property_id",
    "urn_pick", "ids_search", "blocked_filter", "profile_replay",
])
def test_non_eligible_stages_blocked(proxy_env: str, stage: str) -> None:
    """Stages that don't make outbound proxy-relevant requests must
    never trigger Layer 4, even on a high-quality cross-origin URL."""
    ctx = _StubCtx()
    d = decide("https://random-cross-origin.host/", ctx, stage)
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.STAGE_NOT_PROXY_ELIGIBLE


# ─── decide_l1_escalate — L1 entry-fetch CF-escalation gate ──────────────
#
# Distinct surface from ``decide``. The L1 escalation gate has different
# semantics (escalation-only, no same-origin denial, caller-supplied
# proxy URL). Tests below pin every branch in the decision tree.


def test_l1_escalate_admits_on_bot_blocked_with_pool() -> None:
    """Happy path: prior direct attempt was BOT_BLOCKED AND the pool has
    a proxy AND budget remains → escalation admitted."""
    d = decide_l1_escalate(
        "https://www.hunterscourtapts.com/",
        prior_outcome="BOT_BLOCKED",
        proxy_hops_used=0,
        pool_has_proxies=True,
    )
    assert d.allow is True
    assert d.reason == ProxyDecisionReason.L1_CF_ESCALATION_ADMITTED
    # Proxy URL is supplied by the caller's ProxyPool, not the gate —
    # the decision is policy-only, the pool is the resource.
    assert d.proxy_url is None
    assert d.host == "www.hunterscourtapts.com"


def test_l1_escalate_denies_on_transient_outcome() -> None:
    """TRANSIENT failures (DNS flake, TCP RST, SSL handshake) have their
    own retry path. They must NOT trigger CF-escalation — that would
    burn proxy bandwidth on flakes the next direct attempt would
    recover."""
    d = decide_l1_escalate(
        "https://www.example.com/",
        prior_outcome="TRANSIENT",
        proxy_hops_used=0,
        pool_has_proxies=True,
    )
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.L1_PRIOR_OUTCOME_NOT_BLOCKED


def test_l1_escalate_denies_on_ok_outcome() -> None:
    """Sanity guard — OK should never trigger escalation. If it does,
    the caller is invoking the gate from the wrong place."""
    d = decide_l1_escalate(
        "https://www.example.com/",
        prior_outcome="OK",
        proxy_hops_used=0,
        pool_has_proxies=True,
    )
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.L1_PRIOR_OUTCOME_NOT_BLOCKED


def test_l1_escalate_denies_on_rate_limited() -> None:
    """RATE_LIMITED has its own escalation path inside the fetcher
    retry loop (line 530-547). The L1 CF gate must not double-fire."""
    d = decide_l1_escalate(
        "https://www.example.com/",
        prior_outcome="RATE_LIMITED",
        proxy_hops_used=0,
        pool_has_proxies=True,
    )
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.L1_PRIOR_OUTCOME_NOT_BLOCKED


def test_l1_escalate_denies_with_empty_pool() -> None:
    """Operator hasn't set ``PROXY_POOL_URLS``. There's nothing to
    escalate to — gate returns NO_PROXY_CONFIGURED."""
    d = decide_l1_escalate(
        "https://www.example.com/",
        prior_outcome="BOT_BLOCKED",
        proxy_hops_used=0,
        pool_has_proxies=False,
    )
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.NO_PROXY_CONFIGURED


def test_l1_escalate_denies_when_hop_budget_exhausted() -> None:
    """One CF-escalation per fetch task. A second escalation is almost
    never useful (the proxy was ALSO CF-blocked → third attempt won't
    recover) and burning another hop just inflates the proxy bill."""
    d = decide_l1_escalate(
        "https://www.example.com/",
        prior_outcome="BOT_BLOCKED",
        proxy_hops_used=DEFAULT_L1_MAX_ESCALATION_HOPS,
        pool_has_proxies=True,
    )
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.L1_HOP_BUDGET_EXHAUSTED


def test_l1_escalate_admits_case_insensitive_outcome() -> None:
    """Defensive — caller might pass lowercase ``"bot_blocked"`` from
    a string-formatted enum. The gate should match either way."""
    d = decide_l1_escalate(
        "https://www.example.com/",
        prior_outcome="bot_blocked",
        proxy_hops_used=0,
        pool_has_proxies=True,
    )
    assert d.allow is True
    assert d.reason == ProxyDecisionReason.L1_CF_ESCALATION_ADMITTED


def test_l1_escalate_denies_on_none_outcome() -> None:
    """None prior_outcome (e.g. caller hasn't actually seen a fetch
    result yet) must fail-closed — no escalation without evidence of
    a CF block."""
    d = decide_l1_escalate(
        "https://www.example.com/",
        prior_outcome=None,
        proxy_hops_used=0,
        pool_has_proxies=True,
    )
    assert d.allow is False
    assert d.reason == ProxyDecisionReason.L1_PRIOR_OUTCOME_NOT_BLOCKED


def test_l1_escalate_custom_max_hops_override() -> None:
    """Tests can raise the cap to verify higher-budget scenarios. The
    cap is per-call so unit tests don't have to monkeypatch a module
    constant to exercise hop-3 paths."""
    d = decide_l1_escalate(
        "https://www.example.com/",
        prior_outcome="BOT_BLOCKED",
        proxy_hops_used=2,
        max_hops=3,
        pool_has_proxies=True,
    )
    assert d.allow is True
    assert d.reason == ProxyDecisionReason.L1_CF_ESCALATION_ADMITTED


def test_l1_escalate_decision_immutable() -> None:
    """ProxyGateDecision is frozen — assigning ``allow`` post-decision
    must raise. Pins the strict-allow contract."""
    d = decide_l1_escalate(
        "https://www.example.com/",
        prior_outcome="BOT_BLOCKED",
        proxy_hops_used=0,
        pool_has_proxies=True,
    )
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
        d.allow = False  # type: ignore[misc]
