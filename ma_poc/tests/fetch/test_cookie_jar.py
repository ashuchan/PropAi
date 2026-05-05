"""S4 tests — cookie persistence, cf_clearance carry, slot scoping."""
from __future__ import annotations

from pathlib import Path

import pytest

from ma_poc.fetch.cookie_jar import PropertyCookieJar


def test_s4_cookie_jar_persists_across_fetches(tmp_path: Path) -> None:
    jar1 = PropertyCookieJar("PROP_001", 0, base_dir=tmp_path)
    jar1.update_from_response("rentcafe.com", {"sessionid": "abc123"})

    jar2 = PropertyCookieJar("PROP_001", 0, base_dir=tmp_path)
    cookies = jar2.cookies_for_host("rentcafe.com")
    assert cookies == {"sessionid": "abc123"}


def test_s4_cf_clearance_carried_forward(tmp_path: Path) -> None:
    jar1 = PropertyCookieJar("PROP_002", 0, base_dir=tmp_path)
    jar1.update_from_response(
        "www.rentcafe.com",
        {"cf_clearance": "L0NgT0kEn", "__cf_bm": "shorter"},
    )

    jar2 = PropertyCookieJar("PROP_002", 0, base_dir=tmp_path)
    cookies = jar2.cookies_for_host("www.rentcafe.com")
    assert cookies.get("cf_clearance") == "L0NgT0kEn"
    assert cookies.get("__cf_bm") == "shorter"


def test_s4_subdomain_match(tmp_path: Path) -> None:
    """A jar entry on `rentcafe.com` should serve `www.rentcafe.com`."""
    jar = PropertyCookieJar("PROP_003", 0, base_dir=tmp_path)
    jar.update_from_response("rentcafe.com", {"k": "v"})

    fresh = PropertyCookieJar("PROP_003", 0, base_dir=tmp_path)
    assert fresh.cookies_for_host("www.rentcafe.com") == {"k": "v"}


def test_s4_identity_rotation_does_not_share_cookies(tmp_path: Path) -> None:
    """Slot 0 and slot 1 for the same property must have separate jars."""
    jar_slot0 = PropertyCookieJar("PROP_004", 0, base_dir=tmp_path)
    jar_slot0.update_from_response("rentcafe.com", {"a": "1"})

    jar_slot1 = PropertyCookieJar("PROP_004", 1, base_dir=tmp_path)
    cookies = jar_slot1.cookies_for_host("rentcafe.com")
    assert cookies == {}, (
        "slot 1 must not see slot 0's cookies — identity rotation should "
        "produce a fresh jar so a banned identity's cookies don't follow"
    )


def test_s4_corrupt_jar_falls_back_empty(tmp_path: Path) -> None:
    """Garbled JSON must not raise — fail-soft to empty jar."""
    (tmp_path / "PROP_005_0.json").write_text("not json at all {[", encoding="utf-8")

    jar = PropertyCookieJar("PROP_005", 0, base_dir=tmp_path)
    assert jar.cookies_for_host("rentcafe.com") == {}


def test_s4_missing_jar_returns_empty(tmp_path: Path) -> None:
    jar = PropertyCookieJar("PROP_NONEXISTENT", 0, base_dir=tmp_path)
    assert jar.cookies_for_host("any.example.com") == {}


def test_s4_update_merges_without_losing_prior_cookies(tmp_path: Path) -> None:
    """Second update must merge, not replace."""
    jar = PropertyCookieJar("PROP_006", 0, base_dir=tmp_path)
    jar.update_from_response("example.com", {"a": "1"})
    jar.update_from_response("example.com", {"b": "2"})

    fresh = PropertyCookieJar("PROP_006", 0, base_dir=tmp_path)
    c = fresh.cookies_for_host("example.com")
    assert c == {"a": "1", "b": "2"}


def test_s4_cookies_scoped_to_host(tmp_path: Path) -> None:
    """Cookies from host A must not bleed into host B lookups."""
    jar = PropertyCookieJar("PROP_007", 0, base_dir=tmp_path)
    jar.update_from_response("site-a.com", {"x": "1"})

    fresh = PropertyCookieJar("PROP_007", 0, base_dir=tmp_path)
    assert fresh.cookies_for_host("site-b.com") == {}


def test_s4_jar_files_named_by_property_and_slot(tmp_path: Path) -> None:
    """Cookie file must be named {property_id}_{slot}.json."""
    jar = PropertyCookieJar("MY_PROP", 3, base_dir=tmp_path)
    jar.update_from_response("host.com", {"t": "v"})

    expected = tmp_path / "MY_PROP_3.json"
    assert expected.exists(), f"expected jar file at {expected}"
