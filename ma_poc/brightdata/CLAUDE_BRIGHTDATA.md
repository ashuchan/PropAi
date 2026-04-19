# CLAUDE_BRIGHTDATA.md

**Goal:** Integrate Bright Data proxies into the Jugnu L1 Fetch layer. Support the tiered proxy model from the architecture (`DIRECT → DATACENTER → RESIDENTIAL → UNBLOCKER`) with per-property escalation based on observed failure signals. Playwright and any raw HTTP calls must both route through the same proxy abstraction. Credentials live in Secret Manager; the runtime code never sees them in env files.

**Read before starting:**
- `Jugnu_Robust_Crawler_Architecture.docx` §4.3 (L1 Fetch layer — stealth, rate limit, retry, proxy pool) — this handoff implements the "proxy pool" part
- `Jugnu_Deployment_Architecture_GCP.docx` — especially the tiered-proxy cost model
- `scripts/jugnu_runner.py` lines 99-115 and 920-945 — the existing `--proxy` CLI surface and how it's threaded into `run_jugnu`
- `ma_poc/fetch/` (if it exists from J1) — the fetcher module this handoff modifies
- Bright Data's current Playwright docs: https://docs.brightdata.com/integrations/playwright

**Prerequisite:** A Bright Data account with at least one Residential zone configured. Human bootstrap instructions are in the companion document `BRIGHT_DATA_SETUP.md` — Claude Code does not do this part.

---

## 1. Scope

What this handoff produces:

- `ma_poc/fetch/proxy/` — new subpackage containing the proxy abstraction
  - `ma_poc/fetch/proxy/__init__.py`
  - `ma_poc/fetch/proxy/base.py` — `ProxyTier` enum, `ProxyConfig` dataclass, `ProxyProvider` protocol
  - `ma_poc/fetch/proxy/brightdata.py` — Bright Data implementation
  - `ma_poc/fetch/proxy/none_provider.py` — direct-connection provider (no proxy)
  - `ma_poc/fetch/proxy/selector.py` — picks the tier for a given property based on profile state
  - `ma_poc/fetch/proxy/escalation.py` — decides when to escalate a property's tier based on fetch failures
- Integration into the existing fetch code: replace any direct `proxy` string passing with `ProxyConfig` construction via the provider
- Cost accounting: every fetch logs bytes through proxy to the cost ledger (L5) tagged with tier
- Unit tests in `tests/fetch/proxy/`
- Integration test against a real Bright Data test endpoint (gated by env var; skipped in CI)
- Minimal schema migration adding `proxy_tier` and `proxy_fail_count` columns to the `properties` table

What this handoff does **not** produce:
- Web Unlocker or Browser API integration (that's the UNBLOCKER tier, covered in a follow-up handoff)
- CAPTCHA solver integration (separate concern; parked in DLQ for now per BRD open question)
- Changes to how proxy credentials are stored (already specified in `CLAUDE_TERRAFORM.md` — Secret Manager slots exist)
- Changes to `jugnu_runner.py`'s CLI — the existing `--proxy` flag is preserved for manual override; programmatic selection happens below the CLI layer

---

## 2. Why a provider abstraction

A naive implementation would pass `proxy="http://user:pass@brd.superproxy.io:33335"` around. That works for Bright Data today and breaks the day you add Zyte, Smartproxy, or an internal proxy rotator. The abstraction is three layers:

```
┌─────────────────────────────────────────────────────────────┐
│  Caller (L1 Fetcher, per-property loop)                     │
│  wants: "give me fetch params for this property"            │
└──────────────────────────────┬──────────────────────────────┘
                                │
┌──────────────────────────────▼──────────────────────────────┐
│  ProxySelector                                              │
│  reads: property profile (current tier, fail count)          │
│  picks: which tier to use for this fetch                    │
└──────────────────────────────┬──────────────────────────────┘
                                │
┌──────────────────────────────▼──────────────────────────────┐
│  ProxyProvider (BrightDataProvider, NoneProvider, ...)      │
│  translates: (tier, property) → ProxyConfig                 │
│  owns: credential construction, session ID, country/city    │
└──────────────────────────────┬──────────────────────────────┘
                                │
┌──────────────────────────────▼──────────────────────────────┐
│  ProxyConfig → Playwright proxy= arg OR httpx proxies= arg  │
└─────────────────────────────────────────────────────────────┘
```

Three benefits this gets us:
- Swapping providers is a one-class change, not a repo-wide grep
- Tier selection logic is testable without live credentials
- The "no proxy" case (`NoneProvider`) is a first-class citizen, not a special-case branch scattered through the fetcher

---

## 3. Core types

### `ma_poc/fetch/proxy/base.py`

```python
"""Proxy abstraction — tier enum, config dataclass, provider protocol."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class ProxyTier(str, Enum):
    """Tier order matters: higher ordinal = more expensive, higher bypass capability."""
    DIRECT      = "direct"       # no proxy, direct GCP egress
    DATACENTER  = "datacenter"   # Bright Data datacenter zone — ~$0.50/GB
    RESIDENTIAL = "residential"  # Bright Data residential zone — ~$4-8/GB
    UNBLOCKER   = "unblocker"    # Bright Data Web Unlocker — per-request billing; future handoff

    def next_tier(self) -> "ProxyTier | None":
        """Return the next-higher tier, or None if already at the top."""
        order = [ProxyTier.DIRECT, ProxyTier.DATACENTER, ProxyTier.RESIDENTIAL, ProxyTier.UNBLOCKER]
        idx = order.index(self)
        return order[idx + 1] if idx + 1 < len(order) else None


@dataclass(frozen=True)
class ProxyConfig:
    """Everything a fetcher needs to route a request through a proxy.

    None values mean 'do not use a proxy' — the DIRECT tier returns a
    ProxyConfig with server=None.
    """
    tier: ProxyTier
    server: str | None = None      # e.g. "http://brd.superproxy.io:33335"
    username: str | None = None
    password: str | None = None
    # For observability only — never used for actual routing decisions
    session_id: str | None = None

    @property
    def is_direct(self) -> bool:
        return self.server is None

    def to_playwright(self) -> dict | None:
        """Format for playwright.chromium.launch(proxy=...). None = no proxy."""
        if self.is_direct:
            return None
        return {
            "server": self.server,
            "username": self.username,
            "password": self.password,
        }

    def to_httpx(self) -> dict | None:
        """Format for httpx.AsyncClient(proxies=...). None = no proxy."""
        if self.is_direct:
            return None
        # Construct the single URL form httpx expects
        from urllib.parse import quote
        user = quote(self.username or "", safe="")
        pwd  = quote(self.password or "", safe="")
        # self.server already includes scheme
        scheme, rest = self.server.split("://", 1)
        return {
            "http://":  f"{scheme}://{user}:{pwd}@{rest}",
            "https://": f"{scheme}://{user}:{pwd}@{rest}",
        }


class ProxyProvider(Protocol):
    """Every provider implementation satisfies this interface."""

    def get_config(
        self,
        *,
        tier: ProxyTier,
        canonical_id: str,
        country: str = "us",
    ) -> ProxyConfig:
        """Return the proxy config for this property at this tier.

        canonical_id is used to derive a stable session ID per property so
        retries on the same property hit the same IP (up to the provider's
        session TTL). This matches the L1 spec's 'per-host session affinity'
        requirement.
        """
        ...
```

### `ma_poc/fetch/proxy/none_provider.py`

```python
"""Direct-connection provider — returns no-proxy config for every tier."""
from ma_poc.fetch.proxy.base import ProxyConfig, ProxyTier


class NoneProvider:
    """Provider that never proxies — useful for testing and local dev."""

    def get_config(self, *, tier: ProxyTier, canonical_id: str, country: str = "us") -> ProxyConfig:
        return ProxyConfig(tier=ProxyTier.DIRECT)
```

---

## 4. The Bright Data provider

### `ma_poc/fetch/proxy/brightdata.py`

The critical thing to get right is the **username format**. Bright Data encodes tier, session, country, and city into the username string, not separate parameters. Getting any part of this wrong produces silent failures — requests succeed but route through the wrong IP pool.

Verified username formats from current Bright Data docs:

| Purpose | Username format |
|---|---|
| Basic | `brd-customer-{CUSTOMER_ID}-zone-{ZONE_NAME}` |
| Country targeting | `brd-customer-{CUSTOMER_ID}-zone-{ZONE_NAME}-country-{cc}` (ISO-3166 2-letter, lowercase) |
| City targeting | `brd-customer-{CUSTOMER_ID}-zone-{ZONE_NAME}-country-{cc}-city-{cityname}` |
| Sticky session | Append `-session-{session_id}` to any of the above |

Ports:
- HTTP/HTTPS: `33335` (current) — older docs may show `22225` which is deprecated
- SOCKS5: `22228`

Host: `brd.superproxy.io`

```python
"""Bright Data proxy provider implementation.

Credentials are loaded from env at module-init time. The env vars are populated
from Secret Manager in production (see CLAUDE_TERRAFORM.md §4.5) and from a
.env file in local dev.

Separate zones per tier — datacenter and residential proxies are different
products with different pricing, and Bright Data requires them to be different
zones in the dashboard.
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass

from ma_poc.fetch.proxy.base import ProxyConfig, ProxyTier

log = logging.getLogger(__name__)

BRIGHTDATA_HOST = os.environ.get("BRIGHTDATA_HOST", "brd.superproxy.io")
BRIGHTDATA_PORT = int(os.environ.get("BRIGHTDATA_PORT", "33335"))


@dataclass(frozen=True)
class BrightDataZone:
    """A single Bright Data zone — one per tier."""
    zone_name: str
    password: str


class BrightDataProvider:
    """Provider that constructs per-request proxy configs for Bright Data.

    Requires env vars:
      BRIGHTDATA_CUSTOMER_ID
      BRIGHTDATA_DC_ZONE, BRIGHTDATA_DC_PASSWORD         (datacenter zone)
      BRIGHTDATA_RESI_ZONE, BRIGHTDATA_RESI_PASSWORD     (residential zone)

    The UNBLOCKER tier raises NotImplementedError; it's a future handoff.
    """

    def __init__(self) -> None:
        self.customer_id = self._require("BRIGHTDATA_CUSTOMER_ID")
        self.zones: dict[ProxyTier, BrightDataZone] = {
            ProxyTier.DATACENTER: BrightDataZone(
                zone_name=self._require("BRIGHTDATA_DC_ZONE"),
                password=self._require("BRIGHTDATA_DC_PASSWORD"),
            ),
            ProxyTier.RESIDENTIAL: BrightDataZone(
                zone_name=self._require("BRIGHTDATA_RESI_ZONE"),
                password=self._require("BRIGHTDATA_RESI_PASSWORD"),
            ),
        }

    @staticmethod
    def _require(key: str) -> str:
        val = os.environ.get(key)
        if not val:
            raise RuntimeError(
                f"{key} is required for BrightDataProvider. "
                f"Set it via Secret Manager in prod or .env in dev. "
                f"See BRIGHT_DATA_SETUP.md for credential sourcing."
            )
        return val

    def get_config(
        self,
        *,
        tier: ProxyTier,
        canonical_id: str,
        country: str = "us",
    ) -> ProxyConfig:
        if tier == ProxyTier.DIRECT:
            return ProxyConfig(tier=ProxyTier.DIRECT)

        if tier == ProxyTier.UNBLOCKER:
            raise NotImplementedError(
                "UNBLOCKER tier requires Web Unlocker integration — future handoff"
            )

        zone = self.zones[tier]
        session_id = self._session_id(canonical_id)
        username = self._build_username(zone.zone_name, country, session_id)
        return ProxyConfig(
            tier=tier,
            server=f"http://{BRIGHTDATA_HOST}:{BRIGHTDATA_PORT}",
            username=username,
            password=zone.password,
            session_id=session_id,
        )

    def _session_id(self, canonical_id: str) -> str:
        """Stable session ID per property — retries hit the same IP."""
        # Use a short, stable hash. Bright Data's session IDs accept any
        # alphanumeric string; keep it short to avoid username length issues.
        # Using hashlib.sha256 per codebase conventions (never built-in hash()).
        digest = hashlib.sha256(canonical_id.encode()).hexdigest()
        return f"s{digest[:10]}"

    def _build_username(self, zone: str, country: str, session_id: str) -> str:
        # Format: brd-customer-{id}-zone-{zone}-country-{cc}-session-{sid}
        # Order matters — country before session per Bright Data docs.
        return (
            f"brd-customer-{self.customer_id}"
            f"-zone-{zone}"
            f"-country-{country.lower()}"
            f"-session-{session_id}"
        )
```

### Important Playwright-specific detail: `ignoreHTTPSErrors`

Bright Data's residential proxies terminate TLS at the proxy. The cert chain presented to the browser is not the target site's cert — it's Bright Data's proxy cert. Without `ignoreHTTPSErrors=True` or installing Bright Data's root CA, Playwright aborts residential-proxy requests with `ERR_CERT_AUTHORITY_INVALID`.

**Claude Code must update the Playwright launch code** (location depends on how J1 wired L1 fetch, likely in `ma_poc/fetch/browser.py` or equivalent) to pass `ignore_https_errors=True` when a non-direct proxy is in use. The existing `browser.new_context()` call becomes:

```python
context = await browser.new_context(
    ignore_https_errors=proxy_config is not None and not proxy_config.is_direct,
    # ... other existing args
)
```

**Never set `ignore_https_errors=True` when the proxy is DIRECT.** Direct-connection requests should still verify certs normally; ignoring TLS errors in that case hides real MITM or misconfiguration problems.

The alternative — installing Bright Data's CA cert — is cleaner but adds an image-build step and a certificate file to maintain. For the POC, `ignore_https_errors` at the context level is the right trade-off. Revisit if the security review asks.

---

## 5. The selector — picking the right tier

### `ma_poc/fetch/proxy/selector.py`

```python
"""Selects which proxy tier to use for a given property fetch.

The selector reads from the property's profile (persisted in Postgres) and
returns the current tier. The profile is updated by the escalation module
after each fetch.

Initial tier logic:
  - New property (no profile yet): start at DATACENTER
  - Property profile says tier=X: use X
  - Manual override via ProxyOverride: use the override tier

Rationale for starting at DATACENTER (not DIRECT):
  Most target PMS sites will accept datacenter IPs, and the tier cost model
  makes DATACENTER ~16x cheaper than RESIDENTIAL. Starting at DIRECT makes
  GCP egress IPs accumulate block-reputation faster across properties.
  The empirical "Phase 1 no-proxy measurement" exercise should precede this
  handoff; if it concludes DIRECT is safe for a subset of properties, those
  properties' profiles can be seeded with tier=DIRECT.
"""
from __future__ import annotations

from dataclasses import dataclass

from ma_poc.fetch.proxy.base import ProxyTier


@dataclass(frozen=True)
class ProxyOverride:
    """Force a specific tier regardless of profile — for testing/manual ops."""
    tier: ProxyTier


class ProxySelector:
    def __init__(self, default_tier: ProxyTier = ProxyTier.DATACENTER) -> None:
        self.default_tier = default_tier

    def pick(
        self,
        *,
        profile_tier: str | None,   # from properties.profile['proxy_tier']
        override: ProxyOverride | None = None,
    ) -> ProxyTier:
        if override is not None:
            return override.tier
        if profile_tier is None:
            return self.default_tier
        try:
            return ProxyTier(profile_tier)
        except ValueError:
            # Corrupt profile value — fall back to default rather than crash
            return self.default_tier
```

### `ma_poc/fetch/proxy/escalation.py`

```python
"""Decides when to escalate a property's tier after a fetch failure.

Escalation rules (from the architecture doc):
  - 403 Forbidden                  → escalate once
  - 407 Proxy Authentication       → DO NOT escalate (it's our problem, not theirs)
  - 429 Too Many Requests          → escalate once (signals rate limit at our tier)
  - CAPTCHA challenge detected     → escalate once
  - Cloudflare challenge detected  → escalate once
  - Timeout                        → count toward fail streak, escalate at 3 consecutive
  - 2xx / 3xx                      → reset fail streak, no escalation

De-escalation:
  - After 30 consecutive successes at tier T, try tier T-1 for the next fetch.
  - Cost optimization: properties that moved up due to transient issues
    should drift back down.
"""
from __future__ import annotations

from dataclasses import dataclass

from ma_poc.fetch.proxy.base import ProxyTier

ESCALATE_IMMEDIATELY_ON_STATUS = {403, 429}
ESCALATE_IMMEDIATELY_ON_SIGNAL = {"captcha", "cloudflare_challenge"}
TIMEOUT_FAIL_STREAK_THRESHOLD  = 3
DEESCALATE_SUCCESS_STREAK      = 30


@dataclass(frozen=True)
class EscalationDecision:
    new_tier: ProxyTier
    new_fail_count: int
    new_success_count: int
    reason: str   # for event logging


def decide(
    *,
    current_tier: ProxyTier,
    fail_count: int,
    success_count: int,
    fetch_status: int | None,
    fetch_signal: str | None,
    is_timeout: bool,
) -> EscalationDecision:
    # Success path
    if fetch_status is not None and 200 <= fetch_status < 400 and not is_timeout:
        new_success = success_count + 1
        if new_success >= DEESCALATE_SUCCESS_STREAK:
            # Try dropping a tier on the next fetch
            lower = _tier_below(current_tier)
            if lower is not None:
                return EscalationDecision(
                    new_tier=lower,
                    new_fail_count=0,
                    new_success_count=0,
                    reason=f"deescalated after {new_success} successes at {current_tier.value}",
                )
        return EscalationDecision(
            new_tier=current_tier,
            new_fail_count=0,
            new_success_count=new_success,
            reason="success — counter incremented",
        )

    # Immediate-escalation signals
    if fetch_status in ESCALATE_IMMEDIATELY_ON_STATUS or fetch_signal in ESCALATE_IMMEDIATELY_ON_SIGNAL:
        higher = current_tier.next_tier()
        if higher is None:
            return EscalationDecision(
                new_tier=current_tier,
                new_fail_count=fail_count + 1,
                new_success_count=0,
                reason=f"failure at top tier {current_tier.value}; sticking",
            )
        return EscalationDecision(
            new_tier=higher,
            new_fail_count=0,
            new_success_count=0,
            reason=f"escalated from {current_tier.value} to {higher.value} on {fetch_status or fetch_signal}",
        )

    # Timeout — accumulate, escalate at threshold
    if is_timeout:
        new_fail = fail_count + 1
        if new_fail >= TIMEOUT_FAIL_STREAK_THRESHOLD:
            higher = current_tier.next_tier()
            if higher is not None:
                return EscalationDecision(
                    new_tier=higher,
                    new_fail_count=0,
                    new_success_count=0,
                    reason=f"escalated on timeout streak ({new_fail})",
                )
        return EscalationDecision(
            new_tier=current_tier,
            new_fail_count=new_fail,
            new_success_count=0,
            reason=f"timeout — fail streak {new_fail}",
        )

    # Other 4xx/5xx — increment but don't escalate yet
    return EscalationDecision(
        new_tier=current_tier,
        new_fail_count=fail_count + 1,
        new_success_count=0,
        reason=f"failure — status {fetch_status}",
    )


def _tier_below(tier: ProxyTier) -> ProxyTier | None:
    order = [ProxyTier.DIRECT, ProxyTier.DATACENTER, ProxyTier.RESIDENTIAL, ProxyTier.UNBLOCKER]
    idx = order.index(tier)
    return order[idx - 1] if idx > 0 else None
```

---

## 6. Schema migration

New Alembic migration — depends on whether `CLAUDE_MIGRATIONS.md` has landed. If it has, add a migration under `infra/sql/versions/`:

```python
"""001_add_proxy_state_to_properties

Revision ID: 001_add_proxy_state
Revises: 000_initial_schema
"""
from alembic import op
import sqlalchemy as sa

revision = "001_add_proxy_state"
down_revision = "000_initial_schema"


def upgrade() -> None:
    # Nullable with server_default so existing rows fill in automatically.
    # Profile JSONB could also hold these, but separate columns let us index
    # and query "how many properties are at tier=residential" cheaply.
    op.add_column("properties",
        sa.Column("proxy_tier", sa.Text, nullable=False, server_default="datacenter"))
    op.add_column("properties",
        sa.Column("proxy_fail_count", sa.Integer, nullable=False, server_default="0"))
    op.add_column("properties",
        sa.Column("proxy_success_count", sa.Integer, nullable=False, server_default="0"))
    op.create_index("ix_properties_proxy_tier", "properties", ["proxy_tier"])


def downgrade() -> None:
    op.drop_index("ix_properties_proxy_tier", table_name="properties")
    op.drop_column("properties", "proxy_success_count")
    op.drop_column("properties", "proxy_fail_count")
    op.drop_column("properties", "proxy_tier")
```

---

## 7. Cost ledger integration

Every fetch through a proxy must log bytes + tier to the cost ledger. The architecture's L5 cost_ledger is the existing sink — if the fetcher currently writes to `cost_ledger.db`, extend that write with two columns: `proxy_tier` and `bytes_through_proxy`.

Rationale: bandwidth is billed per GB and tier. Without this, you can't answer "how much did RESIDENTIAL cost us today" without guesswork.

Minimal change to the fetch response handling:

```python
# After a successful fetch
cost_ledger.log(
    run_date=run_date,
    canonical_id=canonical_id,
    event_type="fetch",
    proxy_tier=proxy_config.tier.value,          # NEW
    bytes_in=len(response_body),                 # existing
    bytes_out=request_size,                      # existing
    cost_estimate_usd=estimate_cost(proxy_config.tier, len(response_body) + request_size),
)
```

`estimate_cost` is a small pure function with the current per-GB rate per tier as a constant map. Rates change; centralize them in `ma_poc/fetch/proxy/pricing.py`:

```python
"""Per-GB rates by tier for cost estimation. Update when Bright Data invoices change.

These are ESTIMATES — authoritative cost comes from Bright Data's dashboard.
Used only for real-time visibility and budget alerts.
"""
from ma_poc.fetch.proxy.base import ProxyTier

USD_PER_GB: dict[ProxyTier, float] = {
    ProxyTier.DIRECT:      0.0,
    ProxyTier.DATACENTER:  0.50,
    ProxyTier.RESIDENTIAL: 6.00,   # mid-tier estimate; PAYG is higher, subscription lower
    ProxyTier.UNBLOCKER:   0.0,    # UNBLOCKER is per-request, not per-GB — handled separately
}


def estimate_cost_usd(tier: ProxyTier, bytes_transferred: int) -> float:
    return (bytes_transferred / 1e9) * USD_PER_GB[tier]
```

---

## 8. Integration points in existing code

Claude Code must identify and modify the following (exact locations depend on how J1 was implemented — read the code first):

1. **The fetcher's top of loop** — where it currently accepts a `proxy: str | None`:
   - Replace the string with a `ProxyConfig` obtained from `provider.get_config(tier=selected_tier, canonical_id=...)`
   - Pass `config.to_playwright()` to `chromium.launch(proxy=...)` or `config.to_httpx()` to `httpx.AsyncClient(proxies=...)`

2. **The Playwright context creation** — add `ignore_https_errors=True` when proxy is non-direct (§4)

3. **The fetch result handling** — after each fetch, call `escalation.decide(...)` with the result; persist the new tier/fail/success counts to the `properties` table

4. **`jugnu_runner.py` `--proxy` CLI flag** — keep it, but its semantics change: it becomes a manual override that sets `ProxyOverride(tier=...)` for the entire run. Parse `--proxy datacenter`, `--proxy residential`, `--proxy direct` rather than a URL. Document the breaking change in the runbook; previous `--proxy http://...` URL form is removed.

5. **The L5 event ledger** — every escalation decision emits an event:
   ```
   {"type": "proxy_escalation", "canonical_id": "...", "from_tier": "datacenter", "to_tier": "residential", "reason": "403 on fetch"}
   ```
   This is critical for debugging later: "why did property X cost so much this run" answers from the event log.

---

## 9. Configuration — where credentials come from

**In production (Cloud Run):** env vars populated from Secret Manager via the `secret_key_ref` pattern in `CLAUDE_TERRAFORM.md` §4.6. Add these secret slots to the Terraform secrets module if not already present:

- `BRIGHTDATA_CUSTOMER_ID`
- `BRIGHTDATA_DC_ZONE` (not secret, but kept together for simplicity)
- `BRIGHTDATA_DC_PASSWORD`
- `BRIGHTDATA_RESI_ZONE`
- `BRIGHTDATA_RESI_PASSWORD`

**In local dev:** a `.env` file at repo root, never committed. Add `.env` to `.gitignore` (should already be there). Example `.env.example`:

```
BRIGHTDATA_CUSTOMER_ID=hl_xxxxxxxx
BRIGHTDATA_DC_ZONE=jugnu_dc_dev
BRIGHTDATA_DC_PASSWORD=change-me
BRIGHTDATA_RESI_ZONE=jugnu_resi_dev
BRIGHTDATA_RESI_PASSWORD=change-me
```

**In tests:** never hit the real Bright Data API in unit tests. Integration tests that do are gated by env var:

```python
@pytest.mark.skipif(not os.environ.get("BRIGHTDATA_INTEGRATION_TEST"), reason="live integration test")
def test_brightdata_live_fetch():
    ...
```

CI never sets `BRIGHTDATA_INTEGRATION_TEST`. Developers run it locally before merging provider changes.

---

## 10. Tests

### Unit tests — `tests/fetch/proxy/test_brightdata.py`

- `test_get_config_direct_returns_no_server` — DIRECT tier produces a ProxyConfig with `server=None`
- `test_get_config_datacenter_builds_username` — username matches `brd-customer-{id}-zone-{zone}-country-us-session-{sid}` pattern
- `test_get_config_residential_uses_residential_zone` — pulls from RESI_ZONE env, not DC_ZONE
- `test_session_id_stable_across_calls` — same canonical_id → same session_id
- `test_session_id_differs_by_canonical_id` — different canonical_id → different session_id
- `test_unblocker_raises_not_implemented`
- `test_missing_env_raises_at_init` — constructor raises clear error if `BRIGHTDATA_CUSTOMER_ID` unset
- `test_to_playwright_format` — output matches `{"server": ..., "username": ..., "password": ...}`
- `test_to_httpx_format_urlencodes_credentials` — password with special chars (`@`, `:`, `/`) is quoted

### Unit tests — `tests/fetch/proxy/test_escalation.py`

- `test_success_resets_fail_count_and_increments_success`
- `test_403_escalates_immediately_from_datacenter_to_residential`
- `test_429_escalates_immediately`
- `test_captcha_signal_escalates`
- `test_timeout_accumulates_before_escalating` — 1 timeout → no escalation; 3 timeouts → escalate
- `test_no_escalation_above_top_tier` — at UNBLOCKER, escalation sticks at UNBLOCKER
- `test_deescalation_after_success_streak` — 30 successes at RESIDENTIAL → DATACENTER
- `test_deescalation_not_from_direct` — at DIRECT, success streak doesn't try to drop below

### Unit tests — `tests/fetch/proxy/test_selector.py`

- `test_new_property_gets_default_tier`
- `test_existing_profile_tier_used`
- `test_corrupt_profile_tier_falls_back_to_default`
- `test_override_wins_over_profile`

### Integration test — `tests/fetch/proxy/test_brightdata_integration.py`

Gated by `BRIGHTDATA_INTEGRATION_TEST` env var. Fetches `https://geo.brdtest.com/welcome.txt` (Bright Data's test endpoint) through each non-direct tier and asserts the returned IP is not in a GCP CIDR block.

---

## 11. Gates

| Gate | Check | Command |
|---|---|---|
| BD-1 | Unit tests pass | `pytest tests/fetch/proxy/ -v` |
| BD-2 | Coverage ≥ 90% on proxy package | `pytest tests/fetch/proxy/ --cov=ma_poc.fetch.proxy --cov-fail-under=90` |
| BD-3 | mypy strict passes | `mypy --strict ma_poc/fetch/proxy/` |
| BD-4 | ruff clean | `ruff check ma_poc/fetch/proxy/` |
| BD-5 | Migration applies cleanly | `python scripts/migrate.py --env staging up` then verify `proxy_tier` column exists on `properties` |
| BD-6 | Migration round-trips | `pytest tests/migrations/` passes for the new migration |
| BD-7 | Live Bright Data integration | With valid staging credentials: `BRIGHTDATA_INTEGRATION_TEST=1 pytest tests/fetch/proxy/test_brightdata_integration.py` passes |
| BD-8 | End-to-end scrape with tier=datacenter | `python scripts/jugnu_runner.py --csv config/properties.csv --limit 5 --proxy datacenter` — all 5 properties return a `_meta.proxy_tier == "datacenter"` stamp |
| BD-9 | Escalation visible in events | In the same run, force a 403 on one property (mock a blocked URL); event log contains a `proxy_escalation` event |
| BD-10 | Cost ledger records tier | Same run; `cost_ledger.db` has rows with `proxy_tier` populated, not null |
| BD-11 | Playwright TLS errors handled | On a residential-proxied fetch, no `ERR_CERT_AUTHORITY_INVALID` errors appear in logs |
| BD-12 | No credential leak in logs | `grep -i password $(find data/v2/runs -name '*.jsonl')` returns nothing |
| BD-13 | No credential in cost_ledger or property profile | Same grep across those files |
| BD-14 | --proxy CLI flag breaking change documented | `docs/OPERATOR_RUNBOOK.md` notes that `--proxy` now takes a tier name, not a URL |

---

## 12. Non-negotiables

- **Never log the password.** Not at DEBUG level, not in exception messages, not even redacted — just don't put it in any log statement. One accidental `log.debug(f"config: {config}")` and your zone password is in a GCS artifact forever.
- **Never cache `ProxyConfig` objects across properties.** Each config has a session_id derived from the canonical_id; reusing across properties collapses the session model. Construct fresh per fetch.
- **No `verify=False` in requests/httpx outside of the proxy path.** `ignore_https_errors` is acceptable for Playwright contexts *only when* routing through a non-direct proxy, and gated accordingly. Anywhere else in the codebase, TLS verification stays on.
- **No hardcoded zone names.** Every zone reference comes from env. Zone names change between dev and prod, and hardcoding them means prod traffic goes to the dev zone (or vice versa).
- **The `--proxy` CLI flag's old URL-form is removed cleanly, not silently accepted.** If an operator passes `--proxy http://...`, the script errors with a clear migration message pointing at the runbook. Don't parse-and-ignore.
- **No fallback to `DIRECT` on credential-missing errors.** If `BRIGHTDATA_CUSTOMER_ID` is unset, fail at construction time. Silently degrading to DIRECT mid-run produces a bill surprise when you thought you were proxying.

---

## 13. Open questions to resolve before starting

- **Single residential zone, or per-country zones?** Bright Data's docs recommend one zone per geolocation. For US-only targets, one is fine. If the target list ever includes non-US properties, revisit. Recommendation: one zone (`jugnu_resi`) for v1.
- **Dedicated vs. shared residential pool?** Dedicated costs more but has cleaner IP reputation. Recommendation: shared for POC; revisit if block rates exceed 15% on residential tier.
- **Country defaults to US — is that right for every property?** The architecture doesn't target non-US markets in Phase 1, so yes. If the property list adds Canadian or UK properties, the provider interface already supports per-call country override.
- **Starting tier — DATACENTER or DIRECT?** Recommendation documented in §5: DATACENTER. The counter-argument (save money by starting DIRECT) assumes the Phase 1 no-proxy measurement has been done. If it hasn't, DATACENTER is safer.

---

## 14. When this handoff is complete

Claude Code has:
1. Created every file in §1
2. All 14 gates in §11 pass
3. The `docs/OPERATOR_RUNBOOK.md` section on `--proxy` reflects the new tier-name semantics
4. `BRIGHT_DATA_SETUP.md` (the human setup doc) is present alongside this handoff — Claude Code doesn't write it, but verifies it exists and the credential names match what the provider code expects
5. One real scrape run at `--limit 20` against staging, through the full DATACENTER → RESIDENTIAL escalation path, produces a cost-ledger entry within 2× of the manual invoice estimate

Only then mark this handoff done and move on to the UNBLOCKER tier handoff (future).
