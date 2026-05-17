"""Centralised curl_cffi probe used by the securecafe / ResMan / Entrata-
subpage / detection-rescue paths.

2026-05-17 iter-13: these lightweight endpoint fetches (~0.1-1.2 MB)
are the ONLY thing that needs a residential proxy to clear Cloudflare
GCP-IP blocking — NOT the 100 MB patchright render. Routing just the
probe through ``PROBE_PROXY_URL`` keeps proxy bandwidth at ~0.6 MB/site
(~$2.5/run for the full RentCafe+ResMan pool @ $4/GB) instead of ~$850
if the full render were proxied.

``PROBE_PROXY_URL`` form: ``http://user:pass@host:port`` (BrightData
residential zone). Unset ⇒ direct (current proxy-less behaviour;
safe default, no functional change off-canary).
"""
from __future__ import annotations

import os
from typing import Any

_DEFAULTS = {"impersonate": "chrome120", "timeout": 25, "allow_redirects": True}


def probe_proxies() -> dict[str, str]:
    """``{"http":url,"https":url}`` from PROBE_PROXY_URL, or ``{}``."""
    u = os.getenv("PROBE_PROXY_URL", "").strip()
    return {"http": u, "https": u} if u else {}


def probe_get(url: str, **kw: Any) -> Any:
    """curl_cffi GET with chrome impersonation + optional probe proxy.

    Raises ImportError if curl_cffi is unavailable (callers already guard
    for that). Proxy + TLS-verify-relaxation only applied when
    PROBE_PROXY_URL is set (BrightData terminates TLS at its edge).
    """
    from curl_cffi import requests as _creq

    opts: dict[str, Any] = {**_DEFAULTS, **kw}
    px = probe_proxies()
    if px:
        opts.setdefault("proxies", px)
        opts.setdefault("verify", False)  # BrightData edge TLS termination
    return _creq.get(url, **opts)
