"""Centralised, strict-allow gate for residential-proxy + Web-Unlocker use.

THIS MODULE IS THE ONLY PLACE THAT MAY RESOLVE ``PROBE_PROXY_URL`` /
``WEB_UNLOCKER_KEY`` INTO A USE-DECISION.

Every adapter-side probe that could route through a paid egress must
call :func:`decide` first. The function returns a
:class:`ProxyGateDecision` whose ``allow`` field is a fail-closed
boolean — when ``allow=False`` the caller MUST issue the request via
direct/curl_cffi/no-proxy. When ``allow=True`` the caller may attach
the proxy returned by :meth:`ProxyGateDecision.proxies` and MUST call
:func:`record_use` after the response returns so per-property budgets
stay accurate.

Architecture
------------

The gate is **layered** — cheap-to-evaluate checks run first; expensive
checks (per-property profile lookups) only fire when none of the
earlier layers fail-closed the decision::

  Layer 0a: env not configured       -> False, "no_proxy_configured"
  Layer 0b: per-property hop budget  -> False, "property_hop_budget_exhausted"
  Layer 0c: per-property byte budget -> False, "property_byte_budget_exhausted"
  Layer 1 : same-eTLD+1 as marketing -> False, "same_origin"
  Layer 2 : static host allowlist    -> True,  "host_allowlist"
  Layer 3 : profile-learned block    -> True,  "profile_learned_block"
  Layer 4 : eligible stage + conf    -> True,  "high_confidence_cross_origin"
  default : fail-closed              -> False, "default_deny"

The five-positive layers (2, 3, 4) are the ONLY paths to ``allow=True``.
Any new mechanism for proxy use must extend one of these layers — never
read ``PROBE_PROXY_URL`` outside this module.

Strict-allow contract
---------------------

The grep test ``tests/fetch/test_proxy_gate_no_leaks.py`` enforces that
no file other than this module and the well-known telemetry/diagnostic
sites reads ``PROBE_PROXY_URL``. If you add a new env var, add the
allowlist entry there.

Per-property budget
-------------------

Each property gets at most ``DEFAULT_MAX_PROXY_HOPS`` (3) total proxy
hops. The counter is on ``AdapterContext.proxy_hops_used``; the gate
returns ``False`` once it hits the cap regardless of layer.
Residential-proxy hops AND Web-Unlocker hops both count — the budget
represents "external paid-egress hops for this property", which is the
right scope for cost attribution.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# ─── Static configuration ──────────────────────────────────────────────

#: Hosts (matched as suffix against URL hostname) where production
#: telemetry has confirmed direct-from-GCP requests are CF-walled at
#: the edge. Adding a host here causes Layer 2 to fire — the proxy is
#: used **before** trying a direct request, which saves the wasted-
#: roundtrip cost of "direct → CF-blocked → escalate".
#:
#: Inclusion criterion: ≥80% of direct probes against this host suffix
#: returned a CF challenge (status 403 + ``challenge-platform``) on
#: ≥1,000 production attempts. Each host's evidence is captured in the
#: comment.
_PROXY_REQUIRED_HOST_SUFFIXES: frozenset[str] = frozenset({
    # RentCafe SecureCafe portal. 2026-05-22 telemetry: 7,307/7,749
    # sc_probe events returned blocked_status_403; the remaining 442
    # were skipped_no_base. Local-residential verification 2026-05-22
    # confirmed 3/5 sampled tenants pass via residential proxy.
    "securecafe.com",
    # Entrata ProspectPortal. 2026-05-22 telemetry: 832/832 prospect-
    # portal_probe events returned ran_empty with via_proxy=False;
    # playbook §20.9 documents the cross-origin clearance gap.
    "prospectportal.com",
    # RentManager iLoveLeasing portal — when ResMan/iLoveLeasing-style
    # availability page is on a separate sub-domain it gets the same
    # CF treatment as SecureCafe.
    "iloveleasing.com",
    # RealPage OneSite / OLL portal. Per playbook §20.3 the bare
    # ``onlineleasing.realpage.com`` host is sometimes CF-walled when
    # accessed cross-origin. Tracked as needing proxy for robustness;
    # direct path will still try first per Layer 5 if the host is not
    # in the profile's learned block list.
    "onlineleasing.realpage.com",
})

#: Stages where the proxy is structurally valuable. Set membership is
#: cheap; any stage not here can never trigger Layer 4 even on a
#: high-confidence detection. Stages are the same ``tier_key`` suffix
#: used by ``_adapter_telemetry.log_adapter_stage`` so the gate's
#: decision space is queryable from events.jsonl.
_PROXY_ELIGIBLE_STAGES: frozenset[str] = frozenset({
    # RentCafe SecureCafe drill — the canonical use case (see playbook §20.6/20.10)
    "sc_probe",
    "sc_homepage_refetch",
    "sc_parse",         # parse-stage hops carry the actual unit body
    # RentCafe WordPress middleware probe — cross-origin to wp-json
    "wp_probe",
    # Entrata ProspectPortal probe — restored 2026-05-22
    "prospectportal_probe",
    # RentManager search probe — sometimes on a third-party host
    "rentmanager_search",
    # RealPage / OneSite cross-origin probes
    "realpage_cws",     # cross-host RealPage CWS API (generic adapter)
    # Nestin per-plan detail page fetch when same-host clearance fails
    "nestin_detail_fetch",
})

#: Detection-confidence floor for Layer 4. Detections at 0.70 or lower
#: are typically CSV-mgmt-prior-only (no URL host confirmation) and we
#: don't burn proxy bandwidth speculating. Confirmed URL host matches
#: score ≥ 0.85 in ``pms.detector._detect_host``.
_MIN_PROXY_DETECTION_CONFIDENCE: float = 0.80

#: Per-property default cap on total proxy hops (residential + Web-
#: Unlocker combined). The cap is on AdapterContext and may be
#: overridden by tests / callers for explicit scenarios.
DEFAULT_MAX_PROXY_HOPS: int = 3

#: Per-property byte budget. Belt-and-braces against a runaway loop
#: where a single property pulls 100 MB through the proxy.
DEFAULT_MAX_PROXY_BYTES: int = 1_500_000   # 1.5 MB

#: Per-property cap on L1 entry-URL escalation hops. Held tight (1)
#: because the L1 escalation fires from inside the fetcher retry loop —
#: each escalation is one extra paid-egress hop and the second escalation
#: (proxy also bot-blocked) is virtually never useful. Independent of
#: ``DEFAULT_MAX_PROXY_HOPS`` which governs the cross-origin probe path.
DEFAULT_L1_MAX_ESCALATION_HOPS: int = 1


# ─── Public types ──────────────────────────────────────────────────────


class ProxyDecisionReason(str, Enum):
    """Stable string codes for the gate decision. Logged into adapter
    telemetry as ``proxy_decision_reason`` so production behaviour is
    queryable per reason without parsing free-form text.

    Adding a new reason: append here AND update the corresponding
    decision branch in :func:`decide`. Never rename existing values —
    they're persisted in events.jsonl and the analyzer keys off them.
    """

    NO_PROXY_CONFIGURED = "no_proxy_configured"
    PROPERTY_HOP_BUDGET_EXHAUSTED = "property_hop_budget_exhausted"
    PROPERTY_BYTE_BUDGET_EXHAUSTED = "property_byte_budget_exhausted"
    SAME_ORIGIN = "same_origin"
    HOST_ALLOWLIST = "host_allowlist"
    PROFILE_LEARNED_BLOCK = "profile_learned_block"
    HIGH_CONFIDENCE_CROSS_ORIGIN = "high_confidence_cross_origin"
    STAGE_NOT_PROXY_ELIGIBLE = "stage_not_proxy_eligible"
    DETECTION_CONFIDENCE_BELOW_THRESHOLD = "detection_confidence_below_threshold"
    MISSING_CTX_OR_STAGE = "missing_ctx_or_stage"
    DEFAULT_DENY = "default_deny"
    # ── L1 entry-fetch CF-escalation reasons (added 2026-05-23) ──
    # These cover the L1 fetcher's *entry-URL* escalation decision —
    # distinct from the cross-origin probe path because at L1 the URL
    # IS the property's marketing origin (so same-eTLD+1 doesn't apply)
    # and the proxy is supplied from the L1 ProxyPool rather than from
    # ``PROBE_PROXY_URL``. See :func:`decide_l1_escalate`.
    L1_PRIOR_OUTCOME_NOT_BLOCKED = "l1_prior_outcome_not_blocked"
    L1_HOP_BUDGET_EXHAUSTED = "l1_hop_budget_exhausted"
    L1_CF_ESCALATION_ADMITTED = "l1_cf_escalation_admitted"
    # 2026-05-24 canary fix — extended L1 escalation triggers for two
    # stealth-block signatures the original BOT_BLOCKED gate missed:
    #
    # * EMPTY_BODY (200 with <16 byte response): the canary observed PID
    #   238171 (live230ash.com) — `LLM_GATE_NO_BODY` verdict from a 200
    #   with empty body. Origin returned an empty page rather than the
    #   CF challenge HTML, which is the stealth-bot-block signature
    #   some operators use. Proxy retry from a different egress
    #   commonly recovers these.
    #
    # * UNREACHABLE with connection-level error_signature: PID 13314
    #   (imt-woodland-meadows) was `FAILED_UNREACHABLE`. Connection
    #   refused / timeout / reset are often IP-based rate-limit or
    #   geo-block patterns. Proxy retry MAY recover; we admit when the
    #   error_signature suggests a network-tier block, NOT when DNS
    #   resolution failed (NXDOMAIN is terminal for proxies too).
    L1_EMPTY_BODY_ADMITTED = "l1_empty_body_admitted"
    L1_UNREACHABLE_ADMITTED = "l1_unreachable_admitted"
    L1_DNS_FAIL_NOT_ADMITTED = "l1_dns_fail_not_admitted"
    L1_SSL_FAIL_NOT_ADMITTED = "l1_ssl_fail_not_admitted"
    # 2026-05-24 — added after user feedback "high confidence signals
    # only, not everything which is bot blocked and unreachable".
    # Bare HTTP_403 / BOT_BLOCKED can be misclassified auth-required
    # responses; bare TIMED_OUT can be network flake. We need a
    # SPECIFIC bot-block provider signature (cloudflare/datadome/etc.)
    # OR a tight connection-error pattern. Low-confidence signals get
    # denied with these reasons so telemetry can see what was filtered.
    L1_BOT_BLOCKED_LOW_CONFIDENCE = "l1_bot_blocked_low_confidence"
    L1_UNREACHABLE_LOW_CONFIDENCE = "l1_unreachable_low_confidence"


@dataclass(frozen=True)
class ProxyGateDecision:
    """Result of :func:`decide`. Immutable so callers can't accidentally
    mutate ``allow`` after the gate ran.

    Attributes
    ----------
    allow:
        Authoritative go/no-go. Callers MUST treat ``False`` as
        "no proxy this call, even if PROBE_PROXY_URL is set".
    reason:
        Why the gate decided as it did. Goes into telemetry.
    proxy_url:
        The resolved ``PROBE_PROXY_URL`` value when ``allow=True``.
        ``None`` when ``allow=False`` — callers MUST NOT read this
        field when ``allow=False``.
    host:
        The probe URL's hostname (lowercased). Cached so the caller
        doesn't re-parse for telemetry / profile updates.
    """

    allow: bool
    reason: ProxyDecisionReason
    proxy_url: str | None = None
    host: str = ""

    def proxies(self) -> dict[str, str]:
        """Return curl_cffi / requests-compatible ``proxies={}`` dict.

        Returns an empty dict when ``allow=False`` — passing the result
        of this method directly into ``curl_cffi.get(..., proxies=...)``
        is safe regardless of gate decision.
        """
        if not self.allow or not self.proxy_url:
            return {}
        return {"http": self.proxy_url, "https": self.proxy_url}


# ─── Internal helpers ─────────────────────────────────────────────────


def _env_proxy_url() -> str:
    """Read ``PROBE_PROXY_URL`` (single source of truth in the codebase)."""
    return os.getenv("PROBE_PROXY_URL", "").strip()


def _etld_plus_one(host: str) -> str:
    """Return the last two dot-separated labels (best-effort eTLD+1).

    Not a full PSL implementation — we don't need to handle
    ``co.uk``-style multi-label TLDs because property marketing
    sites use simple ``.com`` / ``.net`` / ``.us`` TLDs. The simple
    "last two labels" rule yields the right answer for every host
    in our production corpus.

    Examples::
        _etld_plus_one("www.montecitoapartments.com") == "montecitoapartments.com"
        _etld_plus_one("api.foo.bar.baz")              == "baz"   # acceptable simplification
    """
    if not host:
        return ""
    parts = host.lower().rstrip(".").split(".")
    if len(parts) <= 2:
        return ".".join(parts)
    return ".".join(parts[-2:])


def _matches_required_host(host: str) -> bool:
    """``True`` when ``host`` ends with a known CF-walled suffix.

    Uses suffix match so subdomains under a known portal host (e.g.
    ``theberkman.securecafe.com``) are caught by the allowlist entry
    ``securecafe.com``.
    """
    host = host.lower()
    return any(host == suffix or host.endswith("." + suffix)
               for suffix in _PROXY_REQUIRED_HOST_SUFFIXES)


def _profile_says_host_needs_proxy(ctx: Any, host: str) -> bool:
    """Layer 3 — check the per-property profile for a learned block.

    Defensive against profiles that lack the ``proxy_required_hosts``
    field (older Pydantic models or unit-test stubs).
    """
    profile = getattr(ctx, "profile", None)
    if profile is None:
        return False
    api_hints = getattr(profile, "api_hints", None)
    if api_hints is None:
        return False
    learned = getattr(api_hints, "proxy_required_hosts", None) or []
    return host.lower() in {h.lower() for h in learned if isinstance(h, str)}


# ─── Public API ───────────────────────────────────────────────────────


def decide(
    url: str,
    ctx: Any | None,
    stage: str | None,
) -> ProxyGateDecision:
    """The gate. Returns a :class:`ProxyGateDecision` for this probe.

    Pure function — no mutation, no I/O. Safe to call multiple times
    for the same ``(url, ctx, stage)`` triple.

    Parameters
    ----------
    url:
        Absolute URL the caller wants to fetch.
    ctx:
        An :class:`AdapterContext`-like object. Required for any
        positive ``allow=True`` decision because budgets live on it.
        ``None`` → fail-closed with ``MISSING_CTX_OR_STAGE``.
    stage:
        Adapter telemetry stage name (matches
        ``_adapter_telemetry`` ``tier_key`` suffix — e.g.
        ``"sc_probe"``, ``"wp_probe"``, ``"prospectportal_probe"``).
        ``None`` → fail-closed.

    Returns
    -------
    A :class:`ProxyGateDecision`. ``allow`` is the only field callers
    need to gate I/O; ``reason`` exists for telemetry; ``proxies()``
    is the convenience accessor for the proxies dict.
    """
    proxy_url = _env_proxy_url()
    host = (urlparse(url).hostname or "").lower()

    # Layer 0a — env kill switch. No proxy configured: nothing else
    # matters, the gate has no proxy to hand back.
    if not proxy_url:
        return ProxyGateDecision(
            allow=False,
            reason=ProxyDecisionReason.NO_PROXY_CONFIGURED,
            host=host,
        )

    # Strict-allow: missing context OR stage immediately fails closed.
    # The caller forgot to thread the gate inputs — we cannot evaluate
    # budgets or quality. Direct path only.
    if ctx is None or not stage:
        return ProxyGateDecision(
            allow=False,
            reason=ProxyDecisionReason.MISSING_CTX_OR_STAGE,
            proxy_url=None,
            host=host,
        )

    # Layer 0b — per-property hop budget. Each property is allotted
    # at most ``proxy_max_hops`` (default 3) proxy attempts across
    # residential AND Web-Unlocker. Once burned, every subsequent
    # probe goes direct regardless of how attractive Layer 4 thinks
    # it is. Prevents a single broken property monopolising spend.
    max_hops = int(getattr(ctx, "proxy_max_hops", DEFAULT_MAX_PROXY_HOPS) or DEFAULT_MAX_PROXY_HOPS)
    used_hops = int(getattr(ctx, "proxy_hops_used", 0) or 0)
    if used_hops >= max_hops:
        return ProxyGateDecision(
            allow=False,
            reason=ProxyDecisionReason.PROPERTY_HOP_BUDGET_EXHAUSTED,
            host=host,
        )

    # Layer 0c — per-property byte budget. Belt-and-braces against a
    # runaway probe that returns multi-MB bodies.
    max_bytes = int(getattr(ctx, "proxy_max_bytes", DEFAULT_MAX_PROXY_BYTES) or DEFAULT_MAX_PROXY_BYTES)
    used_bytes = int(getattr(ctx, "proxy_bytes_used", 0) or 0)
    if used_bytes >= max_bytes:
        return ProxyGateDecision(
            allow=False,
            reason=ProxyDecisionReason.PROPERTY_BYTE_BUDGET_EXHAUSTED,
            host=host,
        )

    # Layer 1 — same-eTLD+1 origin. The patchright entry-page fetch
    # has already minted CF clearance cookies on the marketing host;
    # the existing ``_with_clearance`` shim re-attaches them on
    # same-origin sub-requests. Burning proxy bandwidth on a probe
    # we can serve from clearance cookies is pure waste.
    marketing_host = (urlparse(getattr(ctx, "base_url", "") or "").hostname or "").lower()
    if host and marketing_host and _etld_plus_one(host) == _etld_plus_one(marketing_host):
        return ProxyGateDecision(
            allow=False,
            reason=ProxyDecisionReason.SAME_ORIGIN,
            host=host,
        )

    # Layer 2 — static allowlist. Known cross-origin CF-walled hosts
    # go straight to proxy. Saves the wasted direct-roundtrip-then-
    # escalate cost (every direct probe to ``*.securecafe.com`` from
    # GCP eats ~700ms + a wasted curl_cffi handshake).
    if host and _matches_required_host(host):
        return ProxyGateDecision(
            allow=True,
            reason=ProxyDecisionReason.HOST_ALLOWLIST,
            proxy_url=proxy_url,
            host=host,
        )

    # Layer 3 — profile-learned block memory. A prior run direct-
    # fetched this host and got CF challenge; the profile updater
    # appended ``host`` to ``api_hints.proxy_required_hosts``.
    if host and _profile_says_host_needs_proxy(ctx, host):
        return ProxyGateDecision(
            allow=True,
            reason=ProxyDecisionReason.PROFILE_LEARNED_BLOCK,
            proxy_url=proxy_url,
            host=host,
        )

    # Layer 4 — quality gate. Only used when:
    #   * the stage is structurally a cross-origin probe (sc_probe,
    #     wp_probe, prospectportal_probe, etc.), AND
    #   * the property's PMS detection confidence is high enough that
    #     we have positive reason to believe the probe will succeed
    #     once we get past the CF edge.
    #
    # The cap on per-property hops (Layer 0b) means Layer 4 is
    # structurally limited to at most 3 fires per property — even if
    # the cascade would naturally have invoked 6 sc_probe stages,
    # only the first 3 can route through proxy. This matches the
    # operator's "max 3 hops per property using proxy" requirement.
    if stage not in _PROXY_ELIGIBLE_STAGES:
        return ProxyGateDecision(
            allow=False,
            reason=ProxyDecisionReason.STAGE_NOT_PROXY_ELIGIBLE,
            host=host,
        )

    detected = getattr(ctx, "detected", None)
    confidence = float(getattr(detected, "confidence", 0.0) or 0.0)
    if confidence < _MIN_PROXY_DETECTION_CONFIDENCE:
        return ProxyGateDecision(
            allow=False,
            reason=ProxyDecisionReason.DETECTION_CONFIDENCE_BELOW_THRESHOLD,
            host=host,
        )

    return ProxyGateDecision(
        allow=True,
        reason=ProxyDecisionReason.HIGH_CONFIDENCE_CROSS_ORIGIN,
        proxy_url=proxy_url,
        host=host,
    )


#: 2026-05-24 canary fix — connection-level error signatures that
#: SUGGEST an IP-based block (worth proxy retry) vs ones that are
#: terminal (DNS failure, SSL/cert mismatch — proxy can't fix these).
#:
#: ``error_signature`` is populated by ``response_classifier.classify``
#: on TRANSIENT / HARD_FAIL outcomes (see fetch/contracts.py). The
#: classifier uses Chromium / curl_cffi error codes verbatim.
#:
#: 2026-05-24 update — tightened after user feedback "high confidence
#: signals only". Removed broader patterns like NETWORK_ACCESS_DENIED,
#: TUNNEL_CONNECTION_FAILED, and bare TIMEOUT (which can match flake)
#: in favour of the 3 patterns that ALMOST ALWAYS indicate an IP-tier
#: block. The narrower set reduces false-positives (network flake
#: incorrectly admitted) at the cost of missing some genuine IP blocks
#: that surface under broader signatures — those will exit the loop
#: via retry-policy exhaustion → mark_host_needs_proxy → next-run
#: profile-learned admission instead.
_PROXY_WORTH_RETRY_SIGS: frozenset[str] = frozenset({
    # Chromium net errors that consistently indicate IP-based block.
    # CONNECTION_TIMED_OUT alone often = flake; we keep it because
    # the 3-attempt retry-policy already filters single-occurrence
    # flake (TRANSIENT path), so by the time fetcher.py emits
    # HARD_FAIL with TIMED_OUT, the IP block hypothesis is strong.
    "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_CONNECTION_RESET",
})

#: Signatures that are terminal regardless of egress — DNS authority
#: rejected the name, or TLS handshake failed in a way the proxy can't
#: rescue. Distinct from the worth-retry list so the gate's reason
#: field tells operators exactly why the escalation was denied.
_PROXY_TERMINAL_SIGS: frozenset[str] = frozenset({
    "ERR_NAME_NOT_RESOLVED",
    "NS_ERROR_UNKNOWN_HOST",
    "DNS_PROBE_FINISHED_NXDOMAIN",
    "ERR_SSL_PROTOCOL_ERROR",
    "ERR_CERT_AUTHORITY_INVALID",
    "ERR_CERT_DATE_INVALID",
    "ERR_CERT_COMMON_NAME_INVALID",
})

#: 2026-05-24 — HIGH-CONFIDENCE bot-block signatures.
#:
#: response_classifier.classify emits a ``sig`` (signature) string alongside
#: the BOT_BLOCKED outcome. We only admit proxy escalation when the sig
#: identifies a SPECIFIC bot-block provider — CF / datadome / akamai /
#: imperva / perimeterx. Bare ``HTTP_403`` and ``BOT_BLOCKED`` (no
#: provider identified) are LOW-confidence: a bare 403 can be auth-
#: required, IP-allowlist mismatch, or any number of non-egress causes
#: that proxy retry can't fix.
#:
#: Tokens are matched case-insensitively as SUBSTRINGS of the signature
#: string. This catches both the upper-case enum-style sigs the
#: classifier emits (CF_CHALLENGE, CAPTCHA_CLOUDFLARE, DATADOME_200,
#: AKAMAI_BM_200, etc.) AND the lower-case provider names emitted by
#: block_signatures.match_block_signature (cloudflare, datadome,
#: akamai_bm, perimeterx, imperva).
_HIGH_CONFIDENCE_BOT_BLOCK_TOKENS: frozenset[str] = frozenset({
    "cloudflare",
    "cf_challenge",
    "cf_chal",
    "captcha_cloudflare",
    "captcha_recaptcha",
    "captcha_hcaptcha",
    "captcha_turnstile",
    "datadome",
    "akamai",
    "perimeterx",
    "px_block",
    "imperva",
    "incapsula",
})


def _is_high_confidence_bot_block(sig: str | None) -> bool:
    """``True`` iff *sig* matches a known bot-block provider (CF /
    datadome / akamai / imperva / perimeterx). Bare ``HTTP_403`` and
    ``BOT_BLOCKED`` return False — those are LOW confidence and the
    cascade should NOT spend proxy budget on them.

    Case-insensitive substring match against
    :data:`_HIGH_CONFIDENCE_BOT_BLOCK_TOKENS`.
    """
    if not sig:
        return False
    s = sig.lower()
    return any(token in s for token in _HIGH_CONFIDENCE_BOT_BLOCK_TOKENS)


def decide_l1_escalate(
    url: str,
    prior_outcome: str | None,
    *,
    proxy_hops_used: int = 0,
    max_hops: int = DEFAULT_L1_MAX_ESCALATION_HOPS,
    pool_has_proxies: bool = False,
    error_signature: str | None = None,
) -> ProxyGateDecision:
    """Gate for L1 entry-URL proxy escalation after a direct-fetch CF block.

    Distinct from :func:`decide` in three important ways:

    1. **Trigger semantics.** This gate is *escalation-only* — it
       returns ``allow=True`` exclusively when the prior direct fetch
       attempt returned ``BOT_BLOCKED`` (CF challenge, WAF wall, etc.).
       It is not a general "should this request use proxy?" gate. Any
       other prior outcome (OK, TRANSIENT, HARD_FAIL, RATE_LIMITED,
       DEAD_URL) fails-closed — those have their own retry paths
       (RetryPolicy + the existing rate-limit escalation at
       :file:`fetcher.py`).

    2. **Same-origin is NOT a denial.** The cross-origin probe gate
       fails-closed on same-eTLD+1 because patchright clearance covers
       the marketing host. At L1 the URL IS the marketing host — the
       whole point of the escalation is "the marketing host CF-walled
       us; route through a different egress".

    3. **Proxy URL is supplied by caller, not the gate.** The L1
       fetcher draws from its own :class:`ProxyPool` (configured via
       ``PROXY_POOL_URLS``), separate from ``PROBE_PROXY_URL`` which
       the cross-origin gate consumes. ``proxy_url`` on the returned
       decision is therefore always ``None`` — callers ignore that
       field and use their pool's :meth:`ProxyPool.pick` instead.

    Parameters
    ----------
    url:
        The L1 entry URL that was just attempted directly.
    prior_outcome:
        The :class:`FetchOutcome` value (as a string) from the direct
        attempt. Only ``"BOT_BLOCKED"`` admits escalation; anything
        else fails-closed with
        :attr:`ProxyDecisionReason.L1_PRIOR_OUTCOME_NOT_BLOCKED`.
    proxy_hops_used:
        Number of L1 escalation hops already burned for this property
        in this fetch task. Caller maintains the counter so each
        fetch() invocation gets its own budget — multiple properties
        don't share the cap.
    max_hops:
        Per-fetch-task escalation cap. Defaults to
        :data:`DEFAULT_L1_MAX_ESCALATION_HOPS` (1) — a second
        escalation is virtually never useful (if the proxy ALSO got
        CF-blocked, the third attempt won't recover) and burning
        another hop just inflates the proxy bill.
    pool_has_proxies:
        ``True`` iff the caller's :class:`ProxyPool` has ≥1 configured
        URL (call sites typically pass ``bool(len(proxy_pool))``).
        Saves a wasted gate-decision call when no proxy is configured.

    Returns
    -------
    A :class:`ProxyGateDecision`. ``allow=True`` only when **all** of
    the gates pass:
      * prior outcome was BOT_BLOCKED
      * caller has ≥1 proxy in their pool
      * per-task hop budget not exhausted

    The decision's ``reason`` field tells telemetry / logs exactly why
    this call admitted or denied — important for diagnosing why the
    proxy bill spiked or didn't.
    """
    host = (urlparse(url).hostname or "").lower()
    # 1. Trigger gate — admission requires HIGH-CONFIDENCE evidence the
    #    failure is egress-related (vs auth-required / network flake /
    #    DNS / SSL). Proxy retry is expensive ($ + wall-time + risk of
    #    burning a healthy proxy on a doomed retry), so admission is
    #    intentionally narrow:
    #
    #    (a) BOT_BLOCKED — admit ONLY when the response_classifier
    #        identified a specific bot-block provider (Cloudflare /
    #        datadome / akamai / imperva / perimeterx) in the response
    #        body or headers. Bare ``HTTP_403`` and bare ``BOT_BLOCKED``
    #        DENY — those low-confidence signatures could be
    #        auth-required, IP-allowlist mismatch, or any non-egress
    #        cause that proxy retry can't fix.
    #
    #    (b) EMPTY_BODY — 200 with <16-byte body; high-confidence on its
    #        own because the response_classifier already filtered this
    #        from normal responses. The signature IS the high-confidence
    #        signal — no further gate needed.
    #
    #    (c) HARD_FAIL with TIGHT connection-error signature
    #        (CONNECTION_REFUSED / TIMED_OUT / RESET only — not the
    #        broader set we initially considered). Combined with the
    #        upstream retry-policy filter (TRANSIENT path absorbs flake
    #        before promoting to HARD_FAIL), this gives high confidence
    #        the failure is an IP-tier block.
    #
    #    DNS failure and SSL/cert errors are TERMINAL — proxy can't fix
    #    them. Fail-closed with the DNS_FAIL_NOT_ADMITTED /
    #    SSL_FAIL_NOT_ADMITTED reasons so the operator sees exactly why.
    #
    #    Low-confidence signals (bare 403, bare BOT_BLOCKED, unknown
    #    signature) deny with L1_BOT_BLOCKED_LOW_CONFIDENCE /
    #    L1_UNREACHABLE_LOW_CONFIDENCE so the analyzer can measure how
    #    often we filter low-confidence signals (the next-run profile
    #    learning catches recurring low-confidence failures separately).
    outcome_upper = (prior_outcome or "").upper()
    sig_upper = (error_signature or "").upper()
    admit_reason: ProxyDecisionReason | None = None
    if outcome_upper == "BOT_BLOCKED":
        # High-confidence check: signature must name a specific
        # bot-block provider. Bare BOT_BLOCKED / HTTP_403 deny.
        if _is_high_confidence_bot_block(error_signature):
            admit_reason = ProxyDecisionReason.L1_CF_ESCALATION_ADMITTED
        else:
            return ProxyGateDecision(
                allow=False,
                reason=ProxyDecisionReason.L1_BOT_BLOCKED_LOW_CONFIDENCE,
                host=host,
            )
    elif outcome_upper == "EMPTY_BODY":
        # EMPTY_BODY is itself a high-confidence signal (200 with
        # <16 bytes is a very specific bot-block fingerprint that
        # the classifier already filtered).
        admit_reason = ProxyDecisionReason.L1_EMPTY_BODY_ADMITTED
    elif outcome_upper in {"TRANSIENT", "HARD_FAIL"}:
        # Connection-level signatures the proxy might rescue.
        if any(s in sig_upper for s in _PROXY_WORTH_RETRY_SIGS):
            admit_reason = ProxyDecisionReason.L1_UNREACHABLE_ADMITTED
        elif any(s in sig_upper for s in _PROXY_TERMINAL_SIGS):
            # Surface DNS vs SSL distinctly so telemetry can split them.
            if "RESOLV" in sig_upper or "NXDOMAIN" in sig_upper or "UNKNOWN_HOST" in sig_upper:
                return ProxyGateDecision(
                    allow=False,
                    reason=ProxyDecisionReason.L1_DNS_FAIL_NOT_ADMITTED,
                    host=host,
                )
            return ProxyGateDecision(
                allow=False,
                reason=ProxyDecisionReason.L1_SSL_FAIL_NOT_ADMITTED,
                host=host,
            )
        else:
            # Unknown / missing signature on TRANSIENT / HARD_FAIL is
            # LOW-confidence — caller didn't populate it OR it's a new
            # variant. Don't burn proxy on it; surface the deny reason.
            return ProxyGateDecision(
                allow=False,
                reason=ProxyDecisionReason.L1_UNREACHABLE_LOW_CONFIDENCE,
                host=host,
            )
    if admit_reason is None:
        return ProxyGateDecision(
            allow=False,
            reason=ProxyDecisionReason.L1_PRIOR_OUTCOME_NOT_BLOCKED,
            host=host,
        )
    # 2. No proxy URL configured in the L1 pool → no escalation possible.
    #    Reuse the existing NO_PROXY_CONFIGURED reason because the
    #    operator-visible effect is identical to the cross-origin gate
    #    saying "no proxy available."
    if not pool_has_proxies:
        return ProxyGateDecision(
            allow=False,
            reason=ProxyDecisionReason.NO_PROXY_CONFIGURED,
            host=host,
        )
    # 3. Per-task hop budget. Independent of the cross-origin
    #    PROPERTY_HOP_BUDGET so a property that's exhausted its
    #    probe budget still gets one L1 escalation.
    if proxy_hops_used >= max_hops:
        return ProxyGateDecision(
            allow=False,
            reason=ProxyDecisionReason.L1_HOP_BUDGET_EXHAUSTED,
            host=host,
        )
    # Admit. ``proxy_url`` deliberately stays None — the L1 fetcher's
    # ProxyPool supplies the actual URL via pick(). The decision is the
    # *policy* decision; the pool is the *resource*.
    #
    # 2026-05-24: the reason carries the specific admission path
    # (CF_ESCALATION / EMPTY_BODY / UNREACHABLE) so the analyzer can
    # split escalation effectiveness by trigger.
    return ProxyGateDecision(
        allow=True,
        reason=admit_reason,
        proxy_url=None,
        host=host,
    )


def record_use(
    ctx: Any,
    decision: ProxyGateDecision,
    *,
    response_bytes: int = 0,
) -> None:
    """Bump the per-property proxy counters after a successful proxy use.

    Idempotent w.r.t. ``allow=False`` decisions — those never consumed
    proxy bandwidth, so this function is a no-op for them. Callers may
    safely call ``record_use`` for every probe response without
    pre-checking ``decision.allow``.

    Parameters
    ----------
    ctx:
        The same AdapterContext-like object passed to :func:`decide`.
    decision:
        The :class:`ProxyGateDecision` returned by :func:`decide`.
    response_bytes:
        Length of the response body in bytes (best-effort). Bumps the
        byte budget so Layer 0c can fire next call when a property
        exceeds its allowance.
    """
    if not decision.allow:
        return
    if ctx is None:
        return
    try:
        ctx.proxy_hops_used = int(getattr(ctx, "proxy_hops_used", 0) or 0) + 1
        if response_bytes > 0:
            ctx.proxy_bytes_used = (
                int(getattr(ctx, "proxy_bytes_used", 0) or 0) + int(response_bytes)
            )
    except (AttributeError, TypeError):
        # ctx is a Mock or read-only dataclass — bookkeeping fails
        # gracefully so the gate doesn't break the probe.
        log.debug("proxy_gate.record_use: failed to bump counters on ctx=%r", ctx)


def mark_host_needs_proxy(ctx: Any, host: str) -> None:
    """Append ``host`` to the profile's ``proxy_required_hosts`` list.

    Idempotent. Caps the list at 30 entries (oldest evicted) so a
    misbehaving property can't grow the profile payload unbounded.

    Called from the escalation path in :mod:`_probe` when a direct
    fetch returns a CF challenge body — the next run's Layer 3 picks
    up the learned block without a runtime decision.
    """
    if ctx is None or not host:
        return
    profile = getattr(ctx, "profile", None)
    if profile is None:
        return
    api_hints = getattr(profile, "api_hints", None)
    if api_hints is None:
        return
    try:
        existing = list(getattr(api_hints, "proxy_required_hosts", None) or [])
    except (AttributeError, TypeError):
        return
    host_lower = host.lower()
    if host_lower in {h.lower() for h in existing if isinstance(h, str)}:
        return
    existing.append(host_lower)
    # Keep the most-recent 30 entries — sensible cap matching the
    # ApiHints.blocked_endpoints precedent.
    try:
        api_hints.proxy_required_hosts = existing[-30:]
    except (AttributeError, TypeError):
        log.debug("proxy_gate.mark_host_needs_proxy: api_hints read-only on ctx=%r", ctx)


__all__ = [
    "ProxyDecisionReason",
    "ProxyGateDecision",
    "DEFAULT_MAX_PROXY_HOPS",
    "DEFAULT_MAX_PROXY_BYTES",
    "DEFAULT_L1_MAX_ESCALATION_HOPS",
    "decide",
    "decide_l1_escalate",
    "record_use",
    "mark_host_needs_proxy",
]
