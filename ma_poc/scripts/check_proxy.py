"""check_proxy.py — in-container proxy diagnostic.

Fetches ``https://lumtest.com/myip.json`` through the first URL in
``PROXY_POOL_URLS`` and asserts the response's ASN is NOT Google's
(15169). A 15169 response means the request went out via Cloud Run's
default egress — i.e. the proxy was silently bypassed. Any other ASN
(AT&T, Comcast, Verizon, …) confirms traffic is actually traversing the
configured proxy.

Designed to run inside the jugnu-scrape Cloud Run job when
``SMOKE_MODE=proxy_check`` is set — ``jugnu_shard_entry.py`` dispatches
to this script and short-circuits the full scrape pipeline.

Exit codes:
  0   Proxy is working (non-Google IP observed)
  1   PROXY_POOL_URLS is unset / empty
  2   Fetch through proxy failed (timeout, TLS, auth, etc.)
  3   ASN came back as Google 15169 (proxy silently bypassed)
  4   Response shape was unexpected (no ``ip``/``asn`` keys)
"""

from __future__ import annotations

import json
import os
import re
import sys

import httpx

IP_CHECK_URL = "https://lumtest.com/myip.json"
GOOGLE_ASN = 15169
TIMEOUT_SECONDS = 30.0


def _redact(url: str) -> str:
    """Strip credentials from a proxy URL before printing."""
    return re.sub(r"://[^@]+@", "://***@", url)


def main() -> int:
    raw = os.getenv("PROXY_POOL_URLS", "").strip()
    if not raw:
        print("[proxy-check] FAIL — PROXY_POOL_URLS is unset or empty", file=sys.stderr)
        return 1

    # PROXY_POOL_URLS can be a comma-separated list (see ProxyPool).
    # The check itself only needs one working proxy — use the first.
    # Deliberately NOT stripping a leading BOM here: if the secret was
    # written with a BOM (PowerShell Out-File default), the production
    # fetcher fails too, and we want smoke to surface that loudly rather
    # than hide it and ship the broken secret.
    proxy_url = raw.split(",")[0].strip()
    print(f"[proxy-check] using proxy: {_redact(proxy_url)}", file=sys.stderr)
    print(f"[proxy-check] hitting: {IP_CHECK_URL}", file=sys.stderr)

    try:
        # httpx 0.27: ``proxy=`` (singular) replaced ``proxies=``. One
        # URL handles both http and https targets.
        with httpx.Client(proxy=proxy_url, timeout=TIMEOUT_SECONDS, verify=True) as client:
            r = client.get(IP_CHECK_URL)
            r.raise_for_status()
            body = r.json()
    except Exception as exc:  # noqa: BLE001 — any error here means proxy unusable
        print(f"[proxy-check] FAIL — fetch through proxy raised: {exc!r}", file=sys.stderr)
        return 2

    print(f"[proxy-check] response: {json.dumps(body)}", file=sys.stderr)

    ip = body.get("ip")
    asn_block = body.get("asn") or {}
    asn = asn_block.get("asnum") if isinstance(asn_block, dict) else None
    if not ip or asn is None:
        print(
            f"[proxy-check] FAIL — response missing ip/asn fields: {body!r}",
            file=sys.stderr,
        )
        return 4

    if asn == GOOGLE_ASN:
        print(
            f"[proxy-check] FAIL — ASN {asn} is Google. Traffic bypassed the "
            f"proxy and went out via Cloud Run egress.",
            file=sys.stderr,
        )
        return 3

    org = asn_block.get("org_name") if isinstance(asn_block, dict) else None
    country = body.get("country")
    print(
        f"[proxy-check] OK — ip={ip} asn={asn} org={org!r} country={country}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
