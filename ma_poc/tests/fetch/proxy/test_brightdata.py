"""Unit tests for BrightDataProvider.

No network. Exercises the username format, session-id derivation, and
the error surface for missing credentials.
"""

from __future__ import annotations

import pytest

from ma_poc.fetch.proxy.base import ProxyTier
from ma_poc.fetch.proxy.brightdata import BrightDataProvider

_ENV_KEYS = (
    "BRIGHTDATA_CUSTOMER_ID",
    "BRIGHTDATA_DC_ZONE",
    "BRIGHTDATA_DC_PASSWORD",
    "BRIGHTDATA_RESI_ZONE",
    "BRIGHTDATA_RESI_PASSWORD",
)


@pytest.fixture
def bd_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIGHTDATA_CUSTOMER_ID", "hl_testcust")
    monkeypatch.setenv("BRIGHTDATA_DC_ZONE", "jugnu_dc_test")
    monkeypatch.setenv("BRIGHTDATA_DC_PASSWORD", "dc_pw")
    monkeypatch.setenv("BRIGHTDATA_RESI_ZONE", "jugnu_resi_test")
    monkeypatch.setenv("BRIGHTDATA_RESI_PASSWORD", "resi_pw")


def test_get_config_direct_returns_no_server(bd_env: None) -> None:
    prov = BrightDataProvider()
    cfg = prov.get_config(tier=ProxyTier.DIRECT, canonical_id="p1")
    assert cfg.tier is ProxyTier.DIRECT
    assert cfg.server is None
    assert cfg.is_direct


def test_get_config_datacenter_builds_username(bd_env: None) -> None:
    prov = BrightDataProvider()
    cfg = prov.get_config(tier=ProxyTier.DATACENTER, canonical_id="p1")
    assert cfg.server == "http://brd.superproxy.io:33335"
    assert cfg.password == "dc_pw"
    assert cfg.username is not None
    # Order is customer → zone → country → session
    assert cfg.username.startswith(
        "brd-customer-hl_testcust-zone-jugnu_dc_test-country-us-session-"
    )


def test_get_config_residential_uses_residential_zone(bd_env: None) -> None:
    prov = BrightDataProvider()
    cfg = prov.get_config(tier=ProxyTier.RESIDENTIAL, canonical_id="p1")
    assert cfg.password == "resi_pw"
    assert cfg.username is not None
    assert "zone-jugnu_resi_test" in cfg.username
    assert "zone-jugnu_dc_test" not in cfg.username


def test_session_id_stable_across_calls(bd_env: None) -> None:
    prov = BrightDataProvider()
    a = prov.get_config(tier=ProxyTier.DATACENTER, canonical_id="prop_abc")
    b = prov.get_config(tier=ProxyTier.DATACENTER, canonical_id="prop_abc")
    assert a.session_id == b.session_id
    assert a.username == b.username


def test_session_id_differs_by_canonical_id(bd_env: None) -> None:
    prov = BrightDataProvider()
    a = prov.get_config(tier=ProxyTier.DATACENTER, canonical_id="prop_abc")
    b = prov.get_config(tier=ProxyTier.DATACENTER, canonical_id="prop_xyz")
    assert a.session_id != b.session_id


def test_country_override_encodes_into_username(bd_env: None) -> None:
    prov = BrightDataProvider()
    cfg = prov.get_config(
        tier=ProxyTier.RESIDENTIAL, canonical_id="p1", country="CA"
    )
    assert cfg.username is not None
    assert "-country-ca-" in cfg.username


def test_unblocker_raises_not_implemented(bd_env: None) -> None:
    prov = BrightDataProvider()
    with pytest.raises(NotImplementedError):
        prov.get_config(tier=ProxyTier.UNBLOCKER, canonical_id="p1")


def test_missing_env_raises_at_init(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="BRIGHTDATA_CUSTOMER_ID"):
        BrightDataProvider()


def test_missing_resi_env_raises_only_on_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-05-24 refactor: tier zone env vars resolve lazily on first
    ``get_config(tier=...)`` call, not at ``__init__``. This lets
    deployments wired for only one tier (e.g. RESIDENTIAL-only via the
    L1 escalator) construct the provider even when the unused tier's
    secrets aren't provisioned.

    The error invariant survives: missing env still raises RuntimeError
    naming the offending key — just at the call site where the tier
    is actually requested.
    """
    monkeypatch.setenv("BRIGHTDATA_CUSTOMER_ID", "hl_x")
    monkeypatch.setenv("BRIGHTDATA_DC_ZONE", "dc")
    monkeypatch.setenv("BRIGHTDATA_DC_PASSWORD", "p")
    monkeypatch.setenv("BRIGHTDATA_RESI_ZONE", "resi")
    monkeypatch.delenv("BRIGHTDATA_RESI_PASSWORD", raising=False)
    # Construction with one tier's env present + the other missing must succeed.
    prov = BrightDataProvider()
    # Using the well-configured tier must also succeed.
    cfg_dc = prov.get_config(tier=ProxyTier.DATACENTER, canonical_id="p1")
    assert cfg_dc.tier == ProxyTier.DATACENTER
    # Using the unconfigured tier raises with the missing key named.
    with pytest.raises(RuntimeError, match="BRIGHTDATA_RESI_PASSWORD"):
        prov.get_config(tier=ProxyTier.RESIDENTIAL, canonical_id="p1")


def test_missing_dc_env_raises_only_on_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric to the RESI case — the residential-only deployment
    pattern that motivated the lazy refactor. Without this, enabling
    ENABLE_RESIDENTIAL_TIER without DC zone/password (the current
    production secret state on jugnu-scrape-production) crashes
    ``DcProxyProvider.__init__`` at startup because both providers
    construct a shared ``BrightDataProvider`` instance.
    """
    monkeypatch.setenv("BRIGHTDATA_CUSTOMER_ID", "hl_x")
    monkeypatch.setenv("BRIGHTDATA_RESI_ZONE", "resi")
    monkeypatch.setenv("BRIGHTDATA_RESI_PASSWORD", "p")
    monkeypatch.delenv("BRIGHTDATA_DC_ZONE", raising=False)
    monkeypatch.delenv("BRIGHTDATA_DC_PASSWORD", raising=False)
    prov = BrightDataProvider()
    cfg_resi = prov.get_config(tier=ProxyTier.RESIDENTIAL, canonical_id="p1")
    assert cfg_resi.tier == ProxyTier.RESIDENTIAL
    with pytest.raises(RuntimeError, match="BRIGHTDATA_DC_ZONE"):
        prov.get_config(tier=ProxyTier.DATACENTER, canonical_id="p1")


def test_zone_lookup_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second call for the same tier reuses the cached zone — the
    underlying env-var read fires once. Guards against accidental
    reintroduction of per-call ``os.environ.get`` overhead in the hot
    fetch path.
    """
    monkeypatch.setenv("BRIGHTDATA_CUSTOMER_ID", "hl_x")
    monkeypatch.setenv("BRIGHTDATA_RESI_ZONE", "resi")
    monkeypatch.setenv("BRIGHTDATA_RESI_PASSWORD", "p")
    prov = BrightDataProvider()
    cfg_1 = prov.get_config(tier=ProxyTier.RESIDENTIAL, canonical_id="p1")
    # Clearing the env between calls must NOT invalidate the cached zone.
    monkeypatch.delenv("BRIGHTDATA_RESI_PASSWORD", raising=False)
    cfg_2 = prov.get_config(tier=ProxyTier.RESIDENTIAL, canonical_id="p2")
    assert cfg_1.password == cfg_2.password == "p"


def test_to_playwright_format(bd_env: None) -> None:
    prov = BrightDataProvider()
    cfg = prov.get_config(tier=ProxyTier.DATACENTER, canonical_id="p1")
    pw = cfg.to_playwright()
    assert pw is not None
    assert set(pw.keys()) == {"server", "username", "password"}
    assert pw["server"] == "http://brd.superproxy.io:33335"


def test_to_httpx_format_urlencodes_credentials(bd_env: None) -> None:
    prov = BrightDataProvider()
    cfg = prov.get_config(tier=ProxyTier.RESIDENTIAL, canonical_id="p1")
    mapping = cfg.to_httpx()
    assert mapping is not None
    # The username contains hyphens and underscores only (safe), but check
    # the shape of the URL and that no raw '@' leaks from the auth part.
    assert mapping["http://"].startswith("http://brd-customer-hl_testcust")
    assert mapping["http://"].endswith("@brd.superproxy.io:33335")
