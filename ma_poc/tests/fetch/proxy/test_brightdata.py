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


def test_missing_resi_env_raises_at_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIGHTDATA_CUSTOMER_ID", "hl_x")
    monkeypatch.setenv("BRIGHTDATA_DC_ZONE", "dc")
    monkeypatch.setenv("BRIGHTDATA_DC_PASSWORD", "p")
    monkeypatch.setenv("BRIGHTDATA_RESI_ZONE", "resi")
    monkeypatch.delenv("BRIGHTDATA_RESI_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="BRIGHTDATA_RESI_PASSWORD"):
        BrightDataProvider()


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


# ─── session_salt — force-rotation extension (2026-05-24) ────────────


def test_session_salt_zero_reproduces_unsalted_session_id(bd_env: None) -> None:
    """Backward compat: salt=0 (default) must produce the same session-id
    as before — existing properties keep their sticky exit IPs."""
    prov = BrightDataProvider()
    a = prov.get_config(tier=ProxyTier.RESIDENTIAL, canonical_id="prop_xyz")
    b = prov.get_config(
        tier=ProxyTier.RESIDENTIAL, canonical_id="prop_xyz", session_salt=0
    )
    assert a.session_id == b.session_id


def test_session_salt_positive_produces_different_session_id(bd_env: None) -> None:
    """Bumping salt must change the session-id so BrightData hands out
    a different exit IP."""
    prov = BrightDataProvider()
    base = prov.get_config(tier=ProxyTier.RESIDENTIAL, canonical_id="prop_xyz")
    rotated = prov.get_config(
        tier=ProxyTier.RESIDENTIAL, canonical_id="prop_xyz", session_salt=1
    )
    assert base.session_id != rotated.session_id
    assert rotated.username is not None
    # The rotated session-id must still flow through into the username
    assert rotated.session_id in (rotated.username or "")


def test_session_salt_values_produce_distinct_ids(bd_env: None) -> None:
    """Distinct salts must produce distinct session-ids (no collisions
    in our 10-char hash slice within small salt range)."""
    prov = BrightDataProvider()
    ids = {
        prov.get_config(
            tier=ProxyTier.RESIDENTIAL, canonical_id="prop_xyz", session_salt=s
        ).session_id
        for s in range(0, 8)
    }
    assert len(ids) == 8, f"expected 8 distinct session-ids, got {len(ids)}"


def test_session_salt_same_salt_same_id(bd_env: None) -> None:
    """Same canonical_id + same salt = same session-id (deterministic)."""
    prov = BrightDataProvider()
    a = prov.get_config(
        tier=ProxyTier.RESIDENTIAL, canonical_id="prop_xyz", session_salt=3
    )
    b = prov.get_config(
        tier=ProxyTier.RESIDENTIAL, canonical_id="prop_xyz", session_salt=3
    )
    assert a.session_id == b.session_id


def test_session_salt_negative_rejected(bd_env: None) -> None:
    """Negative salts are nonsensical (would imply pre-rotation)."""
    prov = BrightDataProvider()
    with pytest.raises(ValueError, match="session_salt must be >= 0"):
        prov.get_config(
            tier=ProxyTier.RESIDENTIAL,
            canonical_id="prop_xyz",
            session_salt=-1,
        )


def test_session_salt_does_not_leak_across_canonical_ids(bd_env: None) -> None:
    """Property A with salt=1 and Property B with salt=0 must produce
    different session-ids — salt rotation must not collide across
    property boundaries."""
    prov = BrightDataProvider()
    a_salted = prov.get_config(
        tier=ProxyTier.RESIDENTIAL, canonical_id="prop_A", session_salt=1
    )
    b_clean = prov.get_config(
        tier=ProxyTier.RESIDENTIAL, canonical_id="prop_B", session_salt=0
    )
    assert a_salted.session_id != b_clean.session_id


def test_session_salt_dc_tier_also_honors_salt(bd_env: None) -> None:
    """Force-rotation works on the DC tier as well (some operators
    block DC IPs aggressively; same mechanism applies)."""
    prov = BrightDataProvider()
    base = prov.get_config(tier=ProxyTier.DATACENTER, canonical_id="prop_dc")
    rotated = prov.get_config(
        tier=ProxyTier.DATACENTER, canonical_id="prop_dc", session_salt=2
    )
    assert base.session_id != rotated.session_id
