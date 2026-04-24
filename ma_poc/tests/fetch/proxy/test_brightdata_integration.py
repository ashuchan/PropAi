"""Live Bright Data integration test — env-gated.

Set ``BRIGHTDATA_INTEGRATION_TEST=1`` and all BRIGHTDATA_* credentials to
run. CI must NOT set this env var. The test fetches Bright Data's public
echo endpoint through each non-direct tier and checks that the returned
IP is not in the GCP egress CIDR (proves the proxy actually routed).
"""

from __future__ import annotations

import ipaddress
import os

import httpx
import pytest

from ma_poc.fetch.proxy.base import ProxyTier
from ma_poc.fetch.proxy.brightdata import BrightDataProvider

_GATE = os.environ.get("BRIGHTDATA_INTEGRATION_TEST")

pytestmark = pytest.mark.skipif(
    not _GATE,
    reason="BRIGHTDATA_INTEGRATION_TEST not set — live integration test skipped",
)

# A small sample of known Google/GCP egress ranges. A hit in one of these
# ranges means we went direct, not through the proxy. Not exhaustive —
# just enough to catch the "silent fall-through to direct" class of bug.
_GCP_CIDRS = (
    "35.184.0.0/13",
    "35.192.0.0/14",
    "35.196.0.0/15",
    "34.64.0.0/10",
)


def _is_gcp(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    return any(
        addr in ipaddress.ip_network(cidr, strict=False) for cidr in _GCP_CIDRS
    )


@pytest.mark.parametrize(
    "tier", [ProxyTier.DATACENTER, ProxyTier.RESIDENTIAL]
)
def test_brightdata_live_tier_routes_through_proxy(tier: ProxyTier) -> None:
    prov = BrightDataProvider()
    cfg = prov.get_config(tier=tier, canonical_id="integration-test")
    proxy_url = cfg.to_httpx_url()
    assert proxy_url is not None

    with httpx.Client(
        proxy=proxy_url,
        timeout=30.0,
        verify=False,  # Bright Data terminates TLS; integration test only.
    ) as client:
        resp = client.get("https://geo.brdtest.com/welcome.txt?product=resi")

    assert resp.status_code == 200, resp.text
    # The welcome doc contains "Country" and the exit IP on separate lines.
    assert "Country" in resp.text
    ip_line = next(
        (ln for ln in resp.text.splitlines() if ln.lower().startswith("ip:")),
        None,
    )
    if ip_line:
        ip = ip_line.split(":", 1)[1].strip()
        assert not _is_gcp(ip), f"tier={tier.value} routed through GCP IP {ip}"
