"""Tier-aware HTTP client factory.

Both adapters (httpx and curl_cffi) expose the same ``request()`` interface so
the caller (``_do_request`` in fetcher.py) doesn't need to know which one ran.

Why two clients:
- DIRECT tier — no proxy, low cost, plain httpx is fine. JA3 fingerprint is
  the egress IP's; we don't pay for impersonation we don't need.
- DC_PROXY and above — the request crosses a proxy edge and is much more
  likely to hit Cloudflare's bot-management. curl_cffi with
  ``impersonate="chrome124"`` ships a Chrome JA3/JA4 fingerprint, which is
  the cheapest step that makes the request indistinguishable from a real
  browser at the TLS layer. Costs nothing per-request beyond linking against
  curl-impersonate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

try:
    from curl_cffi import requests as curl_cffi_requests
except ImportError:  # pragma: no cover - dependency guarded by S2 install
    curl_cffi_requests = None  # type: ignore[assignment]

from .contracts import FetchTier

log = logging.getLogger(__name__)

# Env-configurable impersonation target.
# Default: "chrome133" — safe for curl_cffi >=0.7.3.
# Override via CURL_CFFI_IMPERSONATE=chrome136 when curl_cffi >=0.8 is installed.
# Bump this in lockstep with the dominant Chrome version in _IDENTITIES when
# a curl_cffi release adds the corresponding impersonation target.
import os as _os

_DEFAULT_IMPERSONATE: str = _os.getenv("CURL_CFFI_IMPERSONATE", "chrome133")


@dataclass(frozen=True)
class _AdapterResponse:
    """Common response shape across both HTTP adapters."""

    status_code: int
    headers: dict[str, str]
    content: bytes
    final_url: str
    cookies: dict[str, str]


class _ClientAdapter(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        cookies: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> _AdapterResponse: ...

    async def aclose(self) -> None: ...


class _HttpxAdapter:
    """DIRECT-tier adapter wrapping httpx.AsyncClient."""

    def __init__(self, *, proxy: str | None) -> None:
        self._client = httpx.AsyncClient(
            proxy=proxy,
            follow_redirects=True,
            verify=True,
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        cookies: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> _AdapterResponse:
        resp = await self._client.request(
            method,
            url,
            headers=headers,
            cookies=cookies or {},
            timeout=timeout,
        )
        return _AdapterResponse(
            status_code=resp.status_code,
            headers={k.lower(): v for k, v in resp.headers.items()},
            content=resp.content,
            final_url=str(resp.url),
            cookies={c.name: c.value for c in resp.cookies.jar},
        )

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass


class _CurlCffiAdapter:
    """Proxied-tier adapter wrapping curl_cffi.requests.AsyncSession.

    Uses Chrome impersonation at the TLS handshake so the JA3/JA4 fingerprint
    matches a real Chrome browser. This is the cheap fix for the silent-403
    failure mode F3 classifies as BOT_BLOCKED.
    """

    def __init__(self, *, proxy: str | None, impersonate: str = _DEFAULT_IMPERSONATE) -> None:
        if curl_cffi_requests is None:
            raise RuntimeError(
                "curl_cffi is not installed — required for DC_PROXY+ tiers. "
                "Add to pyproject.toml and reinstall."
            )
        self._session = curl_cffi_requests.AsyncSession(
            impersonate=impersonate,
            proxy=proxy,
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        cookies: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> _AdapterResponse:
        resp = await self._session.request(
            method,
            url,
            headers=headers,
            cookies=cookies or {},
            timeout=timeout,
            allow_redirects=True,
        )
        # curl_cffi response surface differs from httpx; normalize.
        raw_content = resp.content
        if not isinstance(raw_content, bytes):
            raw_content = (resp.text or "").encode("utf-8", errors="replace")
        return _AdapterResponse(
            status_code=resp.status_code,
            headers={k.lower(): v for k, v in (resp.headers or {}).items()},
            content=raw_content,
            final_url=resp.url,
            cookies=dict(resp.cookies) if hasattr(resp, "cookies") else {},
        )

    async def aclose(self) -> None:
        try:
            await self._session.close()
        except Exception:
            # curl_cffi versions vary on close semantics; best-effort.
            pass


def make_http_client(tier: FetchTier, proxy: str | None) -> _HttpxAdapter | _CurlCffiAdapter:
    """Pick the right HTTP client for the tier.

    DIRECT → httpx (plain). DC_PROXY+ → curl_cffi with chrome impersonation.

    The caller is responsible for ``await client.aclose()``.

    Override (2026-05-13 — opt-in): set ``CURL_CFFI_FOR_DIRECT=1`` in the
    environment to use curl_cffi for the DIRECT tier as well. The
    2026-04-30 failure-recovery investigation demonstrated 95.2% Cloudflare
    bypass (258/271) with curl_cffi where plain httpx was 403-ing. Enabling
    this at DIRECT unlocks ~378 currently-CF-walled properties (134
    short-circuit + 244 Entrata-mis-classified, the latter mostly being
    CF-walled per the 2026-05-13 random-sample probe). The default stays
    on httpx so the switch is a deliberate ops decision, not a silent
    regression risk.
    """
    if tier == FetchTier.DIRECT:
        if _os.getenv("CURL_CFFI_FOR_DIRECT") in ("1", "true", "yes", "on"):
            if curl_cffi_requests is None:
                log.warning(
                    "CURL_CFFI_FOR_DIRECT requested but curl_cffi not "
                    "installed; falling back to httpx for DIRECT tier"
                )
                return _HttpxAdapter(proxy=proxy)
            return _CurlCffiAdapter(proxy=proxy)
        return _HttpxAdapter(proxy=proxy)
    return _CurlCffiAdapter(proxy=proxy)
